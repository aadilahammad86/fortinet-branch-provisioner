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
import queue
import sys
import threading
import traceback
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext, simpledialog

from fortigate import FortiGate, LoginError, FortiGateError, load_env
from fortigate import appctrl, branch, ddns, templates, utm, vpn
from fortigate.templates import TemplateError

APP_TITLE = "FortiGate Branch Provisioner"
APP_VERSION = "1.3.0-rc5"


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


class AppBrowser(tk.Toplevel):
    """The device's own application signature database, searchable.

    Exists because blocking by category is a trap: Telegram is filed under
    Collaboration, not Social.Media, so a sensor that blocks Social.Media
    misses it entirely. Being able to see the real category -- and block one
    application without blocking its whole category -- is the difference
    between believing something is blocked and it being blocked.

    Nothing here touches the device until 'Save to firewall'.
    """

    COLS = [("name", "Application", 260), ("category", "Category", 150),
            ("technology", "Technology", 130), ("popularity", "Popularity", 90),
            ("risk", "Risk", 60), ("blocked", "Blocked", 80)]

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Application signatures")
        self.geometry("1000x620")
        self.sigs = []
        self.blocked = set()          # individual app ids, local until saved
        self.blocked_cats = set()     # category ids blocked wholesale
        self.categories = []
        self.dirty = False

        bar = ttk.Frame(self, padding=10)
        bar.pack(fill="x")
        ttk.Label(bar, text="Search").pack(side="left")
        self.v_search = tk.StringVar()
        e = ttk.Entry(bar, textvariable=self.v_search, width=24)
        e.pack(side="left", padx=(6, 12))
        e.bind("<KeyRelease>", lambda _e: self._fill())
        ttk.Label(bar, text="Category").pack(side="left")
        self.v_cat = tk.StringVar(value="All categories")
        self.cat_box = ttk.Combobox(bar, textvariable=self.v_cat, width=28,
                                    state="readonly", values=["All categories"])
        self.cat_box.pack(side="left", padx=(6, 12))
        self.cat_box.bind("<<ComboboxSelected>>", lambda _e: self._fill())
        self.v_only_blocked = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Blocked only", variable=self.v_only_blocked,
                        command=self._fill).pack(side="left")
        ttk.Button(bar, text="Load from firewall", width=18,
                   command=self.app.act_apps_load).pack(side="right")

        self.count = ttk.Label(self, foreground="#555", padding=(10, 0))
        self.count.pack(anchor="w")

        holder = ttk.Frame(self, padding=(10, 6))
        holder.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(holder, columns=[c for c, _t, _w in self.COLS],
                                 show="headings", selectmode="extended")
        for key, title, width in self.COLS:
            self.tree.heading(key, text=title,
                              command=lambda k=key: self._sort(k))
            self.tree.column(key, width=width,
                             anchor="center" if key in ("popularity", "risk",
                                                        "blocked") else "w")
        vsb = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.tag_configure("blocked", foreground="#b00020")
        self.tree.tag_configure("bycat", foreground="#a04000")
        self.tree.bind("<Double-1>", lambda _e: self._toggle())

        btns = ttk.Frame(self, padding=10)
        btns.pack(fill="x")
        for text, cmd, tip in (
            ("Block selected", lambda: self._set_selected(True),
             "Add the highlighted applications to the block list. "
             "Double-clicking a row does the same."),
            ("Unblock selected", lambda: self._set_selected(False), ""),
            ("Block everything shown", self._block_shown,
             "Blocks every row currently listed -- use it with a category "
             "chosen to block a whole category by name."),
        ):
            b = ttk.Button(btns, text=text, command=cmd, width=22)
            b.pack(side="left", padx=(0, 6))
            if tip:
                Tooltip(b, tip)
        ttk.Button(btns, text="Close", width=10,
                   command=self.destroy).pack(side="right")
        self.save_btn = ttk.Button(btns, text="Save to firewall", width=18,
                                   command=self.app.act_apps_save)
        self.save_btn.pack(side="right", padx=6)
        Tooltip(self.save_btn, "Write the block list to the branch application "
                               "sensor. Nothing is sent until you press this.")

        self._sort_key, self._sort_rev = "name", False
        self._status("Press 'Load from firewall' to read the signature "
                     "database from the device.")

    # -- data ------------------------------------------------------------
    def populate(self, sigs, blocked, blocked_cats=(), source=""):
        self.sigs = sigs
        self.blocked = set(blocked)
        self.blocked_cats = set(blocked_cats)
        self.categories = appctrl.categories(sigs)
        names = {s["category"]: s["category_id"] for s in sigs}
        self.cat_box.configure(values=["All categories"] + [
            f"{n}  ({c})" + ("  - BLOCKED" if names.get(n) in self.blocked_cats
                             else "")
            for n, c in self.categories])
        self.dirty = False
        self._fill(note=f"read from {source}" if source else "")

    def _is_blocked(self, row):
        """How this app is blocked: by its whole category, on its own, or not.

        A category block covers every signature in it -- 181 apps for
        Social.Media alone -- so showing only the individual overrides hides
        almost everything that is actually blocked.
        """
        if row["category_id"] in self.blocked_cats:
            return "CATEGORY"
        if row["id"] in self.blocked:
            return "app"
        return ""

    def _rows(self):
        cat = self.v_cat.get()
        cat = "" if cat.startswith("All") else cat.split("  (")[0]
        rows = appctrl.search(self.sigs, self.v_search.get(), cat)
        if self.v_only_blocked.get():
            rows = [r for r in rows if self._is_blocked(r)]
        key = self._sort_key
        rows.sort(key=lambda r: (str(r[key]).lower() if isinstance(r[key], str)
                                 else r[key]), reverse=self._sort_rev)
        return rows

    def _fill(self, note=""):
        self.tree.delete(*self.tree.get_children())
        rows = self._rows()
        for r in rows[:4000]:
            how = self._is_blocked(r)
            self.tree.insert(
                "", "end", iid=str(r["id"]),
                values=(r["name"], r["category"], r["technology"],
                        "*" * r["popularity"], r["risk"], how),
                tags=(("bycat",) if how == "CATEGORY"
                      else ("blocked",) if how else ()))
        self._status(note)

    def _status(self, note=""):
        shown = len(self.tree.get_children())
        by_cat = sum(1 for s in self.sigs
                     if s["category_id"] in self.blocked_cats)
        solo = sum(1 for s in self.sigs if s["id"] in self.blocked
                   and s["category_id"] not in self.blocked_cats)
        bits = [f"{len(self.sigs)} signatures on this firewall",
                f"showing {shown}",
                f"{by_cat + solo} blocked  ({by_cat} by category, "
                f"{solo} individually)"]
        if self.dirty:
            bits.append("UNSAVED -- press 'Save to firewall'")
        if note:
            bits.append(note)
        self.count.configure(
            text="   |   ".join(bits),
            foreground="#b00020" if self.dirty else "#555")

    # -- editing (local only) ---------------------------------------------
    def _set_selected(self, on):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Application signatures",
                                "Highlight one or more rows first.")
            return
        for iid in sel:
            self.blocked.add(int(iid)) if on else self.blocked.discard(int(iid))
        self.dirty = True
        self._fill()
        self.tree.selection_set(sel)

    def _toggle(self):
        for iid in self.tree.selection():
            i = int(iid)
            self.blocked.discard(i) if i in self.blocked else self.blocked.add(i)
        self.dirty = True
        self._fill()

    def _block_shown(self):
        rows = self._rows()
        if not rows or not messagebox.askokcancel(
                "Application signatures",
                f"Block all {len(rows)} applications currently listed?"):
            return
        self.blocked |= {r["id"] for r in rows}
        self.dirty = True
        self._fill()

    def _block_messaging(self):
        self._block_set(appctrl.MESSAGING_APPS, "messaging")

    def _block_social(self):
        self._block_set(appctrl.SOCIAL_APPS, "social media")

    def _block_set(self, names, label):
        """Select a named set, reporting per name what it matched and why."""
        if not self.sigs:
            messagebox.showinfo("Application signatures",
                                "Press 'Load from firewall' first.")
            return
        report = appctrl.resolve_report(self.sigs, names)
        self.app._write_log(f"Matching the {label} list against this "
                            f"firewall's {len(self.sigs)} signatures:", "head")
        added, missed = set(), []
        for want, hits, how in report:
            if not hits:
                missed.append(want)
                self.app._write_log(f"  {want:<20} NOT IN THIS FIRMWARE", "warn")
                continue
            added |= {s["id"] for s in hits}
            shown = ", ".join(s["name"] for s in hits[:6])
            more = f" (+{len(hits) - 6} more)" if len(hits) > 6 else ""
            self.app._write_log(
                f"  {want:<20} {len(hits):>3} signature(s) [{how}]: "
                f"{shown}{more}", "ok")

        new = added - self.blocked
        self.blocked |= added
        self.dirty = bool(new) or self.dirty
        self.v_only_blocked.set(True)          # show the result immediately
        self._fill(note=f"{len(new)} newly selected from the {label} list")
        self.app._write_log(
            f"[ok] {len(added)} signature(s) selected, {len(new)} of them new. "
            f"NOTHING IS SENT until you press 'Save to firewall'.",
            "ok" if new else "warn")
        if missed:
            self.app._write_log(
                f"[!] {len(missed)} name(s) have no signature in this "
                f"firmware: {', '.join(missed)}. That is usually because the "
                f"signature database has never updated -- it needs the unit "
                f"registered with FortiCare.", "warn")
        messagebox.showinfo(
            "Application signatures",
            f"{len(added)} {label} signature(s) selected"
            f"{f', {len(missed)} name(s) not in this firmware' if missed else ''}."
            f"\n\nThey are ticked locally and shown in the list now.\n"
            f"Press 'Save to firewall' to actually block them.")

    def _sort(self, key):
        self._sort_rev = not self._sort_rev if key == self._sort_key else False
        self._sort_key = key
        self._fill()


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
        self.app_browser = None
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

        # Saved branches live above the tabs, not inside one: picking a branch
        # refills fields on every tab, so it must be reachable from all of them.
        self._branch_bar(outer)

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
        self._tab_ddns()
        self._tab_vpn()

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

    # ---- saved-branch bar ------------------------------------------------
    def _branch_bar(self, parent):
        """Pick a saved branch and the whole form fills in from it."""
        bar = ttk.LabelFrame(parent, text="Saved branches", padding=8)
        bar.pack(fill="x", pady=(0, 8))

        ttk.Label(bar, text="Branch:").pack(side="left")
        self.v_branch = tk.StringVar()
        self.branch_box = ttk.Combobox(bar, textvariable=self.v_branch, width=30,
                                       state="readonly", values=[])
        self.branch_box.pack(side="left", padx=(6, 8))
        self.branch_box.bind("<<ComboboxSelected>>", lambda _e: self.act_branch_load())
        Tooltip(self.branch_box,
                "Every branch you have saved. Choosing one fills in every "
                "setting on every tab.\nPasswords are never saved — re-type them "
                "on the Connect tab.")

        for text, cmd, width, tip in (
            ("Save as new…", self.act_branch_save_as, 14,
             "Save the settings on the tabs as a new branch you can pick again later."),
            ("Update", self.act_branch_update, 9,
             "Overwrite the selected branch with what is on the tabs now."),
            ("Delete", self.act_branch_delete, 9,
             "Remove the selected branch from the list. The device is not touched."),
        ):
            b = ttk.Button(bar, text=text, command=cmd, width=width)
            b.pack(side="left", padx=(0, 6))
            Tooltip(b, tip)

        self.branch_hint = ttk.Label(bar, text="", foreground="#666")
        self.branch_hint.pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Folder", width=8,
                   command=self.act_branch_folder).pack(side="right")
        self._refresh_branch_list()

    def _refresh_branch_list(self, select=None):
        try:
            names = templates.list_names(ROOT)
        except TemplateError as e:
            self._write_log(f"[!] {e}", "fail")
            names = []
        self.branch_box.configure(values=names)
        if select is not None:
            self.v_branch.set(select)
        elif self.v_branch.get() not in names:
            self.v_branch.set("")
        self.branch_hint.configure(
            text=(f"{len(names)} saved" if names
                  else "none saved yet — set up a branch, then 'Save as new…'"))

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

        self.url_count = ttk.Label(urls, foreground="#666", text="")
        self.url_count.pack(anchor="w", pady=(4, 0))
        self.urls_text.bind("<KeyRelease>", lambda _e: self._count_urls())

        # Add a whole category at a time rather than typing 130 lines.
        add = ttk.Frame(urls)
        add.pack(fill="x", pady=(6, 0))
        ttk.Label(add, text="Add a group:").grid(row=0, column=0, sticky="w")
        # One full-width column: a fixed character width clipped the longer
        # names ("Malayalam and Gulf news"), and a button nobody can read is
        # worse than a taller panel.
        add.columnconfigure(0, weight=1)
        for i, (key, label, group) in enumerate(utm.URL_GROUPS):
            b = ttk.Button(add, text=f"{label.split(' (')[0]}  ({len(group)})",
                           command=lambda k=key: self._add_url_group(k))
            b.grid(row=1 + i, column=0, sticky="ew", pady=2)
            Tooltip(b, f"Add the {len(group)} entries for {label}.\n"
                       f"Sites already in the list are not added twice.")

        row = ttk.Frame(urls)
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="Reset to standard list",
                   command=self._reset_urls, width=21).pack(side="left")
        ttk.Button(row, text="Clear", width=8,
                   command=self._clear_urls).pack(side="left", padx=6)

        ttk.Label(urls, style="Good.TLabel", wraplength=320, justify="left",
                  text="WhatsApp is always allowed — it is written to the "
                       "firewall as an explicit exception above every block "
                       "rule.").pack(anchor="w", pady=(8, 0))

        cats = ttk.LabelFrame(body, text="Blocked application categories", padding=8)
        cats.pack(side="left", fill="both", expand=True, padx=(12, 0))
        # The checkboxes below use grid, so this header lives in its own frame.
        hdr = ttk.Frame(cats)
        hdr.grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        ttk.Label(hdr, wraplength=300, justify="left", style="Warn.TLabel",
                  text="Categories are coarse and not where you would guess — "
                       "Telegram is filed under Collaboration, not Social."
                       "Media. Use the button below to see the real category "
                       "of any app and block it on its own.").pack(
            anchor="w", pady=(0, 6))
        b = ttk.Button(hdr, text="Browse application signatures…", width=32,
                       command=self.act_apps_open)
        b.pack(anchor="w")
        Tooltip(b, "Open the firewall's own signature database: search it, see "
                   "which category each application really belongs to, and "
                   "block individual applications.")
        self.action_buttons.append(b)
        self.cat_vars = {}
        defaults = {cid for cid, _ in utm.DEFAULT_CATEGORIES}
        for i, (cid, name) in enumerate(utm.ALL_CATEGORIES):
            v = tk.BooleanVar(value=cid in defaults)
            self.cat_vars[cid] = v
            ttk.Checkbutton(cats, text=f"{name}  ({cid})", variable=v).grid(
                row=1 + i % 9, column=i // 9, sticky="w", padx=(0, 18))

        ttk.Label(f, wraplength=880, justify="left", style="Warn.TLabel", text=(
            "YouTube is in Video/Audio, not Social.Media — the website list is what "
            "blocks it. Remote.Access includes RDP, VNC and Telnet going out to the "
            "internet. Blocking a site here stops the website; phone apps that talk "
            "to their own servers are stopped by the Social.Media category.")
                  ).pack(anchor="w", pady=(10, 0))

        # The everyday job: change the list on a branch that is already running.
        upd = ttk.LabelFrame(f, text="Update a firewall that is already set up",
                             padding=10)
        upd.pack(fill="x", pady=(16, 0))
        ttk.Label(upd, wraplength=860, justify="left", text=(
            "Sends only the blocked-website list to the firewall on the Connect "
            "tab. Nothing else is touched — no interfaces, no DHCP, no policies, "
            "no application control — so this is safe to run on a live branch "
            "during working hours. The new list takes effect immediately.")
                  ).pack(anchor="w")

        # A FortiGate carries several web filter profiles (default, monitor-all,
        # wifi-default, ...). Say which one is being written to, and let it be
        # changed, rather than silently assuming the branch standard.
        pick = ttk.Frame(upd)
        pick.pack(fill="x", pady=(10, 0))
        ttk.Label(pick, text="Web filter profile").pack(side="left")
        self.v_wf_profile = tk.StringVar(value=utm.WEBFILTER_PROFILE)
        self.wf_box = ttk.Combobox(pick, textvariable=self.v_wf_profile, width=26,
                                   values=[utm.WEBFILTER_PROFILE])
        self.wf_box.pack(side="left", padx=(8, 8))
        Tooltip(self.wf_box,
                "The profile whose blocked-site list gets rewritten.\n"
                "'Branch-WebFilter' is the one this tool creates. Press "
                "'Show profiles' to list what is actually on this firewall.")
        b = ttk.Button(pick, text="Show profiles", width=15,
                       command=self.act_read_profiles)
        b.pack(side="left")
        Tooltip(b, "List every web filter profile on the firewall, the URL "
                   "table each one uses and which policies use it. "
                   "Changes nothing.")
        self.action_buttons.append(b)

        self.wf_target = ttk.Label(
            upd, foreground="#555", wraplength=860, justify="left",
            text="Target: Branch-WebFilter — press 'Show profiles' to confirm "
                 "against this firewall.")
        self.wf_target.pack(anchor="w", pady=(6, 0))

        r = ttk.Frame(upd)
        r.pack(fill="x", pady=(8, 0))
        b = ttk.Button(r, text="Update blocked sites now", width=26,
                       command=self.act_update_urls)
        b.pack(side="left")
        Tooltip(b, "Rewrite the blocked-site list on the connected firewall.\n"
                   "Only that list changes.")
        self.action_buttons.append(b)
        b = ttk.Button(r, text="Check what is blocked", width=22,
                       command=self.act_read_urls)
        b.pack(side="left", padx=8)
        Tooltip(b, "Read the list currently on the firewall and compare it with "
                   "the box above. Changes nothing.")
        self.action_buttons.append(b)

        # A profile nothing references blocks nothing, and that is invisible
        # from the profile screens -- so it gets its own check and its own fix.
        r2 = ttk.Frame(upd)
        r2.pack(fill="x", pady=(10, 0))
        b = ttk.Button(r2, text="Is filtering switched on?", width=24,
                       command=self.act_policy_state)
        b.pack(side="left")
        Tooltip(b, "Show every internet policy and whether it actually "
                   "enforces the web filter and application control. "
                   "Changes nothing.")
        self.action_buttons.append(b)
        b = ttk.Button(r2, text="Switch filtering ON", width=22,
                       command=self.act_attach_filters)
        b.pack(side="left", padx=8)
        Tooltip(b, "Attach the web filter and application sensor to the "
                   "office LAN and Staff WiFi internet policies.\n"
                   "Only those policies' filtering fields change -- no "
                   "interfaces, no DHCP, no addresses. Guest is left alone.")
        self.action_buttons.append(b)

        self._count_urls()

    def _reset_urls(self):
        self.urls_text.delete("1.0", "end")
        self.urls_text.insert("1.0", "\n".join(u for u, _ in utm.DEFAULT_URLS))
        self._count_urls()

    def _clear_urls(self):
        self.urls_text.delete("1.0", "end")
        self._count_urls()

    def _url_lines(self):
        return [ln.strip() for ln in
                self.urls_text.get("1.0", "end").splitlines() if ln.strip()]

    def _add_url_group(self, key):
        have = set(self._url_lines())
        fresh = [u for u in utm.group_urls(key) if u not in have]
        if not fresh:
            self._write_log(f"[skip] {utm.GROUP_LABELS[key]}: already in the list")
            return
        text = self.urls_text.get("1.0", "end").rstrip()
        self.urls_text.delete("1.0", "end")
        self.urls_text.insert("1.0", (text + "\n" if text else "") + "\n".join(fresh))
        self._write_log(f"[ok] added {len(fresh)} entries for "
                        f"{utm.GROUP_LABELS[key]}", "ok")
        self._count_urls()

    def _count_urls(self):
        n = len(self._url_lines())
        self.url_count.configure(text=f"{n} sites blocked  ·  WhatsApp allowed")

    # ---- tab 5: apply ---------------------------------------------------
    # ---- tab 6: dynamic DNS ---------------------------------------------
    def _tab_ddns(self):
        f = self._page("  6. Dynamic DNS  ")
        ttk.Label(f, text="This branch's name on the internet",
                  style="Head.TLabel").pack(anchor="w")
        ttk.Label(f, wraplength=880, justify="left", foreground="#555", text=(
            "Head office and this branch both get their address from the ISP, "
            "and it changes. Registering a name means each end can still find "
            "the other. Head office is already homadina.fortidyndns.com; this "
            "gives the branch its own.")).pack(anchor="w", pady=(4, 12))

        ttk.Label(f, style="Warn.TLabel", wraplength=880, justify="left", text=(
            "Do this ON SITE, after the ISP line is plugged in and working. "
            "The name is registered with FortiGuard over the internet, so it "
            "cannot be done while the unit is being staged.")).pack(anchor="w")

        grid = ttk.Frame(f)
        grid.pack(fill="x", pady=(12, 0))
        ttk.Label(grid, text="Branch name").grid(row=0, column=0, sticky="w",
                                                 padx=(0, 8), pady=3)
        self.v_ddns_name = tk.StringVar()
        e = ttk.Entry(grid, textvariable=self.v_ddns_name, width=26)
        e.grid(row=0, column=1, sticky="w", pady=3)
        Tooltip(e, f"Lower case letters, digits and hyphens, up to "
                   f"{ddns.MAX_NAME} characters.\nThis is separate from the "
                   f"VPN tunnel name on the next tab.")
        self.ddns_count = ttk.Label(grid, foreground="#666", text=f"0 / {ddns.MAX_NAME}")
        self.ddns_count.grid(row=0, column=2, sticky="w", padx=(10, 0))

        ttk.Label(grid, text="Domain").grid(row=1, column=0, sticky="w",
                                            padx=(0, 8), pady=3)
        self.v_ddns_suffix = tk.StringVar(value=ddns.DEFAULT_SUFFIX)
        ttk.Combobox(grid, textvariable=self.v_ddns_suffix, width=24,
                     state="readonly", values=ddns.SUFFIXES).grid(
            row=1, column=1, sticky="w", pady=3)
        ttk.Label(grid, foreground="#666",
                  text="matches head office").grid(row=1, column=2, sticky="w",
                                                   padx=(10, 0))

        self.ddns_full = ttk.Label(f, style="Good.TLabel", text="")
        self.ddns_full.pack(anchor="w", pady=(8, 0))
        for var in (self.v_ddns_name, self.v_ddns_suffix):
            var.trace_add("write", lambda *_a: self._refresh_ddns_name())

        grid2 = ttk.Frame(f)
        grid2.pack(fill="x", pady=(10, 0))
        ttk.Label(grid2, text="Internet port").grid(row=0, column=0, sticky="w",
                                                    padx=(0, 8), pady=3)
        self.v_ddns_port = tk.StringVar(value="wan1")
        self.ddns_port_box = ttk.Combobox(grid2, textvariable=self.v_ddns_port,
                                          width=24, values=["wan1", "wan2"])
        self.ddns_port_box.grid(row=0, column=1, sticky="w", pady=3)
        Tooltip(self.ddns_port_box, "The port the ISP line plugs into.")

        self.v_ddns_public = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            f, variable=self.v_ddns_public,
            text="This FortiGate sits behind an ISP router and gets a private "
                 "address  (register the public address instead)").pack(
            anchor="w", pady=(8, 0))

        row = ttk.Frame(f)
        row.pack(fill="x", pady=(14, 0))
        for text, cmd, width, tip in (
            ("Check the name is free", self.act_ddns_check_free, 24,
             "Look the name up from this laptop before claiming it. "
             "FortiGuard names are global; only Fortinet Support can release "
             "one that is taken."),
            ("Apply", self.act_ddns_apply, 12,
             "Register this name on the firewall."),
            ("Check it works", self.act_ddns_verify, 18,
             "Read the setting back, resolve the name, and compare it with "
             "the address the WAN port actually holds."),
        ):
            b = ttk.Button(row, text=text, command=cmd, width=width)
            b.pack(side="left", padx=(0, 8))
            Tooltip(b, tip)
            self.action_buttons.append(b)
        b = ttk.Button(f, text="Show what is registered now", width=30,
                       command=self.act_ddns_show)
        b.pack(anchor="w", pady=(10, 0))
        self.action_buttons.append(b)
        self._refresh_ddns_name()

    def _refresh_ddns_name(self):
        name = self.v_ddns_name.get().strip()
        self.ddns_count.configure(text=f"{len(name)} / {ddns.MAX_NAME}")
        err = ddns.validate_name(name) if name else None
        if err:
            self.ddns_full.configure(text=err, style="Warn.TLabel")
        elif name:
            self.ddns_full.configure(
                text="-> " + ddns.full_name(name, self.v_ddns_suffix.get()),
                style="Good.TLabel")
        else:
            self.ddns_full.configure(text="", style="Good.TLabel")

    # ---- tab 7: VPN tunnel ----------------------------------------------
    def _tab_vpn(self):
        f = self._page("  7. VPN Tunnel  ")
        ttk.Label(f, text="Tunnel to head office",
                  style="Head.TLabel").pack(anchor="w")
        ttk.Label(f, wraplength=880, justify="left", foreground="#555", text=(
            "Builds the encrypted link from this branch to head office: the "
            "tunnel, the networks allowed across it, the rules and the routes. "
            "Head office must have its matching end for the tunnel to come "
            "up.")).pack(anchor="w", pady=(4, 12))

        grid = ttk.Frame(f)
        grid.pack(fill="x")
        ttk.Label(grid, text="Branch name").grid(row=0, column=0, sticky="w",
                                                 padx=(0, 8), pady=3)
        self.v_vpn_branch = tk.StringVar()
        e = ttk.Entry(grid, textvariable=self.v_vpn_branch, width=26)
        e.grid(row=0, column=1, sticky="w", pady=3)
        Tooltip(e, f"Up to {vpn.MAX_BRANCH} characters. The tunnel is named "
                   f"<branch>{vpn.SUFFIX}, and a FortiGate interface name "
                   f"stops at {vpn.MAX_TUNNEL}.\nSeparate from the Dynamic DNS "
                   f"name on the previous tab.")
        self.vpn_count = ttk.Label(grid, foreground="#666",
                                   text=f"0 / {vpn.MAX_BRANCH}")
        self.vpn_count.grid(row=0, column=2, sticky="w", padx=(10, 0))

        ttk.Label(grid, text="Tunnel name").grid(row=1, column=0, sticky="w",
                                                 padx=(0, 8), pady=3)
        self.vpn_tunnel_lbl = ttk.Label(grid, style="Good.TLabel", text=vpn.SUFFIX)
        self.vpn_tunnel_lbl.grid(row=1, column=1, sticky="w", pady=3)
        ttk.Label(grid, foreground="#666", text="built for you").grid(
            row=1, column=2, sticky="w", padx=(10, 0))
        self.v_vpn_branch.trace_add("write", lambda *_a: self._refresh_vpn_name())

        ttk.Label(grid, text="Head office name").grid(row=2, column=0, sticky="w",
                                                      padx=(0, 8), pady=3)
        self.v_vpn_ho = tk.StringVar()
        e = ttk.Entry(grid, textvariable=self.v_vpn_ho, width=36)
        e.grid(row=2, column=1, sticky="w", pady=3)
        Tooltip(e, "Head office's internet name, e.g. homadina.fortidyndns.com")

        ttk.Label(grid, text="Pre-shared key").grid(row=3, column=0, sticky="w",
                                                    padx=(0, 8), pady=3)
        self.v_vpn_psk = tk.StringVar()
        self.vpn_psk_entry = ttk.Entry(grid, textvariable=self.v_vpn_psk,
                                       width=36, show="*")
        self.vpn_psk_entry.grid(row=3, column=1, sticky="w", pady=3)
        Tooltip(self.vpn_psk_entry,
                "Must match head office exactly. Never saved to a branch file.")
        self.v_psk_show = tk.BooleanVar(value=False)
        ttk.Checkbutton(grid, text="show", variable=self.v_psk_show,
                        command=lambda: self.vpn_psk_entry.configure(
                            show="" if self.v_psk_show.get() else "*")).grid(
            row=3, column=2, sticky="w", padx=(10, 0))

        ports = ttk.LabelFrame(f, text="Ports", padding=10)
        ports.pack(fill="x", pady=(14, 0))
        r = ttk.Frame(ports)
        r.pack(fill="x")
        ttk.Label(r, text="Internet port out").pack(side="left")
        self.v_vpn_wan = tk.StringVar(value="wan1")
        ttk.Combobox(r, textvariable=self.v_vpn_wan, width=14,
                     values=["wan1", "wan2"]).pack(side="left", padx=(8, 8))
        ttk.Label(r, foreground="#666",
                  text="the tunnel is built on this port").pack(side="left")

        ttk.Label(ports, text="Inside ports that may reach head office").pack(
            anchor="w", pady=(10, 2))
        self.vpn_port_vars = {}
        for port, label, on in (("internal", "Office LAN", True),
                                ("internal2", "Staff WiFi", True),
                                ("internal3", "Guest WiFi", False)):
            v = tk.BooleanVar(value=on)
            self.vpn_port_vars[port] = v
            cb = ttk.Checkbutton(ports, variable=v,
                                 text=f"{port}   {label}"
                                      + ("" if on else
                                         "   — never; guests must not reach "
                                         "head office"))
            cb.pack(anchor="w")
            if port in vpn.GUEST_PORTS:
                cb.configure(state="disabled")

        nets = ttk.LabelFrame(f, text="Networks", padding=10)
        nets.pack(fill="x", pady=(14, 0))
        ttk.Label(nets, text="Head office networks (one per line, e.g. "
                             "10.0.0.0/24)").pack(anchor="w")
        self.vpn_remote_text = scrolledtext.ScrolledText(
            nets, width=30, height=4, font=("Consolas", 9))
        self.vpn_remote_text.pack(anchor="w", pady=(4, 0))
        self.vpn_local_lbl = ttk.Label(
            nets, foreground="#555", wraplength=840, justify="left",
            text="This branch's networks are read from the firewall itself "
                 "when you press Preview.")
        self.vpn_local_lbl.pack(anchor="w", pady=(8, 0))

        row = ttk.Frame(f)
        row.pack(fill="x", pady=(14, 0))
        for text, cmd, width, tip in (
            ("Preview", self.act_vpn_preview, 14,
             "List exactly what would be created, and read this branch's own "
             "networks off the firewall. Changes nothing."),
            ("Apply", self.act_vpn_apply, 12, "Build the tunnel."),
            ("Check tunnel", self.act_vpn_status, 16,
             "Is it up? Reads the live VPN monitor."),
            ("Verify", self.act_vpn_verify, 12,
             "Read every object back and check it."),
        ):
            b = ttk.Button(row, text=text, command=cmd, width=width)
            b.pack(side="left", padx=(0, 8))
            Tooltip(b, tip)
            self.action_buttons.append(b)

        ttk.Label(f, style="Warn.TLabel", wraplength=880, justify="left", text=(
            "Head office must have a matching tunnel with the same key, or "
            "this end sits there dialling and never connects. The branch "
            "always dials out, so it works even where the ISP does not give "
            "this site a reachable address.")).pack(anchor="w", pady=(12, 0))
        b = ttk.Button(f, text="Remove this tunnel", width=20,
                       command=self.act_vpn_remove)
        b.pack(anchor="w", pady=(10, 0))
        self.action_buttons.append(b)
        self._refresh_vpn_name()

    def _refresh_vpn_name(self):
        name = self.v_vpn_branch.get().strip()
        if len(name) > vpn.MAX_BRANCH:
            name = name[:vpn.MAX_BRANCH]
            self.v_vpn_branch.set(name)          # refuse the 12th character
        self.vpn_count.configure(text=f"{len(name)} / {vpn.MAX_BRANCH}")
        t = vpn.tunnel_name(name)
        self.vpn_tunnel_lbl.configure(
            text=f"{t}   ({len(t)} of {vpn.MAX_TUNNEL})" if name else vpn.SUFFIX)

    def vpn_spec(self):
        return vpn.VpnSpec(
            branch_name=self.v_vpn_branch.get().strip(),
            remote_ddns=self.v_vpn_ho.get().strip(),
            psk=self.v_vpn_psk.get(),
            wan_port=self.v_vpn_wan.get().strip() or "wan1",
            inside_ports=[p for p, v in self.vpn_port_vars.items() if v.get()],
            remote_subnets=[ln.strip() for ln in
                            self.vpn_remote_text.get("1.0", "end").splitlines()
                            if ln.strip()],
        )

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
        ttk.Label(prof, text="Branch settings file:").pack(side="left")
        b = ttk.Button(prof, text="Export…", command=self.act_branch_export, width=10)
        b.pack(side="left", padx=6)
        Tooltip(b, "Write the selected saved branch to a file you can send to "
                   "another engineer.")
        b = ttk.Button(prof, text="Import…", command=self.act_branch_import, width=10)
        b.pack(side="left")
        Tooltip(b, "Add a branch settings file someone sent you to the saved "
                   "branch list at the top.")
        ttk.Label(prof, foreground="#666",
                  text="  (day-to-day, use the Saved branches bar at the top)"
                  ).pack(side="left")
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
                elif kind == "profiles":
                    # Widget updates must happen on the UI thread, so the
                    # worker posts them here rather than touching Tk directly.
                    self.wf_box.configure(values=payload)
                elif kind == "wf_target":
                    self.wf_target.configure(text=payload)
                elif kind == "vpn_local":
                    self.vpn_local_lbl.configure(text=payload)
                elif kind == "apps":
                    sigs, blocked, cats, source = payload
                    # Keep the Filtering tab's ticks honest: they should show
                    # what the firewall has, not what was last typed here.
                    for cid, var in self.cat_vars.items():
                        var.set(cid in cats)
                    if self.app_browser and self.app_browser.winfo_exists():
                        self.app_browser.populate(sigs, blocked, cats, source)
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

    # ---- blocked-site list only -----------------------------------------
    def act_read_profiles(self):
        """List the firewall's web filter profiles and say which we would edit."""
        want = self.v_wf_profile.get().strip()

        def job(fg, log):
            profs = utm.list_profiles(fg)
            log(f"Web filter profiles on {self.f_host.get()}:", "head")
            for p in profs:
                pols = utm.profile_policies(fg, p["name"])
                mark = " <-- updates go here" if p["name"] == want else ""
                log(f"  {p['name']:<22} URL table "
                    f"{('#' + str(p['table'])) if p['table'] else '(none)':<8} "
                    f"{('used by ' + ', '.join(pols)) if pols else 'not used by any policy'}"
                    f"{mark}", "ok" if mark else None)
            names = [p["name"] for p in profs]
            self.q.put(("profiles", names))
            if want in names:
                summary, _tid, _tn, _pols = utm.describe_target(fg, want)
                self.q.put(("wf_target", "Target: " + summary))
                log("")
                log(f"[ok] '{want}' exists — Update blocked sites now would "
                    f"rewrite its list.", "ok")
            else:
                self.q.put(("wf_target",
                            f"Target: '{want}' is NOT on this firewall — pick "
                            f"one of: {', '.join(names)}"))
                log("")
                log(f"[!] '{want}' is not on this firewall. Pick one of the "
                    f"profiles above, or run a full Apply to create the branch "
                    f"standard one.", "fail")
        self._run("Show web filter profiles", job)

    def act_update_urls(self):
        urls = self._url_lines()
        profile = self.v_wf_profile.get().strip()
        if not urls:
            messagebox.showwarning(
                APP_TITLE, "The blocked-website list is empty.\n\n"
                           "Press 'Reset to standard list' or add a group first.")
            return
        if not profile:
            messagebox.showwarning(APP_TITLE, "Choose a web filter profile first.")
            return
        if not messagebox.askokcancel(APP_TITLE, (
                f"Send {len(urls)} blocked sites to the firewall at "
                f"{self.f_host.get()}?\n\n"
                f"Web filter profile:  {profile}\n"
                f"WhatsApp stays allowed.\n\n"
                f"Only that profile's blocked-site list changes. Interfaces, "
                f"DHCP, policies and application control are left exactly as "
                f"they are.")):
            return

        def job(fg, log):
            log(f"Updating '{profile}' ({len(urls)} sites)…", "head")
            utm.update_urls(fg, urls, log, profile=profile)
            summary, _t, _n, _p = utm.describe_target(fg, profile)
            self.q.put(("wf_target", "Target: " + summary))
            log("")
            log(f"Done. The new list is live on every policy using '{profile}'.",
                "ok")
        self._run("Update blocked sites", job)

    def act_read_urls(self):
        want = set(self._url_lines())
        profile = self.v_wf_profile.get().strip()

        def job(fg, log):
            summary, tid, tname, _pols = utm.describe_target(fg, profile)
            log(summary, "head")
            self.q.put(("wf_target", "Target: " + summary))
            if not tid:
                log("[!] that profile has no URL table yet — 'Update blocked "
                    "sites now' would create one.", "warn")
                return
            uf = fg.results(f"/api/v2/cmdb/webfilter/urlfilter/{tid}")[0]
            rows = uf.get("entries", [])
            have = {e.get("url") for e in utm.blocked_only(rows)}
            allowed = [e.get("url") for e in rows if e.get("action") == "allow"]
            log(f"On the firewall: {len(have)} sites blocked, "
                f"{len(allowed)} allowed ({', '.join(allowed) or 'none'})", "head")
            missing = sorted(want - have)
            extra = sorted(have - want)
            for u in missing:
                log(f"  [would add]    {u}", "warn")
            for u in extra:
                log(f"  [would remove] {u}", "warn")
            if not missing and not extra:
                log("  the firewall already matches the list in the box.", "ok")
            else:
                log(f"  {len(missing)} to add, {len(extra)} to remove — press "
                    f"'Update blocked sites now' to apply.", "warn")
        self._run("Check blocked sites", job)

    # ---- is filtering actually switched on? ------------------------------
    def act_policy_state(self):
        def job(fg, log):
            rows = utm.policy_filter_state(fg)
            if not rows:
                log("[!] no internet-bound policies found at all.", "fail")
                return
            log(f"Internet policies on {self.f_host.get()}:", "head")
            live = 0
            for p in rows:
                on = p["utm"] and (p["webfilter"] or p["applist"])
                live += 1 if on else 0
                log(f"  #{p['policyid']:<4} {','.join(p['src']):<24} -> "
                    f"{','.join(p['dst']):<6} "
                    f"web={p['webfilter'] or '-':<18} "
                    f"app={p['applist'] or '-':<18} "
                    f"ssl={p['ssl'] or '-'}",
                    "ok" if on else "fail")
            log("")
            if live:
                log(f"[ok] {live} of {len(rows)} internet policies are "
                    f"filtering.", "ok")
            else:
                log("[!] NOTHING is being filtered. The profiles exist but no "
                    "policy references them, so every site and app is allowed. "
                    "Press 'Switch filtering ON'.", "fail")
        self._run("Check filtering state", job)

    def act_attach_filters(self):
        spec = self.spec_from_form()
        ports = [spec.lan_port]
        if spec.configure_staff:
            ports.append(spec.staff_port)
        if not messagebox.askokcancel(APP_TITLE, (
                f"Switch filtering on for {', '.join(ports)} on "
                f"{self.f_host.get()}?\n\n"
                f"Attaches:\n"
                f"    web filter          {utm.WEBFILTER_PROFILE}\n"
                f"    application control {utm.APPLIST_NAME}\n"
                f"    HTTPS inspection    {spec.ssl_mode}\n\n"
                f"Only those policies' filtering fields change. Interfaces, "
                f"DHCP and addresses are untouched, and Guest WiFi is left "
                f"unfiltered as designed.")):
            return

        def job(fg, log):
            utm.attach_filters(fg, ports, spec.ssl_mode, spec.web_filter,
                               spec.app_filter, log)
            log("")
            log("Filtering is now live on those policies.", "ok")
            if spec.ssl_mode == "deep-inspection":
                log("[!] Deep inspection is on: every phone and PC needs the "
                    "FortiGate CA certificate installed, or secure sites will "
                    "fail rather than load.", "warn")
        self._run("Switch filtering on", job)

    # ---- application signatures -----------------------------------------
    def act_apps_open(self):
        if self.app_browser and self.app_browser.winfo_exists():
            self.app_browser.lift()
            return
        self.app_browser = AppBrowser(self)
        self.act_apps_load()

    def act_apps_load(self):
        if not (self.app_browser and self.app_browser.winfo_exists()):
            return

        def job(fg, log):
            sigs, path = appctrl.load_signatures(fg)
            log(f"[ok] {len(sigs)} application signatures read from {path}")
            cats = appctrl.categories(sigs)
            log("  categories on this firmware: " + ", ".join(
                f"{n} ({c})" for n, c in cats[:12])
                + (" ..." if len(cats) > 12 else ""))
            try:
                sensor = appctrl.read_sensor(fg, utm.APPLIST_NAME)
            except FortiGateError as e:
                log(f"[!] {e}", "warn")
                sensor = {"categories": [], "applications": []}
            names = dict(utm.ALL_CATEGORIES)
            log(f"  '{utm.APPLIST_NAME}' blocks categories: "
                + (", ".join(f"{names.get(c, c)} ({c})"
                             for c in sensor["categories"]) or "none"))
            log(f"  and {len(sensor['applications'])} individual application(s)")
            pols = appctrl.sensor_policies(fg, utm.APPLIST_NAME)
            log(f"  enforced by: {', '.join(pols) or 'NO POLICY -- nothing is '
                                              'being blocked'}",
                "ok" if pols else "fail")
            self.q.put(("apps", (sigs, sensor["applications"],
                                 sensor["categories"], path)))
        self._run("Read application signatures", job)

    def act_apps_save(self):
        b = self.app_browser
        if not (b and b.winfo_exists()):
            return
        blocked = sorted(b.blocked)
        cats = sorted(b.blocked_cats) or [
            cid for cid, v in self.cat_vars.items() if v.get()]
        names = {s["id"]: s["name"] for s in b.sigs}
        sample = ", ".join(names.get(i, str(i)) for i in blocked[:8])
        if not messagebox.askokcancel(APP_TITLE, (
                f"Write the application sensor '{utm.APPLIST_NAME}' to "
                f"{self.f_host.get()}?\n\n"
                f"{len(blocked)} individual application(s): {sample}"
                f"{' ...' if len(blocked) > 8 else ''}\n"
                f"{len(cats)} whole categor(y/ies) from the Filtering tab.\n\n"
                f"Individual applications are written above the categories, so "
                f"they take effect first. Nothing else on the firewall "
                f"changes.")):
            return

        def job(fg, log):
            appctrl.write_sensor(fg, utm.APPLIST_NAME, cats, blocked, log)
            pols = appctrl.sensor_policies(fg, utm.APPLIST_NAME)
            if pols:
                log(f"[ok] live on: {', '.join(pols)}")
            else:
                log("[!] no policy uses this sensor, so nothing is actually "
                    "being blocked. Run a full Apply to attach it.", "fail")
            b.dirty = False
            # Re-read rather than assume: the window should show what the
            # firewall says, not what we asked for.
            sigs, path = appctrl.load_signatures(fg)
            live = appctrl.read_sensor(fg, utm.APPLIST_NAME)
            log(f"[ok] firewall now blocks {len(live['applications'])} "
                f"individual application(s)")
            self.q.put(("apps", (sigs, live["applications"],
                                 live["categories"], "after saving")))
        self._run("Save application blocks", job)

    # ---- dynamic DNS -----------------------------------------------------
    def _ddns_args(self):
        name = self.v_ddns_name.get().strip()
        err = ddns.validate_name(name)
        if err:
            messagebox.showwarning(APP_TITLE, err)
            return None
        return name, self.v_ddns_suffix.get(), self.v_ddns_port.get().strip()

    def act_ddns_check_free(self):
        got = self._ddns_args()
        if not got:
            return
        name, suffix, _port = got
        host = ddns.full_name(name, suffix)
        self._write_log(f"Looking up {host} …", "head")
        free, detail = ddns.name_is_free(host)
        self._write_log(("[ok] " if free else "[!] ") + detail,
                        "ok" if free else "warn")

    def act_ddns_apply(self):
        got = self._ddns_args()
        if not got:
            return
        name, suffix, port = got
        public = self.v_ddns_public.get()
        host = ddns.full_name(name, suffix)
        if not messagebox.askokcancel(APP_TITLE, (
                f"Register this branch as:\n\n    {host}\n\n"
                f"watching {port} on the firewall at {self.f_host.get()}.\n\n"
                f"Nothing else on the firewall changes.")):
            return

        def job(fg, log):
            ddns.apply_ddns(fg, name, suffix, port, public, log)
            log("")
            log("Registration happens between the firewall and FortiGuard and "
                "can take a few minutes. Press 'Check it works' to confirm.",
                "warn")
        self._run("Register the branch name", job)

    def act_ddns_verify(self):
        got = self._ddns_args()
        if not got:
            return
        name, suffix, port = got

        def job(fg, log):
            results = ddns.verify(fg, name, suffix, port)
            passed = sum(1 for _, ok, _ in results if ok)
            for label, ok, detail in results:
                log(f"  [{'PASS' if ok else 'FAIL'}] {label:<34} {detail}",
                    "ok" if ok else "fail")
            log("")
            log(f"RESULT: {passed}/{len(results)} checks passed",
                "ok" if passed == len(results) else "warn")
            wan = ddns.wan_address(fg, port)
            if wan and ddns.is_private(wan):
                log("")
                log(f"[!] {port} holds {wan}, which is a private address. The "
                    f"ISP is putting this site behind their own NAT, so head "
                    f"office will not be able to dial in. The tunnel still "
                    f"works because the branch always dials out.", "warn")
        self._run("Check the branch name", job)

    def act_ddns_show(self):
        def job(fg, log):
            entries = ddns.read_ddns(fg)
            if not entries:
                log("No dynamic DNS is registered on this firewall.", "warn")
                return
            log(f"Dynamic DNS on {self.f_host.get()}:", "head")
            for e in entries:
                ip = ddns.resolve(e["domain"])
                log(f"  #{e['ddnsid']}  {e['domain']:<34} {e['server']:<16} "
                    f"{', '.join(e['ports']) or '(no port)':<10} "
                    f"resolves to {ip or 'nothing yet'}")
        self._run("Show dynamic DNS", job)

    # ---- VPN tunnel ------------------------------------------------------
    def _vpn_checked(self):
        spec = self.vpn_spec()
        errs = vpn.validate(spec)
        if errs:
            messagebox.showwarning(APP_TITLE, "Please fix:\n\n- " + "\n- ".join(errs))
            return None
        return spec

    def act_vpn_preview(self):
        spec = self.vpn_spec()
        errs = vpn.validate(spec)

        def job(fg, log):
            try:
                locals_ = vpn.local_selectors(fg, spec.inside_ports)
            except FortiGateError as e:
                log(f"[!] {e}", "fail")
                return
            self.q.put(("vpn_local", "This branch would send: " + ", ".join(
                f"{p} ({s})" for p, s in locals_)))
            log("This branch's networks, read from the firewall:", "head")
            for p, s in locals_:
                log(f"  {p:<12} {s}")
            for w in vpn.check_overlap(fg, spec):
                log(f"[!] {w}", "fail")
            if errs:
                log("")
                log("Cannot build it yet:", "head")
                for e in errs:
                    log(f"  - {e}", "warn")
                return
            log("")
            log("What Apply would create:", "head")
            for label, verdict in vpn.preview(fg, spec):
                log(f"  {label}  ->  {verdict}",
                    "warn" if verdict.startswith("would") else "ok")
        self._run("Preview the tunnel", job)

    def act_vpn_apply(self):
        spec = self._vpn_checked()
        if not spec:
            return
        if not messagebox.askokcancel(APP_TITLE, (
                f"Build the tunnel '{spec.tunnel}' to {spec.remote_ddns}?\n\n"
                f"Out via {spec.wan_port}. Networks allowed across: "
                f"{', '.join(spec.inside_ports)}.\n"
                f"Head office networks: {', '.join(spec.remote_subnets)}.\n\n"
                f"Guest WiFi is not included and cannot be.\n\n"
                f"Head office must have its matching end with the same key.")):
            return

        def job(fg, log):
            for w in vpn.check_overlap(fg, spec):
                log(f"[!] {w}", "fail")
            vpn.apply_vpn(fg, spec, log)
        self._run("Build the tunnel", job)

    def act_vpn_status(self):
        name = vpn.tunnel_name(self.v_vpn_branch.get().strip())

        def job(fg, log):
            up, detail = vpn.tunnel_status(fg, name)
            log(f"{name}: {detail}", "ok" if up else "warn")
        self._run("Check the tunnel", job)

    def act_vpn_verify(self):
        spec = self.vpn_spec()
        if not spec.branch_name:
            messagebox.showwarning(APP_TITLE, "Enter the branch name first.")
            return

        def job(fg, log):
            results = vpn.verify(fg, spec)
            passed = sum(1 for _, ok, _ in results if ok)
            for label, ok, detail in results:
                log(f"  [{'PASS' if ok else 'FAIL'}] {label:<34} {detail}",
                    "ok" if ok else "fail")
            log("")
            log(f"RESULT: {passed}/{len(results)} checks passed",
                "ok" if passed == len(results) else "warn")
        self._run("Verify the tunnel", job)

    def act_vpn_remove(self):
        name = self.v_vpn_branch.get().strip()
        if not name:
            messagebox.showwarning(APP_TITLE, "Enter the branch name first.")
            return
        if not messagebox.askokcancel(APP_TITLE, (
                f"Remove the tunnel '{vpn.tunnel_name(name)}' and everything "
                f"built with it — selectors, addresses, groups, both policies "
                f"and the routes?\n\nHead office will lose its link to this "
                f"branch.")):
            return
        self._run("Remove the tunnel",
                  lambda fg, log: vpn.remove_vpn(fg, name, log))

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

    # ---- saved branches --------------------------------------------------
    def _save_branch(self, name):
        """Write the form to the library under `name`. True if it was saved."""
        try:
            templates.save(name, self.spec_from_form(), ROOT,
                           host=self.f_host.get(),
                           backup_dir=self.v_backup_dir.get())
        except TemplateError as e:
            messagebox.showerror(APP_TITLE, str(e))
            return False
        self._refresh_branch_list(select=name)
        self._write_log(f"[ok] branch saved: {name}"
                        f"   (passwords are never saved)", "ok")
        return True

    def act_branch_save_as(self):
        name = simpledialog.askstring(
            APP_TITLE, "Name for this branch\n(e.g. 'Al Ain' or 'Branch 07'):",
            initialvalue=self.v_branch.get() or self.f_hostname.get(), parent=self)
        if not name or not name.strip():
            return
        name = name.strip()
        if templates.exists(name, ROOT) and not messagebox.askokcancel(
                APP_TITLE, f"'{name}' is already saved. Overwrite it?"):
            return
        self._save_branch(name)

    def act_branch_update(self):
        name = self.v_branch.get()
        if not name:
            messagebox.showinfo(APP_TITLE,
                                "Pick a branch from the list first, or use "
                                "'Save as new…'.")
            return
        if messagebox.askokcancel(
                APP_TITLE, f"Overwrite the saved settings for '{name}' with "
                           f"what is on the tabs now?"):
            self._save_branch(name)

    def act_branch_load(self):
        name = self.v_branch.get()
        if not name:
            return
        try:
            data = templates.load(name, ROOT)
        except TemplateError as e:
            messagebox.showerror(APP_TITLE, str(e))
            self._refresh_branch_list()
            return
        self.form_from_spec(data)
        self._write_log(f"[ok] branch loaded: {name}"
                        + (f"   (saved {data['saved']})" if data.get("saved") else "")
                        + "   — passwords are not stored, re-enter them", "ok")

    def act_branch_delete(self):
        name = self.v_branch.get()
        if not name:
            messagebox.showinfo(APP_TITLE, "Pick a branch from the list first.")
            return
        if not messagebox.askokcancel(
                APP_TITLE, f"Remove '{name}' from the saved branch list?\n\n"
                           f"This only deletes the saved settings file. Nothing "
                           f"on the FortiGate changes."):
            return
        try:
            templates.delete(name, ROOT)
        except TemplateError as e:
            messagebox.showerror(APP_TITLE, str(e))
        else:
            self._write_log(f"[ok] branch deleted: {name}", "ok")
        self.v_branch.set("")
        self._refresh_branch_list()

    def act_branch_folder(self):
        try:
            webbrowser.open(templates.templates_dir(ROOT).as_uri())
        except TemplateError as e:
            messagebox.showerror(APP_TITLE, str(e))

    # ---- send a branch to another machine --------------------------------
    def act_branch_export(self):
        name = self.v_branch.get()
        if not name:
            messagebox.showinfo(APP_TITLE,
                                "Pick a saved branch from the list at the top first.")
            return
        path = filedialog.asksaveasfilename(
            title="Export branch settings", defaultextension=".json",
            filetypes=[("Branch settings", "*.json")],
            initialfile=templates.slug(name) + ".branch.json")
        if not path:
            return
        try:
            templates.export_file(name, path, ROOT)
        except TemplateError as e:
            messagebox.showerror(APP_TITLE, str(e))
            return
        self._write_log(f"[ok] exported '{name}' to {path}", "ok")

    def act_branch_import(self):
        path = filedialog.askopenfilename(
            title="Import branch settings",
            filetypes=[("Branch settings", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            name = templates.import_file(path, ROOT)
        except TemplateError as e:
            messagebox.showerror(APP_TITLE, str(e))
            return
        self._refresh_branch_list(select=name)
        self.act_branch_load()
        self._write_log(f"[ok] imported branch '{name}' from {path}", "ok")


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
