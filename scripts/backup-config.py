#!/usr/bin/env python3
"""Download a full backup of the FortiGate configuration.

Saves the complete running config to ../configs/<serial>-<timestamp>.conf.
This file contains secrets (password hashes, keys) so configs/ is git-ignored.
Use it as a restore point before a factory reset or any big change.

Reads FGT_HOST / FGT_USER / FGT_PASSWORD from ../.env.
"""
import ssl
import json
import urllib.parse
import urllib.request
import http.cookiejar
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
OUTDIR = ROOT / "configs"


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
        raise SystemExit(f"[!] Login failed: {resp!r}")
    csrf = ""
    for c in jar:
        if c.name.lower().startswith("ccsrftoken"):
            csrf = c.value.strip('"')

    def get(path, raw=False):
        req = urllib.request.Request(base + path)
        req.add_header("X-CSRFTOKEN", csrf)
        req.add_header("Referer", base + "/")
        data = opener.open(req, timeout=30).read()
        return data if raw else json.loads(data.decode())

    # identify the device for a friendly filename
    st = get("/api/v2/monitor/system/status")
    serial = st.get("serial", "FGT")
    hostname = st.get("results", {}).get("hostname", "fortigate")
    fw = st.get("version", "")

    # download the full config (raw text, not JSON)
    config = get("/api/v2/monitor/system/config/backup?scope=global", raw=True)

    OUTDIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = OUTDIR / f"{serial}-{stamp}.conf"
    out.write_bytes(config)

    lines = config.decode(errors="replace").count("\n")
    print(f"[ok] Backup saved: {out}")
    print(f"     device={hostname} serial={serial} firmware={fw}")
    print(f"     size={len(config):,} bytes, {lines:,} lines")
    # sanity check: a real FortiGate config starts with a config-version header
    head = config.decode(errors="replace").splitlines()[:1]
    print(f"     first line: {head[0] if head else '(empty)'}")


if __name__ == "__main__":
    main()
