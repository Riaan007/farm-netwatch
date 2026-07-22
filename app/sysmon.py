"""Raspberry Pi self-health monitor: metrics + early-warning ntfy alerts.

Samples the Pi every 60s (CPU temp, load, CPU %, RAM/swap, disk, uptime,
undervoltage/throttling) and pushes ntfy alerts BEFORE things break: warning
thresholds sit below the critical/failure points (75°C warns before the ~80°C
throttle, 85% disk warns before full, undervoltage fires immediately because
it corrupts SD cards). Recovery messages confirm when a metric returns to
normal.

Beyond the basics it also watches:
  * disk growth trend  -> "full in ~N days" prediction (14-day sample history)
  * clock drift        -> SNTP probe once an hour (RTC-less Pis drift badly)
  * SMART health       -> smartctl on SSD/NVMe/USB disks (needs the health
                          add-on: docker-compose.health.yml, privileged)
  * container restarts -> restart-looping containers via the Docker socket
                          (also needs the health add-on's socket mount)
  * hardware watchdog  -> optional (Settings -> Experimental): feeds
                          /dev/watchdog so a hard-hung Pi reboots itself;
                          disarmed cleanly on container stop (magic close).

State (alert levels + last-fired timestamps) persists in
/data/sysmon_state.json; the disk-growth history in /data/sysmon_history.json.
"""
import fcntl
import glob
import http.client
import json
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import threading
import time

import config
import notify

DATA_DIR = os.environ.get("NETWATCH_DATA", "/data")
STATE_PATH = os.path.join(DATA_DIR, "sysmon_state.json")
HISTORY_PATH = os.path.join(DATA_DIR, "sysmon_history.json")
DOCKER_SOCK = "/var/run/docker.sock"
WD_DEV = "/dev/watchdog"

SAMPLE_S = 60
REALERT_S = 6 * 3600          # repeat an unresolved warning at most every 6h
CLOCK_EVERY_S = 3600
SMART_EVERY_S = 1800
STORAGE_EVERY_S = 600

# kernel messages that mean the storage medium is in trouble
_IO_ERR_RE = re.compile(
    r"I/O error|EXT4-fs error|blk_update_request.+error|"
    r"mmc\d+:.*(?:error|timeout)|Buffer I/O error", re.IGNORECASE)

# warn = "act before it breaks"; crit = "it is breaking"
THRESH = {
    "temp":  {"warn": 75.0, "crit": 82.0},   # soft throttle starts ~80-85°C
    "disk":  {"warn": 85.0, "crit": 95.0},   # percent used (worst filesystem)
    "mem":   {"warn": 90.0, "crit": 97.0},   # percent used (MemAvailable-based)
    "load":  {"warn": 1.5,  "crit": 3.0},    # load5 per core
    "clock": {"warn": 120.0, "crit": 600.0},  # |offset| seconds vs NTP
}
DAYS_FULL_WARN = 14
DAYS_FULL_CRIT = 5


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
                    "used_pct": round(100 * (total - free) / total, 1),
                    "used_b": total - free, "free_b": free})
    return out


def _sntp_offset(server, timeout=3.0):
    """Single-shot SNTP: seconds the local clock is BEHIND (+) / ahead (-)."""
    NTP_DELTA = 2208988800
    pkt = b"\x1b" + 47 * b"\0"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        t0 = time.time()
        s.sendto(pkt, (server, 123))
        data, _ = s.recvfrom(64)
        t3 = time.time()
    finally:
        s.close()
    if len(data) < 48:
        raise ValueError("short NTP reply")
    secs, frac = struct.unpack("!II", data[40:48])
    server_t = secs - NTP_DELTA + frac / 2**32
    return server_t - (t0 + t3) / 2


# ---- Docker over the unix socket (stdlib only) -------------------------------
class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost")
        self._unix_path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(self._unix_path)
        self.sock = s


def _docker_get(path):
    conn = _UnixHTTPConnection(DOCKER_SOCK)
    try:
        conn.request("GET", path)
        r = conn.getresponse()
        if r.status != 200:
            return None
        return json.loads(r.read())
    finally:
        conn.close()


class SysMonitor:
    def __init__(self):
        self._lock = threading.Lock()
        self._last = {}            # latest metrics snapshot for /api/sysinfo
        self._cpu_prev = None      # (busy, total) from /proc/stat
        self._started = False
        self._clock = None         # {"offset_s", "checked_ts", "server"}
        self._clock_next = 0
        self._smart = None         # {"available", "devices"/"reason"}
        self._smart_next = 0
        self._cont_started = {}    # container name -> last StartedAt seen
        self._cont_changes = {}    # container name -> [restart timestamps]
        self._storage = None       # cached SD/flash health section
        self._storage_next = 0
        self._kmsg_seq = -1        # last /dev/kmsg sequence processed
        self._io_total = 0         # storage I/O errors seen since container start
        self._wd_fd = None
        self._wd_sig = False
        self._wd_err = None
        try:
            with open(STATE_PATH) as f:
                self._state = json.load(f)
        except (OSError, ValueError):
            self._state = {}       # metric -> {"level": "ok", "ts": last-alert}
        try:
            with open(HISTORY_PATH) as f:
                self._history = json.load(f)
        except (OSError, ValueError):
            self._history = {}     # mount -> [[ts, used_bytes], ...]

    # ---- disk growth trend --------------------------------------------------
    def _track_disks(self, disks):
        now = int(time.time())
        changed = False
        for d in disks:
            hist = self._history.setdefault(d["mount"], [])
            if not hist or now - hist[-1][0] >= 3500:
                hist.append([now, d["used_b"]])
                del hist[:-400]
                changed = True
            cutoff = now - 14 * 86400
            pts = [(t, u) for t, u in hist if t >= cutoff]
            trend = None
            if len(pts) >= 6 and pts[-1][0] - pts[0][0] >= 12 * 3600:
                n = len(pts)
                mt = sum(t for t, _ in pts) / n
                mu = sum(u for _, u in pts) / n
                den = sum((t - mt) ** 2 for t, _ in pts)
                if den > 0:
                    slope = sum((t - mt) * (u - mu) for t, u in pts) / den   # B/s
                    per_day = slope * 86400
                    if per_day > 5e6:              # ignore noise under ~5 MB/day
                        days = d["free_b"] / per_day
                        trend = {"growth_gb_day": round(per_day / 1e9, 2),
                                 "days_to_full": round(days, 1) if days < 3650 else None}
                    else:
                        trend = {"growth_gb_day": round(max(per_day, 0) / 1e9, 2),
                                 "days_to_full": None}
            d["trend"] = trend
        if changed:
            try:
                tmp = HISTORY_PATH + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(self._history, f)
                os.replace(tmp, HISTORY_PATH)
            except OSError:
                pass

    # ---- clock drift --------------------------------------------------------
    def _check_clock(self):
        if time.time() < self._clock_next:
            return
        self._clock_next = time.time() + CLOCK_EVERY_S
        for server in ("pool.ntp.org", "time.google.com"):
            try:
                off = _sntp_offset(server)
                self._clock = {"offset_s": round(off, 2),
                               "checked_ts": int(time.time()), "server": server}
                return
            except (OSError, ValueError):
                continue
        # offline site: keep the last reading, note nothing new

    # ---- SMART --------------------------------------------------------------
    def _smartctl(self, args):
        try:
            r = subprocess.run(["smartctl", "-j"] + args,
                               capture_output=True, text=True, timeout=25)
            return json.loads(r.stdout) if r.stdout else None
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None

    def _check_smart(self):
        if time.time() < self._smart_next:
            return
        self._smart_next = time.time() + SMART_EVERY_S
        if not shutil.which("smartctl"):
            self._smart = {"available": False, "reason": "smartctl not in image"}
            return
        scan = self._smartctl(["--scan"])
        devs = (scan or {}).get("devices") or []
        if not devs:
            self._smart = {"available": False,
                           "reason": "no disks visible — enable the health "
                                     "add-on (docker-compose.health.yml)"}
            return
        out = []
        for dev in devs[:4]:
            info = self._smartctl(["-H", "-A", "-d", dev.get("type", "auto"),
                                   dev["name"]])
            if not info:
                continue
            passed = ((info.get("smart_status") or {}).get("passed"))
            temp = (info.get("temperature") or {}).get("current")
            notes = []
            healthy = passed is not False
            nvme = info.get("nvme_smart_health_information_log")
            if nvme:
                if nvme.get("critical_warning"):
                    healthy = False
                    notes.append(f"critical_warning={nvme['critical_warning']}")
                if nvme.get("percentage_used") is not None:
                    notes.append(f"{nvme['percentage_used']}% worn")
                if nvme.get("media_errors"):
                    healthy = False
                    notes.append(f"{nvme['media_errors']} media errors")
            for attr in ((info.get("ata_smart_attributes") or {}).get("table") or []):
                if attr.get("id") in (5, 197, 198):
                    raw = (attr.get("raw") or {}).get("value", 0)
                    if raw:
                        healthy = False
                        notes.append(f"{attr.get('name')}={raw}")
            out.append({"dev": dev["name"],
                        "model": info.get("model_name"),
                        "healthy": bool(healthy), "temp_c": temp,
                        "notes": ", ".join(notes)})
        self._smart = ({"available": True, "devices": out} if out else
                       {"available": False, "reason": "no SMART-capable disks"})

    # ---- container restart watch -------------------------------------------
    def _check_containers(self):
        if not os.path.exists(DOCKER_SOCK):
            return {"available": False}
        try:
            lst = _docker_get("/containers/json?all=1") or []
        except OSError:
            return {"available": False}
        now = time.time()
        out = []
        for c in lst[:20]:
            name = (c.get("Names") or ["?"])[0].lstrip("/")
            state = c.get("State") or "?"
            started, restarts = None, 0
            try:
                ins = _docker_get(f"/containers/{c.get('Id')}/json") or {}
                started = (ins.get("State") or {}).get("StartedAt")
                restarts = ins.get("RestartCount", 0)
            except OSError:
                pass
            if started and self._cont_started.get(name) not in (None, started):
                self._cont_changes.setdefault(name, []).append(now)
            if started:
                self._cont_started[name] = started
            recent = [t for t in self._cont_changes.get(name, []) if now - t < 900]
            self._cont_changes[name] = recent
            out.append({"name": name, "state": state,
                        "restarts_15m": len(recent), "restart_count": restarts,
                        "flapping": state == "restarting" or len(recent) >= 3})
        return {"available": True, "containers": out,
                "flapping": [c["name"] for c in out if c["flapping"]]}

    # ---- SD card / flash storage health -------------------------------------
    # SD cards and USB sticks have no SMART, so we watch the SYMPTOMS of a dying
    # medium instead: the filesystem being remounted read-only (the kernel's
    # death announcement), ext4 error counters, storage I/O errors in the kernel
    # log, a tiny write+fsync probe (definitive, and its latency spikes when the
    # card controller starts struggling), and the card's own identity/age from
    # the SD CID register (manufacture date — old no-name cards fail first).
    @staticmethod
    def _mount_info(path):
        best = None
        try:
            with open("/proc/mounts") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 4:
                        continue
                    mnt = parts[1].replace("\\040", " ")
                    if path == mnt or path.startswith(mnt.rstrip("/") + "/") or mnt == "/":
                        if best is None or len(mnt) > len(best[1]):
                            best = (parts[0], mnt, parts[2], parts[3])
        except OSError:
            return None
        if not best:
            return None
        return {"device": best[0], "fstype": best[2],
                "ro": "ro" in best[3].split(",")}

    @staticmethod
    def _write_probe(directory):
        """1-block write+fsync+readback. Definitive 'is this disk still taking
        writes' test; latency in ms doubles as an early-degradation signal."""
        path = os.path.join(directory, ".sysmon_write_test")
        payload = os.urandom(512)
        t0 = time.time()
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            with open(path, "rb") as f:
                ok = f.read() == payload
            os.remove(path)
            return {"ok": ok, "fsync_ms": round((time.time() - t0) * 1000, 1)}
        except OSError as e:
            return {"ok": False, "error": e.__class__.__name__,
                    "fsync_ms": round((time.time() - t0) * 1000, 1)}

    @staticmethod
    def _ext4_errors():
        out, checked = [], 0
        for p in glob.glob("/sys/fs/ext4/*/errors_count"):
            v = _read(p)
            if v is None or not v.isdigit():
                continue
            checked += 1
            if int(v):
                dev = os.path.basename(os.path.dirname(p))
                last = _read(os.path.join(os.path.dirname(p), "last_error_time"))
                out.append({"dev": dev, "count": int(v), "last_error": last})
        return out, checked

    def _scan_kmsg(self):
        """Count NEW storage-error lines in the kernel ring buffer (needs the
        health add-on for /dev/kmsg). Sequence numbers dedupe re-reads."""
        try:
            fd = os.open("/dev/kmsg", os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            return None
        new = 0
        try:
            while True:
                try:
                    rec = os.read(fd, 8192).decode("utf-8", "replace")
                except (BlockingIOError, OSError):
                    break
                try:
                    hdr, msg = rec.split(";", 1)
                    seq = int(hdr.split(",")[1])
                except (ValueError, IndexError):
                    continue
                if seq <= self._kmsg_seq:
                    continue
                self._kmsg_seq = seq
                if _IO_ERR_RE.search(msg):
                    new += 1
        finally:
            os.close(fd)
        self._io_total += new
        return new

    @staticmethod
    def _sd_cards():
        out = []
        for dev in glob.glob("/sys/block/mmcblk*"):
            base = os.path.join(dev, "device")
            if not os.path.isdir(base):
                continue
            date = _read(os.path.join(base, "date"))       # "MM/YYYY"
            age = None
            if date and "/" in date:
                try:
                    mm, yy = date.split("/")
                    age = round((time.time() -
                                 time.mktime((int(yy), int(mm), 1, 0, 0, 0, 0, 1, -1)))
                                / (365.25 * 86400), 1)
                except (ValueError, OverflowError):
                    pass
            out.append({"dev": os.path.basename(dev),
                        "name": _read(os.path.join(base, "name")),
                        "date": date, "age_years": age})
        return out

    def _check_storage(self):
        if time.time() < self._storage_next:
            return
        self._storage_next = time.time() + STORAGE_EVERY_S
        fs = []
        info = self._mount_info(DATA_DIR) or {}
        info = {"mount": "data", **info, "write_test": self._write_probe(DATA_DIR)}
        fs.append(info)
        ext_err, ext_checked = self._ext4_errors()
        io_new = self._scan_kmsg()
        self._storage = {
            "checked_ts": int(time.time()),
            "filesystems": fs,
            "ext4_errors": ext_err, "ext4_checked": ext_checked,
            "io_errors_new": io_new,
            "io_errors_total": self._io_total if io_new is not None else None,
            "kmsg_available": io_new is not None,
            "cards": self._sd_cards(),
        }

    # ---- hardware watchdog --------------------------------------------------
    def _wd_reconcile(self, cfg):
        want = bool((cfg.get("sysmon") or {}).get("watchdog"))
        if want and self._wd_fd is None and os.path.exists(WD_DEV):
            try:
                fd = os.open(WD_DEV, os.O_WRONLY)
                try:   # WDIOC_SETTIMEOUT — BCM watchdog maxes out at ~15s anyway
                    fcntl.ioctl(fd, 0xC0045706, struct.pack("I", 15))
                except OSError:
                    pass
                self._wd_fd = fd
                self._wd_err = None
                if not self._wd_sig:
                    self._wd_sig = True
                    signal.signal(signal.SIGTERM, self._wd_sigterm)
                threading.Thread(target=self._wd_feed, daemon=True).start()
                print("[sysmon] hardware watchdog ARMED (15s)", flush=True)
            except OSError as e:
                err = ("already held by the host (systemd RuntimeWatchdogSec?) — "
                       "the Pi is watchdog-protected at OS level"
                       if e.errno == 16 else str(e))
                if err != self._wd_err:
                    self._wd_err = err
                    print(f"[sysmon] watchdog unavailable: {err}", flush=True)
        elif not want and self._wd_fd is not None:
            self._wd_disarm()
            self._wd_err = None

    def _wd_feed(self):
        while self._wd_fd is not None:
            try:
                os.write(self._wd_fd, b".")
            except OSError:
                break
            time.sleep(5)

    def _wd_disarm(self):
        fd, self._wd_fd = self._wd_fd, None
        if fd is not None:
            try:
                os.write(fd, b"V")     # magic close: disarm instead of reboot
                os.close(fd)
                print("[sysmon] hardware watchdog disarmed", flush=True)
            except OSError:
                pass

    def _wd_sigterm(self, signum, frame):
        self._wd_disarm()
        os._exit(143)

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
        disks = _disks()
        self._track_disks(disks)
        self._check_clock()
        self._check_smart()
        self._check_storage()
        containers = self._check_containers()
        snap = {
            "ts": int(time.time()),
            "model": (_read("/proc/device-tree/model") or "").replace("\x00", "") or None,
            "temp_c": _temp_c(),
            "load": [float(x) for x in load],
            "cores": cores,
            "cpu_pct": self._cpu_pct(),
            "mem": _meminfo(),
            "disks": disks,
            "uptime_s": int(up),
            "throttled": _throttled(),
            "clock": self._clock,
            "smart": self._smart,
            "storage": self._storage,
            "containers": containers,
            "watchdog": {"supported": os.path.exists(WD_DEV),
                         "enabled": bool((config.load().get("sysmon") or {})
                                         .get("watchdog")),
                         "active": self._wd_fd is not None,
                         "note": self._wd_err},
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
        self._wd_reconcile(cfg)

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

        # predictive: alert on the TREND long before the % threshold trips
        days = min((d["trend"]["days_to_full"] for d in snap.get("disks") or []
                    if d.get("trend") and d["trend"].get("days_to_full") is not None),
                   default=None)
        dl = ("crit" if days is not None and days <= DAYS_FULL_CRIT else
              "warn" if days is not None and days <= DAYS_FULL_WARN else "ok")
        self._fire(alerts_cfg, "disk_trend", dl,
                   f"[{site}] Pi disk will be FULL in ~{days} days" if days else
                   f"[{site}] Pi disk trend",
                   f"At the current growth rate the disk fills in about {days} "
                   "days. Prune history/photos or fit a bigger card before then.")

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

        ck = snap.get("clock") or {}
        off = abs(ck.get("offset_s")) if ck.get("offset_s") is not None else None
        self._fire(alerts_cfg, "clock", self._level("clock", off),
                   f"[{site}] Pi clock is drifting",
                   f"Clock is off by {off}s vs NTP ({ck.get('server')}). HTTPS, "
                   "Kuma history and log timestamps go wrong — check that NTP "
                   "sync works from this site.")

        sm = snap.get("smart") or {}
        bad = [d for d in (sm.get("devices") or []) if not d.get("healthy")]
        self._fire(alerts_cfg, "smart", "crit" if bad else "ok",
                   f"[{site}] Pi disk FAILING (SMART)",
                   "SMART reports problems on " +
                   "; ".join(f"{d['dev']} ({d.get('model') or 'disk'}): "
                             f"{d.get('notes') or 'health check failed'}"
                             for d in bad) +
                   " — back up and replace the disk." if bad else "Disk SMART")

        co = snap.get("containers") or {}
        flap = co.get("flapping") or []
        self._fire(alerts_cfg, "containers", "warn" if flap else "ok",
                   f"[{site}] Pi container restart-looping",
                   f"Container(s) {', '.join(flap)} keep restarting — the service "
                   "is effectively down. Check `docker logs` on the Pi." if flap
                   else "Containers")

        # SD/flash symptoms: read-only or failed writes = the card is dying NOW;
        # ext4 errors = corruption already started; kernel I/O errors = warning.
        sto = snap.get("storage") or {}
        problems, level = [], "ok"
        for f_ in sto.get("filesystems") or []:
            wt = f_.get("write_test") or {}
            if f_.get("ro"):
                problems.append(f"the {f_.get('mount')} filesystem is READ-ONLY "
                                "(the kernel gave up on the medium)")
                level = "crit"
            elif wt.get("ok") is False:
                problems.append(f"writing to {f_.get('mount')} FAILS "
                                f"({wt.get('error') or 'readback mismatch'})")
                level = "crit"
            elif (wt.get("fsync_ms") or 0) > 5000:
                problems.append(f"writes are extremely slow ({wt['fsync_ms']} ms "
                                "fsync) — the card controller is struggling")
                level = "warn" if level == "ok" else level
        ext_bad = sum(e.get("count") or 0 for e in sto.get("ext4_errors") or [])
        if ext_bad:
            problems.append(f"{ext_bad} filesystem error(s) recorded by ext4 — "
                            "corruption has started")
            level = "crit"
        if (sto.get("io_errors_new") or 0) > 0:
            problems.append(f"{sto['io_errors_new']} new storage I/O error(s) in "
                            "the kernel log")
            level = "warn" if level == "ok" else level
        self._fire(alerts_cfg, "storage", level,
                   f"[{site}] Pi SD/flash storage FAILING",
                   ("; ".join(problems) + " — back up now (the hub keeps a daily "
                    "config backup) and replace the card/drive.") if problems
                   else "Storage")

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
