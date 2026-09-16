"""The network diagram + map for the Control Center: proxies a site's
/api/topology (app/topology.py on the site Pi) with the hub's key.

The site owns the records — groups, hand-added equipment, links, positions —
exactly like device names and GPS pins, so the site's own Network page, its
backups and the hub always agree. The hub only forwards, caches the read for a
few seconds (several views ask at once over a thin farm link) and serves the
uploaded icons from a small cache.
"""
import re
import threading
import time
from urllib.parse import quote

import requests
from flask import Blueprint, Response, jsonify, request

import hubconfig
import siteapi

bp = Blueprint("topology", __name__)

READ_TTL = 15
ICON_TTL = 3600
_CACHE = {}               # (site id, scope) -> (ts, payload)
_ICONS = {}               # (site id, icon id) -> (ts, mime, bytes)
_LOCK = threading.Lock()
# Everything the site's topology API accepts; nothing else is forwarded.
_WRITE_PATH = re.compile(r"^(groups|equipment|nodes|links|suggestions|layout|arrange|icons|type-icons|dismissed)"
                         r"(/(?!\.+(?:/|$))[A-Za-z0-9:._~-]+){0,3}$")
_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}   # never SVG (it can carry script)


def _site(site_id):
    site = hubconfig.get_site(site_id)
    if not site:
        return None, (jsonify({"ok": False, "error": "unknown site"}), 404)
    return site, None


def _too_old():
    return jsonify({"ok": False, "legacy": True,
                    "error": "this site's Netwatch is too old for the network view — update it"}), 501


def _drop_cache(site_id):
    with _LOCK:
        for k in [k for k in _CACHE if k[0] == site_id]:
            _CACHE.pop(k, None)


def _reply(r):
    ctype = r.headers.get("Content-Type", "")
    if r.status_code == 404 and "json" not in ctype:
        return _too_old()                     # an older site has no such route (Flask's HTML 404)
    if r.status_code == 401:
        return jsonify({"ok": False, "error": "the site doesn't accept this hub's key yet"}), 502
    try:
        return jsonify(r.json()), r.status_code
    except ValueError:
        return jsonify({"ok": False, "error": "site returned a bad reply"}), 502


@bp.route("/api/hub/sites/<site_id>/topology")
def site_topology(site_id):
    site, err = _site(site_id)
    if err:
        return err
    scope = "all" if request.args.get("scope") == "all" else "infra"
    key = (site_id, scope)
    with _LOCK:
        hit = _CACHE.get(key)
    if hit and request.args.get("fresh") != "1" and time.time() - hit[0] < READ_TTL:
        return jsonify(hit[1])
    try:
        r = requests.get(siteapi.base_url(site) + "/api/topology", params={"scope": scope} if scope == "all" else None,
                         headers=siteapi.headers(site), timeout=(5, 30))
    except requests.RequestException as e:
        if hit:                                # the last good copy, marked as such
            return jsonify({**hit[1], "stale": True, "error": f"site unreachable: {e.__class__.__name__}"})
        return jsonify({"ok": False, "error": f"site unreachable: {e.__class__.__name__}"}), 502
    if r.ok and "json" in r.headers.get("Content-Type", ""):
        try:
            body = r.json()
            with _LOCK:
                _CACHE[key] = (time.time(), body)
        except ValueError:
            pass
    return _reply(r)


@bp.route("/api/hub/sites/<site_id>/topology/<path:sub>", methods=["POST", "DELETE"])
def site_topology_write(site_id, sub):
    site, err = _site(site_id)
    if err:
        return err
    if not _WRITE_PATH.match(sub):
        return jsonify({"ok": False, "error": "not a network-view action"}), 404
    params = {"graph": "1"} if request.args.get("graph") == "1" else {}
    if request.args.get("scope") == "all":
        params["scope"] = "all"
    try:
        r = requests.request(request.method, f"{siteapi.base_url(site)}/api/topology/{sub}", params=params,
                             json=request.get_json(silent=True) if request.method == "POST" else None,
                             headers=siteapi.headers(site), timeout=(5, 45))
    except requests.RequestException as e:
        return jsonify({"ok": False, "error": f"site unreachable: {e.__class__.__name__}"}), 502
    _drop_cache(site_id)
    if sub.startswith("icons/") and request.method == "DELETE":
        with _LOCK:
            _ICONS.pop((site_id, sub.split("/", 1)[1]), None)
    out = _reply(r)
    if r.ok:
        try:
            body = r.json()
            graph = body.get("graph") if isinstance(body, dict) else None
            if isinstance(graph, dict):
                with _LOCK:
                    _CACHE[(site_id, params.get("scope", "infra"))] = (time.time(), graph)
        except ValueError:
            pass
    return out


@bp.route("/api/hub/sites/<site_id>/topology/icons/<iid>")
def site_topology_icon(site_id, iid):
    site, err = _site(site_id)
    if err:
        return err
    if not re.fullmatch(r"i-[0-9a-f]{8}", iid):
        return jsonify({"ok": False, "error": "no such icon"}), 404
    key = (site_id, iid)
    with _LOCK:
        hit = _ICONS.get(key)
    if not hit or time.time() - hit[0] > ICON_TTL:
        try:
            r = requests.get(f"{siteapi.base_url(site)}/api/topology/icons/{quote(iid)}",
                             headers=siteapi.headers(site), timeout=(5, 20))
        except requests.RequestException:
            r = None
        mime = (r.headers.get("Content-Type", "").split(";")[0].strip().lower() if r is not None else "")
        if r is None or not r.ok or mime not in _IMAGE_TYPES or len(r.content) > 512 * 1024:
            if not hit:
                return jsonify({"ok": False, "error": "no such icon"}), 404
        else:
            hit = (time.time(), mime, r.content)
            with _LOCK:
                if len(_ICONS) > 400:
                    _ICONS.clear()
                _ICONS[key] = hit
    resp = Response(hit[2], mimetype=hit[1])
    resp.headers["Cache-Control"] = "private, max-age=86400"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return resp
