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
    with its last good picture, flagged by X-Picture-Stale."""
    size = "full" if request.args.get("size") == "full" else "thumb"
    pic, flags, err, status = engine.picture(key, size, request.args.get("fresh") == "1")
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
