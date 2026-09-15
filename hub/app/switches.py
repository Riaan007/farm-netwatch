"""Switch management for the Control Center: proxies a site's switch API
(app/switchmon.py + app/edgeswitch.py on the site Pi) with the hub's key.

The switch itself is only reachable from the site's LAN, so every read and every
change runs ON the site Pi — the hub never sees the switch's password. The site
enforces the "Manage switches" setting and refuses ports the Pi or the router
are on; the hub only forwards.
"""
import threading
import time
from urllib.parse import quote

import requests
from flask import Blueprint, Response, jsonify, request

import hubconfig
import siteapi

bp = Blueprint("switches", __name__)

_CACHE = {}                 # site id -> (ts, payload) for the list
_LOCK = threading.Lock()
LIST_TTL = 45


def _site(site_id):
    site = hubconfig.get_site(site_id)
    if not site:
        return None, (jsonify({"ok": False, "error": "unknown site"}), 404)
    return site, None


def _too_old():
    return jsonify({"ok": False, "legacy": True,
                    "error": "this site's Netwatch is too old for switch management — update it (docker compose pull)"}), 501


def _forward(site, method, path, params=None, body=None, read_s=25, raw=False):
    try:
        r = requests.request(method, siteapi.base_url(site) + path, params=params, json=body,
                             headers=siteapi.headers(site), timeout=(5, read_s), stream=raw)
    except requests.RequestException as e:
        return None, (jsonify({"ok": False, "error": f"site unreachable: {e.__class__.__name__}"}), 502)
    ctype = r.headers.get("Content-Type", "")
    if r.status_code == 404 and "json" not in ctype:
        return None, _too_old()          # an older site has no such route (Flask's HTML 404)
    if r.status_code == 401:
        return None, (jsonify({"ok": False, "error": "the site doesn't accept this hub's key yet"}), 502)
    if raw:
        return r, None
    try:
        return r, (jsonify(r.json()), r.status_code)
    except ValueError:
        return None, (jsonify({"ok": False, "error": "site returned a bad reply"}), 502)


@bp.route("/api/hub/sites/<site_id>/switches")
def site_switches(site_id):
    """Every switch at a site with ports, PoE, traffic, problems (cached ~45 s:
    the site reads its switches every few minutes, and several views ask)."""
    site, err = _site(site_id)
    if err:
        return err
    fresh = request.args.get("fresh") == "1"
    with _LOCK:
        hit = _CACHE.get(site_id)
    if hit and not fresh and time.time() - hit[0] < LIST_TTL:
        return jsonify({**hit[1], "cached_at": int(hit[0])})
    r, out = _forward(site, "GET", "/api/switches")
    if r is not None and r.ok:
        try:
            with _LOCK:
                _CACHE[site_id] = (time.time(), r.json())
        except ValueError:
            pass
    return out


def _dev(key, tail=""):
    return f"/api/devices/{quote(key, safe='')}/switch{tail}"


@bp.route("/api/hub/sites/<site_id>/devices/<path:key>/switch")
def switch_detail(site_id, key):
    site, err = _site(site_id)
    if err:
        return err
    params = {k: request.args[k] for k in ("hours", "port") if request.args.get(k)}
    return _forward(site, "GET", _dev(key), params=params, read_s=40)[1]


@bp.route("/api/hub/sites/<site_id>/devices/<path:key>/switch/poll", methods=["POST"])
def switch_poll(site_id, key):
    site, err = _site(site_id)
    if err:
        return err
    with _LOCK:
        _CACHE.pop(site_id, None)
    return _forward(site, "POST", _dev(key, "/poll"), read_s=45)[1]


@bp.route("/api/hub/sites/<site_id>/devices/<path:key>/switch/action", methods=["POST"])
def switch_action(site_id, key):
    site, err = _site(site_id)
    if err:
        return err
    with _LOCK:
        _CACHE.pop(site_id, None)
    # A PoE power cycle waits on the switch (off time + two saves), a cable test ~30 s.
    return _forward(site, "POST", _dev(key, "/action"), body=request.get_json(silent=True) or {}, read_s=90)[1]


@bp.route("/api/hub/sites/<site_id>/devices/<path:key>/switch/backups", methods=["GET", "POST"])
def switch_backups(site_id, key):
    site, err = _site(site_id)
    if err:
        return err
    return _forward(site, request.method, _dev(key, "/backups"), read_s=70)[1]


@bp.route("/api/hub/sites/<site_id>/devices/<path:key>/switch/backups/<name>")
def switch_backup_download(site_id, key, name):
    site, err = _site(site_id)
    if err:
        return err
    r, out = _forward(site, "GET", _dev(key, "/backups/" + quote(name, safe="")), read_s=60, raw=True)
    if r is None:
        return out
    if not r.ok:
        try:
            return jsonify(r.json()), r.status_code
        except ValueError:
            return jsonify({"ok": False, "error": f"site answered {r.status_code}"}), 502
    headers = {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "content-disposition", "content-length")}
    return Response(r.iter_content(64 * 1024), status=200, headers=headers)
