"""The IPsec tunnel from a branch back to head office.

This is the API equivalent of the GUI's IPsec wizard run as
"Site to Site -> FortiGate -> Dynamic DNS": a route-based tunnel whose remote
gateway is HO's DDNS name, phase-2 selectors for the inside networks, the two
policies, and the routes -- including the blackhole route the wizard adds and
people forget.

DESIGN NOTES

  * Naming. The phase1 name becomes an interface name, and FortiGate caps
    those at 15 characters, so the branch half is capped at 11 and the tunnel
    is always `<branch>toHO`. Nothing else is derived from it.

  * The branch always dials. Both ends are on DDNS, and a branch behind CGNAT
    registers an address nobody outside can reach -- head office could never
    initiate. So phase 2 gets `auto-negotiate` and `keepalive`, and phase 1
    gets `dpd on-idle`: the tunnel builds itself, retries every few seconds
    and stays up with no traffic. HO simply finds it there.

  * Guest never crosses. `internal3` is not merely absent from the defaults --
    GUEST_PORTS is refused outright, so no caller can put it in a selector or
    a policy by passing the wrong argument.

  * Local subnets are read FROM THE DEVICE, not from the form. The operator
    ticks which ports may reach HO; what those ports are actually addressed as
    is a question only the firewall can answer correctly.

  * The blackhole route is not optional. Without it, HO-bound traffic falls to
    the default route whenever the tunnel is down and leaves the branch over
    the open internet, NATed and unencrypted, with nothing to show for it.
"""
import ipaddress
from dataclasses import dataclass, field

from .client import FortiGateError

SUFFIX = "toHO"
MAX_TUNNEL = 15                       # FortiGate interface-name limit
MAX_BRANCH = MAX_TUNNEL - len(SUFFIX)  # therefore 11

# Never allowed to reach head office, whatever the caller asks for.
GUEST_PORTS = ("internal3",)

DEFAULT_IKE = "2"
DEFAULT_P1_PROPOSAL = "aes256-sha256 aes128-sha256"
DEFAULT_P2_PROPOSAL = "aes256-sha256 aes128-sha256"
DEFAULT_DHGRP = "14"
ROUTE_DISTANCE = 10
BLACKHOLE_DISTANCE = 254


def _noop(_msg):
    pass


@dataclass
class VpnSpec:
    """Everything the branch end of the tunnel needs."""
    branch_name: str = ""              # <= 11 chars; tunnel is <name>toHO
    remote_ddns: str = ""              # e.g. homadina.fortidyndns.com
    psk: str = ""                      # never saved to a template
    wan_port: str = "wan1"             # the tunnel is built on this
    inside_ports: list = field(default_factory=lambda: ["internal", "internal2"])
    remote_subnets: list = field(default_factory=list)   # ["10.0.0.0/24"]
    ike: str = DEFAULT_IKE
    p1_proposal: str = DEFAULT_P1_PROPOSAL
    p2_proposal: str = DEFAULT_P2_PROPOSAL
    dhgrp: str = DEFAULT_DHGRP

    @property
    def tunnel(self):
        return tunnel_name(self.branch_name)


def tunnel_name(branch_name):
    return f"{(branch_name or '').strip()}{SUFFIX}"


# =========================================================================
#  Validation -- everything that can be caught without touching the device
# =========================================================================
def validate(spec):
    errs = []
    name = (spec.branch_name or "").strip()
    if not name:
        errs.append("Enter a short branch name for the tunnel.")
    elif len(name) > MAX_BRANCH:
        errs.append(
            f"Branch name '{name}' is {len(name)} characters. The tunnel name "
            f"'{name}{SUFFIX}' would be {len(name) + len(SUFFIX)}, and a "
            f"FortiGate interface name stops at {MAX_TUNNEL}. Use "
            f"{MAX_BRANCH} characters or fewer.")
    elif not all(c.isalnum() or c == "-" for c in name):
        errs.append("Branch name: letters, digits and hyphens only.")

    ho = (spec.remote_ddns or "").strip()
    if not ho:
        errs.append("Enter head office's internet name "
                    "(e.g. homadina.fortidyndns.com).")
    elif "." not in ho or " " in ho:
        errs.append(f"'{ho}' does not look like an internet name.")

    if len(spec.psk or "") < 6:
        errs.append("The pre-shared key must be at least 6 characters -- "
                    "that is the FortiGate's own minimum.")

    ports = [p for p in spec.inside_ports if p]
    if not ports:
        errs.append("Tick at least one inside network that may reach head "
                    "office.")
    for p in ports:
        if p in GUEST_PORTS:
            errs.append(f"{p} is the Guest network and must never reach head "
                        f"office. Untick it.")

    if not spec.remote_subnets:
        errs.append("Enter at least one head office network, e.g. 10.0.0.0/24.")
    for s in spec.remote_subnets:
        try:
            ipaddress.ip_network(s, strict=False)
        except (ValueError, TypeError):
            errs.append(f"'{s}' is not a valid network. Write it as "
                        f"10.0.0.0/24.")
    return errs


def check_overlap(fg, spec):
    """Warnings: HO networks that collide with this branch's own subnets.

    Overlapping selectors send a branch's own traffic into the tunnel, which
    looks like the whole site falling over rather than a routing mistake.
    """
    warn = []
    remote = []
    for s in spec.remote_subnets:
        try:
            remote.append(ipaddress.ip_network(s, strict=False))
        except (ValueError, TypeError):
            pass
    for port, _mask_form, net in _device_subnets(fg):
        for r in remote:
            if net.overlaps(r):
                warn.append(f"{port} is {net} here, which overlaps head "
                            f"office's {r}. Traffic for one would be sent to "
                            f"the other -- give this branch its own subnets "
                            f"before bringing the tunnel up.")
    return warn


# =========================================================================
#  Reading the device
# =========================================================================
def _device_subnets(fg):
    """[(port, 'ip mask', ip_network)] for every inside interface with an IP."""
    out = []
    for i in fg.results("/api/v2/cmdb/system/interface"):
        raw = (i.get("ip") or "").split()
        if len(raw) != 2 or raw[0] == "0.0.0.0":
            continue
        try:
            net = ipaddress.ip_network(f"{raw[0]}/{raw[1]}", strict=False)
        except ValueError:
            continue
        out.append((i.get("name"), f"{net.network_address} {net.netmask}", net))
    return out


def local_selectors(fg, ports):
    """[(port, 'net mask')] for the ticked ports, read off the firewall."""
    have = {p: s for p, s, _n in _device_subnets(fg)}
    out = []
    for p in ports:
        if p in GUEST_PORTS:
            raise FortiGateError(f"{p} is the Guest network -- it must never "
                                 f"be given a route to head office.")
        if p not in have:
            raise FortiGateError(
                f"Interface '{p}' has no IP address on this firewall, so there "
                f"is no network to send over the tunnel. Set it up on the "
                f"Networks tab first.")
        out.append((p, have[p]))
    return out


def _subnet_pair(cidr):
    net = ipaddress.ip_network(cidr, strict=False)
    return f"{net.network_address} {net.netmask}"


def tunnel_status(fg, name):
    """(up, detail) from the live IPsec monitor."""
    try:
        res = fg.get("/api/v2/monitor/vpn/ipsec").get("results") or []
    except FortiGateError as e:
        return False, f"could not read the VPN monitor: {e}"
    for t in res:
        if t.get("name") != name:
            continue
        proxies = t.get("proxyid") or []
        up = [p for p in proxies if p.get("status") == "up"]
        rx = sum(p.get("incoming_bytes", 0) or 0 for p in proxies)
        tx = sum(p.get("outgoing_bytes", 0) or 0 for p in proxies)
        if up:
            return True, (f"UP -- {len(up)} of {len(proxies)} selectors, "
                          f"{rx} bytes in / {tx} bytes out")
        return False, (f"the tunnel exists but no selector is up "
                       f"({len(proxies)} configured). Usual causes: the "
                       f"pre-shared key does not match head office, head "
                       f"office has no matching tunnel for this branch, or "
                       f"this unit has no internet yet.")
    return False, (f"no tunnel called '{name}' is running. If it was just "
                   f"created, give it a minute; otherwise check the WAN.")


# =========================================================================
#  Building it
# =========================================================================
def _addr_name(tunnel, kind, i):
    return f"{tunnel}_{kind}_{i}"[:79]


def plan(fg, spec):
    """What apply() would write: [(kind, name, detail)]. Touches nothing."""
    t = spec.tunnel
    items = [("phase1", t, f"{spec.wan_port} -> {spec.remote_ddns} "
                           f"(IKEv{spec.ike}, DDNS remote gateway)")]
    locals_ = local_selectors(fg, spec.inside_ports)
    for port, sub in locals_:
        for i, r in enumerate(spec.remote_subnets, start=1):
            items.append(("phase2", f"{t}-{port}-{i}",
                          f"{sub}  <->  {_subnet_pair(r)}"))
    for port, sub in locals_:
        items.append(("address", _addr_name(t, "local", port), sub))
    for i, r in enumerate(spec.remote_subnets, start=1):
        items.append(("address", _addr_name(t, "remote", i), _subnet_pair(r)))
    items.append(("group", f"{t}_local",
                  ", ".join(p for p, _ in locals_)))
    items.append(("group", f"{t}_remote", ", ".join(spec.remote_subnets)))
    items.append(("policy", f"vpn_{t}_out",
                  f"{', '.join(p for p, _ in locals_)} -> {t}, no NAT"))
    items.append(("policy", f"vpn_{t}_in", f"{t} -> "
                  f"{', '.join(p for p, _ in locals_)}, no NAT"))
    for r in spec.remote_subnets:
        items.append(("route", r, f"via {t}, distance {ROUTE_DISTANCE}"))
        items.append(("route", r, f"blackhole, distance {BLACKHOLE_DISTANCE} "
                                  f"-- stops HO traffic leaking to the "
                                  f"internet when the tunnel is down"))
    return items


def preview(fg, spec):
    """[(label, verdict)] comparing the plan against what is already there."""
    out = []
    have_p1 = {p.get("name") for p in
               fg.results("/api/v2/cmdb/vpn.ipsec/phase1-interface")}
    have_p2 = {p.get("name") for p in
               fg.results("/api/v2/cmdb/vpn.ipsec/phase2-interface")}
    have_addr = {a.get("name") for a in fg.results("/api/v2/cmdb/firewall/address")}
    have_grp = {g.get("name") for g in fg.results("/api/v2/cmdb/firewall/addrgrp")}
    have_pol = {p.get("name") for p in fg.results("/api/v2/cmdb/firewall/policy")}
    routes = fg.results("/api/v2/cmdb/router/static")

    for kind, name, detail in plan(fg, spec):
        if kind == "phase1":
            seen = name in have_p1
        elif kind == "phase2":
            seen = name in have_p2
        elif kind == "address":
            seen = name in have_addr
        elif kind == "group":
            seen = name in have_grp
        elif kind == "policy":
            seen = name in have_pol
        else:
            black = "blackhole" in detail
            seen = any(_route_matches(r, name, spec.tunnel, black) for r in routes)
        out.append((f"{kind:<8} {name:<28} {detail}",
                    "already there" if seen else "would be created"))
    return out


def _route_matches(route, cidr, tunnel, blackhole):
    net = ipaddress.ip_network(cidr, strict=False)
    dst = (route.get("dst") or "").split()
    if len(dst) != 2:
        return False
    try:
        if ipaddress.ip_network(f"{dst[0]}/{dst[1]}", strict=False) != net:
            return False
    except ValueError:
        return False
    is_black = (route.get("blackhole") or "disable") == "enable"
    if blackhole:
        return is_black
    return not is_black and route.get("device") == tunnel


def apply_vpn(fg, spec, log=_noop):
    """Create the tunnel end to end. Idempotent; reads back as it goes."""
    errs = validate(spec)
    if errs:
        raise FortiGateError("Fix these first:\n  - " + "\n  - ".join(errs))

    t = spec.tunnel
    locals_ = local_selectors(fg, spec.inside_ports)
    log(f"[..] building '{t}' on {spec.wan_port} to {spec.remote_ddns}")

    # ---- phase 1 ---------------------------------------------------------
    p1 = {
        "name": t,
        "type": "ddns",
        "remotegw-ddns": spec.remote_ddns,
        "interface": spec.wan_port,
        "ike-version": str(spec.ike),
        "peertype": "any",
        "authmethod": "psk",
        "psksecret": spec.psk,
        "proposal": spec.p1_proposal,
        "dhgrp": spec.dhgrp,
        "dpd": "on-idle",
        "dpd-retryinterval": 60,
        "nattraversal": "enable",
        "net-device": "disable",
        "wizard-type": "static-fortigate",
        "comments": "Branch to head office -- FortiGate Branch Provisioner",
    }
    state = fg.upsert("/api/v2/cmdb/vpn.ipsec/phase1-interface", t, p1)
    got = fg.results(f"/api/v2/cmdb/vpn.ipsec/phase1-interface/{t}")[0]
    if got.get("remotegw-ddns") != spec.remote_ddns:
        raise FortiGateError(
            f"phase1 '{t}' was accepted but reports remote gateway "
            f"'{got.get('remotegw-ddns')}' instead of '{spec.remote_ddns}'.")
    log(f"[ok] phase1 {t} {state}: IKEv{spec.ike} to {spec.remote_ddns}")

    # ---- phase 2: one selector per inside network x head office network ---
    for port, sub in locals_:
        for i, r in enumerate(spec.remote_subnets, start=1):
            name = f"{t}-{port}-{i}"[:35]
            body = {
                "name": name, "phase1name": t,
                "src-addr-type": "subnet", "src-subnet": sub,
                "dst-addr-type": "subnet", "dst-subnet": _subnet_pair(r),
                "proposal": spec.p2_proposal,
                "pfs": "enable", "dhgrp": spec.dhgrp,
                # The branch is always the one that dials -- see module notes.
                "auto-negotiate": "enable", "keepalive": "enable",
            }
            st = fg.upsert("/api/v2/cmdb/vpn.ipsec/phase2-interface", name, body)
            log(f"[ok] phase2 {name} {st}: {sub} <-> {_subnet_pair(r)}")

    # ---- address objects and groups --------------------------------------
    local_names, remote_names = [], []
    for port, sub in locals_:
        n = _addr_name(t, "local", port)
        fg.upsert("/api/v2/cmdb/firewall/address", n, {
            "name": n, "subnet": sub, "type": "ipmask",
            "comment": f"{t}: local {port}"})
        local_names.append(n)
    for i, r in enumerate(spec.remote_subnets, start=1):
        n = _addr_name(t, "remote", i)
        fg.upsert("/api/v2/cmdb/firewall/address", n, {
            "name": n, "subnet": _subnet_pair(r), "type": "ipmask",
            "comment": f"{t}: head office"})
        remote_names.append(n)
    log(f"[ok] addresses: {len(local_names)} local, {len(remote_names)} head office")

    lgrp, rgrp = f"{t}_local", f"{t}_remote"
    for gname, members in ((lgrp, local_names), (rgrp, remote_names)):
        fg.upsert("/api/v2/cmdb/firewall/addrgrp", gname, {
            "name": gname, "member": [{"name": m} for m in members],
            "comment": f"{t}"})
    log(f"[ok] groups {lgrp} / {rgrp}")

    # ---- policies, both directions, NAT off ------------------------------
    ports = [{"name": p} for p, _ in locals_]
    for pname, src_if, dst_if, src_a, dst_a in (
            (f"vpn_{t}_out", ports, [{"name": t}], lgrp, rgrp),
            (f"vpn_{t}_in", [{"name": t}], ports, rgrp, lgrp)):
        body = {
            "name": pname,
            "srcintf": src_if, "dstintf": dst_if,
            "srcaddr": [{"name": src_a}], "dstaddr": [{"name": dst_a}],
            "action": "accept", "schedule": "always",
            "service": [{"name": "ALL"}],
            "nat": "disable",              # VPN traffic must not be NATed
            "status": "enable", "logtraffic": "all",
            "comments": f"{t}: branch <-> head office",
        }
        existing = next((p for p in fg.results("/api/v2/cmdb/firewall/policy")
                         if p.get("name") == pname), None)
        if existing:
            fg.call("PUT",
                    f"/api/v2/cmdb/firewall/policy/{existing['policyid']}", body)
            log(f"[ok] policy {pname} updated")
        else:
            r = fg.call("POST", "/api/v2/cmdb/firewall/policy", body)
            log(f"[ok] policy {pname} created (#{r.get('mkey')})")

    # ---- routes: one via the tunnel, one blackhole behind it -------------
    for cidr in spec.remote_subnets:
        dst = _subnet_pair(cidr)
        routes = fg.results("/api/v2/cmdb/router/static")
        if not any(_route_matches(r, cidr, t, False) for r in routes):
            fg.call("POST", "/api/v2/cmdb/router/static", {
                "dst": dst, "device": t, "distance": ROUTE_DISTANCE,
                "comment": f"{t}: head office"})
            log(f"[ok] route {cidr} via {t} (distance {ROUTE_DISTANCE})")
        else:
            log(f"[skip] route {cidr} via {t} already there")
        if not any(_route_matches(r, cidr, t, True) for r in routes):
            fg.call("POST", "/api/v2/cmdb/router/static", {
                "dst": dst, "blackhole": "enable",
                "distance": BLACKHOLE_DISTANCE,
                "comment": f"{t}: drop HO traffic when the tunnel is down"})
            log(f"[ok] blackhole route {cidr} (distance {BLACKHOLE_DISTANCE}) "
                f"-- HO traffic fails instead of leaking to the internet")
        else:
            log(f"[skip] blackhole route {cidr} already there")

    log(f"[ok] '{t}' is built. It dials head office itself; give it a minute, "
        f"then press Check tunnel.")
    return t


def verify(fg, spec):
    """[(label, passed, detail)] over everything apply_vpn() writes."""
    t = spec.tunnel
    out = []

    try:
        p1 = fg.results(f"/api/v2/cmdb/vpn.ipsec/phase1-interface/{t}")[0]
    except (FortiGateError, IndexError):
        return [(f"tunnel {t} exists", False, "MISSING")]
    out.append((f"tunnel {t} exists", True, "found"))
    out.append(("remote gateway", p1.get("remotegw-ddns") == spec.remote_ddns,
                p1.get("remotegw-ddns") or "(none)"))
    out.append(("remote gateway type is DDNS", p1.get("type") == "ddns",
                p1.get("type") or "?"))
    out.append(("built on the right WAN", p1.get("interface") == spec.wan_port,
                p1.get("interface") or "?"))

    p2s = [p for p in fg.results("/api/v2/cmdb/vpn.ipsec/phase2-interface")
           if p.get("phase1name") == t]
    want = len(spec.inside_ports) * max(len(spec.remote_subnets), 1)
    out.append(("phase 2 selectors", len(p2s) == want,
                f"{len(p2s)} (expected {want})"))
    out.append(("branch dials by itself",
                all((p.get("auto-negotiate") or "disable") == "enable" for p in p2s),
                "auto-negotiate on all selectors"))

    pols = {p.get("name"): p for p in fg.results("/api/v2/cmdb/firewall/policy")}
    for pname in (f"vpn_{t}_out", f"vpn_{t}_in"):
        p = pols.get(pname)
        out.append((f"policy {pname}", bool(p), "found" if p else "MISSING"))
        if p:
            out.append((f"{pname}: NAT off", p.get("nat") == "disable",
                        p.get("nat") or "?"))

    routes = fg.results("/api/v2/cmdb/router/static")
    for cidr in spec.remote_subnets:
        out.append((f"route {cidr} via {t}",
                    any(_route_matches(r, cidr, t, False) for r in routes), ""))
        out.append((f"blackhole route {cidr}",
                    any(_route_matches(r, cidr, t, True) for r in routes),
                    "stops HO traffic leaking when the tunnel is down"))

    # Guest must not have been given a way in, by any route.
    guest_in_p2 = [p.get("name") for p in p2s
                   if any(g in (p.get("src-subnet") or "") for g in GUEST_PORTS)]
    out.append(("Guest network excluded", not guest_in_p2,
                "no guest selector" if not guest_in_p2 else str(guest_in_p2)))

    up, detail = tunnel_status(fg, t)
    out.append(("tunnel is up", up, detail))
    return out


def remove_vpn(fg, branch_name, log=_noop):
    """Delete everything apply_vpn() created, in dependency order."""
    t = tunnel_name(branch_name)
    for r in fg.results("/api/v2/cmdb/router/static"):
        if (r.get("comment") or "").startswith(f"{t}:"):
            fg.call("DELETE", f"/api/v2/cmdb/router/static/{r.get('seq-num')}")
            log(f"[ok] route #{r.get('seq-num')} removed")
    for p in fg.results("/api/v2/cmdb/firewall/policy"):
        if p.get("name") in (f"vpn_{t}_out", f"vpn_{t}_in"):
            fg.call("DELETE", f"/api/v2/cmdb/firewall/policy/{p['policyid']}")
            log(f"[ok] policy {p.get('name')} removed")
    for g in (f"{t}_local", f"{t}_remote"):
        try:
            fg.call("DELETE", f"/api/v2/cmdb/firewall/addrgrp/{g}")
            log(f"[ok] group {g} removed")
        except FortiGateError:
            pass
    for a in fg.results("/api/v2/cmdb/firewall/address"):
        if (a.get("name") or "").startswith(f"{t}_"):
            try:
                fg.call("DELETE", f"/api/v2/cmdb/firewall/address/{a['name']}")
                log(f"[ok] address {a['name']} removed")
            except FortiGateError:
                pass
    for p in fg.results("/api/v2/cmdb/vpn.ipsec/phase2-interface"):
        if p.get("phase1name") == t:
            fg.call("DELETE",
                    f"/api/v2/cmdb/vpn.ipsec/phase2-interface/{p['name']}")
            log(f"[ok] selector {p['name']} removed")
    try:
        fg.call("DELETE", f"/api/v2/cmdb/vpn.ipsec/phase1-interface/{t}")
        log(f"[ok] tunnel {t} removed")
    except FortiGateError as e:
        log(f"[!] could not remove tunnel {t}: {e}")
