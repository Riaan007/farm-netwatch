"""HTTP API behind the Cameras tab — see app/campics.py.

Unlike /api/devices, EVERY route here needs the site login or the hub key: a
camera picture is personal information (POPIA), and the listing says which
logins work. Pictures are sent with Cache-Control: no-store so a browser keeps
no copy on its disk either.
"""
from flask import Blueprint, Response, jsonify, request

import campics
import siteauth

bp = Blueprint("cameras", __name__)
guard = siteauth.required
engine = campics.engine


@bp.route("/api/cameras")
@guard
def api_cameras():
    """Every camera and recorder: the way to its picture, or why there is none."""
    return jsonify(engine.listing())


@bp.route("/api/cameras/<path:key>/picture")
@guard
def api_camera_picture(key):
    """One camera's picture. ?size=full for the large one (on demand only);
    ?fresh=1 reads a new one now. A camera that is offline (or fails) answers
    with its last good picture, flagged by X-Picture-Stale. ?view=2 is a 2-lens
    camera's second lens."""
    size = "full" if request.args.get("size") == "full" else "thumb"
    try:
        view = int(request.args.get("view") or 1)
    except ValueError:
        return jsonify({"ok": False, "error": "view must be a number"}), 400
    pic, flags, err, status = engine.picture(key, size, request.args.get("fresh") == "1", view=view)
    if pic is None:
        return jsonify({"ok": False, **err}), status
    resp = Response(pic.data, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Picture-Ts"] = str(int(pic.ts))
    resp.headers["X-Picture-Route"] = pic.route
    resp.headers["X-Picture-Size"] = f"{pic.w}x{pic.h}"
    resp.headers["X-Picture-Stale"] = "1" if flags.get("stale") else "0"
    if flags.get("why"):
        resp.headers["X-Picture-Why"] = flags["why"]
    return resp


@bp.route("/api/cameras/refresh", methods=["POST"])
@guard
def api_cameras_refresh():
    """Read every camera's picture again now, in the background, one at a time."""
    engine.refresh_now()
    return jsonify({"ok": True, "started": True})


# ---- settings (hiksettings.py, Phase 3) -----------------------------------------------
import hiksettings  # noqa: E402

settings = hiksettings.settings


def _who():
    """Who made a change: the hub (with the hub user it names, trusted only with the
    hub key) or someone logged in on the site page."""
    if siteauth.hub_ok(request):
        u = (request.headers.get("X-Netwatch-Hub-User") or "").strip()[:60]
        return "hub" + (f": {u}" if u else "")
    return "site page"


def _err(e):
    return jsonify({"ok": False, "error": str(e), **e.extra}), e.status


@bp.route("/api/cameras/<path:key>/settings", methods=["GET", "POST"])
@guard
def api_camera_settings(key):
    """GET ?lens=1&group=basic|detect|record|recorder: the camera's settings — basic =
    picture, text on video, time and streams; detect = motion, intrusion, line crossing,
    tampering, privacy masks, strobe and siren; record = its recording on its recorder;
    recorder (a recorder's own key) = the recorder's disk settings; security = the
    device's users, ports and Hik-Connect (shown) and its login lock, SSH and UPnP —
    with what each may be set to (from the device's own capabilities).
    POST {lens, section, changes: {field: value}, expect: {field: value seen}, confirm}:
    change them — needs the site's "Camera settings" switch; careful sections need
    confirm; security changes also need the "Risky camera work" switch and confirm_text
    (the word the page shows); the camera is read back to prove the change took."""
    try:
        if request.method == "GET":
            return jsonify(settings.read(key, int(request.args.get("lens") or 1),
                                         str(request.args.get("group") or "basic"),
                                         route=str(request.args.get("route") or "") or None))
        b = request.get_json(force=True, silent=True) or {}
        return jsonify(settings.apply(key, int(b.get("lens") or 1), str(b.get("section") or ""),
                                      b.get("changes") if isinstance(b.get("changes"), dict) else {},
                                      who=_who(), expect=b.get("expect") if isinstance(b.get("expect"), dict) else {},
                                      confirm=b.get("confirm") is True, route=str(b.get("route") or "") or None,
                                      confirm_text=str(b.get("confirm_text") or "")[:20]))
    except ValueError:
        return jsonify({"ok": False, "error": "lens must be a number"}), 400
    except hiksettings.SettingsError as e:
        return _err(e)


@bp.route("/api/cameras/<path:key>/changes")
@guard
def api_camera_changes(key):
    """This camera's settings changes, newest first (before → after, who, Undo state)."""
    return jsonify({"ok": True, "changes": settings.changes(key)})


@bp.route("/api/cameras/<path:key>/changes/<cid>/undo", methods=["POST"])
@guard
def api_camera_change_undo(key, cid):
    """Put back what one change replaced — only if the camera still shows that change."""
    b = request.get_json(force=True, silent=True) or {}
    if b.get("confirm") is not True:
        return jsonify({"ok": False, "error": "confirm the undo first", "needs_confirm": True}), 400
    try:
        return jsonify(settings.undo(key, cid, who=_who()))
    except hiksettings.SettingsError as e:
        return _err(e)


@bp.route("/api/cameras/<path:key>/ptz", methods=["GET", "POST"])
@guard
def api_camera_ptz(key):
    """GET ?lens=1: what PTZ the camera has and its presets.
    POST {lens, action: "move", pan, tilt, zoom (-100..100), ms} — a move that stops by
    itself — or {action: "goto", preset}. Needs the site's "Camera settings" switch."""
    try:
        if request.method == "GET":
            return jsonify(settings.ptz(key, int(request.args.get("lens") or 1),
                                        route=str(request.args.get("route") or "") or None))
        b = request.get_json(force=True, silent=True) or {}
        return jsonify(settings.ptz_do(key, int(b.get("lens") or 1), str(b.get("action") or ""),
                                       pan=b.get("pan", 0), tilt=b.get("tilt", 0), zoom=b.get("zoom", 0),
                                       ms=b.get("ms", 500), preset=b.get("preset"), who=_who(),
                                       route=str(b.get("route") or "") or None))
    except ValueError:
        return jsonify({"ok": False, "error": "lens must be a number"}), 400
    except hiksettings.SettingsError as e:
        return _err(e)


@bp.route("/api/cameras/<path:key>/account", methods=["POST"])
@guard
def api_camera_account(key):
    """POST {action: "create"|"remove", confirm_text}: the guided Netwatch account job
    (hiksecurity.py) — create an Operator user `netwatch` with a random password nobody
    sees, prove it logs in and switch Netwatch to it (the admin login is kept aside for
    risky jobs), or remove it and go back to admin. Needs both camera switches."""
    b = request.get_json(force=True, silent=True) or {}
    try:
        return jsonify(settings.account(key, str(b.get("action") or ""), who=_who(),
                                        confirm_text=str(b.get("confirm_text") or "")[:20]))
    except hiksettings.SettingsError as e:
        return _err(e)


@bp.route("/api/cameras/<path:key>/channels", methods=["GET", "POST"])
@guard
def api_recorder_channels(key):
    """A recorder's channels (hikchannels.py). GET: each channel's camera, address,
    login user and state, matched to Netwatch's devices, and the cameras Netwatch could
    add. POST {action: fix_login|move|add|remove, ch, camera, ip, port, confirm_text}:
    the channel's password always comes from Netwatch's saved admin login."""
    try:
        if request.method == "GET":
            return jsonify(settings.channels(key))
        b = request.get_json(force=True, silent=True) or {}
        return jsonify(settings.channel_do(key, str(b.get("action") or ""), ch=b.get("ch"), camera=b.get("camera"),
                                           ip=b.get("ip"), port=b.get("port"), who=_who(),
                                           confirm_text=str(b.get("confirm_text") or "")[:20]))
    except hiksettings.SettingsError as e:
        return _err(e)


@bp.route("/api/cameras/<path:key>/password", methods=["GET", "POST"])
@guard
def api_camera_password(key):
    """The password job. GET: the camera's admin user and the recorder channels that
    would get the new password. POST {new, again, confirm_text}: change it on the
    camera and those channels; Netwatch saves it. The password is never logged."""
    try:
        if request.method == "GET":
            return jsonify(settings.password_plan(key))
        b = request.get_json(force=True, silent=True) or {}
        return jsonify(settings.password(key, str(b.get("new") or "")[:64], str(b.get("again") or "")[:64],
                                         who=_who(), confirm_text=str(b.get("confirm_text") or "")[:20]))
    except hiksettings.SettingsError as e:
        return _err(e)


@bp.route("/api/cameras/<path:key>/isapi", methods=["GET", "POST"])
@guard
def api_camera_isapi(key):
    """The ISAPI explorer (hikexplorer.py). GET ?path=/ISAPI/…: the device's document
    with every secret hidden, and its capabilities. POST {path, doc, preview, confirm_text}:
    change VALUES only (same elements, same order); preview returns the change list.
    Needs Camera settings and Risky camera work; risky paths are refused."""
    try:
        if request.method == "GET":
            return jsonify(settings.explore_read(key, str(request.args.get("path") or "")))
        b = request.get_json(force=True, silent=True) or {}
        return jsonify(settings.explore_write(key, str(b.get("path") or ""), str(b.get("doc") or ""),
                                              preview=b.get("preview") is True, who=_who(),
                                              confirm_text=str(b.get("confirm_text") or "")[:20]))
    except hiksettings.SettingsError as e:
        return _err(e)


# ---- the FarmCamSA standard and bulk jobs (hikbulk.py, Phase 5) ------------------------
import hikbulk  # noqa: E402

bulk = hikbulk.jobs


def _berr(e):
    return jsonify({"ok": False, "error": str(e), **e.extra}), e.status


@bp.route("/api/camera-standard", methods=["GET", "POST"])
@guard
def api_camera_standard():
    """GET: the rules, the last check (per device, per rule: ok / off / n/a) and the
    recent jobs. POST: start a new check (reads every device, one at a time; writes nothing)."""
    try:
        if request.method == "GET":
            return jsonify(bulk.overview())
        return jsonify(bulk.start_check(who=_who()))
    except hikbulk.BulkError as e:
        return _berr(e)


@bp.route("/api/camera-jobs", methods=["POST"])
@guard
def api_camera_jobs():
    """Start a job: {kind: "rule", rule, keys, pause_s, stop_on_fail, confirm_text} brings
    the devices to one rule; {kind: "copy", source, lens, sections, keys, …} copies settings
    to cameras of the same model. One device at a time, each change read back, logged and
    undoable — and the whole job can be undone at once."""
    b = request.get_json(force=True, silent=True) or {}
    keys = [str(k)[:80] for k in b.get("keys") or [] if isinstance(k, str)]
    try:
        kind = str(b.get("kind") or "")
        if kind == "rule":
            return jsonify(bulk.start_rule(b.get("rule"), keys, who=_who(), pause_s=b.get("pause_s"),
                                           stop_on_fail=b.get("stop_on_fail") is True,
                                           confirm_text=str(b.get("confirm_text") or "")[:20]))
        if kind == "copy":
            secs = [str(x) for x in b.get("sections") or [] if isinstance(x, str)]
            return jsonify(bulk.start_copy(str(b.get("source") or ""), int(b.get("lens") or 1), secs, keys, who=_who(),
                                           pause_s=b.get("pause_s"), stop_on_fail=b.get("stop_on_fail") is True))
        return jsonify({"ok": False, "error": "unknown job kind"}), 400
    except ValueError:
        return jsonify({"ok": False, "error": "lens must be a number"}), 400
    except hikbulk.BulkError as e:
        return _berr(e)


@bp.route("/api/camera-jobs/<jid>")
@guard
def api_camera_job(jid):
    j = bulk.get(jid)
    return jsonify({"ok": True, "job": j}) if j else (jsonify({"ok": False, "error": "unknown job"}), 404)


@bp.route("/api/camera-jobs/<jid>/<action>", methods=["POST"])
@guard
def api_camera_job_action(jid, action):
    try:
        if action == "stop":
            return jsonify(bulk.stop(jid))
        if action == "undo":
            return jsonify(bulk.start_undo(jid, who=_who()))
        return jsonify({"ok": False, "error": "unknown action"}), 404
    except hikbulk.BulkError as e:
        return _berr(e)


# ---- device backup files and settings snapshots (hikbackup.py, Phase 6) ----------------
# The hub keeps them (encrypted); the site Pi only reads from the device and passes on.
import json as _json  # noqa: E402

import hikbackup  # noqa: E402


def _raw_body(limit):
    """The request body up to `limit` bytes. A backup file is bigger than the site's
    general 8 MB upload cap (MAX_CONTENT_LENGTH), so it is read from the raw stream."""
    try:
        n = int(request.headers.get("Content-Length") or 0)
    except ValueError:
        n = 0
    if n <= 0 or n > limit:
        raise hikbackup.BackupError("that is not a backup file (or it is too big)", 413 if n > limit else 400)
    stream, out = request.environ["wsgi.input"], bytearray()
    while len(out) < n:
        chunk = stream.read(min(1 << 20, n - len(out)))
        if not chunk:
            break
        out += chunk
    return bytes(out)


def _bkerr(e):
    return jsonify({"ok": False, "error": str(e), **e.extra}), e.status


@bp.route("/api/cameras/<path:key>/config-file", methods=["GET", "PUT"])
@guard
def api_camera_config_file(key):
    """GET: the device's own backup file, read now with a fresh file password (in the
    X-Config-Secret header — keep it with the file). PUT (raw file body, X-Config-Secret,
    X-Confirm-Text): load it into the device, which restarts — risky work."""
    try:
        if request.method == "GET":
            data, secret, facts = hikbackup.export(settings, key)
            resp = Response(data, mimetype="application/octet-stream")
            resp.headers["X-Config-Secret"] = secret
            resp.headers["X-Config-Facts"] = _json.dumps(facts, ensure_ascii=True)
            resp.headers["Cache-Control"] = "no-store"
            return resp
        return jsonify(hikbackup.restore(settings, key, _raw_body(hikbackup.MAX_FILE),
                                         str(request.headers.get("X-Config-Secret") or "")[:64], who=_who(),
                                         confirm_text=str(request.headers.get("X-Confirm-Text") or "")[:20]))
    except hikbackup.BackupError as e:
        return _bkerr(e)
    except hiksettings.SettingsError as e:
        return _err(e)


@bp.route("/api/cameras/<path:key>/snapshot")
@guard
def api_camera_snapshot(key):
    """Every setting Netwatch knows on this device, now (for "what changed")."""
    try:
        return jsonify(hikbackup.snapshot(settings, key))
    except hikbackup.BackupError as e:
        return _bkerr(e)
