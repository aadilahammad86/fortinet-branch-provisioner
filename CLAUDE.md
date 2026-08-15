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

## Scripts (in `scripts/`)

| Script | Purpose |
|--------|---------|
| `provision-branch.py` | **Main entry.** Provisions a whole branch. Prompts for WiFi gateway IP + client count (or `--wifi-ip`/`--clients`/`--yes`). Applies interface, DHCP, NAT policies, wan1 PPPoE. Idempotent. |
| `backup-config.py` | Downloads a full config backup to `configs/` (git-ignored; holds secrets). Run before any reset/big change. |
| `pull-device-info.py` | Writes `docs/device-info.md` (model, firmware, interfaces, load). |
| `check-connection.ps1` | Reachability + login test. |
| `configure-wifi-lan.py` | One-off: set internal2 WiFi LAN + DHCP. |
| `configure-internet-access.py` | One-off: create NAT internet policies. |
| `configure-wan1-pppoe.py` | One-off: set wan1 to PPPoE plug-and-play. |
| `configure-wan-dhcp.py` | One-off: set a WAN to DHCP. |

## What has been configured on the staged unit

- **WiFi LAN** on port `internal2`: `192.168.1.1/24`, alias `WiFi-LAN`, allow ping.
- **DHCP** on internal2: `192.168.1.2–192.168.1.26` (25 clients), 7-day lease,
  DNS via FortiGate.
- **Internet NAT policies:** `internal2→wan1`, `internal2→wan2`, `internal→wan2`
  (plus the default `internal→wan1`).
- **wan1 = PPPoE plug-and-play:** mode set, auto default route, **ISP username/
  password intentionally left blank** — entered ON-SITE at each branch.
- A full **config backup** was taken to `configs/FGT60FTK25040370-*.conf`.

> No WAN cable is connected yet, so the box has no internet uplink until an ISP
> line is plugged into wan1 on-site.

## Branch deployment model

Each FortiGate is **staged centrally**, then shipped to a branch where the
**ISP PPPoE username/password are entered on-site** and the WAN cable + WiFi AP
are connected. The 60F has **no built-in WiFi radio** — "WiFi LAN" = a LAN port
that a separate access point plugs into.

## Updated branch design (from the IT team)

Per branch: **one ISP** (wan1) and **three inside networks** — **LAN**,
**Staff WiFi**, **Guest WiFi**. All need to reach HO except Guest.

## Planned next (not yet built)

1. **VPN to Head Office** — hub-and-spoke, **dial-up IPsec**, using **DDNS**
   names (HO's DDNS name is given to each branch). Branches dial in.
2. **Unique subnets per branch** (required for VPN) — proposed pattern
   `10.<branch>.0.x` = LAN, `.10.x` = Staff, `.20.x` = Guest; HO `10.0.0.0/24`.
3. **Guest WiFi = internet only**, isolated from LAN/Staff/VPN (recommended;
   confirm with IT — the note said "all" go to HO, which is unsafe for guests).
4. Fold VPN + DDNS + guest isolation into `provision-branch.py`.

Open questions for IT are documented in the VPN plan page (see below).

## Related documents (outside this repo)

- Visual project doc: `docs/project-documentation.html` (also an Artifact).
- VPN design plan: `C:\Users\aadil\Fortinet-Branch-VPN-Plan.html` (Artifact).
- Git account-switch guide: `C:\Users\aadil\Git-Account-Switch-Guide\`.

## Cautions

- **Rotate the admin password** — it has been shared in plain text; use a unique
  one per branch unit.
- **Never commit `.env` or `configs/*.conf`** — both are git-ignored (they hold
  secrets). Verify before pushing anywhere.
- Over the VPN, **subnets must be unique per branch** or routing breaks.
