"""Raspberry Pi self-health monitor: metrics + early-warning ntfy alerts.

Samples the Pi every 60s (CPU temp, load, CPU %, RAM/swap, disk, uptime,
undervoltage/throttling) and pushes ntfy alerts BEFORE things break: warning
thresholds sit below the critical/failure points (75°C warns before the ~80°C
throttle, 85% disk warns before full, undervoltage fires immediately because
it corrupts SD cards). Recovery messages confirm when a metric returns to
normal. Everything is readable from inside the container (host /sys + /proc;
statvfs on the overlay reports the host SD card).

State (alert levels + last-fired timestamps for throttling) persists in
/data/sysmon_state.json so a container restart doesn't re-spam.
"""
import glob
import json
import os
import threading
import time

import config
import notify

DATA_DIR = os.environ.get("NETWATCH_DATA", "/data")
STATE_PATH = os.path.join(DATA_DIR, "sysmon_state.json")

SAMPLE_S = 60
REALERT_S = 6 * 3600          # repeat an unresolved warning at most every 6h

# warn = "act before it breaks"; crit = "it is breaking"
THRESH = {
    "temp":  {"warn": 75.0, "crit": 82.0},   # soft throttle starts ~80-85°C
    "disk":  {"warn": 85.0, "crit": 95.0},   # percent used (worst filesystem)
    "mem":   {"warn": 90.0, "crit": 97.0},   # percent used (MemAvailable-based)
    "load":  {"warn": 1.5,  "crit": 3.0},    # load5 per core
}


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _temp_c():
    vals = []
    for p in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        v = _read(p)
        if v and v.lstrip("-").isdigit():
            vals.append(int(v) / 1000.0)
    return round(max(vals), 1) if vals else None


def _throttled():
    """Undervoltage / throttling flags from the firmware, best-effort.
    Returns dict or None when the platform doesn't expose it (non-Pi)."""
    raw = _read("/sys/devices/platform/soc/soc:firmware/get_throttled")
    if raw:
        try:
            bits = int(raw, 16)
            return {"undervoltage_now": bool(bits & 0x1),
                    "throttled_now": bool(bits & 0x4),
                    "undervoltage_ever": bool(bits & 0x10000),
                    "throttled_ever": bool(bits & 0x40000)}
        except ValueError:
            pass
    # Pi 5: rpi_volt hwmon exposes an undervoltage alarm instead
    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        if _read(os.path.join(hw, "name")) == "rpi_volt":
            alarm = _read(os.path.join(hw, "in0_lcrit_alarm"))
            if alarm is not None:
                return {"undervoltage_now": alarm == "1",
                        "throttled_now": False,
                        "undervoltage_ever": None, "throttled_ever": None}
    return None


def _meminfo():
    out = {}
    for line in (_read("/proc/meminfo") or "").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            out[parts[0].rstrip(":")] = int(parts[1])   # kB
    total = out.get("MemTotal", 0)
    avail = out.get("MemAvailable", 0)
    swap_t = out.get("SwapTotal", 0)
    swap_f = out.get("SwapFree", 0)
    return {
        "total_mb": round(total / 1024),
        "used_pct": round(100 * (total - avail) / total, 1) if total else None,
        "swap_pct": round(100 * (swap_t - swap_f) / swap_t, 1) if swap_t else 0.0,
    }


def _disks():
    out = []
    seen = set()
    for label, path in (("data", DATA_DIR), ("root", "/")):
        try:
            st = os.statvfs(path)
        except OSError:
            continue
        key = (st.f_blocks, st.f_bfree, st.f_frsize)
        if key in seen:            # same filesystem mounted twice — report once
            continue
        seen.add(key)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        if not total:
            continue
        out.append({"mount": label, "total_gb": round(total / 1e9, 1),
                    "free_gb": round(free / 1e9, 1),
                    "used_pct": round(100 * (total - free) / total, 1)})
    return out


class SysMonitor:
    def __init__(self):
        self._lock = threading.Lock()
        self._last = {}            # latest metrics snapshot for /api/sysinfo
        self._cpu_prev = None      # (busy, total) from /proc/stat
        self._started = False
        try:
            with open(STATE_PATH) as f:
                self._state = json.load(f)
        except (OSError, ValueError):
            self._state = {}       # metric -> {"level": "ok", "ts": last-alert}

    # ---- sampling -----------------------------------------------------------
    def _cpu_pct(self):
        line = (_read("/proc/stat") or "").splitlines()[0:1]
        if not line or not line[0].startswith("cpu "):
            return None
        f = [int(x) for x in line[0].split()[1:]]
        idle = f[3] + (f[4] if len(f) > 4 else 0)
        total = sum(f)
        prev = self._cpu_prev
        self._cpu_prev = (total - idle, total)
        if not prev or total == prev[1]:
            return None
        return round(100 * ((total - idle) - prev[0]) / (total - prev[1]), 1)

    def sample(self):
        load = (_read("/proc/loadavg") or "0 0 0").split()[:3]
        up = float((_read("/proc/uptime") or "0").split()[0])
        cores = os.cpu_count() or 1
        snap = {
            "ts": int(time.time()),
            "model": (_read("/proc/device-tree/model") or "").replace("\x00", "") or None,
            "temp_c": _temp_c(),
            "load": [float(x) for x in load],
            "cores": cores,
            "cpu_pct": self._cpu_pct(),
            "mem": _meminfo(),
            "disks": _disks(),
            "uptime_s": int(up),
            "throttled": _throttled(),
        }
        with self._lock:
            self._last = snap
        return snap

    def snapshot(self):
        with self._lock:
            return dict(self._last)

    # ---- alerting -----------------------------------------------------------
    def _level(self, metric, value):
        t = THRESH[metric]
        if value is None:
            return "ok"
        if value >= t["crit"]:
            return "crit"
        if value >= t["warn"]:
            return "warn"
        return "ok"

    def _fire(self, alerts_cfg, metric, level, title, msg):
        st = self._state.get(metric, {"level": "ok", "ts": 0})
        now = time.time()
        escalated = (level == "crit" and st["level"] == "warn")
        if level == st["level"] and now - st["ts"] < REALERT_S:
            return
        if level == st["level"] and level == "ok":
            return
        if level == "ok":
            if st["level"] != "ok":
                notify.push(alerts_cfg, "Pi health recovered",
                            f"{msg} — back to normal.", priority="low",
                            tags=["white_check_mark"])
        else:
            notify.push(alerts_cfg, title, msg,
                        priority="high" if level == "crit" else "default",
                        tags=["warning"] if level == "crit" else ["thermometer"])
        self._state[metric] = {"level": level,
                               "ts": now if (level != st["level"] or escalated or
                                             now - st["ts"] >= REALERT_S) else st["ts"]}
        try:
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._state, f)
            os.replace(tmp, STATE_PATH)
        except OSError:
            pass

    def check(self, snap):
        cfg = config.load()
        alerts_cfg = cfg.get("alerts") or {}
        site = (cfg.get("site") or {}).get("name") or "site"

        temp = snap.get("temp_c")
        self._fire(alerts_cfg, "temp", self._level("temp", temp),
                   f"[{site}] Pi running hot",
                   f"CPU {temp}°C — thermal throttling starts around 80°C. "
                   "Check ventilation, dust and enclosure before performance drops.")

        worst = max((d["used_pct"] for d in snap.get("disks") or []), default=None)
        self._fire(alerts_cfg, "disk", self._level("disk", worst),
                   f"[{site}] Pi storage filling up",
                   f"Disk {worst}% full — when it fills, scans, history and Kuma "
                   "stop recording. Prune old data or grow the card soon.")

        mem = (snap.get("mem") or {}).get("used_pct")
        self._fire(alerts_cfg, "mem", self._level("mem", mem),
                   f"[{site}] Pi memory pressure",
                   f"RAM {mem}% used (swap {(snap.get('mem') or {}).get('swap_pct')}%) "
                   "— the Pi may start killing services if this keeps climbing.")

        load5 = (snap.get("load") or [0, 0, 0])[1] / max(1, snap.get("cores") or 1)
        self._fire(alerts_cfg, "load", self._level("load", load5),
                   f"[{site}] Pi CPU overloaded",
                   f"Load {snap.get('load', ['?'] * 3)[1]} on {snap.get('cores')} cores "
                   "sustained — the dashboard and scans will feel sluggish.")

        th = snap.get("throttled") or {}
        uv = "crit" if th.get("undervoltage_now") else "ok"
        self._fire(alerts_cfg, "undervoltage", uv,
                   f"[{site}] Pi POWER PROBLEM",
                   "Undervoltage detected RIGHT NOW — a failing power supply or "
                   "cable. This corrupts SD cards and causes random crashes; "
                   "replace the PSU before it kills the Pi.")

    # ---- thread -------------------------------------------------------------
    def start(self):
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        self.sample()              # prime the CPU% baseline
        time.sleep(3)
        while True:
            try:
                snap = self.sample()
                self.check(snap)
            except Exception as e:                     # never die
                print(f"[sysmon] sample failed: {e}", flush=True)
            time.sleep(SAMPLE_S)


monitor = SysMonitor()
