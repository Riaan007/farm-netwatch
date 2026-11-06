"""Camera pictures for the Control Center: forwards a site's camera API
(app/camera_routes.py + app/campics.py on the site Pi) with the hub's key.

Cameras are only reachable from their site's LAN, so the site Pi reads every
picture and the hub never sees a camera password. What the hub adds:
  - a short memory cache, so several views of one picture cross the farm link once;
  - a picture over 150 KB (firmware that can't send small ones) is shrunk here
    before it reaches the browser — the hub has Pillow, the site Pis don't;
  - one saved copy per camera (written at most hourly, topped up every 6 h by a
    gentle background pull) and each site's last camera list, so while a whole
    site is offline the Control Center still shows what each camera looked at.
Pictures are personal information (POPIA): they go out with no-store, and the
saved copies live in /data/camera_pics/<site>/ and nowhere else.
"""
import collections
import hashlib
import io
import json
import os
import threading
import time
from urllib.parse import quote

import requests
from flask import Blueprint, Response, jsonify, request

import camconfig
import hubconfig
import siteapi

bp = Blueprint("cameras", __name__)

PIC_DIR = os.path.join(os.environ.get("HUB_DATA", "/data"), "camera_pics")
LIST_TTL = 30
PIC_TTL = {"thumb": 60, "full": 120}
MEM_CAP = 64 << 20
MAX_BYTES = 8 << 20
SHRINK_OVER = 150 << 10
SHRINK_TO = 960
SAVE_EVERY = 3600
PULL_EVERY = 6 * 3600
LIST_SAVE_EVERY = 1800
PASS_HEADERS = ("X-Picture-Ts", "X-Picture-Route", "X-Picture-Size", "X-Picture-Stale", "X-Picture-Why")

_lock = threading.Lock()
_lists = {}                          # site id -> (ts, listing)
_pics = collections.OrderedDict()    # (site, key, size) -> (ts, bytes, headers)
_saved_at = {}                       # (site, key) -> time the hub last saved its copy
_list_saved_at = {}


def _site(site_id):
    site = hubconfig.get_site(site_id)
    if not site:
        return None, (jsonify({"ok": False, "error": "unknown site"}), 404)
    return site, None


def _too_old():
    return jsonify({"ok": False, "legacy": True,
                    "error": "this site's Netwatch is too old for camera pictures — update it (docker compose pull)"}), 501


def _dir(site_id):
    return os.path.join(PIC_DIR, hashlib.sha1(site_id.encode()).hexdigest()[:16])


def _pic_path(site_id, key):
    return os.path.join(_dir(site_id), hashlib.sha1(key.encode()).hexdigest()[:24] + ".jpg")


def _is_jpeg(b):
    return len(b) > 3 and b[0] == 0xFF and b[1] == 0xD8


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "wb") as f:
        f.write(data)
    os.replace(path + ".tmp", path)


def _shrink(data, headers):
    """A big "small" picture made small: at most SHRINK_TO px wide/high, same shape."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im.draft("RGB", (SHRINK_TO, SHRINK_TO))      # JPEG: decode at 1/2, 1/4 or 1/8 — cheap
        im = im.convert("RGB")
        im.thumbnail((SHRINK_TO, SHRINK_TO))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=78, optimize=True)
        small = out.getvalue()
    except Exception:  # noqa: BLE001 - a picture we can't shrink is still a picture
        return data, headers
    if len(small) >= len(data):
        return data, headers
    return small, {**headers, "X-Picture-Size": f"{im.width}x{im.height}"}


def _send(data, headers, stale=None, route=None):
    resp = Response(data, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "no-store"
    for h in PASS_HEADERS:
        if headers.get(h):
            resp.headers[h] = headers[h]
    if stale is not None:
        resp.headers["X-Picture-Stale"] = "1" if stale else "0"
    if route:
        resp.headers["X-Picture-Route"] = route
    return resp


def _mem_put(k, data, headers):
    with _lock:
        _pics[k] = (time.time(), data, headers)
        _pics.move_to_end(k)
        total = sum(len(v[1]) for v in _pics.values())
        while total > MEM_CAP and len(_pics) > 1:
            _old, v = _pics.popitem(last=False)
            total -= len(v[1])


def _maybe_save(site_id, key, data, headers):
    """The hub's own copy, for when the whole site is offline (at most hourly)."""
    if headers.get("X-Picture-Stale") == "1":
        return
    sk = (site_id, key)
    with _lock:
        if time.time() - _saved_at.get(sk, 0) < SAVE_EVERY:
            return
        _saved_at[sk] = time.time()
    try:
        _write(_pic_path(site_id, key), data)
    except OSError as e:
        print(f"[cameras] could not save a picture: {e}", flush=True)


def _saved(site_id, key):
    p = _pic_path(site_id, key)
    try:
        with open(p, "rb") as f:
            data = f.read(MAX_BYTES + 1)
        ts = os.path.getmtime(p)
    except OSError:
        return None
    return (data, ts) if _is_jpeg(data) and len(data) <= MAX_BYTES else None


def _remember_list(site_id, listing):
    with _lock:
        _lists[site_id] = (time.time(), listing)
        due = time.time() - _list_saved_at.get(site_id, 0) >= LIST_SAVE_EVERY
        if due:
            _list_saved_at[site_id] = time.time()
    if due:
        try:
            _write(os.path.join(_dir(site_id), "listing.json"), json.dumps(listing).encode())
        except OSError:
            pass


def _last_list(site_id):
    with _lock:
        hit = _lists.get(site_id)
    if hit:
        return hit
    try:
        p = os.path.join(_dir(site_id), "listing.json")
        with open(p) as f:
            return os.path.getmtime(p), json.load(f)
    except (OSError, ValueError):
        return None


@bp.route("/api/hub/sites/<site_id>/cameras")
def site_cameras(site_id):
    """The site's cameras and recorders (cached ~30 s). While the site can't be
    reached, its last list is returned with `stale: true` so the page can still
    show the saved pictures."""
    site, err = _site(site_id)
    if err:
        return err
    with _lock:
        hit = _lists.get(site_id)
    if hit and request.args.get("fresh") != "1" and time.time() - hit[0] < LIST_TTL:
        return jsonify({**hit[1], "cached_at": int(hit[0])})
    try:
        r = requests.get(siteapi.base_url(site) + "/api/cameras", headers=siteapi.headers(site), timeout=(5, 30))
    except requests.RequestException as e:
        last = _last_list(site_id)
        if last:
            return jsonify({**last[1], "stale": True, "stale_since": int(last[0]),
                            "error": f"the site can't be reached ({e.__class__.__name__}) — showing its last camera list"})
        return jsonify({"ok": False, "error": f"site unreachable: {e.__class__.__name__}"}), 502
    if r.status_code == 404 and "json" not in r.headers.get("Content-Type", ""):
        return _too_old()
    if r.status_code == 401:
        return jsonify({"ok": False, "error": "the site doesn't accept this hub's key yet"}), 502
    try:
        listing = r.json()
    except ValueError:
        return jsonify({"ok": False, "error": "site returned a bad reply"}), 502
    if r.ok and isinstance(listing, dict) and isinstance(listing.get("cameras"), list):
        _remember_list(site_id, listing)
    return jsonify(listing), r.status_code


@bp.route("/api/hub/sites/<site_id>/cameras/<path:key>/picture")
def site_camera_picture(site_id, key):
    site, err = _site(site_id)
    if err:
        return err
    size = "full" if request.args.get("size") == "full" else "thumb"
    fresh = request.args.get("fresh") == "1"
    view = request.args.get("view", "1")
    if not view.isdigit() or not 1 <= int(view) <= 9:
        return jsonify({"ok": False, "error": "view must be 1-9"}), 400
    vkey = key if view == "1" else f"{key}#v{view}"      # one cache entry / saved copy per lens
    mk = (site_id, vkey, size)
    with _lock:
        hit = _pics.get(mk)
        if hit:
            _pics.move_to_end(mk)
    if hit and time.time() - hit[0] < (2 if fresh else PIC_TTL[size]):
        return _send(hit[1], hit[2])
    params = {"size": size, **({"fresh": "1"} if fresh else {}), **({"view": view} if view != "1" else {})}
    try:
        r = requests.get(f"{siteapi.base_url(site)}/api/cameras/{quote(key, safe='')}/picture",
                         params=params, headers=siteapi.headers(site), timeout=(5, 45), stream=True)
    except requests.RequestException as e:
        return _offline(site_id, vkey, size, hit, f"the site can't be reached ({e.__class__.__name__})")
    try:
        ctype = r.headers.get("Content-Type", "")
        if r.status_code == 404 and "json" not in ctype:
            return _too_old()
        if r.status_code == 401:
            return jsonify({"ok": False, "error": "the site doesn't accept this hub's key yet"}), 502
        if not r.ok:
            try:
                body = r.json()
            except ValueError:
                body = {"ok": False, "error": f"site answered HTTP {r.status_code}"}
            if r.status_code >= 500 or body.get("why") == "failed":
                return _offline(site_id, vkey, size, hit, body.get("error") or "the site could not read the picture",
                                status=r.status_code, body=body)
            return jsonify(body), r.status_code
        data, total = [], 0
        for chunk in r.iter_content(65536):
            total += len(chunk)
            if total > MAX_BYTES:
                return jsonify({"ok": False, "error": "the picture is too large"}), 502
            data.append(chunk)
    except requests.RequestException as e:
        return _offline(site_id, vkey, size, hit, f"the site stopped answering ({e.__class__.__name__})")
    finally:
        r.close()
    data = b"".join(data)
    if not _is_jpeg(data):
        return jsonify({"ok": False, "error": "the site's answer was not a picture"}), 502
    headers = {h: r.headers.get(h, "") for h in PASS_HEADERS}
    if size == "thumb" and len(data) > SHRINK_OVER:
        data, headers = _shrink(data, headers)
    _mem_put(mk, data, headers)
    if size == "thumb":
        _maybe_save(site_id, vkey, data, headers)
    return _send(data, headers)


def _offline(site_id, key, size, hit, why, status=502, body=None):
    """The site can't give a picture: the best older one we have, flagged stale."""
    if hit:
        return _send(hit[1], hit[2], stale=True)
    saved = _saved(site_id, key)
    if saved:
        data, ts = saved
        resp = _send(data, {"X-Picture-Ts": str(int(ts))}, stale=True, route="hub-saved")
        resp.headers["X-Picture-Why"] = "failed"
        return resp
    return jsonify(body or {"ok": False, "error": why, "why": "failed"}), status


@bp.route("/api/hub/sites/<site_id>/cameras/refresh", methods=["POST"])
def site_cameras_refresh(site_id):
    site, err = _site(site_id)
    if err:
        return err
    try:
        r = requests.post(siteapi.base_url(site) + "/api/cameras/refresh", headers=siteapi.headers(site),
                          json={}, timeout=(5, 20))
    except requests.RequestException as e:
        return jsonify({"ok": False, "error": f"site unreachable: {e.__class__.__name__}"}), 502
    if r.status_code == 404 and "json" not in r.headers.get("Content-Type", ""):
        return _too_old()
    try:
        return jsonify(r.json()), r.status_code
    except ValueError:
        return jsonify({"ok": False, "error": "site returned a bad reply"}), 502


def _forward(site_id, path, method, body=None, timeout=(5, 60), backup_key=None):
    """Pass one camera-settings call to the site Pi (which does all the device work).
    `backup_key`: risky work on that device — take its backup first (camconfig.py),
    unless a recent one exists. A failed backup never blocks the change; the reply says."""
    site, err = _site(site_id)
    if err:
        return err
    note = camconfig.before_change(site, backup_key) if backup_key else ""
    try:
        r = requests.request(method, siteapi.base_url(site) + path, headers=siteapi.headers(site),
                             params=request.args if method == "GET" else None, json=body, timeout=timeout)
    except requests.RequestException as e:
        return jsonify({"ok": False, "error": f"site unreachable: {e.__class__.__name__}"}), 502
    if r.status_code in (404, 405) and "json" not in r.headers.get("Content-Type", ""):
        return jsonify({"ok": False, "legacy": True,
                        "error": "this site's Netwatch is too old for camera settings — update it"}), 501
    if r.status_code == 401:
        return jsonify({"ok": False, "error": "the site refused the hub's key — see the site's Backups tab"}), 502
    try:
        j = r.json()
    except ValueError:
        return jsonify({"ok": False, "error": "site returned a bad reply"}), 502
    if note and isinstance(j, dict):
        j["backup_note"] = note
    return jsonify(j), r.status_code


@bp.route("/api/hub/sites/<site_id>/cameras/<path:key>/settings", methods=["GET", "POST"])
def site_camera_settings(site_id, key):
    body = (request.get_json(silent=True) or {}) if request.method == "POST" else None
    risky = bool(body) and body.get("section") == "access"
    return _forward(site_id, f"/api/cameras/{quote(key, safe='')}/settings", request.method, body,
                    backup_key=key if risky else None)


@bp.route("/api/hub/sites/<site_id>/cameras/<path:key>/ptz", methods=["GET", "POST"])
def site_camera_ptz(site_id, key):
    body = (request.get_json(silent=True) or {}) if request.method == "POST" else None
    return _forward(site_id, f"/api/cameras/{quote(key, safe='')}/ptz", request.method, body, timeout=(5, 20))


@bp.route("/api/hub/sites/<site_id>/cameras/<path:key>/account", methods=["POST"])
def site_camera_account(site_id, key):
    """The guided Netwatch account job — the site Pi does it; this only forwards."""
    return _forward(site_id, f"/api/cameras/{quote(key, safe='')}/account", "POST",
                    request.get_json(silent=True) or {}, timeout=(5, 90), backup_key=key)


@bp.route("/api/hub/sites/<site_id>/cameras/<path:key>/channels", methods=["GET", "POST"])
def site_recorder_channels(site_id, key):
    body = (request.get_json(silent=True) or {}) if request.method == "POST" else None
    return _forward(site_id, f"/api/cameras/{quote(key, safe='')}/channels", request.method, body, timeout=(5, 120),
                    backup_key=key if body else None)


@bp.route("/api/hub/sites/<site_id>/cameras/<path:key>/password", methods=["GET", "POST"])
def site_camera_password(site_id, key):
    """The password job — the site Pi does it; the hub only forwards (never logs bodies)."""
    body = (request.get_json(silent=True) or {}) if request.method == "POST" else None
    return _forward(site_id, f"/api/cameras/{quote(key, safe='')}/password", request.method, body, timeout=(5, 300),
                    backup_key=key if body else None)


@bp.route("/api/hub/sites/<site_id>/cameras/<path:key>/isapi", methods=["GET", "POST"])
def site_camera_isapi(site_id, key):
    """The ISAPI explorer — the site Pi does it; this only forwards (GET ?path= passes on)."""
    body = (request.get_json(silent=True) or {}) if request.method == "POST" else None
    return _forward(site_id, f"/api/cameras/{quote(key, safe='')}/isapi", request.method, body,
                    backup_key=key if body and not body.get("preview") else None)


@bp.route("/api/hub/sites/<site_id>/camera-standard", methods=["GET", "POST"])
def site_camera_standard(site_id):
    """The FarmCamSA standard and its check — the site Pi does the work; this forwards."""
    return _forward(site_id, "/api/camera-standard", request.method, {} if request.method == "POST" else None)


@bp.route("/api/hub/sites/<site_id>/camera-jobs", methods=["POST"])
def site_camera_jobs(site_id):
    return _forward(site_id, "/api/camera-jobs", "POST", request.get_json(silent=True) or {}, timeout=(5, 60))


@bp.route("/api/hub/sites/<site_id>/camera-jobs/<jid>")
def site_camera_job(site_id, jid):
    return _forward(site_id, f"/api/camera-jobs/{quote(jid, safe='')}", "GET")


@bp.route("/api/hub/sites/<site_id>/camera-jobs/<jid>/<action>", methods=["POST"])
def site_camera_job_action(site_id, jid, action):
    if action not in ("stop", "undo"):
        return jsonify({"ok": False, "error": "unknown action"}), 404
    return _forward(site_id, f"/api/camera-jobs/{quote(jid, safe='')}/{action}", "POST", {})


@bp.route("/api/hub/sites/<site_id>/cameras/<path:key>/changes")
def site_camera_changes(site_id, key):
    return _forward(site_id, f"/api/cameras/{quote(key, safe='')}/changes", "GET")


@bp.route("/api/hub/sites/<site_id>/cameras/<path:key>/changes/<cid>/undo", methods=["POST"])
def site_camera_change_undo(site_id, key, cid):
    return _forward(site_id, f"/api/cameras/{quote(key, safe='')}/changes/{quote(cid, safe='')}/undo", "POST",
                    request.get_json(silent=True) or {})


# ---- the background top-up of the hub's saved copies -------------------------------
def _pull_site(site):
    """One site: fetch each camera's picture the hub has no recent copy of. The
    site serves these from its own memory, so the cameras see no extra load; one
    at a time with a pause, so the farm link barely notices."""
    try:
        r = requests.get(siteapi.base_url(site) + "/api/cameras", headers=siteapi.headers(site), timeout=(5, 30))
        listing = r.json() if r.ok else None
    except (requests.RequestException, ValueError):
        return
    if not (isinstance(listing, dict) and listing.get("enabled") and isinstance(listing.get("cameras"), list)):
        return
    _remember_list(site["id"], listing)
    jobs = []
    for cam in listing["cameras"]:
        views = cam.get("views") or [{"n": 1, "picture": cam.get("picture")}]
        for v in views:
            jobs.append((cam.get("key"), int(v.get("n") or 1), v.get("picture") or {}))
    for key, view, pic in jobs:
        if not key or not pic.get("ts") or pic.get("route") == "saved":
            continue
        vkey = key if view == 1 else f"{key}#v{view}"
        p = _pic_path(site["id"], vkey)
        try:
            if time.time() - os.path.getmtime(p) < PULL_EVERY * 0.9:
                continue
        except OSError:
            pass
        try:
            rr = requests.get(f"{siteapi.base_url(site)}/api/cameras/{quote(key, safe='')}/picture",
                              params={"size": "thumb", **({"view": view} if view > 1 else {})},
                              headers=siteapi.headers(site), timeout=(5, 45))
        except requests.RequestException:
            return                                     # the site went away: try again next round
        if rr.ok and _is_jpeg(rr.content) and len(rr.content) <= MAX_BYTES and rr.headers.get("X-Picture-Stale") != "1":
            data, _h = (_shrink(rr.content, {}) if len(rr.content) > SHRINK_OVER else (rr.content, {}))
            try:
                _write(p, data)
                with _lock:
                    _saved_at[(site["id"], vkey)] = time.time()
            except OSError:
                return
        time.sleep(1.0)


def _pull_loop(reachable):
    time.sleep(120)
    while True:
        for site in hubconfig.load().get("sites", []):
            if site.get("enabled", True) and site.get("id") and site.get("vpn_ip") and reachable(site["id"]):
                try:
                    _pull_site(site)
                except Exception as e:  # noqa: BLE001 - one site's trouble must not stop the others
                    print(f"[cameras] pull {site.get('id')}: {e}", flush=True)
        time.sleep(PULL_EVERY)


def start(reachable):
    """reachable(site_id) -> bool, from the poller's last status poll."""
    threading.Thread(target=_pull_loop, args=(reachable,), daemon=True, name="camera-pull").start()
