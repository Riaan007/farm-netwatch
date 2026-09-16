"""Monitored devices — the equipment the operator must see online or offline.

One switch per device, kept as the registry's `watch` flag (its older name: the
hub, config backups and hubs that predate this all read it). A monitored device

  * is listed under "Monitored" on the site page and in the hub, offline ones
    first; every other device is listed under "Other devices";
  * never goes "quiet" and is never pruned — down for a week is still down;
  * is a site fault on the hub while it is down, and the hub pushes an alert
    when it drops or comes back (hub Settings -> Alerts);
  * is pinged every minute for its latency chart (scanner heartbeats);
  * has an Uptime Kuma ping monitor when Kuma's admin login is saved. Switching
    monitoring ON creates that monitor (or resumes it); switching it OFF pauses
    it — no checks or Kuma alerts, history kept. Forgetting or pruning the
    device deletes it. A hand-made push-token monitor is left alone.

Upgrade from the two old switches (the 🔔 watch flag and "Monitor in Uptime
Kuma"): the operator's real choices were the Kuma monitors, so every device
that has one becomes monitored — once. config_rev 3 sets
`monitoring.seed_pending`; seed() consumes it at startup and after a restore.

Kuma calls take seconds each (socket.io login), so they run on one background
worker. Keys are coalesced and the worker applies each key's CURRENT switch, so
quick on/off clicks end in the last state. After every scan reconcile() queues
devices whose monitor is out of step (e.g. Kuma was down), each at most once per
RETRY_S, and every SYNC_S it has the worker read Kuma's real monitor list: a
monitor deleted or paused/resumed in Kuma's own UI is noticed and put right.
Only an explicit "off" pauses a monitor — a device nobody has switched either
way (e.g. from an older backup) keeps its monitor running.
"""
import threading
import time

import config
import creds
import kuma

RETRY_S = 1800          # a device's Kuma follow-up is retried at most this often
SYNC_S = 1800           # how often Kuma's real monitor list is read back
MAX_KEYS = 2000

_lock = threading.Lock()
_pending = set()        # keys whose Kuma monitor may be out of step
_want_sync = False      # the worker should read Kuma's monitor list first
_worker = None          # the running drain thread, or None (all three guarded by _lock)
_tried = {}             # key -> last Kuma attempt (reconcile's retry spacing)
_last_sync = 0.0


def is_monitored(reg):
    return bool((reg or {}).get("watch"))


def kuma_login():
    """(base, user, password) when Netwatch may manage Kuma monitors, else None."""
    ki = config.load()["integrations"]["kuma"]
    base = kuma.effective_base(ki)
    user = ki.get("username", "")
    pw = creds.get("@kuma").get("password", "")
    return (base, user, pw) if base and user and pw else None


def kuma_follows():
    return kuma_login() is not None


def _set_live(scanner, key, on):
    with scanner.lock:
        if key in scanner.devices:
            scanner.devices[key]["watch"] = on


def seed(scanner):
    """One-time upgrade step (see module doc). Returns how many devices it marked.
    A paused monitor is an operator's "stop" from this version, so it is skipped."""
    if not (config.load().get("monitoring") or {}).get("seed_pending"):
        return 0
    marked = []
    for key, reg in list(scanner.registry.items()):
        if isinstance(reg, dict) and not reg.get("watch") and not reg.get("kuma_paused") \
                and (reg.get("kuma_monitor_id") or reg.get("kuma_token")):
            reg["watch"] = True
            marked.append(key)
    if marked:
        scanner.save_registry()
        for key in marked:
            _set_live(scanner, key, True)
    config.update({"monitoring": {"seed_pending": False}})
    print(f"[monitoring] upgrade: {len(marked)} devices with a Kuma monitor are now monitored",
          flush=True)
    return len(marked)


def set_many(scanner, on=(), off=()):
    """Switch monitoring on for the keys in `on` and off for those in `off`.
    One registry write; Kuma follows in the background.
    Returns {changed: [{key, monitored}], unchanged: [...], unknown: [...], summary}."""
    on, off = list(dict.fromkeys(on or ())), list(dict.fromkeys(off or ()))
    with scanner.lock:
        known = set(scanner.registry) | set(scanner.devices)
    changed, unchanged, unknown = [], [], []
    dirty = False
    for key, want in [(k, True) for k in on] + [(k, False) for k in off]:
        if key not in known:
            unknown.append(key)
            continue
        reg = scanner.registry.setdefault(key, {})
        was = bool(reg.get("watch"))
        if reg.get("watch", None) is not want:  # always an explicit bool: see _out_of_step
            reg["watch"] = want
            dirty = True
        if was != want:
            changed.append({"key": key, "monitored": want})
        else:
            unchanged.append(key)
    if dirty:
        scanner.save_registry()
    if changed:
        for c in changed:
            _set_live(scanner, c["key"], c["monitored"])
        follow_kuma(scanner, [c["key"] for c in changed])
    return {"changed": changed, "unchanged": unchanged, "unknown": unknown,
            "summary": summary(scanner)}


def follow_kuma(scanner, keys, sync=False):
    """Queue keys (and/or a read-back of Kuma's list) for the worker; start it when idle."""
    global _worker, _want_sync
    with _lock:
        _pending.update(keys)
        _want_sync = _want_sync or sync
        if _worker is not None:
            return                      # the running worker picks them up
        _worker = threading.Thread(target=_drain, args=(scanner,), daemon=True,
                                   name="kuma-follow")
        _worker.start()


def _drain(scanner):
    global _worker, _want_sync
    while True:
        with _lock:
            keys, sync = set(_pending), _want_sync
            _pending.clear()
            _want_sync = False
            if not keys and not sync:
                _worker = None
                return
        try:
            if sync:
                keys |= _sync(scanner)
            if keys:
                _apply(scanner, keys)
        except Exception as e:  # noqa: BLE001 - the worker must survive a bad Kuma
            print("[monitoring] kuma follow error:", e, flush=True)


def _sync(scanner):
    """Put the registry in line with what Kuma really has: a monitor deleted in
    Kuma's own UI is forgotten, one paused or resumed there is recorded as such.
    Returns the keys this changed — their follow-up runs straight away."""
    login = kuma_login()
    states = kuma.monitor_states(*login) if login else None
    if states is None:
        return set()
    changed = set()
    for key, reg in list(scanner.registry.items()):
        mid = reg.get("kuma_monitor_id") if isinstance(reg, dict) else None
        if not mid:
            continue
        if mid not in states:
            reg["kuma_monitor_id"] = 0
            reg.pop("kuma_paused", None)
        elif bool(reg.get("kuma_paused")) == states[mid]:     # our note says the opposite
            if states[mid]:
                reg.pop("kuma_paused", None)
            else:
                reg["kuma_paused"] = True
        else:
            continue
        changed.add(key)
        _tried.pop(key, None)
    if changed:
        scanner.save_registry()
    return changed


def _out_of_step(scanner, key, reg):
    """What Kuma needs for this key: 'resume' | 'pause' | 'create' | None."""
    want = bool(reg.get("watch"))
    if reg.get("kuma_monitor_id"):
        if want and reg.get("kuma_paused"):
            return "resume"
        if "watch" in reg and not want and not reg.get("kuma_paused"):
            return "pause"
        return None
    if want and not reg.get("kuma_token"):
        with scanner.lock:
            dev = scanner.devices.get(key)
        if dev and dev.get("ip"):
            return "create"
    return None


def _still_there(scanner, reg):
    """The registry entry object is still in the registry (under any key — an
    IP-keyed device that gains a MAC keeps the same dict under its new key)."""
    return any(v is reg for v in list(scanner.registry.values()))


def _apply(scanner, keys):
    login = kuma_login()
    if not login:
        return                          # Kuma not set up here: nothing to follow
    base, user, pw = login
    now = time.time()
    toggles, creates = [], []
    for key in keys:
        reg = scanner.registry.get(key)
        if not isinstance(reg, dict):
            continue                    # forgotten meanwhile
        need = _out_of_step(scanner, key, reg)
        if need:
            _tried[key] = now
        if need in ("resume", "pause"):
            toggles.append((reg["kuma_monitor_id"], need == "resume", key, reg))
        elif need == "create":
            creates.append((key, reg))
    dirty = False
    if toggles:
        res = kuma.set_active_many(base, user, pw, [(mid, active) for mid, active, _k, _r in toggles])
        for mid, active, key, reg in toggles:
            r = res.get(mid) or {}
            if r.get("ok"):
                if active:
                    reg.pop("kuma_paused", None)
                else:
                    reg["kuma_paused"] = True
                dirty = True
            elif r.get("gone"):
                # deleted in Kuma's own UI: forget it; a monitored device gets a new one
                reg["kuma_monitor_id"] = 0
                reg.pop("kuma_paused", None)
                dirty = True
                if active and not reg.get("kuma_token"):
                    creates.append((key, reg))
            else:
                print(f"[monitoring] kuma {'resume' if active else 'pause'} #{mid} failed: "
                      f"{r.get('error')}", flush=True)
    if creates:
        regs = dict(creates)
        with scanner.lock:
            devs = {k: dict(scanner.devices[k]) for k in regs if k in scanner.devices}
        items = [(k, scanner._kuma_name(d), d["ip"], d.get("category"))
                 for k, d in devs.items() if d.get("ip")]
        res = kuma.provision_many(base, user, pw, items, 60) if items else {}
        for key, r in res.items():
            if not r.get("ok"):
                print(f"[monitoring] kuma create for {key} failed: {r.get('error')}", flush=True)
                continue
            reg = regs[key]
            if not _still_there(scanner, reg):
                # forgotten while its monitor was being made: don't leave an orphan
                try:
                    kuma.deprovision(base, user, pw, r["monitor_id"])
                except Exception as e:  # noqa: BLE001
                    print(f"[monitoring] kuma cleanup #{r['monitor_id']} failed: {e}", flush=True)
                continue
            # Stored even if monitoring was switched off meanwhile: that flip queued
            # the key again, and the next pass pauses this monitor.
            reg.update(kuma_monitor_id=r["monitor_id"], kuma_ip=devs[key]["ip"])
            reg.pop("kuma_paused", None)
            dirty = True
    if dirty:
        scanner.save_registry()


def reconcile(scanner):
    """After a scan: now and then have the worker read Kuma's real state, and
    queue devices whose monitor is out of step, each at most once per RETRY_S."""
    global _last_sync
    if not kuma_follows():
        return
    now = time.time()
    sync = now - _last_sync >= SYNC_S
    if sync:
        _last_sync = now
    due = [key for key, reg in list(scanner.registry.items())
           if isinstance(reg, dict) and now - _tried.get(key, 0) > RETRY_S
           and _out_of_step(scanner, key, reg)]
    if due or sync:
        follow_kuma(scanner, due, sync=sync)


def after_restore(scanner):
    """A restored registry may predate this version (seed it) and never matches
    Kuma exactly (a pause or resume made after the backup): read Kuma now."""
    global _last_sync
    seed(scanner)
    _tried.clear()
    _last_sync = time.time()
    if kuma_follows():
        follow_kuma(scanner, [], sync=True)


def summary(scanner):
    """Counts for the page header / hub: monitored, how many up and down."""
    with scanner.lock:
        live = {k: bool(d.get("online")) for k, d in scanner.devices.items()}
    mon = [k for k in live if is_monitored(scanner.registry.get(k))]
    up = sum(1 for k in mon if live[k])
    return {"monitored": len(mon), "online": up, "offline": len(mon) - up,
            "kuma_follows": kuma_follows()}
