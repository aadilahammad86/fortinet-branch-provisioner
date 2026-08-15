#!/usr/bin/env python3
"""One-off: set wan1 to PPPoE.

Thin CLI wrapper around fortigate.branch.set_wan1_pppoe.

By default the ISP username/password are left BLANK so the unit can be staged
centrally and the credentials typed in on-site. Pass --user/--pass (or set
FGT_PPPOE_USER / FGT_PPPOE_PASS in ../.env plus --from-env) to fill them in.

Once wan1 dials, it also takes the ISP's DNS servers (dns-server-override) and
hands them to DHCP clients automatically -- there is no DNS to configure.

Usage:  python configure-wan1-pppoe.py [--from-env | --user U --pass P]
"""
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fortigate import FortiGate, FortiGateError, load_env      # noqa: E402
from fortigate import branch                                   # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="")
    ap.add_argument("--password", "--pass", dest="password", default="")
    ap.add_argument("--from-env", action="store_true",
                    help="take the ISP credentials from .env")
    args = ap.parse_args()

    cfg = load_env()
    user, password = args.user, args.password
    if args.from_env:
        user = user or cfg.get("FGT_PPPOE_USER", "")
        password = password or cfg.get("FGT_PPPOE_PASS", "")

    fg = FortiGate(cfg["FGT_HOST"], cfg["FGT_USER"], cfg["FGT_PASSWORD"])
    branch.set_wan1_pppoe(fg, user, password, lambda m: print(f"  {m}"))

    w = fg.results("/api/v2/cmdb/system/interface/wan1")[0]
    print("\n--- verification ---")
    print(f"  wan1 mode={w.get('mode')} defaultgw={w.get('defaultgw')} "
          f"distance={w.get('distance')} dns-override={w.get('dns-server-override')}")
    print(f"  ISP username: {w.get('username') or '(blank -- enter on-site)'}")


if __name__ == "__main__":
    try:
        main()
    except FortiGateError as e:
        raise SystemExit(f"[!] {e}")
