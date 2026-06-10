"""Per-device credential storage (username / password).

Stored in /data/credentials.json, separate from the device list so secrets are
never part of the frequently-polled /api/devices feed. Values are obfuscated at
rest with a per-install key (/data/secret.key) — this protects against casual
reading of the file or a backup, but is NOT a substitute for keeping the
dashboard on a trusted network: anyone who can reach the API can request a
stored password. Use the VPN for remote access and don't expose port 8090.
"""
import base64
import hashlib
import hmac
import json
import os
import threading

DATA_DIR = os.environ.get("NETWATCH_DATA", "/data")
CRED_PATH = os.path.join(DATA_DIR, "credentials.json")
KEY_PATH = os.path.join(DATA_DIR, "secret.key")

_lock = threading.Lock()
_key_cache = None


def _key():
    global _key_cache
    if _key_cache is not None:
        return _key_cache
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(KEY_PATH, "rb") as f:
            _key_cache = f.read()
    except FileNotFoundError:
        _key_cache = os.urandom(32)
        with open(KEY_PATH, "wb") as f:
            f.write(_key_cache)
        try:
            os.chmod(KEY_PATH, 0o600)
        except OSError:
            pass
    return _key_cache


def _stream(nonce, n):
    key, out, counter = _key(), b"", 0
    while len(out) < n:
        out += hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        counter += 1
    return out[:n]


def _enc(plain):
    if plain is None:
        plain = ""
    data = plain.encode("utf-8")
    nonce = os.urandom(8)
    ct = bytes(b ^ k for b, k in zip(data, _stream(nonce, len(data))))
    return base64.b64encode(nonce + ct).decode("ascii")


def _dec(token):
    try:
        raw = base64.b64decode(token)
        nonce, ct = raw[:8], raw[8:]
        return bytes(b ^ k for b, k in zip(ct, _stream(nonce, len(ct)))).decode("utf-8")
    except (ValueError, TypeError, UnicodeDecodeError):
        return ""


def _load():
    try:
        with open(CRED_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(store):
    with _lock:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = CRED_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(store, f, indent=2)
        os.replace(tmp, CRED_PATH)
        try:
            os.chmod(CRED_PATH, 0o600)
        except OSError:
            pass


def has(key):
    return key in _load()


def get(key):
    """Return {'username','password','notes'} (decrypted), or empty strings."""
    e = _load().get(key)
    if not e:
        return {"username": "", "password": "", "notes": ""}
    return {"username": _dec(e.get("u", "")),
            "password": _dec(e.get("p", "")),
            "notes": _dec(e.get("n", ""))}


def set_(key, username="", password="", notes=""):
    store = _load()
    if not (username or password or notes):
        store.pop(key, None)            # empty save clears the entry
    else:
        store[key] = {"u": _enc(username), "p": _enc(password), "n": _enc(notes)}
    _save(store)
    return has(key)


def keys_with_creds():
    return set(_load().keys())
