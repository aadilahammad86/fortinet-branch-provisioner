"""Remembered connections: type a firewall's address, get its credentials.

An engineer looks after a dozen branches, each with its own admin password, and
retyping them from a note is both slow and the reason passwords end up written
on a note. This stores them per address, on this laptop only.

HOW THE PASSWORD IS PROTECTED

On Windows the password is encrypted with **DPAPI** (`CryptProtectData`), which
ties it to the signed-in Windows account on this machine. Copy the file to
another laptop, or open it as another user, and it will not decrypt. That is
the point: it is a convenience for one engineer's own machine, not a portable
password store.

Where DPAPI is unavailable the entry is stored base64-encoded and clearly
marked `plain` -- encoding is not encryption, and the file says so.

WHAT IT IS NOT

Not part of a saved branch template. Branch files get emailed between laptops
and must never contain a password; this file must never leave the laptop. It
lives beside the .exe, is git-ignored, and holds nothing else.
"""
import base64
import ctypes
import json
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

FILENAME = "connections.json"


class ConnectionError_(Exception):
    """The store could not be read or written."""


# =========================================================================
#  Windows DPAPI, through ctypes so there is no third-party dependency
# =========================================================================
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _to_blob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _from_blob(blob):
    out = ctypes.string_at(blob.pbData, blob.cbData)
    ctypes.windll.kernel32.LocalFree(blob.pbData)
    return out


def dpapi_available():
    try:
        return hasattr(ctypes, "windll") and bool(ctypes.windll.crypt32)
    except (AttributeError, OSError):
        return False


def _protect(text):
    """DPAPI-encrypt, or raise so the caller can fall back."""
    blob_in, _keep = _to_blob(text.encode("utf-8"))
    blob_out = _Blob()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), "FortiGate Branch Provisioner",
        None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError("CryptProtectData failed")
    return base64.b64encode(_from_blob(blob_out)).decode("ascii")


def _unprotect(token):
    blob_in, _keep = _to_blob(base64.b64decode(token))
    blob_out = _Blob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0,
        ctypes.byref(blob_out))
    if not ok:
        raise OSError("CryptUnprotectData failed")
    return _from_blob(blob_out).decode("utf-8")


# =========================================================================
#  The store
# =========================================================================
def store_path(root):
    return Path(root) / FILENAME


def _load(root):
    p = store_path(root)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # A corrupt store must not stop anyone connecting -- it is a
        # convenience, so treat it as empty rather than fatal.
        return {}
    return data if isinstance(data, dict) else {}


def _save(root, data):
    p = store_path(root)
    try:
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as e:
        raise ConnectionError_(f"Could not write {p}: {e}")
    return p


def hosts(root):
    """Remembered addresses, for the address drop-down."""
    return sorted(_load(root).keys())


def remember(root, host, user, password):
    """Store the credentials for one firewall address."""
    host = (host or "").strip()
    if not host:
        raise ConnectionError_("No device address to remember.")
    data = _load(root)
    entry = {"user": (user or "").strip(),
             "saved": datetime.now().strftime("%Y-%m-%d %H:%M")}
    if password and dpapi_available():
        try:
            entry["password"] = _protect(password)
            entry["enc"] = "dpapi"
        except OSError:
            entry["password"] = base64.b64encode(
                password.encode("utf-8")).decode("ascii")
            entry["enc"] = "plain"
    elif password:
        entry["password"] = base64.b64encode(
            password.encode("utf-8")).decode("ascii")
        entry["enc"] = "plain"
    else:
        entry["password"], entry["enc"] = "", "none"
    data[host] = entry
    _save(root, data)
    return entry["enc"]


def lookup(root, host):
    """(user, password, how) for an address, or None if it is not remembered.

    `how` is 'dpapi', 'plain' or 'none'. A password that will not decrypt --
    a store copied from another machine or another Windows account -- comes
    back empty rather than raising, so the operator simply types it again.
    """
    entry = _load(root).get((host or "").strip())
    if not entry:
        return None
    how = entry.get("enc", "none")
    token = entry.get("password") or ""
    password = ""
    if token and how == "dpapi":
        try:
            password = _unprotect(token)
        except (OSError, ValueError):
            password, how = "", "locked"
    elif token:
        try:
            password = base64.b64decode(token).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            password = ""
    return entry.get("user", ""), password, how


def forget(root, host):
    data = _load(root)
    if (host or "").strip() in data:
        del data[host.strip()]
        _save(root, data)
        return True
    return False
