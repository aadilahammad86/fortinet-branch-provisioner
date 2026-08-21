#!/usr/bin/env python3
"""Build the IPsec tunnel from this branch to head office (command-line front end).

Thin CLI wrapper around fortigate.vpn -- the GUI's "VPN Tunnel" tab applies the
identical objects. This is the API equivalent of the IPsec wizard run as
"Site to Site -> FortiGate -> Dynamic DNS".

WHAT IT CREATES
  * phase1  <branch>toHO   -- remote gateway = head office's DDNS name
  * phase2  one selector per inside network x head office network
  * firewall addresses + groups for both ends
  * two policies, NAT off, in and out
  * a static route to each head office network via the tunnel
  * a BLACKHOLE route behind each one, so head office traffic fails instead of
    leaking out of the branch's internet connection when the tunnel is down

Guest WiFi (internal3) is refused outright -- it must never reach head office.

The branch always dials (auto-negotiate + keepalive), so the tunnel works even
where the ISP gives this site an address nobody outside can reach. Head office
must have its matching end with the same pre-shared key.

    >> The tunnel name becomes an interface name, and those stop at 15
    >> characters -- so --branch-name is capped at 11 and the tunnel is always
    >> <branch>toHO.

Usage:
    python configure-vpn.py --branch-name mafraq --ho homadina.fortidyndns.com \
                            --psk SECRET --remote 10.0.0.0/24 --preview
    python configure-vpn.py --branch-name mafraq --ho homadina.fortidyndns.com \
                            --psk SECRET --remote 10.0.0.0/24 --yes --verify
    python configure-vpn.py --branch-name mafraq --status
    python configure-vpn.py --branch-name mafraq --remove --yes
"""
import sys
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fortigate import FortiGate, FortiGateError, load_env      # noqa: E402
from fortigate import vpn                                      # noqa: E402


def log(msg):
    print(f"  {msg}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--branch-name", required=True,
                    help=f"short name; the tunnel is <name>{vpn.SUFFIX} "
                         f"(max {vpn.MAX_BRANCH} characters)")
    ap.add_argument("--ho", help="head office's internet name")
    ap.add_argument("--psk", help="pre-shared key; must match head office")
    ap.add_argument("--wan", default="wan1", help="port the tunnel goes out of")
    ap.add_argument("--ports", default="internal,internal2",
                    help="inside ports allowed across (never internal3)")
    ap.add_argument("--remote", action="append", default=[],
                    metavar="CIDR", help="a head office network; repeatable")
    ap.add_argument("--ike", default=vpn.DEFAULT_IKE, choices=["1", "2"])
    ap.add_argument("--preview", action="store_true",
                    help="list what would be created; changes nothing")
    ap.add_argument("--status", action="store_true", help="is the tunnel up?")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--remove", action="store_true",
                    help="delete the tunnel and everything built with it")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--host")
    ap.add_argument("--user")
    ap.add_argument("--password")
    args = ap.parse_args()

    cfg = load_env()
    fg = FortiGate(args.host or cfg["FGT_HOST"],
                   args.user or cfg["FGT_USER"],
                   args.password if args.password is not None
                   else cfg["FGT_PASSWORD"])

    spec = vpn.VpnSpec(
        branch_name=args.branch_name, remote_ddns=args.ho or "",
        psk=args.psk or "", wan_port=args.wan,
        inside_ports=[p.strip() for p in args.ports.split(",") if p.strip()],
        remote_subnets=list(args.remote), ike=args.ike)

    if args.status:
        up, detail = vpn.tunnel_status(fg, spec.tunnel)
        print(f"  {spec.tunnel}: {detail}")
        raise SystemExit(0 if up else 1)

    if args.remove:
        if not args.yes and input(
                f"  Remove {spec.tunnel} and everything with it? [y/N]: "
        ).strip().lower() not in ("y", "yes"):
            print("  Aborted.")
            return
        vpn.remove_vpn(fg, args.branch_name, log)
        return

    if args.verify and not (args.preview or args.ho):
        results = vpn.verify(fg, spec)
        passed = sum(1 for _, ok, _ in results if ok)
        for label, ok, detail in results:
            print(f"  [{'PASS' if ok else 'FAIL'}] {label:<34} {detail}")
        print(f"  RESULT: {passed}/{len(results)} checks passed")
        return

    for w in vpn.check_overlap(fg, spec):
        print(f"  [!] {w}")

    if args.preview:
        try:
            for port, sub in vpn.local_selectors(fg, spec.inside_ports):
                print(f"  this branch sends {port:<12} {sub}")
        except FortiGateError as e:
            raise SystemExit(f"[!] {e}")
        errs = vpn.validate(spec)
        if errs:
            print("  Cannot build it yet:")
            for e in errs:
                print(f"    - {e}")
            return
        print("  What --yes would create:")
        for label, verdict in vpn.preview(fg, spec):
            print(f"    {label}  ->  {verdict}")
        return

    errs = vpn.validate(spec)
    if errs:
        print("[!] Please fix:")
        for e in errs:
            print(f"    - {e}")
        raise SystemExit(1)

    print("=" * 66)
    print(f"  Tunnel {spec.tunnel} -> {spec.remote_ddns}")
    print("=" * 66)
    print(f"  Out via        : {spec.wan_port}")
    print(f"  Inside networks: {', '.join(spec.inside_ports)}")
    print(f"  Head office    : {', '.join(spec.remote_subnets)}")
    print(f"  IKE            : v{spec.ike}")
    print("  Guest WiFi is not included and cannot be.")
    print("-" * 66)
    if not args.yes and input("  Proceed? [y/N]: ").strip().lower() not in ("y", "yes"):
        print("  Aborted. Nothing changed.")
        return

    vpn.apply_vpn(fg, spec, log)

    if args.verify:
        print("\n--- verification ---")
        results = vpn.verify(fg, spec)
        passed = sum(1 for _, ok, _ in results if ok)
        for label, ok, detail in results:
            print(f"  [{'PASS' if ok else 'FAIL'}] {label:<34} {detail}")
        print(f"  RESULT: {passed}/{len(results)} checks passed")


if __name__ == "__main__":
    try:
        main()
    except FortiGateError as e:
        raise SystemExit(f"[!] {e}")
