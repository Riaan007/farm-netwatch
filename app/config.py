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
        # After the nmap pass, ICMP-sweep the local subnet and add any pingable
        # host nmap's ARP discovery missed (catches high-latency wireless radios).
        "icmp_sweep": True,
        # MACs (bare-hex lowercase) of proxy-ARP bridges — e.g. a Ubiquiti
        # station fronting a camera pole answers ARP for every client behind
        # the wireless link. Devices seen with one of these MACs are tracked
        # per-IP instead of being merged into one MAC-keyed record.
        "bridge_macs": [],
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
        # Up/down alerts are Uptime Kuma's job now (KISS split), so Netwatch's own
        # offline/online ntfy default OFF. The online/offline state is still tracked
        # for the dashboard and problem detection.
        "notify_offline": False,
        "notify_online": False,
        "notify_hub_offline": True,  # alert when the VPN link to the Central Hub drops
        # Per-category overrides: {"camera": {"offline": true, "online": true}, ...}
        # A category present here overrides the globals above for that category.
        "categories": {},
        "offline_after": 2,        # consecutive missed scans before "offline"
        # Listen on the topic for ping/scan/etc commands. OFF by default: anyone who
        # knows the topic name (ntfy.sh topics are public) could run them.
        "allow_commands": False,
    },
    "vpn": {
        "mode": "none",            # none | tailscale | wireguard
    },
    # Feature switches. airos_change_ip exposes the SSH "Change IP" action on
    # Ubiquiti airOS radios. It shipped off while the SSH path was unproven;
    # verified against live LiteAP/PowerBeam gear at Tankwa (2026-07-29) it is
    # now ON for every airOS radio, and stays a switch so a site that doesn't
    # want a reboot-capable button on its backhaul can turn it back off.
    "features": {
        "airos_change_ip": True,
    },
    # Bumped when a default changes in a way an EXISTING config must adopt —
    # _deep_merge only fills MISSING keys, so a stored False would otherwise
    # pin the old default forever. See _migrate().
    "config_rev": 2,
    # Wireless telemetry from Ubiquiti radios (radiomon.py). Read-only SSH, only
    # ever touches radios that have a saved login. It rides along with the scan
    # but keeps its own cadence — polling a radio every scan would be pointless
    # traffic on a link that is already the bottleneck.
    "radio": {
        "enabled": True,
        "poll_min": 15,            # minutes between polls of the SAME radio
        "history_days": 30,        # telemetry retention
        "alerts": True,            # ntfy on degradation (uses the alerts section)
    },
    # Pi self-health monitor. watchdog arms /dev/watchdog (auto-reboot on a hard
    # hang) — needs the health add-on (docker-compose.health.yml) for /dev access
    # and is deliberately opt-in: an armed watchdog reboots the Pi if Netwatch
    # is killed without a clean stop.
    "sysmon": {
        "watchdog": False,
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
            "auto_url": False,                       # True = host part follows this server's LAN IP
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


def _migrate(cfg, stored_rev):
    """Adopt changed defaults on an existing install, once.

    `stored_rev` MUST come from the config file as read, never from the merged
    result.

    A stored value always wins over DEFAULTS (that is the point of the merge),
    so flipping a default is invisible to sites that already have the old one
    written out. Each revision below is applied exactly once, then recorded.
    """
    if stored_rev >= DEFAULTS["config_rev"]:
        return cfg, False
    rev = stored_rev
    if rev < 1:
        # airOS Change IP: proven on real radios, so it is no longer opt-in.
        # An operator who deliberately turned it off keeps that choice only if
        # they turn it off again — there is no way to tell "never touched" from
        # "explicitly off" in the stored config, and defaulting it on is the
        # requested behaviour.
        cfg.setdefault("features", {})["airos_change_ip"] = True
    if rev < 2:
        # ntfy remote commands: off everywhere. Every stored config has the old
        # True written out, so the new default alone would change nothing. An
        # operator who wants them back ticks the box in Settings once.
        cfg.setdefault("alerts", {})["allow_commands"] = False
    cfg["config_rev"] = DEFAULTS["config_rev"]
    return cfg, True


def load():
    with _lock:
        try:
            with open(CONFIG_PATH) as f:
                raw = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            raw = {}
        merged = _deep_merge(DEFAULTS, raw)
        # raw, not merged: _deep_merge would have supplied the CURRENT rev from
        # DEFAULTS, making an un-migrated config look up to date.
        merged, changed = _migrate(merged, (raw or {}).get("config_rev", 0))
    if changed:
        save(merged)      # outside the lock — save() takes it itself
    return merged


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
