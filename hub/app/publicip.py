"""Each site's public (internet) IP address, as the hub sees it.

VPN sites: the address the site's WireGuard tunnel arrives from. wg0 lives in
the network namespace the hub shares with wg-easy, so `wg show wg0 endpoints`
lists the address every peer last sent from: the public address of the farm's
internet connection (its router's WAN address, or the ISP's shared address when
the farm sits behind carrier NAT). No request to the site, no outside service,
and nothing to update on the farm Pis. Needs `wg` (wireguard-tools, in the hub
image) and NET_ADMIN (the hub already has it for vpnfw). Only the `endpoints`,
`allowed-ips` and `latest-handshakes` views are read; never `dump`, whose first
line carries the server's private key.

A site the hub reaches on a private LAN address instead (the home Netwatch next
to the hub) shares the hub's own internet connection, so it gets the hub's
public address: one small HTTPS request to Cloudflare's /cdn-cgi/trace every
15 minutes.

Kept in /data/public_ip.json, so an offline site still shows its last address,
when it was last seen, and the addresses it had before.
"""
import ipaddress
import json
import os
import subprocess
import threading
import time

import requests

import hubconfig

STATE_PATH = os.path.join(os.environ.get("HUB_DATA", "/data"), "public_ip.json")
IFACE = "wg0"
VPN_SUBNET = ipaddress.ip_network("10.8.0.0/24")   # same as proxycfg.VPN_SUBNET
REFRESH_S = 60        # tunnels are read this often
LIVE_S = 180          # WireGuard drops a session 180 s after the last handshake
HUB_EVERY_S = 900     # the hub's own address: one HTTPS request per 15 min
SAVE_EVERY_S = 900    # "last seen" goes to disk at most this often; a change saves at once
PREV_MAX = 10         # earlier addresses kept per site
TRACE_URLS = ("https://1.1.1.1/cdn-cgi/trace", "https://1.0.0.1/cdn-cgi/trace")

_lock = threading.Lock()
_state = None         # {"sites": {site_id: entry}, "hub": entry | None}, loaded lazily
_saved_at = 0.0
_hub_checked = 0.0
_error = ""           # why the tunnels could not be read last time ("" = they could)


# ---- reading the tunnels -------------------------------------------------------
def _wg(view):
    r = subprocess.run(["wg", "show", IFACE, view], capture_output=True, text=True,
                       timeout=10)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "").strip()[:160] or f"wg exited {r.returncode}")
    return r.stdout


def _pairs(text):
    """`wg show <if> <view>` output: one '<peer public key>\\t<value>' line per peer."""
    out = {}
    for line in (text or "").splitlines():
        key, sep, val = line.partition("\t")
        if sep and key.strip():
            out[key.strip()] = val.strip()
    return out


def endpoint_ip(value):
    """'203.0.113.7:51820' or '[2001:db8::1]:51820' -> the address; '(none)' -> None."""
    v = (value or "").strip()
    if not v or v == "(none)":
        return None
    if v.startswith("["):
        host = v[1:v.find("]")] if "]" in v else ""
    else:
        host = v.rpartition(":")[0]
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


def parse_peers(endpoints, allowed_ips, handshakes):
    """{tunnel address: {"ip": public address or None, "handshake": unix ts or None}}
    from the three `wg show` views, keyed by each peer's /32 inside the tunnel."""
    ends, allowed, shakes = _pairs(endpoints), _pairs(allowed_ips), _pairs(handshakes)
    peers = {}
    for key, nets in allowed.items():
        try:
            hs = int(shakes.get(key) or 0)
        except ValueError:
            hs = 0
        rec = {"ip": endpoint_ip(ends.get(key)), "handshake": hs or None}
        for net in nets.replace(",", " ").split():
            addr, _, bits = net.partition("/")
            if bits == "32":
                peers[addr] = rec
    return peers


def read_peers():
    return parse_peers(_wg("endpoints"), _wg("allowed-ips"), _wg("latest-handshakes"))


def site_kind(vpn_ip):
    """'vpn' for a site on the tunnel, 'lan' for one the hub reaches on its own
    private network (it shares the hub's internet connection), else None."""
    try:
        ip = ipaddress.ip_address((vpn_ip or "").strip())
    except ValueError:
        return None
    if ip in VPN_SUBNET:
        return "vpn"
    return "lan" if ip.is_private else None


def hub_lookup():
    """The hub's own public IPv4 address, or None (offline, or Cloudflare unreachable)."""
    for url in TRACE_URLS:
        try:
            r = requests.get(url, timeout=(3, 5))
            if r.status_code != 200:
                continue
            for line in r.text.splitlines()[:40]:
                if line.startswith("ip="):
                    return str(ipaddress.ip_address(line[3:].strip()))
        except (requests.exceptions.RequestException, ValueError):
            continue
    return None


# ---- remembering what was seen -------------------------------------------------
def _load():
    global _state
    if _state is None:
        try:
            with open(STATE_PATH) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        sites = data.get("sites") if isinstance(data, dict) else None
        _state = {"sites": sites if isinstance(sites, dict) else {},
                  "hub": data.get("hub") if isinstance(data, dict) else None}
    return _state


def _save(st, now):
    global _saved_at
    _saved_at = now
    tmp = STATE_PATH + ".tmp"
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(st, f)
        os.replace(tmp, STATE_PATH)
    except OSError as e:
        print(f"[publicip] cannot save {STATE_PATH}: {e}", flush=True)


def _observe(entry, ip, source, seen, now):
    """Fold one sighting into an entry (a dict, changed in place). Returns
    'new' / 'changed' / 'seen' / None, so the caller knows whether to save now."""
    if not entry.get("ip"):
        entry.clear()
        entry.update(ip=ip, source=source, since=None, first=now, seen=seen, prev=[])
        return "new"
    if entry["ip"] != ip:
        prev = [{"ip": entry["ip"], "from": entry.get("since") or entry.get("first"),
                 "from_known": bool(entry.get("since")), "to": entry.get("seen") or now}]
        prev += list(entry.get("prev") or [])
        entry.update(ip=ip, source=source, since=now, seen=seen, prev=prev[:PREV_MAX])
        return "changed"
    entry["source"] = source
    if seen and seen > (entry.get("seen") or 0):
        entry["seen"] = seen
        return "seen"
    return None


def refresh(sites, now=None, peers=None, hub_ip=None):
    """Read the tunnels (and every 15 min the hub's own address) and remember what
    changed. `peers` / `hub_ip` are for tests; normally both are looked up here."""
    global _hub_checked, _error
    now = time.time() if now is None else now
    if peers is None:
        try:
            peers, err = read_peers(), ""
        except FileNotFoundError:
            peers, err = None, "wg is not installed in this hub image (rebuild it)"
        except (OSError, subprocess.SubprocessError, RuntimeError) as e:
            peers, err = None, (str(e) or e.__class__.__name__)[:160]
        if err != _error and err:
            print(f"[publicip] cannot read the WireGuard tunnels: {err}", flush=True)
        _error = err
    if hub_ip is None and now - _hub_checked >= HUB_EVERY_S:
        _hub_checked = now
        hub_ip = hub_lookup()

    with _lock:
        st = _load()
        save = False
        seen_only = False
        if hub_ip:
            st["hub"] = st.get("hub") or {}
            res = _observe(st["hub"], hub_ip, "hub", now, now)
            save |= res in ("new", "changed")
            seen_only |= res == "seen"
        ids = set()
        for s in sites:
            sid = s.get("id")
            if not sid:
                continue
            ids.add(sid)
            kind = site_kind(s.get("vpn_ip"))
            if kind == "vpn" and peers is not None:
                p = peers.get(s["vpn_ip"].strip())
                if not p or not p["ip"]:
                    continue          # never connected since wg-easy started: keep the last one
                ip, source, seen = p["ip"], "vpn", p["handshake"] or now
            elif kind == "lan" and hub_ip:
                ip, source, seen = hub_ip, "hub", now
            else:
                continue
            res = _observe(st["sites"].setdefault(sid, {}), ip, source, seen, now)
            save |= res in ("new", "changed")
            seen_only |= res == "seen"
            if res == "changed":
                print(f"[publicip] {sid}: public IP changed to {ip}", flush=True)
        for sid in [k for k in st["sites"] if k not in ids or not st["sites"][k].get("ip")]:
            del st["sites"][sid]       # site removed from the hub (or never seen)
            save = True
        if save or (seen_only and now - _saved_at >= SAVE_EVERY_S):
            _save(st, now)


def _view(entry, now):
    if not entry or not entry.get("ip"):
        return None
    seen = entry.get("seen")
    limit = LIVE_S if entry.get("source") == "vpn" else 2 * HUB_EVERY_S + REFRESH_S
    return {
        "ip": entry["ip"],
        "source": entry.get("source"),      # 'vpn' = its tunnel, 'hub' = the hub's line
        "since": entry.get("since"),        # None = unchanged since the hub first saw it
        "first": entry.get("first"),
        "seen": seen,
        "live": bool(seen) and now - seen <= limit,
        "prev": [dict(p) for p in (entry.get("prev") or [])[:5]],
    }


def card(site_id, now=None):
    """What the Control Center shows for one site, or None when not known yet."""
    now = time.time() if now is None else now
    with _lock:
        return _view(_load()["sites"].get(site_id), now)


def hub_card(now=None):
    now = time.time() if now is None else now
    with _lock:
        return _view(_load().get("hub"), now)


# ---- background loop -------------------------------------------------------------
def _loop():
    while True:
        try:
            refresh(hubconfig.load()["sites"])
        except Exception as e:                   # never let the thread die
            print(f"[publicip] refresh failed: {e}", flush=True)
        time.sleep(REFRESH_S)


def start():
    threading.Thread(target=_loop, daemon=True, name="hub-publicip").start()
