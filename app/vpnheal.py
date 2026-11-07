"""Self-repair for the VPN tunnel to the Central Hub.

Tri-Lectro, 2026-11-07: from 03:20 to 06:39 the hub could not reach the site,
yet the Pi kept scanning, the router and recorders answered and the Pi's
internet checks passed — only the WireGuard tunnel was stuck, and it took a
Pi restart to bring it back. A tunnel can get stuck like that when the farm
router's NAT entry for it goes stale, or when the hub's address changes under
a DDNS name that WireGuard resolved only once, at start.

The heartbeat (scanner._heartbeat_tick) pings the hub every minute and calls
check() with that answer and whether this site's internet works. While the hub
stays silent and the internet works, this module steps in — gently, and never
by deleting or restarting wg0 (on sidecar sites another container owns it):

  after `heal_after_min` (5)  : re-resolve the hub's endpoint from wg0.conf and
                                set it on the peer, so WireGuard sends a fresh
                                handshake to the hub's current address;
  after twice that (10)       : give wg0 a new random listen port, so the farm
                                router makes a brand-new NAT entry;
  then every `heal_repeat_min`: the new-port step again, while it stays down.

Every step and the recovery are written to History (type `vpn_heal`); the
recovery also notifies. Switch: config vpn.self_heal (default on).
"""
import random
import re
import socket
import threading
import time

import hubvpn

_lock = threading.Lock()
_state = {"down_since": None, "actions": [], "last_action": None, "next_at": None}


def _conf_endpoint():
    """(host, port) of the hub from wg0.conf, or None."""
    try:
        with open(hubvpn.WG_CONF) as f:
            m = re.search(r"^\s*Endpoint\s*=\s*(\S+):(\d+)\s*$", f.read(), re.M)
    except OSError:
        return None
    if not m:
        return None
    return m.group(1).strip("[]"), int(m.group(2))


def _peer():
    rc, out = hubvpn._run(["wg", "show", hubvpn.IFACE, "peers"])
    return out.split()[0] if rc == 0 and out.strip() else None


def rehandshake():
    """Point the peer at the hub's freshly resolved address. Returns (ok, detail)."""
    ep, peer = _conf_endpoint(), _peer()
    if not ep or not peer:
        return False, "no endpoint in wg0.conf" if not ep else "wg0 has no peer"
    host, port = ep
    try:
        ip = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)[0][4][0]
    except (socket.gaierror, IndexError, OSError) as e:
        return False, f"could not resolve {host}: {e}"
    rc, out = hubvpn._run(["wg", "set", hubvpn.IFACE, "peer", peer, "endpoint", f"{ip}:{port}"])
    return rc == 0, (f"endpoint {host} -> {ip}:{port}" if rc == 0 else out or "wg set failed")


def new_port():
    """Give wg0 a new source port, so the farm router opens a fresh NAT entry."""
    port = random.randint(20000, 60999)
    rc, out = hubvpn._run(["wg", "set", hubvpn.IFACE, "listen-port", str(port)])
    return rc == 0, (f"listen port {port}" if rc == 0 else out or "wg set failed")


def _settings(cfg):
    v = (cfg or {}).get("vpn") or {}
    return (v.get("self_heal", True), max(2, int(v.get("heal_after_min", 5) or 5)),
            max(5, int(v.get("heal_repeat_min", 15) or 15)))


def check(cfg, hub_up, internet_ok, now=None, log=None, notify_fn=None):
    """One heartbeat. Returns the action taken ('rehandshake' / 'new_port') or None."""
    on, after, repeat = _settings(cfg)
    now = now or time.time()
    with _lock:
        if hub_up:
            if _state["down_since"] is not None and _state["actions"]:
                mins = round((now - _state["down_since"]) / 60)
                tried = ", ".join(a["step"] for a in _state["actions"])
                if log:
                    log("recovered", {"down_min": mins, "tried": tried})
                if notify_fn:
                    notify_fn(f"The VPN link to the Central Hub is back after {mins} min "
                              f"(self-repair: {tried}).")
            _state.update(down_since=None, actions=[], last_action=None, next_at=None)
            return None
        if not on or not hubvpn.has_config() or not hubvpn.is_up():
            return None
        if not internet_ok:
            # nothing a tunnel tweak can fix; restart the clock once the internet is back
            _state.update(down_since=None, actions=[], next_at=None)
            return None
        if _state["down_since"] is None:
            _state["down_since"] = now
            _state["next_at"] = now + after * 60
            return None
        if now < _state["next_at"]:
            return None
        step = "rehandshake" if not _state["actions"] else "new_port"
        ok, detail = (rehandshake if step == "rehandshake" else new_port)()
        _state["actions"].append({"step": step, "ok": ok, "detail": detail, "ts": int(now)})
        _state["last_action"] = _state["actions"][-1]
        _state["next_at"] = now + (after if step == "rehandshake" else repeat) * 60
        if log:
            log(step, {"ok": ok, "detail": detail,
                       "down_min": round((now - _state["down_since"]) / 60)})
        return step


def status():
    """For the Central Hub card / API: what the self-repair is doing right now."""
    with _lock:
        return {"down_since": _state["down_since"], "last_action": _state["last_action"],
                "next_at": _state["next_at"] if _state["down_since"] else None}


def reset():
    with _lock:
        _state.update(down_since=None, actions=[], last_action=None, next_at=None)
