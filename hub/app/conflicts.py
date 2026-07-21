"""Hub-side IP-conflict detection.

The hub computes conflicts itself from each site's device list rather than
trusting a per-site flag, so it works even for sites running an older Netwatch
image that doesn't yet report `ip_conflict`. Same rule as the site scanner:
2+ distinct devices (MAC keys) claiming one IP, both seen within the window.
"""
import time

import requests

CONFLICT_WINDOW_S = 24 * 3600


def fetch_site_conflicts(base, timeout):
    """The site's own /api/conflicts — the authoritative source, since the site
    honours its "Clear & re-test" acknowledgements (the hub-side computation
    below can't know about those). Returns [{ip, devices}] or None when the
    site is unreachable — callers then fall back to computing from the cached
    device snapshot."""
    try:
        r = requests.get(f"{base}/api/conflicts", timeout=timeout)
        r.raise_for_status()
        return [{"ip": c.get("ip"), "devices": c.get("devices") or []}
                for c in (r.json().get("conflicts") or [])]
    except (requests.RequestException, ValueError):
        return None


def _ipkey(ip):
    try:
        return tuple(int(o) for o in (ip or "").split("."))
    except ValueError:
        return (9999,)


def conflict_map(devices, window_s=CONFLICT_WINDOW_S):
    """{ip: [device, ...]} for IPs claimed by 2+ distinct keys seen within window."""
    cutoff = int(time.time()) - window_s
    by_ip = {}
    for d in devices or []:
        ip = d.get("ip")
        if ip and d.get("last_seen", 0) >= cutoff:
            by_ip.setdefault(ip, []).append(d)
    return {ip: ds for ip, ds in by_ip.items()
            if len({d.get("key") for d in ds}) > 1}


def conflict_ips(devices, window_s=CONFLICT_WINDOW_S):
    """Sorted list of conflicted IP addresses."""
    return sorted(conflict_map(devices, window_s), key=_ipkey)
