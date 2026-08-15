# Fortinet Branch Project — Working Context

> This file is auto-loaded by Claude Code when a session starts in this folder.
> It is the "memory" for the fortinet-project workspace. Keep it up to date.

## What this project is

Tooling to set up **Fortinet FortiGate firewalls for branch offices** in a fast,
repeatable way. We built the full config on one unit, saved every step as a
Python script, and made a one-command provisioner for each new branch. The next
big goal is a **VPN from every branch back to Head Office (HO)**.

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
available. Python 3.12 is at
`C:\Users\aadil\AppData\Local\Programs\Python\Python312`. No `jq` installed —
parse JSON in Python.

## Architecture (since the GUI was added)

**All provisioning logic lives in the `fortigate/` package.** The CLI scripts
and the GUI are both thin front ends over it — never add device logic to a
script or to the GUI, add it to the package.

| Module | Holds |
|--------|-------|
| `fortigate/client.py` | `FortiGate` class: login, CSRF, `call/get/results/upsert`, `status()`, `backup()`. The one copy of the connection code that used to be duplicated in all 9 scripts. |
| `fortigate/branch.py` | `BranchSpec` dataclass, `validate()`, `compute_range()`, interface/DHCP/policy/wan1/LAN functions, `provision()`, `preview()` (dry run), `verify()`. |
| `fortigate/utm.py` | Blocked-URL list, category IDs, `apply_filters()`, `clear_filters()`, `verify_filters()`, `licence_state()`. |
| `branch_gui.py` | Tkinter GUI. Worker thread + queue; never call the device on the UI thread. |
| `build_exe.py` | PyInstaller build. `--onedir` by default (fewer AV false positives). |

`BranchSpec` is the single source of truth for a branch's settings — the GUI
form, the CLI flags, saved profiles, `preview()` and `verify()` all speak it.

## Scripts (in `scripts/`)

| Script | Purpose |
|--------|---------|
| `provision-branch.py` | **Main entry.** Provisions a whole branch. Prompts for Staff + Guest WiFi gateway IPs and client counts (or `--wifi-ip`/`--clients`/`--guest-ip`/`--guest-clients`/`--yes`). Applies both interfaces, DHCP, the three wan1 NAT policies, wan1 PPPoE. Idempotent. Pass `--guest-ip -` to skip guest. |
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
  id 1): blocks `facebook.com`, `*.facebook.com`, `*.fbcdn.net`, `youtube.com`,
  `*.youtube.com`, `youtu.be`, `*.googlevideo.com`. Static list, **not**
  FortiGuard categories — works with no licence.
- **App control `Branch-AppControl`**: blocks category **7 = Remote.Access**
  and **23 = Social.Media** (IDs verified against this unit's signature DB).
  Everything else passes.
- Both attached to policies **#1 (LAN)** and **#2 (Staff WiFi)** only —
  Guest (#5) is left unfiltered on purpose.
- **HTTPS = `deep-inspection`** on those two policies.
- **wan1 = PPPoE plug-and-play:** mode set, auto default route, **ISP username/
  password intentionally left blank** — entered ON-SITE at each branch.
- A full **config backup** was taken to `configs/FGT60FTK25040370-*.conf`.

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

## HO VPN — decided: configured ON-SITE

**The HO VPN is no longer a scripting task.** It is set up on-site at each
branch, alongside the ISP PPPoE credentials. Do not build it into
`provision-branch.py` unless the decision changes.

Design context still applies for whoever configures it on-site: hub-and-spoke
**dial-up IPsec** using **DDNS** names (HO's DDNS name is given to each
branch); branches dial in. See the VPN plan page below.

## Still open

1. **Unique subnets per branch** (required once the VPN is up) — proposed
   pattern `10.<branch>.0.x` = LAN, `.10.x` = Staff, `.20.x` = Guest;
   HO `10.0.0.0/24`. The staged unit currently uses 172.21.0.0/21 +
   192.168.1.0/24 + 192.168.2.0/24, which will collide across branches.
2. **Rotate the admin password** per unit (see Cautions).
3. **Register the unit with FortiGuard** once it has an uplink, so application
   control signatures update (see Licensing state).

When the VPN is added on-site, give phase-2 selectors + policies to `internal`
and `internal2` only — **never `internal3`**, or guests reach HO.

## Related documents (outside this repo)

- Visual project doc: `docs/project-documentation.html` (also an Artifact).
- VPN design plan: `C:\Users\aadil\Fortinet-Branch-VPN-Plan.html` (Artifact).
- Git account-switch guide: `C:\Users\aadil\Git-Account-Switch-Guide\`.

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
- The GUI defaults to `deep-inspection` to match the CLI and the staged unit.
  Changing that default means changing `DEFAULT_SSL` in `branch_gui.py` **and**
  the CLI's `--ssl` default, or the two front ends disagree.
- Device calls run on a worker thread and report back through a `queue`. Calling
  the device on the UI thread freezes the window and Windows greys it out.
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
