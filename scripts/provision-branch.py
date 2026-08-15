#!/usr/bin/env python3
r"""Provision a FortiGate for a new branch (command-line front end).

This is the CLI face of the same engine the GUI uses -- see ../fortigate/.
Both apply byte-identical configuration; fix a bug in the package and both
front ends get it.

WHAT IT APPLIES
  * Staff WiFi port  -> gateway IP, /24, alias WiFi-LAN,  role lan, ping
  * Guest WiFi port  -> gateway IP, /24, alias Guest-WiFi, role lan, ping
  * DHCP server on each -> pool sized to that network's client count
  * NAT policies -> all inside networks out via wan1 (one ISP per site)
  * wan1 -> PPPoE (ISP credentials from .env, or entered ON-SITE)
  * Web filter (blocked sites) + application control on LAN and Staff WiFi only
  * Optionally the office LAN address (--lan-only; drops your session)

Guest WiFi is internet-only: it gets a wan1 policy and nothing else, so the
FortiGate's default deny keeps it off the LAN, Staff WiFi and the HO VPN.

TWO-PHASE ON A FACTORY DEVICE
  A factory 60F has its LAN on 192.168.1.99/24, which collides with a Staff
  WiFi on 192.168.1.x. The LAN must move first, and moving it kills the
  session doing the work:

      python provision-branch.py --lan-only --yes
      ipconfig /release & ipconfig /renew        (reconnect at the new IP)
      python provision-branch.py --skip-lan --wifi-ip ... --guest-ip ... --yes

  Phase 2 refuses to run if the WiFi subnets still overlap the LAN, and tells
  you to do phase 1 first.

USAGE
  Interactive:      python provision-branch.py
  Non-interactive:  python provision-branch.py --skip-lan --yes \
                        --wifi-ip 192.168.1.1 --clients 25 \
                        --guest-ip 192.168.2.1 --guest-clients 25 \
                        --hostname FGT-BranchB

  Options:
      --wifi-ip / --clients / --port          Staff WiFi
      --guest-ip / --guest-clients / --guest-port   Guest WiFi ('-' skips guest)
      --start N            first DHCP host number    (default 2)
      --hostname STR       device name
      --lan-ip / --lan-mask / --lan-start / --lan-end / --lan-port
      --lan-only           PHASE 1: move the office LAN, then stop
      --skip-lan           leave the office LAN alone
      --no-filters         skip web filter + application control
      --ssl MODE           certificate-inspection | deep-inspection | no-inspection
      --verify             read the config back and check it after applying
      --host / --user / --password    connection overrides (else ../.env)
      --yes                skip the confirmation prompt

ON-SITE, in addition to the ISP credentials: with --ssl deep-inspection the
FortiGate CA certificate must be installed on every client device, or browsers
block every secure site.
"""
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fortigate import FortiGate, FortiGateError, load_env      # noqa: E402
from fortigate import branch, utm                              # noqa: E402


def log(msg):
    print(f"  {msg}")


def build_spec(args):
    guest_on = args.guest_ip not in (None, "", "-")
    return branch.BranchSpec(
        hostname=args.hostname or "",
        lan_port=args.lan_port, lan_ip=args.lan_ip, lan_mask=args.lan_mask,
        lan_start=args.lan_start, lan_end=args.lan_end,
        configure_lan=not args.skip_lan,
        staff_port=args.port, staff_ip=args.wifi_ip or "",
        staff_clients=args.clients if args.clients is not None else 25,
        staff_first=args.start, configure_staff=bool(args.wifi_ip),
        guest_port=args.guest_port, guest_ip=args.guest_ip if guest_on else "",
        guest_clients=args.guest_clients if args.guest_clients is not None else 25,
        guest_first=args.start, configure_guest=guest_on,
        wan_pppoe=True,
        pppoe_user=args.pppoe_user or "", pppoe_pass=args.pppoe_pass or "",
        web_filter=not args.no_filters, app_filter=not args.no_filters,
        ssl_mode=args.ssl,
    )


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--wifi-ip")
    ap.add_argument("--clients", type=int)
    ap.add_argument("--guest-ip")
    ap.add_argument("--guest-clients", type=int)
    ap.add_argument("--start", type=int, default=2)
    ap.add_argument("--hostname")
    ap.add_argument("--port", default="internal2")
    ap.add_argument("--guest-port", default="internal3")
    ap.add_argument("--lan-port", default="internal")
    ap.add_argument("--lan-ip", default="172.21.0.1")
    ap.add_argument("--lan-mask", default="255.255.248.0")
    ap.add_argument("--lan-start", default="172.21.0.100")
    ap.add_argument("--lan-end", default="172.21.0.230")
    ap.add_argument("--lan-only", action="store_true")
    ap.add_argument("--skip-lan", action="store_true")
    ap.add_argument("--no-filters", action="store_true")
    ap.add_argument("--ssl", default="deep-inspection",
                    choices=["certificate-inspection", "deep-inspection",
                             "no-inspection"])
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--host")
    ap.add_argument("--user")
    ap.add_argument("--password")
    ap.add_argument("--pppoe-user")
    ap.add_argument("--pppoe-pass")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    cfg = load_env()
    host = args.host or cfg["FGT_HOST"]
    user = args.user or cfg["FGT_USER"]
    password = args.password if args.password is not None else cfg["FGT_PASSWORD"]
    if args.pppoe_user is None:
        args.pppoe_user = cfg.get("FGT_PPPOE_USER", "")
    if args.pppoe_pass is None:
        args.pppoe_pass = cfg.get("FGT_PPPOE_PASS", "")

    # ---- PHASE 1: office LAN only ---------------------------------------
    if args.lan_only:
        spec = build_spec(args)
        spec.configure_lan = True
        print("=" * 66)
        print("  FortiGate branch provisioner -- PHASE 1 (office LAN only)")
        print("=" * 66)
        print(f"  Target device : {host}  (admin: {user})")
        print(f"  {spec.lan_port} -> {spec.lan_ip} {spec.lan_mask}, "
              f"DHCP {spec.lan_start}-{spec.lan_end}")
        print("  This drops your management session on purpose.")
        print("-" * 66)
        if not args.yes and input("  Proceed? [y/N]: ").strip().lower() not in ("y", "yes"):
            print("  Aborted. Nothing changed.")
            return
        fg = FortiGate(host, user, password)
        branch.lan_phase(fg, spec, log)
        print("\n" + "=" * 66)
        print("  PHASE 1 DONE.")
        print("=" * 66)
        print(f"  1. Renew your address:  ipconfig /release && ipconfig /renew")
        print(f"  2. Reconnect to:        https://{spec.lan_ip}")
        print(f"  3. Set FGT_HOST={spec.lan_ip} in .env")
        print(f"  4. Run PHASE 2:  python provision-branch.py --skip-lan "
              f"--wifi-ip ... --guest-ip ... --yes")
        return

    # ---- gather per-branch values ---------------------------------------
    if not args.wifi_ip:
        args.wifi_ip = input("  Staff WiFi gateway IP (e.g. 192.168.1.1): ").strip()
    if args.clients is None:
        args.clients = int(input("  Number of Staff WiFi clients [25]: ").strip() or 25)
    if args.guest_ip is None and not args.yes:
        args.guest_ip = input(
            "  Guest WiFi gateway IP (e.g. 192.168.2.1, '-' to skip): ").strip() or "-"
    if args.guest_clients is None:
        args.guest_clients = 25
    if args.hostname is None and not args.yes:
        args.hostname = input("  Device hostname (optional): ").strip() or None

    spec = build_spec(args)
    spec.configure_lan = False          # applied only via --lan-only

    errs = branch.validate(spec)
    if errs:
        print("[!] Please fix:")
        for e in errs:
            print(f"    - {e}")
        raise SystemExit(1)

    # ---- confirm ---------------------------------------------------------
    print("=" * 66)
    print("  FortiGate branch provisioner")
    print("=" * 66)
    print(f"  Target device : {host}  (admin: {user})")
    print("-" * 66)
    print("  About to apply:")
    print(f"    {spec.lan_port} (office LAN) : left as-is")
    if spec.configure_staff:
        a, b = spec.staff_range()
        print(f"    {spec.staff_port} (Staff WiFi)  : {spec.staff_ip} {branch.NETMASK}")
        print(f"      DHCP pool ({spec.staff_clients})       : {a} - {b}")
    if spec.configure_guest:
        a, b = spec.guest_range()
        print(f"    {spec.guest_port} (Guest WiFi)  : {spec.guest_ip} {branch.NETMASK}")
        print(f"      DHCP pool ({spec.guest_clients})       : {a} - {b}")
    print(f"    NAT policies            : all inside networks -> wan1")
    print(f"    wan1                    : PPPoE" +
          (" (credentials from .env)" if spec.pppoe_user else " (creds entered ON-SITE)"))
    if spec.web_filter or spec.app_filter:
        print(f"    Web / app filters       : on {', '.join(spec.filtered_ports())}")
        print(f"    HTTPS inspection        : {spec.ssl_mode}")
    else:
        print(f"    Web / app filters       : skipped (--no-filters)")
    if spec.hostname:
        print(f"    hostname                : {spec.hostname}")
    print("-" * 66)
    if not args.yes and input("  Proceed? [y/N]: ").strip().lower() not in ("y", "yes"):
        print("  Aborted. Nothing changed.")
        return

    # ---- apply -----------------------------------------------------------
    print("\n  Connecting...")
    fg = FortiGate(host, user, password)

    # Preflight: the FortiGate refuses overlapping interface subnets, and the
    # LAN cannot be moved in this same run because that kills the session.
    lan = fg.results(f"/api/v2/cmdb/system/interface/{spec.lan_port}")[0]
    parts = (lan.get("ip") or "").split()
    if len(parts) == 2 and parts[0] != "0.0.0.0":
        import ipaddress
        lan_net = ipaddress.ip_network(f"{parts[0]}/{parts[1]}", strict=False)
        for label, want, on in (("Staff WiFi", spec.staff_ip, spec.configure_staff),
                                ("Guest WiFi", spec.guest_ip, spec.configure_guest)):
            if on and ipaddress.ip_network(f"{want}/24", strict=False).overlaps(lan_net):
                raise SystemExit(
                    f"[!] {label} {want}/24 overlaps the office LAN ({lan_net}) on "
                    f"{spec.lan_port}.\n"
                    f"    The FortiGate will not accept both. Move the LAN first:\n"
                    f"      python provision-branch.py --lan-only\n"
                    f"    then reconnect at the new LAN IP and re-run this command.")

    print("  Applying configuration:")
    branch.provision(fg, spec, log, apply_filters=utm.apply_filters)

    print("\n" + "=" * 66)
    print("  DONE. Branch base config applied.")
    print("=" * 66)

    if args.verify:
        print("\n  Verifying...")
        results = branch.verify(fg, spec, utm_mod=utm)
        passed = sum(1 for _, ok, _ in results if ok)
        for label, ok, detail in results:
            if not ok:
                print(f"    [FAIL] {label}: {detail}")
        print(f"  RESULT: {passed}/{len(results)} checks passed"
              + ("  -- ALL GOOD" if passed == len(results) else "  -- SEE FAILURES"))

    print("\n  ON-SITE steps remaining:")
    print("   1. Enter the ISP PPPoE username/password on wan1, plug in the WAN cable.")
    print(f"   2. Plug the Staff WiFi access point into {spec.staff_port}.")
    if spec.configure_guest:
        print(f"   3. Plug the Guest WiFi access point into {spec.guest_port}.")
        print("      Guest is internet-only -- no route to LAN, Staff or HO.")
    if spec.ssl_mode == "deep-inspection" and (spec.web_filter or spec.app_filter):
        print("   4. Install the FortiGate CA certificate (Fortinet_CA_SSL) on every")
        print("      client device -- HTTPS deep inspection is on, so without it")
        print("      browsers block every secure site.")
    print(f"\n  NOTE: {spec.staff_port} and {spec.guest_port} are removed from the")
    print("  'internal' hardware switch automatically if they are members, so a")
    print("  factory-default 60F needs no manual port break-out.")


if __name__ == "__main__":
    try:
        main()
    except FortiGateError as e:
        raise SystemExit(f"[!] {e}")
