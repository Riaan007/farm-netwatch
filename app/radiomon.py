"""Wireless telemetry, trending and early warning for Ubiquiti radios.

A farm's cameras hang off wireless backhaul, so the link degrades long before
anything goes offline: a dish drifts in the wind, a neighbour lights up the same
channel, a mast sags. Uptime monitoring cannot see any of that — by the time a
device pings badly the link has already failed. This module polls each
credentialed radio on its own cadence (read-only, the same SSH path as the Wi-Fi
info button), stores what it finds in SQLite, and compares it against that
radio's OWN recent history rather than absolute numbers, because a link that
lives at -70 dBm is fine while one that fell from -55 to -65 is not.

Every alert carries a `hint` — what an installer would actually go check. Those
hints are also what the hub's AI analysis is grounded in, so they are written to
be useful with or without it.

State (last alert level per metric) lives in /data/radiomon_state.json; the
samples live in netwatch.db via history.radio_*.
"""
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import airos
import creds
import edgeswitch
import history
import notify

DATA_DIR = os.environ.get("NETWATCH_DATA", "/data")
STATE_PATH = os.path.join(DATA_DIR, "radiomon_state.json")

REALERT_S = 6 * 3600        # don't repeat the same alert inside this window
MAX_WORKERS = 4             # SSH is slow; a pole of radios shouldn't take minutes
MIN_BASELINE = 8            # samples needed before trend rules are trusted

# Absolute floors — these are bad whatever the history says.
ABS = {
    "signal":   {"warn": -75.0, "crit": -82.0},   # dBm, lower is worse
    "airtime":  {"warn": 80.0,  "crit": 92.0},    # % of air time used
    "score":    {"warn": 50.0,  "crit": 35.0},    # airMAX link score, lower is worse
    "chain_gap": {"warn": 8.0,  "crit": 12.0},    # dB between antenna chains
}
# Change-vs-baseline thresholds — the early warnings.
DELTA = {
    "signal_drop":   {"warn": 6.0,  "crit": 10.0},   # dB worse than usual
    "noise_rise":    {"warn": 8.0,  "crit": 12.0},   # dB noisier than usual
    "score_drop":    {"warn": 20.0, "crit": 35.0},   # link-score points lost
    "capacity_drop": {"warn": 0.60, "crit": 0.40},   # fraction of usual capacity
}


def _f(v):
    """Numbers arrive from firmware as strings with units ('5480 MHz', '59.2%')."""
    if v is None or v == "":
        return None
    try:
        return float(str(v).strip().split()[0].rstrip("%"))
    except (ValueError, IndexError):
        return None


def is_radio(dev):
    """Ubiquiti gear we can read over SSH. Cameras answer to Hikvision instead;
    EdgeSwitch/UISP switches are read over their own API by switchmon."""
    vendor = (dev.get("vendor") or "").lower()
    return ("ubiquiti" in vendor or "ubnt" in vendor) and not edgeswitch.is_edgeswitch(dev)


def targets(devices, registry):
    """Radios worth polling: Ubiquiti, online, with a saved login, not opted out."""
    have = creds.keys_with_creds()
    out = []
    for key, dev in devices.items():
        if not is_radio(dev) or not dev.get("online") or key not in have:
            continue
        if registry.get(key, {}).get("radio_monitor") is False:
            continue
        out.append((key, dev))
    return out


def _sample_from(res):
    """Flatten a get_wifi() result into the columns history.radio_record expects."""
    return {
        "ip": res.get("ip"), "mode": res.get("mode"), "ssid": res.get("ssid"),
        "freq": res.get("frequency"), "chanbw": res.get("channelWidth"),
        "signal": _f(res.get("signal")), "noise": _f(res.get("noise")),
        "chain0": _f(res.get("chain0")), "chain1": _f(res.get("chain1")),
        "airtime": _f(res.get("airtime")),
        "cap_dl": _f(res.get("capacityDown")), "cap_ul": _f(res.get("capacityUp")),
        "tx_rate": _f(res.get("txRate")), "rx_rate": _f(res.get("rxRate")),
        "links": len(res.get("stations") or []),
    }


def _links_from(res):
    out = []
    for s in res.get("stations") or []:
        peer = (s.get("mac") or "").upper()
        if not peer:
            continue
        out.append({
            "peer": peer, "name": s.get("name") or "", "ip": s.get("ip") or "",
            "model": s.get("model") or "",
            "signal": _f(s.get("signal")), "remote_signal": _f(s.get("remoteSignal")),
            "score_dl": _f(s.get("scoreDown")), "score_ul": _f(s.get("scoreUp")),
            "tx": _f(s.get("tx")), "rx": _f(s.get("rx")),
            "latency": _f(s.get("latency")), "distance": _f(s.get("distance")),
        })
    return out


class RadioMonitor:
    def __init__(self):
        self._lock = threading.Lock()
        self._last_poll = {}        # key -> ts of last successful/attempted poll
        self._last = {}             # key -> last result summary (for the API)
        self._problems = []         # current problems, rebuilt every poll round
        self._busy = False
        self._warmed = False
        self.on_identity = None     # fn(key, name, model=) — set by the scanner
        try:
            with open(STATE_PATH) as f:
                self._state = json.load(f)
        except (OSError, ValueError):
            self._state = {}        # "key|metric[|peer]" -> {"level","ts"}

    # ---- polling ----------------------------------------------------------
    def poll_round(self, cfg, devices, registry, force=False):
        """Poll every due radio. Called after a scan; returns the problems found."""
        rcfg = cfg.get("radio") or {}
        if not rcfg.get("enabled", True):
            return []
        with self._lock:
            if self._busy:
                return self._problems      # a slow round must not stack up
            self._busy = True
        try:
            due = []
            gap = max(1, int(rcfg.get("poll_min", 15))) * 60
            now = time.time()
            for key, dev in targets(devices, registry):
                if force or now - self._last_poll.get(key, 0) >= gap:
                    due.append((key, dev))
            if not due:
                return self._problems
            problems = []
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                for found in pool.map(lambda kd: self._poll_one(cfg, *kd), due):
                    problems += found
            # Keep problems for radios we didn't poll this round.
            polled = {k for k, _ in due}
            self._problems = [p for p in self._problems if p["key"] not in polled] + problems
            if rcfg.get("alerts", True):
                for p in problems:
                    self._fire(cfg, p)
                self._clear_stale(cfg, polled, problems)
            try:
                history.radio_prune(int(rcfg.get("history_days", 30)))
            except Exception:  # noqa: BLE001 - pruning must never break a round
                pass
            return problems
        finally:
            with self._lock:
                self._busy = False

    def _poll_one(self, cfg, key, dev):
        self._last_poll[key] = time.time()
        c = creds.get(key)
        try:
            res = airos.get_wifi(dev.get("ip"), c["username"], c["password"])
        except Exception as e:  # noqa: BLE001 - one bad radio must not stop the round
            res = {"ok": False, "error": str(e)}
        if not res.get("ok"):
            self._last[key] = {"ok": False, "error": res.get("error", "failed"),
                               "ts": int(time.time()), "ip": dev.get("ip")}
            return []       # unreachable-over-SSH is the uptime monitor's story
        if self.on_identity:
            try:
                self.on_identity(key, res.get("deviceName"), model=res.get("platform"))
            except Exception as e:  # noqa: BLE001 - a name is cosmetic
                print("radiomon: could not store device name:", e, flush=True)
        sample, links = _sample_from(res), _links_from(res)
        try:
            history.radio_record(key, sample, links)
        except Exception as e:  # noqa: BLE001
            print("radiomon: could not store sample:", e, flush=True)
        self._last[key] = {"ok": True, "ts": int(time.time()), "ip": dev.get("ip"),
                           "name": dev.get("name") or dev.get("ip"),
                           "model": res.get("platform") or "",
                           "mode": res.get("mode_label"), "ssid": res.get("ssid"),
                           "sample": sample, "links": links}
        return self._evaluate(key, dev, sample, links)

    # ---- rules ------------------------------------------------------------
    def _evaluate(self, key, dev, sample, links):
        label = dev.get("name") or dev.get("ip") or key
        out = []

        def add(metric, level, what, hint, value=None, base=None, peer=None, peer_name=None):
            out.append({"key": key, "ip": dev.get("ip"), "device": label,
                        "metric": metric, "level": level, "what": what, "hint": hint,
                        "value": value, "baseline": base,
                        "peer": peer, "peer_name": peer_name, "ts": int(time.time())})

        # Radio-level: air congestion, interference, antenna alignment.
        airtime = sample.get("airtime")
        if airtime is not None:
            lvl = _abs_level(airtime, ABS["airtime"], higher_is_worse=True)
            if lvl:
                add("airtime", lvl, f"air time {airtime:.0f}% used",
                    "The channel is saturated. Move to a quieter channel, narrow the "
                    "channel width, or split heavy clients onto a second radio.",
                    value=airtime)

        noise = sample.get("noise")
        base_noise, n = history.radio_baseline(key, "noise")
        if noise is not None and base_noise is not None and n >= MIN_BASELINE:
            rise = noise - base_noise
            lvl = _delta_level(rise, DELTA["noise_rise"])
            if lvl:
                add("noise", lvl,
                    f"noise floor {noise:.0f} dBm, normally {base_noise:.0f} dBm",
                    "Something new is transmitting nearby. Run airView / a spectrum "
                    "scan and move to a clear channel.", value=noise, base=base_noise)

        c0, c1 = sample.get("chain0"), sample.get("chain1")
        if c0 is not None and c1 is not None:
            gap = abs(c0 - c1)
            lvl = _abs_level(gap, ABS["chain_gap"], higher_is_worse=True)
            if lvl:
                add("chain_gap", lvl, f"antenna chains differ by {gap:.0f} dB "
                                      f"({c0:.0f} / {c1:.0f})",
                    "One polarisation is much weaker — usually a mis-aimed dish, a "
                    "loose/wet pigtail or water in a connector. Re-aim and check the "
                    "feed cables.", value=gap)

        cap = sample.get("cap_dl")
        base_cap, n = history.radio_baseline(key, "cap_dl")
        if cap and base_cap:
            frac = cap / base_cap
            lvl = ("crit" if frac <= DELTA["capacity_drop"]["crit"]
                   else "warn" if frac <= DELTA["capacity_drop"]["warn"] else None)
            if lvl and n >= MIN_BASELINE:
                add("capacity", lvl,
                    f"link capacity {cap / 1000:.0f} Mbps, normally {base_cap / 1000:.0f} Mbps",
                    "Throughput has fallen well below this link's own normal. Check "
                    "signal and interference first, then cabling and PoE.",
                    value=cap, base=base_cap)

        # Per-link: signal, link score, and links that have vanished.
        for ln in links:
            peer, pname = ln["peer"], ln.get("name") or ln["peer"]
            sig = ln.get("signal")
            if sig is not None:
                base_sig, n = history.radio_baseline(key, "signal", peer=peer)
                lvl = _abs_level(sig, ABS["signal"], higher_is_worse=False)
                trended = base_sig is not None and n >= MIN_BASELINE
                if trended:
                    lvl = _worse(lvl, _delta_level(base_sig - sig, DELTA["signal_drop"]))
                if lvl:
                    add("signal", lvl,
                        f"{pname}: signal {sig:.0f} dBm"
                        + (f", normally {base_sig:.0f} dBm" if trended else ""),
                        "The link has weakened. Check the dish alignment and mount at "
                        "both ends, and look for new growth or structures in the path.",
                        value=sig, base=base_sig if trended else None,
                        peer=peer, peer_name=pname)

            for side, col in (("download", "score_dl"), ("upload", "score_ul")):
                sc = ln.get(col)
                if sc is None:
                    continue
                lvl = _abs_level(sc, ABS["score"], higher_is_worse=False)
                base_sc, n = history.radio_baseline(key, col, peer=peer)
                if base_sc is not None and n >= MIN_BASELINE:
                    lvl = _worse(lvl, _delta_level(base_sc - sc, DELTA["score_drop"]))
                if lvl:
                    add(f"score_{side}", lvl,
                        f"{pname}: {side} link score {sc:.0f}"
                        + (f", normally {base_sc:.0f}" if base_sc is not None else ""),
                        "airMAX rates this link poorly. Interference, alignment or an "
                        "overloaded channel — check air time and noise on this radio.",
                        value=sc, base=base_sc, peer=peer, peer_name=pname)

        # A peer that was there yesterday and isn't now: the link dropped, even
        # though the radio itself still answers.
        seen_now = {ln["peer"] for ln in links}
        for peer, (pname, last_ts) in history.radio_peers_seen(key).items():
            if peer not in seen_now and time.time() - last_ts > 900:
                add("link_lost", "crit",
                    f"{pname or peer} is no longer connected to this radio",
                    "The far end lost association. Check power/PoE at that end first, "
                    "then alignment — it may be down rather than degraded.",
                    peer=peer, peer_name=pname)
        return out

    # ---- alerting ---------------------------------------------------------
    def _fire(self, cfg, p):
        alerts = cfg.get("alerts") or {}
        sid = "|".join(x for x in (p["key"], p["metric"], p.get("peer") or "") if x)
        st = self._state.get(sid, {"level": "ok", "ts": 0})
        now = time.time()
        escalated = p["level"] == "crit" and st["level"] == "warn"
        if p["level"] == st["level"] and now - st["ts"] < REALERT_S and not escalated:
            return
        notify.push(alerts, f"Wi-Fi: {p['device']}",
                    f"{p['what']}\n\n{p['hint']}",
                    priority="high" if p["level"] == "crit" else "default",
                    tags=["satellite" if p["level"] == "warn" else "warning"])
        self._state[sid] = {"level": p["level"], "ts": now}
        self._save_state()

    def _clear_stale(self, cfg, polled, problems):
        """Anything we alerted on that is no longer a problem gets a recovery note."""
        live = {"|".join(x for x in (p["key"], p["metric"], p.get("peer") or "") if x)
                for p in problems}
        alerts = cfg.get("alerts") or {}
        for sid, st in list(self._state.items()):
            if sid.split("|")[0] not in polled or sid in live or st["level"] == "ok":
                continue
            notify.push(alerts, "Wi-Fi recovered",
                        f"{sid.split('|')[1]} back to normal on {sid.split('|')[0]}.",
                        priority="low", tags=["white_check_mark"])
            self._state[sid] = {"level": "ok", "ts": time.time()}
        self._save_state()

    def _save_state(self):
        try:
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._state, f)
            os.replace(tmp, STATE_PATH)
        except OSError:
            pass

    # ---- api --------------------------------------------------------------
    def warm_start(self):
        """Re-populate the last-reading cache from the database.

        The readings live in SQLite; only the in-memory copy dies with the
        process. Without this, every container update blanked the Wireless tile
        and the whole Wi-Fi history page until the next poll came round — which
        is precisely when someone is most likely to be looking at them.
        """
        if self._warmed:
            return
        self._warmed = True
        try:
            for key, ts in history.radio_keys().items():
                if key in self._last:
                    continue
                sample, links = history.radio_latest(key)
                if not sample:
                    continue
                self._last[key] = {
                    "ok": True, "ts": sample["ts"], "ip": sample.get("ip"),
                    "name": sample.get("ip") or key,
                    "mode": sample.get("mode"), "ssid": sample.get("ssid"),
                    "sample": {k: sample.get(k) for k in
                               ("ip", "mode", "ssid", "freq", "chanbw", "signal", "noise",
                                "chain0", "chain1", "airtime", "cap_dl", "cap_ul",
                                "tx_rate", "rx_rate", "links")},
                    "links": links,
                    "from_history": True,     # remembered, not freshly read
                }
        except Exception as e:  # noqa: BLE001 - a cold cache must never 500 the API
            print("radiomon warm start:", e, flush=True)

    def snapshot(self):
        self.warm_start()
        return {"radios": self._last, "problems": self._problems,
                "busy": self._busy}


def _abs_level(value, band, higher_is_worse):
    if value is None:
        return None
    if higher_is_worse:
        return "crit" if value >= band["crit"] else "warn" if value >= band["warn"] else None
    return "crit" if value <= band["crit"] else "warn" if value <= band["warn"] else None


def _delta_level(delta, band):
    if delta is None:
        return None
    return "crit" if delta >= band["crit"] else "warn" if delta >= band["warn"] else None


def _worse(a, b):
    order = {None: 0, "warn": 1, "crit": 2}
    return a if order[a] >= order[b] else b


monitor = RadioMonitor()
