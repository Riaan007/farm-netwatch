"""Device configuration backups and settings snapshots (Phase 6, part 1).

The site Pi only reads and passes on; it keeps nothing on its SD card. The hub pulls:

  * the device's own backup file (/ISAPI/System/configurationData). Current firmware
    refuses an export without a file password ("secretkey", cfgFileSecretKeyError), so
    every export gets a fresh random one, sent to the hub with the file — the hub keeps
    both encrypted. Old firmware that doesn't know the parameter is asked once without.
  * a snapshot: Netwatch's own readable copy of the settings it knows (every section
    of every group, all lenses), so the hub can say what changed between two snapshots.

Restoring a file is risky work ("Risky camera work" + the typed word): the device takes
every setting from the file — users and its network address too. Current firmware
answers "Reboot Required" and waits: the file is used only after a restart (seen live on
a DS-2CD2046G2: no restart in 7 minutes until asked).
"""
import hashlib
import json
import secrets
import string
import threading
import time
import uuid

import hikisapi
import hiksecurity
import hiksettings

PATH = "/ISAPI/System/configurationData"
MAX_FILE = 64 << 20
SECRET_LEN = 16                     # the home recorder's cfgFileSecretKeyLenLimit
_ALPHA = string.ascii_letters + string.digits
_lock = threading.Lock()            # one backup file at a time per site: they are big


class BackupError(Exception):
    def __init__(self, msg, status=400, extra=None):
        super().__init__(msg)
        self.status = status
        self.extra = extra or {}


def new_secret():
    """16 letters and digits with at least one of each kind (URL-safe as it is)."""
    while True:
        s = "".join(secrets.choice(_ALPHA) for _ in range(SECRET_LEN))
        if any(c.isupper() for c in s) and any(c.islower() for c in s) and any(c.isdigit() for c in s):
            return s


def _is_status(b):
    head = (b or b"")[:400].lstrip()
    return head.startswith(b"<") and b"ResponseStatus" in head


def _sub(b):
    root = hikisapi.parse(b) if b else None
    return (hikisapi.text(root, "subStatusCode") or hikisapi.text(root, "statusString") or "") if root is not None else ""


def _device(settings, key):
    """The device itself, reached with its own login (a camera seen only through its
    recorder is backed up as part of the recorder's file)."""
    if not settings.enabled():
        raise BackupError("Camera settings are turned off for this site (Settings → Cameras).", 403)
    try:
        e, use, ch, _cl, sem, d = settings._target(key, 1, "direct")
    except hiksettings.SettingsError as ex:
        if (ex.extra or {}).get("reason") == "no_own_login":
            raise BackupError("This camera has no login of its own; its recorder's backup holds its channel.", 409,
                              {"reason": "no_own_login"}) from None
        raise BackupError(str(ex), ex.status, ex.extra) from None
    return e, settings._admin_client(e["_d"]), sem, d


def export(settings, key):
    """(file bytes, file password or "", facts about the device)."""
    e, cl, sem, d = _device(settings, key)
    if not _lock.acquire(timeout=1):
        raise BackupError("another backup is being read on this site; try again in a minute", 409)
    try:
        with sem:
            sec = new_secret()
            r = cl.request("GET", f"{PATH}?secretkey={sec}", max_bytes=MAX_FILE, timeout=(5, 120))
            if r.status != 200 and r.status not in (401, 403) and "secret" not in _sub(r.content).lower():
                # old firmware: no file password at all
                r2 = cl.request("GET", PATH, max_bytes=MAX_FILE, timeout=(5, 120))
                if r2.status == 200 and not _is_status(r2.content):
                    r, sec = r2, ""
    except hikisapi.LoginRefused as ex:
        raise BackupError(str(ex) or "the device refused the saved login", 403) from None
    except hikisapi.TooBig:
        raise BackupError("the backup file is bigger than Netwatch takes (64 MB)", 502) from None
    except hikisapi.Unreachable as ex:
        raise BackupError(f"the device did not answer ({str(ex) or ex.__class__.__name__})", 502) from None
    finally:
        _lock.release()
    if r.status != 200 or _is_status(r.content) or len(r.content) < 1024:
        why = _sub(r.content)
        raise BackupError(f"the device did not give its backup file (HTTP {r.status}{' ' + why if why else ''})"
                          + (" — the login may not be its admin" if r.status in (401, 403) else ""), 502)
    facts = {"key": key, "name": e.get("name") or "", "model": d.get("model") or "", "firmware": d.get("firmware") or "",
             "recorder": bool(e.get("_recorder")), "bytes": len(r.content),
             "sha256": hashlib.sha256(r.content).hexdigest()}
    return r.content, sec, facts


def restore(settings, key, data, secret, who="", confirm_text=""):
    """Load a backup file into the device (it restarts). Risky work."""
    settings._risky_gate(confirm_text)
    if not data or len(data) < 1024 or len(data) > MAX_FILE:
        raise BackupError("that is not a backup file")
    if secret and (len(secret) > 64 or any(c not in _ALPHA for c in secret)):
        raise BackupError("the file password has characters Netwatch can't send")
    e, cl, sem, d = _device(settings, key)
    if not settings._risky_lock.acquire(blocking=False):
        raise BackupError("another risky job is running on this site; one at a time", 409)
    try:
        with sem:
            r = cl.request("PUT", PATH + (f"?secretkey={secret}" if secret else ""), data=data,
                           headers={"Content-Type": "application/octet-stream"}, max_bytes=64 << 10, timeout=(5, 180))
    except hikisapi.LoginRefused as ex:
        raise BackupError(str(ex) or "the device refused the saved login", 403) from None
    except hikisapi.Unreachable as ex:
        raise BackupError(f"the device did not answer ({str(ex) or ex.__class__.__name__}) — it may be restarting; "
                          "check it in a few minutes", 502) from None
    finally:
        settings._risky_lock.release()
    ok, reboot, msg = hiksettings._status(r)
    entry = {"id": uuid.uuid4().hex[:12], "ts": int(time.time()), "who": who or "", "key": key, "ip": e.get("ip"),
             "mac": e.get("mac"), "name": e.get("name"), "lens": 1, "route": "direct", "channel": None,
             "section": "config_restore", "risk": "dangerous", "section_label": "Backup file loaded",
             "fields": [], "result": "ok" if ok else "failed", "error": "" if ok else msg, "reboot": True,
             "undo_of": None, "undone_by": None, "no_undo": True}
    settings.log.add(entry)
    if settings.on_change:
        try:
            settings.on_change(d, entry)
        except Exception:  # noqa: BLE001 - history must never fail a change
            pass
    if not ok:
        raise BackupError(f"the device refused the file: {msg}", 502)
    # statusCode 7 = "Reboot Required": the file is only used after the next restart,
    # which the device does NOT do by itself (seen live) — Netwatch says so, and the
    # page offers the restart.
    return {"ok": True, "reboot_needed": bool(reboot), "http": r.status,
            "message": ("The device took the file. It uses it only after a restart — restart it now (Device details → Restart)."
                        if reboot else "The device took the file and restarts by itself — give it two or three minutes."),
            "change": entry}


def snapshot(settings, key):
    """{lenses: {"1": {section: {field: value}}}, labels, fingerprint} — what the
    device's settings are now, in Netwatch's words."""
    if not settings.enabled():
        raise BackupError("Camera settings are turned off for this site (Settings → Cameras).", 403)
    try:
        e, _use, _ch, _cl, _sem, _d = settings._target(key, 1)
        out, labels = {}, {}
        for lens in settings._lenses(e):
            v = settings.values(key, lens, None)
            out[str(lens)] = v["values"]
            labels.update(v.get("labels") or {})
    except hiksettings.SettingsError as ex:
        raise BackupError(str(ex), ex.status, ex.extra) from None
    fp = hashlib.sha256(json.dumps(out, sort_keys=True).encode()).hexdigest()[:16]
    return {"ok": True, "key": key, "name": e.get("name") or "", "ts": int(time.time()), "lenses": out,
            "labels": labels, "fingerprint": fp}
