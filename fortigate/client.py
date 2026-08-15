"""FortiGate REST client -- login, CSRF, and request plumbing.

This is the single copy of the connection logic that used to be duplicated in
every script under scripts/.

Gotchas encoded here, learned the hard way against a real FortiGate 60F:

  * You must GET / first to prime the session before POSTing /logincheck.
  * A leading '1' in the /logincheck reply means success.
  * The CSRF cookie is named `ccsrftoken_<port>_<hex>`, NOT plain `ccsrftoken`
    -- match by prefix. It must be sent as the X-CSRFTOKEN header on writes,
    along with a Referer header, or writes are rejected.
  * The device uses a self-signed certificate, so TLS verification is off.
  * A cmdb PUT can return `success` while silently ignoring fields it cannot
    apply (see branch.set_lan_interface). Read values back; do not trust the
    status code alone.
"""
import ssl
import json
import socket
import urllib.parse
import urllib.request
import urllib.error
import http.cookiejar
from datetime import datetime
from pathlib import Path

DEFAULTS = {
    "FGT_HOST": "192.168.1.99",
    "FGT_USER": "admin",
    "FGT_PASSWORD": "",
    "FGT_PPPOE_USER": "",
    "FGT_PPPOE_PASS": "",
}


class FortiGateError(Exception):
    """Any failure talking to the device."""


class LoginError(FortiGateError):
    """Credentials rejected, or the device was unreachable."""


def load_env(env_path=None):
    """Read connection settings from a .env file, falling back to defaults."""
    cfg = dict(DEFAULTS)
    path = Path(env_path) if env_path else Path(__file__).resolve().parent.parent / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


class FortiGate:
    """A logged-in session against one FortiGate."""

    def __init__(self, host, user, password, timeout=30):
        self.host = host
        self.timeout = timeout
        self.base = f"https://{host}"
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=ctx),
        )
        self.csrf = ""
        self._login(user, password)

    # -- connection ------------------------------------------------------
    def _login(self, user, password):
        try:
            self.opener.open(self.base + "/", timeout=self.timeout).read()
            body = urllib.parse.urlencode(
                {"username": user, "secretkey": password, "ajax": "1"}
            ).encode()
            resp = self.opener.open(
                self.base + "/logincheck", data=body, timeout=self.timeout
            ).read().decode(errors="replace")
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            raise LoginError(
                f"Cannot reach {self.host}: {type(e).__name__}. Check the cable, "
                f"your IP address, and that the host is correct."
            ) from e
        if not resp.lstrip().startswith("1"):
            raise LoginError(
                f"Login rejected by {self.host}. Check the username and password."
            )
        # Cookie is ccsrftoken_<port>_<hex>; the last one set is the live token.
        for c in self.jar:
            if c.name.lower().startswith("ccsrftoken"):
                self.csrf = c.value.strip('"')

    # -- requests --------------------------------------------------------
    def call(self, method, path, payload=None, raw=False):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("X-CSRFTOKEN", self.csrf)
        req.add_header("Referer", self.base + "/")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            body = self.opener.open(req, timeout=self.timeout).read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:400]
            raise FortiGateError(f"{method} {path} -> HTTP {e.code}: {detail}") from e
        return body if raw else json.loads(body.decode(errors="replace"))

    def get(self, path):
        return self.call("GET", path)

    def results(self, path):
        """GET and return the `results` list/dict, or [] if absent."""
        return self.get(path).get("results", [])

    def exists(self, path):
        try:
            self.get(path)
            return True
        except FortiGateError as e:
            if "HTTP 404" in str(e):
                return False
            raise

    def upsert(self, collection, key, body):
        """PUT if the object exists, POST if it does not. Returns 'updated'/'created'."""
        quoted = urllib.parse.quote(str(key))
        if self.exists(f"{collection}/{quoted}"):
            self.call("PUT", f"{collection}/{quoted}", body)
            return "updated"
        self.call("POST", collection, body)
        return "created"

    # -- device info -----------------------------------------------------
    def status(self):
        """Model, serial, firmware, hostname -- used by Test Connection."""
        st = self.get("/api/v2/monitor/system/status")
        res = st.get("results", {}) or {}
        return {
            "hostname": res.get("hostname", "?"),
            "serial": st.get("serial", "?"),
            "version": st.get("version", "?"),
            "model": res.get("model_name") or st.get("model", "?"),
        }

    def backup(self, outdir):
        """Download the full running config. Returns the written Path."""
        st = self.status()
        blob = self.call(
            "GET", "/api/v2/monitor/system/config/backup?scope=global", raw=True
        )
        outdir = Path(outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = outdir / f"{st['serial']}-{stamp}.conf"
        out.write_bytes(blob)
        return out, st, len(blob), blob.decode(errors="replace").count("\n")
