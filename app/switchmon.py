"""Switch monitor: what is happening on every Ubiquiti EdgeSwitch / UISP switch.

A farm's cameras, radios and NVR all hang off one or two PoE switches. When a
cable corrodes, a camera's PoE fails or somebody unplugs the wrong thing, the
switch sees it first: the port renegotiates at 100 Mbps, errors climb, the PoE
draw drops to zero, the link goes down. This module reads each switch that has a
saved login over its own JSON API (edgeswitch.py) every few minutes, keeps the
readings in SQLite, logs what CHANGED as device events (so the History page and
the hub show "Port 5 went down — Gate camera"), raises problems and ntfy
alerts, and knows which device sits on which port.

Rules compare a port with its OWN history (history.switch_port_baseline): a port
that is always up and goes down matters; a laptop port going down does not.

It runs on its own thread with its own cadence (switch.poll_min, default 5 min) —
the scan interval (15 min) is too slow to catch a flapping port, and a switch
answers on the LAN in well under a second, so polling costs nothing on the
farm's internet link.
"""
import json
import os
import re
import subprocess
import threading
import time

import creds
import edgeswitch
import history
import notify

DATA_DIR = os.environ.get("NETWATCH_DATA", "/data")
STATE_PATH = os.path.join(DATA_DIR, "switchmon_state.json")
LAST_PATH = os.path.join(DATA_DIR, "switchmon_last.json")

REALERT_S = 6 * 3600
MIN_BASELINE = 12              # polls (~1 h at 5 min) before "usually up" is trusted
TICK_S = 30
NO_LOGIN_RECHECK_S = 6 * 3600

LIMITS = {
    # °C on the switch's own sensors. Bennie's ES-8-150W reads 67-69 °C on its
    # board sensors on a cool night, so the usual 70 °C would alarm every day.
    "temp":        {"warn": 80.0, "crit": 90.0},
    "cpu":         {"warn": 90.0, "crit": 98.0},     # %
    "ram":         {"warn": 90.0, "crit": 97.0},     # %
    "poe_budget":  {"warn": 80.0, "crit": 92.0},     # % of the PoE budget in use
    "errors_hour": {"warn": 100.0, "crit": 1000.0},  # errors + drops per hour on one port
    "flaps_6h":    {"warn": 4, "crit": 10},          # link down/up changes in 6 h
}
INFRA_CATEGORIES = {"camera", "nvr", "network", "internet-ap", "router", "solar", "alarm"}


def targets(devices, registry):
    """(key, dev, has_login) for every EdgeSwitch/UISP switch that is online."""
    have = creds.keys_with_creds()
    out = []
    for key, dev in devices.items():
        if not edgeswitch.is_edgeswitch(dev) or not dev.get("online"):
            continue
        if registry.get(key, {}).get("switch_monitor") is False:
            continue
        out.append((key, dev, key in have))
    return out


# ---- the ports that must never be switched off ----------------------------------
def pi_macs():
    """This Pi's own interface MACs (the container runs in the host's netns)."""
    out = set()
    try:
        for name in os.listdir("/sys/class/net"):
            if name == "lo" or name.startswith(("docker", "veth", "br-", "wg", "tailscale", "virbr")):
                continue
            try:
                with open(f"/sys/class/net/{name}/address") as f:
                    mac = edgeswitch.normalize_mac(f.read().strip())
                if mac and mac != "00:00:00:00:00:00":
                    out.add(mac)
            except OSError:
                pass
    except OSError:
        pass
    return out


def gateway_mac():
    """MAC of the default gateway, from the kernel's neighbour table."""
    try:
        out = subprocess.run(["ip", "route", "show", "default"], capture_output=True,
                             text=True, timeout=4).stdout
        m = re.search(r"via\s+([0-9.]+)", out)
        if not m:
            return None, None
        gw = m.group(1)
        with open("/proc/net/arp") as f:
            for line in f.read().splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 4 and parts[0] == gw:
                    return gw, edgeswitch.normalize_mac(parts[3])
        return gw, None
    except (OSError, subprocess.SubprocessError):
        return None, None


def protected_ports(snap, own_macs=None, gw_mac=None):
    """{port_id: reason} — ports whose loss would cut this site off. The Pi reads
    and controls the switch THROUGH these, so once one is off nobody can turn it
    back on from outside. Also flags obvious uplinks (many MACs behind one port)."""
    own = own_macs if own_macs is not None else pi_macs()
    if gw_mac is None:
        _, gw_mac = gateway_mac()
    out = {}
    for p in snap.get("ports") or []:
        macs = {m["mac"] for m in p.get("macs") or []}
        reasons = []
        if own & macs:
            reasons.append("the Netwatch Pi is connected through this port")
        if gw_mac and gw_mac in macs:
            reasons.append("the site's router / internet is connected through this port")
        if reasons:
            out[p["id"]] = "; ".join(reasons)
    return out


def uplink_ports(snap, min_macs=6):
    """Ports with many MAC addresses behind them — another switch or a wireless
    link. Not blocked, but switching one off takes everything behind it down."""
    return {p["id"]: len(p.get("macs") or []) for p in snap.get("ports") or []
            if len(p.get("macs") or []) >= min_macs}


# ---- monitor ---------------------------------------------------------------------
class SwitchMonitor:
    def __init__(self):
        self._lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._last_poll = {}
        self._last = {}          # key -> {ok, ts, ip, error, kind, snap}
        self._problems = []
        self._busy = False
        self._thread = None
        self._get_devices = None
        self._get_registry = None
        self.on_identity = None  # fn(key, name, model=)
        self.on_meta = None      # fn(key, model=, serial=, firmware=)
        self._state = self._load(STATE_PATH, {})
        for key, row in self._load(LAST_PATH, {}).items():
            if isinstance(row, dict):
                row["from_history"] = True
                self._last[key] = row

    @staticmethod
    def _load(path, default):
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    def _save(self, path, data):
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, separators=(",", ":"))
            os.replace(tmp, path)
        except OSError:
            pass

    # ---- loop ---------------------------------------------------------------------
    def start(self, get_devices, get_registry, get_config):
        self._get_devices, self._get_registry, self._get_config = get_devices, get_registry, get_config
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="switchmon", daemon=True)
        self._thread.start()

    def _loop(self):
        time.sleep(20)                     # let the first scan load the device list
        while True:
            try:
                self.poll_round()
            except Exception as e:  # noqa: BLE001 - monitoring must never die
                print("switchmon error:", e, flush=True)
            time.sleep(TICK_S)

    def _devices(self):
        return {d["key"]: d for d in (self._get_devices() or []) if d.get("key")}

    def poll_round(self, force=False, only=None):
        cfg = self._get_config()
        scfg = cfg.get("switch") or {}
        if not scfg.get("enabled", True) and not only:
            return []
        if not self._poll_lock.acquire(blocking=bool(only)):
            return self._problems
        self._busy = True
        try:
            gap = max(1, int(scfg.get("poll_min", 5))) * 60
            now = time.time()
            polled, found = set(), []
            current = targets(self._devices(), self._get_registry())
            if not only:
                # A switch that went offline (the uptime monitor's story) or was
                # opted out keeps no stale port problems.
                keep = {k for k, _, _ in current}
                with self._lock:
                    self._problems = [p for p in self._problems if p["key"] in keep]
            for key, dev, has_login in current:
                if only and key != only:
                    continue
                last = self._last.get(key) or {}
                if not has_login:
                    if force or now - (last.get("ts") or 0) > NO_LOGIN_RECHECK_S or last.get("kind") != "no_login":
                        self._no_login(key, dev)
                    polled.add(key)
                    found += self._evaluate_unreadable(key, dev)
                    continue
                if not (force or only) and now - self._last_poll.get(key, 0) < gap:
                    continue
                # A rejected login is not retried every poll — a switch that counts
                # failures could lock the account. Retry when the saved login
                # changes, or every few hours.
                if last.get("kind") == "auth_failed" and not only and not last.get("from_history") and \
                        last.get("cred_fp") == self._cred_fp(key) and now - (last.get("ts") or 0) < NO_LOGIN_RECHECK_S:
                    polled.add(key)
                    found += self._evaluate_unreadable(key, dev)
                    continue
                polled.add(key)
                found += self._poll_one(cfg, key, dev)
            if polled:
                with self._lock:
                    self._problems = [p for p in self._problems if p["key"] not in polled] + found
                if scfg.get("alerts", True):
                    for p in found:
                        if p["level"] in ("warn", "crit"):
                            self._fire(cfg, p)
                    self._clear_stale(cfg, polled, found)
                try:
                    history.switch_prune(int(scfg.get("history_days", 30)))
                except Exception:  # noqa: BLE001
                    pass
            return found
        finally:
            self._busy = False
            self._poll_lock.release()

    @staticmethod
    def _cred_fp(key):
        c = creds.get(key)
        return creds.fingerprint(c["username"], c["password"])

    def _no_login(self, key, dev):
        pub = edgeswitch.public_device(dev.get("ip"))
        self._last[key] = {"ok": False, "kind": "no_login", "ts": int(time.time()), "ip": dev.get("ip"),
                           "error": "no login saved for this switch",
                           "model": pub.get("model") or "", "product": pub.get("product") or ""}
        if pub.get("model") and self.on_meta:
            self.on_meta(key, model=pub["model"])

    def _poll_one(self, cfg, key, dev):
        self._last_poll[key] = time.time()
        c = creds.get(key)
        prev = (self._last.get(key) or {}).get("snap")
        try:
            snap = edgeswitch.read(dev.get("ip"), c["username"], c["password"])
        except Exception as e:  # noqa: BLE001
            snap = {"ok": False, "error": str(e), "kind": "error"}
        if not snap.get("ok"):
            old = self._last.get(key) or {}
            row = {"ok": False, "ts": int(time.time()), "ip": dev.get("ip"),
                   "error": snap.get("error"), "kind": snap.get("kind"), "snap": prev,
                   "cred_fp": creds.fingerprint(c["username"], c["password"]),
                   "failed_since": (old.get("failed_since") if not old.get("ok") else None) or int(time.time())}
            if snap.get("kind") == "auth_failed":
                pub = edgeswitch.public_device(dev.get("ip"))
                row["model"] = pub.get("model") or ""
            self._last[key] = row
            self._persist()
            return self._evaluate_unreadable(key, dev)

        own, (gw_ip, gw_mac) = pi_macs(), gateway_mac()
        snap["protected"] = protected_ports(snap, own, gw_mac)
        snap["uplinks"] = uplink_ports(snap)
        # What was last plugged into each port. The MAC table forgets a device a few
        # minutes after its link drops — exactly when "what was on port 5?" matters.
        memory = dict((prev or {}).get("port_memory") or {})
        for p in snap["ports"]:
            if p.get("macs"):
                memory[p["id"]] = {"macs": [m["mac"] for m in p["macs"]][:24], "ts": snap["ts"]}
        snap["port_memory"] = memory
        self._identity(key, snap)
        self._last[key] = {"ok": True, "ts": snap["ts"], "ip": dev.get("ip"), "snap": snap}
        self._record(key, snap)
        events = self._changes(key, dev, prev, snap)
        if events:
            try:
                history.log_events(events)
            except Exception as e:  # noqa: BLE001
                print("switchmon: could not log events:", e, flush=True)
        self._persist()
        return self._evaluate(key, dev, prev, snap)

    def _identity(self, key, snap):
        d = snap["device"]
        try:
            if self.on_meta:
                self.on_meta(key, model=d.get("model") or None, serial=d.get("serial") or None,
                             firmware=d.get("firmware") or None)
            if self.on_identity and d.get("name"):
                self.on_identity(key, d["name"], model=d.get("model"))
        except Exception as e:  # noqa: BLE001 - names are cosmetic
            print("switchmon: could not store identity:", e, flush=True)

    def _record(self, key, snap):
        h, s = snap.get("health") or {}, snap.get("summary") or {}
        sample = {"cpu": h.get("cpu"), "ram": h.get("ram"), "temp": h.get("temp"),
                  "poe_w": (snap.get("poe") or {}).get("used_w"), "ports_up": s.get("up"),
                  "rx_bps": s.get("rx_bps"), "tx_bps": s.get("tx_bps"),
                  "uptime": (snap.get("device") or {}).get("uptime")}
        ports = [{"port": p["id"], "up": 1 if p["up"] else 0, "speed": p.get("speed"),
                  "poe_w": p.get("poe_w"), "rx_bps": p.get("rx_bps"), "tx_bps": p.get("tx_bps"),
                  "errors": p.get("errors"), "dropped": p.get("dropped"),
                  "macs": len(p.get("macs") or [])} for p in snap.get("ports") or []]
        try:
            history.switch_record(key, sample, ports, ts=snap["ts"])
        except Exception as e:  # noqa: BLE001
            print("switchmon: could not store sample:", e, flush=True)

    def _persist(self):
        self._save(LAST_PATH, {k: {kk: vv for kk, vv in v.items() if kk != "from_history"}
                               for k, v in list(self._last.items())})

    # ---- what changed -> device events --------------------------------------------
    def _who(self, port, devices_by_mac):
        names = []
        for m in port.get("macs") or []:
            d = devices_by_mac.get(m["mac"])
            if d:
                names.append(d.get("name") or d.get("device_name") or d.get("model") or d.get("ip") or m["mac"])
        return names

    def _changes(self, key, dev, prev, snap):
        if not prev or not prev.get("ports"):
            return []
        by_mac = {edgeswitch.normalize_mac(d.get("mac")): d for d in self._devices().values() if d.get("mac")}
        before = {p["id"]: p for p in prev["ports"]}
        named = {**dev, "name": dev.get("name") or dev.get("device_name") or (snap.get("device") or {}).get("name") or ""}
        rows = []

        def ev(what, port=None, **extra):
            detail = {"switch_event": what, **extra}
            if port:
                detail.update({"port": port["id"], "port_name": port.get("name") or ""})
            rows.append(history.build_event("switch", named, detail))

        pu, su = (prev.get("device") or {}).get("uptime"), (snap.get("device") or {}).get("uptime")
        if pu and su is not None and su + 120 < pu:
            ev("rebooted", uptime_s=su)
        prev_where = {}
        for p in prev["ports"]:
            for m in p.get("macs") or []:
                prev_where[m["mac"]] = p["id"]
        for p in snap["ports"]:
            b = before.get(p["id"])
            if not b:
                continue
            who = self._who(p if p["up"] else b, by_mac)
            if b["enabled"] != p["enabled"]:
                ev("port_enabled" if p["enabled"] else "port_disabled", p, devices=who)
            elif b["up"] and not p["up"]:
                ev("link_down", p, devices=self._who(b, by_mac), speed=b.get("speed"))
            elif p["up"] and not b["up"]:
                ev("link_up", p, devices=who, speed=p.get("speed"))
            elif p["up"] and b["up"] and b.get("speed") and p.get("speed") and b["speed"] != p["speed"]:
                ev("speed_change", p, devices=who, speed=p["speed"], was=b["speed"])
            if (b.get("poe_mode") or "") != (p.get("poe_mode") or "") and b.get("poe_mode") is not None:
                ev("poe_mode", p, devices=who, mode=p.get("poe_mode"), was=b.get("poe_mode"))
            elif (b.get("poe_w") or 0) >= 1.0 and (p.get("poe_w") or 0) < 0.5 and p.get("poe_mode") not in ("", "off"):
                ev("poe_lost", p, devices=self._who(b, by_mac), was_w=b.get("poe_w"))
            if b.get("name") != p.get("name") and b.get("name") is not None:
                ev("port_renamed", p, was=b.get("name"))
            for m in p.get("macs") or []:
                old = prev_where.get(m["mac"])
                d = by_mac.get(m["mac"])
                if old and old != p["id"] and d:
                    ev("device_moved", p, devices=[d.get("name") or d.get("ip") or m["mac"]], mac=m["mac"], was_port=old)
        return rows

    # ---- rules ----------------------------------------------------------------------
    def _add(self, out, key, dev, metric, level, what, hint, port=None, value=None):
        out.append({"key": key, "ip": dev.get("ip"), "device": dev.get("name") or dev.get("device_name") or dev.get("ip") or key,
                    "metric": metric, "level": level, "what": what, "hint": hint,
                    "port": port["id"] if port else None, "port_name": (port or {}).get("name") or "",
                    "value": value, "ts": int(time.time())})

    def _evaluate_unreadable(self, key, dev):
        last = self._last.get(key) or {}
        out = []
        kind = last.get("kind")
        if kind == "no_login":
            self._add(out, key, dev, "switch_login", "info", "No login saved — the switch's ports, PoE and traffic can't be read",
                      "Save the switch's own web login on this device's Access tab.")
        elif kind == "auth_failed":
            self._add(out, key, dev, "switch_login", "warn", "The switch rejected the saved login",
                      "Save the switch's own web username and password on the Access tab (the login used on its web page).")
        elif kind:
            since = last.get("failed_since") or time.time()
            if time.time() - since > 900:
                self._add(out, key, dev, "switch_unreadable", "warn",
                          f"Can't read the switch ({last.get('error') or 'no answer'})",
                          "The switch answers ping but not its management API. Check that its web server is on.")
        return out

    def _evaluate(self, key, dev, prev, snap):
        out = []
        add = lambda *a, **k: self._add(out, key, dev, *a, **k)  # noqa: E731
        h = snap.get("health") or {}
        for metric, label, unit in (("temp", "temperature", "°C"), ("cpu", "CPU", "%"), ("ram", "memory", "%")):
            v = h.get(metric)
            if v is None:
                continue
            lvl = "crit" if v >= LIMITS[metric]["crit"] else "warn" if v >= LIMITS[metric]["warn"] else None
            if lvl:
                add(metric, lvl, f"Switch {label} {v:.0f}{unit}",
                    "Check the cabinet's ventilation and that the fan (if any) turns — heat kills PoE switches."
                    if metric == "temp" else "The switch is overloaded — a broadcast storm or a loop is the usual cause.",
                    value=v)
        poe = snap.get("poe") or {}
        if poe.get("pct") is not None:
            lvl = "crit" if poe["pct"] >= LIMITS["poe_budget"]["crit"] else "warn" if poe["pct"] >= LIMITS["poe_budget"]["warn"] else None
            if lvl:
                add("poe_budget", lvl, f"PoE budget {poe['pct']}% used ({poe['used_w']} of {poe['budget_w']} W)",
                    "Near the limit the switch cuts power to the lowest-priority port. Move a camera or radio to another PoE source.",
                    value=poe["pct"])
        before = {p["id"]: p for p in (prev or {}).get("ports") or []}
        gap_h = max(1 / 60, (snap["ts"] - (prev or {}).get("ts", snap["ts"] - 300)) / 3600) if prev else None
        port_rows = history.switch_port_series(key, window_s=6 * 3600)
        flaps = {}
        last_up = {}
        for r in port_rows:
            if r["port"] in last_up and last_up[r["port"]] != r["up"]:
                flaps[r["port"]] = flaps.get(r["port"], 0) + 1
            last_up[r["port"]] = r["up"]
        for p in snap.get("ports") or []:
            base = history.switch_port_baseline(key, p["id"])
            label = f"Port {p['id'].split('/')[-1]}" + (f" ({p['name']})" if p.get("name") and not re.match(r"^port \d+$", p["name"], re.I) else "")
            trusted = base.get("n", 0) >= MIN_BASELINE
            if p["enabled"] and not p["up"] and trusted and (base.get("up_share") or 0) >= 0.9:
                add("port_down", "crit" if (p.get("id") in self._infra_ports(key)) else "warn",
                    f"{label} has no link — it is normally always connected",
                    "The device on this port is off, its cable is cut/unplugged or the PoE injector failed. "
                    "Check the device first, then the cable and connectors.", port=p)
            if p["up"] and trusted and base.get("speed") and p.get("speed") and p["speed"] < base["speed"]:
                add("speed_drop", "warn", f"{label} linked at {p['speed']} Mbps, normally {base['speed']} Mbps",
                    "A link that drops to a lower speed has a damaged cable, a wet or corroded connector, or a pair "
                    "broken inside the cable. Re-crimp or replace the cable.", port=p, value=p["speed"])
            if p["up"] and p.get("duplex") == "half":
                add("half_duplex", "warn", f"{label} is running half duplex",
                    "Half duplex means one side is forced to a fixed speed or the cable is bad. Set both ends to auto.", port=p)
            if (p.get("poe_mode") or "off") != "off" and trusted and (base.get("poe_share") or 0) >= 0.8 \
                    and (base.get("poe_w") or 0) >= 1 and (p.get("poe_w") or 0) < 0.5:
                add("poe_lost", "crit", f"{label} is powering nothing — it normally draws {base['poe_w']:.1f} W",
                    "The PoE device on this port has died or its cable is broken. Try a PoE power cycle from here "
                    "first; if it stays at 0 W someone must check the device and cable.", port=p)
            b = before.get(p["id"])
            if b and gap_h:
                cur = (p.get("errors") or 0) + (p.get("dropped") or 0)
                old = (b.get("errors") or 0) + (b.get("dropped") or 0)
                if cur >= old:          # counters reset on reboot -> skip that poll
                    rate = (cur - old) / gap_h
                    lvl = "crit" if rate >= LIMITS["errors_hour"]["crit"] else "warn" if rate >= LIMITS["errors_hour"]["warn"] else None
                    if lvl and p["up"]:
                        add("port_errors", lvl, f"{label}: {rate:.0f} errors/drops per hour",
                            "Errors on a port point at a bad cable, a failing connector, electrical interference "
                            "(cable next to power lines) or a duplex mismatch.", port=p, value=round(rate))
            n = flaps.get(p["id"], 0)
            if n >= LIMITS["flaps_6h"]["warn"]:
                add("port_flapping", "crit" if n >= LIMITS["flaps_6h"]["crit"] else "warn",
                    f"{label} went down and up {n} times in 6 hours",
                    "A link that keeps dropping is a loose connector, a failing cable, or a device rebooting on "
                    "low PoE power. Check the connector and the device's power draw.", port=p, value=n)
        svc = snap.get("services") or {}
        if svc.get("telnet"):
            add("telnet_on", "info", "Telnet is switched on (passwords cross the network in clear text)",
                "Turn Telnet off on the switch; SSH and the web page do the same job securely.")
        if svc.get("snmp") and (svc.get("snmp_community") or "").lower() in ("public", "private"):
            add("snmp_default", "info", f"SNMP uses the default community '{svc['snmp_community']}'",
                "Change the SNMP community or turn SNMP off.")
        return out

    def _infra_ports(self, key):
        """Ports whose last known devices include a camera/NVR/radio/router."""
        snap = (self._last.get(key) or {}).get("snap") or {}
        by_mac = {edgeswitch.normalize_mac(d.get("mac")): d for d in self._devices().values() if d.get("mac")}
        out = set()
        for pid, mem in (snap.get("port_memory") or {}).items():
            for mac in mem.get("macs") or []:
                d = by_mac.get(mac)
                if d and (d.get("watch") or d.get("category") in INFRA_CATEGORIES):
                    out.add(pid)
        return out

    # ---- alerting ---------------------------------------------------------------------
    @staticmethod
    def _sid(p):
        return "|".join(x for x in (p["key"], p["metric"], p.get("port") or "") if x)

    def _fire(self, cfg, p):
        sid = self._sid(p)
        st = self._state.get(sid, {"level": "ok", "ts": 0})
        now = time.time()
        escalated = p["level"] == "crit" and st["level"] == "warn"
        if p["level"] == st["level"] and now - st["ts"] < REALERT_S and not escalated:
            return
        notify.push(cfg.get("alerts") or {}, f"Switch: {p['device']}", f"{p['what']}\n\n{p['hint']}",
                    priority="high" if p["level"] == "crit" else "default",
                    tags=["electric_plug" if p["level"] == "warn" else "warning"])
        self._state[sid] = {"level": p["level"], "ts": now, "what": p["what"]}
        self._save(STATE_PATH, self._state)

    def _clear_stale(self, cfg, polled, found):
        live = {self._sid(p) for p in found if p["level"] in ("warn", "crit")}
        changed = False
        for sid, st in list(self._state.items()):
            if sid.split("|")[0] not in polled or sid in live or st["level"] == "ok":
                continue
            notify.push(cfg.get("alerts") or {}, "Switch recovered",
                        f"Back to normal: {st.get('what') or sid}", priority="low", tags=["white_check_mark"])
            self._state[sid] = {"level": "ok", "ts": time.time()}
            changed = True
        if changed:
            self._save(STATE_PATH, self._state)

    # ---- api --------------------------------------------------------------------------
    def snapshot(self):
        return {"switches": dict(self._last), "problems": list(self._problems), "busy": self._busy}

    def mac_map(self):
        """{mac: {switch_key, port, port_name, uplink}} — where each device plugs in.
        A MAC seen on an uplink port AND on a lower-count port is placed on the
        one with the fewest MACs (its real edge port)."""
        best = {}
        for key, row in list(self._last.items()):     # the poll thread may add a switch meanwhile
            snap = row.get("snap") or {}
            ups = snap.get("uplinks") or {}
            for p in snap.get("ports") or []:
                count = len(p.get("macs") or [])
                for m in p.get("macs") or []:
                    cur = best.get(m["mac"])
                    if cur is None or count < cur["_count"]:
                        best[m["mac"]] = {"switch_key": key, "port": p["id"], "port_name": p.get("name") or "",
                                          "uplink": p["id"] in ups, "_count": count, "ts": snap.get("ts")}
        for v in best.values():
            v.pop("_count", None)
        return best

    def forget(self, key):
        self._last.pop(key, None)
        self._persist()


monitor = SwitchMonitor()
