#!/usr/bin/env python3
"""Pull key info from the FortiGate REST API and write docs/device-info.md.

Reads FGT_HOST / FGT_USER / FGT_PASSWORD from ../.env.
Uses the self-signed cert, so TLS verification is disabled on purpose.
"""
import os
import ssl
import json
import urllib.parse
import urllib.request
import http.cookiejar
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"


def load_env():
    cfg = {"FGT_HOST": "172.21.0.1", "FGT_USER": "admin", "FGT_PASSWORD": ""}
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


def main():
    cfg = load_env()
    host = cfg["FGT_HOST"]
    base = f"https://{host}"

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(context=ctx),
    )

    # Prime session, then log in
    opener.open(base + "/", timeout=20).read()
    body = urllib.parse.urlencode(
        {"username": cfg["FGT_USER"], "secretkey": cfg["FGT_PASSWORD"], "ajax": "1"}
    ).encode()
    resp = opener.open(base + "/logincheck", data=body, timeout=20).read().decode()
    if not resp.lstrip().startswith("1"):
        raise SystemExit(f"Login failed: {resp!r}")

    csrf = ""
    for c in jar:
        if c.name.lower() == "ccsrftoken":
            csrf = c.value.strip('"')

    def api(path):
        req = urllib.request.Request(base + path, headers={"X-CSRFTOKEN": csrf})
        return json.loads(opener.open(req, timeout=20).read().decode())

    status_resp = api("/api/v2/monitor/system/status")
    status = status_resp["results"]
    # serial / version / build / vdom are siblings of "results", not inside it
    for k in ("serial", "version", "build", "vdom"):
        status.setdefault(k, status_resp.get(k))
    usage = api("/api/v2/monitor/system/resource/usage?scope=global")["results"]
    ifaces = api("/api/v2/monitor/system/interface?scope=global")["results"]

    def cur(key):
        try:
            return usage[key][0]["current"]
        except Exception:
            return "?"

    uptime_s = cur("uptime")
    uptime = "n/a" if uptime_s == "?" else f"{uptime_s}s"
    if isinstance(uptime_s, (int, float)):
        d, rem = divmod(int(uptime_s), 86400)
        h, rem = divmod(rem, 3600)
        m, _ = divmod(rem, 60)
        uptime = f"{d}d {h}h {m}m"

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Device Info",
        "",
        f"_Generated {now} by pull-device-info.py_",
        "",
        "## System",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| Hostname | {status.get('hostname')} |",
        f"| Model | {status.get('model_name')} {status.get('model_number')} ({status.get('model')}) |",
        f"| Serial | {status.get('serial')} |",
        f"| Firmware | {status.get('version')} build {status.get('build')} |",
        f"| VDOM | {status.get('vdom')} |",
        f"| Uptime | {uptime} |",
        "",
        "## Current load",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| CPU | {cur('cpu')}% |",
        f"| Memory | {cur('mem')}% |",
        f"| Sessions | {cur('session')} |",
        "",
        "## Interfaces",
        "",
        "| Name | IP | Link | Speed | MAC |",
        "|------|----|------|-------|-----|",
    ]
    items = ifaces.items() if isinstance(ifaces, dict) else [(i.get("name"), i) for i in ifaces]
    for name, v in sorted(items):
        lines.append(
            f"| {v.get('name', name)} | {v.get('ip', '-')} | "
            f"{'up' if v.get('link') else 'down'} | {v.get('speed', '-')} | {v.get('mac', '-')} |"
        )

    out = ROOT / "docs" / "device-info.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {out}")
    print(f"{status.get('hostname')} | {status.get('model')} | {status.get('version')} | up {uptime}")


if __name__ == "__main__":
    main()
