"""Hub-side IP-conflict detection.

The hub computes conflicts itself from each site's device list rather than
trusting a per-site flag, so it works even for sites running an older Netwatch
image that doesn't yet report `ip_conflict`. Mirrors the site scanner's two
problem kinds:

  ip_conflict      — 2+ devices proven up on one IP at the same time (red;
                     feeds the badge and the ntfy alert)
  identity_rotated — the IP was handed between MACs sequentially (rotating
                     private Wi-Fi addresses, DHCP reuse) — informational only
"""
import time

import requests

CONFLICT_WINDOW_S = 24 * 3600

LIVE, ROTATED = "ip_conflict", "identity_rotated"


def is_laa(mac):
    """Locally-administered MAC (second hex digit 2/6/A/E) — the mark of a
    randomized 'private Wi-Fi address' (iOS/Android/smart TVs)."""
    m = "".join(c for c in (mac or "").lower() if c in "0123456789abcdef")
    return len(m) == 12 and m[1] in "26ae"


def entry_kind(devs):
    """Classify one conflict entry from a point-in-time device snapshot: 2+
    claimants online at the same moment = overlapping = live; otherwise they
    held the address in turn. The site's own kind (pass-level + ARP-verified
    evidence) is better — use this only when the site didn't say."""
    return LIVE if sum(1 for d in devs or [] if d.get("online")) >= 2 else ROTATED


def fetch_site_conflicts(base, timeout):
    """The site's own /api/conflicts — the authoritative source, since the site
    honours its "Clear & re-test" acknowledgements and has the pass-level
    overlap evidence the hub can't see. Newer sites split live `conflicts`
    from `rotated`; an older site sends one undifferentiated list, classified
    here from the online flags. Returns [{ip, devices, kind}] or None when the
    site is unreachable — callers then fall back to the cached snapshot."""
    try:
        r = requests.get(f"{base}/api/conflicts", timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError):
        return None
    out = []
    for c in data.get("conflicts") or []:
        devs = c.get("devices") or []
        out.append({"ip": c.get("ip"), "devices": devs,
                    "kind": c.get("kind") or entry_kind(devs)})
    for c in data.get("rotated") or []:
        out.append({"ip": c.get("ip"), "devices": c.get("devices") or [],
                    "kind": ROTATED})
    return out


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


def conflict_entries(devices, window_s=CONFLICT_WINDOW_S):
    """[{ip, devices, kind}] computed from a cached device snapshot — the
    fallback when the site can't be asked live. Sorted by IP, newest sighting
    first within each entry."""
    cmap = conflict_map(devices, window_s)
    return [{"ip": ip,
             "devices": sorted(cmap[ip], key=lambda d: d.get("last_seen") or 0,
                               reverse=True),
             "kind": entry_kind(cmap[ip])}
            for ip in sorted(cmap, key=_ipkey)]


def split_ips(entries):
    """(live_ips, rotated_ips) from a list of {ip, kind} entries, each sorted."""
    live = sorted((e["ip"] for e in entries or [] if e.get("kind") != ROTATED),
                  key=_ipkey)
    rot = sorted((e["ip"] for e in entries or [] if e.get("kind") == ROTATED),
                 key=_ipkey)
    return live, rot
