"""Flask web layer for the Netwatch Hub: multi-site dashboard + JSON API.

Aggregates every farm site's Netwatch (and its Uptime Kuma) over the WireGuard
VPN. All data endpoints sit behind a session login; /api/health stays open as
a liveness probe.
"""
import base64
import ipaddress
import os
import re
import time

import requests
from flask import (Flask, jsonify, redirect, request, send_from_directory,
                   session)

import auth
import hubconfig
import notify
import proxycfg
import sitehistory
import tunnels
import wgeasy
from poller import poller


def _lan_host():
    """The host the browser used to reach the hub — for building site-dashboard
    links that point back through the hub's reverse proxy. Honour X-Forwarded-Host
    (future front proxy), else request.host; strip the port (the proxy port differs
    from the hub's 8091)."""
    h = (request.headers.get("X-Forwarded-Host", "").split(",")[0].strip()
         or request.host or "")
    return h.rsplit(":", 1)[0] if ":" in h else h

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
    else:
        # Backfill the Caddy basic-auth hash for installs predating the proxy.
        auth.ensure_proxy_hash(pw)
    proxycfg.sync()                        # (re)enable the site proxy now we have a hash
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
        "links": dict(zip(("netwatch", "kuma"), proxycfg.links(site, _lan_host()))),
        "vpn_ip": site["vpn_ip"],
        "spark": sitehistory.series(site["id"], 86400, 48),
        "reach_24h": sitehistory.reachability_pct(site["id"], 86400),
    }


@app.route("/api/hub/alerts", methods=["GET", "POST"])
def api_alerts():
    """Hub ntfy alert settings (topic/server + site-offline toggle)."""
    if request.method == "POST":
        body = request.get_json(force=True, silent=True) or {}
        patch = {}
        for k in ("ntfy_server", "ntfy_topic"):
            if k in body:
                patch[k] = (body[k] or "").strip()
        if "notify_site_offline" in body:
            patch["notify_site_offline"] = bool(body["notify_site_offline"])
        hubconfig.update({"alerts": patch})
        return jsonify({"ok": True})
    a = hubconfig.load()["alerts"]
    return jsonify({"ntfy_server": a.get("ntfy_server", "https://ntfy.sh"),
                    "ntfy_topic": a.get("ntfy_topic", ""),
                    "notify_site_offline": a.get("notify_site_offline", True)})


@app.route("/api/hub/alerts/test", methods=["POST"])
def api_alerts_test():
    ok = notify.push(hubconfig.load()["alerts"], "Hub test alert",
                     "ntfy is wired up — you'll get site-offline alerts here.",
                     tags=["bell"])
    return jsonify({"ok": bool(ok),
                    "error": None if ok else "No topic set, or ntfy unreachable."})


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


_VPN_NET = ipaddress.ip_network("10.8.0.0/24")


def _office_lan():
    """The office subnet to route to remote-access clients: the configured value, or
    auto-derived from the home site's LAN IP (the non-10.8.0.x private site)."""
    cfg = hubconfig.load()
    ol = (cfg.get("vpn") or {}).get("office_lan") or ""
    if ol:
        return ol
    for s in cfg["sites"]:
        try:
            ip = ipaddress.ip_address(s.get("vpn_ip", ""))
        except ValueError:
            continue
        if ip not in _VPN_NET and ip.is_private:
            return str(ipaddress.ip_network(f"{ip}/24", strict=False))
    return ""


def _qr_svg(text):
    import io
    import segno
    qr = segno.make(text, error="m")
    try:
        return qr.svg_inline(scale=4, border=2)
    except AttributeError:
        b = io.BytesIO()
        qr.save(b, kind="svg", scale=4, border=2)
        return re.sub(r"<\?xml.*?\?>", "", b.getvalue().decode(), flags=re.S).strip()


@app.route("/api/hub/remote", methods=["GET", "POST"])
def api_remote():
    """Road-warrior VPN clients: a laptop/phone that connects to the office wg-easy
    and routes the office LAN + VPN subnet (split tunnel) — 'as if at the office'."""
    cfg = hubconfig.load()
    if request.method == "POST":
        body = request.get_json(force=True, silent=True) or {}
        name = (body.get("name") or "").strip()[:48] or "device"
        try:
            client = wgeasy.create_client(f"remote-{name}")
        except wgeasy.WgEasyError as e:
            return jsonify({"ok": False, "error": str(e)}), 502
        office = _office_lan()
        allowed = "10.8.0.0/24" + (f", {office}" if office else "")
        try:
            conf = wgeasy.remote_config(client["id"], allowed)
        except wgeasy.WgEasyError as e:
            return jsonify({"ok": False, "error": str(e)}), 502
        rec = {"id": client["id"], "name": name,
               "address": client.get("address", ""), "created": int(time.time())}
        cfg.setdefault("remote_clients", []).append(rec)
        hubconfig.save(cfg)
        return jsonify({"ok": True, **rec, "allowed_ips": allowed,
                        "config": conf, "qr_svg": _qr_svg(conf)})
    return jsonify({"clients": cfg.get("remote_clients", []), "office_lan": _office_lan()})


@app.route("/api/hub/remote/<cid>", methods=["DELETE"])
def api_remote_delete(cid):
    cfg = hubconfig.load()
    cfg["remote_clients"] = [c for c in cfg.get("remote_clients", []) if c.get("id") != cid]
    hubconfig.save(cfg)
    removed = False
    try:
        wgeasy.delete_client(cid)
        removed = True
    except wgeasy.WgEasyError:
        pass
    return jsonify({"ok": True, "removed_vpn_client": removed})


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
        proxycfg.sync()
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
        gone = cfg["sites"].pop(idx)
        hubconfig.save(cfg)
        proxycfg.sync()                     # free its proxy ports + drop its Caddy block
        # Best-effort: a wizard-created site also owns a wg-easy client.
        removed_vpn = False
        if gone.get("wg_client_id"):
            try:
                wgeasy.delete_client(gone["wg_client_id"])
                removed_vpn = True
            except wgeasy.WgEasyError:
                pass
        return jsonify({"ok": True, "removed_vpn_client": removed_vpn})
    body = request.get_json(force=True)
    site, err = _validate_site(body, {s["id"] for s in cfg["sites"]}, keep_id=site_id)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    # merge over the old entry so extra keys (wg_client_id, proxy_*) survive an edit
    cfg["sites"][idx] = {**cfg["sites"][idx], **site}
    hubconfig.save(cfg)
    proxycfg.sync()                         # vpn_ip/kuma_url change may re-target the proxy
    poller.poll_now(site_id)
    return jsonify({"ok": True, "site": cfg["sites"][idx]})


# ---- add-site wizard --------------------------------------------------------
# Where the bare-Pi installer lives (override per deployment / fork).
NETWATCH_INSTALL_URL = os.environ.get(
    "NETWATCH_INSTALL_URL",
    "https://raw.githubusercontent.com/Riaan007/farm-netwatch/main/install.sh")

# One paste on the site's Pi (FRESH or already running Netwatch): the installer
# preflights, installs Docker/Compose/WireGuard + Netwatch + Kuma if missing,
# drops this site's VPN config in, and links it to the hub. Running everything
# inside `sudo bash -c` lets us set the env for the piped installer directly,
# avoiding sudo's env-var stripping.
_ENROLL_TEMPLATE = (
    "# Paste on the farm site's Pi (a fresh Pi or one already running Netwatch).\n"
    "# Installs everything needed and links this site to the hub in one command.\n"
    "sudo bash -c 'curl -fsSL \"{install_url}\" | "
    "WG_CONF_B64=\"{conf_b64}\" ASSUME_YES=1 bash'\n")


def _enroll_payload(site):
    """conf + paste-ready one-command bootstrap for a wizard-created site."""
    conf = wgeasy.get_configuration(site["wg_client_id"]).strip()
    conf_b64 = base64.b64encode(conf.encode()).decode()
    return {
        "vpn_ip": site["vpn_ip"],
        "conf": conf,
        "script": _ENROLL_TEMPLATE.format(conf_b64=conf_b64,
                                          install_url=NETWATCH_INSTALL_URL),
    }


@app.route("/api/hub/wizard", methods=["POST"])
def api_hub_wizard():
    """One-shot site onboarding: create the wg-easy client, register the site,
    and return the enrollment script to paste on the farm Pi."""
    cfg = hubconfig.load()
    body = request.get_json(force=True)
    sid = (body.get("id") or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", sid):
        return jsonify({"ok": False,
                        "error": "Site id: lowercase letters, digits and dashes only"}), 400
    if sid in {s["id"] for s in cfg["sites"]}:
        return jsonify({"ok": False, "error": f"Site id '{sid}' already exists"}), 400

    try:
        client = wgeasy.create_client(sid)
        vpn_ip = client["address"]
    except wgeasy.WgEasyError as e:
        return jsonify({"ok": False, "error": str(e)}), 502

    with_kuma = bool(body.get("kuma", False))
    site = {
        "id": sid,
        "name": (body.get("name") or "").strip(),
        "vpn_ip": vpn_ip,
        "netwatch_port": 8090,
        "kuma_url": f"http://{vpn_ip}:3001" if with_kuma else "",
        "kuma_status_slug": "farm" if with_kuma else "",
        "enabled": True,
        "wg_client_id": client["id"],
    }
    cfg["sites"].append(site)
    hubconfig.save(cfg)
    proxycfg.sync()
    poller.poll_now(sid)

    try:
        payload = _enroll_payload(site)
    except wgeasy.WgEasyError as e:
        return jsonify({"ok": False, "error": f"site created, but: {e}"}), 502
    return jsonify({"ok": True, "site": site, **payload})


@app.route("/api/hub/sites/<site_id>/enroll")
def api_site_enroll(site_id):
    """Re-issue the enrollment script (e.g. the copy was lost, or re-flashing a Pi)."""
    site, err = _site_or_404(site_id)
    if err:
        return err
    if not site.get("wg_client_id"):
        return jsonify({"ok": False,
                        "error": "this site was added manually — no wizard VPN client"}), 400
    try:
        return jsonify({"ok": True, **_enroll_payload(site)})
    except wgeasy.WgEasyError as e:
        return jsonify({"ok": False, "error": str(e)}), 502


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


@app.route("/api/hub/sites/<site_id>/command", methods=["POST"])
def api_site_command(site_id):
    """Run a diagnostic on the site (ping / port / connection-quality test) for a
    device and return its result. Generous read timeout — the quality test fires
    ~20 pings (~20s)."""
    site, err = _site_or_404(site_id)
    if err:
        return err
    base = f"http://{site['vpn_ip']}:{site.get('netwatch_port', 8090)}"
    try:
        r = requests.post(f"{base}/api/command",
                          json=request.get_json(force=True, silent=True) or {},
                          timeout=(5, 60))
        return jsonify(r.json()), r.status_code
    except (requests.exceptions.RequestException, ValueError):
        return jsonify({"ok": False, "error": "site unreachable"}), 502


@app.route("/api/hub/sites/<site_id>/trigger", methods=["POST"])
def api_site_trigger(site_id):
    """Trigger a scan on the site (e.g. a deep scan of one device). Returns
    immediately; results land in the device list on the next poll."""
    site, err = _site_or_404(site_id)
    if err:
        return err
    base = f"http://{site['vpn_ip']}:{site.get('netwatch_port', 8090)}"
    try:
        r = requests.post(f"{base}/api/trigger",
                          json=request.get_json(force=True, silent=True) or {},
                          timeout=(5, 10))
        return jsonify(r.json()), r.status_code
    except (requests.exceptions.RequestException, ValueError):
        return jsonify({"ok": False, "error": "site unreachable"}), 502


@app.route("/api/hub/sites/<site_id>/history/<path:key>/beats")
def api_site_beats(site_id, key):
    """Proxy the site's fine-grained latency series for the device chart."""
    site, err = _site_or_404(site_id)
    if err:
        return err
    poll = hubconfig.load()["poll"]
    base = f"http://{site['vpn_ip']}:{site.get('netwatch_port', 8090)}"
    try:
        r = requests.get(f"{base}/api/history/{key}/beats",
                         params={"range": request.args.get("range", "1h")},
                         timeout=(poll["timeout_connect_s"], poll["timeout_read_s"]))
        return jsonify(r.json()), r.status_code
    except (requests.exceptions.RequestException, ValueError):
        return jsonify({"ok": False, "error": "site unreachable", "points": []}), 502


@app.route("/api/hub/sites/<site_id>/internet")
def api_site_internet(site_id):
    """Proxy the site's internet-uptime snapshot (gateway / external / DNS)."""
    site, err = _site_or_404(site_id)
    if err:
        return err
    poll = hubconfig.load()["poll"]
    base = f"http://{site['vpn_ip']}:{site.get('netwatch_port', 8090)}"
    try:
        r = requests.get(f"{base}/api/internet",
                         timeout=(poll["timeout_connect_s"], poll["timeout_read_s"]))
        return jsonify(r.json()), r.status_code
    except (requests.exceptions.RequestException, ValueError):
        return jsonify({"ok": False, "error": "site unreachable"}), 502


@app.route("/api/hub/sites/<site_id>/events")
def api_site_events(site_id):
    """Proxy the site's device/IP event log (offline/online/new/ip_change)."""
    site, err = _site_or_404(site_id)
    if err:
        return err
    poll = hubconfig.load()["poll"]
    base = f"http://{site['vpn_ip']}:{site.get('netwatch_port', 8090)}"
    try:
        r = requests.get(f"{base}/api/events", params=request.args.to_dict(),
                         timeout=(poll["timeout_connect_s"], poll["timeout_read_s"]))
        return jsonify(r.json()), r.status_code
    except (requests.exceptions.RequestException, ValueError):
        return jsonify({"ok": False, "error": "site unreachable", "events": []}), 502


@app.route("/api/hub/sites/<site_id>/ip-history")
def api_site_ip_history(site_id):
    """Proxy the site's per-IP history summary (which devices used each IP)."""
    site, err = _site_or_404(site_id)
    if err:
        return err
    poll = hubconfig.load()["poll"]
    base = f"http://{site['vpn_ip']}:{site.get('netwatch_port', 8090)}"
    try:
        r = requests.get(f"{base}/api/ip-history",
                         timeout=(poll["timeout_connect_s"], poll["timeout_read_s"]))
        return jsonify(r.json()), r.status_code
    except (requests.exceptions.RequestException, ValueError):
        return jsonify({"ok": False, "error": "site unreachable", "ips": []}), 502


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


# ---- device tunnels (reach a device on the site's LAN via the hub) ----------
@app.route("/api/hub/sites/<site_id>/tunnel", methods=["POST"])
def api_site_tunnel(site_id):
    """Open an on-demand TCP tunnel to <ip>:<port> on the site's LAN and return
    {host, port} to connect to (PuTTY/SSH or browser). Behind the hub login."""
    site, err = _site_or_404(site_id)
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    try:
        res = tunnels.manager.open(site, body.get("ip", ""), body.get("port"), _lan_host())
    except tunnels.TunnelError as e:
        return jsonify({"ok": False, "error": str(e)}), e.status
    return jsonify({"ok": True, **res})


@app.route("/api/hub/sites/<site_id>/tunnels")
def api_site_tunnels(site_id):
    site, err = _site_or_404(site_id)
    if err:
        return err
    return jsonify({"tunnels": tunnels.manager.list(site_id)})


@app.route("/api/hub/sites/<site_id>/tunnel/<tid>", methods=["DELETE"])
def api_site_tunnel_close(site_id, tid):
    return jsonify({"ok": tunnels.manager.close(tid)})


def main():
    auth.seed_from_env()
    lo, hi = proxycfg.port_range()
    print(f"[hub] site reverse-proxy port range {lo}-{hi} "
          "(must match the published range in docker-compose.yml)", flush=True)
    proxycfg.sync()                        # assign ports + write/reload the Caddyfile
    tunnels.manager.start()                # device-tunnel relay manager
    poller.start()
    port = int(os.environ.get("HUB_PORT", "8091"))
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
