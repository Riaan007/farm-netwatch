"""Background poller: pulls every registered site's Netwatch + Kuma over the VPN.

Pull model — sites need no hub-specific code; the hub calls their existing
JSON APIs (http://<vpn-ip>:8090/api/...). Three fetch classes with their own
cadence per site:

  status   (60s)  GET /api/status   -> reachability heartbeat + site name
  devices  (300s) GET /api/devices  -> full device list (also fetched right
                                       after a site comes back online)
  kuma     (120s) status-page JSON  -> per-monitor beats (server-cached 60s)

Each successful devices payload is persisted to /data/snapshots/<site>.json so
the dashboard still renders (marked stale) across hub restarts and site
outages. One site_samples row is written per status poll.
"""
import concurrent.futures
import json
import os
import re
import threading
import time

import requests

import conflicts as conflictutil
import hubconfig
import kuma_status
import notify
import sitehistory

SNAP_DIR = os.path.join(os.environ.get("HUB_DATA", "/data"), "snapshots")

_CLASSES = ("status", "devices", "kuma")


def _safe(site_id):
    return re.sub(r"[^A-Za-z0-9_.-]", "-", site_id)


def _ip_sortkey(ip):
    try:
        return tuple(int(o) for o in (ip or "").split("."))
    except ValueError:
        return (9999,)


class Poller:
    def __init__(self):
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._snap = {}        # site_id -> snapshot dict
        self._due = {}         # site_id -> {class: next_due_ts}
        self._last_prune = 0

    # ---- public API used by the web layer --------------------------------
    def snapshot(self, site_id):
        with self._lock:
            return dict(self._snap.get(site_id) or {})

    def poll_now(self, site_id=None):
        with self._lock:
            for sid, due in self._due.items():
                if site_id in (None, sid):
                    for cls in _CLASSES:
                        due[cls] = 0
        self._wake.set()

    def start(self):
        self._load_snapshots()
        threading.Thread(target=self._run, daemon=True, name="hub-poller").start()

    # ---- snapshot persistence ---------------------------------------------
    def _load_snapshots(self):
        try:
            names = os.listdir(SNAP_DIR)
        except FileNotFoundError:
            return
        for n in names:
            if not n.endswith(".json"):
                continue
            try:
                with open(os.path.join(SNAP_DIR, n)) as f:
                    saved = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            sid = saved.get("site_id")
            if sid:
                with self._lock:
                    self._snap[sid] = {
                        "reachable": False,
                        "devices": saved.get("devices"),
                        "devices_fetched": saved.get("fetched_at"),
                        "stale": True,
                    }

    def _save_snapshot(self, site_id, devices, fetched_at):
        os.makedirs(SNAP_DIR, exist_ok=True)
        path = os.path.join(SNAP_DIR, _safe(site_id) + ".json")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"site_id": site_id, "fetched_at": fetched_at,
                       "devices": devices}, f)
        os.replace(tmp, path)

    # ---- main loop ---------------------------------------------------------
    def _run(self):
        while True:
            try:
                self._tick()
            except Exception as e:                     # never let the loop die
                print(f"[poller] tick failed: {e}", flush=True)
            self._wake.wait(timeout=15)
            self._wake.clear()

    def _tick(self):
        cfg = hubconfig.load()
        poll = cfg["poll"]
        timeout = (poll["timeout_connect_s"], poll["timeout_read_s"])
        sites = [s for s in cfg["sites"] if s.get("enabled", True)]
        now = time.time()

        jobs = []
        with self._lock:
            known = {s["id"] for s in sites}
            for sid in list(self._due):
                if sid not in known:
                    del self._due[sid]
            for site in sites:
                due = self._due.setdefault(site["id"], {c: 0 for c in _CLASSES})
                if now >= due["status"]:
                    due["status"] = now + poll["status_interval_s"]
                    jobs.append(("status", site))
                if now >= due["devices"]:
                    due["devices"] = now + poll["devices_interval_s"]
                    jobs.append(("devices", site))
                if site.get("kuma_url") and site.get("kuma_status_slug") \
                        and now >= due["kuma"]:
                    due["kuma"] = now + poll["kuma_interval_s"]
                    jobs.append(("kuma", site))

        if jobs:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(self._fetch, cls, site, timeout)
                           for cls, site in jobs]
                concurrent.futures.wait(futures, timeout=60)

        if now - self._last_prune > 86400:
            self._last_prune = now
            sitehistory.prune(poll["history_days"])

    # ---- fetchers ------------------------------------------------------------
    def _base_url(self, site):
        return f"http://{site['vpn_ip']}:{site.get('netwatch_port', 8090)}"

    def _fetch(self, cls, site, timeout):
        sid = site["id"]
        if cls == "status":
            self._fetch_status(sid, site, timeout)
        elif cls == "devices":
            self._fetch_devices(sid, site, timeout)
        elif cls == "kuma":
            res = kuma_status.fetch(site["kuma_url"], site["kuma_status_slug"],
                                    timeout=timeout)
            with self._lock:
                self._snap.setdefault(sid, {})["kuma"] = res
                self._snap[sid]["kuma_fetched"] = int(time.time())

    def _fetch_status(self, sid, site, timeout):
        t0 = time.time()
        status, err = None, ""
        try:
            r = requests.get(self._base_url(site) + "/api/status", timeout=timeout)
            r.raise_for_status()
            status = r.json()
        except requests.exceptions.RequestException as e:
            err = e.__class__.__name__
        except ValueError:
            err = "bad-json"
        latency = round((time.time() - t0) * 1000, 1) if status else None

        with self._lock:
            snap = self._snap.setdefault(sid, {})
            was_reachable = snap.get("reachable", False)
            snap["reachable"] = status is not None
            snap["status"] = status if status else snap.get("status")
            snap["status_error"] = err
            snap["latency_ms"] = latency
            snap["status_fetched"] = int(time.time())
            devices = (snap.get("devices") or {}).get("devices") or []
            kuma = snap.get("kuma") or {}
            # A site that just came back deserves fresh devices right away.
            if status is not None and not was_reachable:
                self._due.setdefault(sid, {c: 0 for c in _CLASSES})["devices"] = 0
                self._wake.set()
            # Debounced offline tracking for ntfy alerts.
            miss = 0 if status is not None else snap.get("offline_miss", 0) + 1
            snap["offline_miss"] = miss
            alerted = snap.get("offline_alerted", False)
            fire = None
            if status is None and not alerted:
                fire = "offline"
            elif status is not None and alerted:
                snap["offline_alerted"] = False
                fire = "online"

        self._maybe_alert_offline(sid, site, fire, miss)

        total = len(devices) if devices else None
        online = sum(1 for d in devices if d.get("online")) if devices else None
        watched_down = sum(1 for d in devices
                           if d.get("watch") and not d.get("online")) if devices else None
        # Prefer the site's own conflict list — it honours "Clear & re-test"
        # acks, so badges and alerts drop a cleared conflict on the next poll.
        site_conf = (conflictutil.fetch_site_conflicts(self._base_url(site), timeout)
                     if status is not None else None)
        if site_conf is not None:
            conflict_ips = sorted((c["ip"] for c in site_conf), key=_ip_sortkey)
        else:
            conflict_ips = conflictutil.conflict_ips(devices)
        with self._lock:
            self._snap.setdefault(sid, {})["conflict_ips"] = conflict_ips
        self._maybe_alert_conflict(sid, site, conflict_ips, reachable=status is not None,
                                   have_devices=bool(devices))
        sitehistory.record(
            sid, status is not None, http_ms=latency,
            devices_total=total, devices_online=online, watched_down=watched_down,
            kuma_up=kuma.get("up") if kuma.get("ok") else None,
            kuma_down=kuma.get("down") if kuma.get("ok") else None,
            conflicts=len(conflict_ips) if devices else None,
        )

    def _maybe_alert_offline(self, sid, site, fire, miss):
        """ntfy when a site crosses the offline threshold, or recovers."""
        if not fire:
            return
        alerts = hubconfig.load().get("alerts", {})
        if not alerts.get("notify_site_offline", True) or not site.get("alerts_enabled", True):
            return
        name = site.get("name") or sid
        if fire == "offline":
            if miss < max(1, int(alerts.get("offline_after_polls", 2))):
                return
            with self._lock:
                self._snap.setdefault(sid, {})["offline_alerted"] = True
            notify.push(alerts, f"Site offline: {name}",
                        f"The Central Hub can't reach '{name}' over the VPN.",
                        priority="high", tags=["red_circle"])
        else:
            notify.push(alerts, f"Site back online: {name}",
                        f"'{name}' is reachable from the hub again.",
                        tags=["white_check_mark"])

    def _maybe_alert_conflict(self, sid, site, conflict_ips, reachable, have_devices):
        """ntfy + log when a site gains a NEW IP conflict, or when all clear.

        Edge-triggered against the set of conflicted IPs already alerted, so each
        conflict pages once (not every poll). Skipped while the site is
        unreachable / has no device data, so a dropped link doesn't look 'resolved'."""
        if not have_devices or not reachable:
            return
        with self._lock:
            snap = self._snap.setdefault(sid, {})
            prev = set(snap.get("conflict_alerted") or [])
            now = set(conflict_ips)
            snap["conflict_alerted"] = sorted(now, key=_ip_sortkey)
        new_ips = now - prev
        cleared = prev and not now
        if new_ips:
            print(f"[poller] {sid}: IP conflict on {', '.join(sorted(new_ips, key=_ip_sortkey))}",
                  flush=True)
        alerts = hubconfig.load().get("alerts", {})
        if not alerts.get("notify_ip_conflict", True) or not site.get("alerts_enabled", True):
            return
        name = site.get("name") or sid
        if new_ips:
            ips = ", ".join(sorted(now, key=_ip_sortkey))
            notify.push(alerts, f"IP conflict: {name}",
                        f"More than one device is answering the same address at '{name}': {ips}. "
                        f"Give one device a unique IP.",
                        priority="high", tags=["warning"])
        elif cleared:
            notify.push(alerts, f"IP conflict cleared: {name}",
                        f"All IP-address conflicts at '{name}' are resolved.",
                        tags=["white_check_mark"])

    def _fetch_devices(self, sid, site, timeout):
        try:
            r = requests.get(self._base_url(site) + "/api/devices", timeout=timeout)
            r.raise_for_status()
            payload = r.json()
        except (requests.exceptions.RequestException, ValueError):
            return                                     # keep last-known-good
        fetched = int(time.time())
        with self._lock:
            snap = self._snap.setdefault(sid, {})
            snap["devices"] = payload
            snap["devices_fetched"] = fetched
            snap["stale"] = False
        try:
            self._save_snapshot(sid, payload, fetched)
        except OSError as e:
            print(f"[poller] snapshot save failed for {sid}: {e}", flush=True)


poller = Poller()
