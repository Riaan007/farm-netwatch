"""Hikvision ISAPI client — the one door new code uses to talk to a camera or NVR.

Why it exists: Hikvision locks an account (newer firmware: the address) after a
few wrong passwords — 7 on the home cameras — and Netwatch talks to devices on
its own schedule, so a wrong saved password must never be tried twice. Every
request with a login goes through `Client`: the first 401 marks that device +
login as refused (remembered by the keyed fingerprint of the login from
creds.py, never the password), and from then on every request for it fails at
once with LoginRefused without touching the network. Saving a different login
allows exactly one new try; so does a login test (credtest.py) that passes. A
login test the device refused counts as a refusal here too.

Device XML is read with `parse()`, which refuses any DOCTYPE/ENTITY: ISAPI never
sends one, and a device on a client LAN is not trusted.
"""
import threading
import time
import xml.etree.ElementTree as ET

import requests
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

import creds

requests.packages.urllib3.disable_warnings()      # devices use self-signed certificates

_lock = threading.Lock()
_seen = {}          # device key -> {"fp", "ok": bool, "ts"} — the last answer to a login


class LoginRefused(Exception):
    """The device said no to this login (now, or before — see refused())."""


class Unreachable(Exception):
    """No HTTP answer from the device."""


class TooBig(Exception):
    """The answer was larger than the caller allows."""


def refused(key, username, password, registry=None):
    """True when the newest word on this exact login is "refused": a 401 seen by
    this module, or a login test the device failed (registry cred_test)."""
    fp = creds.fingerprint(username or "", password or "")
    words = []
    with _lock:
        s = _seen.get(key)
    if s and s["fp"] == fp:
        words.append((s["ts"], s["ok"]))
    ct = ((registry or {}).get(key) or {}).get("cred_test") or {}
    if ct.get("fp") == fp and ct.get("result") in ("ok", "auth_failed"):
        words.append((ct.get("ts") or 0, ct["result"] == "ok"))
    return bool(words) and not max(words)[1]


def _note(key, fp, ok):
    with _lock:
        _seen[key] = {"fp": fp, "ok": ok, "ts": time.time()}


def forget(key):
    with _lock:
        _seen.pop(key, None)


class Reply:
    __slots__ = ("status", "content", "headers")

    def __init__(self, status, content, headers):
        self.status, self.content, self.headers = status, content, headers

    @property
    def text(self):
        return self.content.decode("utf-8", "replace")

    @property
    def ok(self):
        return self.status == 200


class Client:
    """Requests to one device with its saved login. One session, so the digest
    nonce is reused (one round trip per request after the first)."""

    def __init__(self, key, ip, username, password, ports=None, registry=None, timeout=(4, 12)):
        self.key, self.ip = key, ip
        self.username, self.password = username or "", password or ""
        self.registry = registry
        self.timeout = timeout
        self.fp = creds.fingerprint(self.username, self.password)
        ports = set(ports or ())
        # Plain http unless the device only answers https (self-signed, so not verified).
        self.base = f"https://{ip}" if (443 in ports and 80 not in ports) else f"http://{ip}"
        self.session = requests.Session()
        self.session.verify = False
        self.session.auth = HTTPDigestAuth(self.username, self.password)
        self._basic = False

    def get(self, path, params=None, max_bytes=8 << 20):
        return self.request("GET", path, params=params, max_bytes=max_bytes)

    def request(self, method, path, params=None, data=None, headers=None, max_bytes=8 << 20):
        if refused(self.key, self.username, self.password, self.registry):
            raise LoginRefused("this device refused the saved login before; fix the login first")
        try:
            r = self._send(method, path, params, data, headers)
            if (r.status_code == 401 and not self._basic and not r.request.headers.get("Authorization")
                    and "basic" in r.headers.get("WWW-Authenticate", "").lower()):
                # The device asked for Basic, so Digest sent no password yet: this is still the first try.
                r.close()
                self._basic = True
                self.session.auth = HTTPBasicAuth(self.username, self.password)
                r = self._send(method, path, params, data, headers)
            if r.status_code == 401:
                r.close()
                if r.request.headers.get("Authorization"):
                    _note(self.key, self.fp, False)
                    raise LoginRefused("the device refused the saved login")
                raise LoginRefused("the device asks for a login this client can't give")
            if r.request.headers.get("Authorization"):
                _note(self.key, self.fp, True)
            body = self._read(r, max_bytes)
        except requests.RequestException as e:
            raise Unreachable(e.__class__.__name__) from None
        return Reply(r.status_code, body, r.headers)

    def _send(self, method, path, params, data, headers):
        return self.session.request(method, self.base + path, params=params, data=data,
                                    headers=headers, timeout=self.timeout, stream=True)

    @staticmethod
    def _read(r, max_bytes):
        chunks, total = [], 0
        try:
            for c in r.iter_content(65536):
                total += len(c)
                if total > max_bytes:
                    raise TooBig(f"answer larger than {max_bytes // 1024} KB")
                chunks.append(c)
        finally:
            r.close()
        return b"".join(chunks)


# ---- XML ----------------------------------------------------------------------
def parse(content):
    """The root element of an ISAPI XML answer, or None. Refuses DOCTYPE/ENTITY."""
    if isinstance(content, bytes):
        content = content.decode("utf-8", "replace")
    head = content[:4096].lower()
    if "<!doctype" in head or "<!entity" in content.lower():
        return None
    try:
        return ET.fromstring(content)
    except ET.ParseError:
        return None


def tag(el):
    return el.tag.rsplit("}", 1)[-1]


def find(root, name):
    """First element below root (any depth, any namespace) called `name`."""
    if root is None:
        return None
    for el in root.iter():
        if tag(el) == name:
            return el
    return None


def text(root, name, default=""):
    el = find(root, name)
    return el.text.strip() if el is not None and el.text and el.text.strip() else default


def child_text(el, name, default=""):
    """Text of el's DIRECT child `name` (namespace-agnostic)."""
    for c in el if el is not None else ():
        if tag(c) == name and c.text and c.text.strip():
            return c.text.strip()
    return default
