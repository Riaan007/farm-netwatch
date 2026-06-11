"""Hub login: one shared password, Flask session cookie.

The VPN is the real perimeter (don't expose the hub port on the WAN); this is
defense in depth so a guest on the LAN can't browse every farm. The password
hash lives in hub.json; the Flask session secret is a per-install key file —
same idiom as the site app's creds.py.
"""
import os

from werkzeug.security import check_password_hash, generate_password_hash

import hubconfig

DATA_DIR = os.environ.get("HUB_DATA", "/data")
KEY_PATH = os.path.join(DATA_DIR, "secret.key")


def secret_key():
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(KEY_PATH, "rb") as f:
            return f.read()
    except FileNotFoundError:
        key = os.urandom(32)
        with open(KEY_PATH, "wb") as f:
            f.write(key)
        try:
            os.chmod(KEY_PATH, 0o600)
        except OSError:
            pass
        return key


def password_set():
    return bool(hubconfig.load()["auth"]["password_hash"])


def set_password(password):
    hubconfig.update({"auth": {"password_hash": generate_password_hash(password)}})


def check(password):
    h = hubconfig.load()["auth"]["password_hash"]
    return bool(h) and check_password_hash(h, password)


def seed_from_env():
    """First boot convenience: HUB_PASSWORD env sets the password once.

    Never overwrites an existing hash, so a password changed in the UI wins
    over a stale env var on restart.
    """
    pw = os.environ.get("HUB_PASSWORD", "").strip()
    if pw and not password_set():
        set_password(pw)
