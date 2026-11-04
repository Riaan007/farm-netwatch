"""This site's public (internet) IP address, shown in the site page header.

The Pi asks Cloudflare's /cdn-cgi/trace which address its request came from: one
small HTTPS request (TLS verified) every 15 minutes, by IP literal so it is the
IPv4 address and works even when DNS is broken. That is the farm's internet
address: the router's WAN address, or the ISP's shared one behind carrier NAT.
The hub shows the same thing from the site's VPN tunnel (hub/app/publicip.py).

Kept in /data/public_ip.json with when it last changed and up to 10 earlier
addresses, so a restart or an internet outage keeps the last known address.
"""
import ipaddress
import json
import os
import threading
import time

import requests

DATA_DIR = os.environ.get("NETWATCH_DATA", "/data")
STATE_PATH = os.path.join(DATA_DIR, "public_ip.json")
EVERY_S = 900          # one lookup per 15 minutes
RETRY_S = 120          # sooner after a failed lookup (internet down / just booted)
STALE_S = 2 * EVERY_S + 300
PREV_MAX = 10
TRACE_URLS = ("https://1.1.1.1/cdn-cgi/trace", "https://1.0.0.1/cdn-cgi/trace")

_lock = threading.Lock()
_state = None
_wake = threading.Event()


def lookup():
    """The address Cloudflare sees this Pi's request come from, or None."""
    for url in TRACE_URLS:
        try:
            r = requests.get(url, timeout=(4, 6))
            if r.status_code != 200:
                continue
            for line in r.text.splitlines()[:40]:
                if line.startswith("ip="):
                    return str(ipaddress.ip_address(line[3:].strip()))
        except (requests.exceptions.RequestException, ValueError):
            continue
    return None


def _load():
    global _state
    if _state is None:
        try:
            with open(STATE_PATH) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        _state = data if isinstance(data, dict) and data.get("ip") else {}
    return _state


def _save(st):
    tmp = STATE_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(st, f)
        os.replace(tmp, STATE_PATH)
    except OSError as e:
        print(f"[publicip] cannot save {STATE_PATH}: {e}", flush=True)


def record(ip, now=None):
    """Fold one successful lookup into the state; returns 'new'/'changed'/'same'.
    "checked" is kept in memory only (a write every 15 min isn't worth it)."""
    now = time.time() if now is None else now
    with _lock:
        st = _load()
        if not st.get("ip"):
            st.clear()
            st.update(ip=ip, since=None, first=now, checked=now, prev=[])
            res = "new"
        elif st["ip"] != ip:
            prev = [{"ip": st["ip"], "from": st.get("since") or st.get("first"),
                     "from_known": bool(st.get("since")), "to": st.get("checked") or now}]
            st.update(ip=ip, since=now, checked=now,
                      prev=(prev + list(st.get("prev") or []))[:PREV_MAX])
            res = "changed"
        else:
            st["checked"] = now
            return "same"
        _save(st)
    if res == "changed":
        print(f"[publicip] public IP changed to {ip}", flush=True)
    return res


def current(now=None):
    """{ip, since, first, checked, fresh, prev} for /api/internet, or None."""
    now = time.time() if now is None else now
    with _lock:
        st = _load()
        if not st.get("ip"):
            return None
        checked = st.get("checked")
        return {"ip": st["ip"], "since": st.get("since"), "first": st.get("first"),
                "checked": checked, "fresh": bool(checked) and now - checked <= STALE_S,
                "prev": [dict(p) for p in (st.get("prev") or [])[:5]]}


def _loop():
    while True:
        ip = None
        try:
            ip = lookup()
            if ip:
                record(ip)
        except Exception as e:                 # never let the thread die
            print(f"[publicip] lookup failed: {e}", flush=True)
        _wake.wait(EVERY_S if ip else RETRY_S)
        _wake.clear()


def start():
    threading.Thread(target=_loop, daemon=True, name="publicip").start()
