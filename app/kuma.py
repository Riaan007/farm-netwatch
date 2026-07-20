"""Uptime Kuma integration.

Two parts:
  * push()  — per-scan heartbeat to a monitor's push URL (HTTP, no deps).
  * provision()/deprovision()/test_login() — auto-create/delete monitors via
    Kuma's Socket.IO admin API. Kuma has no REST API for this; the token is
    generated client-side and passed in the `add` payload (that's how Kuma's own
    UI does it). python-socketio is imported lazily so push() needs no extra deps.
"""
import re
import secrets
import socket

import requests


def lan_ip():
    """This host's primary LAN IPv4 — the address other LAN devices reach it on."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 53))   # routing lookup only; nothing is sent
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return "127.0.0.1"


def effective_base(ki):
    """The Kuma base URL to actually use. With `auto_url` on, the host part
    follows this server's CURRENT LAN IP (Kuma runs alongside Netwatch), so the
    dashboard's Open-Kuma link works from any device and stays right when DHCP
    moves the Pi. The port is kept from base_url (default 3001)."""
    base = (ki.get("base_url") or "").rstrip("/")
    if not ki.get("auto_url"):
        return base
    m = re.search(r":(\d+)$", base)
    return f"http://{lan_ip()}:{m.group(1) if m else '3001'}"


def push(base_url, token, up, msg="", ping_ms=None):
    base = (base_url or "").rstrip("/")
    token = (token or "").strip()
    if not base or not token:
        return False
    params = {"status": "up" if up else "down", "msg": msg or ("online" if up else "offline")}
    if ping_ms is not None:
        try:
            params["ping"] = int(round(float(ping_ms)))
        except (TypeError, ValueError):
            pass
    try:
        r = requests.get(f"{base}/api/push/{token}", params=params, timeout=6)
        return r.status_code < 300
    except requests.RequestException:
        return False


# ---- admin API (Socket.IO) --------------------------------------------
def _connect(base_url, capture_info=False, timeout=15):
    import socketio
    sio = socketio.Client(reconnection=False)
    info = {}
    if capture_info:
        @sio.on("info")
        def _i(d):
            info.update(d or {})
    sio.connect((base_url or "").rstrip("/"), wait_timeout=timeout)
    return sio, info


def _login(sio, user, pw, timeout=15):
    r = sio.call("login", {"username": user, "password": pw, "token": ""}, timeout=timeout)
    return bool(r and r.get("ok")), (r or {}).get("msg", "login failed")


def test_login(base_url, user, pw):
    try:
        sio, info = _connect(base_url, capture_info=True)
    except Exception as e:
        return {"ok": False, "error": f"cannot reach Kuma at {base_url}: {e}"}
    try:
        ok, msg = _login(sio, user, pw)
        return {"ok": ok, "version": info.get("version"), "error": None if ok else msg}
    finally:
        try:
            sio.disconnect()
        except Exception:
            pass


# device category -> (Kuma tag label, colour) so monitors are tagged like the grid
_TAG = {
    "camera": ("Camera", "#f43f5e"), "nvr": ("NVR", "#e11d48"),
    "network": ("Network", "#6366f1"), "internet-ap": ("Internet AP", "#a855f7"),
    "router": ("Router", "#6366f1"), "printer": ("Printer", "#06b6d4"),
    "nas": ("NAS", "#3b82f6"), "voip": ("VoIP", "#22c55e"),
    "solar": ("Solar", "#eab308"), "media": ("Media", "#f97316"),
    "iot": ("IoT", "#14b8a6"), "pc": ("PC", "#64748b"),
    "server": ("Server", "#10b981"), "unknown": ("Unknown", "#f59e0b"),
}


def _tag_for(category):
    return _TAG.get(category, ((category or "Other").title(), "#64748b"))


def _ensure_tag(sio, cache, label, color):
    """Return a Kuma tag id for `label`, creating the tag if needed. `cache` maps
    lower(name) -> id and is filled lazily from getTags."""
    if cache is None:
        return None
    if not cache:
        tg = sio.call("getTags", timeout=15)
        for t in (tg.get("tags") if isinstance(tg, dict) else []) or []:
            cache[t["name"].lower()] = t["id"]
        cache.setdefault("__loaded__", True)
    key = label.lower()
    if key in cache:
        return cache[key]
    at = sio.call("addTag", {"name": label, "color": color}, timeout=15)
    tid = ((at.get("tag") or {}) if isinstance(at, dict) else {}).get("id")
    if tid is not None:
        cache[key] = tid
    return tid


def provision_many(base_url, user, pw, items, interval=60):
    """Create a Kuma PING monitor per item (Kuma pings the device directly, which
    gives a smooth graph + accurate uptime). items: [(key, name, ip, category)].
    Returns {key: {ok, monitor_id, error}}."""
    items = [tuple(it) + (None,) * (4 - len(it)) for it in items]
    try:
        sio, _ = _connect(base_url)
    except Exception as e:
        return {k: {"ok": False, "error": f"cannot reach Kuma: {e}"} for k, *_ in items}
    out = {}
    tag_cache = {}
    try:
        ok, msg = _login(sio, user, pw)
        if not ok:
            return {k: {"ok": False, "error": msg} for k, *_ in items}
        for key, name, ip, category in items:
            monitor = {"type": "ping", "name": name, "hostname": ip or "",
                       "interval": int(interval), "maxretries": 1,
                       "retryInterval": int(interval), "resendInterval": 0,
                       "upsideDown": False, "notificationIDList": {},
                       "accepted_statuscodes": ["200-299"], "packetSize": 56}
            try:
                add = sio.call("add", monitor, timeout=15)
            except Exception as e:
                out[key] = {"ok": False, "error": str(e)[:100]}
                continue
            if not (add and add.get("ok")):
                out[key] = {"ok": False, "error": (add or {}).get("msg", "add failed")}
                continue
            mid = add.get("monitorID")
            if category:
                try:
                    label, color = _tag_for(category)
                    tid = _ensure_tag(sio, tag_cache, label, color)
                    if tid is not None:
                        sio.call("addMonitorTag", (tid, mid, ""), timeout=15)
                except Exception:
                    pass        # tagging is best-effort; the monitor still works
            out[key] = {"ok": True, "monitor_id": mid}
    finally:
        try:
            sio.disconnect()
        except Exception:
            pass
    return out


def provision(base_url, user, pw, name, ip, interval=60, category=None):
    return provision_many(base_url, user, pw, [("_", name, ip, category)], interval).get(
        "_", {"ok": False, "error": "unknown"})


def provision_internet(base_url, user, pw, gateway_ip, interval=60):
    """Create the default internet-uptime monitors, tagged 'Internet': ping the
    gateway + public DNS (8.8.8.8 / 1.1.1.1) AND a real DNS-resolution check of
    google.com (so you can tell 'no link' from 'DNS broken'). Idempotency is the
    caller's job (guard on a registry marker). Returns {ok, monitors:{name:{...}}}."""
    plan = []
    if gateway_ip:
        plan.append(("Gateway", "ping", gateway_ip))
    plan += [("Internet 8.8.8.8", "ping", "8.8.8.8"),
             ("Internet 1.1.1.1", "ping", "1.1.1.1"),
             ("DNS google.com", "dns", "google.com")]
    try:
        sio, _ = _connect(base_url)
    except Exception as e:
        return {"ok": False, "error": f"cannot reach Kuma: {e}"}
    out = {}
    try:
        ok, msg = _login(sio, user, pw)
        if not ok:
            return {"ok": False, "error": msg}
        tid = _ensure_tag(sio, {}, "Internet", "#0ea5e9")
        for name, mtype, target in plan:
            mon = {"name": name, "interval": int(interval), "maxretries": 2,
                   "retryInterval": int(interval), "resendInterval": 0,
                   "upsideDown": False, "notificationIDList": {},
                   "accepted_statuscodes": ["200-299"], "packetSize": 56}
            if mtype == "dns":
                mon.update(type="dns", hostname=target, dns_resolve_server="8.8.8.8",
                           dns_resolve_type="A", port=53)
            else:
                mon.update(type="ping", hostname=target)
            try:
                add = sio.call("add", mon, timeout=15)
            except Exception as e:
                out[name] = {"ok": False, "error": str(e)[:100]}
                continue
            if not (add and add.get("ok")):
                out[name] = {"ok": False, "error": (add or {}).get("msg", "add failed")}
                continue
            mid = add.get("monitorID")
            if tid is not None:
                try:
                    sio.call("addMonitorTag", (tid, mid, ""), timeout=15)
                except Exception:
                    pass
            out[name] = {"ok": True, "monitor_id": mid}
    finally:
        try:
            sio.disconnect()
        except Exception:
            pass
    return {"ok": True, "monitors": out}


def ensure_ping(base_url, user, pw, items, interval=60):
    """Make each monitor a PING monitor pointing at `ip`. Used to follow a device
    that changed IP and to repair old push monitors. items: [(monitor_id, ip)]."""
    try:
        sio, _ = _connect(base_url)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    try:
        ok, msg = _login(sio, user, pw)
        if not ok:
            return {"ok": False, "error": msg}
        n = 0
        for mid, ip in items:
            if not (mid and ip):
                continue
            try:
                mon = (sio.call("getMonitor", mid, timeout=15) or {}).get("monitor")
                if not mon:
                    continue
                mon["type"] = "ping"
                mon["hostname"] = ip
                mon["interval"] = int(interval)
                mon["retryInterval"] = int(interval)
                r = sio.call("editMonitor", mon, timeout=15)
                if r and r.get("ok"):
                    n += 1
            except Exception:
                pass
        return {"ok": True, "updated": n}
    finally:
        try:
            sio.disconnect()
        except Exception:
            pass


def tag_monitors(base_url, user, pw, items):
    """Ensure each existing monitor carries its category tag.
    items: [(monitor_id, category)]. Idempotent (skips a tag already present)."""
    try:
        sio, _ = _connect(base_url)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    try:
        ok, msg = _login(sio, user, pw)
        if not ok:
            return {"ok": False, "error": msg}
        cache, n = {}, 0
        for mid, category in items:
            if not (mid and category):
                continue
            label, color = _tag_for(category)
            try:
                mon = sio.call("getMonitor", mid, timeout=15)
                have = {t.get("name", "").lower()
                        for t in (((mon or {}).get("monitor") or {}).get("tags") or [])}
                if label.lower() in have:
                    continue
                tid = _ensure_tag(sio, cache, label, color)
                if tid is not None:
                    sio.call("addMonitorTag", (tid, mid, ""), timeout=15)
                    n += 1
            except Exception:
                pass
        return {"ok": True, "tagged": n}
    finally:
        try:
            sio.disconnect()
        except Exception:
            pass


def deprovision(base_url, user, pw, monitor_id):
    try:
        sio, _ = _connect(base_url)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    try:
        ok, msg = _login(sio, user, pw)
        if not ok:
            return {"ok": False, "error": msg}
        r = sio.call("deleteMonitor", monitor_id, timeout=15)
        return {"ok": bool(r and r.get("ok")), "error": (r or {}).get("msg")}
    finally:
        try:
            sio.disconnect()
        except Exception:
            pass
