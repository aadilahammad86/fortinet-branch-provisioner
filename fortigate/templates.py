"""Saved branch templates -- one JSON file per branch, kept in a folder.

A branch template is simply a `BranchSpec` written to disk under a name the
operator chooses ("Al Ain", "Branch 07"). Pick the name from the list and
every field on the form -- or every default on the command line -- is filled
in from it, so the same branch can be re-provisioned identically after a
factory reset, a hardware swap or a year later by someone else.

Rules that matter:
  * Passwords are NEVER written. `pppoe_pass` and the admin password stay out
    of the file; templates get copied around and mailed. The operator re-types
    them, exactly as with the old profile files.
  * The folder lives next to the .exe (see `app_dir()` in branch_gui.py), not
    inside the PyInstaller bundle -- a one-file bundle is a temp folder that
    Windows deletes on exit, which would silently eat every saved branch.
  * Loading is forgiving: unknown keys are ignored and missing keys keep the
    BranchSpec default, so a template saved by an older build still loads.
"""
import json
import re
from dataclasses import fields as dataclass_fields
from datetime import datetime
from pathlib import Path

from .branch import BranchSpec

FOLDER = "branches"                 # sub-folder of the app directory
SUFFIX = ".branch.json"
SECRET_KEYS = ("pppoe_pass", "password", "admin_pass")

#: Extra (non-BranchSpec) keys a template may carry for the front ends.
EXTRA_KEYS = ("host", "backup_dir", "notes")

_SPEC_KEYS = {f.name for f in dataclass_fields(BranchSpec)}


class TemplateError(Exception):
    """A template could not be read, written or removed."""


# =========================================================================
#  Locations and names
# =========================================================================
def templates_dir(root):
    """The branches folder under `root`, created on demand."""
    d = Path(root) / FOLDER
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise TemplateError(f"Cannot create {d}: {e}")
    return d


def slug(name):
    """A safe file stem for a branch name ('Al Ain / HQ' -> 'al-ain-hq')."""
    s = re.sub(r"[^A-Za-z0-9]+", "-", str(name).strip().lower()).strip("-")
    return s or "branch"


def path_for(root, name):
    return templates_dir(root) / (slug(name) + SUFFIX)


# =========================================================================
#  Spec <-> plain dict
# =========================================================================
def to_dict(spec, **extra):
    """A JSON-ready dict of a BranchSpec, with every secret stripped."""
    data = {k: v for k, v in vars(spec).items() if k not in SECRET_KEYS}
    for k, v in extra.items():
        if k in EXTRA_KEYS and v not in (None, ""):
            data[k] = v
    return data


def to_spec(data):
    """Build a BranchSpec from a template dict, ignoring unknown keys."""
    known = {k: v for k, v in data.items()
             if k in _SPEC_KEYS and k not in SECRET_KEYS}
    return BranchSpec(**known)


# =========================================================================
#  Library operations
# =========================================================================
def list_names(root):
    """Saved branch names, sorted for display. Unreadable files are skipped."""
    out = []
    for p in sorted(templates_dir(root).glob("*" + SUFFIX)):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append(data.get("branch_name") or p.name[: -len(SUFFIX)])
    return sorted(out, key=str.lower)


def summaries(root):
    """[(name, one-line description)] for every saved branch."""
    rows = []
    for name in list_names(root):
        try:
            d = load(name, root)
        except TemplateError:
            continue
        bits = []
        if d.get("hostname"):
            bits.append(d["hostname"])
        if d.get("configure_lan") and d.get("lan_ip"):
            bits.append(f"LAN {d['lan_ip']}")
        if d.get("configure_staff") and d.get("staff_ip"):
            bits.append(f"Staff {d['staff_ip']}")
        if d.get("configure_guest") and d.get("guest_ip"):
            bits.append(f"Guest {d['guest_ip']}")
        rows.append((name, "  ".join(bits) or "(no networks enabled)"))
    return rows


def exists(name, root):
    return path_for(root, name).exists()


def save(name, spec, root, **extra):
    """Write (or overwrite) the template for `name`. Returns the file path."""
    name = str(name).strip()
    if not name:
        raise TemplateError("A branch name is required.")
    data = to_dict(spec, **extra)
    data["branch_name"] = name
    data["saved"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    p = path_for(root, name)
    try:
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as e:
        raise TemplateError(f"Could not save {p}: {e}")
    return p


def load(name, root):
    """Return the raw dict for `name` (front ends need the extra keys too)."""
    p = path_for(root, name)
    if not p.exists():
        raise TemplateError(
            f"No saved branch called '{name}' in {templates_dir(root)}.")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise TemplateError(f"Could not read {p}: {e}")


def load_spec(name, root):
    return to_spec(load(name, root))


def delete(name, root):
    p = path_for(root, name)
    try:
        p.unlink()
    except FileNotFoundError:
        raise TemplateError(f"No saved branch called '{name}'.")
    except OSError as e:
        raise TemplateError(f"Could not delete {p}: {e}")
    return p


def import_file(src, root, name=None):
    """Copy an exported template file into the library. Returns its name."""
    src = Path(src)
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise TemplateError(f"Could not read {src}: {e}")
    name = name or data.get("branch_name") or src.name.split(".")[0]
    save(name, to_spec(data), root,
         **{k: data[k] for k in EXTRA_KEYS if k in data})
    return name


def export_file(name, dest, root):
    """Write a copy of a saved branch to `dest` for sending to someone."""
    data = load(name, root)
    dest = Path(dest)
    try:
        dest.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as e:
        raise TemplateError(f"Could not write {dest}: {e}")
    return dest
