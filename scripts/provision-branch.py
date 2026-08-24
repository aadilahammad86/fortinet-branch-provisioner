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

SAVED BRANCHES
  Settings for a branch can be stored once and re-used by name. The library is
  the `branches/` folder beside this project (shared with the GUI's "Saved
  branches" bar), one JSON file per branch, never containing a password.

      python provision-branch.py --list-branches
      python provision-branch.py --branch "Al Ain" --skip-lan --yes
      python provision-branch.py --wifi-ip 10.7.10.1 --guest-ip 10.7.20.1 \
                                 --save-branch "Branch 07"

  --branch supplies the defaults; any flag you also pass wins over the saved
  value, so a template can be reused with a one-off tweak.

USAGE
  Interactive:      python provision-branch.py
  Non-interactive:  python provision-branch.py --skip-lan --yes \
                        --wifi-ip 192.168.1.1 --clients 25 \
                        --guest-ip 192.168.2.1 --guest-clients 25 \
                        --hostname FGT-BranchB

  Options:
      --branch NAME        load a saved branch's settings as the defaults
      --save-branch NAME   save these settings to the library under NAME
      --list-branches      print the saved branches and exit
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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fortigate import FortiGate, FortiGateError, load_env      # noqa: E402
from fortigate import branch, templates, utm                   # noqa: E402


def log(msg):
    print(f"  {msg}")


def build_spec(args, tmpl=None):
    """Merge, in increasing priority: built-in default, saved branch, CLI flag."""
    tmpl = tmpl or {}

    def pick(val, key, default):
        if val is not None:
            return val
        saved = tmpl.get(key)
        return default if saved in (None, "") else saved

    staff_ip = pick(args.wifi_ip, "staff_ip", "")
    staff_on = bool(staff_ip) and (args.wifi_ip is not None
                                   or tmpl.get("configure_staff", True))

    guest_ip = pick(args.guest_ip, "guest_ip", "")
    guest_on = guest_ip not in ("", "-") and (args.guest_ip is not None
                                              or tmpl.get("configure_guest", True))

    filters_on = not args.no_filters
    return branch.BranchSpec(
        hostname=pick(args.hostname, "hostname", ""),
        lan_port=pick(args.lan_port, "lan_port", "internal"),
        lan_ip=pick(args.lan_ip, "lan_ip", "172.21.0.1"),
        lan_mask=pick(args.lan_mask, "lan_mask", "255.255.248.0"),
        lan_start=pick(args.lan_start, "lan_start", "172.21.0.100"),
        lan_end=pick(args.lan_end, "lan_end", "172.21.0.230"),
        configure_lan=not args.skip_lan,
        staff_port=pick(args.port, "staff_port", "internal2"),
        staff_ip=staff_ip if staff_on else "",
        staff_clients=int(pick(args.clients, "staff_clients", 25)),
        staff_first=int(pick(args.start, "staff_first", 2)),
        configure_staff=staff_on,
        guest_port=pick(args.guest_port, "guest_port", "internal3"),
        guest_ip=guest_ip if guest_on else "",
        guest_clients=int(pick(args.guest_clients, "guest_clients", 25)),
        guest_first=int(pick(args.start, "guest_first", 2)),
        configure_guest=guest_on,
        wan_pppoe=True,
        pppoe_user=args.pppoe_user or "", pppoe_pass=args.pppoe_pass or "",
        web_filter=filters_on and tmpl.get("web_filter", True),
        app_filter=filters_on and tmpl.get("app_filter", True),
        ssl_mode=pick(args.ssl, "ssl_mode", "certificate-inspection"),
        blocked_urls=list(tmpl.get("blocked_urls") or []),
        blocked_categories=list(tmpl.get("blocked_categories") or []),
    )


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--wifi-ip")
    ap.add_argument("--clients", type=int)
    ap.add_argument("--guest-ip")
    ap.add_argument("--guest-clients", type=int)
    ap.add_argument("--start", type=int)
    ap.add_argument("--hostname")
    ap.add_argument("--port")
    ap.add_argument("--guest-port")
    ap.add_argument("--lan-port")
    ap.add_argument("--lan-ip")
    ap.add_argument("--lan-mask")
    ap.add_argument("--lan-start")
    ap.add_argument("--lan-end")
    ap.add_argument("--lan-only", action="store_true")
    ap.add_argument("--skip-lan", action="store_true")
    ap.add_argument("--no-filters", action="store_true")
    ap.add_argument("--ssl", choices=["certificate-inspection", "deep-inspection",
                                      "no-inspection"])
    ap.add_argument("--branch", help="use a saved branch's settings as defaults")
    ap.add_argument("--save-branch", metavar="NAME",
                    help="save these settings to the branch library as NAME")
    ap.add_argument("--save-only", action="store_true",
                    help="with --save-branch: save and exit, touch no device")
    ap.add_argument("--list-branches", action="store_true",
                    help="list the saved branches and exit")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--host")
    ap.add_argument("--user")
    ap.add_argument("--password")
    ap.add_argument("--pppoe-user")
    ap.add_argument("--pppoe-pass")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    # ---- saved branches --------------------------------------------------
    if args.list_branches:
        rows = templates.summaries(ROOT)
        if not rows:
            print(f"  No saved branches yet in {templates.templates_dir(ROOT)}.")
            print("  Create one by adding --save-branch \"Name\" to a normal run.")
            return
        print(f"  Saved branches ({templates.templates_dir(ROOT)}):")
        width = max(len(n) for n, _ in rows)
        for name, detail in rows:
            print(f"    {name.ljust(width)}   {detail}")
        return

    tmpl = templates.load(args.branch, ROOT) if args.branch else {}
    if tmpl:
        print(f"  Using saved branch '{tmpl.get('branch_name', args.branch)}'"
              + (f" (saved {tmpl['saved']})" if tmpl.get("saved") else ""))

    cfg = load_env()
    host = args.host or tmpl.get("host") or cfg["FGT_HOST"]
    user = args.user or cfg["FGT_USER"]
    password = args.password if args.password is not None else cfg["FGT_PASSWORD"]
    if args.pppoe_user is None:
        args.pppoe_user = cfg.get("FGT_PPPOE_USER", "")
    if args.pppoe_pass is None:
        args.pppoe_pass = cfg.get("FGT_PPPOE_PASS", "")

    # ---- PHASE 1: office LAN only ---------------------------------------
    if args.lan_only:
        spec = build_spec(args, tmpl)
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
    # A saved branch already answers these, so only ask for what is missing.
    if not args.wifi_ip and not tmpl.get("staff_ip"):
        args.wifi_ip = input("  Staff WiFi gateway IP (e.g. 192.168.1.1): ").strip()
    if args.clients is None and not tmpl:
        args.clients = int(input("  Number of Staff WiFi clients [25]: ").strip() or 25)
    if args.guest_ip is None and not tmpl and not args.yes:
        args.guest_ip = input(
            "  Guest WiFi gateway IP (e.g. 192.168.2.1, '-' to skip): ").strip() or "-"
    if args.hostname is None and not tmpl and not args.yes:
        args.hostname = input("  Device hostname (optional): ").strip() or None

    spec = build_spec(args, tmpl)
    spec.configure_lan = False          # applied only via --lan-only

    errs = branch.validate(spec)
    if errs:
        print("[!] Please fix:")
        for e in errs:
            print(f"    - {e}")
        raise SystemExit(1)

    # Saved before touching the device: the settings are valid, and if the
    # apply fails half way the branch can be re-run by name.
    if args.save_branch:
        p = templates.save(args.save_branch, spec, ROOT, host=host)
        print(f"  Saved branch '{args.save_branch}' -> {p}")
        print("  (no passwords are stored in it)")
        if args.save_only:
            print(f"  --save-only: nothing was sent to a device. Provision it with:")
            print(f"      python provision-branch.py --branch \"{args.save_branch}\" "
                  f"--skip-lan --yes")
            return
    elif args.save_only:
        raise SystemExit("[!] --save-only needs --save-branch NAME.")

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
    except (FortiGateError, templates.TemplateError) as e:
        raise SystemExit(f"[!] {e}")
