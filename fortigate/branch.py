"""Branch provisioning: interfaces, DHCP, policies, wan1, LAN, verification.

Every provisioning step lives here so the CLI (scripts/provision-branch.py)
and the GUI (branch_gui.py) apply byte-identical configuration.

The design rule throughout: after a write, read the value back. A FortiGate
cmdb PUT can return `success` while silently dropping fields it cannot apply
-- notably an `ip` on a port that is still a hardware-switch member. Trusting
the status code alone ships half-configured units.
"""
import ipaddress
from dataclasses import dataclass, field

from .client import FortiGateError

# ---- constants shared by every branch ------------------------------------
NETMASK = "255.255.255.0"          # /24 for the WiFi networks
LEASE = 604800                     # 7 days
DNS_SERVICE = "default"            # clients get the system DNS (ISP's, via PPPoE)
STAFF_ALIAS = "WiFi-LAN"
GUEST_ALIAS = "Guest-WiFi"


def _noop(_msg):
    pass


# =========================================================================
#  Branch specification
# =========================================================================
@dataclass
class BranchSpec:
    """Everything that varies per branch. Shared by the GUI and the CLI."""
    hostname: str = ""

    lan_port: str = "internal"
    lan_ip: str = "172.21.0.1"
    lan_mask: str = "255.255.248.0"
    lan_start: str = "172.21.0.100"
    lan_end: str = "172.21.0.230"
    configure_lan: bool = False        # off by default: it drops the session

    staff_port: str = "internal2"
    staff_ip: str = "192.168.1.1"
    staff_clients: int = 25
    staff_first: int = 2
    configure_staff: bool = True

    guest_port: str = "internal3"
    guest_ip: str = "192.168.2.1"
    guest_clients: int = 25
    guest_first: int = 2
    configure_guest: bool = True

    wan_pppoe: bool = True
    pppoe_user: str = ""
    pppoe_pass: str = ""

    web_filter: bool = True
    app_filter: bool = True
    ssl_mode: str = "certificate-inspection"   # or deep-inspection / no-inspection
    blocked_urls: list = field(default_factory=list)     # [] = use utm defaults
    blocked_categories: list = field(default_factory=list)

    # ---- derived ---------------------------------------------------------
    def staff_range(self):
        return compute_range(self.staff_ip, self.staff_clients, self.staff_first)

    def guest_range(self):
        return compute_range(self.guest_ip, self.guest_clients, self.guest_first)

    def filtered_ports(self):
        """Ports that get the web/app filters -- LAN and Staff, never Guest."""
        ports = [self.lan_port]
        if self.configure_staff:
            ports.append(self.staff_port)
        return ports


def compute_range(ip, clients, first_host):
    """Return (start, end) inside the /24 containing `ip`."""
    net = ipaddress.ip_network(f"{ip}/24", strict=False)
    base = str(net.network_address).rsplit(".", 1)[0]
    last = first_host + clients - 1
    return f"{base}.{first_host}", f"{base}.{last}"


def valid_ip(s):
    try:
        ipaddress.IPv4Address(s)
        return True
    except (ipaddress.AddressValueError, ValueError):
        return False


def validate(spec):
    """Return a list of human-readable problems. Empty list means OK."""
    errs = []

    def check_net(label, ip, clients, first, enabled):
        if not enabled:
            return None
        if not valid_ip(ip):
            errs.append(f"{label}: '{ip}' is not a valid IPv4 address.")
            return None
        if not (1 <= clients <= 250):
            errs.append(f"{label}: client count must be between 1 and 250.")
            return None
        if first + clients - 1 > 254:
            errs.append(
                f"{label}: {clients} clients starting at .{first} runs past .254. "
                f"Lower the count or the starting number."
            )
            return None
        return ipaddress.ip_network(f"{ip}/24", strict=False)

    staff_net = check_net("Staff WiFi", spec.staff_ip, spec.staff_clients,
                          spec.staff_first, spec.configure_staff)
    guest_net = check_net("Guest WiFi", spec.guest_ip, spec.guest_clients,
                          spec.guest_first, spec.configure_guest)

    lan_net = None
    if spec.configure_lan:
        if not valid_ip(spec.lan_ip):
            errs.append(f"Office LAN: '{spec.lan_ip}' is not a valid IPv4 address.")
        elif not valid_ip(spec.lan_mask):
            errs.append(f"Office LAN: '{spec.lan_mask}' is not a valid netmask.")
        else:
            try:
                lan_net = ipaddress.ip_network(
                    f"{spec.lan_ip}/{spec.lan_mask}", strict=False)
            except ValueError as e:
                errs.append(f"Office LAN: {e}")
        for label, val in (("DHCP start", spec.lan_start), ("DHCP end", spec.lan_end)):
            if not valid_ip(val):
                errs.append(f"Office LAN {label}: '{val}' is not a valid IPv4 address.")
        if lan_net and valid_ip(spec.lan_start) and valid_ip(spec.lan_end):
            for label, val in (("start", spec.lan_start), ("end", spec.lan_end)):
                if ipaddress.IPv4Address(val) not in lan_net:
                    errs.append(
                        f"Office LAN DHCP {label} {val} is outside the LAN subnet "
                        f"({lan_net}).")

    pairs = [("Staff WiFi", staff_net), ("Guest WiFi", guest_net), ("Office LAN", lan_net)]
    for i, (a_label, a_net) in enumerate(pairs):
        for b_label, b_net in pairs[i + 1:]:
            if a_net and b_net and a_net.overlaps(b_net):
                errs.append(
                    f"{a_label} ({a_net}) overlaps {b_label} ({b_net}). "
                    f"The FortiGate will reject overlapping interface subnets.")

    ports = [(p, l) for p, l, on in (
        (spec.lan_port, "Office LAN", True),
        (spec.staff_port, "Staff WiFi", spec.configure_staff),
        (spec.guest_port, "Guest WiFi", spec.configure_guest)) if on]
    seen = {}
    for port, label in ports:
        if port in seen:
            errs.append(f"{label} and {seen[port]} are both set to port '{port}'.")
        seen[port] = label

    if spec.ssl_mode not in ("deep-inspection", "certificate-inspection", "no-inspection"):
        errs.append(f"Unknown SSL inspection mode '{spec.ssl_mode}'.")
    if spec.web_filter and spec.ssl_mode == "no-inspection":
        errs.append(
            "Web filter is on with SSL inspection set to 'no-inspection'. The "
            "FortiGate cannot see HTTPS hostnames, so Facebook/YouTube blocking "
            "will not work. Use certificate-inspection or deep-inspection.")
    return errs


# =========================================================================
#  Individual provisioning steps
# =========================================================================
def ensure_standalone_port(fg, port, log=_noop):
    """Remove `port` from the built-in hardware switch if it is a member.

    On a factory-default 60F, internal1-internal5 all belong to the `internal`
    hard-switch. A switch member cannot hold its own IP: the FortiGate accepts
    the interface PUT, applies the harmless fields (alias, role) and SILENTLY
    DROPS the `ip`, so the write looks like it succeeded."""
    try:
        switches = fg.results("/api/v2/cmdb/system/virtual-switch")
    except FortiGateError:
        return False                                  # model has no virtual-switch
    for vs in switches:
        members = [p.get("name") for p in vs.get("port", [])]
        if port not in members:
            continue
        remaining = [{"name": m} for m in members if m != port]
        fg.call("PUT", f"/api/v2/cmdb/system/virtual-switch/{vs['name']}",
                {"port": remaining})
        log(f"[ok] {port} removed from '{vs['name']}' hardware switch "
            f"(was: {', '.join(members)})")
        return True
    return False


def set_lan_interface(fg, port, gateway, alias, netmask=NETMASK, log=_noop):
    """Give a port an IP, then READ IT BACK. Raises if it did not stick."""
    ensure_standalone_port(fg, port, log)
    fg.call("PUT", f"/api/v2/cmdb/system/interface/{port}", {
        "ip": f"{gateway} {netmask}",
        "alias": alias,
        "role": "lan",
        "allowaccess": "ping",
        "status": "up",
        "device-identification": "enable",
    })
    got = fg.results(f"/api/v2/cmdb/system/interface/{port}")[0]
    if not (got.get("ip") or "").startswith(gateway + " "):
        raise FortiGateError(
            f"{port} did not take the IP: wanted '{gateway} {netmask}', device "
            f"reports '{got.get('ip')}'. The write was accepted but silently "
            f"ignored -- usually the port is still a switch member, or "
            f"{gateway} overlaps an existing interface subnet.")
    log(f"[ok] interface {port} -> {got.get('ip')} (alias {got.get('alias')})")


def set_dhcp_server(fg, port, gateway, start, end, netmask=NETMASK, log=_noop):
    body = {
        "status": "enable",
        "interface": port,
        "default-gateway": gateway,
        "netmask": netmask,
        "dns-service": DNS_SERVICE,
        "lease-time": LEASE,
        "ip-range": [{"id": 1, "start-ip": start, "end-ip": end}],
    }
    servers = fg.results("/api/v2/cmdb/system.dhcp/server")
    existing = next((s for s in servers if s.get("interface") == port), None)
    if existing:
        fg.call("PUT", f"/api/v2/cmdb/system.dhcp/server/{existing['id']}", body)
        log(f"[ok] DHCP server #{existing['id']} on {port} -> {start}-{end}")
    else:
        r = fg.call("POST", "/api/v2/cmdb/system.dhcp/server", body)
        log(f"[ok] DHCP server #{r.get('mkey')} on {port} -> {start}-{end}")


POLICY_NAMES = {
    "internal": "internal-to-wan1",
    "internal2": "WiFi-LAN-to-wan1",
    "internal3": "Guest-WiFi-to-wan1",
}


def ensure_policies(fg, ports, log=_noop):
    """One NAT policy per inside network, all out via wan1 (one ISP per branch).

    Guest gets its wan1 policy and nothing else -- the FortiGate's default deny
    is what keeps guests off the LAN, Staff WiFi and (later) the HO VPN."""
    existing = fg.results("/api/v2/cmdb/firewall/policy")
    have = set()
    for p in existing:
        si = ",".join(x["name"] for x in p.get("srcintf", []))
        di = ",".join(x["name"] for x in p.get("dstintf", []))
        have.add((si, di))
        if p.get("name"):
            have.add(p["name"])
    for port in ports:
        name = POLICY_NAMES.get(port, f"{port}-to-wan1")
        if name in have or (port, "wan1") in have:
            log(f"[skip] policy {name} ({port}->wan1) already present")
            continue
        fg.call("POST", "/api/v2/cmdb/firewall/policy", {
            "name": name,
            "srcintf": [{"name": port}], "dstintf": [{"name": "wan1"}],
            "srcaddr": [{"name": "all"}], "dstaddr": [{"name": "all"}],
            "action": "accept", "schedule": "always", "service": [{"name": "ALL"}],
            "nat": "enable", "status": "enable", "logtraffic": "all",
        })
        log(f"[ok] policy {name} ({port}->wan1) created")


def set_wan1_pppoe(fg, username="", password="", log=_noop):
    body = {
        "mode": "pppoe",
        "defaultgw": "enable",
        "dns-server-override": "enable",
        "allowaccess": "ping",
        "distance": 5,
        "status": "up",
        "role": "wan",
    }
    if username:
        body["username"] = username
        if password:
            body["password"] = password
    fg.call("PUT", "/api/v2/cmdb/system/interface/wan1", body)
    if username:
        log("[ok] wan1 -> PPPoE with ISP credentials set")
    else:
        log("[ok] wan1 -> PPPoE plug-and-play (ISP credentials entered ON-SITE)")


def set_hostname(fg, hostname, log=_noop):
    fg.call("PUT", "/api/v2/cmdb/system/global", {"hostname": hostname})
    log(f"[ok] hostname -> {hostname}")


def set_lan_management(fg, port, ip, mask, start, end, log=_noop):
    """Point the office LAN at the branch standard. RUN THIS LAST.

    This is the management interface, so changing its IP tears down the very
    session doing the work. Order matters: the DHCP server is updated FIRST,
    while we can still talk to the device. If the interface changed first and
    the DHCP update then failed, the LAN would hand out addresses on the old
    subnet and nobody could reconnect without setting a static IP."""
    servers = fg.results("/api/v2/cmdb/system.dhcp/server")
    existing = next((s for s in servers if s.get("interface") == port), None)
    body = {
        "status": "enable", "interface": port, "default-gateway": ip,
        "netmask": mask, "dns-service": DNS_SERVICE, "lease-time": LEASE,
        "ip-range": [{"id": 1, "start-ip": start, "end-ip": end}],
    }
    if existing:
        fg.call("PUT", f"/api/v2/cmdb/system.dhcp/server/{existing['id']}", body)
        log(f"[ok] LAN DHCP server #{existing['id']} -> {start}-{end}")
    else:
        r = fg.call("POST", "/api/v2/cmdb/system.dhcp/server", body)
        log(f"[ok] LAN DHCP server #{r.get('mkey')} -> {start}-{end}")

    log(f"[..] setting {port} -> {ip} {mask} (this drops the session on purpose)")
    try:
        fg.call("PUT", f"/api/v2/cmdb/system/interface/{port}", {
            "ip": f"{ip} {mask}",
            "allowaccess": "ping https ssh fabric",
            "role": "lan", "status": "up",
        })
        log(f"[ok] {port} -> {ip} {mask}")
    except (FortiGateError, OSError) as e:
        # Expected: the reply cannot come back over a connection whose
        # destination IP just changed underneath it.
        log(f"[ok] {port} -> {ip} {mask} (session dropped as expected: "
            f"{type(e).__name__})")


# =========================================================================
#  Drivers
# =========================================================================
def provision(fg, spec, log=_noop, apply_filters=None):
    """Apply everything except the office LAN (which kills the session).

    `apply_filters` is utm.apply_filters, passed in to avoid a circular import
    when only the network half is wanted."""
    if spec.hostname:
        set_hostname(fg, spec.hostname, log)

    if spec.configure_staff:
        s_start, s_end = spec.staff_range()
        set_lan_interface(fg, spec.staff_port, spec.staff_ip, STAFF_ALIAS, NETMASK, log)
        set_dhcp_server(fg, spec.staff_port, spec.staff_ip, s_start, s_end, NETMASK, log)

    if spec.configure_guest:
        g_start, g_end = spec.guest_range()
        set_lan_interface(fg, spec.guest_port, spec.guest_ip, GUEST_ALIAS, NETMASK, log)
        set_dhcp_server(fg, spec.guest_port, spec.guest_ip, g_start, g_end, NETMASK, log)

    ports = [spec.lan_port]
    if spec.configure_staff:
        ports.append(spec.staff_port)
    if spec.configure_guest:
        ports.append(spec.guest_port)
    ensure_policies(fg, ports, log)

    if spec.wan_pppoe:
        set_wan1_pppoe(fg, spec.pppoe_user, spec.pppoe_pass, log)

    if (spec.web_filter or spec.app_filter) and apply_filters is not None:
        apply_filters(fg, spec, log)


def lan_phase(fg, spec, log=_noop):
    """Phase 1 on a factory device: move the office LAN, then stop."""
    set_lan_management(fg, spec.lan_port, spec.lan_ip, spec.lan_mask,
                       spec.lan_start, spec.lan_end, log)


# =========================================================================
#  Preview (dry run) and verification
# =========================================================================
def preview(fg, spec):
    """Compare the spec against the live device. Returns (changes, unchanged)."""
    changes, same = [], []
    ifaces = {i["name"]: i for i in fg.results("/api/v2/cmdb/system/interface")}
    dhcp = fg.results("/api/v2/cmdb/system.dhcp/server")
    pols = fg.results("/api/v2/cmdb/firewall/policy")

    def cmp(label, actual, wanted):
        (same if actual == wanted else changes).append(
            f"{label}: {actual!r} -> {wanted!r}" if actual != wanted
            else f"{label}: already {wanted!r}")

    if spec.hostname:
        cur = (fg.get("/api/v2/cmdb/system/global").get("results") or {}).get("hostname")
        cmp("hostname", cur, spec.hostname)

    for on, port, ip, alias, clients, first, label in (
        (spec.configure_staff, spec.staff_port, spec.staff_ip, STAFF_ALIAS,
         spec.staff_clients, spec.staff_first, "Staff WiFi"),
        (spec.configure_guest, spec.guest_port, spec.guest_ip, GUEST_ALIAS,
         spec.guest_clients, spec.guest_first, "Guest WiFi"),
    ):
        if not on:
            continue
        cur = ifaces.get(port, {})
        cmp(f"{label} {port} ip", cur.get("ip"), f"{ip} {NETMASK}")
        cmp(f"{label} {port} alias", cur.get("alias"), alias)
        start, end = compute_range(ip, clients, first)
        srv = next((s for s in dhcp if s.get("interface") == port), None)
        rng = (srv.get("ip-range") or [{}])[0] if srv else {}
        cmp(f"{label} DHCP",
            f"{rng.get('start-ip')}-{rng.get('end-ip')}" if srv else None,
            f"{start}-{end}")

    if spec.configure_lan:
        cur = ifaces.get(spec.lan_port, {})
        cmp(f"Office LAN {spec.lan_port} ip", cur.get("ip"),
            f"{spec.lan_ip} {spec.lan_mask}")

    have_pairs = {(",".join(x["name"] for x in p.get("srcintf", [])),
                   ",".join(x["name"] for x in p.get("dstintf", []))) for p in pols}
    for port in ([spec.lan_port]
                 + ([spec.staff_port] if spec.configure_staff else [])
                 + ([spec.guest_port] if spec.configure_guest else [])):
        label = f"policy {port}->wan1"
        if (port, "wan1") in have_pairs:
            same.append(f"{label}: already present")
        else:
            changes.append(f"{label}: will be created")

    if spec.wan_pppoe:
        cmp("wan1 mode", ifaces.get("wan1", {}).get("mode"), "pppoe")

    filtered = spec.filtered_ports()
    for p in pols:
        srcs = [x["name"] for x in p.get("srcintf", [])]
        if "wan1" not in [x["name"] for x in p.get("dstintf", [])]:
            continue
        pid = p.get("policyid")
        want_utm = any(s in filtered for s in srcs) and (spec.web_filter or spec.app_filter)
        label = f"policy #{pid} ({','.join(srcs)}->wan1) inspection"
        cmp(label, p.get("ssl-ssh-profile"),
            spec.ssl_mode if want_utm else p.get("ssl-ssh-profile"))
    return changes, same


def verify(fg, spec, utm_mod=None):
    """Read the device back and check it against the spec.

    Returns a list of (label, passed, detail) tuples."""
    out = []

    def check(label, actual, expected):
        out.append((label, actual == expected,
                    f"{actual!r}" if actual == expected
                    else f"got {actual!r}, want {expected!r}"))

    ifaces = {i["name"]: i for i in fg.results("/api/v2/cmdb/system/interface")}
    dhcp = fg.results("/api/v2/cmdb/system.dhcp/server")
    pols = fg.results("/api/v2/cmdb/firewall/policy")

    if spec.hostname:
        cur = (fg.get("/api/v2/cmdb/system/global").get("results") or {}).get("hostname")
        check("hostname", cur, spec.hostname)

    def pool(intf):
        s = next((x for x in dhcp if x.get("interface") == intf), None)
        if not s:
            return None
        r = (s.get("ip-range") or [{}])[0]
        return f"{r.get('start-ip')}-{r.get('end-ip')} gw={s.get('default-gateway')} status={s.get('status')}"

    if spec.configure_lan:
        check(f"{spec.lan_port} (LAN) ip", ifaces.get(spec.lan_port, {}).get("ip"),
              f"{spec.lan_ip} {spec.lan_mask}")
        check(f"{spec.lan_port} (LAN) DHCP pool", pool(spec.lan_port),
              f"{spec.lan_start}-{spec.lan_end} gw={spec.lan_ip} status=enable")

    for on, port, ip, alias, clients, first, label in (
        (spec.configure_staff, spec.staff_port, spec.staff_ip, STAFF_ALIAS,
         spec.staff_clients, spec.staff_first, "Staff WiFi"),
        (spec.configure_guest, spec.guest_port, spec.guest_ip, GUEST_ALIAS,
         spec.guest_clients, spec.guest_first, "Guest WiFi"),
    ):
        if not on:
            continue
        cur = ifaces.get(port, {})
        check(f"{port} ({label}) ip", cur.get("ip"), f"{ip} {NETMASK}")
        check(f"{port} ({label}) alias", cur.get("alias"), alias)
        check(f"{port} ({label}) role", cur.get("role"), "lan")
        start, end = compute_range(ip, clients, first)
        check(f"{port} ({label}) DHCP pool", pool(port),
              f"{start}-{end} gw={ip} status=enable")

    # ports must be out of the hardware switch
    try:
        members = sorted(p["name"] for v in fg.results("/api/v2/cmdb/system/virtual-switch")
                         for p in v.get("port", []))
        for port in ([spec.staff_port] if spec.configure_staff else []) + \
                    ([spec.guest_port] if spec.configure_guest else []):
            out.append((f"{port} out of hardware switch", port not in members,
                        "standalone" if port not in members else "STILL A SWITCH MEMBER"))
    except FortiGateError:
        pass

    if spec.wan_pppoe:
        w = ifaces.get("wan1", {})
        check("wan1 mode", w.get("mode"), "pppoe")
        check("wan1 default route", w.get("defaultgw"), "enable")
        if not spec.pppoe_user:
            check("wan1 PPPoE user blank (set on-site)", w.get("username", ""), "")

    filtered = spec.filtered_ports()
    for port in ([spec.lan_port]
                 + ([spec.staff_port] if spec.configure_staff else [])
                 + ([spec.guest_port] if spec.configure_guest else [])):
        p = next((x for x in pols
                  if [i["name"] for i in x.get("srcintf", [])] == [port]
                  and [i["name"] for i in x.get("dstintf", [])] == ["wan1"]), None)
        if not p:
            out.append((f"{port}->wan1 policy exists", False, "MISSING"))
            continue
        check(f"{port}->wan1 action/nat", f"{p.get('action')}/{p.get('nat')}",
              "accept/enable")
        want = port in filtered and (spec.web_filter or spec.app_filter)
        check(f"{port}->wan1 UTM", p.get("utm-status"), "enable" if want else "disable")
        check(f"{port}->wan1 SSL inspection", p.get("ssl-ssh-profile"),
              spec.ssl_mode if want else "no-inspection")
        if utm_mod:
            check(f"{port}->wan1 web filter", p.get("webfilter-profile") or "",
                  utm_mod.WEBFILTER_PROFILE if (want and spec.web_filter) else "")
            check(f"{port}->wan1 app control", p.get("application-list") or "",
                  utm_mod.APPLIST_NAME if (want and spec.app_filter) else "")

    # guest isolation: no policy from guest toward any inside network
    if spec.configure_guest:
        leaks = [p.get("policyid") for p in pols
                 if spec.guest_port in [x["name"] for x in p.get("srcintf", [])]
                 and any(d["name"] in (spec.lan_port, spec.staff_port)
                         for d in p.get("dstintf", []))]
        out.append(("Guest isolated (no guest->inside policy)", leaks == [],
                    "isolated" if leaks == [] else f"LEAKING via policies {leaks}"))

    if utm_mod and (spec.web_filter or spec.app_filter):
        out.extend(utm_mod.verify_filters(fg, spec))
    return out
