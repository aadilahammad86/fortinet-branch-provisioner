#!/usr/bin/env python3
"""Why is a blocked site or app still reachable? A read-only audit.

Answers the question "we blocked Telegram, so why is it working on the WiFi?"
by reading what is ACTUALLY on the firewall rather than what we believe we
applied. It writes nothing -- safe to run on a live branch at any time.

It checks, in the order things actually fail:

  1. Which inside networks have filtering at all. Guest WiFi is unfiltered on
     purpose, so a phone on Guest is not blocked by anything -- that alone
     explains most "it still works" reports.
  2. Whether each internet policy really has the web filter and application
     list attached, and with which SSL inspection mode.
  3. Whether the blocked-site list contains the site in question.
  4. Which application-control categories are blocked, and which category the
     device's own signature database puts that app in.
  5. Whether the signature database can even be current (FortiGuard
     entitlements) -- an app signature that has never updated will not catch a
     protocol that changed since the firmware shipped.

Usage:
    python audit-filtering.py                     # full audit
    python audit-filtering.py --app telegram      # focus on one app
    python audit-filtering.py --app telegram --site telegram.org
"""
import sys
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fortigate import FortiGate, FortiGateError, load_env      # noqa: E402
from fortigate import utm                                      # noqa: E402

# Signature lists live in different places depending on build; try in order.
SIG_PATHS = [
    "/api/v2/cmdb/application/name",
    "/api/v2/monitor/application/name",
    "/api/v2/cmdb/application/list/name",
]


def head(title):
    print("\n" + "=" * 74)
    print(f"  {title}")
    print("=" * 74)


def signatures(fg, needle):
    """Every signature whose name contains `needle`, with its category id."""
    for path in SIG_PATHS:
        try:
            rows = fg.results(f"{path}?filter=name=@{needle}")
        except FortiGateError:
            try:
                rows = fg.results(path)
            except FortiGateError:
                continue
        hits = [r for r in rows
                if needle.lower() in str(r.get("name", "")).lower()]
        if hits or rows:
            return hits, len(rows), path
    return None, 0, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default="telegram",
                    help="application to look up in the signature database")
    ap.add_argument("--site", default="telegram.org",
                    help="domain to look for in the blocked-site list")
    ap.add_argument("--host")
    ap.add_argument("--user")
    ap.add_argument("--password")
    args = ap.parse_args()

    cfg = load_env()
    host = args.host or cfg["FGT_HOST"]
    fg = FortiGate(host, args.user or cfg["FGT_USER"],
                   args.password if args.password is not None
                   else cfg["FGT_PASSWORD"])
    st = fg.status()
    print(f"  {st['model']}  {st['serial']}  {st['version']}  "
          f"hostname {st['hostname']}  at {host}")

    # ---- 1 + 2: the policies, which is where filtering is really decided ---
    head("Internet policies -- what is filtered and what is not")
    policies = fg.results("/api/v2/cmdb/firewall/policy")
    unfiltered = []
    print(f"  {'#':<4} {'from':<22} {'to':<8} {'UTM':<5} {'web filter':<20} "
          f"{'app control':<20} {'SSL':<24} NAT")
    for p in policies:
        src = ",".join(x["name"] for x in p.get("srcintf", []))
        dst = ",".join(x["name"] for x in p.get("dstintf", []))
        if "wan" not in dst:
            continue
        utm_on = (p.get("utm-status") or "disable") == "enable"
        wf = p.get("webfilter-profile") or "-"
        al = p.get("application-list") or "-"
        print(f"  {p.get('policyid'):<4} {src:<22} {dst:<8} "
              f"{'ON' if utm_on else 'OFF':<5} {wf:<20} {al:<20} "
              f"{p.get('ssl-ssh-profile') or '-':<24} {p.get('nat')}")
        if not utm_on or wf == "-":
            unfiltered.append((p.get("policyid"), src, p.get("name")))

    if unfiltered:
        print("\n  [!] These inside networks reach the internet with NO web "
              "filtering:")
        for pid, src, name in unfiltered:
            note = ("  (Guest is unfiltered BY DESIGN -- a phone on Guest WiFi "
                    "is not blocked by anything)"
                    if "internal3" in src else "")
            print(f"      policy #{pid}  {src}  '{name}'{note}")
        print("\n      If the users reporting this are on that WiFi, that is "
              "the whole answer.")
    else:
        print("\n  [ok] every internet policy has a web filter attached.")

    # ---- 3: is the site actually in the list ------------------------------
    head(f"Blocked-site list -- is {args.site} in it?")
    profiles = utm.list_profiles(fg)
    for p in profiles:
        pols = utm.profile_policies(fg, p["name"])
        print(f"  {p['name']:<22} URL table "
              f"{('#' + str(p['table'])) if p['table'] else '(none)':<8} "
              + (f"used by {', '.join(pols)}" if pols else "NOT USED by any policy"))
    tid = next((p["table"] for p in profiles
                if p["name"] == utm.WEBFILTER_PROFILE and p["table"]), None)
    if tid:
        rows = fg.results(f"/api/v2/cmdb/webfilter/urlfilter/{tid}")[0].get("entries", [])
        blocked = [e.get("url") for e in utm.blocked_only(rows)]
        hits = [u for u in blocked if args.app.lower() in u.lower()
                or args.site.lower() in u.lower()]
        print(f"\n  table #{tid}: {len(blocked)} sites blocked")
        print(f"  matching '{args.app}': {hits or 'NONE'}")
        allow = [e.get("url") for e in rows if e.get("action") == "allow"]
        print(f"  explicitly allowed: {allow or 'none'}")
    else:
        print(f"\n  [!] {utm.WEBFILTER_PROFILE} has no URL table -- nothing is "
              f"blocked by name.")

    # ---- 4: application control -------------------------------------------
    head(f"Application control -- would it catch the {args.app} app?")
    try:
        al = fg.results(f"/api/v2/cmdb/application/list/{utm.APPLIST_NAME}")[0]
    except (FortiGateError, IndexError):
        print(f"  [!] no application list '{utm.APPLIST_NAME}' on this device.")
        al = None
    blocked_cats = []
    if al:
        names = dict(utm.ALL_CATEGORIES)
        for e in al.get("entries", []):
            for c in e.get("category", []):
                blocked_cats.append(c["id"])
                print(f"  blocking category {c['id']:<3} "
                      f"{names.get(c['id'], '?'):<18} action={e.get('action')}")
        print(f"  other applications: {al.get('other-application-action')}")

    hits, total, path = signatures(fg, args.app)
    if hits is None:
        print(f"\n  [!] could not read the signature database from this device.")
    else:
        print(f"\n  signature database: {total} signatures read from {path}")
        if not hits:
            print(f"  [!] NO signature matching '{args.app}' -- application "
                  f"control cannot block what it cannot recognise.")
        for h in hits[:15]:
            cid = h.get("category")
            cid = cid.get("id") if isinstance(cid, dict) else cid
            caught = cid in blocked_cats
            print(f"  {h.get('name'):<34} category {str(cid):<4} "
                  f"{'BLOCKED by your list' if caught else '*** NOT BLOCKED ***'}")

    # ---- 5: can the signatures even be current? ---------------------------
    head("FortiGuard entitlements -- can application control update?")
    lic = utm.licence_state(fg)
    for k, v in lic.items():
        print(f"  {k:<16} {v}")
    if any(v != "valid" for v in lic.values()):
        print("\n  [!] Application control is running on the signature database"
              "\n      that shipped with the firmware. Messaging apps change "
              "their\n      protocols often, so an un-updated database misses "
              "them.\n      Registering the unit with FortiCare is what fixes "
              "this.")

    head("How to read all this")
    print("""  A website and a phone app are blocked by two different things.

    telegram.org in a browser  -> stopped by the blocked-site list, but only
                                  when SSL inspection lets the firewall see
                                  which site is being requested.
    the Telegram APP on a phone -> does NOT speak HTTP to telegram.org. It
                                  speaks its own protocol (MTProto) straight
                                  to Telegram's servers, so the URL list never
                                  sees a name to match. ONLY application
                                  control can stop it -- and only if a current
                                  signature recognises it.

  So the three things to confirm, in order:
    1. Which WiFi were the users on? Guest is unfiltered on purpose.
    2. Does the Staff WiFi policy really have BOTH the web filter and the
       application list attached? (see the table at the top)
    3. Is the app's category in the blocked list, with signatures able to
       update?""")


if __name__ == "__main__":
    try:
        main()
    except FortiGateError as e:
        raise SystemExit(f"[!] {e}")
