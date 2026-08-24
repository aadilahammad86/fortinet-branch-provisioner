# Fortinet Branch Project — Working Context

> This file is auto-loaded by Claude Code when a session starts in this folder.
> It is the "memory" for the fortinet-project workspace. Keep it up to date.

## What this project is

Tooling to set up **Fortinet FortiGate firewalls for branch offices** in a fast,
repeatable way. We built the full config on one unit, saved every step as a
Python script, and made a one-command provisioner for each new branch. The next
big goal is a **VPN from every branch back to Head Office (HO)**.

## Where things stand (updated 2026-08-20)

**Released:** v1.2.2 on GitHub — the expanded blocked-site list (social media,
messaging except WhatsApp, Malayalam + Gulf news, job sites) and the
"Update blocked sites now" action for a firewall that is already running.
v1.1 before it was the saved-branch templates.

**In flight:** the **Dynamic DNS** and **VPN Tunnel** tabs (now v1.3, since
v1.2 went to the blocklist work). Planned in
full, nothing built. Start at `docs/ddns-vpn-plan.md`; the decision context is
under "HO VPN" below. Four answers are needed before coding starts — they are
listed at the end of the plan.

**Picking this up on another machine:** clone, `cp .env.example .env` and fill
in the device address and password (`.env` is git-ignored and holds secrets).
`branches/` (saved branch templates) is also git-ignored, so saved branches do
not travel with the repo — copy that folder by hand, or use the GUI's
Export…/Import… buttons.

## The device (current staged unit)

| Field | Value |
|-------|-------|
| Model | FortiGate 60F |
| Serial | FGT60FTK25040370 |
| Firmware | v7.4.9 (build 2829) |
| Current manage URL | `https://172.21.0.1` (interface `internal1`) |
| Admin user | `admin` (password in `.env`) |

> ⚠️ If this unit is **factory reset**, the manage URL reverts to the FortiGate
> default **`https://192.168.1.99`** with admin / blank password. After a reset,
> update `FGT_HOST` (and the password) in `.env` before using the scripts.

## Credentials & connection

- Connection settings live in **`.env`** (git-ignored): `FGT_HOST`, `FGT_USER`,
  `FGT_PASSWORD`, and optional `FGT_PPPOE_USER` / `FGT_PPPOE_PASS`.
- Copy `.env.example` → `.env` to set up on a new machine.
- The device uses a **self-signed cert** — all tools skip TLS verification
  (`curl -k`, Python `ssl.CERT_NONE`).

## How the scripts talk to the FortiGate (conventions / gotchas)

All scripts use the **FortiGate REST API** over HTTPS with a login cookie:

1. `GET /` to prime the session, then `POST /logincheck` with
   `username`/`secretkey`/`ajax=1`. A leading `1` in the reply = success.
2. **CSRF token gotcha:** the cookie is named `ccsrftoken_<port>_<hex>`, NOT
   plain `ccsrftoken`. Match by **prefix** (`startswith("ccsrftoken")`) and send
   it as the `X-CSRFTOKEN` header on writes. Also send a `Referer` header.
3. GET (monitor) endpoints don't need CSRF; PUT/POST (cmdb) do.

Environment: **Windows + PowerShell** primary; a Bash (Git Bash) tool is also
available. Python 3.12, on PATH as `python` (the install path differs per
machine — do not hard-code it). No `jq` — parse JSON in Python. The package
uses only the standard library; PyInstaller is needed **only** to build the
.exe (`python -m pip install pyinstaller`).

## Architecture (since the GUI was added)

**All provisioning logic lives in the `fortigate/` package.** The CLI scripts
and the GUI are both thin front ends over it — never add device logic to a
script or to the GUI, add it to the package.

| Module | Holds |
|--------|-------|
| `fortigate/client.py` | `FortiGate` class: login, CSRF, `call/get/results/upsert`, `status()`, `backup()`. The one copy of the connection code that used to be duplicated in all 9 scripts. |
| `fortigate/branch.py` | `BranchSpec` dataclass, `validate()`, `compute_range()`, interface/DHCP/policy/wan1/LAN functions, `provision()`, `preview()` (dry run), `verify()`. |
| `fortigate/utm.py` | Blocked-URL list, category IDs, `apply_filters()`, `clear_filters()`, `verify_filters()`, `licence_state()`. |
| `fortigate/connections.py` | Remembered logins per device address, for the Connect tab's "Remember this connection". Password encrypted with **Windows DPAPI via ctypes** (`CryptProtectData`), so it only decrypts for the same Windows account on the same laptop; a copied file returns `locked` and the operator retypes. Lives in `connections.json` beside the .exe, git-ignored. **Never** put a password in a branch template -- those get emailed; this file must not leave the laptop. |
| `fortigate/appctrl.py` | The device's own signature database + per-application blocking. **Categories are not where you would guess: Telegram is `Collaboration`, not `Social.Media`** -- which is why a sensor blocking 7+23 never stopped it. `load_signatures`, `categories`, `search`, `resolve`, `read_sensor`, `write_sensor` (app overrides written ABOVE category entries, because the FortiGate takes the first match). |
| `fortigate/ddns.py` | FortiGuard DDNS: register `<branch>.fortidyndns.com`, resolve it back, compare with the WAN address. |
| `fortigate/vpn.py` | The HO tunnel: phase1/phase2, addresses, groups, policies, routes incl. the blackhole. |
| `fortigate/templates.py` | Saved-branch library: one JSON file per branch in `branches/` (beside the .exe, git-ignored). `save/load/load_spec/delete/list_names/summaries/export_file/import_file`. Strips `pppoe_pass` and every other secret; ignores unknown keys on load so old files still work. |
| `branch_gui.py` | Tkinter GUI. Worker thread + queue; never call the device on the UI thread. |
| `build_exe.py` | PyInstaller build. `--onedir` by default (fewer AV false positives). |

`BranchSpec` is the single source of truth for a branch's settings — the GUI
form, the CLI flags, saved profiles, `preview()` and `verify()` all speak it.

## Scripts (in `scripts/`)

| Script | Purpose |
|--------|---------|
| `provision-branch.py` | **Main entry.** `--branch NAME` loads a saved branch as the defaults (CLI flags still win), `--save-branch NAME` stores one, `--list-branches` lists them. Provisions a whole branch. Prompts for Staff + Guest WiFi gateway IPs and client counts (or `--wifi-ip`/`--clients`/`--guest-ip`/`--guest-clients`/`--yes`). Applies both interfaces, DHCP, the three wan1 NAT policies, wan1 PPPoE. Idempotent. Pass `--guest-ip -` to skip guest. |
| `backup-config.py` | Downloads a full config backup to `configs/` (git-ignored; holds secrets). Run before any reset/big change. |
| `pull-device-info.py` | Writes `docs/device-info.md` (model, firmware, interfaces, load). |
| `check-connection.ps1` | Reachability + login test. |
| `configure-wifi-lan.py` | One-off: set internal2 Staff WiFi + DHCP. |
| `configure-guest-wifi.py` | One-off: set internal3 Guest WiFi + DHCP. |
| `configure-internet-access.py` | One-off: create the three wan1 NAT policies. |
| `configure-utm-filters.py` | Web filter + app control (`--ssl`, `--off`, `--verify`). Wraps `fortigate.utm`. |
| `configure-wan1-pppoe.py` | One-off: set wan1 to PPPoE plug-and-play. |
| `configure-wan-dhcp.py` | One-off: set a WAN to DHCP. |

## What has been configured on the staged unit

- **LAN** on `internal` (hw switch): `172.21.0.1/21`, DHCP `.100–.230`.
- **Staff WiFi** on port `internal2`: `192.168.1.1/24`, alias `WiFi-LAN`, allow
  ping. DHCP `192.168.1.2–.26` (25 clients), 7-day lease.
- **Guest WiFi** on port `internal3`: `192.168.2.1/24`, alias `Guest-WiFi`,
  allow ping. DHCP `192.168.2.2–.26` (25 clients), same lease.
- **DNS needs no on-site setup.** `wan1` has `dns-server-override enable`, so
  PPPoE-learned ISP DNS replaces the system DNS; all DHCP servers use
  `dns-service default`, so clients get the ISP resolvers automatically.
  Clients resolve directly out through NAT — not via the FortiGate.
- **Internet NAT policies — one ISP (wan1), three inside networks:**
  `internal→wan1` (#1, default), `internal2→wan1` (#2), `internal3→wan1` (#5).
  The old wan2 policies were deleted; `wan2` is a spare port with no policies.
- **Guest isolation is by omission** — Guest WiFi has only its wan1 policy, so
  default deny keeps it off LAN/Staff/VPN. Do not add guest→inside policies.
- **Web filter `Branch-WebFilter`** (static URL table `Branch-Blocked-Sites`,
  id 1). Since v1.2 the list is **133 entries in five named groups** defined in
  `fortigate/utm.py` as `URL_GROUPS`: social media, messaging apps, video
  sharing, Malayalam + Gulf news, job hunting. Static list, **not** FortiGuard
  categories — works with no licence.
  **WhatsApp is deliberately allowed**: `ALLOW_URLS` is written as `allow` rows
  with ids 1-4, above every block row, because the FortiGate takes the first
  match walking the table. Never add a whatsapp domain to a block group.
  `utm.update_urls(fg, urls, log, profile=...)` rewrites *only* the table the
  chosen profile reads from -- the action behind the GUI's "Update blocked
  sites now" and `configure-utm-filters.py --update-urls`, for changing the
  list on a live branch without touching anything else. Since v1.2.1 the table
  is resolved **from the profile** (`profile_table()`) instead of assuming id
  1, so aiming it at `default` / `monitor-all` / `wifi-default` edits that
  profile's own list and cannot overwrite the branch one. `list_profiles()`
  and `describe_target()` back the GUI's profile picker and the CLI's
  `--list-profiles`.
- **Everything in `fortigate/` and `scripts/` must stay ASCII.** The CLI prints
  these log lines to a cp1252 Windows console, where a stray arrow or en dash
  crashes the script with UnicodeEncodeError. The GUI is fine either way; the
  package is shared, so it is the package that has to be plain.

> No WAN cable is connected yet, so the box has no internet uplink until an ISP
> line is plugged into wan1 on-site.

## Factory-default gotchas (validated by a real reset on 2026-08-15)

The staged unit was factory reset and re-provisioned from scratch; **39/39
verification checks passed**. Three things a factory device does that the
scripts now handle:

1. **Manage URL reverts** to `https://192.168.1.99`, admin / blank password.
2. **`internal2`–`internal5` return to the `internal` hardware switch.** A
   switch member cannot hold an IP — and the FortiGate ACCEPTS the interface
   write, applies `alias`/`role` and **silently drops `ip`**. `set_lan_interface()`
   now breaks the port out first *and reads the IP back*, failing loudly if it
   did not stick. Never trust a `success` status from a cmdb PUT alone.
3. **LAN `192.168.1.99/24` collides with Staff WiFi `192.168.1.1/24`.** Overlapping
   interface subnets are rejected, so the LAN must move first — which kills the
   session doing the work. Hence **two-phase provisioning**:
   `--lan-only` → reconnect at the new IP → `--skip-lan` for the rest.
   In `set_lan_management()` the **DHCP server is updated BEFORE the interface
   IP**, so a failure can never leave the LAN handing out addresses on the old
   subnet with no way back in.

## Branch deployment model

Each FortiGate is **staged centrally**, then shipped to a branch where the
**ISP PPPoE username/password are entered on-site** and the WAN cable + WiFi AP
are connected. The 60F has **no built-in WiFi radio** — "WiFi LAN" = a LAN port
that a separate access point plugs into.

## Updated branch design (from the IT team)

Per branch: **one ISP** (wan1) and **three inside networks** — **LAN**,
**Staff WiFi**, **Guest WiFi**. All need to reach HO except Guest.

This is now **built on the staged unit** (internal / internal2 / internal3, all
NATed out wan1). What remains from this design is the VPN back to HO.

## Done since the original plan

- **Three inside networks + IPs + DHCP ranges** — built and scripted. ✅
- **Guest WiFi = internet only**, isolated from LAN/Staff/VPN. ✅
- **Web filter + application control** — built and scripted. ✅

## HO VPN + DDNS -- BUILT 2026-08-21, NOT YET TESTED ON HARDWARE

Reverses the old "configured on-site by hand, never scripted" decision. Phases
1-3 of `docs/ddns-vpn-plan.md` are implemented; **phase 4, the bench test, has
not happened.** Nothing here has been sent to a real FortiGate -- every API
body came from documentation and was proven only against stubs.

| Piece | Where |
|---|---|
| DDNS engine | `fortigate/ddns.py` -- `apply_ddns`, `verify`, `resolve`, `name_is_free`, `wan_address` |
| VPN engine | `fortigate/vpn.py` -- `VpnSpec`, `validate`, `preview`, `apply_vpn`, `verify`, `tunnel_status`, `remove_vpn` |
| GUI | tabs 6 (Dynamic DNS) and 7 (VPN Tunnel) |
| CLI | `scripts/configure-ddns.py`, `scripts/configure-vpn.py` |

Rules the code enforces, do not weaken them:

- **Two independent names.** DDNS name max 24; VPN branch name max 11 because
  `<name>toHO` must fit FortiGate's 15-character interface-name limit. The GUI
  refuses the 12th character. Neither field copies from the other.
- **Guest never crosses.** `vpn.GUEST_PORTS` is refused in `local_selectors()`
  and in `validate()`, not merely unticked in the GUI.
- **The branch always dials** -- phase2 `auto-negotiate` + `keepalive`, phase1
  `dpd on-idle`. A branch behind CGNAT registers an address HO cannot reach, so
  the design never depends on HO initiating.
- **The blackhole route is not optional.** Without it, HO traffic falls to the
  default route when the tunnel drops and leaves the branch unencrypted.
- **Local subnets are read from the device**, never from the form -- only the
  firewall knows what its ports are really addressed as.

Still open before it can be trusted: HO's real subnets, HO's IKE version and
proposals (defaults are IKEv2 / aes256-sha256 aes128-sha256 / DH14), and
whether the PSK is per branch or estate-wide. See the end of the plan.

## Still open

1. **Unique subnets per branch** (required once the VPN is up) — proposed
   pattern `10.<branch>.0.x` = LAN, `.10.x` = Staff, `.20.x` = Guest;
   HO `10.0.0.0/24`. The staged unit currently uses 172.21.0.0/21 +
   192.168.1.0/24 + 192.168.2.0/24, which will collide across branches.
   Saved branches (`branches/`) are how this is kept straight — one template per
   site, each with its own addresses. Never re-use a template for a second
   branch unchanged; change the addresses, then "Save as new…".
2. **Rotate the admin password** per unit (see Cautions).
3. **Register the unit with FortiGuard** once it has an uplink, so application
   control signatures update (see Licensing state).

When the VPN is added on-site, give phase-2 selectors + policies to `internal`
and `internal2` only — **never `internal3`**, or guests reach HO.

## Related documents (outside this repo)

- Visual project doc: `docs/project-documentation.html` (also an Artifact).
- **DDNS + VPN implementation plan: `docs/ddns-vpn-plan.md`** (text, read this
  one) and `docs/ddns-vpn-plan.html` (visual; Artifact:
  https://claude.ai/code/artifact/b4097763-b966-46e8-9809-f18b7211a36e).
- HO screenshots the plan was designed from: `docs/reference/*.png` — the DNS
  page's Dynamic DNS table and the three IPsec wizard steps.
- Older VPN design plan and the Git account-switch guide lived at
  `C:\Users\aadil\…` on the original machine and are **not in this repo**.

## Licensing state (important)

All FortiGuard entitlements read **`pending`** — the unit has never had an
uplink. The static URL filter is unaffected. Application control enforces on
the firmware's bundled signature DB but will not update until the unit is
registered and online. **FortiGuard web *category* filtering must not be used**
until then — it fails silently without live rating lookups.

## GUI notes

- Run from source: `python branch_gui.py`. Build: `python build_exe.py`.
- Operator documentation is `docs/gui-user-guide.md` — written for someone with
  no networking background; the **User guide** button in the app opens it.
- **`certificate-inspection` is the default everywhere (changed 2026-08-24).**
  It blocks the same sites with nothing to install on clients; deep inspection
  breaks every secure site when the CA is missing, which is the state most
  branches are actually in. The default lives in FIVE places and they must
  agree: `DEFAULT_SSL` in `branch_gui.py`, `BranchSpec.ssl_mode`,
  `utm.attach_filters()`, `configure-utm-filters.py --ssl`, and the
  `pick(args.ssl, ...)` fallback in `provision-branch.py`.
- Device calls run on a worker thread and report back through a `queue`. Calling
  the device on the UI thread freezes the window and Windows greys it out.
- **Saved branches bar** sits above the notebook, not inside a tab — picking a
  branch refills fields on every tab, so it must be reachable from all of them.
  Selecting from the combobox loads immediately (`act_branch_load`). Everything
  it does goes through `fortigate.templates`; never write template JSON from the
  GUI directly, or the CLI and GUI libraries drift apart.
- **Frozen-path rule:** operator files (backups, profiles, `.env`) resolve via
  `app_dir()` = the folder holding the `.exe`; bundled read-only resources via
  `bundled()` = `sys._MEIPASS`. Never use `Path(__file__).parent` for a file the
  operator should keep — in a one-file build that is a temp folder Windows
  deletes on exit, so backups would silently disappear.
- Two builds: `dist/portable/…exe` (10.5 MB, **truly standalone**) and
  `dist/FortiGate Branch Provisioner/` (25.3 MB folder, **the .exe alone will
  not start** — it needs `_internal`). Send the portable one; mail systems block
  `.exe` attachments even inside a zip, so share a cloud link.

## Cautions

- **Rotate the admin password** — it has been shared in plain text; use a unique
  one per branch unit.
- **Never commit `.env` or `configs/*.conf`** — both are git-ignored (they hold
  secrets). Verify before pushing anywhere.
- Over the VPN, **subnets must be unique per branch** or routing breaks.
- **Deep inspection needs the FortiGate CA on every client** (`Fortinet_CA_SSL`,
  System → Certificates). Without it, every HTTPS site throws a cert warning.
  Certificate-pinned apps may need an `ssl-exempt` entry.
- Blocking **Remote.Access (7) also blocks RDP, VNC and Telnet outbound.**
