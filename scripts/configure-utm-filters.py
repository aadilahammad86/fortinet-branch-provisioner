#!/usr/bin/env python3
"""Apply the branch web filter + application control filter.

Thin CLI wrapper around fortigate.utm -- the GUI applies the identical objects.

  * Web filter  "Branch-WebFilter"   -- static URL list (no FortiGuard licence
                                        needed) blocking Facebook and YouTube
  * App control "Branch-AppControl"  -- blocks category 7 (Remote.Access) and
                                        category 23 (Social.Media)

Attached to the LAN and Staff WiFi internet policies only. Guest WiFi is left
unfiltered -- it is already isolated from the inside networks, so it is treated
as an untrusted public hotspot.

    >> HTTPS: with --ssl deep-inspection the FortiGate CA certificate
    >> (Fortinet_CA_SSL) must be installed on every client device, or browsers
    >> block every secure site. --ssl certificate-inspection blocks the same
    >> sites with no per-device setup.

LICENCE NOTE: a unit that has never had an uplink reports all FortiGuard
entitlements as `pending`. The static URL filter works offline. Application
control enforces on the firmware's bundled signature database but will not
update until the unit is registered and online. FortiGuard *category* web
filtering is deliberately NOT used -- it fails silently without live lookups.

Usage:  python configure-utm-filters.py [--ssl MODE] [--off] [--verify]
"""
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fortigate import FortiGate, FortiGateError, load_env      # noqa: E402
from fortigate import branch, utm                              # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ssl", default="deep-inspection",
                    choices=["certificate-inspection", "deep-inspection",
                             "no-inspection"])
    ap.add_argument("--lan-port", default="internal")
    ap.add_argument("--staff-port", default="internal2")
    ap.add_argument("--off", action="store_true",
                    help="remove filtering from the in-scope policies")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()

    cfg = load_env()
    fg = FortiGate(cfg["FGT_HOST"], cfg["FGT_USER"], cfg["FGT_PASSWORD"])
    spec = branch.BranchSpec(lan_port=args.lan_port, staff_port=args.staff_port,
                             ssl_mode=args.ssl)

    if args.off:
        print("Removing branch UTM filters:")
        utm.clear_filters(fg, spec, lambda m: print(f"  {m}"))
        return

    print("Applying branch UTM filters:")
    utm.apply_filters(fg, spec, lambda m: print(f"  {m}"))

    lic = utm.licence_state(fg)
    pending = [k for k, v in lic.items() if v != "valid"]
    if pending:
        print(f"\n  [note] FortiGuard entitlements pending: {', '.join(pending)}")
        print("         URL filtering is unaffected; application control enforces on")
        print("         the bundled signature database but will not update yet.")

    if args.verify:
        print("\n--- verification ---")
        results = branch.verify(fg, spec, utm_mod=utm)
        passed = sum(1 for _, ok, _ in results if ok)
        for label, ok, detail in results:
            if not ok:
                print(f"  [FAIL] {label}: {detail}")
        print(f"  RESULT: {passed}/{len(results)} checks passed")


if __name__ == "__main__":
    try:
        main()
    except FortiGateError as e:
        raise SystemExit(f"[!] {e}")
