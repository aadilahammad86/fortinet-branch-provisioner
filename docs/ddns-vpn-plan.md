# DDNS + VPN tabs — implementation plan (v1.2)

> **Status 2026-08-21: phases 1-3 BUILT, phase 4 (bench test) NOT DONE.**
> `fortigate/ddns.py`, `fortigate/vpn.py`, GUI tabs 6 and 7 and the two CLI
> scripts exist and pass their stub tests. Nothing has been sent to a real
> FortiGate. Written 2026-08-17 with no device reachable, so
> every API body below comes from documentation, not from a live unit.
>
> Visual version of this plan: `docs/ddns-vpn-plan.html`
> (also an Artifact: https://claude.ai/code/artifact/b4097763-b966-46e8-9809-f18b7211a36e)
>
> Screenshots this was designed from: `docs/reference/*.png`

## The job

Both ends of the HO VPN sit on **dynamic ISP addresses**, so each side finds the
other by name, not by IP. HO already registers `homadina.fortidyndns.com` on
WAN (port1). A branch has no name yet, so HO cannot dial it.

Two new GUI tabs, in this order:

| Tab | Purpose |
|---|---|
| **6. Dynamic DNS** | Register this branch's `<name>.fortidyndns.com` with FortiGuard. |
| **7. VPN Tunnel** | Build the IPsec tunnel to HO, route LAN + Staff over it, keep Guest out. |

**Both are ON-SITE steps.** FortiGuard DDNS only registers once the unit is
online and registered with FortiCare — every entitlement on the staged unit
still reads `pending`.

## Naming — two independent names (decided 2026-08-20)

The DNS name and the tunnel name are **separate fields on separate tabs**. They
may read the same; nothing forces it. Neither tab copies from the other.

| Field | Max | Why that limit |
|---|---|---|
| DNS tab, branch name | **24** | It is a DNS label (63 is the technical ceiling). |
| VPN tab, branch name | **11** | `<name>` + `toHO` must stay inside FortiGate's **15-character interface-name limit**. |

- The tunnel name is **derived and read-only**: `vpn_branch_name + "toHO"`,
  e.g. `alaintoHO`. The field refuses the 12th character rather than failing
  later; a live counter shows `alain - 5/11`.
- Both fields: lowercase letters, digits, hyphens; no leading/trailing hyphen.
- The VPN tab shows this branch's registered DNS name as a **read-only
  reference line** (it is what HO points at) — information, not a link.

## Tab 6 — Dynamic DNS

Form: branch name (24) + domain suffix dropdown + internet port dropdown +
"this FortiGate sits behind an ISP router" checkbox.
Buttons: **Check the name is free** / **Apply** / **Check it works**.

Suffixes offered: `fortidyndns.com` (default, matches HO), `fortiddns.com`,
`float-zone.com`.

```
PUT/POST /api/v2/cmdb/system/ddns        # keyed by integer ddnsid
{
  "ddnsid": 1,                            # reuse the entry on this interface, else max+1
  "ddns-server": "FortiGuardDDNS",
  "ddns-domain": "alain.fortidyndns.com", # full name including the suffix
  "monitor-interface": [{"interface-name": "wan1"}],
  "use-public-ip": "disable"              # matches HO (Public IP column reads Disabled)
}
```

- FortiGuard DDNS names are **globally unique**; only Fortinet Support can
  release one that is taken. Hence the pre-check.
- No REST status exists for registration. **Verify by resolving the name from
  the laptop and comparing it to the WAN address read off the device.**

## Tab 7 — VPN Tunnel

The form asks four things: branch name (11), HO's DDNS name, pre-shared key,
HO's subnets. Two interface pickers, mirroring the wizard:

- **Internet port out** = wizard step 2 "Outgoing Interface" (the WAN).
- **Inside ports in** = wizard step 3 "Local interface" (internal, internal2).
  `internal3` (Guest) is rendered but **permanently locked off** — enforced in
  the package, so no code path can put it in a selector or a policy.

Local subnets are read from the branch's own config, not typed.
Internet Access stays **None** (split tunnel; branch internet still exits wan1).

Advanced, collapsed: IKE version, proposals, DH group, DPD, keylife, NAT-T.

### phase1-interface (POST /api/v2/cmdb/vpn.ipsec/phase1-interface)

```
{
  "name": "alaintoHO",                      # becomes the tunnel interface
  "type": "ddns",
  "remotegw-ddns": "homadina.fortidyndns.com",
  "interface": "wan1",
  "ike-version": "2",                       # MUST match HO
  "peertype": "any",
  "authmethod": "psk",
  "psksecret": "...",                       # never logged, never saved
  "proposal": "aes256-sha256 aes128-sha256",
  "dhgrp": "14",
  "dpd": "on-idle",
  "dpd-retryinterval": 60,
  "nattraversal": "enable",
  "net-device": "disable",
  "wizard-type": "static-fortigate",        # GUI then shows it as a wizard tunnel
  "comments": "Built by FortiGate Branch Provisioner"
}
```

### phase2-interface — one selector per inside network

```
{
  "name": "alaintoHO-lan",
  "phase1name": "alaintoHO",
  "src-addr-type": "subnet", "src-subnet": "172.21.0.0 255.255.248.0",
  "dst-addr-type": "name",   "dst-name": "alaintoHO_remote",
  "auto-negotiate": "enable",   # the branch initiates
  "keepalive": "enable",        # and holds the tunnel up with no traffic
  "pfs": "enable", "dhgrp": "14"
}
# second selector: alaintoHO-staff, src 192.168.1.0/24
```

### Everything else the wizard creates

| Object | Named | Detail |
|---|---|---|
| `firewall/address` | `alaintoHO_local_lan`, `_local_staff`, `_remote_1`… | one per subnet on each side |
| `firewall/addrgrp` | `alaintoHO_local`, `alaintoHO_remote` | keeps the policies readable |
| `firewall/policy` | `vpn_alaintoHO_out` | internal+internal2 to tunnel, local grp to remote grp, **NAT off** |
| `firewall/policy` | `vpn_alaintoHO_in` | the mirror image |
| `router/static` | per HO subnet | `dst` = HO subnet, `device` = alaintoHO, distance 10 |
| `router/static` | blackhole | same subnet, `blackhole enable`, **distance 254** |

**The blackhole route is not optional.** Without it, when the tunnel drops,
HO-bound traffic falls to the default route and leaves the branch over the open
internet — NATed, unencrypted and silently.

Read every write back before reporting success — the same rule as the rest of
the package (a cmdb PUT can return `success` and silently drop a field).

## Both ends on DDNS — and why the branch always initiates

Fortinet's documented pattern is one static side and one DDNS side.
Both-ends-DDNS works, with two weak points: DNS lag after an ISP address change,
and **CGNAT** — a branch on carrier NAT registers an address nobody outside can
reach, so HO could never dial in.

So the design never depends on HO dialling: `auto-negotiate enable` +
`keepalive enable` on phase 2, `dpd on-idle` on phase 1. The branch builds the
tunnel itself, retries every 5 seconds, and holds it up. The branch DDNS name
stays useful for HO-initiated management.

## Validation (all local, before anything is sent)

| Check | Result |
|---|---|
| VPN branch name 1-11 chars, `a-z 0-9 -` | block |
| DNS branch name 1-24 chars, same set | block |
| HO name is a valid FQDN | block |
| PSK at least 6 characters (FortiGate minimum) | block |
| HO subnets do not overlap ANY branch subnet | block |
| At least one inside port ticked | block |
| Chosen internet port exists and is up | warn |
| Staff WiFi still on the default 192.168.1.0/24 (same on every branch) | warn |
| DDNS name already resolves elsewhere | warn |
| Guest in any selector or policy | impossible by construction |

## Code layout (project rule: no device logic in the GUI or the scripts)

| File | New/changed | Contents |
|---|---|---|
| `fortigate/ddns.py` | new | `SUFFIXES`, `full_name()`, `apply_ddns()`, `read_ddns()`, `clear_ddns()`, `resolve()`, `verify_ddns()` |
| `fortigate/vpn.py` | new | `tunnel_name()`, `validate_vpn()`, `preview_vpn()`, `apply_vpn()`, `tunnel_status()`, `verify_vpn()`, `remove_vpn()` |
| `fortigate/branch.py` | changed | new `BranchSpec` fields, overlap checks in `validate()` |
| `fortigate/templates.py` | changed | add `vpn_psk` to `SECRET_KEYS` (unknown keys already load safely) |
| `branch_gui.py` | changed | two tabs, character-limited name fields, derived-name label, worker-thread jobs |
| `scripts/configure-ddns.py` | new | `--name --suffix --port --public-ip --verify` |
| `scripts/configure-vpn.py` | new | `--branch --branch-name --ho --psk --preview --verify --remove` |

`provision-branch.py` is untouched — staging and on-site work stay separate.

### New BranchSpec / template fields

`ddns_name` (24), `ddns_suffix`, `ddns_port`, `ddns_public_ip`,
`vpn_branch_name` (11), `vpn_remote_ddns`, `vpn_wan_port`, `vpn_inside_ports`,
`vpn_remote_subnets`, `vpn_ike` plus proposals and DH group.
**`vpn_psk` is NEVER saved** — same rule as the admin and PPPoE passwords.

Suggested, not yet decided: HO's name and the crypto profile are identical for
every branch, so they belong in an **estate defaults** file beside `.env`,
pre-loaded into each new branch, rather than retyped into every template.

## On-site sequence

1. Pick the branch from Saved branches
2. Enter the ISP PPPoE credentials, plug in the WAN cable (Internet tab)
3. Confirm the branch is online (Connect tab)
4. DDNS: Check the name is free, Apply, Check it works
5. VPN: HO name + PSK, Preview, Apply
6. Check tunnel — expect up within about a minute
7. Update the saved branch so the DDNS name is recorded

Step 4 must finish before HO adds its end — HO's tunnel points at a name that
has to resolve already.

## Phases

1. `fortigate/ddns.py` + DDNS tab + `configure-ddns.py`
2. `fortigate/vpn.py` (the engine, CLI-drivable)
3. VPN Tunnel tab
4. **BENCH TEST — a gate, not a formality.** Nothing ships before it.
5. Template fields, estate defaults, docs, v1.2 build and release

## Bench test checklist (phase 4)

Staged 60F + an internet uplink + HO's real tunnel at the other end.

1. Back up the config first
2. DDNS apply, reads back, resolves from the laptop, matches the WAN address
3. Apply twice — the second run reports "already correct" and changes nothing
4. VPN Preview on a clean unit, then on a configured one (the second must list
   zero changes)
5. Apply, then confirm: phase1, both selectors, 4 addresses, 2 groups,
   2 policies, 2 routes including the blackhole
6. The tunnel comes up against HO; a branch LAN machine reaches an HO machine
7. **A Guest machine cannot** — the test that matters most
8. Pull the WAN cable: HO traffic must FAIL, not leak out of the internet
   connection. This is what proves the blackhole route.
9. Save branch, delete tunnel, reload branch, re-apply — identical result
10. Factory-reset rehearsal, as was done for v1.0

## OPEN DECISIONS — answer before phase 1/2

1. **HO's networks** — what subnet(s) sit behind `homadina.fortidyndns.com`?
   The old plan proposes `10.0.0.0/24`. NEEDED.
2. **HO's IKE version and proposals** — the branch must match exactly or the
   tunnel dies with unhelpful logs. NEEDED.
   Default if unanswered: IKEv2, aes256-sha256 / aes128-sha256, DH 14.
3. **One PSK for the estate, or one per branch?** Recommended: per branch.
4. **"Incoming internet port"** — the planned reading is the wizard's two
   pickers (outgoing WAN + inside ports). The alternative is a *second WAN for
   failover*, which is a bigger, separate feature. CONFIRM.
5. Linking the two names later — separate for now, revisit after a few branches.
6. Domain suffix default `fortidyndns.com` (matches HO).
7. PSK never saved in templates — recommended, confirm.

## Sources checked while planning

- config system ddns (CLI reference 7.4)
- Technical Tip: How to configure Dynamic DNS FortiGate (the FortiGuardDDNS CLI example)
- phase1-interface / phase2-interface field schemas (Ansible FortiOS collection)
- Technical Tip: IPsec VPN phase1 interface name characters limitation (the 15-char limit)
- Technical Tip: Using the IPsec auto-negotiate and keepalive options
- Technical Tip: How to configure VPN Site to Site between FortiGates (what the wizard creates)
