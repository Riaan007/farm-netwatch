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
    """GET ?lens=1: the camera's picture, text-on-video, time and stream settings,
    with what each may be set to (from the camera's own capabilities).
    POST {lens, section, changes: {field: value}, expect: {field: value seen}, confirm}:
    change them — needs the site's "Camera settings" switch; careful sections need
    confirm; the camera is read back to prove the change took."""
    try:
        if request.method == "GET":
            return jsonify(settings.read(key, int(request.args.get("lens") or 1)))
        b = request.get_json(force=True, silent=True) or {}
        return jsonify(settings.apply(key, int(b.get("lens") or 1), str(b.get("section") or ""),
                                      b.get("changes") if isinstance(b.get("changes"), dict) else {},
                                      who=_who(), expect=b.get("expect") if isinstance(b.get("expect"), dict) else {},
                                      confirm=b.get("confirm") is True))
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
