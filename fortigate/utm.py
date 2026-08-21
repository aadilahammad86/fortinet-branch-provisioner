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


def update_urls(fg, urls, log=_noop):
    """Rewrite ONLY the blocked-site list on a firewall that is already set up.

    Touches nothing else -- no interfaces, no policies, no application control.
    The profile is already attached to the policies, so the new list takes
    effect the moment the table is written. Safe to run against a live branch.
    """
    entries = url_entries(urls)
    state = fg.upsert("/api/v2/cmdb/webfilter/urlfilter", URLFILTER_ID, {
        "id": URLFILTER_ID, "name": URLFILTER_NAME,
        "comment": "Branch standard blocked sites",
        "entries": entries,
    })
    got = fg.results(f"/api/v2/cmdb/webfilter/urlfilter/{URLFILTER_ID}")[0]
    live = got.get("entries", [])
    if len(live) != len(entries):
        raise FortiGateError(
            f"URL list did not stick: sent {len(entries)} entries, the device "
            f"reports {len(live)}.")
    log(f"[ok] blocked-site list {state}: {len(blocked_only(live))} sites blocked, "
        f"{len(ALLOW_URLS)} WhatsApp entries allowed")

    # The profile must exist and point at this table, or the list is inert.
    try:
        wf = fg.results(f"/api/v2/cmdb/webfilter/profile/{WEBFILTER_PROFILE}")[0]
    except (FortiGateError, IndexError):
        raise FortiGateError(
            f"No web filter profile '{WEBFILTER_PROFILE}' on this firewall. "
            f"The list was saved but nothing uses it -- run a full Apply first.")
    if (wf.get("web") or {}).get("urlfilter-table") != URLFILTER_ID:
        fg.call("PUT", f"/api/v2/cmdb/webfilter/profile/{WEBFILTER_PROFILE}",
                {"web": {"urlfilter-table": URLFILTER_ID}})
        log(f"[ok] profile '{WEBFILTER_PROFILE}' repointed at table "
            f"#{URLFILTER_ID}")

    # Report, do not touch: which policies actually enforce this.
    using = [p.get("name") or f"#{p.get('policyid')}"
             for p in fg.results("/api/v2/cmdb/firewall/policy")
             if p.get("webfilter-profile") == WEBFILTER_PROFILE
             and p.get("utm-status") == "enable"]
    if using:
        log(f"[ok] live on {len(using)} polic{'y' if len(using) == 1 else 'ies'}: "
            f"{', '.join(using)}")
    else:
        log("[!] no policy currently uses this web filter -- the list is saved "
            "but nothing is being blocked. Run a full Apply to attach it.", )
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
