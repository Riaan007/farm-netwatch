"""Site login: who may read saved device passwords and change this Pi's settings.

Two kinds of caller get in:

  * a PERSON with the site password — Flask session cookie, 12 h, per-site
    cookie name (several sites are proxied on one hub host, and browser cookies
    ignore the port, so a shared name would log people out of each other);
  * the HUB — header `X-Netwatch-Hub-Key`, compared against a SHA-256 stored
    here (the plaintext key only lives in the hub's hub.json).

Everything else on the API stays open (dashboard, device list, scans), so the
hub's heartbeat/devices polling works with or without a key.

Getting the first secrets in, without trust-on-first-use from the farm LAN:

  * Hub key — `POST /api/auth/claim-hub` is accepted ONCE, while no key is set,
    and only from the hub's own VPN address (10.8.0.1, override with
    NETWATCH_HUB_IP) arriving without proxy headers. The reply to a spoofed
    source would route into wg0, so a LAN host cannot complete the handshake.
    Rotating needs the current key; resetting needs shell on the Pi (CLI below).
  * Site password — set by the hub (hub key) or, once logged in, by a person.
    A blank Pi has NO password and nobody on the LAN can set one.

State lives in /data/auth.json. It is deliberately NOT part of the config
export bundle: a restored Pi is claimed again by its hub.

CLI (on the Pi):  docker exec -it netwatch python siteauth.py set-password
                  docker exec -it netwatch python siteauth.py reset-hub-key
                  docker exec -i  netwatch python siteauth.py set-hub-key < keyfile
"""
import functools
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time

from werkzeug.security import check_password_hash, generate_password_hash

DATA_DIR = os.environ.get("NETWATCH_DATA", "/data")
AUTH_PATH = os.path.join(DATA_DIR, "auth.json")
SESSION_KEY_PATH = os.path.join(DATA_DIR, "session.key")
HUB_IP = os.environ.get("NETWATCH_HUB_IP", "10.8.0.1").strip()
HEADER = "X-Netwatch-Hub-Key"
SESSION_HOURS = 12
MIN_PASSWORD = 8

_lock = threading.Lock()
_fails = {}                      # ip -> [timestamps of recent failed logins]
FAIL_WINDOW_S, FAIL_MAX = 300, 5


# ---- storage ----------------------------------------------------------------
def _load():
    try:
        with open(AUTH_PATH) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = AUTH_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, AUTH_PATH)


def _update(**kv):
    with _lock:
        d = _load()
        d.update(kv)
        _save(d)
        return d


def session_secret():
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(SESSION_KEY_PATH, "rb") as f:
            key = f.read()
        if len(key) >= 32:
            return key
    except OSError:
        pass
    key = os.urandom(32)
    with open(SESSION_KEY_PATH, "wb") as f:
        f.write(key)
    try:
        os.chmod(SESSION_KEY_PATH, 0o600)
    except OSError:
        pass
    return key


def cookie_name():
    """Unique per install — see the module docstring for why."""
    return "nw_site_" + hashlib.sha256(b"cookie" + session_secret()).hexdigest()[:10]


def _digest(key):
    return hashlib.sha256(("netwatch-hub:" + key).encode()).hexdigest()


# ---- state ------------------------------------------------------------------
def password_set():
    return bool(_load().get("password_hash"))


def hub_key_set():
    return bool(_load().get("hub_key_sha256"))


def set_password(password):
    if len(password or "") < MIN_PASSWORD:
        raise ValueError(f"Password must be at least {MIN_PASSWORD} characters")
    # A new epoch signs every existing session out.
    _update(password_hash=generate_password_hash(password),
            epoch=secrets.token_hex(8), password_changed=int(time.time()))


def set_hub_key(key):
    key = (key or "").strip()
    if len(key) < 32:
        raise ValueError("Hub key must be at least 32 characters")
    _update(hub_key_sha256=_digest(key), hub_key_set_at=int(time.time()))


def clear_hub_key():
    _update(hub_key_sha256="", hub_key_set_at=0)


# ---- request checks ---------------------------------------------------------
def hub_ok(req):
    """True when the request carries the hub key this Pi was claimed with."""
    stored = _load().get("hub_key_sha256")
    sent = req.headers.get(HEADER, "")
    return bool(stored and sent) and hmac.compare_digest(stored, _digest(sent))


def user_ok(sess):
    d = _load()
    return bool(d.get("password_hash")) and bool(sess.get("site_auth")) \
        and hmac.compare_digest(str(sess.get("site_auth")), str(d.get("epoch", "")))


def allowed(req, sess):
    return hub_ok(req) or user_ok(sess)


def state(req, sess):
    """Safe to show anyone: booleans only."""
    return {"password_set": password_set(), "hub_key_set": hub_key_set(),
            "logged_in": user_ok(sess), "hub": hub_ok(req)}


def _denied(req, sess):
    from flask import jsonify
    body = {"ok": False, "error": "auth_required", "password_set": password_set()}
    body["message"] = ("Log in with this Pi's password." if body["password_set"] else
                       "This Pi has no password yet. Set one from the hub (site page → "
                       "Pi password) or on the Pi: docker exec -it netwatch python "
                       "siteauth.py set-password")
    return jsonify(body), 401


def required(fn):
    """Route decorator: a logged-in person or the hub."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        from flask import request, session
        if not allowed(request, session):
            return _denied(request, session)
        return fn(*a, **kw)
    return wrapper


def required_if(predicate):
    """Like `required`, but only while predicate() is true (e.g. first-run
    setup stays open until the Pi is configured)."""
    def deco(fn):
        guarded = required(fn)

        @functools.wraps(fn)
        def wrapper(*a, **kw):
            return guarded(*a, **kw) if predicate() else fn(*a, **kw)
        return wrapper
    return deco


# ---- login ------------------------------------------------------------------
def _throttled(ip):
    now = time.time()
    with _lock:
        recent = [t for t in _fails.get(ip, []) if now - t < FAIL_WINDOW_S]
        _fails[ip] = recent
        return len(recent) >= FAIL_MAX


def _fail(ip):
    with _lock:
        _fails.setdefault(ip, []).append(time.time())


def login(req, sess, password):
    """Returns (status_code, body)."""
    ip = req.remote_addr or "?"
    if _throttled(ip):
        return 429, {"ok": False, "error": "Too many attempts — wait 5 minutes."}
    d = _load()
    h = d.get("password_hash")
    if not h:
        return 403, {"ok": False, "error": "This Pi has no password yet — set it from the hub."}
    if not check_password_hash(h, password or ""):
        _fail(ip)
        time.sleep(0.5)
        return 401, {"ok": False, "error": "Wrong password."}
    sess.clear()
    sess.permanent = True
    sess["site_auth"] = d.get("epoch", "")
    return 200, {"ok": True}


def claim_hub(req, key):
    """First-time (or rotating) hub key. Returns (status_code, body)."""
    if hub_key_set():
        if not hub_ok(req):
            return 409, {"ok": False, "error": "This Pi is already claimed by a hub."}
    else:
        forwarded = any(req.headers.get(h) for h in
                        ("X-Forwarded-For", "X-Forwarded-Host", "Forwarded", "X-Real-Ip"))
        if req.remote_addr != HUB_IP or forwarded:
            return 403, {"ok": False, "error": "Only the hub's VPN address may claim this Pi."}
    try:
        set_hub_key(key)
    except ValueError as e:
        return 400, {"ok": False, "error": str(e)}
    return 200, {"ok": True}


# ---- Flask wiring -----------------------------------------------------------
def init_app(app):
    from datetime import timedelta
    app.secret_key = session_secret()
    app.config.update(
        SESSION_COOKIE_NAME=cookie_name(),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=SESSION_HOURS),
    )


# ---- CLI --------------------------------------------------------------------
def _cli(argv):
    import getpass
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "set-password":
        if sys.stdin.isatty():
            pw = getpass.getpass("New site password: ")
            if getpass.getpass("Again: ") != pw:
                print("Passwords differ — nothing changed.")
                return 1
        else:
            pw = sys.stdin.readline().rstrip("\n")
        try:
            set_password(pw)
        except ValueError as e:
            print(e)
            return 1
        print("Site password set. Existing logins were signed out.")
    elif cmd == "reset-hub-key":
        clear_hub_key()
        print("Hub key cleared — the hub will claim this Pi again on its next poll.")
    elif cmd == "set-hub-key":
        try:
            set_hub_key(sys.stdin.readline())
        except ValueError as e:
            print(e)
            return 1
        print("Hub key set.")
    elif cmd == "status":
        print(json.dumps({"password_set": password_set(), "hub_key_set": hub_key_set()}))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
