"""The firmware library and firmware updates, run from the hub (Phase 6, part 2).

Hikvision has no public firmware feed: you download a file from Hikvision and add it
here with the models it is for, its version and its region. Netwatch then lists which
cameras and recorders at every site it fits and which run something older.

  /data/firmware/<sha256>.dav        the firmware file (a .zip from Hikvision is unpacked)
  /data/firmware/library.json        what each file is, and where it was proven
  /data/firmware/jobs.json           the newest update per device
  /data/firmware/uploads/<id>.part   an upload from the browser, in 8 MB pieces

An update, one device at a time per site:
  check through the site (model fits, file newer, own login, "Risky camera work" on)
  → a configuration backup (camconfig.py; an update does not start without one)
  → the file to the site Pi in pieces that resume, ONCE per site
  → the site Pi sends it to the device on the farm's network, restarts it, checks it
    (app/hikfw.py) — the hub follows along.

Rules: a file nobody has used yet goes on ONE device first (the test unit); others wait
until that update was checked. Never two updates at once on one site. Recorders are not
updated at night (19:00–06:00 SA time), when nobody could go and look.
"""
import fnmatch
import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
import zipfile
from urllib.parse import quote

import requests
from flask import Blueprint, jsonify, request

import camconfig
import cameras
import hubconfig
import siteapi

bp = Blueprint("camfw", __name__)

DIR = os.path.join(os.environ.get("HUB_DATA", "/data"), "firmware")
MAX_FILE = 512 << 20
MIN_FILE = 1 << 20
PIECE = 4 << 20                     # to the site Pi
UP_PIECE = 8 << 20                  # from the browser
POLL_S = 5
SITE_JOB_MAX_S = 45 * 60
PIECE_TRIES = 6
NIGHT_FROM, NIGHT_TO = 19, 6        # SA hours: no recorder updates
REGIONS = ("International / EU", "China", "Other")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_lock = threading.Lock()
_jobs = {}                          # (site, key) -> job


class FwError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


# ---- versions (the same rules as the site's app/hikfw.py) -------------------------------
def version_key(s):
    s = str(s or "")
    m = re.search(r"[Vv]?(\d+(?:\.\d+)+)", s)
    nums = tuple(int(x) for x in m.group(1).split(".")) if m else ()
    b = re.search(r"build\s*(\d{6})", s, re.I)
    return nums + (0,) * (4 - len(nums)) if nums else (), int(b.group(1)) if b else 0


def compare(device_fw, file_version):
    dv, db = version_key(device_fw)
    fv, fb = version_key(file_version)
    if not dv or not fv:
        return "unknown"
    if fv != dv:
        return "newer" if fv > dv else "older"
    if fb and db and fb != db:
        return "newer" if fb > db else "older"
    return "same"


def model_fits(model, patterns):
    m = str(model or "").strip().upper()
    return bool(m) and any(p and (fnmatch.fnmatchcase(m, p) or m == p)
                           for p in (str(x or "").strip().upper() for x in patterns or []))


# ---- the library -----------------------------------------------------------------------
def _read_json(path, empty):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return empty


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def library():
    return [x for x in _read_json(os.path.join(DIR, "library.json"), []) if isinstance(x, dict) and _SHA_RE.match(x.get("sha256") or "")]


def _save_library(lib):
    _write_json(os.path.join(DIR, "library.json"), lib)


def entry(sha):
    e = next((x for x in library() if x["sha256"] == sha), None)
    if not e or not os.path.exists(_file(sha)):
        raise FwError("that file is not in the library", 404)
    return e


def _file(sha):
    if not _SHA_RE.match(sha or ""):
        raise FwError("unknown file", 404)
    return os.path.join(DIR, sha + ".dav")


def _clean_models(v):
    if isinstance(v, str):
        v = re.split(r"[,\n;]", v)
    out = []
    for m in v or []:
        m = re.sub(r"\s+", "", str(m or "")).upper()[:60]
        if m and re.match(r"^[A-Z0-9*?/()._-]+$", m) and m not in out and m not in ("*", "DS-*"):
            out.append(m)
    return out[:40]


def _upload_dir():
    return os.path.join(DIR, "uploads")


def upload_begin(name, size):
    if not MIN_FILE <= int(size or 0) <= MAX_FILE:
        raise FwError(f"a firmware file is between 1 MB and {MAX_FILE >> 20} MB", 400)
    os.makedirs(_upload_dir(), exist_ok=True)
    # throw away uploads nobody finished
    for n in os.listdir(_upload_dir()):
        p = os.path.join(_upload_dir(), n)
        if time.time() - os.path.getmtime(p) > 86400:
            try:
                os.remove(p)
            except OSError:
                pass
    free = shutil.disk_usage(_upload_dir()).free
    if free < 3 * int(size) + (500 << 20):
        raise FwError("the hub has too little free disk space for this file", 507)
    uid = uuid.uuid4().hex[:16]
    _write_json(os.path.join(_upload_dir(), uid + ".json"), {"name": str(name or "firmware")[:120], "bytes": int(size)})
    open(os.path.join(_upload_dir(), uid + ".part"), "wb").close()
    return uid


def _upload_paths(uid):
    if not _ID_RE.match(uid or ""):
        raise FwError("unknown upload", 404)
    meta = _read_json(os.path.join(_upload_dir(), uid + ".json"), None)
    if not meta:
        raise FwError("unknown upload", 404)
    return meta, os.path.join(_upload_dir(), uid + ".part")


def upload_piece(uid, offset, data):
    meta, part = _upload_paths(uid)
    have = os.path.getsize(part)
    if offset != have:
        raise FwError(f"piece out of order (have {have})", 409)
    if have + len(data) > meta["bytes"]:
        raise FwError("too many bytes", 400)
    with open(part, "ab") as f:
        f.write(data)
    return have + len(data)


def _sha_copy(src, dst):
    h = hashlib.sha256()
    n = 0
    with open(dst, "wb") as out:
        for c in iter(lambda: src.read(1 << 20), b""):
            h.update(c)
            out.write(c)
            n += len(c)
    return h.hexdigest(), n


def upload_finish(uid, version, models, region, notes=""):
    meta, part = _upload_paths(uid)
    try:
        if os.path.getsize(part) != meta["bytes"]:
            raise FwError("the upload is not complete", 409)
        version = str(version or "").strip()[:60]
        if not version_key(version)[0]:
            raise FwError("give the version as Hikvision writes it, e.g. V5.7.15 build 230425")
        models = _clean_models(models)
        if not models:
            raise FwError("list the models this file is for (e.g. DS-2CD2046G2-IU, or DS-2CD2*G2* for a family)")
        if region not in REGIONS:
            raise FwError("choose the file's region")
        name, inner = meta["name"], ""
        tmp = os.path.join(_upload_dir(), uid + ".dav")
        if zipfile.is_zipfile(part):
            with zipfile.ZipFile(part) as z:
                davs = [i for i in z.infolist() if i.filename.lower().endswith(".dav") and not i.is_dir()]
                if len(davs) != 1:
                    raise FwError("the zip file must hold exactly one .dav firmware file (it holds "
                                  f"{len(davs)}); unpack it and add the right one")
                if davs[0].file_size > MAX_FILE:
                    raise FwError("the firmware inside the zip is too big")
                inner = os.path.basename(davs[0].filename)
                with z.open(davs[0]) as src:
                    sha, size = _sha_copy(src, tmp)
        else:
            with open(part, "rb") as src:
                sha, size = _sha_copy(src, tmp)
        with open(tmp, "rb") as f:
            head = f.read(512)
        if size < MIN_FILE or head.lstrip()[:1] in (b"<", b"{") or head[:5] == b"%PDF-":
            raise FwError("that is not a firmware file")
        lib = library()
        if any(x["sha256"] == sha for x in lib):
            raise FwError("this file is already in the library", 409)
        os.makedirs(DIR, exist_ok=True)
        os.replace(tmp, _file(sha))
        e = {"sha256": sha, "file": name, "inner": inner, "bytes": size, "version": version, "models": models,
             "region": region, "notes": str(notes or "")[:500], "added": int(time.time()), "proven": [], "failed": []}
        lib.append(e)
        _save_library(lib)
        return e
    finally:
        for p in (part, os.path.join(_upload_dir(), uid + ".json"), os.path.join(_upload_dir(), uid + ".dav")):
            try:
                os.remove(p)
            except OSError:
                pass


def remove(sha):
    with _lock:
        if any(j["sha256"] == sha and j["state"] == "running" for j in _jobs.values()):
            raise FwError("an update with this file is running", 409)
    lib = library()
    if not any(x["sha256"] == sha for x in lib):
        raise FwError("that file is not in the library", 404)
    _save_library([x for x in lib if x["sha256"] != sha])
    try:
        os.remove(_file(sha))
    except OSError:
        pass


def _mark(sha, field, rec):
    lib = library()
    for x in lib:
        if x["sha256"] == sha:
            x.setdefault(field, []).append(rec)
            x[field] = x[field][-50:]
    _save_library(lib)


# ---- what fits where --------------------------------------------------------------------
def _devices_of(listing):
    out = []
    for c in (listing or {}).get("cameras") or []:
        if c.get("kind", "camera") == "camera":
            out.append({"key": c.get("key"), "name": c.get("name") or "", "model": c.get("model") or "",
                        "firmware": c.get("firmware") or "", "recorder": False, "own_login": bool(c.get("has_login")),
                        "online": bool(c.get("online"))})
    for r in (listing or {}).get("recorders") or []:
        out.append({"key": r.get("key"), "name": r.get("name") or "", "model": r.get("model") or "",
                    "firmware": r.get("firmware") or "", "recorder": True, "own_login": bool(r.get("has_login")),
                    "online": bool(r.get("online"))})
    return [d for d in out if d["key"]]


def _site_devices():
    out = []
    for s in hubconfig.load().get("sites", []):
        if not (s.get("enabled", True) and s.get("id")):
            continue
        hit = cameras._last_list(s["id"])
        for d in _devices_of(hit[1] if hit else None):
            out.append({**d, "site": s["id"], "site_name": s.get("name") or s["id"]})
    return out


def fits(e, devices=None):
    """The devices this file is for, with how their version compares."""
    out = []
    for d in devices if devices is not None else _site_devices():
        if model_fits(d["model"], e["models"]):
            out.append({**d, "compare": compare(d["firmware"], e["version"])})
    return out


# ---- update jobs -------------------------------------------------------------------------
def _jobs_path():
    return os.path.join(DIR, "jobs.json")


def _load_jobs():
    for j in _read_json(_jobs_path(), []):
        if isinstance(j, dict) and j.get("site") and j.get("key"):
            if j.get("state") == "running":
                j.update(state="done", result="unknown", finished=int(time.time()),
                         message="The hub restarted during this update. Look at the device: its page shows the version it runs.")
            _jobs[(j["site"], j["key"])] = j


def _save_jobs():
    with _lock:
        data = [dict(j) for j in _jobs.values()]
    _write_json(_jobs_path(), sorted(data, key=lambda j: j.get("started") or 0)[-300:])


def job_of(site_id, key):
    with _lock:
        j = _jobs.get((site_id, key))
        return dict(j, log=list(j.get("log") or []), checks=list(j.get("checks") or [])) if j else None


def _say(job, text):
    job.setdefault("log", []).append([int(time.time()), text])
    del job["log"][:-60]


def _sa_hour():
    return (time.gmtime().tm_hour + 2) % 24


def _night():
    h = _sa_hour()
    return h >= NIGHT_FROM or h < NIGHT_TO


def _site(site_id):
    site = hubconfig.get_site(site_id)
    if not site:
        raise FwError("unknown site", 404)
    return site


def _site_call(site, method, path, timeout=(5, 60), **kw):
    try:
        r = requests.request(method, siteapi.base_url(site) + path, headers=siteapi.headers(site, kw.pop("headers", None)),
                             timeout=timeout, **kw)
    except requests.RequestException as e:
        raise FwError(f"site unreachable: {e.__class__.__name__}", 502) from None
    if r.status_code in (404, 405) and "json" not in r.headers.get("Content-Type", ""):
        raise FwError("this site's Netwatch is too old for firmware updates — update it", 501)
    try:
        j = r.json()
    except ValueError:
        raise FwError(f"the site gave a bad answer (HTTP {r.status_code})", 502) from None
    return r.status_code, j


def preflight(site, key, e):
    q = {"version": e["version"], "models": ",".join(e["models"])}
    status, j = _site_call(site, "GET", f"/api/cameras/{quote(key, safe='')}/firmware", params=q)
    if status != 200:
        raise FwError(j.get("error") or f"the site could not check the device (HTTP {status})", status if status < 500 else 502)
    return j


def start(site_id, key, sha, confirm_text, region_ok, who="hub"):
    site = _site(site_id)
    e = entry(sha)
    if str(confirm_text or "").strip().upper() != "CHANGE":
        raise FwError("type CHANGE to confirm the update")
    if not region_ok:
        raise FwError(f"confirm that this device was bought for the file's region ({e['region']})")
    with _lock:
        if any(j["site"] == site_id and j["state"] == "running" for j in _jobs.values()):
            raise FwError("another update is running on this site; one at a time", 409)
        if not e.get("proven") and any(j["sha256"] == sha and j["state"] == "running" for j in _jobs.values()):
            raise FwError("this file is being tried on its test unit; wait until that update is checked", 409)
    pre = preflight(site, key, e)
    if pre.get("problems"):
        raise FwError("Not started: " + "; ".join(pre["problems"]) + ".", 409)
    if pre.get("recorder") and _night():
        raise FwError("Recorders are not updated at night (19:00–06:00): it stops recording, and nobody could go and look if it doesn't come back.", 409)
    job = {"id": uuid.uuid4().hex[:12], "site": site_id, "key": key, "name": pre.get("name") or key,
           "model": pre.get("model") or "", "from": pre.get("firmware") or "", "to": e["version"], "sha256": sha,
           "recorder": bool(pre.get("recorder")), "test_unit": not e.get("proven"), "who": who,
           "state": "running", "step": "backup", "staged": 0, "bytes": e["bytes"], "rate": None, "sent": 0,
           "percent": None, "started": int(time.time()), "finished": None, "result": "", "message": "",
           "checks": [], "log": []}
    with _lock:
        if any(j["site"] == site_id and j["state"] == "running" for j in _jobs.values()):
            raise FwError("another update is running on this site; one at a time", 409)
        _jobs[(site_id, key)] = job
    _save_jobs()
    threading.Thread(target=_run, args=(site, job, e, str(confirm_text)), daemon=True, name=f"fw-{site_id}").start()
    return job


def _run(site, job, e, word):
    try:
        _steps(site, job, e, word)
    except FwError as ex:
        job.update(state="done", result="failed", message=str(ex))
        _say(job, f"stopped: {ex}")
    except Exception as ex:  # noqa: BLE001 - a job must end with a state
        job.update(state="done", result="failed", message=f"Stopped: {ex.__class__.__name__}: {ex}")
        _say(job, f"stopped: {ex}")
    finally:
        job["finished"] = job.get("finished") or int(time.time())
        if job["result"] == "ok":
            _mark(job["sha256"], "proven", {"site": job["site"], "key": job["key"], "name": job["name"], "ts": job["finished"]})
        elif job["result"] in ("failed", "warn") and job.get("device_touched"):
            _mark(job["sha256"], "failed", {"site": job["site"], "key": job["key"], "name": job["name"], "ts": job["finished"],
                                            "why": job["message"][:200]})
        _save_jobs()
        print(f"[camfw] {job['site']}/{job['key']} {job['to']}: {job['result']} — {job['message']}", flush=True)


def _stage(site, job, e):
    """The file to the site Pi in pieces, from where its copy ends (it resumes)."""
    sha, size, path = e["sha256"], e["bytes"], _file(e["sha256"])
    status, j = _site_call(site, "GET", f"/api/firmware/{sha}")
    if status != 200:
        raise FwError(j.get("error") or "the site could not say what it has", 502)
    have = int(j.get("have") or 0)
    if j.get("done"):
        job["staged"] = size
        _say(job, "the file is already on the site Pi")
        return
    _say(job, f"sending {size >> 20} MB to the site Pi" + (f" (from {have >> 20} MB)" if have else ""))
    t0, start_have, tries = time.time(), have, 0
    with open(path, "rb") as f:
        while have < size:
            f.seek(have)
            piece = f.read(PIECE)
            try:
                status, j = _site_call(site, "PUT", f"/api/firmware/{sha}", timeout=(10, 180), data=piece,
                                       params={"offset": have, "total": size},
                                       headers={"Content-Type": "application/octet-stream"})
            except FwError as ex:
                status, j = 0, {"error": str(ex)}
            if status == 200:
                have, tries = int(j.get("have") or 0), 0
                if j.get("done"):
                    have = size
            elif status == 409 and "have" in j:
                have = int(j["have"])               # resync with what the site Pi has
            elif status == 422:
                have, tries = 0, tries + 1          # damaged on the way: start again
                _say(job, "the copy was damaged on the way; sending it again")
            else:
                tries += 1
                if tries >= PIECE_TRIES:
                    raise FwError(f"sending the file to the site Pi failed: {j.get('error') or status}")
                time.sleep(min(60, 5 * tries))
            job["staged"] = have
            dt = time.time() - t0
            job["rate"] = int((have - start_have) / dt) if dt > 1 else None
    _say(job, f"the file is on the site Pi ({int(time.time() - t0)} s)")


def _steps(site, job, e, word):
    key = job["key"]
    # 1. a configuration backup first (a recent one counts)
    idx = camconfig.list_(site["id"], key)
    if idx and time.time() - idx[-1]["ts"] < camconfig.BEFORE_CHANGE_AGE:
        _say(job, "a recent backup exists")
    else:
        _say(job, "taking a configuration backup")
        try:
            camconfig.take(site, key, "change", job["who"])
        except camconfig.CamBackupError as ex:
            raise FwError(f"no update without a backup, and the backup failed: {ex}") from None
        _say(job, "backup taken")
    # 2. the file to the site Pi
    job["step"] = "stage"
    _stage(site, job, e)
    # 3. the site does the device work
    job["step"] = "send"
    status, j = _site_call(site, "POST", f"/api/cameras/{quote(key, safe='')}/firmware", timeout=(5, 60),
                           json={"sha256": e["sha256"], "version": e["version"], "models": e["models"], "confirm_text": word})
    if status != 200:
        raise FwError(j.get("error") or f"the site did not start the update (HTTP {status})")
    sid = (j.get("job") or {}).get("id")
    job["device_touched"] = True
    _say(job, "the site Pi is updating the device")
    end, misses = time.time() + SITE_JOB_MAX_S, 0
    while time.time() < end:
        time.sleep(POLL_S)
        try:
            status, j = _site_call(site, "GET", f"/api/cameras/{quote(key, safe='')}/firmware/job", timeout=(5, 20))
        except FwError:
            misses += 1
            continue
        sj = j.get("job") or {}
        if sj.get("id") != sid:
            misses += 1
            if misses > 60:
                raise FwError("the site Pi lost track of the update (did it restart?) — look at the device")
            continue
        for k in ("step", "sent", "percent", "checks", "now"):
            if k in sj:
                job[k] = sj[k]
        job["site_log"] = sj.get("log") or []
        if sj.get("state") == "done":
            job.update(state="done", result=sj.get("result") or "failed", message=sj.get("message") or "")
            break
    else:
        raise FwError("the update took longer than 45 minutes — look at the device")
    # 4. the settings after (for "what changed"), best effort
    if job["result"] in ("ok", "warn"):
        try:
            camconfig.snapshot(site, key)
        except camconfig.CamBackupError:
            pass


# ---- routes ----------------------------------------------------------------------------
def _fail(ex):
    return jsonify({"ok": False, "error": str(ex)}), ex.status


def _with_fits(e, devices):
    f = fits(e, devices)
    return {**e, "fits": f, "newer": sum(1 for d in f if d["compare"] == "newer")}


@bp.route("/api/hub/firmware")
def api_library():
    devices = _site_devices()
    with _lock:
        running = [dict(j, log=None) for j in _jobs.values() if j["state"] == "running"]
    return jsonify({"ok": True, "files": [_with_fits(e, devices) for e in reversed(library())],
                    "regions": list(REGIONS), "running": running, "piece": UP_PIECE})


@bp.route("/api/hub/firmware/<sha>", methods=["DELETE"])
def api_library_remove(sha):
    try:
        remove(sha)
    except FwError as ex:
        return _fail(ex)
    return jsonify({"ok": True})


@bp.route("/api/hub/firmware/uploads", methods=["POST"])
def api_upload_begin():
    b = request.get_json(silent=True) or {}
    try:
        return jsonify({"ok": True, "id": upload_begin(b.get("name"), b.get("bytes")), "piece": UP_PIECE})
    except (FwError, ValueError, TypeError) as ex:
        return _fail(ex if isinstance(ex, FwError) else FwError("bad size"))


@bp.route("/api/hub/firmware/uploads/<uid>", methods=["PUT"])
def api_upload_piece(uid):
    try:
        offset = int(request.args.get("offset", ""))
        n = int(request.headers.get("Content-Length") or 0)
    except ValueError:
        return _fail(FwError("offset is needed"))
    if not 0 < n <= UP_PIECE:
        return _fail(FwError("bad piece"))
    data = request.environ["wsgi.input"].read(n)
    try:
        return jsonify({"ok": True, "have": upload_piece(uid, offset, data)})
    except FwError as ex:
        return _fail(ex)


@bp.route("/api/hub/firmware/uploads/<uid>/finish", methods=["POST"])
def api_upload_finish(uid):
    b = request.get_json(silent=True) or {}
    try:
        e = upload_finish(uid, b.get("version"), b.get("models"), b.get("region"), b.get("notes"))
    except FwError as ex:
        return _fail(ex)
    return jsonify({"ok": True, "file": _with_fits(e, _site_devices())})


@bp.route("/api/hub/sites/<site_id>/cameras/<path:key>/firmware", methods=["GET", "POST"])
def api_device_firmware(site_id, key):
    """GET: the device, the library files for its model, and its update (if any).
    POST {sha256, confirm_text, region_ok}: update it."""
    try:
        _site(site_id)
        if request.method == "POST":
            b = request.get_json(silent=True) or {}
            j = start(site_id, key, str(b.get("sha256") or ""), b.get("confirm_text"), bool(b.get("region_ok")))
            return jsonify({"ok": True, "job": j})
        hit = cameras._last_list(site_id)
        dev = next((d for d in _devices_of(hit[1] if hit else None) if d["key"] == key), None)
        files = []
        for e in reversed(library()):
            if dev and model_fits(dev["model"], e["models"]):
                files.append({**e, "compare": compare(dev["firmware"], e["version"]),
                              "proven_here": any(p["site"] == site_id and p["key"] == key for p in e.get("proven") or [])})
        with _lock:
            busy = next((dict(j, log=None) for j in _jobs.values() if j["site"] == site_id and j["state"] == "running"
                         and j["key"] != key), None)
        return jsonify({"ok": True, "device": dev, "files": files, "job": job_of(site_id, key), "site_busy": busy,
                        "night": _night(), "library_size": len(library())})
    except FwError as ex:
        return _fail(ex)


def init():
    _load_jobs()
