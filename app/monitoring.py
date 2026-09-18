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

Every switch is written to the device's history (event type "monitoring",
detail {monitored, by}) so "who stopped watching the gate camera?" has an answer.

Monitors Netwatch creates carry the description "netwatch:<device key>". Kuma
answers an add only after re-sending its whole monitor list, so on a busy Pi a
reply can be lost although the monitor exists; the marker lets the next pass
adopt that monitor instead of making a second one. unowned()/remove_unowned()
back the Settings "Tidy Kuma" list: monitors no device or internet check
accounts for, with Netwatch's own leftovers flagged.

A device's monitor carries the device's name (Scanner._kuma_name) — with its
address in brackets when the monitor is named that older way (kuma.styled_name).
The registry keeps `kuma_name`, the device name Netwatch last gave it; when the
device's name in Netwatch no longer matches, the read-back renames the monitor — at once after a
rename through the device API (the site page; name_changed), otherwise within
SYNC_S. A name given in Kuma's own UI stays until the device is renamed in
Netwatch. Monitors from before this have no `kuma_name` and are brought in line
once. A hand-made push monitor (a push token, no monitor id) and any
non-ping monitor are never renamed.
"""
import threading
import time

import config
import creds
import history
import kuma

RETRY_S = 1800          # a device's Kuma follow-up is retried at most this often
SYNC_S = 1800           # how often Kuma's real monitor list is read back
RENAME_BUDGET_S = 120   # renaming per pass stops after this; the rest waits for the next pass
MAX_KEYS = 2000

_lock = threading.Lock()
_pending = set()        # keys whose Kuma monitor may be out of step
_want_sync = False      # the worker should read Kuma's monitor list first
_rename_first = set()   # devices just renamed: their monitor is renamed before any other
_worker = None          # the running drain thread, or None (all four guarded by _lock)
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


def _log(scanner, changes, by):
    """History rows for [(key, monitored)] — best-effort, never blocks a switch."""
    rows = []
    for key, on in changes:
        with scanner.lock:
            live = dict(scanner.devices.get(key) or {})
        reg = scanner.registry.get(key) or {}
        dev = {"key": key, "ip": live.get("ip"), "mac": live.get("mac"),
               "name": live.get("name") or reg.get("name"), "category": live.get("category"),
               "vendor": live.get("vendor"), "hostname": live.get("hostname")}
        rows.append(history.build_event("monitoring", dev, {"monitored": bool(on), "by": by}))
    try:
        history.log_events(rows)
    except Exception as e:  # noqa: BLE001
        print("[monitoring] history write failed:", e, flush=True)
    if changes:
        on = sum(1 for _k, v in changes if v)
        print(f"[monitoring] by {by}: {on} on, {len(changes) - on} off", flush=True)


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
        _log(scanner, [(k, True) for k in marked], "upgrade (had a Kuma monitor)")
    config.update({"monitoring": {"seed_pending": False}})
    print(f"[monitoring] upgrade: {len(marked)} devices with a Kuma monitor are now monitored",
          flush=True)
    return len(marked)


def set_many(scanner, on=(), off=(), by=""):
    """Switch monitoring on for the keys in `on` and off for those in `off`
    (`by` = who, for the history). One registry write; Kuma follows in the background.
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
        _log(scanner, [(c["key"], c["monitored"]) for c in changed], by or "unknown")
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


def monitor_name(scanner, key):
    """The name the device's Kuma monitor should carry (None: device unknown)."""
    with scanner.lock:
        dev = dict(scanner.devices.get(key) or {})
    return scanner._kuma_name(dev) if dev else None


def name_changed(scanner, key):
    """The operator renamed a device (or changed the type its name falls back
    to): rename its Kuma monitor now instead of at the next read-back."""
    if not (scanner.registry.get(key) or {}).get("kuma_monitor_id") or not kuma_follows():
        return
    with _lock:
        _rename_first.add(key)
    follow_kuma(scanner, [], sync=True)


def _drain(scanner):
    global _worker, _want_sync
    while True:
        with _lock:
            keys, sync, first = set(_pending), _want_sync, set(_rename_first)
            _pending.clear()
            _rename_first.clear()
            _want_sync = False
            if not keys and not sync:
                _worker = None
                return
        try:
            renames = []
            if sync:
                keys |= _sync(scanner, renames)
            if keys:
                _apply(scanner, keys)
            if renames:                 # after the on/off work: a name can wait, a switch shouldn't
                _rename(scanner, renames, first)
        except Exception as e:  # noqa: BLE001 - the worker must survive a bad Kuma
            print("[monitoring] kuma follow error:", e, flush=True)


def _owned_ids(scanner):
    """Kuma monitor ids Netwatch accounts for: each device's monitor and the
    internet checks (registry "__internet__")."""
    ids = set()
    for reg in list(scanner.registry.values()):
        if isinstance(reg, dict) and reg.get("kuma_monitor_id"):
            ids.add(reg["kuma_monitor_id"])
    net = scanner.registry.get("__internet__") or {}
    ids |= {v for v in (net.get("monitors") or {}).values() if v}
    return ids


def _marked_key(mon):
    desc = mon.get("description") or ""
    return desc[len(kuma.MARKER):] if desc.startswith(kuma.MARKER) else None


def _adopt(reg, mon):
    reg["kuma_monitor_id"] = mon["id"]
    reg["kuma_ip"] = mon.get("hostname") or reg.get("kuma_ip", "")
    reg["kuma_name"] = mon.get("name") or ""      # the name Netwatch gave it when it was made
    if mon.get("active"):
        reg.pop("kuma_paused", None)
    else:
        reg["kuma_paused"] = True


def _sync(scanner, renames=None):
    """Put the registry in line with what Kuma really has: a monitor deleted in
    Kuma's own UI is forgotten, one paused or resumed there is recorded as such,
    and a monitor Netwatch made whose reply was lost is adopted. With a
    `renames` list, the monitors whose device was renamed are appended to it
    (see _plan_renames). Returns the keys this changed — their follow-up runs
    straight away."""
    login = kuma_login()
    mons = kuma.monitor_list(*login) if login else None
    if mons is None:
        return set()
    states = {m["id"]: m["active"] for m in mons}
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
    owned = _owned_ids(scanner)
    for mon in mons:
        key = _marked_key(mon)
        reg = scanner.registry.get(key) if key and mon["id"] not in owned else None
        if isinstance(reg, dict) and not reg.get("kuma_monitor_id") and not reg.get("kuma_token"):
            _adopt(reg, mon)
            owned.add(mon["id"])
            changed.add(key)
            _tried.pop(key, None)
    noted = renames is not None and _plan_renames(scanner, mons, renames)
    if changed or noted:
        scanner.save_registry()
    return changed


def _plan_renames(scanner, mons, out):
    """Append (key, monitor_id, name, label) for each device monitor to rename:
    the device's name in Netwatch (`label`) changed since Netwatch last named the
    monitor (or it never did, before this version) and the monitor doesn't carry
    it yet. `name` is the label in the monitor's own style (kuma.styled_name).
    A monitor that already carries it just has the label noted. Returns True
    when a note was made (the registry needs saving)."""
    by_id = {m["id"]: m for m in mons}
    with scanner.lock:
        devs = {k: dict(d) for k, d in scanner.devices.items()}
    noted = False
    for key, reg in list(scanner.registry.items()):
        if not isinstance(reg, dict):
            continue
        # Only Netwatch's own ping monitors (a monitor id). A hand-made push monitor
        # is a token WITHOUT an id; a token next to an id is a leftover from before
        # "Fix monitors -> ping" and changes nothing.
        mon, dev = by_id.get(reg.get("kuma_monitor_id")), devs.get(key)
        if not mon or mon.get("type") != "ping" or not dev:
            continue
        label = scanner._kuma_name(dev)
        if reg.get("kuma_name") == label:
            continue                    # not renamed in Netwatch: a name given in Kuma stays
        name = kuma.styled_name(label, mon)
        if mon.get("name") == name:
            reg["kuma_name"] = label
            noted = True
        else:
            out.append((key, mon["id"], name, label))
    return noted


def _rename(scanner, plan, first=()):
    """Rename the planned monitors, devices just renamed first, for at most
    RENAME_BUDGET_S; what is left is planned again on the next read-back."""
    login = kuma_login()
    if not login:
        return
    plan = sorted(plan, key=lambda p: p[0] not in first)
    res = kuma.rename_many(*login, [(mid, name) for _k, mid, name, _l in plan], budget_s=RENAME_BUDGET_S)
    done = 0
    for key, mid, name, label in plan:
        r = res.get(mid)
        if r is None:
            continue                    # not reached this pass
        if not r.get("ok"):
            print(f"[monitoring] kuma rename #{mid} to {name!r} failed: {r.get('error')}", flush=True)
            continue
        reg = scanner.registry.get(key)
        if isinstance(reg, dict) and reg.get("kuma_monitor_id") == mid:
            reg["kuma_name"] = label
            done += 1
    if done:
        scanner.save_registry()
        left = len(plan) - len(res)
        print(f"[monitoring] renamed {done} Kuma monitor(s) after their device"
              + (f"; {left} left for the next pass" if left else ""), flush=True)


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


def _current_entry(scanner, key, reg):
    """Where a Kuma result belongs once the call is back: the entry under `key`
    now (a restore replaces the registry wholesale), else the same dict under a
    new key (an IP-keyed device that gained a MAC). None = the device was forgotten."""
    cur = scanner.registry.get(key)
    if isinstance(cur, dict):
        return cur
    return next((v for v in list(scanner.registry.values()) if v is reg), None)


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
            reg = _current_entry(scanner, key, reg)
            if reg is None:
                continue                # forgotten meanwhile (Forget deletes the monitor)
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
        # a monitor made earlier whose reply was lost carries our marker: adopt it
        owned = _owned_ids(scanner)
        marked = {}
        for mon in kuma.monitor_list(base, user, pw) or []:
            key = _marked_key(mon)
            if key in devs and mon["id"] not in owned:
                marked.setdefault(key, mon)
        res = {k: {"ok": True, "monitor_id": m["id"], "adopt": m} for k, m in marked.items()}
        names = {k: scanner._kuma_name(d) for k, d in devs.items()}
        items = [(k, names[k], d["ip"], d.get("category"), kuma.marker(k))
                 for k, d in devs.items() if d.get("ip") and k not in marked]
        res.update(kuma.provision_many(base, user, pw, items, 60) if items else {})
        for key, r in res.items():
            if not r.get("ok"):
                print(f"[monitoring] kuma create for {key} failed: {r.get('error')}", flush=True)
                continue
            reg = _current_entry(scanner, key, regs[key])
            if reg is None:
                # forgotten while its monitor was being made: don't leave an orphan
                try:
                    kuma.deprovision(base, user, pw, r["monitor_id"])
                except Exception as e:  # noqa: BLE001
                    print(f"[monitoring] kuma cleanup #{r['monitor_id']} failed: {e}", flush=True)
                continue
            # Stored even if monitoring was switched off meanwhile: that flip queued
            # the key again, and the next pass pauses this monitor.
            if r.get("adopt"):
                _adopt(reg, r["adopt"])
                if _out_of_step(scanner, key, reg):
                    follow_kuma(scanner, [key])     # e.g. an adopted monitor that is paused
            else:
                reg.update(kuma_monitor_id=r["monitor_id"], kuma_ip=devs[key]["ip"],
                           kuma_name=names[key])
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


_INTERNET_NAMES = ("Gateway", "Internet", "DNS ")


def _dev_brief(key, reg, dev):
    return {"key": key, "ip": dev.get("ip") or "",
            "name": reg.get("name") or dev.get("name") or dev.get("device_name")
            or dev.get("type") or dev.get("vendor") or key}


def unowned(scanner):
    """Kuma monitors that no device and no internet check accounts for (Settings
    "Tidy Kuma"), each with `leftover` = looks like Netwatch's own spare copy:
      * marked for a device that has since been forgotten, or that uses another
        monitor; or
      * a ping of a device that already has its own monitor, under one of that
        device's own labels (a copy made before monitors were marked).
    Internet checks, push monitors and anything else are listed, never flagged.
    None when Kuma can't be read."""
    login = kuma_login()
    mons = kuma.monitor_list(*login) if login else None
    if mons is None:
        return None
    owned = _owned_ids(scanner)
    with scanner.lock:
        devs = {k: dict(d) for k, d in scanner.devices.items()}
    by_ip = {}
    for k, d in devs.items():
        if d.get("ip"):
            by_ip.setdefault(d["ip"], []).append(k)
    out = []
    for mon in mons:
        if mon["id"] in owned:
            continue
        row = dict(mon, device=None, leftover=False, why="")
        key = _marked_key(mon)
        if key is not None:
            reg = scanner.registry.get(key)
            if not isinstance(reg, dict):
                row.update(leftover=True, why="made by Netwatch for a device that has since been forgotten",
                           device={"key": key, "ip": "", "name": key})
            else:
                row["device"] = _dev_brief(key, reg, devs.get(key) or {})
                if reg.get("kuma_monitor_id"):
                    row.update(leftover=True, why=f"a spare copy — this device uses monitor #{reg['kuma_monitor_id']}")
                else:
                    row["why"] = "made by Netwatch; it is linked to its device on the next check"
        elif mon["type"] == "push":
            row["why"] = "a push monitor (set up by hand)"
        elif mon["name"].startswith(_INTERNET_NAMES):
            row["why"] = "looks like an internet check"
        elif mon["type"] == "ping" and mon["hostname"] in by_ip:
            for k in by_ip[mon["hostname"]]:
                reg, dev = scanner.registry.get(k) or {}, devs[k]
                row["device"] = _dev_brief(k, reg, dev)
                labels = {x for x in (reg.get("name"), dev.get("name"), dev.get("device_name"),
                                      dev.get("type"), dev.get("vendor"), dev.get("model")) if x}
                if reg.get("kuma_monitor_id") and mon["name"] in labels:
                    row.update(leftover=True, why=f"a spare copy — this device uses monitor #{reg['kuma_monitor_id']}")
                    break
            if not row["why"]:
                row["why"] = "pings a device Netwatch knows, but was not made for it"
        else:
            row["why"] = "not made by Netwatch"
        out.append(row)
    return out


def remove_unowned(scanner, ids):
    """Delete the given Kuma monitors — only those that still belong to nothing
    when checked again right now. Returns {ok, removed, skipped}."""
    login = kuma_login()
    if not login:
        return {"ok": False, "error": "Uptime Kuma is not set up on this Pi"}
    mons = kuma.monitor_list(*login)
    if mons is None:
        return {"ok": False, "error": "could not read Uptime Kuma"}
    owned, present = _owned_ids(scanner), {m["id"] for m in mons}
    removed, skipped = [], []
    for mid in ids:
        if mid in owned or mid not in present:
            skipped.append(mid)
            continue
        r = kuma.deprovision(*login, mid)
        (removed if r.get("ok") else skipped).append(mid)
    if removed:
        print(f"[monitoring] removed Kuma monitors no device owned: {removed}", flush=True)
    return {"ok": True, "removed": removed, "skipped": skipped}


def summary(scanner):
    """Counts for the page header / hub: monitored, how many up and down."""
    with scanner.lock:
        live = {k: bool(d.get("online")) for k, d in scanner.devices.items()}
    mon = [k for k in live if is_monitored(scanner.registry.get(k))]
    up = sum(1 for k in mon if live[k])
    return {"monitored": len(mon), "online": up, "offline": len(mon) - up,
            "kuma_follows": kuma_follows()}
