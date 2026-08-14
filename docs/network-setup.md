# Network Setup

_FortiGate-60F (FGT60FTK25040370), FortiOS v7.4.9_

## LAN interfaces

| Interface | Subnet | DHCP pool | Purpose |
|-----------|--------|-----------|---------|
| `internal` (hw switch, incl. internal1) | 172.21.0.0/21, gw 172.21.0.1 | 172.21.0.100–.230 | PC / management LAN |
| `internal2` | 192.168.1.0/24, gw 192.168.1.1 | 192.168.1.2–.26 (25) | **WiFi LAN** — plug AP here |

Unused: `dmz` (10.10.10.1, down), `internal3/4/5` (no IP, down).

## WAN interfaces

| Interface | Mode | Link | Default route |
|-----------|------|------|---------------|
| `wan1` | PPPoE (needs ISP user/pass) | **down (no cable)** | auto (defaultgw=enable) |
| `wan2` | DHCP | **down (no cable)** | auto (defaultgw=enable) |

## Firewall policies (internet / NAT)

| ID | Name | Src → Dst | NAT |
|----|------|-----------|-----|
| 1 | (default) | internal → wan1 | yes |
| 2 | WiFi-LAN-to-wan1 | internal2 → wan1 | yes |
| 3 | WiFi-LAN-to-wan2 | internal2 → wan2 | yes |
| 4 | internal-to-wan2 | internal → wan2 | yes |

Both LANs can reach the internet via either WAN once a WAN link is up.

## To actually get internet (physical step required)

The firewall is fully configured but **no WAN port is connected**, so there is
no default route and no internet yet. To bring it up:

- **Easiest — wan2 (DHCP):** plug an internet feed (router/modem LAN port)
  into **wan2**. It will pull an IP + gateway via DHCP, install a default
  route automatically, and both LANs get internet immediately.
- **wan1 (PPPoE):** if your ISP is PPPoE, set the PPPoE username/password on
  `wan1` and connect its cable.

DNS for DHCP clients is served by the FortiGate (192.168.1.1), which forwards
upstream once a WAN is active.
