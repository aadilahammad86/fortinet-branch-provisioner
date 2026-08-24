# Fortinet Project

Working project for managing and interacting with the FortiGate firewall.

## Device

| Field | Value |
|-------|-------|
| Type | FortiGate firewall |
| Admin URL | https://172.21.0.1 |
| Admin user | `admin` |
| Credentials | see `.env` (NOT committed — copy from `.env.example`) |

> The device uses a self-signed certificate, so tools need to skip cert
> verification (`curl -k`, etc.) when connecting.

## Layout

```
fortinet-project/
├── configs/    # exported / backed-up FortiGate configs
├── docs/       # notes, findings, diagrams
├── scripts/    # helper scripts (connectivity, backup, API calls)
└── .env        # secrets — git-ignored
```

## Two ways to drive it

| | For | Entry point |
|---|---|---|
| **GUI** | Field engineers, anyone who shouldn't need a command line | `branch_gui.py` → **FortiGate Branch Provisioner.exe** |
| **CLI** | Automation, scripted rollouts, head-office staging | `scripts/provision-branch.py` |

Both drive the **same engine** in `fortigate/`, so they apply byte-identical
configuration. Fix a bug in the package and both front ends get it.

```
fortigate/
  client.py     login / CSRF / REST plumbing, config backup
  branch.py     interfaces, DHCP, policies, wan1, LAN, validation, verification
  utm.py        web filter + application control
  templates.py  saved-branch library (one JSON file per branch, no passwords)
branch_gui.py   the GUI
build_exe.py    builds the .exe with PyInstaller
scripts/*.py    thin CLI wrappers over the package
```

## The GUI

```
python branch_gui.py           # run from source
python build_exe.py            # dist/FortiGate Branch Provisioner/   (folder)
python build_exe.py --onefile  # dist/portable/…exe                  (single file)
```

The two builds coexist — building one no longer deletes the other.

| Build | Size | Portable alone? |
|---|---|---|
| `dist/portable/FortiGate Branch Provisioner.exe` | 10.5 MB | **Yes.** Hand someone this one file and it runs. |
| `dist/FortiGate Branch Provisioner/` | 25.3 MB, 947 files | **No.** The .exe needs its `_internal` folder; zip the whole folder. |

Use the portable build for sending to a technician. Note that Gmail/Outlook
block `.exe` attachments even inside a zip — share a OneDrive/Drive link
instead. Both builds are unsigned, so Windows SmartScreen prompts on first run.

Config backups are written to a `configs` folder beside the running .exe.

Five tabs — Connect, Networks, Internet, Filtering, Apply — with local
validation, a dry-run **Preview**, one-click **Verify**, config backup, and a
**Saved branches** bar. Full instructions for a non-technical operator:
**`docs/gui-user-guide.md`**.

### Saved branches (templates)

Each branch's settings are stored under a name and re-used: pick the branch
from the drop-down at the top of the window and every field on every tab fills
in. **Save as new…** creates one, **Update** overwrites it, **Export…/Import…**
move one between laptops.

Templates are one JSON file per branch in a `branches/` folder beside the
program (or the repo root when running from source), managed by
`fortigate/templates.py` and shared with the CLI. **No password of any kind is
written to them** — the admin password and the ISP PPPoE password are always
re-typed. The folder is git-ignored: it names real sites and subnets.

The same library from the command line:

```
python scripts/provision-branch.py --list-branches
python scripts/provision-branch.py --branch "Al Ain" --skip-lan --yes --verify
python scripts/provision-branch.py --wifi-ip 10.7.10.1 --guest-ip 10.7.20.1 \
                                   --hostname FGT-Branch07 --save-branch "Branch 07"
```

`--branch` provides the defaults; any flag given alongside it wins, so a saved
branch can be reused with a one-off change.

The build is unsigned, so Windows SmartScreen prompts on first run
(*More info → Run anyway*). Distribute by zipping the `dist/FortiGate Branch
Provisioner` folder.

## Provision a new branch (CLI workflow)

For every new FortiGate, connect to it and run the provisioner. It asks only
for what changes per branch (Staff + Guest WiFi gateway IPs and client counts)
and applies the whole standard template (both interfaces, DHCP, NAT policies,
wan1 PPPoE plug-and-play). ISP PPPoE credentials are entered **on-site**, not
here.

### On a factory-default device: two phases

A factory 60F has its LAN on `192.168.1.99/24`, which **collides** with the
Staff WiFi standard of `192.168.1.1/24` — the FortiGate refuses overlapping
interface subnets. The LAN has to move first, and moving it kills the
management session doing the work. So it is two runs with a reconnect between:

```
:: PHASE 1 -- move the office LAN to 172.21.0.1/21. Drops your session.
python scripts\provision-branch.py --lan-only --yes

:: reconnect: renew your address, then browse to https://172.21.0.1
ipconfig /release & ipconfig /renew
:: set FGT_HOST=172.21.0.1 in .env

:: PHASE 2 -- everything else
python scripts\provision-branch.py --skip-lan ^
                                   --wifi-ip 192.168.1.1 --clients 25 ^
                                   --guest-ip 192.168.2.1 --guest-clients 25 ^
                                   --hostname FGT-BranchB --yes
```

Phase 2 refuses to run if the requested WiFi subnets still overlap the LAN,
and tells you to do phase 1 first.

### On a device whose LAN is already correct: one run

```
# interactive (operator answers prompts)
python scripts\provision-branch.py --skip-lan

# or non-interactive
python scripts\provision-branch.py --skip-lan ^
                                   --wifi-ip 192.168.1.1 --clients 25 ^
                                   --guest-ip 192.168.2.1 --guest-clients 25 ^
                                   --hostname FGT-BranchB --yes
```

Ports are removed from the `internal` hardware switch automatically, so no
manual break-out is needed.

Each branch gets **one ISP** (`wan1`) and **three inside networks**:

| Network | Port | Reaches | Filtered |
|---------|------|---------|----------|
| Office LAN | `internal` | internet (+ HO VPN, added on-site) | yes |
| Staff WiFi | `internal2` | internet (+ HO VPN, added on-site) | yes |
| Guest WiFi | `internal3` | internet **only** — isolated by default deny | no |

## Content filtering

`scripts/configure-utm-filters.py` applies, to the LAN and Staff WiFi only:

- **Web filter** — a static URL list of **133 entries in five groups**
  (`utm.URL_GROUPS`): social media, messaging apps, video sharing, Malayalam +
  Gulf news, job hunting. **WhatsApp is explicitly allowed** — `utm.ALLOW_URLS`
  is written as `allow` rows above every block row, because the FortiGate takes
  the first match walking the table.

### Changing the list on a live branch

`utm.update_urls()` rewrites *only* the URL table — no interfaces, policies or
application control — so a blocklist change is safe to push to a running
branch. Two front ends:

```
# GUI:  Filtering tab -> "Update blocked sites now"
python scripts/configure-utm-filters.py --list-groups
python scripts/configure-utm-filters.py --list-profiles     # what is on the device
python scripts/configure-utm-filters.py --update-urls                 # standard list
python scripts/configure-utm-filters.py --update-urls --groups social,jobs
python scripts/configure-utm-filters.py --update-urls --from-file sites.txt
python scripts/configure-utm-filters.py --update-urls --profile wifi-default
```

The URL table is resolved **from the chosen profile**, not assumed to be id 1,
so pointing this at `default` / `monitor-all` / `wifi-default` edits that
profile's own list and cannot overwrite the branch one. It refuses outright if
the named profile is not on the device, rather than saving a list nothing uses.
The GUI shows the same thing: a profile picker, a **Show profiles** button and a
live "Target: ... -> URL table #N ... used by <policies>" line.
- **Application control** — blocks category **7 Remote.Access** (Teamviewer,
  AnyDesk, RDP, VNC, …) and **23 Social.Media** (Facebook, Instagram,
  Twitter, …). Everything else passes.

Both run over HTTPS **certificate-inspection** by default: it blocks the same sites with nothing to install on client devices. `--ssl deep-inspection` is available, but then the FortiGate CA certificate must be on every client or *all* secure sites fail.

See `docs/network-setup.md` for the full detail and caveats.

## On-site steps at each branch

1. Enter the ISP PPPoE username/password on `wan1`, plug in the WAN cable.
2. Plug the Staff WiFi AP into `internal2`, the Guest WiFi AP into `internal3`.
3. Install the FortiGate CA certificate on client devices.
4. Configure the IPsec VPN back to Head Office.

Set the target device / admin login in `.env` (or pass `--host/--user/--password`).

## Getting started

1. Copy the secrets template and fill it in:
   ```
   copy .env.example .env
   ```
2. Run the connectivity check:
   ```
   powershell -File scripts\check-connection.ps1
   ```

## Security note

Do not commit real credentials. `.env` is git-ignored. Rotate the admin
password if it has ever been shared in plain text.
