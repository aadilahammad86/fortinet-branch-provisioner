#!/usr/bin/env python3
"""Ensure LAN interfaces have internet (NAT) firewall policies to the WANs.

Creates, if missing (matched by name), accept+NAT policies so that both the
new WiFi LAN (internal2) and the existing LAN (internal) can reach the
internet via either wan1 or wan2. Idempotent: existing policies are skipped.

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

# Policies we want to exist: (name, srcintf, dstintf)
WANTED = [
    ("WiFi-LAN-to-wan1", "internal2", "wan1"),
    ("WiFi-LAN-to-wan2", "internal2", "wan2"),
    ("internal-to-wan2", "internal", "wan2"),
]


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

    existing = call("GET", "/api/v2/cmdb/firewall/policy")["results"]
    have = set()
    for p in existing:
        si = ",".join(x["name"] for x in p.get("srcintf", []))
        di = ",".join(x["name"] for x in p.get("dstintf", []))
        have.add((si, di))
        if p.get("name"):
            have.add(p["name"])

    for name, src, dst in WANTED:
        if name in have or (src, dst) in have:
            print(f"skip  {name} ({src} -> {dst}) already present")
            continue
        payload = {
            "name": name,
            "srcintf": [{"name": src}],
            "dstintf": [{"name": dst}],
            "srcaddr": [{"name": "all"}],
            "dstaddr": [{"name": "all"}],
            "action": "accept",
            "schedule": "always",
            "service": [{"name": "ALL"}],
            "nat": "enable",
            "status": "enable",
            "logtraffic": "all",
        }
        r = call("POST", "/api/v2/cmdb/firewall/policy", payload)
        print(f"create {name} ({src} -> {dst}): {r.get('status')} id={r.get('mkey')}")

    print("\n--- current policies ---")
    for p in call("GET", "/api/v2/cmdb/firewall/policy")["results"]:
        si = ",".join(x["name"] for x in p.get("srcintf", []))
        di = ",".join(x["name"] for x in p.get("dstintf", []))
        print(f"  #{p.get('policyid')} {p.get('name') or '(unnamed)'}: "
              f"{si} -> {di} act={p.get('action')} nat={p.get('nat')} status={p.get('status')}")


if __name__ == "__main__":
    main()
