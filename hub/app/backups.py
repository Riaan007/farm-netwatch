"""Per-site config backups, stored on the hub.

The poller pulls each site's /api/config/export once a day (and on demand from
the site page) into /data/backups/<site_id>/<utc-ts>.json, keeping the newest
KEEP copies. A bundle contains everything needed to rebuild the site on a new
Pi — settings, device registry, obfuscated logins + key, and the wg config.

Encrypted at rest (AES-256-GCM, `cryptography`). Each file is a small JSON
envelope — the key id and nonce are visible, the bundle is not — so a copied
backup folder, a downloaded file or an offsite copy is useless without the
backup key. The key is /data/backup.key (mode 600), generated on first use.
It lives on the same disk as the store, so this does NOT protect against
someone who has the whole hub data volume: keep a copy of the key somewhere
else (hub site page -> Backups -> Backup key) — without it the backups cannot
be restored on a new hub.

Older plaintext files are encrypted in place at startup (encrypt_existing).

CLI (inside the hub container):
    python backups.py key                    print the backup key (base64)
    python backups.py decrypt FILE [KEYB64]  print a backup's bundle JSON
"""
import base64
import hashlib
import json
import os
import re
import sys
import time

import requests
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import siteapi

DATA_DIR = os.environ.get("HUB_DATA", "/data")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
KEY_PATH = os.path.join(DATA_DIR, "backup.key")
KEEP = 14
_NAME_RE = re.compile(r"^[0-9]{8}-[0-9]{6}\.json$")
KIND = "netwatch-backup-encrypted"
_AAD = b"netwatch-backup-v1"


class BackupError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


# ---- encryption --------------------------------------------------------------
def key():
    try:
        with open(KEY_PATH, "rb") as f:
            k = f.read()
        if len(k) == 32:
            return k
        raise BackupError(f"{KEY_PATH} is not a 32-byte key — refusing to guess", 500)
    except FileNotFoundError:
        pass
    os.makedirs(DATA_DIR, exist_ok=True)
    k = os.urandom(32)
    fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(k)
    print("[backups] generated a new backup key", flush=True)
    return k


def key_id(k=None):
    return hashlib.sha256(k or key()).hexdigest()[:16]


def encrypt(plain, k=None):
    k = k or key()
    nonce = os.urandom(12)
    ct = AESGCM(k).encrypt(nonce, plain, _AAD)
    return json.dumps({"kind": KIND, "version": 1, "alg": "AES-256-GCM",
                       "key_id": key_id(k), "nonce": base64.b64encode(nonce).decode(),
                       "data": base64.b64encode(ct).decode()}).encode()


def is_encrypted(raw):
    return raw[:200].lstrip().startswith(b"{") and KIND.encode() in raw[:200]


def decrypt(raw, k=None):
    """Envelope bytes -> bundle bytes. Plain (pre-encryption) files pass through."""
    if not is_encrypted(raw):
        return raw
    try:
        env = json.loads(raw)
    except ValueError:
        raise BackupError("damaged backup file", 500)
    k = k or key()
    if env.get("key_id") != key_id(k):
        raise BackupError("this backup was encrypted with a different backup key", 409)
    try:
        return AESGCM(k).decrypt(base64.b64decode(env["nonce"]),
                                 base64.b64decode(env["data"]), _AAD)
    except (InvalidTag, KeyError, ValueError):
        raise BackupError("backup failed its integrity check (damaged or tampered)", 500)


def _write_atomic(path, data, mtime=None):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    if mtime:
        os.utime(path, (mtime, mtime))


def encrypt_existing():
    """Encrypt any plaintext backups left from before encryption. Keeps names
    and timestamps so the list and retention are unchanged."""
    done = 0
    try:
        sites = os.listdir(BACKUP_DIR)
    except OSError:
        return 0
    for sd in sites:
        d = os.path.join(BACKUP_DIR, sd)
        if not os.path.isdir(d):
            continue
        for n in os.listdir(d):
            if not _NAME_RE.match(n):
                continue
            p = os.path.join(d, n)
            try:
                with open(p, "rb") as f:
                    raw = f.read()
                if is_encrypted(raw):
                    continue
                json.loads(raw)                 # only encrypt what is a real bundle
                _write_atomic(p, encrypt(raw), mtime=os.path.getmtime(p))
                done += 1
            except (OSError, ValueError) as e:
                print(f"[backups] could not encrypt {sd}/{n}: {e}", flush=True)
    if done:
        print(f"[backups] encrypted {done} older plaintext backup(s)", flush=True)
    return done


def _dir(site_id):
    return os.path.join(BACKUP_DIR, re.sub(r"[^A-Za-z0-9_-]", "-", site_id))


def store(site, timeout):
    """Pull the site's export bundle and persist it. Returns the list entry."""
    base = f"http://{site['vpn_ip']}:{site.get('netwatch_port', 8090)}"
    try:
        r = requests.get(f"{base}/api/config/export", timeout=timeout,
                         headers=siteapi.headers(site))
    except requests.RequestException as e:
        raise BackupError(f"site unreachable: {e.__class__.__name__}")
    if r.status_code == 401:
        raise BackupError("the site refused the hub's key — see the Pi login "
                          "warning on the site card", 502)
    if r.status_code == 404:
        raise BackupError("this site's Netwatch is too old for backups — "
                          "update it (docker compose pull)", 501)
    try:
        r.raise_for_status()
        bundle = r.json()
    except (requests.RequestException, ValueError):
        raise BackupError("site returned a bad export")
    if bundle.get("kind") != "netwatch-backup":
        raise BackupError("site returned a bad export")

    d = _dir(site["id"])
    os.makedirs(d, exist_ok=True)
    name = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + ".json"
    _write_atomic(os.path.join(d, name), encrypt(json.dumps(bundle).encode()))
    for old in sorted(os.listdir(d))[:-KEEP]:
        if _NAME_RE.match(old):
            try:
                os.remove(os.path.join(d, old))
            except OSError:
                pass
    print(f"[backups] stored {site['id']}/{name}", flush=True)
    return {"name": name, "ts": int(time.time()),
            "bytes": os.path.getsize(os.path.join(d, name))}


def list_(site_id):
    d = _dir(site_id)
    out = []
    try:
        names = sorted(os.listdir(d), reverse=True)
    except OSError:
        return out
    for n in names:
        if not _NAME_RE.match(n):
            continue
        p = os.path.join(d, n)
        try:
            with open(p, "rb") as f:
                head = f.read(200)
            out.append({"name": n, "bytes": os.path.getsize(p),
                        "ts": int(os.path.getmtime(p)),
                        "encrypted": is_encrypted(head)})
        except OSError:
            pass
    return out


def read(site_id, name):
    """Decrypted bundle bytes (for restore), or (None, None)."""
    raw, name = read_raw(site_id, name)
    return (decrypt(raw), name) if raw is not None else (None, None)


def read_raw(site_id, name):
    """Stored (encrypted) file bytes, or None. `name` must match the timestamp
    pattern (no traversal); name=None -> newest."""
    if name is None:
        entries = list_(site_id)
        if not entries:
            return None, None
        name = entries[0]["name"]
    if not _NAME_RE.match(name):
        return None, None
    try:
        with open(os.path.join(_dir(site_id), name), "rb") as f:
            return f.read(), name
    except OSError:
        return None, None


def delete_(site_id, name):
    """Delete one stored backup. `name` must match the timestamp pattern (the
    same guard as read() — no traversal outside the site's backup folder).
    Returns True if a file was removed."""
    if not _NAME_RE.match(name or ""):
        return False
    try:
        os.remove(os.path.join(_dir(site_id), name))
        return True
    except OSError:
        return False


def _cli(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "key":
        print(base64.b64encode(key()).decode())
        return 0
    if cmd == "decrypt" and len(argv) >= 3:
        k = base64.b64decode(argv[3]) if len(argv) > 3 else None
        with open(argv[2], "rb") as f:
            try:
                sys.stdout.write(decrypt(f.read(), k).decode())
            except BackupError as e:
                print(e, file=sys.stderr)
                return 1
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
