"""Application control: the signature database and per-application blocking.

Blocking by CATEGORY is coarse, and the categories are not where you would
guess. Telegram, on this firmware, is in **Collaboration** -- not Social.Media
-- so a sensor blocking Social.Media never touches it. That is exactly how a
branch ends up with Telegram working on the WiFi while everyone believes it is
blocked.

So this module does two things the category list cannot:

  * reads the device's OWN signature database (2,414 signatures on a 60F
    running 7.4.9) so you can see which category an application really is in,
    rather than trusting documentation; and
  * writes per-application overrides -- `config application list / entries /
    set application <id>` -- so a single app can be blocked without blocking
    the whole category it happens to live in.

Two facts worth keeping in mind while using it:

  * A phone app is only ever caught by application control. The URL filter
    matches hostnames in HTTP/HTTPS; the Telegram app speaks MTProto straight
    to Telegram's servers and never presents one.
  * Signatures only update on a registered unit. While FortiGuard entitlements
    read `pending`, this database is frozen at whatever shipped with the
    firmware, and messaging apps change constantly.
"""
from .client import FortiGateError
from .utm import ALL_CATEGORIES

# The signature table has moved between builds; try each and use what answers.
SIG_PATHS = [
    "/api/v2/cmdb/application/name",
    "/api/v2/monitor/application/name",
    "/api/v2/cmdb/application/custom",
]

# Applications people actually mean when they say "block messaging". Matched
# against the device's own signature names, so a name that does not exist on
# this firmware is simply reported as missing rather than silently ignored.
# WhatsApp is deliberately absent -- the business runs on it.
MESSAGING_APPS = [
    "Telegram", "IMO", "Botim", "Line", "Viber", "WeChat", "Signal",
    "Facebook.Messenger", "Snapchat", "Discord", "Skype", "KakaoTalk",
    "Zalo", "Hike", "Tango",
]

SOCIAL_APPS = [
    "Facebook", "Instagram", "Twitter", "TikTok", "Reddit", "Pinterest",
    "Tumblr", "LinkedIn", "Threads",
]


# id -> name, so the browser reads like the FortiGate's own screen. Verified
# against this 60F: 28 really is Collaboration (299 signatures), which is
# where Telegram lives.
CATEGORY_NAMES = dict(ALL_CATEGORIES)


def _noop(_msg):
    pass


def _first(row, *keys):
    for k in keys:
        if row.get(k) not in (None, ""):
            return row[k]
    return ""


def _stars(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def load_signatures(fg):
    """Every signature the device knows: normalised dicts, sorted by name.

    Field names differ between the cmdb table and the monitor endpoint, so
    everything is mapped onto one shape: id, name, category, category_id,
    technology, popularity, risk.
    """
    rows, used = [], None
    for path in SIG_PATHS:
        try:
            got = fg.results(path)
        except FortiGateError:
            continue
        if got:
            rows, used = got, path
            break
    if not rows:
        raise FortiGateError(
            "Could not read the application signature database from this "
            "firewall. The GUI shows it under Security Profiles > Application "
            "Signatures; if that is empty too, the unit has no signature "
            "package loaded.")

    out = []
    for r in rows:
        cat = _first(r, "category", "cat", "category-name")
        cat_id = _first(r, "cat-id", "category-id", "catid")
        if isinstance(cat, dict):
            cat_id = cat.get("id", cat_id)
            cat = cat.get("name", "")
        if isinstance(cat, int):
            cat_id, cat = cat, ""
        # This firmware returns the category as a bare id, so put the name
        # back: "Collaboration" is what an operator recognises, "28" is not.
        if not cat and str(cat_id) != "":
            try:
                cat = CATEGORY_NAMES.get(int(cat_id), f"category {cat_id}")
            except (TypeError, ValueError):
                cat = f"category {cat_id}"
        out.append({
            "id": _first(r, "id", "app-id"),
            "name": _first(r, "name", "app-name"),
            "category": str(cat or ""),
            "category_id": cat_id,
            "technology": _first(r, "technology"),
            "popularity": _stars(_first(r, "popularity")),
            "risk": _stars(_first(r, "risk", "weight")),
        })
    out.sort(key=lambda a: str(a["name"]).lower())
    return out, used


def categories(sigs):
    """[(name, count)] over the signatures actually present, most first."""
    counts = {}
    for s in sigs:
        key = s["category"] or (f"category {s['category_id']}"
                                if s["category_id"] != "" else "(none)")
        counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))


def search(sigs, needle="", category=""):
    """Filter by name substring and/or exact category name."""
    n = (needle or "").strip().lower()
    c = (category or "").strip().lower()
    out = []
    for s in sigs:
        if n and n not in str(s["name"]).lower():
            continue
        if c and c not in ("all", "all categories") \
                and str(s["category"]).lower() != c:
            continue
        out.append(s)
    return out


def resolve(sigs, names):
    """(found, missing) -- map signature names to the device's own ids.

    Matching is exact first, then case-insensitive, then 'starts with', so
    "Telegram" also picks up "Telegram_FileTransfer" style variants.
    """
    by_exact = {str(s["name"]).lower(): s for s in sigs}
    found, missing = [], []
    for want in names:
        w = want.lower()
        hit = by_exact.get(w)
        if hit:
            found.append(hit)
            continue
        prefixed = [s for s in sigs if str(s["name"]).lower().startswith(w)]
        if prefixed:
            found.extend(prefixed)
        else:
            missing.append(want)
    seen, unique = set(), []
    for s in found:
        if s["id"] not in seen:
            seen.add(s["id"])
            unique.append(s)
    return unique, missing


# =========================================================================
#  The sensor itself
# =========================================================================
def read_sensor(fg, sensor):
    """What the sensor blocks now: {'categories': [...], 'applications': [...]}."""
    try:
        al = fg.results(f"/api/v2/cmdb/application/list/{sensor}")[0]
    except (FortiGateError, IndexError):
        raise FortiGateError(
            f"No application sensor called '{sensor}' on this firewall. "
            f"Run a full Apply first, which creates it.")
    cats, apps = [], []
    for e in al.get("entries", []):
        if (e.get("action") or "") != "block":
            continue
        cats += [c["id"] for c in e.get("category", []) if "id" in c]
        apps += [a["id"] for a in e.get("application", []) if "id" in a]
    return {"categories": sorted(set(cats)), "applications": sorted(set(apps)),
            "other": al.get("other-application-action")}


def write_sensor(fg, sensor, category_ids, app_ids, log=_noop):
    """Rewrite the sensor: per-application blocks first, then categories.

    Order matters -- the FortiGate walks the entries and takes the first
    match, so the specific applications have to sit above the broad category
    rules for an override to mean anything.
    """
    entries, i = [], 1
    app_ids = sorted({int(a) for a in app_ids if str(a).strip() != ""})
    if app_ids:
        entries.append({"id": i, "action": "block", "log": "enable",
                        "application": [{"id": a} for a in app_ids]})
        i += 1
    for cid in sorted({int(c) for c in category_ids}):
        entries.append({"id": i, "action": "block", "log": "enable",
                        "category": [{"id": cid}]})
        i += 1

    state = fg.upsert("/api/v2/cmdb/application/list", sensor, {
        "name": sensor,
        "comment": "Branch standard application control",
        "entries": entries,
        "other-application-action": "pass",
        "other-application-log": "disable",
        "unknown-application-action": "pass",
        "app-replacemsg": "enable",
    })
    back = read_sensor(fg, sensor)
    missing = set(app_ids) - set(back["applications"])
    if missing:
        raise FortiGateError(
            f"The sensor was accepted but {len(missing)} application(s) did "
            f"not stick: {sorted(missing)}. They may not exist in this "
            f"firmware's signature database.")
    log(f"[ok] sensor '{sensor}' {state}: {len(app_ids)} individual "
        f"application(s) + {len(category_ids)} categor(y/ies) blocked")
    return back


def block_apps(fg, sensor, sigs, names, log=_noop):
    """Add named applications to the sensor's block list, keeping categories."""
    found, missing = resolve(sigs, names)
    if missing:
        log(f"[!] not in this firmware's signature database: "
            f"{', '.join(missing)}")
    if not found:
        raise FortiGateError("None of those applications exist on this device.")
    current = read_sensor(fg, sensor)
    ids = set(current["applications"]) | {int(s["id"]) for s in found}
    for s in found:
        log(f"[..] {s['name']} (id {s['id']}, category {s['category']})")
    return write_sensor(fg, sensor, current["categories"], ids, log)


def sensor_policies(fg, sensor):
    """Enabled policies that actually enforce this sensor."""
    return [p.get("name") or f"#{p.get('policyid')}"
            for p in fg.results("/api/v2/cmdb/firewall/policy")
            if p.get("application-list") == sensor
            and p.get("utm-status") == "enable"]
