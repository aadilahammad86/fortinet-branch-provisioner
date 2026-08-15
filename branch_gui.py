#!/usr/bin/env python3
"""FortiGate Branch Provisioner -- graphical front end.

A point-and-click wrapper around the same `fortigate` package the CLI scripts
use, so a field engineer never has to touch a command line. Built with Tkinter
(ships with Python) and packaged to a single .exe with PyInstaller.

Design notes:
  * Every device call runs on a worker thread and reports back through a queue.
    Doing them on the UI thread freezes the window mid-apply and Windows greys
    it out, which looks like a crash.
  * Validation runs before anything is sent. Subnet overlaps, oversized DHCP
    ranges and duplicate ports are caught locally rather than as a confusing
    error halfway through provisioning.
  * The office LAN is a separate, explicitly-confirmed action because changing
    it drops the very connection doing the work.

Run from source:   python branch_gui.py
Build the exe:     python build_exe.py
"""
import json
import queue
import sys
import threading
import traceback
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

from fortigate import FortiGate, LoginError, FortiGateError, load_env
from fortigate import branch, utm

APP_TITLE = "FortiGate Branch Provisioner"
APP_VERSION = "1.0"


def app_dir():
    """Where the operator's files live (backups, profiles, .env).

    Must be the folder holding the .exe, NOT the bundle. In a one-file build
    the bundle is a temp folder that Windows deletes when the app closes --
    writing config backups there would make them silently disappear.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def bundled(*parts):
    """A read-only file packaged inside the build (e.g. the user guide)."""
    base = Path(getattr(sys, "_MEIPASS", "")) if getattr(sys, "frozen", False) \
        else Path(__file__).resolve().parent
    return base.joinpath(*parts)


ROOT = app_dir()
PORTS = ["internal", "internal1", "internal2", "internal3", "internal4",
         "internal5", "dmz"]
SSL_MODES = [
    ("deep-inspection",
     "Deep inspection  (current branch standard - needs FortiGate CA on every device)"),
    ("certificate-inspection",
     "Certificate inspection  (blocks the same sites, no per-device setup)"),
    ("no-inspection", "No inspection  (HTTPS site blocking will NOT work)"),
]
DEFAULT_SSL = "deep-inspection"      # matches the CLI and the staged unit


# =========================================================================
#  Small helpers
# =========================================================================
class Field:
    """A labelled entry with optional live-computed hint text."""

    def __init__(self, parent, row, label, value="", width=22, show=None,
                 hint="", tooltip=""):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w",
                                           padx=(0, 8), pady=3)
        self.var = tk.StringVar(value=str(value))
        self.entry = ttk.Entry(parent, textvariable=self.var, width=width, show=show)
        self.entry.grid(row=row, column=1, sticky="w", pady=3)
        self.hint = ttk.Label(parent, text=hint, foreground="#666")
        self.hint.grid(row=row, column=2, sticky="w", padx=(10, 0))
        if tooltip:
            Tooltip(self.entry, tooltip)

    def get(self):
        return self.var.get().strip()

    def set(self, v):
        self.var.set(str(v))

    def set_hint(self, text, color="#666"):
        self.hint.configure(text=text, foreground=color)


class ScrollFrame(ttk.Frame):
    """A tab page that scrolls when it is taller than the space available.

    Dragging the log divider upwards shrinks the tab area, and without this the
    bottom of a tab (the Guest WiFi box, for instance) is simply cut off with no
    way to reach it. The scrollbar appears only when it is actually needed.

    Put content into `.body`.
    """

    def __init__(self, parent):
        super().__init__(parent)
        bg = ttk.Style().lookup("TFrame", "background") or "#f0f0f0"
        self.canvas = tk.Canvas(self, bd=0, highlightthickness=0, background=bg)
        self.vsb = ttk.Scrollbar(self, orient="vertical",
                                 command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self._sb_shown = False

        self.body = ttk.Frame(self.canvas, padding=16)
        self._win = self.canvas.create_window((0, 0), window=self.body,
                                              anchor="nw")

        self.body.bind("<Configure>", self._sync)
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        # Wheel is bound only while the pointer is over this page, so it does
        # not fight the log pane or the blocked-sites box for scroll events.
        self.bind("<Enter>", lambda _e: self.bind_all("<MouseWheel>", self._on_wheel))
        self.bind("<Leave>", lambda _e: self.unbind_all("<MouseWheel>"))

    def _on_canvas_resize(self, event):
        # Keep the page as wide as the viewport so inner layouts fill it.
        self.canvas.itemconfigure(self._win, width=event.width)
        self._sync()

    def _sync(self, _e=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        needed = self.body.winfo_reqheight() > self.canvas.winfo_height()
        if needed and not self._sb_shown:
            self.vsb.pack(side="right", fill="y")
            self._sb_shown = True
        elif not needed and self._sb_shown:
            self.vsb.pack_forget()
            self._sb_shown = False
            self.canvas.yview_moveto(0)

    def _on_wheel(self, event):
        if not self._sb_shown:
            return
        # Let a Text/Listbox under the pointer scroll itself instead.
        w = self.winfo_containing(event.x_root, event.y_root)
        while w is not None and w is not self.canvas:
            if isinstance(w, (tk.Text, tk.Listbox)):
                return
            w = getattr(w, "master", None)
        self.canvas.yview_scroll(-1 * (event.delta // 120), "units")


class Tooltip:
    """Minimal hover tooltip (no third-party dependency)."""

    def __init__(self, widget, text):
        self.widget, self.text, self.tip = widget, text, None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, _e=None):
        if self.tip:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, justify="left", background="#ffffe0",
                 relief="solid", borderwidth=1, wraplength=380,
                 font=("Segoe UI", 8)).pack(ipadx=4, ipady=2)

    def _hide(self, _e=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


# =========================================================================
#  Main window
# =========================================================================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_TITLE}  v{APP_VERSION}")
        # The Networks tab is the tallest page (~640px of content). Open tall
        # enough that it fits without scrolling on a normal screen, but never
        # taller than the screen itself. Scrolling covers everything smaller.
        height = max(620, min(920, self.winfo_screenheight() - 90))
        self.geometry(f"1020x{height}")
        self.minsize(880, 520)

        self.q = queue.Queue()
        self.busy = False
        self.device_info = None
        self.action_buttons = []

        self._build_ui()
        self._load_env_defaults()
        self.after(100, self._drain_queue)

    # ---- layout ---------------------------------------------------------
    def _build_ui(self):
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Head.TLabel", font=("Segoe UI", 11, "bold"))
        style.configure("Warn.TLabel", foreground="#a04000")
        style.configure("Good.TLabel", foreground="#1e7b1e")
        # Resizing is drag-only, so make the divider thick enough to grab
        # easily and give it a visible grip.
        try:
            style.configure("TPanedwindow", background="#c8ccd2")
            style.configure("Sash", sashthickness=7, gripcount=14,
                            handlesize=7, handlepad=6)
        except tk.TclError:
            pass

        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)

        # Status bar first, anchored to the bottom, so it stays visible no
        # matter where the operator drags the divider.
        bar = ttk.Frame(outer)
        bar.pack(side="bottom", fill="x", pady=(6, 0))

        # Tabs and log live in a draggable split -- grab the divider between
        # them to make the log taller for a long apply, or shrink it away.
        self.paned = ttk.PanedWindow(outer, orient="vertical")
        self.paned.pack(fill="both", expand=True)

        nb_holder = ttk.Frame(self.paned)
        self.nb = ttk.Notebook(nb_holder)
        self.nb.pack(fill="both", expand=True)
        self.paned.add(nb_holder, weight=3)

        self._tab_connect()
        self._tab_networks()
        self._tab_internet()
        self._tab_filtering()
        self._tab_apply()

        # ---- log pane ----
        logframe = ttk.LabelFrame(self.paned, text="Activity log  (drag the divider "
                                                   "above to resize)", padding=6)
        self.paned.add(logframe, weight=1)
        self.log_widget = scrolledtext.ScrolledText(
            logframe, height=10, wrap="word", font=("Consolas", 9), state="disabled")
        self.log_widget.pack(fill="both", expand=True)
        self.log_widget.tag_config("ok", foreground="#1e7b1e")
        self.log_widget.tag_config("fail", foreground="#b00020")
        self.log_widget.tag_config("warn", foreground="#a04000")
        self.log_widget.tag_config("head", font=("Consolas", 9, "bold"))

        self.status = ttk.Label(bar, text="Not connected.")
        self.status.pack(side="left")

        # The progress bar only exists while work is running. A stopped
        # indeterminate bar still paints a green block, which reads as "busy"
        # when nothing is happening -- so it is shown and hidden instead.
        # The holder keeps its size either way so the bar's arrival does not
        # shove the other controls sideways.
        self._prog_holder = ttk.Frame(bar, width=170, height=20)
        self._prog_holder.pack(side="right")
        self._prog_holder.pack_propagate(False)
        self.progress = ttk.Progressbar(self._prog_holder, mode="indeterminate")

        ttk.Button(bar, text="Clear log", command=self._clear_log,
                   width=10).pack(side="right", padx=8)

        # Put the divider somewhere sensible once the window has a real size.
        self.after(60, self._init_sash)

    def _init_sash(self):
        try:
            self.update_idletasks()
            total = self.paned.winfo_height()
            if total > 300:
                # Favour the form -- enough for the tallest tab (Networks) to
                # fit outright. The log still gets a readable slice and can be
                # dragged bigger whenever it is being watched.
                self.paned.sashpos(0, int(total * 0.79))
        except tk.TclError:
            pass

    # ---- tab 1: connect -------------------------------------------------
    def _page(self, label):
        """Add a scrollable notebook page and return its content frame."""
        sf = ScrollFrame(self.nb)
        self.nb.add(sf, text=label)
        return sf.body

    def _tab_connect(self):
        f = self._page("  1. Connect  ")
        ttk.Label(f, text="Connect to the FortiGate",
                  style="Head.TLabel").grid(row=0, column=0, columnspan=3,
                                            sticky="w", pady=(0, 10))
        self.f_host = Field(f, 1, "Device address", "192.168.1.99",
                            tooltip="A factory-default FortiGate is 192.168.1.99.\n"
                                    "A branch unit already set up is usually 172.21.0.1.")
        self.f_user = Field(f, 2, "Username", "admin")
        self.f_pass = Field(f, 3, "Password", "", show="*",
                            tooltip="A factory-default unit has a blank password.")

        btns = ttk.Frame(f)
        btns.grid(row=4, column=0, columnspan=4, sticky="w", pady=(14, 0))
        b = ttk.Button(btns, text="Test connection", command=self.act_test)
        b.pack(side="left")
        self.action_buttons.append(b)

        self.dev_label = ttk.Label(f, text="", justify="left")
        self.dev_label.grid(row=5, column=0, columnspan=4, sticky="w", pady=(14, 0))

        ttk.Separator(f, orient="horizontal").grid(row=6, column=0, columnspan=4,
                                                   sticky="ew", pady=16)

        # ---- config backup ----
        ttk.Label(f, text="Config backup", style="Head.TLabel").grid(
            row=7, column=0, columnspan=4, sticky="w", pady=(0, 4))
        ttk.Label(f, wraplength=820, justify="left", foreground="#555", text=(
            "Take a backup before changing anything — it is your restore point. "
            "The file is named after the device serial and the date.")).grid(
            row=8, column=0, columnspan=4, sticky="w", pady=(0, 10))

        ttk.Label(f, text="Save backups to").grid(row=9, column=0, sticky="w",
                                                  padx=(0, 8), pady=3)
        self.v_backup_dir = tk.StringVar(value=str(ROOT / "configs"))
        e = ttk.Entry(f, textvariable=self.v_backup_dir, width=52)
        e.grid(row=9, column=1, columnspan=2, sticky="we", pady=3)
        Tooltip(e, "Where config backup files are written. Defaults to a "
                   "'configs' folder next to this program.")
        ttk.Button(f, text="Browse…", width=11,
                   command=self.act_browse_backup_dir).grid(
            row=9, column=3, sticky="w", padx=(8, 0))

        row = ttk.Frame(f)
        row.grid(row=10, column=0, columnspan=4, sticky="w", pady=(12, 0))
        b2 = ttk.Button(row, text="Download config backup", command=self.act_backup)
        b2.pack(side="left")
        self.action_buttons.append(b2)
        ttk.Button(row, text="Open backup folder", width=19,
                   command=self.act_open_backup_dir).pack(side="left", padx=8)

        ttk.Separator(f, orient="horizontal").grid(row=11, column=0, columnspan=4,
                                                   sticky="ew", pady=16)
        ttk.Label(f, justify="left", wraplength=820, foreground="#555", text=(
            "The FortiGate uses a self-signed certificate, so this tool skips "
            "certificate verification — that is expected and not a problem on a "
            "direct cable connection.")).grid(
            row=12, column=0, columnspan=4, sticky="w")
        f.columnconfigure(2, weight=1)

    # ---- tab 2: networks ------------------------------------------------
    def _tab_networks(self):
        f = self._page("  2. Networks  ")

        # Office LAN
        lan = ttk.LabelFrame(f, text="Office LAN  (the network your PCs are on)",
                             padding=10)
        lan.pack(fill="x")
        self.v_lan_on = tk.BooleanVar(value=False)
        cb = ttk.Checkbutton(
            lan, text="Change the office LAN address  (this disconnects you — "
                      "use the button on the Apply tab)",
            variable=self.v_lan_on, command=self._refresh_hints)
        cb.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        self.lan_port = self._port_combo(lan, 1, "Port", "internal")
        self.f_lan_ip = Field(lan, 2, "Gateway IP", "172.21.0.1")
        self.f_lan_mask = Field(lan, 3, "Subnet mask", "255.255.248.0")
        self.f_lan_start = Field(lan, 4, "DHCP first address", "172.21.0.100")
        self.f_lan_end = Field(lan, 5, "DHCP last address", "172.21.0.230")

        # Staff WiFi
        staff = ttk.LabelFrame(f, text="Staff WiFi", padding=10)
        staff.pack(fill="x", pady=(9, 0))
        self.v_staff_on = tk.BooleanVar(value=True)
        ttk.Checkbutton(staff, text="Set up this network", variable=self.v_staff_on,
                        command=self._refresh_hints).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        self.staff_port = self._port_combo(staff, 1, "Port", "internal2")
        self.f_staff_ip = Field(staff, 2, "Gateway IP", "192.168.1.1",
                                tooltip="Devices on this network use this as their "
                                        "gateway. It becomes the .1 of a /24.")
        self.f_staff_n = Field(staff, 3, "Number of devices", "25", width=8)
        self.f_staff_first = Field(staff, 4, "First address number", "2", width=8)

        # Guest WiFi
        guest = ttk.LabelFrame(f, text="Guest WiFi  (internet only — isolated)",
                               padding=10)
        guest.pack(fill="x", pady=(9, 0))
        self.v_guest_on = tk.BooleanVar(value=True)
        ttk.Checkbutton(guest, text="Set up this network", variable=self.v_guest_on,
                        command=self._refresh_hints).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        self.guest_port = self._port_combo(guest, 1, "Port", "internal3")
        self.f_guest_ip = Field(guest, 2, "Gateway IP", "192.168.2.1")
        self.f_guest_n = Field(guest, 3, "Number of devices", "25", width=8)
        self.f_guest_first = Field(guest, 4, "First address number", "2", width=8)

        ttk.Label(f, style="Warn.TLabel", wraplength=880, justify="left", text=(
            "Guest WiFi gets internet and nothing else. It has no rule toward the "
            "office LAN or Staff WiFi, so the firewall blocks that by default — "
            "do not add one.")).pack(anchor="w", pady=(9, 0))

        for fld in (self.f_staff_ip, self.f_staff_n, self.f_staff_first,
                    self.f_guest_ip, self.f_guest_n, self.f_guest_first):
            fld.var.trace_add("write", lambda *_: self._refresh_hints())
        self._refresh_hints()

    def _port_combo(self, parent, row, label, value):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w",
                                          padx=(0, 8), pady=3)
        var = tk.StringVar(value=value)
        cb = ttk.Combobox(parent, textvariable=var, values=PORTS, width=19,
                          state="readonly")
        cb.grid(row=row, column=1, sticky="w", pady=3)
        cb.bind("<<ComboboxSelected>>", lambda _e: self._refresh_hints())
        return var

    # ---- tab 3: internet ------------------------------------------------
    def _tab_internet(self):
        f = self._page("  3. Internet  ")
        ttk.Label(f, text="Internet connection (wan1)",
                  style="Head.TLabel").grid(row=0, column=0, columnspan=3,
                                            sticky="w", pady=(0, 10))
        self.v_pppoe = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="Set wan1 to PPPoE (the usual branch ISP type)",
                        variable=self.v_pppoe).grid(row=1, column=0, columnspan=3,
                                                    sticky="w", pady=(0, 8))
        self.f_ppp_user = Field(f, 2, "ISP username", "", width=30,
                                tooltip="Leave blank to stage the unit centrally and "
                                        "have the engineer type it in on-site.")
        self.f_ppp_pass = Field(f, 3, "ISP password", "", width=30, show="*")
        ttk.Label(f, wraplength=820, justify="left", style="Warn.TLabel", text=(
            "Leave both blank for plug-and-play staging: wan1 is set to PPPoE and "
            "waits for credentials to be entered on-site.")).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Separator(f, orient="horizontal").grid(row=5, column=0, columnspan=3,
                                                   sticky="ew", pady=14)
        ttk.Label(f, wraplength=820, justify="left", text=(
            "Each branch uses ONE internet line, on wan1. All three inside networks "
            "share it with NAT.\n\n"
            "DNS needs no setup: wan1 takes the ISP's DNS servers automatically when "
            "PPPoE connects, and hands them to devices via DHCP.")).grid(
            row=6, column=0, columnspan=3, sticky="w")

    # ---- tab 4: filtering -----------------------------------------------
    def _tab_filtering(self):
        f = self._page("  4. Filtering  ")

        top = ttk.Frame(f)
        top.pack(fill="x")
        self.v_web = tk.BooleanVar(value=True)
        self.v_app = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="Block websites by name", variable=self.v_web
                        ).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(top, text="Block application categories", variable=self.v_app
                        ).grid(row=0, column=1, sticky="w", padx=(30, 0))

        ttk.Label(f, text="HTTPS inspection").pack(anchor="w", pady=(12, 2))
        self.v_ssl = tk.StringVar(value=DEFAULT_SSL)
        for val, label in SSL_MODES:
            ttk.Radiobutton(f, text=label, value=val, variable=self.v_ssl
                            ).pack(anchor="w")
        ttk.Label(f, style="Warn.TLabel", wraplength=880, justify="left", text=(
            "Deep inspection re-signs every secure site, so the FortiGate CA "
            "certificate must be installed on every phone and PC or browsers will "
            "block sites outright. Certificate inspection blocks the same sites with "
            "no per-device setup — it just cannot show a branded block page.")
                  ).pack(anchor="w", pady=(6, 0))

        body = ttk.Frame(f)
        body.pack(fill="both", expand=True, pady=(14, 0))

        urls = ttk.LabelFrame(body, text="Blocked websites (one per line)", padding=8)
        urls.pack(side="left", fill="both", expand=True)
        self.urls_text = scrolledtext.ScrolledText(urls, width=34, height=11,
                                                   font=("Consolas", 9))
        self.urls_text.pack(fill="both", expand=True)
        self.urls_text.insert("1.0", "\n".join(u for u, _ in utm.DEFAULT_URLS))
        ttk.Button(urls, text="Reset to defaults",
                   command=self._reset_urls).pack(anchor="w", pady=(6, 0))

        cats = ttk.LabelFrame(body, text="Blocked application categories", padding=8)
        cats.pack(side="left", fill="both", expand=True, padx=(12, 0))
        self.cat_vars = {}
        defaults = {cid for cid, _ in utm.DEFAULT_CATEGORIES}
        for i, (cid, name) in enumerate(utm.ALL_CATEGORIES):
            v = tk.BooleanVar(value=cid in defaults)
            self.cat_vars[cid] = v
            ttk.Checkbutton(cats, text=f"{name}  ({cid})", variable=v).grid(
                row=i % 9, column=i // 9, sticky="w", padx=(0, 18))

        ttk.Label(f, wraplength=880, justify="left", style="Warn.TLabel", text=(
            "YouTube is in Video/Audio, not Social.Media — the website list is what "
            "blocks it. Remote.Access includes RDP, VNC and Telnet going out to the "
            "internet.")).pack(anchor="w", pady=(10, 0))

    def _reset_urls(self):
        self.urls_text.delete("1.0", "end")
        self.urls_text.insert("1.0", "\n".join(u for u, _ in utm.DEFAULT_URLS))

    # ---- tab 5: apply ---------------------------------------------------
    def _tab_apply(self):
        f = self._page("  5. Apply  ")
        ttk.Label(f, text="Review and apply", style="Head.TLabel").pack(anchor="w")

        hn = ttk.Frame(f)
        hn.pack(fill="x", pady=(10, 4))
        self.f_hostname = Field(hn, 0, "Device name (optional)", "", width=30,
                                tooltip="Shows in the FortiGate interface and logs, "
                                        "e.g. FGT-BranchB.")

        row1 = ttk.Frame(f)
        row1.pack(fill="x", pady=(12, 0))
        for text, cmd, tip in (
            ("Check settings", self.act_validate,
             "Check the form for mistakes. Does not touch the device."),
            ("Preview changes", self.act_preview,
             "Compare your settings against the device and list what would change. "
             "Changes nothing."),
            ("Back up now", self.act_backup, "Save a full config backup first."),
        ):
            b = ttk.Button(row1, text=text, command=cmd, width=17)
            b.pack(side="left", padx=(0, 8))
            Tooltip(b, tip)
            if text != "Check settings":
                self.action_buttons.append(b)

        row2 = ttk.Frame(f)
        row2.pack(fill="x", pady=(10, 0))
        b = ttk.Button(row2, text="APPLY CONFIGURATION", command=self.act_apply, width=24)
        b.pack(side="left")
        Tooltip(b, "Apply everything except the office LAN address.")
        self.action_buttons.append(b)
        b = ttk.Button(row2, text="Verify", command=self.act_verify, width=14)
        b.pack(side="left", padx=8)
        Tooltip(b, "Read the device back and check every setting against this form.")
        self.action_buttons.append(b)

        lanbox = ttk.LabelFrame(
            f, text="Office LAN address change  (do this LAST)", padding=10)
        lanbox.pack(fill="x", pady=(18, 0))
        ttk.Label(lanbox, wraplength=860, justify="left", text=(
            "On a factory-default FortiGate the office LAN is 192.168.1.99, which "
            "clashes with a Staff WiFi on 192.168.1.x. The LAN has to move first, "
            "and moving it disconnects this program on purpose.\n\n"
            "After it runs: release and renew your network address, then reconnect "
            "to the new LAN address and continue.")).pack(anchor="w")
        b = ttk.Button(lanbox, text="Change office LAN address (disconnects me)",
                       command=self.act_lan, width=42)
        b.pack(anchor="w", pady=(8, 0))
        self.action_buttons.append(b)

        prof = ttk.Frame(f)
        prof.pack(fill="x", pady=(18, 0))
        ttk.Label(prof, text="Branch profile:").pack(side="left")
        ttk.Button(prof, text="Save…", command=self.act_save_profile,
                   width=10).pack(side="left", padx=6)
        ttk.Button(prof, text="Load…", command=self.act_load_profile,
                   width=10).pack(side="left")
        ttk.Label(prof, foreground="#666",
                  text="  (saves every setting except passwords)").pack(side="left")
        ttk.Button(prof, text="User guide", width=12,
                   command=self._open_docs).pack(side="right")

    def _open_docs(self):
        for doc in (bundled("docs", "gui-user-guide.md"),
                    ROOT / "docs" / "gui-user-guide.md"):
            if doc.exists():
                webbrowser.open(doc.as_uri())
                return
        messagebox.showinfo(
            APP_TITLE, "User guide not found next to the program.\n\n"
                       "It is docs/gui-user-guide.md in the project folder.")

    # =====================================================================
    #  Form <-> spec
    # =====================================================================
    def _int(self, field, default):
        try:
            return int(field.get())
        except ValueError:
            return default

    def spec_from_form(self):
        urls = [ln.strip() for ln in
                self.urls_text.get("1.0", "end").splitlines() if ln.strip()]
        cats = [cid for cid, v in self.cat_vars.items() if v.get()]
        return branch.BranchSpec(
            hostname=self.f_hostname.get(),
            lan_port=self.lan_port.get(), lan_ip=self.f_lan_ip.get(),
            lan_mask=self.f_lan_mask.get(), lan_start=self.f_lan_start.get(),
            lan_end=self.f_lan_end.get(), configure_lan=self.v_lan_on.get(),
            staff_port=self.staff_port.get(), staff_ip=self.f_staff_ip.get(),
            staff_clients=self._int(self.f_staff_n, 25),
            staff_first=self._int(self.f_staff_first, 2),
            configure_staff=self.v_staff_on.get(),
            guest_port=self.guest_port.get(), guest_ip=self.f_guest_ip.get(),
            guest_clients=self._int(self.f_guest_n, 25),
            guest_first=self._int(self.f_guest_first, 2),
            configure_guest=self.v_guest_on.get(),
            wan_pppoe=self.v_pppoe.get(), pppoe_user=self.f_ppp_user.get(),
            pppoe_pass=self.f_ppp_pass.get(),
            web_filter=self.v_web.get(), app_filter=self.v_app.get(),
            ssl_mode=self.v_ssl.get(),
            blocked_urls=urls, blocked_categories=cats,
        )

    def form_from_spec(self, d):
        def s(field, key):
            if key in d:
                field.set(d[key])

        s(self.f_hostname, "hostname")
        s(self.f_lan_ip, "lan_ip"); s(self.f_lan_mask, "lan_mask")
        s(self.f_lan_start, "lan_start"); s(self.f_lan_end, "lan_end")
        s(self.f_staff_ip, "staff_ip"); s(self.f_staff_n, "staff_clients")
        s(self.f_staff_first, "staff_first")
        s(self.f_guest_ip, "guest_ip"); s(self.f_guest_n, "guest_clients")
        s(self.f_guest_first, "guest_first")
        s(self.f_ppp_user, "pppoe_user")
        s(self.f_host, "host")
        for var, key in ((self.v_lan_on, "configure_lan"),
                         (self.v_staff_on, "configure_staff"),
                         (self.v_guest_on, "configure_guest"),
                         (self.v_pppoe, "wan_pppoe"),
                         (self.v_web, "web_filter"),
                         (self.v_app, "app_filter")):
            if key in d:
                var.set(bool(d[key]))
        for var, key in ((self.lan_port, "lan_port"), (self.staff_port, "staff_port"),
                         (self.guest_port, "guest_port"), (self.v_ssl, "ssl_mode"),
                         (self.v_backup_dir, "backup_dir")):
            if key in d:
                var.set(d[key])
        if "blocked_urls" in d:
            self.urls_text.delete("1.0", "end")
            self.urls_text.insert("1.0", "\n".join(d["blocked_urls"]))
        if "blocked_categories" in d:
            for cid, var in self.cat_vars.items():
                var.set(cid in d["blocked_categories"])
        self._refresh_hints()

    def _load_env_defaults(self):
        # A .env sitting next to the .exe pre-fills the connection boxes.
        try:
            cfg = load_env(ROOT / ".env")
        except OSError:
            return
        self.f_host.set(cfg.get("FGT_HOST", "192.168.1.99"))
        self.f_user.set(cfg.get("FGT_USER", "admin"))
        self.f_pass.set(cfg.get("FGT_PASSWORD", ""))
        self.f_ppp_user.set(cfg.get("FGT_PPPOE_USER", ""))
        self.f_ppp_pass.set(cfg.get("FGT_PPPOE_PASS", ""))
        if cfg.get("FGT_BACKUP_DIR"):
            self.v_backup_dir.set(cfg["FGT_BACKUP_DIR"])

    def _refresh_hints(self):
        try:
            spec = self.spec_from_form()
        except Exception:
            return
        for on, fld, rng in ((self.v_staff_on.get(), self.f_staff_n, spec.staff_range),
                             (self.v_guest_on.get(), self.f_guest_n, spec.guest_range)):
            if not on:
                fld.set_hint("(not being set up)")
                continue
            try:
                a, b = rng()
                fld.set_hint(f"gives {a} – {b}")
            except (ValueError, IndexError):
                fld.set_hint("check the gateway IP", "#b00020")

    # =====================================================================
    #  Threading plumbing
    # =====================================================================
    def log(self, msg, tag=None):
        self.q.put(("log", (msg, tag)))

    def _write_log(self, msg, tag=None):
        if tag is None:
            low = msg.lower()
            tag = ("fail" if "[fail]" in low or "[!]" in low or "error" in low
                   else "ok" if "[ok]" in low
                   else "warn" if "[skip]" in low or "[..]" in low
                   else None)
        self.log_widget.configure(state="normal")
        self.log_widget.insert("end", msg + "\n", tag or ())
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")

    def _clear_log(self):
        self.log_widget.configure(state="normal")
        self.log_widget.delete("1.0", "end")
        self.log_widget.configure(state="disabled")

    def _set_busy(self, busy, status=""):
        self.busy = busy
        for b in self.action_buttons:
            b.configure(state="disabled" if busy else "normal")
        if busy:
            self.progress.pack(fill="both", expand=True)
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.pack_forget()          # nothing running, nothing shown
        if status:
            self.status.configure(text=status)

    def _run(self, name, fn):
        """Run `fn(fg, log)` on a worker thread with a fresh connection."""
        if self.busy:
            return
        host, user, pw = self.f_host.get(), self.f_user.get(), self.f_pass.get()
        if not host:
            messagebox.showwarning(APP_TITLE, "Enter the device address first.")
            return
        self._set_busy(True, f"{name}…")
        self.log(f"\n=== {name} ===", "head")

        def worker():
            try:
                fg = FortiGate(host, user, pw)
                fn(fg, self.log)
                self.q.put(("done", name))
            except LoginError as e:
                self.q.put(("error", str(e)))
            except FortiGateError as e:
                self.q.put(("error", str(e)))
            except Exception:                       # noqa: BLE001 - surface anything
                self.q.put(("error", traceback.format_exc(limit=4)))

        threading.Thread(target=worker, daemon=True).start()

    def _drain_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    msg, tag = payload
                    self._write_log(msg, tag)
                elif kind == "done":
                    self._set_busy(False, f"{payload}: finished.")
                    self._write_log(f"--- {payload}: finished ---", "head")
                elif kind == "error":
                    self._set_busy(False, "Failed — see the log.")
                    self._write_log(f"[!] {payload}", "fail")
                    messagebox.showerror(APP_TITLE, str(payload)[:1200])
                elif kind == "device":
                    self.device_info = payload
                    self.dev_label.configure(text=payload)
        except queue.Empty:
            pass
        self.after(100, self._drain_queue)

    # =====================================================================
    #  Actions
    # =====================================================================
    def act_test(self):
        def job(fg, log):
            st = fg.status()
            text = (f"Connected.\n"
                    f"    Device   : {st['hostname']}  ({st['model']})\n"
                    f"    Serial   : {st['serial']}\n"
                    f"    Firmware : {st['version']}")
            self.q.put(("device", text))
            log(f"[ok] connected to {st['hostname']} — serial {st['serial']}, "
                f"firmware {st['version']}")
            lic = utm.licence_state(fg)
            if lic:
                pending = [k for k, v in lic.items() if v != "valid"]
                if pending:
                    log(f"[skip] FortiGuard licences not active: {', '.join(pending)}. "
                        f"Website blocking still works (it is a fixed list). "
                        f"Application blocking works but will not receive updates "
                        f"until the unit is registered online.")
            try:
                members = sorted(p["name"] for v in
                                 fg.results("/api/v2/cmdb/system/virtual-switch")
                                 for p in v.get("port", []))
                if members:
                    log(f"[ok] ports currently in the built-in switch: "
                        f"{', '.join(members)} (they are separated automatically "
                        f"when needed)")
            except FortiGateError:
                pass
        self._run("Test connection", job)

    def backup_dir(self):
        return Path(self.v_backup_dir.get().strip() or (ROOT / "configs"))

    def act_browse_backup_dir(self):
        start = self.backup_dir()
        while not start.exists() and start.parent != start:
            start = start.parent
        chosen = filedialog.askdirectory(
            title="Choose where to save config backups",
            initialdir=str(start), mustexist=False)
        if chosen:
            self.v_backup_dir.set(str(Path(chosen)))
            self._write_log(f"[ok] backups will be saved to: {chosen}", "ok")

    def act_open_backup_dir(self):
        d = self.backup_dir()
        if not d.exists():
            messagebox.showinfo(
                APP_TITLE,
                f"That folder does not exist yet:\n{d}\n\n"
                f"It is created the first time you download a backup.")
            return
        webbrowser.open(d.as_uri())

    def act_backup(self):
        dest = self.backup_dir()
        try:
            dest.mkdir(parents=True, exist_ok=True)
            probe = dest / ".write-test"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except OSError as e:
            messagebox.showerror(
                APP_TITLE,
                f"Cannot save backups to:\n{dest}\n\n{e}\n\n"
                f"Pick a different folder with Browse.")
            return

        def job(fg, log):
            out, st, size, lines = fg.backup(dest)
            log(f"[ok] backup saved: {out}")
            log(f"     {st['hostname']} / {st['serial']} / {st['version']} — "
                f"{size:,} bytes, {lines:,} lines")
        self._run("Config backup", job)

    def act_validate(self):
        self._write_log("\n=== Check settings ===", "head")
        errs = branch.validate(self.spec_from_form())
        if not errs:
            self._write_log("[ok] all settings look valid.", "ok")
            self.status.configure(text="Settings look valid.")
            return
        for e in errs:
            self._write_log(f"[!] {e}", "fail")
        self.status.configure(text=f"{len(errs)} problem(s) found.")
        messagebox.showwarning(APP_TITLE, "Please fix:\n\n• " + "\n• ".join(errs))

    def _guard(self):
        errs = branch.validate(self.spec_from_form())
        if errs:
            for e in errs:
                self._write_log(f"[!] {e}", "fail")
            messagebox.showwarning(APP_TITLE, "Please fix:\n\n• " + "\n• ".join(errs))
            return False
        return True

    def act_preview(self):
        if not self._guard():
            return
        spec = self.spec_from_form()

        def job(fg, log):
            changes, same = branch.preview(fg, spec)
            if not changes:
                log("[ok] the device already matches these settings — "
                    "nothing would change.")
            else:
                log(f"{len(changes)} change(s) would be made:")
                for c in changes:
                    log(f"    CHANGE  {c}", "warn")
            log(f"{len(same)} setting(s) already correct.")
        self._run("Preview changes", job)

    def act_apply(self):
        if not self._guard():
            return
        spec = self.spec_from_form()
        bits = []
        if spec.configure_staff:
            a, b = spec.staff_range()
            bits.append(f"Staff WiFi {spec.staff_port} → {spec.staff_ip} (DHCP {a}–{b})")
        if spec.configure_guest:
            a, b = spec.guest_range()
            bits.append(f"Guest WiFi {spec.guest_port} → {spec.guest_ip} (DHCP {a}–{b})")
        if spec.wan_pppoe:
            bits.append("wan1 → PPPoE" +
                        (" with ISP credentials" if spec.pppoe_user else
                         " (credentials entered on-site)"))
        if spec.web_filter:
            bits.append("Website blocking on office LAN + Staff WiFi")
        if spec.app_filter:
            bits.append("Application blocking on office LAN + Staff WiFi")
        if spec.web_filter or spec.app_filter:
            bits.append(f"HTTPS inspection: {spec.ssl_mode}")
        if not messagebox.askokcancel(
                APP_TITLE,
                "Apply this configuration?\n\n• " + "\n• ".join(bits) +
                "\n\nThe office LAN address is NOT changed by this button."):
            return

        def job(fg, log):
            branch.provision(fg, spec, log, apply_filters=utm.apply_filters)
            log("")
            log("ON-SITE STEPS REMAINING:", "head")
            log("  1. Enter the ISP username/password on wan1, connect the WAN cable.")
            log(f"  2. Plug the Staff WiFi access point into {spec.staff_port}.")
            if spec.configure_guest:
                log(f"  3. Plug the Guest WiFi access point into {spec.guest_port}.")
            if spec.ssl_mode == "deep-inspection" and (spec.web_filter or spec.app_filter):
                log("  4. Install the FortiGate CA certificate (Fortinet_CA_SSL) on "
                    "every device, or secure sites will be blocked.", "warn")
        self._run("Apply configuration", job)

    def act_verify(self):
        spec = self.spec_from_form()

        def job(fg, log):
            results = branch.verify(fg, spec, utm_mod=utm)
            passed = sum(1 for _, ok, _ in results if ok)
            for label, ok, detail in results:
                log(f"  [{'PASS' if ok else 'FAIL'}] {label:<44} {detail}",
                    "ok" if ok else "fail")
            log("")
            log(f"RESULT: {passed}/{len(results)} checks passed"
                + ("  — ALL GOOD" if passed == len(results) else "  — SEE FAILURES"),
                "ok" if passed == len(results) else "fail")
        self._run("Verify configuration", job)

    def act_lan(self):
        spec = self.spec_from_form()
        if not spec.configure_lan:
            messagebox.showinfo(
                APP_TITLE,
                "Tick 'Change the office LAN address' on the Networks tab first, "
                "and check the address and DHCP range there.")
            return
        errs = branch.validate(spec)
        if errs:
            messagebox.showwarning(APP_TITLE, "Please fix:\n\n• " + "\n• ".join(errs))
            return
        if not messagebox.askokcancel(APP_TITLE, (
                f"Change the office LAN to {spec.lan_ip} {spec.lan_mask}?\n\n"
                f"DHCP will hand out {spec.lan_start} – {spec.lan_end}.\n\n"
                f"THIS WILL DISCONNECT THIS PROGRAM. That is expected.\n\n"
                f"Afterwards:\n"
                f"  1. Open Command Prompt and run:  ipconfig /release\n"
                f"     then:  ipconfig /renew\n"
                f"  2. Come back here, set the device address to {spec.lan_ip}\n"
                f"     and press Test connection.")):
            return

        def job(fg, log):
            branch.lan_phase(fg, spec, log)
            log("")
            log("NEXT STEPS:", "head")
            log("  1. Run:  ipconfig /release   then   ipconfig /renew")
            log(f"  2. Set the device address on the Connect tab to {spec.lan_ip}")
            log("  3. Press Test connection, then Apply configuration.")
        self._run("Change office LAN", job)
        self.f_host.set(spec.lan_ip)

    # ---- profiles -------------------------------------------------------
    def act_save_profile(self):
        path = filedialog.asksaveasfilename(
            title="Save branch profile", defaultextension=".json",
            filetypes=[("Branch profile", "*.json")],
            initialfile=(self.f_hostname.get() or "branch") + ".json")
        if not path:
            return
        spec = self.spec_from_form()
        data = {k: v for k, v in vars(spec).items()
                if k not in ("pppoe_pass",)}
        data["host"] = self.f_host.get()
        data["backup_dir"] = self.v_backup_dir.get()
        try:
            Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as e:
            messagebox.showerror(APP_TITLE, f"Could not save:\n{e}")
            return
        self._write_log(f"[ok] profile saved: {path}", "ok")

    def act_load_profile(self):
        path = filedialog.askopenfilename(
            title="Load branch profile", filetypes=[("Branch profile", "*.json")])
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            messagebox.showerror(APP_TITLE, f"Could not load:\n{e}")
            return
        self.form_from_spec(data)
        self._write_log(f"[ok] profile loaded: {path}  "
                        f"(passwords are never stored — re-enter them)", "ok")


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
