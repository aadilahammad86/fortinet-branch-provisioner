#!/usr/bin/env python3
r"""Provision a FortiGate for a new branch (WiFi LAN + DHCP + internet + WAN).

Run this after connecting a freshly-staged FortiGate. It asks for the only
things that change per branch -- the WiFi LAN gateway IP and how many DHCP
clients -- and applies the whole standard branch template:

  * internal2  -> WiFi LAN gateway IP, /24, alias WiFi-LAN, role lan, ping
  * DHCP server on internal2 -> pool sized to the client count
  * NAT policies -> WiFi + existing LAN out via wan1 / wan2
  * wan1 -> PPPoE plug-and-play (ISP username/password entered ON-SITE)

Connection settings (host / admin user / admin password) come from ../.env,
or can be overridden with --host / --user / --password.

USAGE
  Interactive (operator answers prompts):
      python provision-branch.py

  Non-interactive (automation):
      python provision-branch.py --wifi-ip 192.168.2.1 --clients 25 --yes

  Options:
      --wifi-ip   IP   WiFi LAN gateway (internal2), e.g. 192.168.2.1
      --clients   N    number of DHCP clients            (default 25)
      --start     N    first DHCP host octet             (default 2)
      --hostname  STR  set device hostname (optional, good per-branch)
      --port      STR  physical port for the WiFi LAN    (default internal2)
      --host / --user / --password   connection overrides
      --yes            skip the confirmation prompt
"""
import ssl
import sys
import json
import argparse
import ipaddress
import urllib.parse
import urllib.request
import http.cookiejar
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"

# ---- things that are the SAME on every branch ---------------------------
NETMASK = "255.255.255.0"          # /24
LEASE = 604800                     # 7 days
IFACE_ALIAS = "WiFi-LAN"
DNS_SERVICE = "default"            # DHCP clients use the FortiGate for DNS
# -------------------------------------------------------------------------


# =========================================================================
#  Tiny FortiGate REST client
# =========================================================================
class FortiGate:
    def __init__(self, host, user, password):
        self.base = f"https://{host}"
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=ctx),
        )
        self.csrf = ""
        self._login(user, password)

    def _login(self, user, password):
        self.opener.open(self.base + "/", timeout=20).read()
        body = urllib.parse.urlencode(
            {"username": user, "secretkey": password, "ajax": "1"}
        ).encode()
        resp = self.opener.open(self.base + "/logincheck", data=body, timeout=20).read().decode()
        if not resp.lstrip().startswith("1"):
            raise SystemExit(f"[!] Login failed for {self.base} -- check .env creds.\n    {resp!r}")
        for c in self.jar:                        # cookie is ccsrftoken_<port>_<hex>
            if c.name.lower().startswith("ccsrftoken"):
                self.csrf = c.value.strip('"')

    def call(self, method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("X-CSRFTOKEN", self.csrf)
        req.add_header("Referer", self.base + "/")
        if data:
            req.add_header("Content-Type", "application/json")
        return json.loads(self.opener.open(req, timeout=20).read().decode())


# =========================================================================
#  Helpers
# =========================================================================
def load_env():
    cfg = {"FGT_HOST": "172.21.0.1", "FGT_USER": "admin", "FGT_PASSWORD": ""}
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


def ask(prompt, default=None):
    suffix = f" [{default}]" if default is not None else ""
    val = input(f"  {prompt}{suffix}: ").strip()
    return val or (str(default) if default is not None else "")


def valid_ip(s):
    try:
        ipaddress.IPv4Address(s)
        return True
    except ValueError:
        return False


def compute_range(wifi_ip, clients, start_host):
    """Return (gateway, dhcp_start, dhcp_end) inside the /24 of wifi_ip."""
    net = ipaddress.ip_network(f"{wifi_ip}/24", strict=False)
    base = str(net.network_address).rsplit(".", 1)[0]      # e.g. 192.168.2
    end_host = start_host + clients - 1
    if end_host > 254:
        raise SystemExit(f"[!] {clients} clients from .{start_host} exceeds .254. "
                         f"Lower the client count or the start octet.")
    return wifi_ip, f"{base}.{start_host}", f"{base}.{end_host}"


# =========================================================================
#  Provisioning steps
# =========================================================================
def set_wifi_interface(fg, port, gateway):
    body = {
        "ip": f"{gateway} {NETMASK}",
        "alias": IFACE_ALIAS,
        "role": "lan",
        "allowaccess": "ping",
        "status": "up",
        "device-identification": "enable",
    }
    fg.call("PUT", f"/api/v2/cmdb/system/interface/{port}", body)
    print(f"  [ok] interface {port} -> {gateway} {NETMASK}")


def set_dhcp_server(fg, port, gateway, start, end):
    body = {
        "status": "enable",
        "interface": port,
        "default-gateway": gateway,
        "netmask": NETMASK,
        "dns-service": DNS_SERVICE,
        "lease-time": LEASE,
        "ip-range": [{"id": 1, "start-ip": start, "end-ip": end}],
    }
    servers = fg.call("GET", "/api/v2/cmdb/system.dhcp/server")["results"]
    existing = next((s for s in servers if s.get("interface") == port), None)
    if existing:
        fg.call("PUT", f"/api/v2/cmdb/system.dhcp/server/{existing['id']}", body)
        print(f"  [ok] DHCP server #{existing['id']} updated -> {start}-{end}")
    else:
        r = fg.call("POST", "/api/v2/cmdb/system.dhcp/server", body)
        print(f"  [ok] DHCP server #{r.get('mkey')} created -> {start}-{end}")


def ensure_policies(fg, port):
    wanted = [
        ("WiFi-LAN-to-wan1", port, "wan1"),
        ("WiFi-LAN-to-wan2", port, "wan2"),
        ("internal-to-wan2", "internal", "wan2"),
    ]
    existing = fg.call("GET", "/api/v2/cmdb/firewall/policy")["results"]
    have = set()
    for p in existing:
        si = ",".join(x["name"] for x in p.get("srcintf", []))
        di = ",".join(x["name"] for x in p.get("dstintf", []))
        have.add((si, di))
        if p.get("name"):
            have.add(p["name"])
    for name, src, dst in wanted:
        if name in have or (src, dst) in have:
            print(f"  [skip] policy {name} ({src}->{dst}) already present")
            continue
        fg.call("POST", "/api/v2/cmdb/firewall/policy", {
            "name": name,
            "srcintf": [{"name": src}], "dstintf": [{"name": dst}],
            "srcaddr": [{"name": "all"}], "dstaddr": [{"name": "all"}],
            "action": "accept", "schedule": "always",
            "service": [{"name": "ALL"}],
            "nat": "enable", "status": "enable", "logtraffic": "all",
        })
        print(f"  [ok] policy {name} ({src}->{dst}) created")


def set_wan1_pppoe(fg):
    fg.call("PUT", "/api/v2/cmdb/system/interface/wan1", {
        "mode": "pppoe",
        "defaultgw": "enable",
        "dns-server-override": "enable",
        "allowaccess": "ping",
        "distance": 5,
        "status": "up",
        "role": "wan",
    })
    print("  [ok] wan1 -> PPPoE plug-and-play (ISP username/password: enter ON-SITE)")


def set_hostname(fg, hostname):
    fg.call("PUT", "/api/v2/cmdb/system/global", {"hostname": hostname})
    print(f"  [ok] hostname -> {hostname}")


# =========================================================================
#  Main
# =========================================================================
def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--wifi-ip")
    ap.add_argument("--clients", type=int)
    ap.add_argument("--start", type=int, default=2)
    ap.add_argument("--hostname")
    ap.add_argument("--port", default="internal2")
    ap.add_argument("--host")
    ap.add_argument("--user")
    ap.add_argument("--password")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    cfg = load_env()
    host = args.host or cfg["FGT_HOST"]
    user = args.user or cfg["FGT_USER"]
    password = args.password or cfg["FGT_PASSWORD"]

    print("=" * 62)
    print("  FortiGate branch provisioner")
    print("=" * 62)
    print(f"  Target device : {host}  (admin: {user})")
    print(f"  WiFi LAN port : {args.port}")
    print("-" * 62)

    # --- gather the per-branch values -----------------------------------
    wifi_ip = args.wifi_ip
    if not wifi_ip:
        wifi_ip = ask("WiFi LAN gateway IP (e.g. 192.168.2.1)")
    if not valid_ip(wifi_ip):
        raise SystemExit(f"[!] '{wifi_ip}' is not a valid IPv4 address.")

    clients = args.clients
    if clients is None:
        clients = int(ask("Number of DHCP clients", 25) or 25)

    hostname = args.hostname
    if hostname is None and not args.yes:
        hostname = ask("Device hostname (optional, Enter to skip)") or None

    gateway, dhcp_start, dhcp_end = compute_range(wifi_ip, clients, args.start)

    # --- confirm ---------------------------------------------------------
    print("-" * 62)
    print("  About to apply:")
    print(f"    {args.port} IP / gateway : {gateway} {NETMASK}")
    print(f"    DHCP pool ({clients})     : {dhcp_start} - {dhcp_end}")
    print(f"    NAT policies            : WiFi->wan1, WiFi->wan2, internal->wan2")
    print(f"    wan1                    : PPPoE (plug-and-play, no creds)")
    if hostname:
        print(f"    hostname                : {hostname}")
    print("-" * 62)
    if not args.yes:
        if input("  Proceed? [y/N]: ").strip().lower() not in ("y", "yes"):
            print("  Aborted. Nothing changed.")
            return

    # --- apply -----------------------------------------------------------
    print("\n  Connecting...")
    fg = FortiGate(host, user, password)
    print("  Applying configuration:")
    if hostname:
        set_hostname(fg, hostname)
    set_wifi_interface(fg, args.port, gateway)
    set_dhcp_server(fg, args.port, gateway, dhcp_start, dhcp_end)
    ensure_policies(fg, args.port)
    set_wan1_pppoe(fg)

    # --- summary ---------------------------------------------------------
    print("\n" + "=" * 62)
    print("  DONE. Branch base config applied.")
    print("=" * 62)
    print("  ON-SITE steps remaining:")
    print("   1. Enter the ISP PPPoE username/password on wan1")
    print("      (GUI: Network > Interfaces > wan1), then plug in the WAN cable.")
    print("   2. Plug the WiFi access point into port", args.port)
    print(f"      Clients get {dhcp_start}-{dhcp_end}, gateway {gateway}.")
    print("\n  NOTE: this assumes", args.port, "is a standalone port. On a")
    print("  factory-default 60F it may be inside the 'internal' hardware")
    print("  switch -- remove it from the switch first if the interface")
    print("  update fails.")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        raise SystemExit(f"[!] API error {e.code}: {e.read().decode()[:300]}")
