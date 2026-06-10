"""Site configuration: load/save config.json with defaults and migration.

Config lives in the /data volume so it survives image updates. On first run the
file does not exist -> defaults are written and `configured` stays False, which
makes the web layer redirect to the setup wizard.
"""
import json
import os
import threading

DATA_DIR = os.environ.get("NETWATCH_DATA", "/data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

_lock = threading.Lock()

DEFAULTS = {
    # Set True by the setup wizard once the operator has confirmed site details.
    "configured": False,
    "site": {
        "name": "",
        "location": "",
    },
    # Scan targets. Each: {cidr, label, local(bool|"auto")}. "auto" lets the
    # scanner decide local vs remote by checking the host's own interfaces.
    "targets": [
        {"cidr": "192.168.88.0/24", "label": "Main", "local": "auto"},
    ],
    "scan": {
        "interval_min": 15,        # quick scan cadence
        "online_lookup": True,     # allow api.macvendors.com + HTTP banner fetch
        "deep_on_new": False,      # auto deep-scan a host the first time it is seen
        "discovery": True,         # mDNS / SSDP / NetBIOS / ARP local enrichment
        "history_days": 90,        # uptime retention before pruning
    },
    "alerts": {
        "ntfy_server": "https://ntfy.sh",
        "ntfy_topic": "",          # blank disables push alerts
        "notify_new": True,
        "notify_offline": True,
        "notify_online": False,    # alert when ANY device returns (watched devices always do)
        # Per-category overrides: {"camera": {"offline": true, "online": true}, ...}
        # A category present here overrides the globals above for that category.
        "categories": {},
        "offline_after": 2,        # consecutive missed scans before "offline"
        "allow_commands": True,    # listen on the topic for ping/scan/etc commands
    },
    "vpn": {
        "mode": "none",            # none | tailscale | wireguard
    },
    "integrations": {
        # Uptime Kuma: Netwatch pushes per-device status to Kuma "Push" monitors.
        "kuma": {
            "base_url": "http://localhost:3001",   # where Netwatch reaches Kuma
            "username": "",                          # Kuma admin (for the create-monitor API)
            # admin password is stored obfuscated in the creds store under "@kuma".
            # Monitors are created ONLY when you tick "Monitor in Uptime Kuma" on a
            # device — never automatically during a scan.
        },
    },
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
