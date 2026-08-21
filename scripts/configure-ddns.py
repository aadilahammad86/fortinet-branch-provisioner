#!/usr/bin/env python3
"""Register this branch's FortiGuard Dynamic DNS name (command-line front end).

Thin CLI wrapper around fortigate.ddns -- the GUI's "Dynamic DNS" tab applies
the identical entry.

Both ends of the head-office VPN sit on dynamic ISP addresses, so each side
registers a name and looks the other one up. Head office is already
homadina.fortidyndns.com; this gives the branch its own.

    >> RUN THIS ON SITE, after the ISP line is up. The name is registered with
    >> FortiGuard over the internet -- it cannot be done during staging, and a
    >> unit that has never been online reports every entitlement as pending.

Names are GLOBALLY UNIQUE and only Fortinet Support can release one that is
taken, so --check looks it up before you claim it.

Usage:
    python configure-ddns.py --show
    python configure-ddns.py --name mafraq --check
    python configure-ddns.py --name mafraq [--suffix fortidyndns.com]
                             [--port wan1] [--public-ip] [--verify]
"""
import sys
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fortigate import FortiGate, FortiGateError, load_env      # noqa: E402
from fortigate import ddns                                     # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", help="the branch's short name, e.g. mafraq")
    ap.add_argument("--suffix", default=ddns.DEFAULT_SUFFIX,
                    choices=ddns.SUFFIXES)
    ap.add_argument("--port", default="wan1", help="the port the ISP line is on")
    ap.add_argument("--public-ip", action="store_true",
                    help="the WAN holds a private address behind an ISP router; "
                         "register the public address instead")
    ap.add_argument("--check", action="store_true",
                    help="look the name up and stop -- does not write anything")
    ap.add_argument("--show", action="store_true",
                    help="list what is registered on the device now")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--host")
    ap.add_argument("--user")
    ap.add_argument("--password")
    args = ap.parse_args()

    if args.check:
        if not args.name:
            raise SystemExit("[!] --check needs --name.")
        host = ddns.full_name(args.name, args.suffix)
        free, detail = ddns.name_is_free(host)
        print(("  [ok] " if free else "  [!] ") + detail)
        return

    cfg = load_env()
    fg = FortiGate(args.host or cfg["FGT_HOST"],
                   args.user or cfg["FGT_USER"],
                   args.password if args.password is not None
                   else cfg["FGT_PASSWORD"])

    if args.show:
        entries = ddns.read_ddns(fg)
        if not entries:
            print("  No dynamic DNS registered on this firewall.")
            return
        for e in entries:
            ip = ddns.resolve(e["domain"])
            print(f"  #{e['ddnsid']}  {e['domain']:<34} {e['server']:<16} "
                  f"{', '.join(e['ports']) or '(no port)':<10} "
                  f"resolves to {ip or 'nothing yet'}")
        return

    if not args.name:
        raise SystemExit("[!] --name is required (or use --show).")

    err = ddns.validate_name(args.name)
    if err:
        raise SystemExit(f"[!] {err}")

    domain = ddns.full_name(args.name, args.suffix)
    print(f"Registering {domain} on {args.port}:")
    ddns.apply_ddns(fg, args.name, args.suffix, args.port, args.public_ip,
                    lambda m: print(f"  {m}"))
    print("\n  Registration happens between the firewall and FortiGuard and can")
    print("  take a few minutes. Re-run with --verify to confirm.")

    if args.verify:
        print("\n--- verification ---")
        results = ddns.verify(fg, args.name, args.suffix, args.port)
        passed = sum(1 for _, ok, _ in results if ok)
        for label, ok, detail in results:
            print(f"  [{'PASS' if ok else 'FAIL'}] {label:<34} {detail}")
        print(f"  RESULT: {passed}/{len(results)} checks passed")


if __name__ == "__main__":
    try:
        main()
    except FortiGateError as e:
        raise SystemExit(f"[!] {e}")
