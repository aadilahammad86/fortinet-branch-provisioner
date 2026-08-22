"""Web filter (static URL list) and application control.

Deliberately uses a STATIC URL FILTER rather than FortiGuard web categories:
the static list works with no FortiGuard licence and no internet lookup, while
category filtering needs live rating lookups and fails silently without them.

Category IDs were verified against a real FortiGate 60F's own signature
database (2,414 signatures), not taken from documentation:
    7  = Remote.Access  (Teamviewer, AnyDesk, RDP, VNC, LogMeIn, Telnet, ...)
    23 = Social.Media   (Facebook, Instagram, Twitter, Snapchat, LinkedIn, ...)

Note YouTube is category 5 (Video/Audio), NOT Social.Media -- application
control does not catch it. YouTube is blocked by the URL filter below.
"""
from .client import FortiGateError

URLFILTER_ID = 1
URLFILTER_NAME = "Branch-Blocked-Sites"
WEBFILTER_PROFILE = "Branch-WebFilter"
APPLIST_NAME = "Branch-AppControl"

# ---- what gets blocked -------------------------------------------------
# Grouped so the GUI can offer them a group at a time and so it is obvious
# what a line is for. A bare domain needs a `simple` entry; subdomains need
# their own `*.` wildcard. CDN domains matter -- without them a site half
# loads and looks broken rather than blocked.
#
# WhatsApp is deliberately absent from every block group and is explicitly
# allowed below: it is the one messaging platform the business relies on.
URL_GROUPS = [
    ("social", "Social media", [
        "facebook.com", "*.facebook.com", "*.fbcdn.net", "fb.watch",
        "instagram.com", "*.instagram.com", "*.cdninstagram.com",
        "threads.net", "*.threads.net", "threads.com", "*.threads.com",
        "twitter.com", "*.twitter.com", "x.com", "*.x.com", "*.twimg.com",
        "t.co",
        "tiktok.com", "*.tiktok.com", "*.tiktokcdn.com", "*.tiktokv.com",
        "snapchat.com", "*.snapchat.com", "*.sc-cdn.net",
        "reddit.com", "*.reddit.com", "*.redd.it",
        "pinterest.com", "*.pinterest.com", "*.pinimg.com",
        "tumblr.com", "*.tumblr.com",
        "sharechat.com", "*.sharechat.com",
        "likee.video", "*.likee.video",
    ]),
    ("messaging", "Messaging apps (WhatsApp stays allowed)", [
        "telegram.org", "*.telegram.org", "telegram.me", "*.telegram.me",
        "t.me", "telesco.pe", "*.telesco.pe",
        "imo.im", "*.imo.im",
        "line.me", "*.line.me", "*.line-apps.com", "*.line-scdn.net",
        "messenger.com", "*.messenger.com", "m.me",
        "signal.org", "*.signal.org",
        "viber.com", "*.viber.com",
        "wechat.com", "*.wechat.com", "weixin.qq.com",
        "botim.me", "*.botim.me",
        "discord.com", "*.discord.com", "discord.gg",
        "*.discordapp.com", "*.discordapp.net",
        "skype.com", "*.skype.com",
    ]),
    ("video", "Video sharing", [
        "youtube.com", "*.youtube.com", "youtu.be",
        "*.googlevideo.com", "*.ytimg.com",
    ]),
    ("news", "Malayalam and Gulf news", [
        "mediaoneonline.com", "*.mediaoneonline.com",
        "madhyamam.com", "*.madhyamam.com",
        "reporterlive.com", "*.reporterlive.com",
        "twentyfournews.com", "*.twentyfournews.com",
        "manoramaonline.com", "*.manoramaonline.com",
        "manoramanews.com", "*.manoramanews.com",
        "onmanorama.com", "*.onmanorama.com",
        "mathrubhumi.com", "*.mathrubhumi.com",
        "asianetnews.com", "*.asianetnews.com",
        "keralakaumudi.com", "*.keralakaumudi.com",
        "deshabhimani.com", "*.deshabhimani.com",
        "marunadanmalayalee.com", "*.marunadanmalayalee.com",
        "deepika.com", "*.deepika.com",
        "malayalam.news18.com",
        "gulfnews.com", "*.gulfnews.com",
        "khaleejtimes.com", "*.khaleejtimes.com",
    ]),
    ("jobs", "Job hunting", [
        "linkedin.com", "*.linkedin.com", "*.licdn.com",
        "indeed.com", "*.indeed.com", "indeed.ae", "*.indeed.ae",
        "naukri.com", "*.naukri.com",
        "naukrigulf.com", "*.naukrigulf.com",
        "bayt.com", "*.bayt.com",
        "gulftalent.com", "*.gulftalent.com",
        "foundit.in", "*.foundit.in", "foundit.ae", "*.foundit.ae",
        "monstergulf.com", "*.monstergulf.com",
        "monster.com", "*.monster.com",
        "shine.com", "*.shine.com",
        "timesjobs.com", "*.timesjobs.com",
        "glassdoor.com", "*.glassdoor.com",
    ]),
]

# Written as `allow` entries ABOVE every block entry, so no wildcard can ever
# catch WhatsApp by accident and it is visible in the FortiGate GUI that the
# exception is deliberate rather than an oversight.
ALLOW_URLS = [
    "whatsapp.com", "*.whatsapp.com", "whatsapp.net", "*.whatsapp.net",
]

GROUP_LABELS = {key: label for key, label, _ in URL_GROUPS}


def group_urls(key):
    for k, _label, urls in URL_GROUPS:
        if k == key:
            return list(urls)
    return []


DEFAULT_URLS = [(u, "wildcard" if "*" in u else "simple")
                for _k, _l, urls in URL_GROUPS for u in urls]

DEFAULT_CATEGORIES = [(7, "Remote.Access"), (23, "Social.Media")]

# All FortiGate application-control categories, for the GUI's checkbox list.
ALL_CATEGORIES = [
    (2, "P2P"), (3, "VoIP"), (5, "Video/Audio"), (6, "Proxy"),
    (7, "Remote.Access"), (8, "Game"), (12, "General.Interest"),
    (15, "Network.Service"), (17, "Update"), (21, "Email"),
    (22, "Storage.Backup"), (23, "Social.Media"), (25, "Web.Client"),
    (28, "Collaboration"), (29, "Business"), (30, "Cloud.IT"), (31, "Mobile"),
]


def _noop(_msg):
    pass


def _url_type(url):
    return "wildcard" if "*" in url else "simple"


def spec_urls(spec):
    if spec.blocked_urls:
        return [(u, _url_type(u)) for u in spec.blocked_urls]
    return list(DEFAULT_URLS)


def spec_categories(spec):
    if spec.blocked_categories:
        names = dict(ALL_CATEGORIES)
        return [(cid, names.get(cid, str(cid))) for cid in spec.blocked_categories]
    return list(DEFAULT_CATEGORIES)


def url_entries(urls):
    """Build the URL-filter rows: the WhatsApp allows first, then the blocks.

    Order matters -- the FortiGate walks this table and takes the first match,
    so the allow rows have to sit above every wildcard that could swallow them.

    `urls` may be plain strings or (url, type) pairs; the type is derived when
    it is not given, so callers never have to think about simple vs wildcard.
    """
    urls = [(u, _url_type(u)) if isinstance(u, str) else u for u in urls]
    rows = [
        {"id": i, "url": u, "type": _url_type(u), "action": "allow",
         "status": "enable"}
        for i, u in enumerate(ALLOW_URLS, start=1)
    ]
    rows += [
        {"id": i, "url": url, "type": typ, "action": "block",
         "status": "enable", "exempt": "av"}
        for i, (url, typ) in enumerate(urls, start=len(rows) + 1)
    ]
    return rows


def blocked_only(entries):
    """The block rows of a table read off the device, ignoring the allows."""
    return [e for e in entries if e.get("action") == "block"]


def list_profiles(fg):
    """Every web filter profile on the device, with the URL table each uses.

    A FortiGate ships with `default`, `monitor-all` and `wifi-default` beside
    whatever the branch standard adds, so "which profile am I editing?" is a
    real question -- the front ends show this list rather than assuming.
    """
    out = []
    for p in fg.results("/api/v2/cmdb/webfilter/profile"):
        table = (p.get("web") or {}).get("urlfilter-table") or 0
        out.append({"name": p.get("name"), "comment": p.get("comment") or "",
                    "table": table})
    return out


def profile_policies(fg, profile):
    """Names of the enabled policies that actually enforce `profile`."""
    return [p.get("name") or f"#{p.get('policyid')}"
            for p in fg.results("/api/v2/cmdb/firewall/policy")
            if p.get("webfilter-profile") == profile
            and p.get("utm-status") == "enable"]


def profile_table(fg, profile):
    """(table_id, table_name) of the URL table `profile` uses, or (0, '').

    Raises if the profile does not exist -- writing a list nothing reads is
    worse than refusing, because it looks like it worked.
    """
    try:
        wf = fg.results(f"/api/v2/cmdb/webfilter/profile/{profile}")[0]
    except (FortiGateError, IndexError):
        raise FortiGateError(
            f"This firewall has no web filter profile called '{profile}'. "
            f"Pick one of the profiles it does have, or run a full Apply to "
            f"create the branch standard one.")
    tid = (wf.get("web") or {}).get("urlfilter-table") or 0
    if not tid:
        return 0, ""
    try:
        return tid, fg.results(
            f"/api/v2/cmdb/webfilter/urlfilter/{tid}")[0].get("name") or ""
    except (FortiGateError, IndexError):
        return tid, ""


def describe_target(fg, profile):
    """One-line summary of what an update to `profile` would write to."""
    tid, tname = profile_table(fg, profile)
    pols = profile_policies(fg, profile)
    where = (f"URL table #{tid} '{tname}'" if tid
             else "no URL table yet -- one will be created")
    used = (f"used by {', '.join(pols)}" if pols
            else "NOT used by any policy -- nothing would be blocked")
    return f"{profile} -> {where} | {used}", tid, tname, pols


def update_urls(fg, urls, log=_noop, profile=WEBFILTER_PROFILE):
    """Rewrite ONLY the blocked-site list that `profile` reads from.

    Touches nothing else -- no interfaces, no policies, no application control.
    The profile is already attached to the policies, so the new list takes
    effect the moment the table is written. Safe to run against a live branch.

    The table is looked up FROM the profile rather than assumed, so pointing
    this at a different profile edits that profile's own list and cannot
    quietly overwrite the branch standard one.
    """
    tid, tname = profile_table(fg, profile)          # raises if no such profile
    if not tid:
        used = {p["table"] for p in list_profiles(fg) if p["table"]}
        tid = URLFILTER_ID if URLFILTER_ID not in used else max(used) + 1
        tname = URLFILTER_NAME
        log(f"[..] '{profile}' had no URL table -- creating #{tid} '{tname}'")
    log(f"[..] target: profile '{profile}' -> URL table #{tid} "
        f"'{tname or URLFILTER_NAME}'")

    entries = url_entries(urls)
    state = fg.upsert("/api/v2/cmdb/webfilter/urlfilter", tid, {
        "id": tid, "name": tname or URLFILTER_NAME,
        "comment": "Branch standard blocked sites",
        "entries": entries,
    })
    live = fg.results(f"/api/v2/cmdb/webfilter/urlfilter/{tid}")[0].get("entries", [])
    if len(live) != len(entries):
        raise FortiGateError(
            f"URL list did not stick: sent {len(entries)} entries, the device "
            f"reports {len(live)}.")
    log(f"[ok] table #{tid} {state}: {len(blocked_only(live))} sites blocked, "
        f"{len(ALLOW_URLS)} WhatsApp entries allowed")

    wf = fg.results(f"/api/v2/cmdb/webfilter/profile/{profile}")[0]
    if (wf.get("web") or {}).get("urlfilter-table") != tid:
        fg.call("PUT", f"/api/v2/cmdb/webfilter/profile/{profile}",
                {"web": {"urlfilter-table": tid}})
        log(f"[ok] profile '{profile}' pointed at table #{tid}")

    # Report, do not touch: which policies actually enforce this.
    using = profile_policies(fg, profile)
    if using:
        log(f"[ok] live on {len(using)} polic{'y' if len(using) == 1 else 'ies'}: "
            f"{', '.join(using)}")
    else:
        log(f"[!] no policy uses '{profile}' -- the list is saved but nothing is "
            f"being blocked. Run a full Apply to attach it.")
    return len(blocked_only(live))


def apply_filters(fg, spec, log=_noop):
    """Create the UTM objects and attach them to the in-scope policies."""
    scope = spec.filtered_ports()

    if spec.web_filter:
        entries = url_entries(spec_urls(spec))
        state = fg.upsert("/api/v2/cmdb/webfilter/urlfilter", URLFILTER_ID, {
            "id": URLFILTER_ID, "name": URLFILTER_NAME,
            "comment": "Branch standard blocked sites",
            "entries": entries,
        })
        log(f"[ok] URL filter table #{URLFILTER_ID} '{URLFILTER_NAME}' "
            f"({len(blocked_only(entries))} blocked, {len(ALLOW_URLS)} allowed) "
            f"{state}")

        state = fg.upsert("/api/v2/cmdb/webfilter/profile", WEBFILTER_PROFILE, {
            "name": WEBFILTER_PROFILE,
            "comment": "Branch standard: block listed sites by URL",
            "feature-set": "flow",
            "options": "block-invalid-url",
            "web": {"urlfilter-table": URLFILTER_ID},
            "log-all-url": "enable",
        })
        log(f"[ok] web filter profile '{WEBFILTER_PROFILE}' {state}")

    if spec.app_filter:
        cats = spec_categories(spec)
        app_entries = [
            {"id": i, "category": [{"id": cid}], "action": "block", "log": "enable"}
            for i, (cid, _) in enumerate(cats, start=1)
        ]
        state = fg.upsert("/api/v2/cmdb/application/list", APPLIST_NAME, {
            "name": APPLIST_NAME,
            "comment": "Branch standard application control",
            "entries": app_entries,
            "other-application-action": "pass",
            "other-application-log": "disable",
            "unknown-application-action": "pass",
            "app-replacemsg": "enable",
        })
        names = ", ".join(f"{n}({c})" for c, n in cats)
        log(f"[ok] application list '{APPLIST_NAME}' {state} -> block {names}")

    for p in fg.results("/api/v2/cmdb/firewall/policy"):
        srcs = [x["name"] for x in p.get("srcintf", [])]
        dsts = [x["name"] for x in p.get("dstintf", [])]
        pid = p.get("policyid")
        label = p.get("name") or f"#{pid}"
        if "wan1" not in dsts:
            continue
        if not any(s in scope for s in srcs):
            log(f"[skip] policy {label} ({','.join(srcs)}->wan1) -- out of scope, "
                f"left unfiltered")
            continue
        body = {
            "utm-status": "enable",
            "ssl-ssh-profile": spec.ssl_mode,
            "profile-protocol-options": "default",
            "logtraffic": "all",
            "webfilter-profile": WEBFILTER_PROFILE if spec.web_filter else "",
            "application-list": APPLIST_NAME if spec.app_filter else "",
        }
        fg.call("PUT", f"/api/v2/cmdb/firewall/policy/{pid}", body)
        bits = [b for b in (
            WEBFILTER_PROFILE if spec.web_filter else None,
            APPLIST_NAME if spec.app_filter else None,
            spec.ssl_mode) if b]
        log(f"[ok] policy {label} ({','.join(srcs)}->wan1) -> {' + '.join(bits)}")


def policy_filter_state(fg):
    """[(policyid, name, srcintf, dstintf, utm_on, webfilter, applist, ssl)].

    Every internet-bound policy and whether it actually enforces anything.
    A profile that exists but is attached to no policy blocks nothing, and
    that failure is invisible from the profile screens.
    """
    out = []
    for p in fg.results("/api/v2/cmdb/firewall/policy"):
        dsts = [x["name"] for x in p.get("dstintf", [])]
        if not any(d.startswith("wan") for d in dsts):
            continue
        out.append({
            "policyid": p.get("policyid"),
            "name": p.get("name") or f"#{p.get('policyid')}",
            "src": [x["name"] for x in p.get("srcintf", [])],
            "dst": dsts,
            "utm": (p.get("utm-status") or "disable") == "enable",
            "webfilter": p.get("webfilter-profile") or "",
            "applist": p.get("application-list") or "",
            "ssl": p.get("ssl-ssh-profile") or "",
        })
    return out


def attach_filters(fg, ports, ssl_mode="deep-inspection", web=True, app=True,
                   log=_noop):
    """Turn filtering ON for the internet policies of `ports`. Nothing else.

    This is the step that was missing when a branch had a perfectly good web
    filter and application sensor that no policy referenced -- so nothing was
    ever inspected. It only touches the UTM fields of matching policies: no
    interfaces, no DHCP, no addresses, no profile contents.
    """
    touched, skipped = [], []
    for p in policy_filter_state(fg):
        if not any(s in ports for s in p["src"]):
            skipped.append(p)
            continue
        body = {
            "utm-status": "enable",
            "ssl-ssh-profile": ssl_mode,
            "profile-protocol-options": "default",
            "logtraffic": "all",
            "webfilter-profile": WEBFILTER_PROFILE if web else "",
            "application-list": APPLIST_NAME if app else "",
        }
        fg.call("PUT", f"/api/v2/cmdb/firewall/policy/{p['policyid']}", body)
        touched.append(p)
        bits = [b for b in (WEBFILTER_PROFILE if web else None,
                            APPLIST_NAME if app else None, ssl_mode) if b]
        log(f"[ok] policy {p['name']} ({','.join(p['src'])}->"
            f"{','.join(p['dst'])}) -> {' + '.join(bits)}")

    for p in skipped:
        why = ("Guest -- left unfiltered on purpose"
               if any(s.endswith("3") for s in p["src"]) else "not in scope")
        log(f"[skip] policy {p['name']} ({','.join(p['src'])}) -- {why}")

    if not touched:
        raise FortiGateError(
            f"No internet policy has any of {ports} as its source, so there "
            f"was nothing to attach filtering to. Check the port names on the "
            f"Networks tab against the policies on the firewall.")

    # Read back: the whole point is that 'configured' and 'enforced' differ.
    after = {p["policyid"]: p for p in policy_filter_state(fg)}
    for p in touched:
        got = after.get(p["policyid"], {})
        if web and got.get("webfilter") != WEBFILTER_PROFILE:
            raise FortiGateError(
                f"policy {p['name']} did not take the web filter: reports "
                f"{got.get('webfilter')!r}.")
        if app and got.get("applist") != APPLIST_NAME:
            raise FortiGateError(
                f"policy {p['name']} did not take the application sensor: "
                f"reports {got.get('applist')!r}.")
    return touched


def clear_filters(fg, spec, log=_noop):
    """Turn UTM back off on the in-scope policies (leaves the objects intact)."""
    scope = spec.filtered_ports()
    for p in fg.results("/api/v2/cmdb/firewall/policy"):
        srcs = [x["name"] for x in p.get("srcintf", [])]
        if "wan1" not in [x["name"] for x in p.get("dstintf", [])]:
            continue
        if not any(s in scope for s in srcs):
            continue
        fg.call("PUT", f"/api/v2/cmdb/firewall/policy/{p.get('policyid')}", {
            "utm-status": "disable", "ssl-ssh-profile": "no-inspection",
            "webfilter-profile": "", "application-list": "",
        })
        log(f"[ok] policy {p.get('name') or p.get('policyid')} -> filtering removed")


def verify_filters(fg, spec):
    """Return (label, passed, detail) tuples for the UTM objects."""
    out = []

    def check(label, actual, expected):
        out.append((label, actual == expected,
                    f"{actual!r}" if actual == expected
                    else f"got {actual!r}, want {expected!r}"))

    if spec.web_filter:
        try:
            uf = fg.results(f"/api/v2/cmdb/webfilter/urlfilter/{URLFILTER_ID}")[0]
        except (FortiGateError, IndexError):
            out.append(("URL filter table exists", False, "MISSING"))
        else:
            check("URL filter name", uf.get("name"), URLFILTER_NAME)
            rows = uf.get("entries", [])
            want = sorted(u for u, _ in spec_urls(spec))
            check("blocked sites", sorted(e.get("url") for e in blocked_only(rows)),
                  want)
            check("WhatsApp allowed",
                  sorted(e.get("url") for e in rows if e.get("action") == "allow"),
                  sorted(ALLOW_URLS))
            check("URL entries all enabled",
                  sorted({e.get("status") for e in rows}), ["enable"])
        try:
            wf = fg.results(f"/api/v2/cmdb/webfilter/profile/{WEBFILTER_PROFILE}")[0]
            check("web filter -> URL table",
                  (wf.get("web") or {}).get("urlfilter-table"), URLFILTER_ID)
        except (FortiGateError, IndexError):
            out.append(("web filter profile exists", False, "MISSING"))

    if spec.app_filter:
        try:
            al = fg.results(f"/api/v2/cmdb/application/list/{APPLIST_NAME}")[0]
        except (FortiGateError, IndexError):
            out.append(("application list exists", False, "MISSING"))
        else:
            cats = sorted(c["id"] for e in al.get("entries", [])
                          for c in e.get("category", []))
            check("blocked app categories", cats,
                  sorted(cid for cid, _ in spec_categories(spec)))
            check("app entries all block",
                  sorted({e.get("action") for e in al.get("entries", [])}), ["block"])
            check("other apps pass", al.get("other-application-action"), "pass")
    return out


def licence_state(fg):
    """FortiGuard entitlement -- 'pending' until the unit is registered online."""
    try:
        res = fg.get("/api/v2/monitor/license/status").get("results") or {}
    except FortiGateError:
        return {}
    return {k: (v.get("status") if isinstance(v, dict) else v)
            for k, v in res.items()
            if k in ("forticare", "appctrl", "web_filtering", "ips", "antivirus")}
