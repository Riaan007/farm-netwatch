"""Flask web layer: serves the dashboard + setup wizard and the JSON API.

Keeps the three endpoints the original HTML already called (/api/status,
/api/trigger, /api/setup) working, and adds the richer endpoints the new UI uses.
"""
import base64
import ipaddress
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import urllib3
from flask import Flask, jsonify, redirect, request, send_file, send_from_directory, session

import airos
import assets
import commands
import credtest
import config
import creds
import edgeswitch
import hikvision
import history
import hubvpn
import identify
import mikrotik
import monitoring
import netcfg
import radiomon
import restart
import siteauth
import switchmon
import sysmon
import topology
import topology_routes
import tunnels
import wifidiag
import kuma
import notify
from listener import listener
from scanner import REGISTRY_PATH, scanner, default_gateway, hik_own_name

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
PHOTO_DIR = os.path.join(os.environ.get("NETWATCH_DATA", "/data"), "photos")
app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024   # 8 MB photo cap
siteauth.init_app(app)

guard = siteauth.required            # a logged-in person or the hub key

# Network page (diagram + map): app/topology.py behind app/topology_routes.py.
app.register_blueprint(topology_routes.bp)
topology_routes.providers.update(
    routers=lambda: {k: {"ts": v["view"].get("read_ts"), "ports": v["view"].get("port_macs") or {}}
                     for k, v in list(_ROUTER_CACHE.items()) if v["view"].get("ok")},
)


def _photo_path(key):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", key)
    return os.path.join(PHOTO_DIR, safe)


def _img_mime(data):
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"GIF8":
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


# ---- pages -------------------------------------------------------------
@app.route("/")
def index():
    if not config.load().get("configured"):
        return redirect("/setup")
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/setup")
def setup_page():
    return send_from_directory(STATIC_DIR, "setup.html")


@app.route("/wifi")
def wifi_page():
    return redirect("/#/wifi")


@app.route("/neighbors")
def neighbors_page():
    return redirect("/#/neighbors")


@app.route("/app.css")
def appcss():
    return send_from_directory(STATIC_DIR, "app.css")


# ---- status / config ---------------------------------------------------
@app.route("/api/status")
def api_status():
    cfg = config.load()
    st = scanner.get_status()
    primary = cfg["targets"][0]["cidr"] if cfg["targets"] else ""
    return jsonify({
        # original fields kept for backwards compatibility
        "last_scan": st["last_scan"],
        "last_scan_ts": st["last_scan_ts"],
        "scan_interval_min": cfg["scan"]["interval_min"],
        "target_network": primary,
        "is_scanning": st["is_scanning"],
        # extended
        "configured": cfg.get("configured", False),
        "mode": st["mode"],
        "progress": st["progress"],
        "site": cfg["site"],
        "vpn": cfg["vpn"]["mode"],
        "features": cfg.get("features", {}),
        "server_ip": kuma.lan_ip(),   # this Pi's own LAN address, shown in the UI
        "auth": siteauth.state(request, session),   # booleans; the hub claims on hub_key_set=false
    })


# ---- login (see siteauth.py) -------------------------------------------------
@app.route("/api/auth/state")
def api_auth_state():
    return jsonify(siteauth.state(request, session))


@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    body = request.get_json(force=True, silent=True) or {}
    code, res = siteauth.login(request, session, body.get("password", ""))
    return jsonify(res), code


@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/auth/password", methods=["POST"])
@guard
def api_auth_password():
    """Set/change the site password (the hub, or a person already logged in)."""
    body = request.get_json(force=True, silent=True) or {}
    try:
        siteauth.set_password(body.get("password", ""))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    if not siteauth.hub_ok(request):
        # the person who changed it stays logged in under the new epoch
        siteauth.login(request, session, body.get("password", ""))
    return jsonify({"ok": True})


@app.route("/api/auth/claim-hub", methods=["POST"])
def api_auth_claim_hub():
    body = request.get_json(force=True, silent=True) or {}
    code, res = siteauth.claim_hub(request, body.get("key", ""))
    return jsonify(res), code


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    authed = siteauth.allowed(request, session)
    if request.method == "POST":
        if not authed:
            return siteauth._denied(request, session)
        # Merge over the CURRENT config (not defaults) so a partial update never
        # silently resets unrelated fields such as `configured`.
        return jsonify(config.update(request.get_json(force=True)))
    if request.args.get("full") and not authed:
        return siteauth._denied(request, session)
    cfg = config.load()
    if not authed:
        # The ntfy topic doubles as the remote-command channel: anyone who knows
        # it can drive this Pi. Settings forms ask for ?full=1 (login first).
        cfg["alerts"]["ntfy_topic_set"] = bool(cfg["alerts"].get("ntfy_topic"))
        cfg["alerts"]["ntfy_topic"] = ""
        cfg["redacted"] = True
    ki = cfg.get("integrations", {}).get("kuma")
    if ki is not None:
        # What the URL currently resolves to (== base_url when auto_url is off);
        # the UI shows this and points its Open-Kuma links at it.
        ki["resolved_url"] = kuma.effective_base(ki)
    return jsonify(cfg)


# ---- scan triggers -----------------------------------------------------
@app.route("/api/trigger", methods=["POST"])
def api_trigger():
    body = request.get_json(silent=True) or {}
    mode = body.get("mode", "quick")
    target = body.get("target")
    hosts = body.get("hosts")  # optional list of IPs to scan specifically
    if hosts and not isinstance(hosts, list):
        hosts = [hosts]
    scanner.trigger(mode=mode if mode in ("quick", "deep") else "quick",
                    target=target, hosts=hosts)
    return jsonify({"ok": True, "mode": mode, "hosts": hosts})


@app.route("/api/command", methods=["POST"])
def api_command():
    """Run an ad-hoc probe from the dashboard (ping / port / tracert)."""
    body = request.get_json(force=True)
    action = body.get("action")
    ip = body.get("ip", "")
    if action == "ping":
        result = commands.ping(ip)
    elif action == "port":
        result = commands.port_check(ip, body.get("port"))
    elif action in ("tracert", "traceroute"):
        result = commands.traceroute(ip)
    elif action in ("quality", "test"):
        # structured result for the dashboard's connection-quality panel
        return jsonify({"ok": True, "quality": commands.quality_test(ip, body.get("count", 20))})
    else:
        return jsonify({"ok": False, "error": "unknown action"}), 400
    return jsonify({"ok": True, "result": result})


@app.route("/api/test-ntfy", methods=["POST"])
def api_test_ntfy():
    body = request.get_json(silent=True) or {}
    cfg = config.load()
    topic = (body.get("topic") or cfg["alerts"]["ntfy_topic"]).strip()
    server = body.get("server") or cfg["alerts"]["ntfy_server"]
    if not topic:
        return jsonify({"ok": False, "error": "No ntfy topic set"}), 400
    alerts = {"ntfy_topic": topic, "ntfy_server": server,
              "allow_commands": cfg["alerts"].get("allow_commands", False)}
    mid = notify.push(
        alerts, "Netwatch test",
        "Alerts are working. Reply 'help' for commands, or use the buttons below.",
        priority="high", tags=["white_check_mark", "satellite"],
        actions=[notify._cmd_action("Status", alerts, "status"),
                 notify._cmd_action("Quick scan", alerts, "quickscan")] if topic else None,
    )
    return jsonify({"ok": mid is not None,
                    "detail": "Sent — check your phone." if mid else "ntfy rejected the message."})


@app.route("/api/setup", methods=["POST"])
@siteauth.required_if(lambda: config.load().get("configured"))
def api_setup():
    """Original lightweight setup from the dashboard gear modal."""
    body = request.get_json(force=True)
    patch = {"scan": {}, "targets": None}
    if "interval" in body:
        patch["scan"]["interval_min"] = int(body["interval"])
    if "deep_on_new" in body:
        patch["scan"]["deep_on_new"] = bool(body["deep_on_new"])
    if "discovery" in body:
        patch["scan"]["discovery"] = bool(body["discovery"])
    if body.get("network"):
        patch["targets"] = [{"cidr": body["network"], "label": "Main", "local": "auto"}]
    alerts = {}
    if "ntfy_topic" in body:
        alerts["ntfy_topic"] = body["ntfy_topic"]
    if "allow_commands" in body:
        alerts["allow_commands"] = bool(body["allow_commands"])
    if "notify_online" in body:
        alerts["notify_online"] = bool(body["notify_online"])
    if "notify_hub_offline" in body:
        alerts["notify_hub_offline"] = bool(body["notify_hub_offline"])
    if alerts:
        patch["alerts"] = alerts
    kuma_patch = {}
    if "kuma_enabled" in body:
        kuma_patch["enabled"] = bool(body["kuma_enabled"])
    if body.get("kuma_base_url"):
        kuma_patch["base_url"] = body["kuma_base_url"].strip()
    if "kuma_auto_url" in body:
        kuma_patch["auto_url"] = bool(body["kuma_auto_url"])
    if "kuma_username" in body:
        kuma_patch["username"] = body["kuma_username"].strip()
    if kuma_patch:
        patch["integrations"] = {"kuma": kuma_patch}
    feats = {}
    if "airos_change_ip" in body:
        feats["airos_change_ip"] = bool(body["airos_change_ip"])
    if "mikrotik_manage" in body:
        feats["mikrotik_manage"] = bool(body["mikrotik_manage"])
    if "switch_manage" in body:
        feats["switch_manage"] = bool(body["switch_manage"])
    if "device_restart" in body:
        feats["device_restart"] = bool(body["device_restart"])
    if feats:
        patch["features"] = feats
    if "watchdog" in body:
        patch["sysmon"] = {"watchdog": bool(body["watchdog"])}
    # admin password -> obfuscated creds store (only when a non-empty value is sent)
    if body.get("kuma_password"):
        uname = body.get("kuma_username") or config.load()["integrations"]["kuma"].get("username", "")
        creds.set_("@kuma", username=uname, password=body["kuma_password"])
    patch = {k: v for k, v in patch.items() if v is not None}
    config.update(patch)
    # Category overrides are replaced wholesale (a deep-merge could never clear a
    # category once set), so handle them after the merge.
    if "categories" in body and isinstance(body["categories"], dict):
        cfg = config.load()
        cfg["alerts"]["categories"] = body["categories"]
        config.save(cfg)
    scanner.wake()
    return jsonify({"ok": True})


# ---- devices -----------------------------------------------------------
@app.route("/api/devices")
def api_devices():
    cfg = config.load()
    have = creds.keys_with_creds()
    devices = scanner.get_devices()
    plugged = switchmon.monitor.mac_map()
    with scanner.lock:
        missed = dict(scanner.miss)
    for d in devices:                      # flag only; never expose the secret here
        # scans in a row that missed it: the list says offline after one, but a
        # device only counts as down after alerts.offline_after (the hub's alerts)
        d["missed_scans"] = missed.get(d.get("key"), 0)
        d["has_credentials"] = d.get("key") in have
        d["has_photo"] = os.path.exists(_photo_path(d.get("key", "")))
        _kreg = scanner.registry.get(d.get("key", ""), {})
        d["has_kuma"] = bool(_kreg.get("kuma_monitor_id") or _kreg.get("kuma_token"))
        d["kuma_paused"] = bool(_kreg.get("kuma_paused"))
        d["watch"] = monitoring.is_monitored(_kreg)   # = "Monitored" (see monitoring.py)
        d["geo"] = _kreg.get("geo") or None         # {lat, lon, note, ts} — set by hand
        ms = _kreg.get("meta_src")                  # model/serial/firmware filled in: {field: "nvr"|"hostname"}
        d["meta_src"] = dict(ms) if isinstance(ms, dict) else {}
        ct = _kreg.get("cred_test")
        d["cred_test"] = {k: v for k, v in ct.items() if k != "fp"} if ct else None
        sp = plugged.get(edgeswitch.normalize_mac(d.get("mac"))) if d.get("mac") else None
        d["switch_port"] = sp if sp and sp["switch_key"] != d.get("key") else None
        if switchmon.is_switch(d):
            d["is_switch"] = True
    return jsonify({
        "targets": cfg["targets"],
        "devices": devices,
        "last_scan": scanner.get_status()["last_scan"],
        "offline_after": cfg["alerts"]["offline_after"],
    })


@app.route("/api/devices/<path:key>/credentials", methods=["GET", "POST"])
@guard
def api_credentials(key):
    if request.method == "POST":
        body = request.get_json(force=True)
        user, pw = body.get("username", "").strip(), body.get("password", "")
        saved = creds.set_(key, user, pw, body.get("notes", "").strip())
        sw = next((d for d in scanner.get_devices() if d.get("key") == key), None)
        if saved and sw and switchmon.is_switch(sw):     # read the switch with the new login now
            threading.Thread(target=switchmon.monitor.poll_round, kwargs={"force": True, "only": key},
                             daemon=True).start()
        # A remembered login test stays meaningful only for the login it tried.
        recent = _CRED_RECENT.pop(key, None)
        if saved and recent and recent["fp"] == creds.fingerprint(user, pw) and time.time() - recent["ts"] < 3600:
            scanner.registry.setdefault(key, {})["cred_test"] = {**recent, "current": True}
            scanner.save_registry()
            return jsonify({"ok": True, "has_credentials": saved})
        ct = scanner.registry.get(key, {}).get("cred_test")
        if ct:
            current = bool(saved) and ct.get("fp") == creds.fingerprint(user, pw)
            if ct.get("current") != current:
                ct["current"] = current
                scanner.save_registry()
        return jsonify({"ok": True, "has_credentials": saved})
    # GET returns the decrypted secret on demand (not part of the polled feed)
    return jsonify(creds.get(key))


_CRED_TESTS = {}      # key -> start ts of a test in flight (a double click must not log in twice)
_CRED_RECENT = {}     # key -> last test of a typed-but-unsaved login; kept if that login is saved


@app.route("/api/devices/<path:key>/credentials/test", methods=["POST"])
@guard
def api_credentials_test(key):
    """Try a login on the device — the typed one, or the saved one when none is
    sent. Nothing is saved except the outcome, which the Access tab shows."""
    body = request.get_json(silent=True) or {}
    dev = next((d for d in scanner.get_devices() if d.get("key") == key), None)
    if not dev:
        return jsonify({"ok": False, "error": "unknown device"}), 404
    saved = creds.get(key)
    if "username" in body or "password" in body:
        user, pw = str(body.get("username") or "").strip(), str(body.get("password") or "")
    else:
        user, pw = saved["username"], saved["password"]
    now = time.time()
    if now - _CRED_TESTS.get(key, 0) < 30:
        return jsonify({"ok": False, "error": "A test for this device is already running"}), 429
    _CRED_TESTS[key] = now
    try:
        res = credtest.test(dev, user, pw)
    finally:
        _CRED_TESTS.pop(key, None)
    fp = creds.fingerprint(user, pw)
    current = bool(saved["username"] or saved["password"]) and fp == creds.fingerprint(saved["username"], saved["password"])
    if res["result"] in ("ok", "auth_failed"):
        rec = {"ts": int(now), "result": res["result"], "method": res.get("method", ""),
               "detail": res.get("detail", ""), "username": user, "fp": fp, "current": current}
        if current:          # only a test of the SAVED login is remembered on the device
            scanner.registry.setdefault(key, {})["cred_test"] = rec
            scanner.save_registry()
            _CRED_RECENT.pop(key, None)
        else:
            _CRED_RECENT[key] = rec
    return jsonify({"ok": True, **res, "username": user, "current": current, "ts": int(now)})


@app.route("/api/asset-schema")
def api_asset_schema():
    """Field definitions per category — the UI renders the form from this."""
    cat = request.args.get("category")
    if cat:
        return jsonify({"ok": True, **assets.schema(cat)})
    return jsonify({"ok": True, **assets.all_schemas()})


@app.route("/api/devices/<path:key>/asset", methods=["GET", "POST"])
def api_device_asset(key):
    """Read/write a device's asset register entry (category-specific details)."""
    dev = next((d for d in scanner.get_devices() if d.get("key") == key), None)
    reg = scanner.registry.get(key, {})
    category = (dev or {}).get("category") or reg.get("category") or "unknown"
    if request.method == "POST":
        if not siteauth.allowed(request, session):
            return siteauth._denied(request, session)
        body = request.get_json(force=True, silent=True)
        values = (body.get("asset") or body) if isinstance(body, dict) else None
        if not isinstance(values, dict):
            return jsonify({"ok": False, "error": "send the asset fields as a JSON object"}), 400
        if not _device_known(key):
            return jsonify({"ok": False, "error": "unknown device"}), 404
        values = assets.clean(category, values)
        scanner.registry.setdefault(key, {})["asset"] = values
        scanner.save_registry()
        return jsonify({"ok": True, "asset": values,
                        "completeness": assets.completeness(category, values)})
    values = reg.get("asset") or {}
    return jsonify({"ok": True, "category": category, "asset": values,
                    **assets.schema(category),
                    "completeness": assets.completeness(category, values)})


def _coord(v, limit):
    try:
        f = round(float(str(v).strip().replace(",", ".")), 6)
    except (TypeError, ValueError):
        return None
    return f if -limit <= f <= limit and f == f else None


@app.route("/api/devices/<path:key>/location", methods=["POST"])
@guard
def api_device_location(key):
    """Pin a device to a GPS position (decimal degrees) for the map, or clear it.
    Body: {lat, lon, note?} or {clear: true}. Stored in the registry under
    `geo`, so it survives rescans and rides along in backups and to the hub.
    Needs the site login or the hub key (the hub sends it)."""
    body = request.get_json(force=True, silent=True) or {}
    if not _device_known(key):
        return jsonify({"ok": False, "error": "unknown device"}), 404
    reg = scanner.registry.setdefault(key, {})
    if body.get("clear"):
        reg.pop("geo", None)
        scanner.save_registry()
        return jsonify({"ok": True, "geo": None})
    lat, lon = _coord(body.get("lat"), 90), _coord(body.get("lon"), 180)
    if lat is None or lon is None or (lat == 0 and lon == 0):
        return jsonify({"ok": False, "error": "Enter a latitude between -90 and 90 and a longitude between -180 and 180"}), 400
    reg["geo"] = {"lat": lat, "lon": lon, "note": str(body.get("note") or "").strip()[:120],
                  "ts": int(time.time())}
    scanner.save_registry()
    return jsonify({"ok": True, "geo": reg["geo"]})


@app.route("/api/radio/overview")
def api_radio_overview():
    """Every monitored radio's latest reading plus the current problem list."""
    snap = radiomon.monitor.snapshot()
    return jsonify({
        "ok": True,
        "enabled": bool((config.load().get("radio") or {}).get("enabled", True)),
        "radios": snap["radios"],
        "problems": sorted(snap["problems"],
                           key=lambda p: (p["level"] != "crit", p["device"])),
        "polling": snap["busy"],
    })


@app.route("/api/radio/poll", methods=["POST"])
def api_radio_poll():
    """Force a poll now instead of waiting for the next scan."""
    cfg = config.load()
    devices = {d["key"]: d for d in scanner.get_devices() if d.get("key")}
    scanner_registry = scanner.registry
    threading.Thread(
        target=radiomon.monitor.poll_round,
        args=(cfg, devices, scanner_registry),
        kwargs={"force": True}, daemon=True).start()
    return jsonify({"ok": True, "started": True})


@app.route("/api/radio/links")
def api_radio_links():
    """Everything the Wi-Fi history page needs in one call: each monitored radio,
    each wireless link, their series over the window, and per-link stats.

    Series come back as parallel column arrays rather than a list of objects —
    a month of 15-minute samples across a pole of radios is a lot of repeated
    JSON keys otherwise — and are bucketed down to at most MAX_POINTS so the
    payload does not grow with the range.
    """
    MAX_POINTS = 300
    hours = max(1, min(int(request.args.get("hours", 24) or 24), 24 * 90))
    window = hours * 3600

    def thin(rows):
        step = max(1, len(rows) // MAX_POINTS)
        return rows[::step] if step > 1 else rows

    def cols(rows, fields):
        out = {"ts": [r["ts"] for r in rows]}
        for f in fields:
            out[f] = [r.get(f) for r in rows]
        return out

    def stats(rows, field, key, peer=None):
        vals = [r[field] for r in rows if r.get(field) is not None]
        if not vals:
            return None
        base, n = history.radio_baseline(key, field, peer=peer)
        return {"now": vals[-1], "min": min(vals), "max": max(vals),
                "avg": round(sum(vals) / len(vals), 1),
                "baseline": base, "baseline_n": n, "n": len(vals)}

    snap = radiomon.monitor.snapshot()
    devs = {d["key"]: d for d in scanner.get_devices() if d.get("key")}
    rcfg = config.load().get("radio") or {}
    radios = []
    for key, last in (snap["radios"] or {}).items():
        dev = devs.get(key, {})
        srows = history.radio_series(key, window_s=window, limit=20000)
        lrows = history.radio_link_series(key, window_s=window, limit=60000)
        by_peer = {}
        for r in lrows:
            by_peer.setdefault(r["peer"], []).append(r)
        radios.append({
            "key": key, "ip": last.get("ip") or dev.get("ip"),
            "name": dev.get("name") or dev.get("device_name") or last.get("name") or key,
            "ok": bool(last.get("ok")), "error": last.get("error"),
            "mode": last.get("mode"), "ssid": last.get("ssid"),
            "model": dev.get("model") or last.get("model") or "", "last_ts": last.get("ts"),
            "current": last.get("sample") or {},
            "rows": srows,
            "series": cols(thin(srows), ["noise", "airtime", "cap_dl", "cap_ul", "links"]),
            "stats": {f: stats(srows, f, key) for f in ("noise", "airtime", "cap_dl")},
            "links": [{"peer": peer, "name": rows[-1].get("name") or peer,
                       "model": rows[-1].get("model") or "", "ip": rows[-1].get("ip") or "",
                       "rows": rows} for peer, rows in by_peer.items()],
        })
    diag = wifidiag.build(radios, poll_gap_s=max(1, int(rcfg.get("poll_min", 15))) * 60)
    # For the hub over a farm's link: ?lite=1 drops every chart series (the
    # findings and figures stay), ?link=<id> returns only that link with its
    # series — a Control Center fetches the summary on a timer and one link's
    # charts only when someone opens it.
    only = request.args.get("link")
    if only:
        diag["links"] = [L for L in diag["links"] if L["id"] == only]
    if request.args.get("lite") == "1" or only:
        for L in diag["links"]:
            if not only:
                L.pop("series", None)
        for r in diag["radios"]:
            r.pop("series", None)
    return jsonify({"ok": True, "hours": hours, "problems": snap["problems"], "polling": snap["busy"],
                    "enabled": bool(rcfg.get("enabled", True)),
                    "poll_min": int(rcfg.get("poll_min", 15)), **diag})


@app.route("/api/devices/<path:key>/radio-history")
def api_radio_history(key):
    """Trend data for one radio: its own samples plus each link's series."""
    window = int(request.args.get("hours", 24)) * 3600
    links = {}
    for row in history.radio_link_series(key, window_s=window):
        links.setdefault(row["peer"], {"peer": row["peer"], "name": row["name"],
                                       "model": row["model"], "points": []})
        links[row["peer"]]["points"].append(row)
    return jsonify({"ok": True, "key": key, "hours": window // 3600,
                    "samples": history.radio_series(key, window_s=window),
                    "links": list(links.values()),
                    "problems": [p for p in radiomon.monitor.snapshot()["problems"]
                                 if p["key"] == key]})


@app.route("/api/credentials/bulk", methods=["POST"])
@guard
def api_credentials_bulk():
    """Apply one login to many devices at once, or copy a device's saved login
    onto others. A farm site has whole families of identical gear (a pole of
    PowerBeams, a row of Hikvision cameras) sharing one login — entering it
    device by device is what stops people saving it at all.

    Body: {"keys": [device keys],
           "username"/"password"/"notes": literal values,   OR
           "copy_from": "<device key>"  — reuse that device's stored login,
           "overwrite": false  — false leaves devices that already have a login,
           "clear": false      — true wipes the login on every target instead}

    Returns per-key outcomes so the UI can say exactly what changed.
    """
    body = request.get_json(force=True)
    keys = [k for k in (body.get("keys") or []) if isinstance(k, str) and k.strip()]
    if not keys:
        return jsonify({"ok": False, "error": "no devices selected"}), 400

    clear = bool(body.get("clear"))
    overwrite = bool(body.get("overwrite"))
    source = (body.get("copy_from") or "").strip()
    if clear:
        username = password = notes = ""
    elif source:
        src = creds.get(source)
        username, password, notes = src["username"], src["password"], src["notes"]
        if not (username or password):
            return jsonify({"ok": False,
                            "error": "the device you're copying from has no login saved"}), 400
    else:
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        notes = (body.get("notes") or "").strip()
        if not (username or password):
            return jsonify({"ok": False,
                            "error": "enter a username or password (or tick Clear)"}), 400

    have = creds.keys_with_creds()
    results = []
    for key in keys:
        if key == source:
            results.append({"key": key, "status": "source"})
            continue
        if not clear and not overwrite and key in have:
            results.append({"key": key, "status": "skipped"})
            continue
        creds.set_(key, username, password, notes)
        results.append({"key": key, "status": "cleared" if clear else "saved"})
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return jsonify({"ok": True, "counts": counts, "results": results})


@app.route("/api/devices/prune", methods=["POST"])
@guard
def api_devices_prune():
    """Forget stale devices. Body: {"days": N} removes offline devices not seen
    in N days; {"days": null} (or omitted) removes ALL currently-offline ones.
    Answers once the device list is saved; their uptime history is deleted in
    the background (see Scanner._drop_history)."""
    body = request.get_json(force=True, silent=True) or {}
    days = body.get("days")
    try:
        days = int(days) if days not in (None, "") else None
    except (TypeError, ValueError):
        days = None
    removed = scanner.prune_devices(days=days, only_offline=True)
    return jsonify({"ok": True, "removed": len(removed), "keys": removed})


@app.route("/api/monitoring", methods=["GET", "POST"])
def api_monitoring():
    """The Monitored list (monitoring.py). GET: counts. POST {monitor: [keys],
    stop: [keys]} switches devices on/off it in one go — needs the site login or
    the hub key, like other settings: it decides what the hub alerts on and
    pauses/resumes Uptime Kuma monitors."""
    if request.method == "GET":
        return jsonify({"ok": True, **monitoring.summary(scanner)})
    if not siteauth.allowed(request, session):
        return siteauth._denied(request, session)
    body = request.get_json(force=True, silent=True) or {}
    lists = {}
    for field in ("monitor", "stop"):
        v = body.get(field) or []
        if not (isinstance(v, list) and all(isinstance(k, str) and k for k in v)):
            return jsonify({"ok": False, "error": f"'{field}' must be a list of device keys"}), 400
        lists[field] = v
    if len(lists["monitor"]) + len(lists["stop"]) > monitoring.MAX_KEYS:
        return jsonify({"ok": False, "error": "too many devices in one request"}), 400
    if set(lists["monitor"]) & set(lists["stop"]):
        return jsonify({"ok": False, "error": "a device can't be in both lists"}), 400
    res = monitoring.set_many(scanner, on=lists["monitor"], off=lists["stop"], by=_who())
    return jsonify({"ok": True, **res})


def _who():
    """Who is changing something, for the device history."""
    return "the hub" if siteauth.hub_ok(request) else f"site login ({request.remote_addr})"


def _device_known(key):
    if key in scanner.registry:
        return True
    with scanner.lock:
        return key in scanner.devices


# Operator text on a device record -> longest value accepted. Generous: the
# limits only stop junk (a Hikvision serial is ~45 characters).
_META_TEXT = {"name": 100, "type": 120, "serial": 100, "model": 100}


def _device_meta_fields(key, body):
    """The metadata in a device POST, checked. Returns (fields, error). A field
    left out (or null) is not changed; "" clears a text field or the link."""
    out = {}
    for field, limit in _META_TEXT.items():
        v = body.get(field)
        if v is None:
            continue
        if not isinstance(v, str):
            return None, f"'{field}' must be text"
        v = v.strip()
        if len(v) > limit:
            return None, f"'{field}' is longer than {limit} characters"
        out[field] = v
    if body.get("category") is not None:
        if not identify.is_category(body["category"]):
            return None, "unknown category — use one of: " + ", ".join(sorted(identify.CATEGORIES))
        out["category"] = body["category"]
    link = body.get("link")
    if link is not None:
        link = link.strip() if isinstance(link, str) else None
        if link is None or (link and (link == key or not _device_known(link))):
            return None, "'link' must be the key of another device on this site"
        out["link"] = link
    return out, None


@app.route("/api/devices/<path:key>", methods=["POST", "DELETE"])
@guard
def api_device_meta(key):
    """POST changes a device's record: name, category, type, serial, model,
    link and `watch` (the Monitored switch — older hubs send it with their key;
    the site page uses /api/monitoring). DELETE = Forget: off every list, its
    Kuma monitor and history deleted. Both need the site login or the hub key."""
    if request.method == "DELETE":
        removed = scanner.delete_device(key)
        return jsonify({"ok": removed}), (200 if removed else 404)
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "send a JSON object"}), 400
    watch = body.get("watch")
    if not _device_known(key):
        return jsonify({"ok": False, "error": "unknown device"}), 404
    if watch is not None and not isinstance(watch, bool):
        return jsonify({"ok": False, "error": "'watch' must be true or false"}), 400
    fields, err = _device_meta_fields(key, body)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    kuma_name = monitoring.monitor_name(scanner, key)
    reg = scanner.set_device_meta(
        key,
        name=fields.get("name"),
        category=fields.get("category"),
        type_label=fields.get("type"),
        serial=fields.get("serial"),
        model=fields.get("model"),
        link=fields.get("link"),
    )
    if monitoring.monitor_name(scanner, key) != kuma_name:
        monitoring.name_changed(scanner, key)       # its Kuma monitor follows the new name
    if watch is not None:
        monitoring.set_many(scanner, on=[key] if watch else [], off=[] if watch else [key], by=_who())
        reg = scanner.registry.get(key, reg)
    return jsonify({"ok": True, "registry": reg})


@app.route("/api/devices/<path:key>/hikvision", methods=["POST"])
@guard
def api_hikvision(key):
    """Pull model/serial/firmware from a Hikvision camera/NVR using its saved login."""
    body = request.get_json(silent=True) or {}
    dev = next((d for d in scanner.get_devices() if d.get("key") == key), None)
    ip = body.get("ip") or (dev["ip"] if dev else None)
    c = creds.get(key)
    user = body.get("username") or c["username"]
    pw = body.get("password") or c["password"]
    if not ip:
        return jsonify({"ok": False, "error": "unknown device IP"}), 400
    if not (user or pw):
        return jsonify({"ok": False, "error": "Save a username/password for this device first"})
    res = hikvision.fetch(ip, user, pw)
    if res.get("ok"):
        info = res["info"]
        scanner.set_device_meta(key, serial=info.get("serialNumber") or None,
                                model=info.get("model") or None,
                                firmware=info.get("firmwareVersion") or None, src="device")
        info["display_name"] = scanner.set_device_name(key, hik_own_name(info),
                                                       model=info.get("model"))
        return jsonify({"ok": True, "info": info})
    return jsonify({"ok": False, "error": res.get("error", "failed")})


def _hik_target(key):
    """(ip, user, pw) for a Hikvision device by key, using its saved credentials."""
    dev = next((d for d in scanner.get_devices() if d.get("key") == key), None)
    c = creds.get(key)
    return (dev["ip"] if dev else None), c["username"], c["password"]


@app.route("/api/devices/<path:key>/network", methods=["GET"])
@guard
def api_device_network(key):
    """Read a Hikvision camera's current IPv4 settings (to pre-fill the change form)."""
    ip, user, pw = _hik_target(key)
    if not ip:
        return jsonify({"ok": False, "error": "unknown device IP"}), 400
    if not (user or pw):
        return jsonify({"ok": False, "error": "Save the camera's username/password first"})
    return jsonify(hikvision.get_network(ip, user, pw))


@app.route("/api/devices/<path:key>/set-ip", methods=["POST"])
@guard
def api_device_set_ip(key):
    """Change a Hikvision camera's IP via ISAPI. Validates the new address, uses
    the saved credentials, then (on success) baselines the new IP as 'home' so it
    doesn't flag as drift and kicks off a scan of the new address."""
    body = request.get_json(force=True)
    new_ip = (body.get("ip") or "").strip()
    mask = (body.get("mask") or "").strip()
    gateway = (body.get("gateway") or "").strip()
    try:
        ipaddress.ip_address(new_ip)
    except ValueError:
        return jsonify({"ok": False, "error": f"Invalid new IP: {new_ip}"}), 400
    for label, val in (("subnet mask", mask), ("gateway", gateway)):
        if val:
            try:
                ipaddress.ip_address(val)
            except ValueError:
                return jsonify({"ok": False, "error": f"Invalid {label}: {val}"}), 400
    cur_ip, user, pw = _hik_target(key)
    if not cur_ip:
        return jsonify({"ok": False, "error": "unknown device IP"}), 400
    if not (user or pw):
        return jsonify({"ok": False, "error": "Save the camera's username/password first"})
    if new_ip == cur_ip:
        return jsonify({"ok": False, "error": "that is already the camera's IP"})
    res = hikvision.set_ip(cur_ip, user, pw, new_ip, mask, gateway)
    if res.get("ok"):
        # Accept the new IP as home (no drift flag) and go find it.
        scanner.registry.setdefault(key, {})["known_ip"] = new_ip
        scanner.save_registry()
        scanner.trigger("quick", hosts=[new_ip])
    return jsonify(res)


def _airos_enabled():
    return bool(config.load().get("features", {}).get("airos_change_ip"))


@app.route("/api/devices/<path:key>/airos-network", methods=["GET"])
@guard
def api_airos_network(key):
    """Read a Ubiquiti airOS radio's current management IP over SSH (gated)."""
    if not _airos_enabled():
        return jsonify({"ok": False, "error": "airOS Change IP is turned off in Settings"})
    ip, user, pw = _hik_target(key)        # same saved-credential lookup
    if not ip:
        return jsonify({"ok": False, "error": "unknown device IP"}), 400
    return jsonify(airos.get_network(ip, user, pw))


@app.route("/api/devices/<path:key>/airos-wifi", methods=["GET"])
@guard
def api_airos_wifi(key):
    """Read a Ubiquiti airOS radio's wireless status over SSH (read-only).

    Deliberately NOT behind the airos_change_ip flag: that gate exists because
    set_ip() reboots a backhaul radio, and this only reads. It shares the same
    SSH path, so it doubles as the safe pre-flight for the IP change — if this
    returns data, the saved credentials and SSH access are good.
    """
    ip, user, pw = _hik_target(key)        # same saved-credential lookup
    if not ip:
        return jsonify({"ok": False, "error": "unknown device IP"}), 400
    if not (user or pw):
        return jsonify({"ok": False, "error": "Save the radio's SSH username/password first"})
    res = airos.get_wifi(ip, user, pw)
    if res.get("ok"):
        # Record model/firmware like the Hikvision fetch does. Radios arrive
        # with an empty model, and "copy this login to the same model" in Bulk
        # logins can only group devices that HAVE one.
        scanner.set_device_meta(key, model=res.get("platform") or None)
        res["display_name"] = scanner.set_device_name(key, res.get("deviceName"),
                                                      model=res.get("platform"))
        if res.get("firmware"):
            scanner.registry.setdefault(key, {})["firmware"] = res["firmware"]
            scanner.save_registry()
    return jsonify(res)


@app.route("/api/devices/<path:key>/airos-set-ip", methods=["POST"])
@guard
def api_airos_set_ip(key):
    """Change a Ubiquiti airOS radio's management IP over SSH (gated, EXPERIMENTAL).
    Edits /tmp/system.cfg, persists and reboots. High blast radius on backhaul."""
    if not _airos_enabled():
        return jsonify({"ok": False, "error": "airOS Change IP is turned off in Settings"})
    body = request.get_json(force=True)
    new_ip = (body.get("ip") or "").strip()
    mask = (body.get("mask") or "").strip()
    gateway = (body.get("gateway") or "").strip()
    try:
        ipaddress.ip_address(new_ip)
    except ValueError:
        return jsonify({"ok": False, "error": f"Invalid new IP: {new_ip}"}), 400
    for label, val in (("subnet mask", mask), ("gateway", gateway)):
        if val:
            try:
                ipaddress.ip_address(val)
            except ValueError:
                return jsonify({"ok": False, "error": f"Invalid {label}: {val}"}), 400
    cur_ip, user, pw = _hik_target(key)
    if not cur_ip:
        return jsonify({"ok": False, "error": "unknown device IP"}), 400
    if not (user or pw):
        return jsonify({"ok": False, "error": "Save the radio's SSH username/password first"})
    if new_ip == cur_ip:
        return jsonify({"ok": False, "error": "that is already the radio's IP"})
    res = airos.set_ip(cur_ip, user, pw, new_ip, mask, gateway)
    if res.get("ok"):
        scanner.registry.setdefault(key, {})["known_ip"] = new_ip
        scanner.save_registry()
        scanner.trigger("quick", hosts=[new_ip])
    return jsonify(res)


# ---- Restart a device ----------------------------------------------------
def _restart_enabled():
    return bool(config.load().get("features", {}).get("device_restart"))


def _restart_target(key):
    """What Netwatch can do about restarting this device, as
    (dev, method, reason, detail) — reason is "" when it can go ahead."""
    dev = next((d for d in scanner.get_devices() if d.get("key") == key), None)
    if not dev:
        return None, None, "unknown", "unknown device"
    if not dev.get("ip"):
        return dev, None, "no_ip", "this device has no IP address right now"
    method = restart.method_for(dev)
    if not method:
        return dev, None, "unsupported", ("Netwatch has no way to restart this kind of device (it restarts "
                                          "Hikvision cameras and NVRs, Ubiquiti airOS radios and EdgeSwitches, "
                                          "MikroTik routers and SwOS switches)")
    if not _restart_enabled():
        return dev, method, "off", "Restarting devices is turned off in this site's Settings"
    gate = method.get("gate")
    if gate and not config.load().get("features", {}).get(gate):
        return dev, method, "gate", f"turn on \u201c{method['gate_label']}\u201d in this site's Settings first"
    return dev, method, "", ""


def _restart_audit(dev, job):
    """A restart is the bluntest write there is — it always lands in history."""
    detail = {"restart": job.get("verdict") or job.get("phase"), "method": job.get("method"),
              "result": "ok" if job.get("ok") else "fail"}
    if job.get("seconds_down"):
        detail["seconds_down"] = job["seconds_down"]
    if not job.get("ok") and job.get("msg"):
        detail["error"] = str(job["msg"])[:200]
    try:
        named = {**(dev or {}), "name": (dev or {}).get("name") or (dev or {}).get("device_name") or ""}
        history.log_events([history.build_event("restart", named, detail)])
    except Exception:  # noqa: BLE001
        pass


@app.route("/api/devices/<path:key>/restart", methods=["GET", "POST"])
@guard
def api_device_restart(key):
    """Restart one device and watch it come back (restart.py).

    GET always answers 200 and says whether this device can be restarted, why
    not if it can't, and where a running restart has got to — a page asks it for
    every device it draws, so "no" is an answer, not an error. POST starts one,
    and refuses without {"confirm": true} so nothing reboots a camera on a stray
    click or a retried request. Both return at once: the watching runs on its own
    thread here, and the page polls GET for the verdict."""
    dev, method, reason, detail = _restart_target(key)
    job = restart.state(key)
    if request.method == "GET":
        out = {"ok": True, "can_restart": not reason, "job": job}
        if method:
            out["method"], out["method_label"], out["warning"] = method["id"], method["label"], method["warn"]
        if reason:
            out["reason"], out["detail"] = reason, detail
        return jsonify(out)
    if reason:
        return jsonify({"ok": False, "error": detail, "reason": reason}), \
            (404 if reason == "unknown" else 403 if reason in ("off", "gate") else 400)
    if restart.running(key):
        return jsonify({"ok": False, "error": "a restart of this device is already running", "job": job}), 409
    c = creds.get(key)
    user, pw = c["username"], c["password"]
    # SwOS and RouterOS ship admin with a blank password, so a saved username alone counts.
    if not (user or pw):
        return jsonify({"ok": False, "error": "Save this device's login first"}), 400
    if not (request.get_json(silent=True) or {}).get("confirm"):
        return jsonify({"ok": False, "needs_confirm": True, "warning": method["warn"],
                        "method_label": method["label"]}), 200
    started = restart.start(dev, user, pw, method,
                            on_done=lambda j: (_restart_audit(dev, j),
                                               scanner.trigger("quick", hosts=[dev["ip"]])))
    if started is None:
        return jsonify({"ok": False, "error": "a restart of this device is already running",
                        "job": restart.state(key)}), 409
    return jsonify({"ok": True, "job": started})


# ---- MikroTik / RouterOS -------------------------------------------------
def _mtk_enabled():
    return bool(config.load().get("features", {}).get("mikrotik_manage"))


def _mtk_target(key):
    """(dev, mac, ip, user, pw) for a device by key. RouterOS ships admin/blank,
    so default the username to 'admin' when no login is saved."""
    dev = next((d for d in scanner.get_devices() if d.get("key") == key), None)
    c = creds.get(key)
    mac = mikrotik.normalize_mac((dev or {}).get("mac", ""))
    ip = (dev or {}).get("ip", "")
    return dev, mac, ip, (c["username"] or "admin"), c["password"]


def _mtk_audit(dev, action, result, extra=None):
    """Write a management action to the device's history (audit trail — this is
    config power over a client's router)."""
    detail = {"mikrotik_action": action, "result": "ok" if result.get("ok") else "fail"}
    if not result.get("ok") and result.get("error"):
        detail["error"] = str(result["error"])[:200]
    if extra:
        detail.update(extra)
    try:
        history.log_events([history.build_event("mikrotik", dev or {}, detail)])
    except Exception:
        pass


@app.route("/api/mikrotik/neighbors", methods=["GET", "POST"])
@guard
def api_mikrotik_neighbors():
    """MNDP discovery — every MikroTik that answers on the LAN, found BY MAC with
    no ROUTER login (WinBox's Neighbors tab). Behind the site login like its
    siblings: it triggers broadcast solicits and reveals router versions. The hub
    reaches it with the hub key."""
    try:
        secs = min(8.0, max(2.0, float(request.args.get("t", 4))))
    except (TypeError, ValueError):
        secs = 4.0
    res = mikrotik.discover(timeout=secs)
    if res.get("ok"):
        by_mac = {identify.normalize_mac(d.get("mac", "")): d
                  for d in scanner.get_devices() if d.get("mac")}
        for n in res["neighbors"]:
            dev = by_mac.get(identify.normalize_mac(n.get("mac", "")))
            n["tracked"] = bool(dev)
            n["key"] = dev.get("key") if dev else None
            n["saved_name"] = (dev or {}).get("name") or (dev or {}).get("device_name") or ""
    return jsonify(res)


@app.route("/api/mikrotik/adopt", methods=["POST"])
@guard
def api_mikrotik_adopt():
    """Scan a discovered neighbour's IP so the normal pipeline tracks + identifies
    it and it appears in the device list."""
    body = request.get_json(force=True)
    ip = (body.get("ip") or "").strip()
    if not mikrotik._valid_ip(ip):
        return jsonify({"ok": False, "error": "need the neighbour's IP to scan it"}), 400
    scanner.trigger("quick", hosts=[ip])
    return jsonify({"ok": True, "scanning": True})


@app.route("/api/devices/<path:key>/mikrotik", methods=["GET"])
@guard
def api_mikrotik_status(key):
    """Structured RouterOS status. MAC-Telnet first (works with no IP), the API on
    the discovered IP as the fallback. Records model/serial/firmware/name like the
    Hikvision/airOS fetches so the grid shows a real name."""
    dev, mac, ip, user, pw = _mtk_target(key)
    if not (mac or ip):
        return jsonify({"ok": False, "error": "unknown device MAC/IP"}), 400
    res = mikrotik.snapshot(mac=mac, ip=ip, user=user, password=pw, prefer="mac")
    if res.get("ok"):
        scanner.set_device_meta(key, serial=res.get("serial") or None,
                                model=res.get("model") or None)
        res["display_name"] = scanner.set_device_name(key, res.get("identity"),
                                                      model=res.get("model"))
        if res.get("firmware"):
            scanner.registry.setdefault(key, {})["firmware"] = res["firmware"]
            scanner.save_registry()
    return jsonify(res)


# ---- managed units: routers listed the same way switches are ----------------
# Reading a router costs a live API call, so the list serves the last reading and
# only re-reads when it is stale or ?refresh=1 — deliberately no extra poller
# thread on a 1 GB client Pi (switchmon already has one).
_ROUTER_CACHE = {}          # device key -> {"view": …, "ts": …}
_ROUTER_TTL = 300


def _is_mikrotik_dev(d):
    """A RouterOS device. A SwOS switch is MikroTik too but has no RouterOS API —
    it is listed with the switches (switchmon/swos.py) instead."""
    if switchmon.driver(d) is not None:
        return False
    text = " ".join(str(d.get(k) or "") for k in ("vendor", "model", "hostname", "os", "banner")).lower()
    if "mikrotik" in text or "routerboard" in text or "routeros" in text:
        return True
    # Port 8291 on its own is NOT proof — HP printers expose it too (the same trap
    # identify.classify() calls out), and a printer in the managed-unit list is noise.
    return (8291 in (d.get("ports") or [])
            and (d.get("category") or "") not in ("printer", "camera", "nvr", "nas", "voip"))


# MNDP costs no login at all, so a router we cannot log into still shows its real
# name, model, RouterOS version and uptime instead of an empty "Login needed" card.
_MNDP_CACHE = {"ts": 0.0, "by_mac": {}}
_MNDP_TTL = 120


def _mndp_map():
    """{key -> neighbour} keyed by BOTH MAC and IP. A MikroTik announces MNDP from
    the interface's own MAC, which is often not the MAC the scanner learned from
    ARP (Tankwa's .254 is exactly that), so the IP is the reliable second key."""
    now = time.time()
    if now - _MNDP_CACHE["ts"] < _MNDP_TTL:
        return _MNDP_CACHE["by_mac"]
    try:
        res = mikrotik.discover(timeout=3)
        if res.get("ok"):
            idx = {}
            for n in res.get("neighbors") or []:
                mac = identify.normalize_mac(n.get("mac", ""))
                if mac:
                    idx[mac] = n
                if n.get("ipv4"):
                    idx[n["ipv4"]] = n
            _MNDP_CACHE["by_mac"] = idx
    except OSError:
        pass
    _MNDP_CACHE["ts"] = now          # don't retry a failed listen every request
    return _MNDP_CACHE["by_mac"]


def _fmt_secs(s):
    try:
        s = int(s)
    except (TypeError, ValueError):
        return ""
    d, s = divmod(s, 86400)
    h, m = divmod(s, 3600)[0], divmod(s % 3600, 60)[0]
    return (f"{d}d {h}h" if d else (f"{h}h {m}m" if h else f"{m}m"))


def _router_view(dev, force=False):
    """One router in the shape the managed-unit list draws (mirrors _sw_view)."""
    key = dev.get("key")
    now = time.time()
    hit = _ROUTER_CACHE.get(key)
    if hit and not force and now - hit["ts"] < _ROUTER_TTL:
        v = dict(hit["view"])
        v["online"] = bool(dev.get("online"))
        return v
    view = {"key": key, "ip": dev.get("ip"), "mac": dev.get("mac"), "type": "router",
            "name": dev.get("name") or dev.get("device_name") or dev.get("ip") or key,
            "online": bool(dev.get("online")), "problems": [], "ports": [],
            "model": dev.get("model") or "", "read_ts": None}
    def _from_mndp():
        """Whatever the router broadcasts about itself — no login involved."""
        idx = _mndp_map()
        nb = idx.get(identify.normalize_mac(dev.get("mac") or "")) or idx.get(dev.get("ip") or "")
        if not nb:
            return
        view["mndp"] = True
        view["model"] = view.get("model") or nb.get("board") or ""
        view["version"] = view.get("version") or nb.get("version") or ""
        view["identity"] = view.get("identity") or nb.get("identity") or ""
        if nb.get("uptime") and not view.get("uptime"):
            view["uptime"] = _fmt_secs(nb["uptime"])
        if not dev.get("name") and nb.get("identity"):
            view["name"] = nb["identity"]

    if not mikrotik._valid_ip(dev.get("ip")):
        view.update({"ok": False, "kind": "no_ip", "error": "no IP for this router yet"})
        _from_mndp()
        return view
    c = creds.get(key)
    rep = mikrotik.api_report(dev.get("ip"), c["username"] or "admin", c["password"])
    if rep.get("ok"):
        view.update(mikrotik.summarize(rep))
        view.update({"ok": True, "kind": None, "read_ts": int(now)})
        view["name"] = dev.get("name") or view.get("identity") or view["name"]
    else:
        err = str(rep.get("error", ""))
        low = err.lower()
        if "refused" in low:
            kind = "api_off"
        elif "login" in low or "denied" in low or "not permitted" in low:
            kind = "auth_failed"
        else:
            kind = "unreachable"
        if kind != "unreachable" and not (c["username"] or c["password"]):
            kind = "no_login"
        view.update({"ok": False, "kind": kind, "error": err})
        _from_mndp()
    _ROUTER_CACHE[key] = {"view": view, "ts": now}
    return view


@app.route("/api/routers")
@guard
def api_routers():
    """Every MikroTik router on the site, shaped like /api/switches so routers and
    switches render in one managed-unit list. Served from the last reading;
    ?refresh=1 re-reads them."""
    force = request.args.get("refresh") in ("1", "true", "yes")
    devs = [d for d in scanner.get_devices() if _is_mikrotik_dev(d)]
    out = []
    if devs:
        with ThreadPoolExecutor(max_workers=4) as ex:
            out = list(ex.map(lambda d: _router_view(d, force), devs))
    out.sort(key=lambda r: (not r.get("online"), (r.get("name") or "").lower()))
    return jsonify({"ok": True, "routers": out, "manage": _mtk_enabled(),
                    "problems": [p for r in out for p in r.get("problems") or []]})


@app.route("/api/devices/<path:key>/mikrotik/report", methods=["GET"])
@guard
def api_mikrotik_report(key):
    """The full management-console snapshot: ports (link/rate/PoE), the merged
    connected-device table (which device on which port), routes, firewall, DNS,
    wireless, neighbours and logs. Read-only, over the RouterOS API on the IP
    discovery gave us. Connected devices get an offline vendor guess."""
    dev, mac, ip, user, pw = _mtk_target(key)
    if not mikrotik._valid_ip(ip):
        return jsonify({"ok": False, "error": "the router's IP isn't known yet — "
                        "open Router info first so discovery can learn it"}), 400
    res = mikrotik.api_report(ip, user, pw)
    if res.get("ok"):
        res["manage_enabled"] = _mtk_enabled()
        # Same normalised ports/summary/poe the list cards use, so one renderer
        # draws both. `connected` stays the full list here (the card wants a count).
        s = mikrotik.summarize(res)
        res["ports"], res["summary"], res["poe"] = s["ports"], s["summary"], s["poe"]
        for c in res.get("connected", []):
            c["vendor"] = identify.vendor_for_mac(c.get("mac", ""), online_ok=False)
        # Opening a router keeps its card in the managed-unit list fresh.
        if dev:
            view = {"key": key, "ip": ip, "mac": dev.get("mac"), "type": "router",
                    "name": dev.get("name") or dev.get("device_name") or ip,
                    "online": bool(dev.get("online")), "problems": [],
                    "ok": True, "kind": None, "read_ts": int(time.time())}
            view.update(mikrotik.summarize(res))
            view["name"] = dev.get("name") or view.get("identity") or view["name"]
            _ROUTER_CACHE[key] = {"view": view, "ts": time.time()}
    else:
        # The console reads over the RouterOS API. Point at the two things that
        # actually block it on a client's router: no saved login, or the API off.
        has_login = bool(creds.get(key).get("password") or creds.get(key).get("username"))
        err = str(res.get("error", ""))
        if "refused" in err.lower():
            res["hint"] = ("The RouterOS API service (port 8728) looks turned off on this router. "
                           "Open the Terminal tab and run  /ip service enable api  (needs the router's "
                           "login saved), then reopen the console.")
        elif not has_login:
            res["hint"] = ("No admin login is saved for this router. Save its username/password on the "
                           "device's Access tab (RouterOS ships admin with a blank password).")
        else:
            res["hint"] = ("Couldn't read the router — check the saved admin login and that it's reachable "
                           "over the VPN. The Terminal tab (MAC-Telnet) may still work.")
    return jsonify(res)


@app.route("/api/devices/<path:key>/mikrotik/action", methods=["POST"])
@guard
def api_mikrotik_action(key):
    """Gated management writes over the RouterOS API (needs the router's IP, which
    discovery supplies). Requires the mikrotik_manage flag; every action is
    written to the device history."""
    if not _mtk_enabled():
        return jsonify({"ok": False, "error": "MikroTik management is turned off in Settings"})
    body = request.get_json(force=True)
    action = (body.get("action") or "").strip()
    dev, mac, ip, user, pw = _mtk_target(key)
    if not mikrotik._valid_ip(ip):
        return jsonify({"ok": False, "error": "management actions need the router's IP — "
                        "open Router info first to discover it"}), 400
    if action == "set-identity":
        res = mikrotik.api_set_identity(ip, user, pw, body.get("name", ""))
    elif action == "interface":
        res = mikrotik.api_set_interface(ip, user, pw, body.get("id", ""),
                                         bool(body.get("enable")))
    elif action == "poe":
        res = mikrotik.api_set_poe(ip, user, pw, body.get("id", ""), body.get("mode", ""))
    elif action == "poe-cycle":
        res = mikrotik.api_poe_cycle(ip, user, pw, body.get("port", ""), body.get("duration", 5))
    elif action == "reboot":
        res = mikrotik.api_reboot(ip, user, pw)
    elif action == "export":
        res = mikrotik.api_export(ip, user, pw)
    else:
        return jsonify({"ok": False, "error": "unknown action %r" % action}), 400
    _mtk_audit(dev, action, res, {k: body[k] for k in ("name", "id", "mode", "enable", "port", "duration")
                                  if k in body})
    if res.get("ok") and action == "set-identity" and body.get("name"):
        scanner.set_device_name(key, body["name"].strip())
    return jsonify(res)


@app.route("/api/devices/<path:key>/mikrotik/console", methods=["POST"])
@guard
def api_mikrotik_console(key):
    """The web terminal: run one RouterOS console command. Gated. MAC-Telnet
    first, SSH-over-IP fallback. Router-stranding commands need an explicit
    confirm. Logged to history."""
    if not _mtk_enabled():
        return jsonify({"ok": False, "error": "MikroTik management is turned off in Settings"})
    body = request.get_json(force=True)
    command = (body.get("command") or "").strip()
    if not command:
        return jsonify({"ok": False, "error": "no command"}), 400
    warning = mikrotik.dangerous_command(command)
    if warning and not body.get("confirm"):
        return jsonify({"ok": False, "needs_confirm": True, "warning": warning})
    dev, mac, ip, user, pw = _mtk_target(key)
    res = mikrotik.run_console(mac=mac, ip=ip, user=user, password=pw,
                               command=command, prefer="mac")
    _mtk_audit(dev, "console", res, {"command": command[:200]})
    return jsonify(res)


# ---- Ubiquiti EdgeSwitch / UISP switches ------------------------------------------
SWITCH_BACKUP_DIR = os.path.join(os.environ.get("NETWATCH_DATA", "/data"), "switch_backups")
SWITCH_BACKUP_KEEP = 10
# EdgeSwitch backups are .tar.gz, SwOS ones the .swb its Backup button saves.
_SW_BACKUP_RE = re.compile(r"^\d{8}-\d{6}\.(tar\.gz|swb)$")
# Actions that can take a port's device (or everything behind the port) off the
# network. Refused outright on a port the Pi or the router is on.
_SW_CUTTING = {"port-off", "poe-off", "poe-cycle", "cable-test"}


def _sw_enabled():
    return bool(config.load().get("features", {}).get("switch_manage"))


def _switch_meta(key, model=None, serial=None, firmware=None):
    """Model/serial/firmware the switch reports about itself. Only written when
    something changed — the monitor calls this every poll and the registry lives
    on the Pi's SD card."""
    reg = scanner.registry.get(key, {})
    if (model and reg.get("model") != model) or (serial and reg.get("serial") != serial):
        scanner.set_device_meta(key, model=model or None, serial=serial or None)
    if firmware and scanner.registry.get(key, {}).get("firmware") != firmware:
        scanner.registry.setdefault(key, {})["firmware"] = firmware
        scanner.save_registry()


def _sw_brief(d):
    return {"key": d.get("key"), "name": d.get("name") or d.get("device_name") or d.get("model") or "",
            "ip": d.get("ip"), "mac": d.get("mac"), "vendor": d.get("vendor"),
            "category": d.get("category"), "online": bool(d.get("online")), "watch": bool(d.get("watch"))}


def _sw_view(key, dev, row, by_mac, problems):
    """One switch as both UIs draw it: the monitor's last reading with every MAC
    on every port resolved to the Netwatch device it belongs to."""
    snap = row.get("snap") or {}
    poll_s = max(1, int((config.load().get("switch") or {}).get("poll_min", 5))) * 60
    protected, uplinks = snap.get("protected") or {}, snap.get("uplinks") or {}
    memory = snap.get("port_memory") or {}
    ports = []
    for p in snap.get("ports") or []:
        q = dict(p)
        known, unknown = [], 0
        for m in p.get("macs") or []:
            d = by_mac.get(m["mac"])
            if d and d.get("key") != key:
                known.append(_sw_brief(d))
            elif not d:
                unknown += 1
        q["devices"], q["unknown_macs"] = known, unknown
        q.pop("macs", None)
        if not p.get("up") and memory.get(p["id"]):
            mem = memory[p["id"]]
            q["last_devices"] = [_sw_brief(by_mac[m]) for m in mem.get("macs") or [] if m in by_mac][:8]
            q["last_seen_ts"] = mem.get("ts")
        q["protected"] = protected.get(p["id"]) or ""
        q["uplink"] = uplinks.get(p["id"]) or 0
        ports.append(q)
    ts = row.get("ts")
    name = (dev or {}).get("name") or (dev or {}).get("device_name") or (snap.get("device") or {}).get("name") or (dev or {}).get("ip") or key
    return {
        "key": key, "ip": (dev or {}).get("ip") or row.get("ip"), "mac": (dev or {}).get("mac"),
        "name": name, "online": bool((dev or {}).get("online")),
        "ok": bool(row.get("ok")), "kind": row.get("kind") or ("pending" if not row else None),
        "error": row.get("error"), "ts": ts, "read_ts": snap.get("ts"),
        "stale": bool(snap.get("ts")) and time.time() - snap["ts"] > 3 * poll_s,
        "model": (snap.get("device") or {}).get("model") or row.get("model") or (dev or {}).get("model") or "",
        "device": snap.get("device") or {}, "health": snap.get("health") or {},
        "poe": snap.get("poe") or {}, "summary": snap.get("summary") or {},
        "ports": ports, "lags": snap.get("lags") or [], "vlans": snap.get("vlans") or [],
        "services": snap.get("services") or {},
        "caps": snap.get("caps") or {},
        "problems": [p for p in problems if p["key"] == key],
        "poll_min": poll_s // 60,
    }


def _sw_devices():
    devs = scanner.get_devices()
    by_mac = {edgeswitch.normalize_mac(d.get("mac")): d for d in devs if d.get("mac")}
    return devs, by_mac


@app.route("/api/switches")
def api_switches():
    """Every Ubiquiti EdgeSwitch/UISP switch on the site with its ports, PoE,
    traffic, what is plugged in where, and its problems — from the monitor's last
    reading (no login happens here; POST …/switch/poll reads live)."""
    devs, by_mac = _sw_devices()
    snap = switchmon.monitor.snapshot()
    out = []
    for d in devs:
        if not switchmon.is_switch(d) and d.get("key") not in snap["switches"]:
            continue
        out.append(_sw_view(d["key"], d, snap["switches"].get(d["key"]) or {}, by_mac, snap["problems"]))
    out.sort(key=lambda s: (not s["online"], s["name"].lower()))
    return jsonify({"ok": True, "switches": out, "manage": _sw_enabled(),
                    "polling": snap["busy"], "problems": snap["problems"]})


def _thin(rows, max_points):
    step = max(1, -(-len(rows) // max_points))
    return rows[::step] if step > 1 else rows


@app.route("/api/devices/<path:key>/switch")
def api_switch_detail(key):
    """One switch: the live view plus history — switch-level series, a light
    per-port series for sparklines (or one port in detail with ?port=), and the
    port events (link down/up, speed changes, PoE lost, devices moving port)."""
    devs, by_mac = _sw_devices()
    dev = next((d for d in devs if d.get("key") == key), None)
    snap = switchmon.monitor.snapshot()
    row = snap["switches"].get(key)
    if not dev and not row:
        return jsonify({"ok": False, "error": "unknown switch"}), 404
    try:
        hours = max(1, min(int(request.args.get("hours", 24)), 24 * 30))
    except (TypeError, ValueError):
        hours = 24
    view = _sw_view(key, dev, row or {}, by_mac, snap["problems"])
    win = hours * 3600
    view["hours"] = hours
    view["series"] = _thin(history.switch_series(key, window_s=win), 300)
    port = (request.args.get("port") or "").strip()[:12]
    rows = history.switch_port_series(key, window_s=win, port=port or None)
    by_port = {}
    for r in rows:
        by_port.setdefault(r["port"], []).append({k: r[k] for k in ("ts", "up", "speed", "poe_w", "rx_bps", "tx_bps", "errors", "dropped")})
    view["port_series"] = {p: _thin(v, 300 if port else 96) for p, v in by_port.items()}
    view["events"] = history.events(key=key, etype="switch", since=int(time.time()) - max(win, 7 * 86400), limit=150)
    view["manage"] = _sw_enabled()
    return jsonify({"ok": True, **view})


@app.route("/api/devices/<path:key>/switch/poll", methods=["POST"])
@guard
def api_switch_poll(key):
    """Read this switch now (uses its saved login)."""
    switchmon.monitor.poll_round(force=True, only=key)
    devs, by_mac = _sw_devices()
    dev = next((d for d in devs if d.get("key") == key), None)
    snap = switchmon.monitor.snapshot()
    row = snap["switches"].get(key)
    if not row:
        return jsonify({"ok": False, "error": "not a switch Netwatch can read (is it online?)"}), 404
    return jsonify({"ok": True, **_sw_view(key, dev, row, by_mac, snap["problems"]), "manage": _sw_enabled()})


def _sw_audit(dev, action, result, extra=None):
    detail = {"switch_event": "action", "action": action, "result": "ok" if result.get("ok") else "fail"}
    if not result.get("ok") and result.get("error"):
        detail["error"] = str(result["error"])[:200]
    detail.update(extra or {})
    try:
        named = {**(dev or {}), "name": (dev or {}).get("name") or (dev or {}).get("device_name") or ""}
        history.log_events([history.build_event("switch", named, detail)])
    except Exception:  # noqa: BLE001
        pass


@app.route("/api/devices/<path:key>/switch/action", methods=["POST"])
@guard
def api_switch_action(key):
    """Change the switch: a port on/off, PoE mode, PoE power cycle, port name,
    cable test, find-me LEDs, reboot. Needs Settings → Manage switches. Ports the
    Pi or the router are on are refused for anything that can cut them off — the
    check is made on a FRESH read of the MAC table, not the last poll. Every action
    lands in the switch's history."""
    if not _sw_enabled():
        return jsonify({"ok": False, "error": "Switch management is turned off in Settings"}), 403
    body = request.get_json(force=True, silent=True) or {}
    action = str(body.get("action") or "").strip()
    port = str(body.get("port") or "").strip()[:12]
    dev = next((d for d in scanner.get_devices() if d.get("key") == key), None)
    if not dev or not dev.get("ip"):
        return jsonify({"ok": False, "error": "unknown switch"}), 404
    drv = switchmon.driver(dev) or edgeswitch
    c = creds.get(key)
    ip, user, pw = dev["ip"], c["username"], c["password"]
    # SwOS's factory login is admin with NO password, so a saved user alone counts.
    if not (user or pw):
        return jsonify({"ok": False, "error": "Save the switch's login first"}), 400
    kind = {"port": "port-off" if body.get("enabled") is False else "port-on",
            "poe": "poe-off" if str(body.get("mode") or "") == edgeswitch.POE_OFF else "poe-mode"}.get(action, action)
    extra = {k: body[k] for k in ("port", "enabled", "mode", "name", "off_s", "on") if k in body}
    if port or action in ("port", "poe", "poe-cycle", "name", "cable-test"):
        if not re.match(r"^\d+/\d+$", port):
            return jsonify({"ok": False, "error": "which port? (e.g. 0/3)"}), 400
    if kind in _SW_CUTTING or kind == "reboot":
        live = drv.read(ip, user, pw)
        if not live.get("ok"):
            return jsonify({"ok": False, "error": f"couldn't check the switch first: {live.get('error')}"}), 502
        prot = switchmon.protected_ports(live)
        if kind in _SW_CUTTING and port in prot:
            res = {"ok": False, "error": f"Refused — {prot[port]}. Switching it off would cut this site off "
                                         "and nobody could switch it back on remotely.", "protected": True}
            _sw_audit(dev, kind, res, extra)
            return jsonify(res), 409
        ups = switchmon.uplink_ports(live)
        if not body.get("confirm"):
            if kind in _SW_CUTTING and port in ups:
                return jsonify({"ok": False, "needs_confirm": True,
                                "warning": f"Port {port} has {ups[port]} devices behind it (another switch or a "
                                           "wireless link). All of them lose their connection."})
            if kind == "reboot":
                return jsonify({"ok": False, "needs_confirm": True,
                                "warning": "Every device on this switch loses its network (and PoE power) for "
                                           "about two minutes while it restarts."})
    if action == "port":
        res = drv.set_port(ip, user, pw, port, enabled=bool(body.get("enabled")))
    elif action == "poe":
        mode = str(body.get("mode") or "")
        if not re.match(r"^[a-z0-9-]{2,16}$", mode):
            return jsonify({"ok": False, "error": "bad PoE mode"}), 400
        res = drv.set_port(ip, user, pw, port, poe=mode)
    elif action == "poe-cycle":
        res = drv.poe_cycle(ip, user, pw, port, body.get("off_s", 8))
    elif action == "name":
        res = drv.set_port(ip, user, pw, port, name=str(body.get("name") or "").strip())
    elif action == "cable-test":
        res = drv.cable_test(ip, user, pw, port)
    elif action == "locate":
        res = drv.locate(ip, user, pw, on=bool(body.get("on", True)))
    elif action == "reboot":
        res = drv.reboot(ip, user, pw)
    else:
        return jsonify({"ok": False, "error": f"unknown action {action!r}"}), 400
    _sw_audit(dev, kind, res, extra)
    if res.get("ok") and action != "reboot":
        threading.Thread(target=switchmon.monitor.poll_round, kwargs={"force": True, "only": key},
                         daemon=True).start()
    return jsonify(res), (200 if res.get("ok") else 502)


def _sw_backup_dir(key):
    return os.path.join(SWITCH_BACKUP_DIR, re.sub(r"[^A-Za-z0-9_.-]", "-", key))


def _sw_backup_list(key):
    d = _sw_backup_dir(key)
    try:
        names = sorted((n for n in os.listdir(d) if _SW_BACKUP_RE.match(n)), reverse=True)
    except OSError:
        return []
    return [{"name": n, "size": os.path.getsize(os.path.join(d, n)),
             "ts": int(os.path.getmtime(os.path.join(d, n)))} for n in names]


@app.route("/api/devices/<path:key>/switch/backups", methods=["GET", "POST"])
@guard
def api_switch_backups(key):
    """GET lists the switch config backups kept on the Pi; POST takes a new one
    (the switch's own .tar.gz, the file its web page's Backup button gives)."""
    if request.method == "GET":
        return jsonify({"ok": True, "backups": _sw_backup_list(key)})
    dev = next((d for d in scanner.get_devices() if d.get("key") == key), None)
    if not dev or not dev.get("ip"):
        return jsonify({"ok": False, "error": "unknown switch"}), 404
    c = creds.get(key)
    res = (switchmon.driver(dev) or edgeswitch).backup(dev["ip"], c["username"], c["password"])
    if not res.get("ok"):
        return jsonify({"ok": False, "error": res.get("error")}), 502
    d = _sw_backup_dir(key)
    os.makedirs(d, exist_ok=True)
    name = time.strftime("%Y%m%d-%H%M%S") + (res.get("ext") or ".tar.gz")
    with open(os.path.join(d, name), "wb") as f:
        f.write(res["content"])
    for old in _sw_backup_list(key)[SWITCH_BACKUP_KEEP:]:
        try:
            os.remove(os.path.join(d, old["name"]))
        except OSError:
            pass
    _sw_audit(dev, "backup", {"ok": True}, {"file": name, "bytes": len(res["content"])})
    return jsonify({"ok": True, "name": name, "backups": _sw_backup_list(key)})


@app.route("/api/devices/<path:key>/switch/backups/<name>")
@guard
def api_switch_backup_download(key, name):
    if not _SW_BACKUP_RE.match(name):
        return jsonify({"ok": False, "error": "no such backup"}), 404
    path = os.path.join(_sw_backup_dir(key), name)
    if not os.path.exists(path):
        return jsonify({"ok": False, "error": "no such backup"}), 404
    dev = next((d for d in scanner.get_devices() if d.get("key") == key), None) or {}
    label = re.sub(r"[^A-Za-z0-9_.-]", "-", dev.get("name") or dev.get("device_name") or dev.get("ip") or "switch")
    return send_file(path, mimetype="application/gzip" if name.endswith(".gz") else "application/octet-stream",
                     as_attachment=True,
                     download_name=f"{label}-{name}")


@app.route("/api/problems")
def api_problems():
    """All detected problems (IP conflict, risky ports, duplicate MAC, IP drift,
    degrading wireless links) for the dashboard's Problems panel."""
    return jsonify({"problems": scanner.problems() + _radio_problems() + _switch_problems()})


def _radio_problems():
    """Radio telemetry findings, shaped like scanner.problems() entries so the
    Problems panel and the hub render them with no special-casing."""
    devs = {d["key"]: d for d in scanner.get_devices() if d.get("key")}
    out = []
    for p in radiomon.monitor.snapshot()["problems"]:
        d = devs.get(p["key"], {})
        out.append({
            "type": "wifi_degraded",
            "severity": "high" if p["level"] == "crit" else "medium",
            "ip": p.get("ip"), "detail": p["what"], "fix": p["hint"],
            "metric": p["metric"], "peer": p.get("peer"),
            "devices": [{"key": p["key"], "ip": p.get("ip"),
                         "name": d.get("name") or p.get("device"),
                         "vendor": d.get("vendor"), "category": d.get("category"),
                         "mac": d.get("mac"), "online": d.get("online", True)}],
        })
    return out


def _switch_problems():
    """Switch findings (port down, speed drop, PoE lost, errors, flapping, heat,
    PoE budget, login) in the Problems-panel shape."""
    devs = {d["key"]: d for d in scanner.get_devices() if d.get("key")}
    out = []
    for p in switchmon.monitor.snapshot()["problems"]:
        d = devs.get(p["key"], {})
        out.append({
            "type": "switch",
            "severity": {"crit": "high", "warn": "medium"}.get(p["level"], "low"),
            "ip": p.get("ip"), "detail": p["what"], "fix": p["hint"],
            "metric": p["metric"], "port": p.get("port"), "port_name": p.get("port_name"),
            "devices": [{"key": p["key"], "ip": p.get("ip"),
                         "name": d.get("name") or d.get("device_name") or p.get("device"),
                         "vendor": d.get("vendor"), "category": d.get("category"),
                         "mac": d.get("mac"), "online": d.get("online", True)}],
        })
    return out


@app.route("/api/devices/<path:key>/ack-ip", methods=["POST"])
def api_ack_ip(key):
    """Accept a device's current IP as its new 'home' — clears its drift flag."""
    return jsonify(scanner.acknowledge_ip(key))


@app.route("/api/conflicts/clear", methods=["POST"])
def api_conflicts_clear():
    """Clear an IP-conflict or identity-rotated problem and re-test it: the
    card is hidden unless fresh (post-clear) sightings show 2+ claimants again,
    and a targeted rescan of the IP starts right away to provide that evidence."""
    body = request.get_json(force=True)
    ip = (body.get("ip") or "").strip()
    if not ip:
        return jsonify({"ok": False, "error": "ip required"}), 400
    res = scanner.clear_conflict(ip)
    scanner.wake()
    scanner.trigger(mode="quick", hosts=[ip])
    return jsonify(res)


@app.route("/api/bridge-macs", methods=["GET", "POST"])
def api_bridge_macs():
    """MACs of proxy-ARP bridges (a wireless station answering ARP for every
    device behind it). Devices fronted by one are tracked per-IP, so each
    camera behind the link shows as its own device. POST {mac, enable}."""
    cur = set(config.load()["scan"].get("bridge_macs") or [])
    if request.method == "GET":
        return jsonify({"bridge_macs": sorted(cur)})
    if not siteauth.allowed(request, session):     # a scan setting
        return siteauth._denied(request, session)
    body = request.get_json(force=True)
    mac = identify.normalize_mac(body.get("mac") or "")
    if not mac:
        return jsonify({"ok": False, "error": "invalid MAC"}), 400
    enable = bool(body.get("enable", True))
    (cur.add if enable else cur.discard)(mac)
    config.update({"scan": {"bridge_macs": sorted(cur)}})
    scanner.apply_bridge_macs(cur, added=mac if enable else None)
    scanner.wake()
    scanner.trigger(mode="quick")   # re-key the fronted devices right away
    return jsonify({"ok": True, "bridge_macs": sorted(cur)})


@app.route("/api/problems/ack-drift", methods=["POST"])
def api_ack_drift():
    """Acknowledge every current IP-drift at once (after an intentional renumber)."""
    return jsonify(scanner.acknowledge_all_drift())


@app.route("/api/conflicts")
def api_conflicts():
    """IP address conflicts for the hub. LIVE conflicts (claimants proven up
    together) stay under `conflicts` — the key old hubs read, so sequential
    identity rotation / IP reuse no longer pages anyone — and the softer
    entries ship under `rotated` for hubs that know the new type."""
    allc = scanner.ip_conflicts()
    return jsonify({"conflicts": [c for c in allc if c.get("live")],
                    "rotated": [c for c in allc if not c.get("live")]})


@app.route("/api/health")
def api_health():
    """Overall liveness — a target for a Kuma 'is Netwatch up' HTTP monitor."""
    return jsonify({"status": "up", "service": "netwatch"})


@app.route("/api/devices/<path:key>/health")
def api_device_health(key):
    """Per-device health for a Kuma HTTP monitor: 200 = online, 503 = offline."""
    dev = next((d for d in scanner.get_devices() if d.get("key") == key), None)
    if not dev:
        return jsonify({"status": "unknown"}), 404
    if dev.get("online"):
        return jsonify({"status": "up", "ip": dev["ip"], "name": dev.get("name", ""),
                        "ping": dev.get("rtt")}), 200
    return jsonify({"status": "down", "ip": dev.get("ip", ""), "name": dev.get("name", "")}), 503


@app.route("/api/devices/<path:key>/kuma", methods=["GET", "POST"])
def api_kuma(key):
    """Get/set the device's Uptime Kuma link.

    The auto monitor follows the Monitored switch (monitoring.py), so POST
    {action:"create"} / {action:"remove"} just switch monitoring on / off (the
    monitor is created or paused in the background); {token:"..."} sets a
    hand-made push monitor's token. POSTs need the site login.

    GET stays open for the monitor's state, but the push token and its URL go
    only to the site login or the hub key: whoever holds the token can send the
    monitor fake "up" beats and hide a real outage. Others get `has_token`.
    """
    cfg = config.load()
    ki = cfg["integrations"]["kuma"]
    base = kuma.effective_base(ki)

    if request.method == "POST":
        if not siteauth.allowed(request, session):
            return siteauth._denied(request, session)
        body = request.get_json(force=True)
        action = body.get("action")
        if action in ("create", "remove"):
            if not monitoring.kuma_follows():
                return jsonify({"ok": False, "error": "Set the Kuma URL, username and password in Settings first"})
            on = action == "create"
            res = monitoring.set_many(scanner, on=[key] if on else [], off=[] if on else [key], by=_who())
            if res["unknown"]:
                return jsonify({"ok": False, "error": "unknown device"}), 404
            return jsonify({"ok": True, "monitored": on, "queued": True})
        # manual token set
        scanner.set_device_meta(key, kuma_token=body.get("token", ""))

    reg = scanner.registry.get(key, {})
    token = reg.get("kuma_token") or ""
    shown = token if siteauth.allowed(request, session) else ""
    return jsonify({
        "token": shown,
        "has_token": bool(token),
        "monitor_id": reg.get("kuma_monitor_id", 0),
        "paused": bool(reg.get("kuma_paused")),
        "monitored": monitoring.is_monitored(reg),
        "follows": monitoring.kuma_follows(),
        "push_url": f"{base}/api/push/{shown}?status=up&msg=OK&ping=0" if shown else "",
        "health_url": f"{request.scheme}://{request.host}/api/devices/{key}/health",
    })


@app.route("/api/kuma/sync-tags", methods=["POST"])
@guard
def api_kuma_sync_tags():
    """Tag every existing Kuma monitor with its device category (one-shot)."""
    cfg = config.load()
    ki = cfg["integrations"]["kuma"]
    user = ki.get("username", "")
    pw = creds.get("@kuma").get("password", "")
    base = kuma.effective_base(ki)
    if not (base and user and pw):
        return jsonify({"ok": False, "error": "Set the Kuma URL, username and password first"})
    devs = {d["key"]: d for d in scanner.get_devices()}
    items = []
    for key, reg in scanner.registry.items():
        mid = reg.get("kuma_monitor_id")
        if not mid:
            continue
        cat = (devs.get(key) or {}).get("category") or reg.get("category") or "unknown"
        items.append((mid, cat))
    return jsonify(kuma.tag_monitors(base, user, pw, items))


@app.route("/api/kuma/repair", methods=["POST"])
@guard
def api_kuma_repair():
    """Convert every existing monitor to a 60s PING monitor pointed at the device's
    current IP (fixes the choppy 30-min push graphs)."""
    cfg = config.load()
    ki = cfg["integrations"]["kuma"]
    user = ki.get("username", "")
    pw = creds.get("@kuma").get("password", "")
    base = kuma.effective_base(ki)
    if not (base and user and pw):
        return jsonify({"ok": False, "error": "Set the Kuma URL, username and password first"})
    devs = {d["key"]: d for d in scanner.get_devices()}
    items = []
    for key, reg in scanner.registry.items():
        mid = reg.get("kuma_monitor_id")
        ip = (devs.get(key) or {}).get("ip")
        if mid and ip:
            items.append((mid, ip))
    res = kuma.ensure_ping(base, user, pw, items)
    if res.get("ok"):
        for key, reg in scanner.registry.items():
            if reg.get("kuma_monitor_id"):
                reg["kuma_ip"] = (devs.get(key) or {}).get("ip", reg.get("kuma_ip"))
        scanner.save_registry()
    return jsonify(res)


@app.route("/api/kuma/monitor-bulk", methods=["POST"])
@guard
def api_kuma_monitor_bulk():
    """Monitor many devices in one shot. body: {scope:"category", value:"camera"}
    or {scope:"identified"}. Kept for older pages: it is /api/monitoring for the
    matching devices, and Kuma follows (a 60 s ping monitor, tagged by category)."""
    if not monitoring.kuma_follows():
        return jsonify({"ok": False, "error": "Set the Kuma URL, username and password first"})
    body = request.get_json(force=True)
    scope = body.get("scope", "category")
    value = body.get("value")

    def wanted(d):
        if scope == "category":
            return d.get("category") == value
        if scope == "identified":
            return bool(d.get("name") or (d.get("category") and d.get("category") != "unknown"))
        return False

    keys = [d["key"] for d in scanner.get_devices() if wanted(d) and d.get("ip")]
    res = monitoring.set_many(scanner, on=keys, by=_who())
    n = len(res["changed"])
    return jsonify({"ok": True, "created": n, "total": len(keys),
                    **({} if n else {"message": "Nothing to add — matching devices are already monitored."})})


@app.route("/api/kuma/unowned", methods=["GET", "POST"])
@guard
def api_kuma_unowned():
    """Settings "Tidy Kuma". GET: Uptime Kuma monitors that no device and no
    internet check owns, with Netwatch's own spare copies flagged `leftover`.
    POST {ids: [monitor ids]}: delete those — each only if it still belongs to
    nothing when checked again."""
    if not monitoring.kuma_follows():
        return jsonify({"ok": False, "error": "Set the Kuma URL, username and password first"}), 400
    if request.method == "POST":
        ids = (request.get_json(silent=True) or {}).get("ids")
        if not (isinstance(ids, list) and ids and len(ids) <= 500
                and all(isinstance(i, int) and not isinstance(i, bool) and i > 0 for i in ids)):
            return jsonify({"ok": False, "error": "'ids' must be a list of monitor numbers"}), 400
        res = monitoring.remove_unowned(scanner, ids)
        return jsonify(res), (200 if res.get("ok") else 502)
    mons = monitoring.unowned(scanner)
    if mons is None:
        return jsonify({"ok": False, "error": "could not read Uptime Kuma"}), 502
    return jsonify({"ok": True, "monitors": mons})


@app.route("/api/kuma/test", methods=["POST"])
@guard
def api_kuma_test():
    cfg = config.load()
    ki = cfg["integrations"]["kuma"]
    body = request.get_json(silent=True) or {}
    user = body.get("username") or ki.get("username", "")
    pw = body.get("password") or creds.get("@kuma").get("password", "")
    base = kuma.effective_base(ki)
    if not (base and user and pw):
        return jsonify({"ok": False, "error": "Enter Kuma URL, username and password"})
    return jsonify(kuma.test_login(base, user, pw))


@app.route("/api/devices/<path:key>/photo", methods=["GET", "POST", "DELETE"])
def api_photo(key):
    path = _photo_path(key)
    if request.method == "GET":
        if not os.path.exists(path):
            return jsonify({"error": "no photo"}), 404
        with open(path, "rb") as f:
            mime = _img_mime(f.read(16)) or "application/octet-stream"
        return send_file(path, mimetype=mime)
    if not siteauth.allowed(request, session):
        return siteauth._denied(request, session)
    if request.method == "DELETE":
        try:
            os.remove(path)
        except OSError:
            pass
        return jsonify({"ok": True})
    # POST: multipart upload, field name "photo"
    if not _device_known(key):
        return jsonify({"ok": False, "error": "unknown device"}), 404
    f = request.files.get("photo")
    if not f:
        return jsonify({"ok": False, "error": "no file"}), 400
    data = f.read()
    if not _img_mime(data):
        return jsonify({"ok": False, "error": "not a supported image (jpg/png/gif/webp)"}), 400
    os.makedirs(PHOTO_DIR, exist_ok=True)
    with open(path, "wb") as out:
        out.write(data)
    return jsonify({"ok": True})


@app.route("/api/history/<path:key>")
def api_history(key):
    window = int(request.args.get("window", 86400))
    return jsonify({
        "summary": history.summary(key),
        "series": history.series(key, window_s=window),
    })


_BEAT_RANGES = {"30m": 1800, "1h": 3600, "12h": 43200, "24h": 86400}


@app.route("/api/history/<path:key>/beats")
def api_history_beats(key):
    """Fine-grained latency/up series for the Kuma-style chart (30m/1h/12h/24h)."""
    rng = request.args.get("range", "1h")
    window = _BEAT_RANGES.get(rng, 3600)
    return jsonify({"range": rng if rng in _BEAT_RANGES else "1h",
                    "points": history.beats(key, window)})


@app.route("/api/internet")
def api_internet():
    """Internet-uptime snapshot from the heartbeat sampler's synthetic probes:
    gateway reachable? external IPs reachable? DNS resolves?"""
    gw = history.latest_beat("__inet__gateway")
    ext = [history.latest_beat("__inet__8.8.8.8"), history.latest_beat("__inet__1.1.1.1")]
    dns = history.latest_beat("__inet__dns")

    def up(b):
        return bool(b and b["online"])
    checked = [b["ts"] for b in [gw, dns, *ext] if b]
    return jsonify({
        "has_gateway": gw is not None,
        "gateway": up(gw),
        "external": any(up(b) for b in ext),
        "dns": up(dns),
        "ok": any(up(b) for b in ext) and up(dns),
        "checked_ts": max(checked) if checked else None,
    })


@app.route("/api/events")
def api_events():
    """Device/IP event log (newest first). Filter with ?ip= / ?key= / ?type=."""
    try:
        limit = min(int(request.args.get("limit", 300)), 2000)
    except (TypeError, ValueError):
        limit = 300
    try:
        since = int(request.args.get("since")) if request.args.get("since") else None
    except (TypeError, ValueError):
        since = None
    return jsonify({"events": history.events(
        ip=request.args.get("ip") or None,
        key=request.args.get("key") or None,
        etype=request.args.get("type") or None,
        since=since,
        limit=limit,
    )})


@app.route("/api/events/summary")
def api_events_summary():
    """Counts per event type since ?since= (epoch; default all time) and the
    addresses with the most events — the History page's tiles and 'busiest'
    list, exact even when the timeline itself is capped."""
    try:
        since = int(request.args.get("since")) if request.args.get("since") else None
    except (TypeError, ValueError):
        since = None
    return jsonify(history.event_summary(since=since))


@app.route("/api/ip-history")
def api_ip_history():
    """One summary row per IP ever seen: device count, last device, last change."""
    return jsonify({"ips": history.ip_history()})


# ---- first-run wizard --------------------------------------------------
@app.route("/api/wizard", methods=["POST"])
@siteauth.required_if(lambda: config.load().get("configured"))
def api_wizard():
    body = request.get_json(force=True)
    # validate target CIDRs
    targets = []
    for t in body.get("targets", []):
        cidr = (t.get("cidr") or "").strip()
        if not cidr:
            continue
        try:
            ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            return jsonify({"ok": False, "error": f"Invalid network: {cidr}"}), 400
        targets.append({"cidr": cidr, "label": t.get("label", "Net"),
                        "local": t.get("local", "auto")})
    if not targets:
        return jsonify({"ok": False, "error": "At least one scan target is required"}), 400

    cfg = config.load()
    cfg.update({
        "configured": True,
        "site": body.get("site", cfg["site"]),
        "targets": targets,
    })
    cfg["scan"].update(body.get("scan", {}))
    cfg["alerts"].update(body.get("alerts", {}))
    cfg["vpn"].update(body.get("vpn", {}))
    config.save(cfg)
    scanner.wake()
    scanner.trigger("quick")
    return jsonify({"ok": True})


@app.route("/api/hub/status")
def api_hub_status():
    """Central-hub VPN link state for the dashboard card."""
    return jsonify(hubvpn.status())


@app.route("/api/hub/connect", methods=["POST"])
@guard
def api_hub_connect():
    """Save a pasted wg config and bring the tunnel up — no SSH, no open ports."""
    body = request.get_json(force=True, silent=True) or {}
    conf = (body.get("config") or "").strip()
    if not conf:
        return jsonify({"ok": False, "error": "No config provided."}), 400
    ok, msg = hubvpn.save_config(conf)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    ok, msg = hubvpn.up()
    code = 200 if ok else 500
    return jsonify({"ok": ok, "error": None if ok else msg,
                    "status": hubvpn.status()}), code


@app.route("/api/hub/disconnect", methods=["POST"])
@guard
def api_hub_disconnect():
    """Bring the tunnel down. Pass {"forget": true} to also delete the config."""
    body = request.get_json(force=True, silent=True) or {}
    if body.get("forget"):
        ok, msg = hubvpn.forget()
    else:
        ok, msg = hubvpn.down()
    code = 200 if ok else 500
    return jsonify({"ok": ok, "error": None if ok else msg,
                    "status": hubvpn.status()}), code


@app.route("/api/tunnel", methods=["GET", "POST"])
@guard
def api_tunnel():
    """On-demand TCP relay to a device on this site's LAN. POST {ip, port} opens a
    relay bound to wg0 (VPN-only) and returns {listen_port}; the hub re-exposes it.
    Only the hub may call this (hub key, or a logged-in person); relays bind to
    the wg0 address, not 0.0.0.0."""
    if request.method == "POST":
        body = request.get_json(force=True, silent=True) or {}
        try:
            res = tunnels.manager.open(body.get("ip", ""), body.get("port"))
        except tunnels.TunnelError as e:
            return jsonify({"ok": False, "error": str(e)}), e.status
        return jsonify({"ok": True, **res})
    return jsonify({"tunnels": tunnels.manager.list()})


@app.route("/api/tunnel/<tid>", methods=["DELETE"])
@guard
def api_tunnel_close(tid):
    return jsonify({"ok": tunnels.manager.close(tid)})


# ---- Pi self-health + config backup/restore ---------------------------------
@app.route("/api/sysinfo")
def api_sysinfo():
    """This Pi's own health: temp, CPU, RAM, disk, uptime, undervoltage."""
    snap = sysmon.monitor.snapshot()
    if not snap:
        snap = sysmon.monitor.sample()
    return jsonify(snap)


def _read_json_file(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


@app.route("/api/config/export")
@guard
def api_config_export():
    """Full settings bundle for disaster recovery / new-Pi deployment: config,
    device registry (names, categories, watch/kuma flags), obfuscated logins +
    their key, and the hub-VPN config. SENSITIVE — treat the file like a
    password. The hub pulls this daily for its per-site backup store."""
    key_b64 = None
    try:
        with open(creds.KEY_PATH, "rb") as f:
            key_b64 = base64.b64encode(f.read()).decode()
    except OSError:
        pass
    wg = None
    try:
        with open(hubvpn.WG_CONF) as f:
            wg = f.read()
    except OSError:
        pass
    cfg = config.load()
    return jsonify({
        "kind": "netwatch-backup", "version": 1, "created": int(time.time()),
        "site_name": (cfg.get("site") or {}).get("name") or "",
        "config": cfg,
        "devices": _read_json_file(REGISTRY_PATH) or {},
        "credentials": _read_json_file(creds.CRED_PATH),
        "secret_key": key_b64,
        "hubvpn_conf": wg,
        "topology": topology.export_bundle(topology.store),
    })


@app.route("/api/config/import", methods=["POST"])
@guard
def api_config_import():
    """Restore a backup bundle (from the hub or an uploaded file). Applies
    settings + device registry + logins; the hub-VPN config is only applied
    when this Pi has none (a fresh deployment), never over a working link."""
    b = request.get_json(force=True, silent=True) or {}
    if b.get("kind") != "netwatch-backup" or "config" not in b:
        return jsonify({"ok": False, "error": "not a Netwatch backup file"}), 400
    applied = []
    cfg = b.get("config") or {}
    cfg.pop("auth", None)          # never carry a foreign auth section (future-proof)
    # A bundle from before config_rev existed must run every migration on load;
    # save() would otherwise fill in the current rev and skip them all.
    cfg.setdefault("config_rev", 0)
    config.save(cfg)
    applied.append("settings")
    if isinstance(b.get("devices"), dict) and b["devices"]:
        scanner.registry = b["devices"]
        scanner.save_registry()
        applied.append(f"device registry ({len(b['devices'])})")
    if b.get("secret_key") and b.get("credentials") is not None:
        try:
            with open(creds.KEY_PATH, "wb") as f:
                f.write(base64.b64decode(b["secret_key"]))
            with open(creds.CRED_PATH, "w") as f:
                json.dump(b["credentials"], f)
            creds.reset_cache()
            applied.append("device logins")
        except (OSError, ValueError):
            pass
    if isinstance(b.get("topology"), dict):
        try:
            if topology.import_bundle(topology.store, b["topology"]):
                applied.append("network diagram")
        except (OSError, ValueError) as e:
            print("topology import:", e, flush=True)
    if b.get("hubvpn_conf") and not hubvpn.has_config():
        ok, msg = hubvpn.save_config(b["hubvpn_conf"])
        if ok:
            hubvpn.up()
            applied.append("hub VPN link")
    monitoring.after_restore(scanner)   # after the logins: seed an older registry, re-read Kuma
    scanner.trigger(mode="quick")
    return jsonify({"ok": True, "applied": applied})


@app.route("/api/network")
def api_network():
    """Interfaces, managed secondary IPs, NM connections + DHCP/static state."""
    cfg = config.load()
    avail = netcfg.nm_available()
    return jsonify({
        "available": avail,                       # nmcli/NM reachable (opt-in image)
        "interfaces": netcfg.list_interfaces(),
        "addresses": cfg.get("network", {}).get("addresses", []),
        "connections": netcfg.list_connections() if avail else [],
        "pending": netcfg.pending_state(),
        "gateway": default_gateway(),
    })


@app.route("/api/network/address", methods=["POST"])
@guard
def api_network_address():
    """Add/remove a managed secondary IP (and an auto scan target if requested)."""
    body = request.get_json(force=True, silent=True) or {}
    action = body.get("action")
    iface = (body.get("iface") or "").strip()
    cidr = (body.get("cidr") or "").strip()
    try:
        ipaddress.ip_interface(cidr)        # host address + prefix, e.g. 10.5.2.50/24
    except ValueError:
        return jsonify({"ok": False, "error": "Enter an IP with prefix, e.g. 10.5.2.50/24"}), 400
    if not iface:
        return jsonify({"ok": False, "error": "Pick an interface"}), 400
    cfg = config.load()
    addrs = cfg.setdefault("network", {}).setdefault("addresses", [])
    addrs = [a for a in addrs if not (a.get("iface") == iface and a.get("cidr") == cidr)]
    if action == "add":
        addrs.append({"iface": iface, "cidr": cidr, "target": bool(body.get("target")),
                      "label": (body.get("label") or "").strip() or "Extra LAN",
                      "managed": True})
    cfg["network"]["addresses"] = addrs
    netcfg.sync_targets(cfg)
    config.save(cfg)
    if action == "add":
        netcfg.apply_addresses(cfg)
    else:
        netcfg.remove_address(iface, cidr)
    scanner.wake()
    return jsonify({"ok": True})


@app.route("/api/network/static", methods=["POST"])
@guard
def api_network_static():
    """Apply DHCP->static on a connection, armed with the auto-revert watchdog."""
    if not netcfg.nm_available():
        return jsonify({"ok": False, "error": "NetworkManager not available on this image/host"}), 400
    body = request.get_json(force=True, silent=True) or {}
    con = (body.get("connection") or "").strip()
    ip_prefix = (body.get("ip_prefix") or "").strip()
    gateway = (body.get("gateway") or "").strip()
    dns = (body.get("dns") or "").strip()
    try:
        net = ipaddress.ip_interface(ip_prefix)
        if gateway:
            if ipaddress.ip_address(gateway) not in net.network:
                return jsonify({"ok": False, "error": "Gateway is not inside the IP's subnet"}), 400
        for d in dns.split(","):
            if d.strip():
                ipaddress.ip_address(d.strip())
    except ValueError:
        return jsonify({"ok": False, "error": "Bad IP/prefix, gateway or DNS"}), 400
    if not con:
        return jsonify({"ok": False, "error": "No connection"}), 400
    try:
        revert_s = max(30, min(600, int(body.get("revert_s") or netcfg.DEFAULT_REVERT_S)))
    except (TypeError, ValueError):
        revert_s = netcfg.DEFAULT_REVERT_S
    ok, msg, deadline = netcfg.apply_static_with_revert(con, ip_prefix, gateway, dns, revert_s)
    return (jsonify({"ok": True, "revert_deadline_ts": deadline})
            if ok else (jsonify({"ok": False, "error": msg}), 500))


@app.route("/api/network/static/confirm", methods=["POST"])
@guard
def api_network_confirm():
    netcfg.confirm_static()
    return jsonify({"ok": True})


@app.route("/api/network/dhcp", methods=["POST"])
@guard
def api_network_dhcp():
    if not netcfg.nm_available():
        return jsonify({"ok": False, "error": "NetworkManager not available"}), 400
    con = ((request.get_json(force=True, silent=True) or {}).get("connection") or "").strip()
    if not con:
        return jsonify({"ok": False, "error": "No connection"}), 400
    ok, msg = netcfg.switch_to_dhcp(con)
    return jsonify({"ok": ok, "error": None if ok else msg})


@app.route("/api/suggest-network")
def api_suggest():
    """Best-guess local /24(s) for the wizard's target step."""
    nets = []
    for n in scanner.local_networks():
        if not (n.is_loopback or str(n).startswith("172.")):
            nets.append(str(n))
    return jsonify({"networks": nets})


def main():
    hubvpn.boot()        # re-assert the hub tunnel if this site was joined
    netcfg.recover_pending()       # revert any unconfirmed static change from before a restart
    netcfg.apply_addresses()       # (re)add managed secondary IPs
    tunnels.manager.start()
    monitoring.seed(scanner)       # one-time: devices with a Kuma monitor become monitored
    scanner.start()
    listener.start()
    sysmon.monitor.start()
    switchmon.monitor.on_identity = scanner.set_device_name
    switchmon.monitor.on_meta = _switch_meta
    switchmon.monitor.start(scanner.get_devices, lambda: scanner.registry, config.load)
    port = int(os.environ.get("NETWATCH_PORT", "8090"))
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
