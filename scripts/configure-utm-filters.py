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
from fortigate import appctrl, branch, utm                     # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ssl", default="deep-inspection",
                    choices=["certificate-inspection", "deep-inspection",
                             "no-inspection"])
    ap.add_argument("--lan-port", default="internal")
    ap.add_argument("--staff-port", default="internal2")
    ap.add_argument("--off", action="store_true",
                    help="remove filtering from the in-scope policies")
    ap.add_argument("--update-urls", action="store_true",
                    help="send ONLY the blocked-site list to an already-configured "
                         "firewall; touches no interfaces, policies or app control")
    ap.add_argument("--from-file", metavar="PATH",
                    help="with --update-urls: read the sites from a text file, "
                         "one per line (default: the built-in standard list)")
    ap.add_argument("--groups", metavar="A,B",
                    help="with --update-urls: only these groups, e.g. "
                         + ",".join(k for k, _, _ in utm.URL_GROUPS))
    ap.add_argument("--list-groups", action="store_true",
                    help="print the built-in site groups and exit")
    ap.add_argument("--profile", default=utm.WEBFILTER_PROFILE,
                    help="with --update-urls: which web filter profile's list "
                         f"to rewrite (default: {utm.WEBFILTER_PROFILE})")
    ap.add_argument("--list-profiles", action="store_true",
                    help="show the web filter profiles on the device, the URL "
                         "table each uses and which policies use it")
    ap.add_argument("--find-app", metavar="NAME",
                    help="search the device's signature database and show the "
                         "real category of each match")
    ap.add_argument("--app-category", metavar="NAME",
                    help="list every signature in one category, e.g. Collaboration")
    ap.add_argument("--app-categories", action="store_true",
                    help="list the signature categories and how many are in each")
    ap.add_argument("--block-app", action="append", default=[], metavar="NAME",
                    help="block one application by signature name; repeatable")
    ap.add_argument("--block-messaging", action="store_true",
                    help="block the messaging apps (Telegram, IMO, Botim, ...); "
                         "WhatsApp is never included")
    ap.add_argument("--show-sensor", action="store_true",
                    help="what the application sensor blocks now")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()

    if args.list_groups:
        for key, label, urls in utm.URL_GROUPS:
            print(f"  {key:<10} {label:<42} {len(urls):>3} entries")
        print(f"  {'':<10} {'always allowed: ' + ', '.join(utm.ALLOW_URLS)}")
        return

    cfg = load_env()
    fg = FortiGate(cfg["FGT_HOST"], cfg["FGT_USER"], cfg["FGT_PASSWORD"])
    spec = branch.BranchSpec(lan_port=args.lan_port, staff_port=args.staff_port,
                             ssl_mode=args.ssl)

    if args.app_categories or args.find_app or args.app_category:
        sigs, path = appctrl.load_signatures(fg)
        print(f"  {len(sigs)} signatures read from {path}")
        if args.app_categories:
            for name, count in appctrl.categories(sigs):
                print(f"    {name:<24} {count:>5}")
            return
        rows = appctrl.search(sigs, args.find_app or "", args.app_category or "")
        blocked = set()
        try:
            blocked = set(appctrl.read_sensor(fg, utm.APPLIST_NAME)["applications"])
        except FortiGateError:
            pass
        print(f"  {len(rows)} match:")
        for s in rows[:200]:
            mark = "  <-- BLOCKED" if s["id"] in blocked else ""
            print(f"    {str(s['name'])[:38]:<38} {s['category']:<18} "
                  f"{s['technology']:<16} id {s['id']}{mark}")
        if len(rows) > 200:
            print(f"    ... and {len(rows) - 200} more")
        return

    if args.show_sensor:
        s = appctrl.read_sensor(fg, utm.APPLIST_NAME)
        names = dict(utm.ALL_CATEGORIES)
        print(f"  sensor '{utm.APPLIST_NAME}':")
        print("    categories: " + (", ".join(
            f"{names.get(c, c)} ({c})" for c in s["categories"]) or "none"))
        if s["applications"]:
            sigs, _p = appctrl.load_signatures(fg)
            by_id = {x["id"]: x for x in sigs}
            for a in s["applications"]:
                x = by_id.get(a)
                print(f"    app {a:<8} {x['name'] if x else '(unknown id)':<32} "
                      f"{x['category'] if x else ''}")
        else:
            print("    individual applications: none")
        pols = appctrl.sensor_policies(fg, utm.APPLIST_NAME)
        print("    enforced by: " + (", ".join(pols) or
                                     "NO POLICY -- nothing is blocked"))
        return

    if args.block_app or args.block_messaging:
        sigs, _p = appctrl.load_signatures(fg)
        wanted = list(args.block_app)
        if args.block_messaging:
            wanted += appctrl.MESSAGING_APPS
        print(f"  Blocking {len(wanted)} named application(s) on "
              f"'{utm.APPLIST_NAME}':")
        appctrl.block_apps(fg, utm.APPLIST_NAME, sigs, wanted,
                           lambda m: print(f"    {m}"))
        return

    if args.list_profiles:
        print(f"Web filter profiles on {cfg['FGT_HOST']}:")
        for p in utm.list_profiles(fg):
            pols = utm.profile_policies(fg, p["name"])
            table = f"#{p['table']}" if p["table"] else "(none)"
            print(f"  {p['name']:<22} URL table {table:<8} "
                  + (f"used by {', '.join(pols)}" if pols
                     else "not used by any policy")
                  + ("   <-- default target" if p["name"] == args.profile else ""))
        return

    if args.update_urls:
        if args.from_file:
            urls = [ln.strip() for ln in
                    Path(args.from_file).read_text(encoding="utf-8").splitlines()
                    if ln.strip() and not ln.startswith("#")]
        elif args.groups:
            urls = []
            for key in [g.strip() for g in args.groups.split(",") if g.strip()]:
                got = utm.group_urls(key)
                if not got:
                    raise SystemExit(f"[!] unknown group '{key}'. "
                                     f"Try --list-groups.")
                urls += got
        else:
            urls = [u for u, _ in utm.DEFAULT_URLS]
        print(f"Updating the blocked-site list on {cfg['FGT_HOST']} "
              f"({len(urls)} sites, profile '{args.profile}'):")
        utm.update_urls(fg, urls, lambda m: print(f"  {m}"),
                        profile=args.profile)
        return

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
