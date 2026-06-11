"""Flask web layer for the Netwatch Hub: multi-site dashboard + JSON API.

Aggregates every farm site's Netwatch (and its Uptime Kuma) over the WireGuard
VPN. All data endpoints sit behind a session login; /api/health stays open as
a liveness probe.
"""
import ipaddress
import os
import re
import time

import requests
from flask import (Flask, jsonify, redirect, request, send_from_directory,
                   session)

import auth
import hubconfig
import sitehistory
from poller import poller

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
app.secret_key = auth.secret_key()
app.config["PERMANENT_SESSION_LIFETIME"] = 30 * 86400

_OPEN_PATHS = {"/login", "/api/login", "/api/auth-state", "/api/health", "/app.css"}


@app.before_request
def _guard():
    p = request.path
    if p in _OPEN_PATHS or p.startswith("/static/"):
        return None
    if session.get("auth"):
        return None
    if p.startswith("/api/"):
        return jsonify({"error": "unauthorized"}), 401
    return redirect("/login")


# ---- pages -------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/site/<site_id>")
def site_page(site_id):
    return send_from_directory(STATIC_DIR, "site.html")


@app.route("/login")
def login_page():
    return send_from_directory(STATIC_DIR, "login.html")


@app.route("/app.css")
def appcss():
    return send_from_directory(STATIC_DIR, "app.css")


# ---- auth ----------------------------------------------------------------
@app.route("/api/auth-state")
def api_auth_state():
    return jsonify({"password_set": auth.password_set(),
                    "logged_in": bool(session.get("auth"))})


@app.route("/api/login", methods=["POST"])
def api_login():
    body = request.get_json(force=True)
    pw = body.get("password", "")
    if not auth.password_set():
        # first run: this request CREATES the password
        if len(pw) < 8:
            return jsonify({"ok": False, "error": "Use at least 8 characters"}), 400
        if pw != body.get("confirm", ""):
            return jsonify({"ok": False, "error": "Passwords do not match"}), 400
        auth.set_password(pw)
    elif not auth.check(pw):
        time.sleep(1)                      # blunt brute-force throttle
        return jsonify({"ok": False, "error": "Wrong password"}), 401
    session.permanent = True
    session["auth"] = True
    return jsonify({"ok": True})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/health")
def api_health():
    return jsonify({"status": "up", "service": "netwatch-hub"})


# ---- overview ------------------------------------------------------------
def _site_card(site):
    snap = poller.snapshot(site["id"])
    status = snap.get("status") or {}
    devices = (snap.get("devices") or {}).get("devices") or []
    kuma = snap.get("kuma") or {}
    fetched = snap.get("devices_fetched")
    poll_cfg = hubconfig.load()["poll"]
    stale = bool(devices) and (
        snap.get("stale", False)
        or not fetched
        or time.time() - fetched > 2 * poll_cfg["devices_interval_s"]
    )
    port = site.get("netwatch_port", 8090)
    return {
        "id": site["id"],
        "name": (status.get("site") or {}).get("name") or site.get("name") or site["id"],
        "location": (status.get("site") or {}).get("location", ""),
        "enabled": site.get("enabled", True),
        "reachable": snap.get("reachable", False),
        "error": snap.get("status_error", ""),
        "latency_ms": snap.get("latency_ms"),
        "last_scan_ts": status.get("last_scan_ts"),
        "is_scanning": status.get("is_scanning", False),
        "devices_total": len(devices) if devices else None,
        "devices_online": sum(1 for d in devices if d.get("online")) if devices else None,
        "watched_down": sum(1 for d in devices
                            if d.get("watch") and not d.get("online")) if devices else None,
        "stale": stale,
        "fetched_at": fetched,
        "kuma": {"up": kuma.get("up"), "down": kuma.get("down"),
                 "url": kuma.get("url")} if kuma.get("ok") else None,
        "kuma_state": (kuma.get("reason") if kuma and not kuma.get("ok") else
                       ("ok" if kuma.get("ok") else "off")),
        "links": {
            "netwatch": f"http://{site['vpn_ip']}:{port}",
            "kuma": site.get("kuma_url") or "",
        },
        "spark": sitehistory.series(site["id"], 86400, 48),
        "reach_24h": sitehistory.reachability_pct(site["id"], 86400),
    }


@app.route("/api/hub/overview")
def api_overview():
    cfg = hubconfig.load()
    return jsonify({
        "hub_name": cfg["hub"]["name"],
        "sites": [_site_card(s) for s in cfg["sites"]],
    })


# ---- sites registry --------------------------------------------------------
def _validate_site(body, existing_ids, keep_id=None):
    sid = (body.get("id") or "").strip().lower()
    if keep_id:
        sid = keep_id
    elif not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", sid):
        return None, "Site id: lowercase letters, digits and dashes only"
    if sid in existing_ids and sid != keep_id:
        return None, f"Site id '{sid}' already exists"
    ip = (body.get("vpn_ip") or "").strip()
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return None, f"Invalid VPN IP: {ip}"
    try:
        port = int(body.get("netwatch_port") or 8090)
    except (TypeError, ValueError):
        return None, "Invalid port"
    kuma_url = (body.get("kuma_url") or "").strip().rstrip("/")
    if kuma_url and not kuma_url.startswith(("http://", "https://")):
        return None, "Kuma URL must start with http:// or https://"
    return {
        "id": sid,
        "name": (body.get("name") or "").strip(),
        "vpn_ip": ip,
        "netwatch_port": port,
        "kuma_url": kuma_url,
        "kuma_status_slug": (body.get("kuma_status_slug") or "").strip(),
        "enabled": bool(body.get("enabled", True)),
    }, None


@app.route("/api/hub/sites", methods=["GET", "POST"])
def api_sites():
    cfg = hubconfig.load()
    if request.method == "POST":
        body = request.get_json(force=True)
        site, err = _validate_site(body, {s["id"] for s in cfg["sites"]})
        if err:
            return jsonify({"ok": False, "error": err}), 400
        cfg["sites"].append(site)
        hubconfig.save(cfg)
        poller.poll_now(site["id"])
        return jsonify({"ok": True, "site": site})
    return jsonify({"sites": cfg["sites"]})


@app.route("/api/hub/sites/<site_id>", methods=["POST", "DELETE"])
def api_site(site_id):
    cfg = hubconfig.load()
    idx = next((i for i, s in enumerate(cfg["sites"]) if s["id"] == site_id), None)
    if idx is None:
        return jsonify({"ok": False, "error": "unknown site"}), 404
    if request.method == "DELETE":
        cfg["sites"].pop(idx)
        hubconfig.save(cfg)
        return jsonify({"ok": True})
    body = request.get_json(force=True)
    site, err = _validate_site(body, {s["id"] for s in cfg["sites"]}, keep_id=site_id)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    cfg["sites"][idx] = site
    hubconfig.save(cfg)
    poller.poll_now(site_id)
    return jsonify({"ok": True, "site": site})


# ---- per-site data ----------------------------------------------------------
def _site_or_404(site_id):
    site = hubconfig.get_site(site_id)
    if not site:
        return None, (jsonify({"ok": False, "error": "unknown site"}), 404)
    return site, None


@app.route("/api/hub/sites/<site_id>/devices")
def api_site_devices(site_id):
    site, err = _site_or_404(site_id)
    if err:
        return err
    snap = poller.snapshot(site_id)
    payload = snap.get("devices") or {"devices": [], "targets": []}
    return jsonify({
        "card": _site_card(site),
        "devices": payload.get("devices") or [],
        "targets": payload.get("targets") or [],
        "fetched_at": snap.get("devices_fetched"),
        "stale": snap.get("stale", False) or not snap.get("reachable", False),
    })


@app.route("/api/hub/sites/<site_id>/history/<path:key>")
def api_site_history(site_id, key):
    """Live proxy: per-device uptime comes straight from the site (too granular
    to cache hub-side). 502 on timeout — the UI hides the sparkline."""
    site, err = _site_or_404(site_id)
    if err:
        return err
    poll = hubconfig.load()["poll"]
    window = request.args.get("window", "86400")
    base = f"http://{site['vpn_ip']}:{site.get('netwatch_port', 8090)}"
    try:
        r = requests.get(f"{base}/api/history/{key}", params={"window": window},
                         timeout=(poll["timeout_connect_s"], poll["timeout_read_s"]))
        r.raise_for_status()
        return jsonify(r.json())
    except (requests.exceptions.RequestException, ValueError):
        return jsonify({"ok": False, "error": "site unreachable"}), 502


@app.route("/api/hub/sites/<site_id>/kuma")
def api_site_kuma(site_id):
    site, err = _site_or_404(site_id)
    if err:
        return err
    snap = poller.snapshot(site_id)
    return jsonify(snap.get("kuma") or {"ok": False, "reason": "not-configured"})


@app.route("/api/hub/sites/<site_id>/reachability")
def api_site_reachability(site_id):
    site, err = _site_or_404(site_id)
    if err:
        return err
    window = int(request.args.get("window", 86400))
    return jsonify({
        "summary": sitehistory.summary(site_id),
        "series": sitehistory.series(site_id, window_s=window),
    })


@app.route("/api/hub/sites/<site_id>/poll", methods=["POST"])
def api_site_poll(site_id):
    site, err = _site_or_404(site_id)
    if err:
        return err
    poller.poll_now(site_id)
    return jsonify({"ok": True})


def main():
    auth.seed_from_env()
    poller.start()
    port = int(os.environ.get("HUB_PORT", "8091"))
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
