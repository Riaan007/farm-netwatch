"""Telegram phase 3 on the hub: WHY a site dropped off the VPN.

Each VPN site gets a private ntfy topic + an HMAC key (made here, pushed to
the site over the VPN with its Telegram settings). While a site cannot reach
the hub it sends signed heartbeats there straight over the farm's internet
(app/linkwatch.py). This module keeps ONE long-lived ntfy subscription to all
of those topics and remembers the last good heartbeat per site.

When a site has been unreachable for DIAG_AFTER_S the hub posts one
diagnosis (Ops group; the site's group too when the site is dark, because
then the site cannot speak for itself):

  heartbeat fresh                → alive, internet up, tunnel broken (+ why)
  no heartbeat, others fine      → DARK: power or internet at the farm
  every VPN site down at once    → the hub's side (one Ops message, not five)
  hub's own internet down        → nothing is said until it is back

Plus the DDNS truth check: does the name the sites dial (read from a wg-easy
client config) still resolve to the hub's real public IP? A mismatch for
DDNS_GRACE_S means every farm is about to lose its tunnel.
"""
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import socket
import threading
import time

import requests

DIAG_AFTER_S = 360
FRESH_S = 900
DDNS_EVERY_S = 300
DDNS_GRACE_S = 600
SERVER = "https://ntfy.sh"
VPN_NET = ipaddress.ip_network("10.8.0.0/24")

_lock = threading.Lock()
_last = {}          # sid -> {"p": payload, "rx": ts}
_down = {}          # sid -> {"since", "diag"}
_fleet = {"alerted": False}
_ddns = {"name": None, "name_ts": 0, "bad_since": None, "alerted": False, "last": None}
_stream = {"topics": None, "error": "", "connected": False}


def new_secret():
    return {"topic": "nwhb-" + secrets.token_hex(12), "key": secrets.token_hex(32), "server": SERVER}


def is_vpn_site(site):
    try:
        return ipaddress.ip_address(site.get("vpn_ip") or "") in VPN_NET
    except ValueError:
        return False


def verify(payload, key):
    if not isinstance(payload, dict) or not isinstance(payload.get("sig"), str) or not key:
        return False
    body = {k: v for k, v in payload.items() if k != "sig"}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    want = hmac.new(key.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, payload["sig"])


def accept(sid, payload, key, now=None):
    """Store a heartbeat if it is signed, recent and newer than the last one."""
    now = now or time.time()
    if not verify(payload, key):
        return False
    t = payload.get("t")
    if not isinstance(t, (int, float)) or t > now + 300 or now - t > 6 * 3600:
        return False
    with _lock:
        old = _last.get(sid)
        if old and old["p"].get("t", 0) >= t:
            return False
        _last[sid] = {"p": payload, "rx": now}
    return True


def latest(sid, now=None):
    now = now or time.time()
    with _lock:
        e = _last.get(sid)
    if not e:
        return None
    return {**e["p"], "age": now - e["p"].get("t", 0), "fresh": now - e["p"].get("t", 0) <= FRESH_S}


# ---- the subscription ---------------------------------------------------------------
def _topics(secrets_for):
    return {v["topic"]: (sid, v["key"]) for sid, v in secrets_for().items() if v.get("topic")}


def _stream_loop(secrets_for):
    while True:
        topics = _topics(secrets_for)
        if not topics:
            time.sleep(60)
            continue
        url = f"{SERVER}/{','.join(sorted(topics))}/json"
        started = time.time()
        try:
            with requests.get(url, params={"since": "1h"}, stream=True, timeout=(10, 90)) as r:
                _stream.update(connected=r.status_code == 200, error="" if r.status_code == 200 else f"HTTP {r.status_code}")
                for line in r.iter_lines():
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get("event") == "message" and ev.get("topic") in topics:
                        sid, key = topics[ev["topic"]]
                        try:
                            accept(sid, json.loads(ev.get("message") or "{}"), key)
                        except ValueError:
                            pass
                    if time.time() - started > 600 and _topics(secrets_for) != topics:
                        break                         # a site was added / removed
        except requests.RequestException as e:
            _stream.update(connected=False, error=e.__class__.__name__)
            time.sleep(30)


# ---- diagnosis -------------------------------------------------------------------
STATE_TEXT = {
    "tunnel_down_internet_up": "its internet and the hub's house both answer; only the tunnel is stuck "
                               "(UDP / NAT on the farm side). Its self-repair is trying a new port.",
    "hub_address_stale": "it says the hub's NAME resolves to a different address than its tunnel uses "
                         "(home IP changed / DDNS).",
    "hub_side_down": "it cannot reach the hub's house at all — look at the hub's internet first.",
    "dns_broken": "its router's DNS is broken (internet itself works).",
    "filtered": "its internet answers but web traffic is filtered.",
    "internet_down": "its internet was DOWN at that moment.",
    "router_down": "its router did not answer.",
    "ok": "all fine.",
}


def diagnose(sid, others_down, others_total, hub_inet_ok, now=None):
    """(kind, title, text) for a site the hub cannot reach."""
    now = now or time.time()
    hb = latest(sid, now)
    if hub_inet_ok is False:
        return "hub_offline", "", ""
    if others_total and others_down >= others_total:
        return "fleet", "Every site dropped together", ""
    if hb and hb["fresh"]:
        why = STATE_TEXT.get(hb.get("st"), hb.get("st") or "?")
        up = hb.get("up")
        extra = f"\nIts Pi has been up {int(up // 3600)} h {int(up % 3600 // 60)} min." if isinstance(up, (int, float)) else ""
        return ("alive", "Site is ALIVE — only the tunnel is down",
                f"Heartbeat {int(hb['age'] // 60)} min ago over its own internet: {why}{extra}")
    last = (f"Last heartbeat {time.strftime('%a %H:%M', time.localtime(hb['t']))} (it said: "
            f"{STATE_TEXT.get(hb.get('st'), hb.get('st'))})." if hb else "No heartbeat received from it.")
    return ("dark", "Site is DARK — power or internet at the farm",
            f"No VPN and no heartbeat over its own internet. {last} The other sites and the hub's "
            "internet are fine, so the cause is at the farm: power, or its internet line. "
            "I'll report when it is back.")


def on_poll(site, reachable, sites, snapshot, hub_inet_ok, send, now=None):
    """Called by the poller after every status poll. send(level, title, text, site, dark)."""
    now = now or time.time()
    sid = site["id"]
    if not is_vpn_site(site):
        return
    with _lock:
        d = _down.get(sid)
        if reachable:
            _down.pop(sid, None)
            if _fleet["alerted"] and not any(_down.values()):
                _fleet["alerted"] = False
            return
        if not d:
            _down[sid] = d = {"since": now, "diag": False}
        if d["diag"] or now - d["since"] < DIAG_AFTER_S:
            return
        d["diag"] = True
        vpn_sites = [s for s in sites if is_vpn_site(s) and s.get("enabled", True)]
        others = [s for s in vpn_sites if s["id"] != sid]
        others_down = sum(1 for s in others if s["id"] in _down)
        fleet_done = _fleet["alerted"]
    kind, title, text = diagnose(sid, others_down, len(others), hub_inet_ok, now)
    if kind == "hub_offline":
        with _lock:
            d["diag"] = False                     # say it once the hub's internet is back
        return
    if kind == "fleet":
        if not fleet_done and len(vpn_sites) >= 2:
            with _lock:
                _fleet["alerted"] = True
            send("critical", title,
                 f"All {len(vpn_sites)} VPN sites stopped answering at about the same time. The problem is on "
                 "the hub's side: its internet, the DDNS name, port 51820 on the router, or wg-easy.",
                 None, False)
        return
    send("critical" if kind == "dark" else "warning", title, text, sid, kind == "dark")


# ---- DDNS truth check -------------------------------------------------------------
def endpoint_name(get_config):
    """The hostname the sites dial, from any wg-easy client config (cached 1 h)."""
    if _ddns["name"] and time.time() - _ddns["name_ts"] < 3600:
        return _ddns["name"]
    try:
        conf = get_config() or ""
    except Exception:
        conf = ""
    m = re.search(r"^\s*Endpoint\s*=\s*\[?([^\]\s]+?)\]?:\d+\s*$", conf, re.M)
    if m:
        _ddns.update(name=m.group(1), name_ts=time.time())
    return _ddns["name"]


def resolve(name):
    try:
        return sorted({a[4][0] for a in socket.getaddrinfo(name, None, socket.AF_INET)})
    except OSError:
        return []


def ddns_check(name, hub_ip, send, now=None, resolver=resolve):
    now = now or time.time()
    if not name or not hub_ip or re.fullmatch(r"[\d.]+", name):
        return None
    ips = resolver(name)
    ok = hub_ip in ips
    _ddns["last"] = {"name": name, "ips": ips, "hub_ip": hub_ip, "ok": ok, "ts": now}
    if ok:
        if _ddns["alerted"]:
            send("info", "DDNS fixed", f"{name} points at the hub again ({hub_ip}).", None, False)
        _ddns.update(bad_since=None, alerted=False)
        return True
    if not ips:
        return None                              # DNS hiccup: not proof of anything
    _ddns["bad_since"] = _ddns["bad_since"] or now
    if not _ddns["alerted"] and now - _ddns["bad_since"] >= DDNS_GRACE_S:
        _ddns["alerted"] = True
        send("critical", "DDNS points at the WRONG address",
             f"The farms dial {name}, which resolves to {', '.join(ips)}, but the hub's public IP is "
             f"{hub_ip}. Every farm will lose its VPN as soon as its tunnel re-resolves. Update the "
             "DDNS record (the Ruijie's No-IP client) or the router.", None, False)
    return False


def _ddns_loop(get_config, hub_ip, send):
    while True:
        try:
            ddns_check(endpoint_name(get_config), hub_ip(), send)
        except Exception as e:
            print(f"[heartbeats] ddns: {e.__class__.__name__}: {e}", flush=True)
        time.sleep(DDNS_EVERY_S)


def status():
    with _lock:
        return {"stream": dict(_stream), "ddns": dict(_ddns.get("last") or {}),
                "last": {sid: {"t": e["p"].get("t"), "st": e["p"].get("st")} for sid, e in _last.items()}}


def start(secrets_for, get_config, hub_ip, send):
    threading.Thread(target=_stream_loop, args=(secrets_for,), daemon=True, name="hb-stream").start()
    threading.Thread(target=_ddns_loop, args=(get_config, hub_ip, send), daemon=True, name="hb-ddns").start()
