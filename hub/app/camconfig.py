"""Camera and recorder configuration backups, kept on the hub (Phase 6, part 1).

The site Pi reads each device's own backup file and a readable snapshot of its
settings (app/hikbackup.py) and passes them on; this keeps them:

  /data/camera_backups/<site>/<device>/<YYYYmmdd-HHMMSS>-<why>.hcb   the device's file
  /data/camera_backups/<site>/<device>/index.json                    what is there (no secrets)
  /data/camera_backups/<site>/<device>/snapshots.json                settings snapshots

A .hcb file is encrypted with the hub's backup key (backups.py, AES-256-GCM): the
device's file AND the file password it was exported with. A snapshot is kept only when
something changed, so "what changed" is the difference between two neighbours.

When: by hand ("Back up now"); before risky work forwarded through the hub (account,
password, channels, ISAPI explorer, logins and remote access) unless one was taken in
the last BEFORE_CHANGE_AGE; and at night (01:00–05:00 SA time) a gentle round: one
device at a time, a snapshot at most weekly, and the 3–5 MB file only when the snapshot
changed since the newest file or that file is older than FULL_EVERY — so a quiet farm
link carries a few KB a week per device.
"""
import hashlib
import json
import os
import re
import struct
import threading
import time
from urllib.parse import quote

import requests
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from flask import Blueprint, Response, jsonify, request

import backups
import hubconfig
import siteapi

bp = Blueprint("camconfig", __name__)

DIR = os.path.join(os.environ.get("HUB_DATA", "/data"), "camera_backups")
MAGIC = b"NWCB1\n"
_AAD = b"netwatch-camera-backup-v1"
KEEP = 6
SNAP_KEEP = 40
MAX_FILE = 64 << 20
SNAP_EVERY = 7 * 86400
FULL_EVERY = 30 * 86400
BEFORE_CHANGE_AGE = 12 * 3600
NIGHT = range(1, 5)                 # SA hours the nightly round may start
PAUSE = 5
WHY = ("hand", "change", "auto")
_NAME_RE = re.compile(r"^\d{8}-\d{6}-(hand|change|auto)\.hcb$")
_lock = threading.Lock()
_busy = {}                          # (site, key) -> True while a file is being taken


class CamBackupError(Exception):
    def __init__(self, msg, status=502):
        super().__init__(msg)
        self.status = status


# ---- files ---------------------------------------------------------------------------
def _dev_dir(site_id, key):
    return os.path.join(DIR, re.sub(r"[^A-Za-z0-9_-]", "-", site_id),
                        hashlib.sha1(str(key).encode()).hexdigest()[:16])


def _read_json(path, empty):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return empty


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def seal(meta, data, k=None):
    k = k or backups.key()
    nonce = os.urandom(12)
    m = json.dumps(meta).encode()
    return MAGIC + backups.key_id(k).encode() + b"\n" + nonce + AESGCM(k).encrypt(nonce, struct.pack(">I", len(m)) + m + data, _AAD)


def unseal(raw, k=None):
    k = k or backups.key()
    if not raw.startswith(MAGIC):
        raise CamBackupError("damaged backup file", 500)
    rest = raw[len(MAGIC):]
    kid, _, rest = rest.partition(b"\n")
    if kid.decode("ascii", "replace") != backups.key_id(k):
        raise CamBackupError("this backup was encrypted with a different backup key", 409)
    try:
        plain = AESGCM(k).decrypt(rest[:12], rest[12:], _AAD)
    except (InvalidTag, ValueError):
        raise CamBackupError("the backup failed its integrity check (damaged or tampered)", 500) from None
    n = struct.unpack(">I", plain[:4])[0]
    return json.loads(plain[4:4 + n]), plain[4 + n:]


def list_(site_id, key):
    idx = _read_json(os.path.join(_dev_dir(site_id, key), "index.json"), [])
    return [x for x in idx if isinstance(x, dict) and _NAME_RE.match(x.get("name") or "")]


def _save_index(site_id, key, idx):
    _write(os.path.join(_dev_dir(site_id, key), "index.json"), json.dumps(idx).encode())


def open_(site_id, key, name):
    if not _NAME_RE.match(name or ""):
        raise CamBackupError("unknown backup", 404)
    try:
        with open(os.path.join(_dev_dir(site_id, key), name), "rb") as f:
            raw = f.read()
    except OSError:
        raise CamBackupError("unknown backup", 404) from None
    return unseal(raw)


# ---- talking to the site -------------------------------------------------------------
def _get(site, path, timeout=(5, 60), **kw):
    try:
        return requests.get(siteapi.base_url(site) + path, headers=siteapi.headers(site), timeout=timeout, **kw)
    except requests.RequestException as e:
        raise CamBackupError(f"site unreachable: {e.__class__.__name__}") from None


def _site_error(r, what):
    if r.status_code in (404, 405) and "json" not in r.headers.get("Content-Type", ""):
        return CamBackupError("this site's Netwatch is too old for camera backups — update it", 501)
    try:
        msg = r.json().get("error") or ""
    except ValueError:
        msg = ""
    return CamBackupError(msg or f"the site could not {what} (HTTP {r.status_code})",
                          r.status_code if r.status_code in (403, 409) else 502)


def take(site, key, why="hand", who=""):
    """Read the device's backup file through the site and keep it. Returns the index entry."""
    if why not in WHY:
        raise CamBackupError("unknown reason", 400)
    with _lock:
        if _busy.get((site["id"], key)):
            raise CamBackupError("a backup of this device is already being taken", 409)
        _busy[(site["id"], key)] = True
    try:
        r = _get(site, f"/api/cameras/{quote(key, safe='')}/config-file", timeout=(5, 240), stream=True)
        if r.status_code != 200:
            raise _site_error(r, "read the backup file")
        data = bytearray()
        for chunk in r.iter_content(1 << 20):
            data += chunk
            if len(data) > MAX_FILE:
                raise CamBackupError("the backup file is too big", 502)
        try:
            facts = json.loads(r.headers.get("X-Config-Facts") or "{}")
        except ValueError:
            facts = {}
        secret = r.headers.get("X-Config-Secret") or ""
        data = bytes(data)
        sha = hashlib.sha256(data).hexdigest()
        if facts.get("sha256") and facts["sha256"] != sha:
            raise CamBackupError("the backup file was damaged on the way", 502)
        ts = int(time.time())
        name = time.strftime("%Y%m%d-%H%M%S", time.gmtime(ts)) + f"-{why}.hcb"
        snap = _newest_snap(site["id"], key)
        meta = {"name": name, "ts": ts, "why": why, "who": who or "", "key": key, "device": facts.get("name") or "",
                "model": facts.get("model") or "", "firmware": facts.get("firmware") or "", "bytes": len(data),
                "sha256": sha, "has_password": bool(secret), "snapshot_fp": (snap or {}).get("fingerprint") or ""}
        d = _dev_dir(site["id"], key)
        _write(os.path.join(d, name), seal({**meta, "secret": secret}, data))
        idx = [x for x in list_(site["id"], key) if x["name"] != name] + [meta]
        idx.sort(key=lambda x: x["ts"])
        # keep the newest KEEP, and always the newest nightly one
        keep = idx[-KEEP:]
        night = [x for x in idx if x["why"] == "auto"]
        if night and night[-1] not in keep:
            keep = [night[-1]] + keep[1:]
        for x in idx:
            if x not in keep:
                try:
                    os.remove(os.path.join(d, x["name"]))
                except OSError:
                    pass
        _save_index(site["id"], key, keep)
        print(f"[camconfig] stored {site['id']}/{key} {name} ({len(data)} bytes)", flush=True)
        return meta
    finally:
        with _lock:
            _busy.pop((site["id"], key), None)


def restore(site, key, name, confirm_text, who=""):
    meta, data = open_(site["id"], key, name)
    if meta.get("key") != key:
        raise CamBackupError("that backup is another device's", 409)
    try:
        r = requests.put(f"{siteapi.base_url(site)}/api/cameras/{quote(key, safe='')}/config-file", data=data,
                         headers=siteapi.headers(site, {"X-Config-Secret": meta.get("secret") or "",
                                                        "X-Confirm-Text": str(confirm_text or "")[:20],
                                                        "Content-Type": "application/octet-stream"}),
                         timeout=(5, 240))
    except requests.RequestException as e:
        raise CamBackupError(f"site unreachable: {e.__class__.__name__}") from None
    try:
        j = r.json()
    except ValueError:
        raise _site_error(r, "load the file") from None
    return j, r.status_code


# ---- snapshots and what changed ------------------------------------------------------
def _snaps(site_id, key):
    return _read_json(os.path.join(_dev_dir(site_id, key), "snapshots.json"), [])


def _newest_snap(site_id, key):
    s = _snaps(site_id, key)
    return s[-1] if s else None


def snapshot(site, key):
    """Read a snapshot through the site; keep it if it differs from the last one.
    Returns (snapshot, changed)."""
    r = _get(site, f"/api/cameras/{quote(key, safe='')}/snapshot", timeout=(5, 120))
    if r.status_code != 200:
        raise _site_error(r, "read the settings")
    try:
        s = r.json()
    except ValueError:
        raise CamBackupError("the site sent a bad snapshot") from None
    s = {k: s.get(k) for k in ("ts", "name", "lenses", "labels", "fingerprint")}
    snaps = _snaps(site["id"], key)
    last = snaps[-1] if snaps else None
    if last and last.get("fingerprint") == s["fingerprint"]:
        last["seen"] = s["ts"]
        changed = False
    else:
        s["seen"] = s["ts"]
        snaps.append(s)
        changed = bool(last)
    _write(os.path.join(_dev_dir(site["id"], key), "snapshots.json"), json.dumps(snaps[-SNAP_KEEP:]).encode())
    return s, changed


def diff(a, b):
    """[(field, before, after)] between two snapshots' values (lens-qualified)."""
    out = []
    la, lb = (a or {}).get("lenses") or {}, (b or {}).get("lenses") or {}
    for lens in sorted(set(la) | set(lb)):
        sa, sb = la.get(lens) or {}, lb.get(lens) or {}
        for sec in sorted(set(sa) | set(sb)):
            fa, fb = sa.get(sec) or {}, sb.get(sec) or {}
            for f in sorted(set(fa) | set(fb)):
                if fa.get(f) != fb.get(f) and (sec in sa and sec in sb):
                    out.append({"lens": lens, "id": f, "before": fa.get(f), "after": fb.get(f)})
    return out


def changes(site_id, key, limit=15):
    snaps = _snaps(site_id, key)
    out = []
    for a, b in zip(snaps, snaps[1:]):
        labels = {**(a.get("labels") or {}), **(b.get("labels") or {})}
        rows = [{**x, "label": labels.get(x["id"]) or x["id"]} for x in diff(a, b)]
        if rows:
            out.append({"from": a.get("seen") or a.get("ts"), "ts": b.get("ts"), "fields": rows[:60], "more": max(0, len(rows) - 60)})
    return list(reversed(out))[:limit]


# ---- before risky work, and the nightly round ----------------------------------------
def before_change(site, key, who=""):
    """Take a backup unless a recent one exists. Never blocks the change: returns a note."""
    idx = list_(site["id"], key)
    if idx and time.time() - idx[-1]["ts"] < BEFORE_CHANGE_AGE:
        return ""
    try:
        take(site, key, "change", who)
        return ""
    except CamBackupError as e:
        return f"no backup was taken first ({e})"


def _sa_hour():
    return (time.gmtime().tm_hour + 2) % 24


def _devices(listing):
    out = [c["key"] for c in (listing.get("cameras") or []) if c.get("has_login") and c.get("kind") == "camera" and c.get("online")]
    out += [r["key"] for r in (listing.get("recorders") or []) if r.get("has_login") and r.get("online")]
    return [k for k in out if k]


def nightly_one(site, key):
    """One device's nightly turn. Returns what happened (for the log)."""
    snaps = _snaps(site["id"], key)
    if snaps and time.time() - (snaps[-1].get("seen") or 0) < SNAP_EVERY:
        return "snapshot recent"
    s, _changed = snapshot(site, key)
    idx = list_(site["id"], key)
    newest = idx[-1] if idx else None
    if newest and newest.get("snapshot_fp") == s["fingerprint"] and time.time() - newest["ts"] < FULL_EVERY:
        return "unchanged"
    take(site, key, "auto")
    return "file taken"


def _round(listing_of, reachable):
    for site in hubconfig.load().get("sites", []):
        if not (site.get("enabled", True) and site.get("id") and site.get("vpn_ip") and reachable(site["id"])):
            continue
        hit = listing_of(site["id"])
        listing = hit[1] if hit else None
        if not (isinstance(listing, dict) and listing.get("enabled")):
            continue
        for key in _devices(listing):
            if _sa_hour() not in NIGHT:
                return
            try:
                what = nightly_one(site, key)
            except CamBackupError as e:
                if e.status in (403, 501):
                    break                     # camera settings off on that site, or too old: next site
                what = f"failed: {e}"
            if what != "snapshot recent":
                print(f"[camconfig] nightly {site['id']}/{key}: {what}", flush=True)
                time.sleep(PAUSE)


def _loop(listing_of, reachable):
    time.sleep(300)
    while True:
        if _sa_hour() in NIGHT:
            try:
                _round(listing_of, reachable)
            except Exception as e:  # noqa: BLE001 - the loop must live
                print(f"[camconfig] nightly round: {e}", flush=True)
        time.sleep(1800)


def start(listing_of, reachable):
    threading.Thread(target=_loop, args=(listing_of, reachable), daemon=True, name="camera-backups").start()


# ---- routes ----------------------------------------------------------------------------
def _site(site_id):
    site = hubconfig.get_site(site_id)
    if not site:
        raise CamBackupError("unknown site", 404)
    return site


def _who():
    return "hub"            # the hub has one login


def _fail(e):
    return jsonify({"ok": False, "error": str(e)}), e.status


@bp.route("/api/hub/sites/<site_id>/cameras/<path:key>/backups", methods=["GET", "POST"])
def device_backups(site_id, key):
    """GET: the device's backups (newest first) and what changed between snapshots.
    POST: back it up now (and take a snapshot)."""
    try:
        site = _site(site_id)
        if request.method == "POST":
            note = ""
            try:
                snapshot(site, key)
            except CamBackupError as e:
                note = f"the settings snapshot failed ({e})"
            meta = take(site, key, "hand", _who())
            return jsonify({"ok": True, "backup": meta, "note": note,
                            "message": "Backed up." + (" " + note if note else "")})
        snap = _newest_snap(site_id, key)
        return jsonify({"ok": True, "backups": list(reversed(list_(site_id, key))), "changes": changes(site_id, key),
                        "snapshot": {"ts": snap.get("ts"), "seen": snap.get("seen")} if snap else None,
                        "keep": KEEP})
    except CamBackupError as e:
        return _fail(e)


@bp.route("/api/hub/sites/<site_id>/cameras/<path:key>/backups/<name>")
def device_backup_file(site_id, key, name):
    """The device's own file, decrypted, to save on a PC (Hikvision's tools import it
    with the file password shown by …/password)."""
    try:
        _site(site_id)
        meta, data = open_(site_id, key, name)
    except CamBackupError as e:
        return _fail(e)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", (meta.get("device") or "device"))[:40]
    resp = Response(data, mimetype="application/octet-stream")
    resp.headers["Content-Disposition"] = f'attachment; filename="{safe}-{name[:15]}.bin"'
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/api/hub/sites/<site_id>/cameras/<path:key>/backups/<name>/password")
def device_backup_password(site_id, key, name):
    try:
        _site(site_id)
        meta, _data = open_(site_id, key, name)
    except CamBackupError as e:
        return _fail(e)
    return jsonify({"ok": True, "password": meta.get("secret") or "",
                    "note": "" if meta.get("secret") else "This device's file has no password."})


@bp.route("/api/hub/sites/<site_id>/cameras/<path:key>/backups/<name>/restore", methods=["POST"])
def device_backup_restore(site_id, key, name):
    b = request.get_json(silent=True) or {}
    try:
        site = _site(site_id)
        j, status = restore(site, key, name, b.get("confirm_text"), _who())
    except CamBackupError as e:
        return _fail(e)
    return jsonify(j), status
