# FortiGate Branch Provisioner — User Guide

A point-and-click tool for setting up a FortiGate firewall at a branch office.
No command line, no scripting. This guide assumes no networking background
beyond "plug the cable in here".

---

## Getting the program to someone else

There are two builds. **They are not equally portable.**

| Build | What it is | Portable on its own? |
|---|---|---|
| `dist\portable\FortiGate Branch Provisioner.exe` | One file, 10.5 MB | **Yes** — copy that single file anywhere and run it |
| `dist\FortiGate Branch Provisioner\` | An .exe plus an `_internal` folder, 25.3 MB | **No** — the .exe alone will not start. Send the whole folder, zipped |

For emailing to a technician, use the **portable** one-file build.

> **Email will almost certainly block it anyway.** Gmail, Outlook and most
> corporate mail systems refuse `.exe` attachments — including inside a `.zip`,
> and including password-protected archives. Do not fight this. Put the file on
> OneDrive, SharePoint, Google Drive or Teams and send the link.
>
> If it must go by email, rename it to `FortiGate Branch Provisioner.exe_` and
> tell the recipient to remove the trailing underscore after saving. Whether
> that passes depends on the mail filter.

**On first run** Windows shows *"Windows protected your PC"*, because the file
is not code-signed. Click **More info** → **Run anyway**. This is expected and
will happen on every machine until the file is signed with a certificate.

**Where files are saved** Config backups go into a `configs` folder created
next to wherever the .exe is run — so run it from a real folder such as
`Documents`, not straight out of a temp folder or an unopened zip.

---

## Before you start

**What you need**

- The FortiGate, powered on
- A network cable from your laptop to one of the FortiGate's `internal` ports
- The admin password (a brand-new or factory-reset unit has a **blank** password)
- The ISP's PPPoE username and password, if you are finishing the job on-site

**Starting the program**

Double-click **FortiGate Branch Provisioner.exe**.

> Windows may show *"Windows protected your PC"* the first time. That is because
> the program is not code-signed, not because anything is wrong with it. Click
> **More info** → **Run anyway**.

The window has five tabs across the top. Work through them left to right.

**The activity log at the bottom is resizable.** Drag the divider between the
tabs and the log — the pointer changes to a resize arrow when you are on it — to
make the log taller for a long apply run, or shrink it out of the way.

**Tabs scroll when they need to.** If you make the log large, or run on a small
screen, a scrollbar appears on the right of the tab so nothing is ever out of
reach. The mouse wheel scrolls the tab, except when the pointer is over a text
box, which scrolls itself.

A progress bar appears at the bottom right **only while the program is talking
to the firewall**. If it is not there, nothing is running.

---

## Tab 1 — Connect

| Field | What to put in it |
|---|---|
| Device address | `192.168.1.99` for a factory-default unit; `172.21.0.1` for a branch unit already set up |
| Username | `admin` |
| Password | Blank on a factory unit, otherwise your admin password |

Press **Test connection**. On success you get the device name, serial number and
firmware version. If it fails, check:

- Is the cable in an `internal` port (not `wan1`)?
- Does your laptop have an address on the same network? Open Command Prompt and
  run `ipconfig` — if you see `169.254.x.x`, the FortiGate is not giving you an
  address; try a different port.
- Is the address right? A factory-reset unit always goes back to `192.168.1.99`.

### Config backup

Take one **before changing anything** — it is your undo button.

| Control | What it does |
|---|---|
| **Save backups to** | Where backup files are written. Defaults to a `configs` folder next to the program. |
| **Browse…** | Pick any folder — a network share, a USB stick, a per-branch folder. It is created if it does not exist. |
| **Download config backup** | Saves the firewall's full settings, named after its serial number and the date. |
| **Open backup folder** | Opens that folder in File Explorer. |

If the folder cannot be written to — a share you are not signed in to, a
read-only stick — the program says so straight away rather than failing
mid-download.

The folder is remembered in the saved branch (see **Saved branches**), so a
per-branch folder only has to be chosen once.

---

## Tab 2 — Networks

Three networks per branch. Each one gets its own port, its own address range,
and hands out addresses automatically.

### Office LAN

The network your office PCs sit on. **Leave the tick box off unless you are
setting up a brand-new or factory-reset unit** — changing this address
disconnects you on purpose, and there is a dedicated button for it on the Apply
tab that walks you through reconnecting.

### Staff WiFi and Guest WiFi

| Field | Meaning |
|---|---|
| Port | The physical socket the access point plugs into |
| Gateway IP | The firewall's own address on that network — becomes the `.1` |
| Number of devices | How many phones/laptops can connect |
| First address number | Where the range starts (leave at 2) |

As you type, the grey text on the right shows the exact range that will be
handed out — for example `gives 192.168.1.2 – 192.168.1.26`.

> **Guest WiFi gets internet and nothing else.** It has no rule pointing at the
> office LAN or Staff WiFi, so the firewall blocks that automatically. Do not
> add one — that isolation is the whole point of a guest network.

**The 60F has no built-in WiFi radio.** "Staff WiFi" means a network port that a
separate wireless access point plugs into.

---

## Tab 3 — Internet

Each branch uses **one internet line**, on port `wan1`.

- Leave **ISP username** and **ISP password** blank to stage the unit at head
  office. The engineer types them in when they arrive at the branch.
- Fill them in if you are doing the whole job on-site.

**You do not need to configure DNS.** When the internet line connects, the
firewall takes the DNS servers from the ISP automatically and passes them to
every device.

---

## Tab 4 — Filtering

### Blocked websites

A plain list, one site per line. The defaults block Facebook and YouTube,
including the extra addresses those two use behind the scenes — without those,
the sites half-load instead of being blocked.

Add or remove lines freely. Use `*.example.com` to cover everything under a
domain, and `example.com` on its own line for the bare address.

### Blocked application categories

Tick whole categories of app. Two are on by default:

| Category | What it stops |
|---|---|
| **Remote.Access** (7) | Teamviewer, AnyDesk, RDP, VNC, LogMeIn, Telnet |
| **Social.Media** (23) | Facebook, Instagram, Twitter, Snapchat, LinkedIn |

Everything not ticked is allowed.

> **YouTube is in Video/Audio, not Social.Media.** The website list is what
> blocks YouTube. If you ever need to unblock it, remove the `youtube.com`,
> `*.youtube.com`, `youtu.be` and `*.googlevideo.com` lines.

> **Remote.Access includes RDP.** Staff will not be able to remote-desktop to
> anything on the public internet. This does not affect head office connecting
> in over the VPN.

### HTTPS inspection — read this one carefully

Nearly every website today is encrypted. To block a site by name, the firewall
has to see which site is being requested. There are three settings:

| Setting | Blocking works? | What it costs you |
|---|---|---|
| **Deep inspection** | Yes, fully | The FortiGate's certificate must be installed on **every phone and PC**. Without it, browsers block **all** secure sites, not just the ones on your list — the unit looks broken. |
| **Certificate inspection** | Yes, by site name | Nothing. No per-device setup. A blocked site shows a browser error rather than a branded "blocked" page. |
| **No inspection** | **No** | Website blocking silently stops working. Only use this if you have turned the website filter off. |

**Deep inspection is the current branch standard**, so it is selected by
default. If nobody at the branch can install a certificate on every device,
switch to **Certificate inspection** — it blocks the same sites with no setup.

---

## Tab 5 — Apply

**Device name** (optional) — shows up in the firewall's own screens and logs,
e.g. `FGT-BranchB`.

Then, in order:

| Button | What it does |
|---|---|
| **Check settings** | Looks for mistakes in the form. Touches nothing. |
| **Preview changes** | Compares your settings against the firewall and lists exactly what would change. Changes nothing. |
| **Back up now** | Saves a full config backup. |
| **APPLY CONFIGURATION** | Does the work. Everything except the office LAN address. |
| **Verify** | Reads every setting back off the firewall and checks it matches. |

The log at the bottom shows each step. Green is good, orange is skipped, red is
a problem.

**Always Preview before Apply.** It is the difference between "I think this is
right" and "I know what is about to happen."

### Sending a branch to someone else

**Export…** writes the branch selected at the top to a file you can email.
**Import…** adds a file someone sent you to your own branch list.

Day to day you do not need either — use the **Saved branches** bar at the top
of the window (next section).

---

## Saved branches

The bar across the top of the window is your list of branches. Set a branch up
once, save it under a name, and from then on you pick the name and every box on
every tab fills in by itself.

| Button | What it does |
|---|---|
| **Branch** (the drop-down) | Pick a branch. The whole form fills in immediately. |
| **Save as new…** | Asks for a name and saves what is on the tabs right now. |
| **Update** | Overwrites the branch currently picked with what is on the tabs. |
| **Delete** | Removes it from the list. **Nothing on the firewall changes.** |
| **Folder** | Opens the `branches` folder, in case you want to back it up. |

### The normal routine

**First time at a new branch**

1. Fill in the tabs as usual — office LAN, Staff WiFi, Guest WiFi, filtering
2. Press **Check settings**, then **Preview changes**
3. Press **Save as new…** and give it the branch name, e.g. `Al Ain` or
   `Branch 07`
4. Apply as normal

**Every time after that — same branch, or a replacement unit**

1. Pick the branch from the drop-down. Everything fills in.
2. Type the firewall password on the **Connect** tab and press
   **Test connection**
3. **Preview changes**, then **APPLY CONFIGURATION**, then **Verify**

That is the whole job: pick the name, type the password, apply.

### Things worth knowing

- **Passwords are never saved.** Not the firewall admin password, not the ISP
  PPPoE password. You re-type them each time — deliberately, because these
  files get copied onto other laptops and emailed.
- Saved branches live in a **`branches` folder next to the program**. Copy that
  folder to another laptop and that laptop has all your branches.
- Every branch **must use different network addresses** once the head-office
  VPN is in place. Do not save one branch and re-use it unchanged for another —
  change the addresses first, then **Save as new…** under the new name.
- Changing a setting after picking a branch does **not** alter the saved copy.
  Press **Update** if you want to keep the change.

---

## Setting up a brand-new or factory-reset FortiGate

A factory unit puts the office LAN on `192.168.1.99`, which clashes with a Staff
WiFi on `192.168.1.x`. The firewall refuses two networks on the same range, so
the office LAN has to move first — and moving it disconnects you.

This is a **two-part job**:

### Part 1 — move the office LAN

1. Connect at `192.168.1.99` (Tab 1) and press **Test connection**
2. On **Networks**, tick **Change the office LAN address** and check the address
   and range
3. On **Apply**, press **Change office LAN address (disconnects me)**
4. Confirm the warning. **The program will lose contact — that is correct.**

### Part 2 — reconnect and do everything else

5. Open Command Prompt and run:
   ```
   ipconfig /release
   ipconfig /renew
   ```
6. Back in the program, the device address has already been changed to the new
   LAN address. Press **Test connection**.
7. Untick **Change the office LAN address** on the Networks tab
8. Press **APPLY CONFIGURATION**, then **Verify**

If you skip Part 1 and go straight to Apply, the program stops and tells you the
networks overlap — it will not leave the unit half-configured.

---

## On-site checklist

After the firewall is configured and shipped to the branch:

1. **Enter the ISP username and password** on `wan1`, then plug in the internet
   cable
2. **Plug the Staff WiFi access point** into the Staff port (`internal2`)
3. **Plug the Guest WiFi access point** into the Guest port (`internal3`)
4. **If you chose Deep inspection:** install the FortiGate certificate on every
   phone and PC. In the firewall's web console: *System → Certificates →
   Fortinet_CA_SSL → Download*, then install it on each device as a trusted
   root certificate.

   *Skip this only if you selected Certificate inspection.*

---

## Troubleshooting

**"Cannot reach …"** — wrong address, wrong port, or no cable. Run `ipconfig`
and check you have an address on the same network as the firewall.

**"Login rejected"** — wrong password. A factory-reset unit has a blank one.

**"… did not take the IP … the write was accepted but silently ignored"** —
usually means the address clashes with another network on the firewall. Check
the addresses on the Networks tab; **Check settings** normally catches this
before you get here.

**Internet works but every website is blocked** — Deep inspection is on and the
certificate is not installed on that device. Install it, or switch to
Certificate inspection and re-apply.

**Facebook/YouTube are not blocked** — HTTPS inspection is set to *No
inspection*. Switch to Certificate or Deep inspection and re-apply.

**Devices are not getting addresses** — check the access point is in the right
port, and that the network's tick box was on when you applied.

---

## What this tool does not do

- **The VPN to head office.** Set up on-site.
- **Wireless settings.** The 60F has no radio; SSIDs and WiFi passwords are
  configured on the access point itself, not here.
- **Anything on `wan2`.** Branches use one internet line, on `wan1`.
