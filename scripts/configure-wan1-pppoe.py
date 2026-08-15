#!/usr/bin/env python3
"""Pre-load PPPoE on wan1 so the line is plug-and-play.

Sets wan1 to PPPoE mode with the ISP username/password from ../.env
(FGT_PPPOE_USER / FGT_PPPOE_PASS). When the WAN cable is connected the
FortiGate dials PPPoE automatically, gets an IP + default route, and the
existing NAT policies give both LANs internet.

If the PPPoE credentials are not set in .env the mode is still applied but
a warning is printed (the line will not authenticate until creds are added).
"""
import ssl
import json
import urllib.parse
import urllib.request
import http.cookiejar
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"


def load_env():
    cfg = {"FGT_HOST": "172.21.0.1", "FGT_USER": "admin", "FGT_PASSWORD": "",
           "FGT_PPPOE_USER": "", "FGT_PPPOE_PASS": ""}
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

    pppoe_user = cfg["FGT_PPPOE_USER"]
    pppoe_pass = cfg["FGT_PPPOE_PASS"]

    payload = {
        "mode": "pppoe",
        "defaultgw": "enable",
        "dns-server-override": "enable",
        "allowaccess": "ping",
        "distance": 5,
        "status": "up",
        "role": "wan",
    }
    if pppoe_user:
        payload["username"] = pppoe_user
    if pppoe_pass:
        payload["password"] = pppoe_pass

    r = call("PUT", "/api/v2/cmdb/system/interface/wan1", payload)
    print(f"wan1 PPPoE: {r.get('status')} {r.get('http_status')}")

    d = call("GET", "/api/v2/cmdb/system/interface/wan1")["results"][0]
    print(f"wan1: mode={d.get('mode')} user={d.get('username') or '(empty)'} "
          f"defaultgw={d.get('defaultgw')} dns-override={d.get('dns-server-override')} "
          f"allowaccess='{d.get('allowaccess')}' status={d.get('status')}")

    if not pppoe_user or not pppoe_pass:
        print("\nWARNING: PPPoE username/password not set in .env "
              "(FGT_PPPOE_USER / FGT_PPPOE_PASS).")
        print("The line will NOT authenticate until they are added. "
              "Add them to .env and re-run this script.")


if __name__ == "__main__":
    main()
