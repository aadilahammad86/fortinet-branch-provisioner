#!/usr/bin/env python3
"""Make WAN interfaces plug-and-play (DHCP mode).

Switches wan1 from PPPoE to DHCP so plugging in any internet feed auto-
configures IP, gateway, DNS and a default route. wan2 is already DHCP.

Reads FGT_HOST / FGT_USER / FGT_PASSWORD from ../.env.
"""
import ssl
import json
import urllib.parse
import urllib.request
import http.cookiejar
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"

# WAN interfaces to set to DHCP plug-and-play. wan1 has lower distance so it
# is preferred as primary when both WANs are live.
WANS = {
    "wan1": {
        "mode": "dhcp",
        "defaultgw": "enable",
        "dns-server-override": "enable",
        "allowaccess": "ping",
        "distance": 5,
        "status": "up",
        "role": "wan",
    },
    # wan2 left as-is (already DHCP); uncomment to normalize/back it up.
    # "wan2": {"mode": "dhcp", "defaultgw": "enable", "dns-server-override": "enable",
    #          "allowaccess": "ping", "distance": 10, "status": "up", "role": "wan"},
}


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

    for name, payload in WANS.items():
        r = call("PUT", f"/api/v2/cmdb/system/interface/{name}", payload)
        print(f"{name}: {r.get('status')} {r.get('http_status')}")

    print("\n--- verification ---")
    for name in WANS:
        d = call("GET", f"/api/v2/cmdb/system/interface/{name}")["results"][0]
        print(f"{name}: mode={d.get('mode')} defaultgw={d.get('defaultgw')} "
              f"dns-override={d.get('dns-server-override')} distance={d.get('distance')} "
              f"allowaccess='{d.get('allowaccess')}' status={d.get('status')}")


if __name__ == "__main__":
    main()
