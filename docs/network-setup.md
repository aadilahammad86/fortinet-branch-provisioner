# Network Setup

_FortiGate-60F (FGT60FTK25040370), FortiOS v7.4.9_

## LAN interfaces

| Interface | Subnet | DHCP pool | Purpose |
|-----------|--------|-----------|---------|
| `internal` (hw switch, incl. internal1) | 172.21.0.0/21, gw 172.21.0.1 | 172.21.0.100–.230 | **LAN** — PC / management |
| `internal2` | 192.168.1.0/24, gw 192.168.1.1 | 192.168.1.2–.26 (25) | **Staff WiFi** — plug AP here |
| `internal3` | 192.168.2.0/24, gw 192.168.2.1 | 192.168.2.2–.26 (25) | **Guest WiFi** — plug AP here |

Unused: `dmz` (10.10.10.1), `internal4/5` (no IP).

## WAN interfaces

**One ISP per branch — `wan1`.** All three inside networks exit through it.

| Interface | Mode | Link | Default route |
|-----------|------|------|---------------|
| `wan1` | **PPPoE** (primary, distance 5) | **down (no cable)** | auto (defaultgw=enable) |
| `wan2` | DHCP — spare, no policies | **down (no cable)** | auto (defaultgw=enable) |

**wan1 plug-and-play (PPPoE):** ISP username/password are loaded from `.env`
(`FGT_PPPOE_USER` / `FGT_PPPOE_PASS`) via `scripts/configure-wan1-pppoe.py`.
Once set, connecting the wan1 cable dials PPPoE automatically and all three
inside networks get internet through the existing NAT policies — no further
action needed.

`wan2` is still configured for DHCP but has **no firewall policies**, so it
carries no traffic. It is a spare port only.

## Firewall policies (internet / NAT)

| ID | Name | Src → Dst | NAT |
|----|------|-----------|-----|
| 1 | (default) | internal → wan1 | yes |
| 2 | WiFi-LAN-to-wan1 | internal2 → wan1 | yes |
| 5 | Guest-WiFi-to-wan1 | internal3 → wan1 | yes |

All three inside networks reach the internet via wan1 once its link is up.

**Guest isolation:** Guest WiFi (`internal3`) has *only* the wan1 policy. With
no guest→internal / guest→internal2 policy, the FortiGate's default deny keeps
guests off the LAN and Staff WiFi — and off the HO VPN when that is added.
Do not add guest→inside policies.

## Web filter + application control

Applied to policies **#1 (LAN)** and **#2 (Staff WiFi)** only. Guest WiFi is
left unfiltered — it is an untrusted hotspot that is already walled off.
Script: `scripts/configure-utm-filters.py`.

**Web filter `Branch-WebFilter`** — static URL table `Branch-Blocked-Sites`
(id 1), 7 entries, all action `block`:

| URL | Type | Why |
|-----|------|-----|
| `facebook.com` | simple | bare domain |
| `*.facebook.com` | wildcard | www, m, web, … |
| `*.fbcdn.net` | wildcard | Facebook images / static content |
| `youtube.com` | simple | bare domain |
| `*.youtube.com` | wildcard | www, m, music, … |
| `youtu.be` | simple | short links |
| `*.googlevideo.com` | wildcard | YouTube video streams |

This is a **static URL list, not FortiGuard category filtering** — deliberately,
because it works with no FortiGuard licence and no internet lookup.

**Application control `Branch-AppControl`** — blocks two categories, verified
against this unit's own signature database:

| Category | ID | Signatures | Includes |
|----------|----|-----------:|----------|
| Remote.Access | 7 | 91 | Teamviewer, AnyDesk, RDP, VNC, LogMeIn, Telnet, GoToMyPC |
| Social.Media | 23 | 181 | Facebook, Instagram, Twitter, Snapchat, LinkedIn, Flickr |

Everything else passes (`other-application-action: pass`).

> **YouTube is category 5 (Video/Audio), not Social.Media** — the app filter
> does not catch it. YouTube is blocked by the URL filter above. If YouTube
> ever needs to be unblocked, the URL entries are the thing to remove.

> **Remote.Access (7) includes RDP, VNC and Telnet.** This is outbound only, so
> it does not affect HO reaching branch machines over the VPN — but staff
> cannot RDP to anything on the public internet.

### HTTPS deep inspection ⚠️

Both policies use the built-in **`deep-inspection`** SSL profile. Facebook and
YouTube are HTTPS-only, so without decryption the FortiGate cannot see which
site is being requested.

**On-site step, required on every client device:** install the FortiGate CA
certificate (`Fortinet_CA_SSL`, from *System → Certificates*) into the OS /
browser trust store. Without it, every HTTPS site shows a certificate warning.

Certificate-pinned apps (banking apps, some mobile apps, some updaters) can
break under deep inspection. Fix by adding the host to the `deep-inspection`
profile's `ssl-exempt` list rather than turning inspection off.

## FortiGuard licensing ⚠️

This unit reports **all FortiGuard entitlements as `pending`** — it has never
had an uplink, so it has never contacted FortiGuard. Effect:

- **URL filter** — unaffected, works fully offline. ✅
- **Application control** — enforces using the signature database shipped with
  the firmware (2,414 signatures present), but gets no updates until the unit
  is registered and online. ⚠️
- **FortiGuard web *category* filtering** — would silently fail; not used. ❌

Re-check licence status after the branch has internet.

## To actually get internet (physical step required)

The firewall is fully configured but **no WAN port is connected**, so there is
no default route and no internet yet. To bring it up, set the ISP PPPoE
username/password on `wan1` and connect its cable — that is the only supported
uplink for a branch.

**DNS is automatic.** `wan1` has `dns-server-override enable`, so when PPPoE
dials, the ISP-supplied DNS servers replace the system DNS. All three DHCP
servers use `dns-service default` ("same as system DNS"), so clients are handed
the ISP's resolvers as soon as the line is up. Nothing to configure on-site.

Before the line is up the system DNS is FortiGuard's (`96.45.45.45` /
`96.45.46.46`), which is what clients would get if they leased an address
early — they will pick up the ISP's on their next renewal.

Note this means client DNS goes straight out through NAT rather than being
resolved by the FortiGate, so DNS queries are not logged or filtered on the
box. Changing that would mean `dns-service local`, which **also** requires
enabling the DNS server on each interface — set `local` without that and DNS
stops working entirely.
