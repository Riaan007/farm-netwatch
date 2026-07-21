"""Hub configuration: load/save hub.json with defaults and migration.

Same pattern as the site app's config.py — the file lives in the /data volume
so it survives image updates. `sites` is the registry of Netwatch instances the
hub polls over the VPN (each addressed by its WireGuard IP).
"""
import json
import os
import threading

DATA_DIR = os.environ.get("HUB_DATA", "/data")
CONFIG_PATH = os.path.join(DATA_DIR, "hub.json")

_lock = threading.Lock()

DEFAULTS = {
    "hub": {
        "name": "Farm Network Asset Identifier",
    },
    "auth": {
        # werkzeug password hash; empty -> login page runs first-time setup
        # (or it is seeded from the HUB_PASSWORD env var at startup).
        "password_hash": "",
        # bcrypt hash of the SAME password, for the Caddy reverse-proxy's basic_auth
        # (Caddy only accepts bcrypt). Written by auth.set_password/ensure_proxy_hash.
        "proxy_basic_hash": "",
    },
    # Remote-access (road-warrior) VPN: laptop/phone clients of the office wg-easy.
    # office_lan is the office subnet routed to remote clients (blank = auto-derive
    # from the home site's vpn_ip). remote_clients is the registry of these devices
    # (kept separate from `sites` so the poller never polls them).
    "vpn": {
        "office_lan": "",
    },
    "remote_clients": [],
    "alerts": {
        # ntfy push alerts when a site drops off the hub. Blank topic = disabled.
        "ntfy_server": "https://ntfy.sh",
        "ntfy_topic": "",
        "notify_site_offline": True,
        "offline_after_polls": 2,   # consecutive failed status polls before alerting
        "notify_ip_conflict": True,  # alert when a site gains a new IP-address conflict
    },
    "ai": {
        # Gemini API for the per-site AI PDF reports (hub Alerts & AI settings).
        "gemini_api_key": "",
        "gemini_model": "gemini-2.5-flash",
    },
    "poll": {
        "status_interval_s": 60,    # site reachability heartbeat
        "devices_interval_s": 300,  # full device-list refresh
        "kuma_interval_s": 120,     # Kuma's status-page endpoints are server-cached 60s
        "timeout_connect_s": 3,
        "timeout_read_s": 6,        # MUST stay short: Kuma 1.x never answers a bad slug
        "history_days": 90,
    },
    # Each site: id (slug, fixed), name (fallback label — the live name comes from
    # the site's own /api/status), vpn_ip (WireGuard IP; the home site uses the
    # host LAN IP because the hub shares wg-easy's netns where localhost != host),
    # netwatch_port, kuma_url + kuma_status_slug ("" disables the Kuma panel).
    # Hub-managed extras (not user-editable): wg_client_id (wizard-created VPN
    # client) and proxy_netwatch_port / proxy_kuma_port (LAN ports the Caddy
    # reverse proxy assigns to VPN sites — see proxycfg.py). The deep-merge in
    # save()/update() preserves these keys across edits.
    "sites": [
        {
            "id": "home",
            "name": "Home",
            "vpn_ip": "192.168.88.250",
            "netwatch_port": 8090,
            "kuma_url": "http://192.168.88.250:3001",
            "kuma_status_slug": "farm",
            "enabled": True,
        },
    ],
}


def _deep_merge(base, override):
    """Recursively fill missing keys in `override` from `base` (config migration)."""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            out[k] = _deep_merge(base[k], v)
        else:
            out[k] = v
    return out


def load():
    with _lock:
        try:
            with open(CONFIG_PATH) as f:
                raw = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            raw = {}
        return _deep_merge(DEFAULTS, raw)


def save(cfg):
    with _lock:
        os.makedirs(DATA_DIR, exist_ok=True)
        merged = _deep_merge(DEFAULTS, cfg)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(merged, f, indent=2)
        os.replace(tmp, CONFIG_PATH)
        return merged


def update(patch):
    """Shallow-by-section update: merge `patch` into the current config and save."""
    return save(_deep_merge(load(), patch))


def get_site(site_id):
    return next((s for s in load()["sites"] if s.get("id") == site_id), None)
