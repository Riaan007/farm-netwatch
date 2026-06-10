"""Uptime Kuma integration.

Two parts:
  * push()  — per-scan heartbeat to a monitor's push URL (HTTP, no deps).
  * provision()/deprovision()/test_login() — auto-create/delete monitors via
    Kuma's Socket.IO admin API. Kuma has no REST API for this; the token is
    generated client-side and passed in the `add` payload (that's how Kuma's own
    UI does it). python-socketio is imported lazily so push() needs no extra deps.
"""
import secrets

import requests


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
    """items: [(key, monitor_name, category)]. category may be None.
    Returns {key: {ok, monitor_id, token, error}}."""
    items = [(it + (None,))[:3] if len(it) < 3 else it for it in items]
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
        for key, name, category in items:
            token = secrets.token_hex(16)
            monitor = {"type": "push", "name": name, "interval": int(interval),
                       "maxretries": 1, "retryInterval": int(interval), "resendInterval": 0,
                       "upsideDown": False, "notificationIDList": {},
                       "accepted_statuscodes": ["200-299"], "pushToken": token}
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
            out[key] = {"ok": True, "monitor_id": mid, "token": token}
    finally:
        try:
            sio.disconnect()
        except Exception:
            pass
    return out


def provision(base_url, user, pw, name, interval=60, category=None):
    return provision_many(base_url, user, pw, [("_", name, category)], interval).get(
        "_", {"ok": False, "error": "unknown"})


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
