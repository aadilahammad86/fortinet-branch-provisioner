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

# Facebook and YouTube, plus the CDN domains those two services stream from --
# without those the sites partly load. `wildcard` covers subdomains; the bare
# domain needs its own `simple` entry.
DEFAULT_URLS = [
    ("facebook.com", "simple"),
    ("*.facebook.com", "wildcard"),
    ("*.fbcdn.net", "wildcard"),          # Facebook static content / images
    ("youtube.com", "simple"),
    ("*.youtube.com", "wildcard"),
    ("youtu.be", "simple"),
    ("*.googlevideo.com", "wildcard"),    # YouTube video streams
]

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


def apply_filters(fg, spec, log=_noop):
    """Create the UTM objects and attach them to the in-scope policies."""
    scope = spec.filtered_ports()

    if spec.web_filter:
        urls = spec_urls(spec)
        entries = [
            {"id": i, "url": url, "type": typ, "action": "block",
             "status": "enable", "exempt": "av"}
            for i, (url, typ) in enumerate(urls, start=1)
        ]
        state = fg.upsert("/api/v2/cmdb/webfilter/urlfilter", URLFILTER_ID, {
            "id": URLFILTER_ID, "name": URLFILTER_NAME,
            "comment": "Branch standard blocked sites",
            "entries": entries,
        })
        log(f"[ok] URL filter table #{URLFILTER_ID} '{URLFILTER_NAME}' "
            f"({len(entries)} entries) {state}")

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
            want = sorted(u for u, _ in spec_urls(spec))
            check("URL filter entries", sorted(e.get("url") for e in uf.get("entries", [])),
                  want)
            check("URL entries all block",
                  sorted({e.get("action") for e in uf.get("entries", [])}), ["block"])
            check("URL entries all enabled",
                  sorted({e.get("status") for e in uf.get("entries", [])}), ["enable"])
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
