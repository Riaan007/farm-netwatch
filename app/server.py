"""Flask web layer: serves the dashboard + setup wizard and the JSON API.

Keeps the three endpoints the original HTML already called (/api/status,
/api/trigger, /api/setup) working, and adds the richer endpoints the new UI uses.
"""
import base64
import ipaddress
import json
import os
import re
import time

import urllib3
from flask import Flask, jsonify, redirect, request, send_file, send_from_directory

import airos
import commands
import config
import creds
import hikvision
import history
import hubvpn
import identify
import netcfg
import sysmon
import tunnels
import kuma
import notify
from listener import listener
from scanner import REGISTRY_PATH, scanner, default_gateway

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
PHOTO_DIR = os.path.join(os.environ.get("NETWATCH_DATA", "/data"), "photos")
app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024   # 8 MB photo cap


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
    })


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "POST":
        # Merge over the CURRENT config (not defaults) so a partial update never
        # silently resets unrelated fields such as `configured`.
        return jsonify(config.update(request.get_json(force=True)))
    cfg = config.load()
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
              "allow_commands": cfg["alerts"].get("allow_commands", True)}
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
    if "airos_change_ip" in body:
        patch["features"] = {"airos_change_ip": bool(body["airos_change_ip"])}
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
    for d in devices:                      # flag only; never expose the secret here
        d["has_credentials"] = d.get("key") in have
        d["has_photo"] = os.path.exists(_photo_path(d.get("key", "")))
        _kreg = scanner.registry.get(d.get("key", ""), {})
        d["has_kuma"] = bool(_kreg.get("kuma_monitor_id") or _kreg.get("kuma_token"))
    return jsonify({
        "targets": cfg["targets"],
        "devices": devices,
        "last_scan": scanner.get_status()["last_scan"],
    })


@app.route("/api/devices/<path:key>/credentials", methods=["GET", "POST"])
def api_credentials(key):
    if request.method == "POST":
        body = request.get_json(force=True)
        saved = creds.set_(key, body.get("username", "").strip(),
                           body.get("password", ""), body.get("notes", "").strip())
        return jsonify({"ok": True, "has_credentials": saved})
    # GET returns the decrypted secret on demand (not part of the polled feed)
    return jsonify(creds.get(key))


@app.route("/api/devices/prune", methods=["POST"])
def api_devices_prune():
    """Forget stale devices. Body: {"days": N} removes offline devices not seen
    in N days; {"days": null} (or omitted) removes ALL currently-offline ones."""
    body = request.get_json(force=True, silent=True) or {}
    days = body.get("days")
    try:
        days = int(days) if days not in (None, "") else None
    except (TypeError, ValueError):
        days = None
    removed = scanner.prune_devices(days=days, only_offline=True)
    return jsonify({"ok": True, "removed": len(removed), "keys": removed})


def _sync_watch_kuma(key, watch_on):
    """Keep Kuma in step with the 🔔 watch flag: watching a device auto-creates
    its ping monitor (when Kuma admin creds are configured); un-watching removes
    the monitor again ONLY if the watch created it — a monitor ticked by hand
    stays. Best-effort: the alert toggle must still work when Kuma is down."""
    try:
        ki = config.load()["integrations"]["kuma"]
        base = kuma.effective_base(ki)
        user = ki.get("username", "")
        pw = creds.get("@kuma").get("password", "")
        if not (base and user and pw):
            return
        reg = scanner.registry.get(key, {})
        mid = reg.get("kuma_monitor_id")
        if watch_on and not mid:
            dev = next((d for d in scanner.get_devices() if d.get("key") == key), None)
            if not (dev and dev.get("ip")):
                return
            res = kuma.provision(base, user, pw, scanner._kuma_name(dev), dev["ip"], 60,
                                 dev.get("category"))
            if res.get("ok"):
                scanner.set_device_meta(key, kuma_monitor_id=res["monitor_id"],
                                        kuma_ip=dev["ip"], kuma_by_watch=True)
        elif not watch_on and mid and reg.get("kuma_by_watch"):
            kuma.deprovision(base, user, pw, mid)
            scanner.set_device_meta(key, kuma_token="", kuma_monitor_id=0,
                                    kuma_by_watch=False)
    except Exception as e:  # noqa: BLE001
        print("watch-kuma sync error:", e, flush=True)


@app.route("/api/devices/<path:key>", methods=["POST", "DELETE"])
def api_device_meta(key):
    if request.method == "DELETE":
        removed = scanner.delete_device(key)
        return jsonify({"ok": removed}), (200 if removed else 404)
    body = request.get_json(force=True)
    prev_watch = bool(scanner.registry.get(key, {}).get("watch"))
    reg = scanner.set_device_meta(
        key,
        name=body.get("name"),
        category=body.get("category"),
        type_label=body.get("type"),
        watch=body.get("watch"),
        serial=body.get("serial"),
        model=body.get("model"),
        link=body.get("link"),
    )
    if body.get("watch") is not None and bool(body["watch"]) != prev_watch:
        _sync_watch_kuma(key, bool(body["watch"]))
        reg = scanner.registry.get(key, reg)
    return jsonify({"ok": True, "registry": reg})


@app.route("/api/devices/<path:key>/hikvision", methods=["POST"])
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
                                model=info.get("model") or None)
        if info.get("firmwareVersion"):
            scanner.registry.setdefault(key, {})["firmware"] = info["firmwareVersion"]
            scanner.save_registry()
        return jsonify({"ok": True, "info": info})
    return jsonify({"ok": False, "error": res.get("error", "failed")})


def _hik_target(key):
    """(ip, user, pw) for a Hikvision device by key, using its saved credentials."""
    dev = next((d for d in scanner.get_devices() if d.get("key") == key), None)
    c = creds.get(key)
    return (dev["ip"] if dev else None), c["username"], c["password"]


@app.route("/api/devices/<path:key>/network", methods=["GET"])
def api_device_network(key):
    """Read a Hikvision camera's current IPv4 settings (to pre-fill the change form)."""
    ip, user, pw = _hik_target(key)
    if not ip:
        return jsonify({"ok": False, "error": "unknown device IP"}), 400
    if not (user or pw):
        return jsonify({"ok": False, "error": "Save the camera's username/password first"})
    return jsonify(hikvision.get_network(ip, user, pw))


@app.route("/api/devices/<path:key>/set-ip", methods=["POST"])
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
def api_airos_network(key):
    """Read a Ubiquiti airOS radio's current management IP over SSH (gated)."""
    if not _airos_enabled():
        return jsonify({"ok": False, "error": "airOS Change IP is turned off in Settings"})
    ip, user, pw = _hik_target(key)        # same saved-credential lookup
    if not ip:
        return jsonify({"ok": False, "error": "unknown device IP"}), 400
    return jsonify(airos.get_network(ip, user, pw))


@app.route("/api/devices/<path:key>/airos-set-ip", methods=["POST"])
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


@app.route("/api/problems")
def api_problems():
    """All detected problems (IP conflict, risky ports, duplicate MAC, IP drift)
    for the dashboard's Problems panel."""
    return jsonify({"problems": scanner.problems()})


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

    POST {action:"create"} auto-creates the monitor (stores id + token),
    {action:"remove"} deletes it, or {token:"..."} sets a token manually.
    """
    cfg = config.load()
    ki = cfg["integrations"]["kuma"]
    base = kuma.effective_base(ki)

    if request.method == "POST":
        body = request.get_json(force=True)
        action = body.get("action")
        if action in ("create", "remove"):
            user = ki.get("username", "")
            pw = creds.get("@kuma").get("password", "")
            if not (base and user and pw):
                return jsonify({"ok": False, "error": "Set the Kuma URL, username and password in Settings first"})
            if action == "create":
                dev = next((d for d in scanner.get_devices() if d.get("key") == key), None)
                if not dev:
                    return jsonify({"ok": False, "error": "device not currently visible"}), 404
                name = scanner._kuma_name(dev)
                # Kuma pings the device directly every 60s -> smooth graph + true uptime
                res = kuma.provision(base, user, pw, name, dev["ip"], 60, dev.get("category"))
                if res.get("ok"):
                    scanner.set_device_meta(key, kuma_monitor_id=res["monitor_id"],
                                            kuma_ip=dev["ip"])
                    return jsonify({"ok": True, "monitor_id": res["monitor_id"]})
                return jsonify({"ok": False, "error": res.get("error", "create failed")})
            # remove
            mid = scanner.registry.get(key, {}).get("kuma_monitor_id")
            if mid:
                kuma.deprovision(base, user, pw, mid)
            scanner.set_device_meta(key, kuma_token="", kuma_monitor_id=0,
                                    kuma_by_watch=False)
            return jsonify({"ok": True})
        # manual token set
        scanner.set_device_meta(key, kuma_token=body.get("token", ""))

    reg = scanner.registry.get(key, {})
    token = reg.get("kuma_token", "")
    return jsonify({
        "token": token,
        "monitor_id": reg.get("kuma_monitor_id", 0),
        "push_url": f"{base}/api/push/{token}?status=up&msg=OK&ping=0" if token else "",
        "health_url": f"{request.scheme}://{request.host}/api/devices/{key}/health",
    })


@app.route("/api/kuma/sync-tags", methods=["POST"])
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
def api_kuma_monitor_bulk():
    """Flag many devices for Kuma monitoring in one shot. body:
    {scope:"category", value:"camera"} or {scope:"identified"}. Creates a 60s
    ping monitor (tagged by category) for each matching device that isn't already
    monitored; the monitor then follows the device's IP via _kuma_sync."""
    cfg = config.load()
    ki = cfg["integrations"]["kuma"]
    base = kuma.effective_base(ki)
    user = ki.get("username", "")
    pw = creds.get("@kuma").get("password", "")
    if not (base and user and pw):
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

    devs = scanner.get_devices()
    items, ip_by_key = [], {}
    for d in devs:
        if not (wanted(d) and d.get("ip")):
            continue
        if scanner.registry.get(d["key"], {}).get("kuma_monitor_id"):
            continue                       # already monitored — skip
        items.append((d["key"], scanner._kuma_name(d), d["ip"], d.get("category")))
        ip_by_key[d["key"]] = d["ip"]
    if not items:
        return jsonify({"ok": True, "created": 0, "total": 0,
                        "message": "Nothing to add — matching devices are already monitored."})
    res = kuma.provision_many(base, user, pw, items, 60)
    created = 0
    for key, r in res.items():
        if r.get("ok"):
            scanner.set_device_meta(key, kuma_monitor_id=r["monitor_id"],
                                    kuma_ip=ip_by_key.get(key))
            created += 1
    return jsonify({"ok": True, "created": created, "total": len(items)})


@app.route("/api/kuma/test", methods=["POST"])
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
    if request.method == "DELETE":
        try:
            os.remove(path)
        except OSError:
            pass
        return jsonify({"ok": True})
    # POST: multipart upload, field name "photo"
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
    return jsonify({"events": history.events(
        ip=request.args.get("ip") or None,
        key=request.args.get("key") or None,
        etype=request.args.get("type") or None,
        limit=limit,
    )})


@app.route("/api/ip-history")
def api_ip_history():
    """One summary row per IP ever seen: device count, last device, last change."""
    return jsonify({"ips": history.ip_history()})


# ---- first-run wizard --------------------------------------------------
@app.route("/api/wizard", methods=["POST"])
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
def api_tunnel():
    """On-demand TCP relay to a device on this site's LAN. POST {ip, port} opens a
    relay bound to wg0 (VPN-only) and returns {listen_port}; the hub re-exposes it.
    Only the hub (over the VPN) should reach this — the site has no auth of its own,
    so relays bind to the wg0 address, not 0.0.0.0."""
    if request.method == "POST":
        body = request.get_json(force=True, silent=True) or {}
        try:
            res = tunnels.manager.open(body.get("ip", ""), body.get("port"))
        except tunnels.TunnelError as e:
            return jsonify({"ok": False, "error": str(e)}), e.status
        return jsonify({"ok": True, **res})
    return jsonify({"tunnels": tunnels.manager.list()})


@app.route("/api/tunnel/<tid>", methods=["DELETE"])
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
    })


@app.route("/api/config/import", methods=["POST"])
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
    if b.get("hubvpn_conf") and not hubvpn.has_config():
        ok, msg = hubvpn.save_config(b["hubvpn_conf"])
        if ok:
            hubvpn.up()
            applied.append("hub VPN link")
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
def api_network_confirm():
    netcfg.confirm_static()
    return jsonify({"ok": True})


@app.route("/api/network/dhcp", methods=["POST"])
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
    scanner.start()
    listener.start()
    sysmon.monitor.start()
    port = int(os.environ.get("NETWATCH_PORT", "8090"))
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
