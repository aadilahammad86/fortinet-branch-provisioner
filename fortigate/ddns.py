"""FortiGuard Dynamic DNS -- give this branch a name the internet can find.

Both ends of the head-office VPN sit on dynamic ISP addresses, so neither can
be reached by a fixed IP. Each side registers a name with FortiGuard and looks
the other one up. HO is already homadina.fortidyndns.com; this module is the
branch half.

    config system ddns
        edit 1
            set ddns-server FortiGuardDDNS
            set ddns-domain "alain.fortidyndns.com"
            set monitor-interface "wan1"
        next
    end

Two things worth knowing before using it:

  * Registration happens between the FortiGate and FortiGuard, and the REST
    API exposes no status for it. A successful write proves nothing. The only
    honest check is to resolve the name independently and compare the answer
    with the WAN address the device reports -- which is what verify() does.
  * FortiGuard names are globally unique and only Fortinet Support can release
    one that is taken, so check before claiming.
"""
import ipaddress
import re
import socket

from .client import FortiGateError

# What FortiGuard offers. The first is the branch standard: it matches the
# suffix head office already uses, which keeps the estate readable.
SUFFIXES = ["fortidyndns.com", "fortiddns.com", "float-zone.com"]
DEFAULT_SUFFIX = SUFFIXES[0]
SERVER = "FortiGuardDDNS"

# 24 is a working limit, not a protocol one (a DNS label allows 63). Long
# enough for a real site name, short enough to type at a branch.
MAX_NAME = 24
NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,%d}[a-z0-9])?$" % (MAX_NAME - 2))


def _noop(_msg):
    pass


def full_name(name, suffix=DEFAULT_SUFFIX):
    return f"{(name or '').strip().lower()}.{suffix}"


def validate_name(name):
    """Return a human-readable problem with `name`, or None if it is fine."""
    name = (name or "").strip()
    if not name:
        return "Enter a name for this branch."
    if len(name) > MAX_NAME:
        return (f"'{name}' is {len(name)} characters; the limit is {MAX_NAME}.")
    if name != name.lower():
        return "Use lower case only -- this becomes an internet address."
    if not NAME_RE.match(name):
        return ("Use letters, digits and hyphens only, and do not start or "
                "end with a hyphen.")
    return None


# =========================================================================
#  Reading what is there
# =========================================================================
def read_ddns(fg):
    """Every DDNS entry on the device: [{ddnsid, domain, server, ports, ...}]."""
    out = []
    for d in fg.results("/api/v2/cmdb/system/ddns"):
        out.append({
            "ddnsid": d.get("ddnsid"),
            "domain": d.get("ddns-domain") or "",
            "server": d.get("ddns-server") or "",
            "ports": [m.get("interface-name")
                      for m in d.get("monitor-interface") or []],
            "public_ip": (d.get("use-public-ip") or "disable") == "enable",
        })
    return out


def find_entry(fg, port=None, domain=None):
    """The existing entry for this interface (or exact domain), if any.

    Matching on the interface is what makes apply() idempotent: a branch has
    one WAN, so re-running with a different name renames that entry instead of
    piling up a second one that fights the first.
    """
    entries = read_ddns(fg)
    if domain:
        for e in entries:
            if e["domain"].lower() == domain.lower():
                return e
    if port:
        for e in entries:
            if port in e["ports"]:
                return e
    return None


def wan_address(fg, port="wan1"):
    """The address the WAN interface currently holds, or None.

    Read from the monitor API, not cmdb: on a PPPoE WAN the cmdb `ip` stays
    0.0.0.0 and the real address only ever appears here.
    """
    for path in (f"/api/v2/monitor/system/interface?interface_name={port}",
                 "/api/v2/monitor/system/interface"):
        try:
            res = fg.get(path).get("results")
        except FortiGateError:
            continue
        rows = []
        if isinstance(res, dict):
            rows = [v for k, v in res.items()
                    if isinstance(v, dict) and (k == port or v.get("name") == port)]
        elif isinstance(res, list):
            rows = [v for v in res if isinstance(v, dict) and v.get("name") == port]
        for row in rows:
            ip = (row.get("ip") or "").split()[0] if row.get("ip") else ""
            if ip and ip != "0.0.0.0":
                return ip
    return None


def resolve(hostname):
    """Look the name up from THIS machine. None if it does not resolve.

    Deliberately independent of the FortiGate: it answers the question the
    branch actually cares about, which is whether the rest of the world can
    find this site.
    """
    try:
        return socket.gethostbyname(hostname)
    except (socket.gaierror, UnicodeError, OSError):
        return None


def name_is_free(hostname):
    """(free, detail) -- a name that already resolves is almost certainly taken."""
    ip = resolve(hostname)
    if ip is None:
        return True, f"{hostname} does not resolve -- looks free."
    return False, (f"{hostname} already resolves to {ip}. FortiGuard names are "
                   f"globally unique, and only Fortinet Support can release "
                   f"one. Pick another name unless that address is this unit.")


# =========================================================================
#  Writing
# =========================================================================
def apply_ddns(fg, name, suffix=DEFAULT_SUFFIX, port="wan1", public_ip=False,
               log=_noop):
    """Register <name>.<suffix> against `port`. Reads back; raises if it did
    not stick."""
    err = validate_name(name)
    if err:
        raise FortiGateError(err)
    if suffix not in SUFFIXES:
        raise FortiGateError(f"'{suffix}' is not a FortiGuard domain. "
                             f"Use one of: {', '.join(SUFFIXES)}.")
    domain = full_name(name, suffix)

    existing = find_entry(fg, port=port, domain=domain)
    body = {
        "ddns-server": SERVER,
        "ddns-domain": domain,
        "monitor-interface": [{"interface-name": port}],
        "use-public-ip": "enable" if public_ip else "disable",
    }
    if existing:
        ddnsid = existing["ddnsid"]
        fg.call("PUT", f"/api/v2/cmdb/system/ddns/{ddnsid}", body)
        state = "updated"
    else:
        used = [e["ddnsid"] for e in read_ddns(fg) if e["ddnsid"]]
        ddnsid = (max(used) + 1) if used else 1
        body["ddnsid"] = ddnsid
        fg.call("POST", "/api/v2/cmdb/system/ddns", body)
        state = "created"

    got = find_entry(fg, domain=domain)
    if not got:
        raise FortiGateError(
            f"The write was accepted but the device does not report "
            f"{domain}. Check that '{port}' exists and is a WAN interface.")
    if port not in got["ports"]:
        raise FortiGateError(
            f"{domain} was saved but is watching {got['ports'] or 'no interface'} "
            f"instead of {port}.")
    log(f"[ok] DDNS entry #{got['ddnsid']} {state}: {domain} on {port}"
        + ("  (registering the public address)" if public_ip else ""))
    return got


def clear_ddns(fg, ddnsid, log=_noop):
    fg.call("DELETE", f"/api/v2/cmdb/system/ddns/{ddnsid}")
    log(f"[ok] DDNS entry #{ddnsid} removed")


# =========================================================================
#  Verification
# =========================================================================
def verify(fg, name, suffix=DEFAULT_SUFFIX, port="wan1"):
    """[(label, passed, detail)] -- config first, then does it actually work."""
    domain = full_name(name, suffix)
    out = []

    entry = find_entry(fg, domain=domain)
    out.append((f"DDNS entry for {domain}", bool(entry),
                f"#{entry['ddnsid']}" if entry else "MISSING"))
    if not entry:
        return out

    out.append(("uses FortiGuard DDNS", entry["server"] == SERVER,
                entry["server"] or "(none)"))
    out.append((f"watching {port}", port in entry["ports"],
                ", ".join(entry["ports"]) or "(none)"))

    wan = wan_address(fg, port)
    out.append((f"{port} has an address", bool(wan), wan or "no address yet -- "
                "the ISP line is not up, so nothing can be registered"))

    ip = resolve(domain)
    out.append((f"{domain} resolves", bool(ip),
                ip or "does not resolve yet -- registration can take a few "
                      "minutes, and needs the unit registered with FortiCare"))

    if ip and wan:
        match = ip == wan
        out.append(("resolves to this firewall", match,
                    f"{ip}" if match else
                    f"resolves to {ip} but {port} holds {wan}. If the ISP put "
                    f"this unit behind their own router, tick 'behind an ISP "
                    f"router'; if the address just changed, wait for the TTL."))
    return out


def is_private(ip):
    """True for CGNAT / RFC1918 -- head office cannot dial in to such a WAN."""
    try:
        a = ipaddress.ip_address(ip)
    except (ValueError, TypeError):
        return False
    return a.is_private or a in ipaddress.ip_network("100.64.0.0/10")
