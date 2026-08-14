#!/usr/bin/env python3
"""Configure the WiFi LAN on the FortiGate.

Retargets interface `internal2` to 192.168.1.1/24 and updates the DHCP
server bound to it to hand out 192.168.1.2 - 192.168.1.26 (25 clients).

Reads FGT_HOST / FGT_USER / FGT_PASSWORD from ../.env.
Idempotent: safe to run repeatedly (PUT overwrites to the target state).
"""
import ssl
import json
import urllib.parse
import urllib.request
import http.cookiejar
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"

# ---- desired state -------------------------------------------------------
IFACE = "internal2"
IFACE_BODY = {
    "ip": "192.168.1.1 255.255.255.0",
    "alias": "WiFi-LAN",
    "role": "lan",
    "allowaccess": "ping",
    "status": "up",
    "device-identification": "enable",
}
DHCP_ID = 3  # existing server bound to internal2
DHCP_BODY = {
    "status": "enable",
    "interface": IFACE,
    "default-gateway": "192.168.1.1",
    "netmask": "255.255.255.0",
    "dns-service": "default",
    "lease-time": 604800,
    "ip-range": [{"id": 1, "start-ip": "192.168.1.2", "end-ip": "192.168.1.26"}],
}
# -------------------------------------------------------------------------


def load_env():
    cfg = {"FGT_HOST": "172.21.0.1", "FGT_USER": "admin", "FGT_PASSWORD": ""}
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


def main():
    cfg = load_env()
    base = f"https://{cfg['FGT_HOST']}"

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(context=ctx),
    )

    opener.open(base + "/", timeout=20).read()
    body = urllib.parse.urlencode(
        {"username": cfg["FGT_USER"], "secretkey": cfg["FGT_PASSWORD"], "ajax": "1"}
    ).encode()
    resp = opener.open(base + "/logincheck", data=body, timeout=20).read().decode()
    if not resp.lstrip().startswith("1"):
        raise SystemExit(f"Login failed: {resp!r}")
    # There can be several ccsrftoken cookies (pre- and post-login); the
    # last one set is the valid session token.
    # Cookie is named like `ccsrftoken_443_<hex>`, not plain `ccsrftoken`.
    csrf = ""
    for c in jar:
        if c.name.lower().startswith("ccsrftoken"):
            csrf = c.value.strip('"')

    def call(method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(base + path, data=data, method=method)
        req.add_header("X-CSRFTOKEN", csrf)
        req.add_header("Referer", base + "/")
        if data:
            req.add_header("Content-Type", "application/json")
        return json.loads(opener.open(req, timeout=20).read().decode())

    print(f"Updating interface {IFACE} -> {IFACE_BODY['ip']} ...")
    r1 = call("PUT", f"/api/v2/cmdb/system/interface/{IFACE}", IFACE_BODY)
    print("  interface:", r1.get("status"), r1.get("http_status"))

    print(f"Updating DHCP server {DHCP_ID} -> "
          f"{DHCP_BODY['ip-range'][0]['start-ip']}-{DHCP_BODY['ip-range'][0]['end-ip']} ...")
    r2 = call("PUT", f"/api/v2/cmdb/system.dhcp/server/{DHCP_ID}", DHCP_BODY)
    print("  dhcp:", r2.get("status"), r2.get("http_status"))

    # verify
    iface = call("GET", f"/api/v2/cmdb/system/interface/{IFACE}")["results"][0]
    dhcp = call("GET", f"/api/v2/cmdb/system.dhcp/server/{DHCP_ID}")["results"][0]
    print("\n--- verification ---")
    print(f"{IFACE}: ip={iface.get('ip')} alias={iface.get('alias')} "
          f"role={iface.get('role')} allowaccess='{iface.get('allowaccess')}' "
          f"status={iface.get('status')}")
    rng = dhcp.get("ip-range", [{}])[0]
    print(f"dhcp#{DHCP_ID}: status={dhcp.get('status')} gw={dhcp.get('default-gateway')} "
          f"mask={dhcp.get('netmask')} range={rng.get('start-ip')}-{rng.get('end-ip')}")


if __name__ == "__main__":
    main()
