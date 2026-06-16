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
        # "Online" requires real reachability — an open port OR an ICMP reply.
        # A host that only answers ARP (e.g. a Wi-Fi NIC answering while the host
        # sleeps) is treated as OFFLINE. Turn off to count ARP presence as online.
        "require_reachable": True,
        # Fine-grained latency sampling for the Uptime-Kuma-style per-device chart.
        # Pings watched/named online devices every interval into a short-retention
        # table (heartbeats). Scope: "watched_named" (default) or "online" (all).
        "heartbeat_enabled": True,
        "heartbeat_interval_s": 60,
        "heartbeat_retention_days": 3,
        "heartbeat_scope": "watched_named",
    },
    "alerts": {
        "ntfy_server": "https://ntfy.sh",
        "ntfy_topic": "",          # blank disables push alerts
        "notify_new": True,
        "notify_offline": True,
        "notify_online": False,    # alert when ANY device returns (watched devices always do)
        "notify_hub_offline": True,  # alert when the VPN link to the Central Hub drops
        # Per-category overrides: {"camera": {"offline": true, "online": true}, ...}
        # A category present here overrides the globals above for that category.
        "categories": {},
        "offline_after": 2,        # consecutive missed scans before "offline"
        "allow_commands": True,    # listen on the topic for ping/scan/etc commands
    },
    "vpn": {
        "mode": "none",            # none | tailscale | wireguard
    },
    # Extra IP addresses Netwatch puts on the Pi at boot so it can sit on several
    # subnets at once (each {iface, cidr, target, label, managed}). cidr is the Pi's
    # host address+prefix on that LAN, e.g. "10.5.2.50/24". Additive only — never
    # touches the primary/DHCP link. See netcfg.py.
    "network": {
        "addresses": [],
    },
    "integrations": {
        # Uptime Kuma: Netwatch pushes per-device status to Kuma "Push" monitors.
        "kuma": {
            "enabled": False,                        # set true once Kuma is wired up
            "internet_monitors": True,               # auto-create gateway+DNS monitors on enable
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
