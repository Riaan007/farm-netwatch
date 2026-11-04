"""Camera and recorder health — phase 2 of the Hikvision plan
(~/reports/netwatch/Hikvision_camera_management_plan_2026-11-03.md). It only READS.

Every recorder with a saved login, every 15 min: which cameras record
(/ISAPI/System/workingstatus), its disks (/ISAPI/ContentMgmt/Storage), its clock
and time zone (/ISAPI/System/time) and whether its password is a default or weak
one (/ISAPI/Security/userCheck). Once a day: the days of footage it holds per
camera — the start of its oldest recording (/ISAPI/ContentMgmt/search, which
only answers with the isapi.org namespace). Every camera with its own login,
hourly: clock, time zone, password and its SD card.

The findings go on the camera list (tiles, camera page, recorder card), into the
Problems panel (type "camera") and so to the hub, and the serious ones — a disk
fault, a camera that is not recording — out as alerts, re-sent at most every 6 h.
The footage days are kept in /data/camera_footage.json (written once a day per
recorder); everything else lives in memory and is read again within 15 minutes.

Every request goes through hikisapi: one try per saved login.
"""
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone

import creds
import hikisapi
import notify

DATA_DIR = os.environ.get("NETWATCH_DATA", "/data")
FOOTAGE_PATH = os.path.join(DATA_DIR, "camera_footage.json")
ALERT_PATH = os.path.join(DATA_DIR, "camera_alerts.json")

REC_EVERY = 15 * 60
CAM_EVERY = 60 * 60
FOOTAGE_EVERY = 24 * 3600
START_DELAY_S = 90
REALERT_S = 6 * 3600
CLOCK_WARN_S = 120
# A footage search over a whole disk can take an old NVR well past the normal 12 s;
# after this many timeouts in a row the rest of that day's search is left for tomorrow.
SEARCH_TIMEOUT = (4, 45)
SEARCH_GIVE_UP = 2
DISK_OK = {"ok", "idle", "sleep", "sleeping", "standby", "normal"}
SEARCH_NS = "http://www.isapi.org/ver20/XMLSchema"


def _parse_time(s):
    """ISAPI time text -> epoch, or None. '2026-11-03T15:33:51+02:00' or '...Z'."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _mins(seconds):
    m = abs(seconds) / 60
    return f"{m:.0f} min" if m < 90 else f"{m / 60:.1f} h"


def read_time(cl):
    r = cl.get("/ISAPI/System/time", max_bytes=64 << 10)
    root = hikisapi.parse(r.content) if r.ok else None
    if root is None:
        return None
    local = hikisapi.text(root, "localTime")
    ts = _parse_time(local)
    return {"mode": hikisapi.text(root, "timeMode"), "tz": hikisapi.text(root, "timeZone"),
            "local": local, "drift": round(ts - time.time()) if ts else None}


def read_password(cl):
    r = cl.get("/ISAPI/Security/userCheck", max_bytes=64 << 10)
    root = hikisapi.parse(r.content) if r.ok else None
    if root is None:
        return None
    return {"default": hikisapi.text(root, "isDefaultPassword").lower() == "true",
            "risky": hikisapi.text(root, "isRiskPassword").lower() == "true"}


def read_disks(cl):
    """[{id, name, type, status, capacity_gb, free_gb}] or None when it won't say."""
    r = cl.get("/ISAPI/ContentMgmt/Storage", max_bytes=256 << 10)
    root = hikisapi.parse(r.content) if r.ok else None
    if root is None:
        return None
    out = []
    for el in root.iter():
        if hikisapi.tag(el) not in ("hdd", "nas"):
            continue
        cap, free = hikisapi.child_text(el, "capacity"), hikisapi.child_text(el, "freeSpace")
        out.append({"id": hikisapi.child_text(el, "id"), "name": hikisapi.child_text(el, "hddName") or hikisapi.child_text(el, "addressingFormatType"),
                    "type": hikisapi.tag(el), "status": hikisapi.child_text(el, "status").lower(),
                    "capacity_gb": round(int(cap) / 1024) if cap.isdigit() else None,
                    "free_gb": round(int(free) / 1024) if free.isdigit() else None})
    return out


def read_recording(cl, chan_ids):
    """{channel id: {"online": bool, "record": bool, "bitrate_kbps": int}} from the
    NVR's working status. IP channels are numbered 1.. on modern NVRs; old ones start
    them at 33 — mapped back when every number is above the channel list."""
    r = cl.get("/ISAPI/System/workingstatus", params={"format": "json"}, max_bytes=1 << 20)
    rows = []
    if r.ok:
        try:
            rows = (json.loads(r.content).get("WorkingStatus") or {}).get("ChanStatus") or []
        except ValueError:
            rows = []
    if not rows:
        r = cl.get("/ISAPI/System/workingstatus", max_bytes=1 << 20)
        root = hikisapi.parse(r.content) if r.ok else None
        for el in (root.iter() if root is not None else ()):
            if hikisapi.tag(el) == "ChanStatus":
                rows.append({k: hikisapi.child_text(el, k) for k in ("chanNo", "online", "record", "bitRate")})
    out = {}
    nums = []
    for row in rows:
        try:
            nums.append(int(row.get("chanNo")))
        except (TypeError, ValueError):
            pass
    ids = [int(c) for c in chan_ids if str(c).isdigit()]
    shift = 32 if nums and ids and min(nums) > 32 and max(ids) <= 32 else 0
    for row in rows:
        try:
            n = int(row.get("chanNo")) - shift
        except (TypeError, ValueError):
            continue
        rec = str(row.get("record")).lower() in ("1", "true")
        onl = str(row.get("online")).lower() in ("1", "true")
        try:
            kbps = int(row.get("bitRate") or 0) // 1000
        except ValueError:
            kbps = 0
        out[str(n)] = {"online": onl, "record": rec, "bitrate_kbps": kbps}
    return out


def read_oldest(cl, chan):
    """Start (epoch) of the oldest recording on channel `chan`, 0 = nothing recorded,
    None = the NVR would not say. Recording times can be local time marked Z —
    a two-hour error that does not matter at day level."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = (f'<?xml version="1.0" encoding="utf-8"?><CMSearchDescription version="1.0" xmlns="{SEARCH_NS}">'
            f"<searchID>{str(uuid.uuid4()).upper()}</searchID><trackIDList><trackID>{int(chan)}01</trackID></trackIDList>"
            f"<timeSpanList><timeSpan><startTime>2000-01-01T00:00:00Z</startTime><endTime>{now}</endTime></timeSpan></timeSpanList>"
            "<maxResults>1</maxResults><searchResultPostion>0</searchResultPostion>"
            "<metadataList><metadataDescriptor>//recordType.meta.std-cgi.com</metadataDescriptor></metadataList>"
            "</CMSearchDescription>")
    r = cl.request("POST", "/ISAPI/ContentMgmt/search", data=body.encode(),
                   headers={"Content-Type": "application/xml"}, max_bytes=256 << 10, timeout=SEARCH_TIMEOUT)
    root = hikisapi.parse(r.content) if r.ok else None
    if root is None:
        return None
    if hikisapi.text(root, "numOfMatches") == "0" or hikisapi.text(root, "responseStatusStrg") == "NO MATCHES":
        return 0
    return _parse_time(hikisapi.text(root, "startTime"))


class Health:
    def __init__(self):
        self._lock = threading.Lock()
        self.rec = {}            # nvr key -> {"ts", "disks", "chans", "time", "pw", "err"}
        self.cam = {}            # camera key -> {"ts", "time", "pw", "disks", "err"}
        self.footage = self._load(FOOTAGE_PATH)     # nvr key -> {"ts", "chans": {ch: oldest epoch|0}}
        self._alerts = self._load(ALERT_PATH)
        self._due = {}
        self.engine = None
        self.cfg = lambda: {}
        self._started = False

    @staticmethod
    def _load(path):
        try:
            with open(path) as f:
                v = json.load(f)
            return v if isinstance(v, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _save(path, data):
        try:
            with open(path + ".tmp", "w") as f:
                json.dump(data, f)
            os.replace(path + ".tmp", path)
        except OSError as e:
            print(f"[camera health] could not save {path}: {e}", flush=True)

    def start(self, engine, cfg, thread=True):
        self.engine, self.cfg = engine, cfg
        engine.health = self
        if thread and not self._started:
            self._started = True
            threading.Thread(target=self._loop, daemon=True, name="camhealth").start()

    def _settings(self):
        c = self.cfg() or {}
        cam = c.get("camera") or {}
        return {"enabled": bool((c.get("features") or {}).get("camera_pictures", True)),
                "target": max(1, int(cam.get("footage_days", 14) or 14)),
                "tz": str(cam.get("time_zone") or "CST-2:00:00")}

    # ---- reading -------------------------------------------------------------------
    def poll_recorder(self, d, chan_ids, footage=False, names=None):
        """Each reading on its own: an old NVR that times out on one question still
        answers the others. A refused login stops the lot (one-try rule)."""
        cl = self.engine._client(d)
        st = {"ts": time.time(), "err": "", "names": dict(names or {})}
        errs = []
        for k, fn in (("chans", lambda: read_recording(cl, chan_ids)), ("disks", lambda: read_disks(cl)),
                      ("time", lambda: read_time(cl)), ("pw", lambda: read_password(cl))):
            try:
                st[k] = fn()
            except hikisapi.LoginRefused as e:
                errs.append(str(e) or "LoginRefused")
                footage = False
                break
            except (hikisapi.Unreachable, hikisapi.TooBig) as e:
                errs.append(f"{k}: {str(e) or e.__class__.__name__}")
        if footage:
            old, fails, missed = {}, 0, 0
            for ch in chan_ids:
                if not str(ch).isdigit():
                    continue
                if fails >= SEARCH_GIVE_UP:
                    missed += 1
                    continue
                try:
                    old[str(ch)] = read_oldest(cl, ch)
                    fails = 0
                except hikisapi.LoginRefused as e:
                    errs.append(str(e) or "LoginRefused")
                    break
                except (hikisapi.Unreachable, hikisapi.TooBig) as e:
                    fails += 1
                    missed += 1
                    if fails == 1:
                        errs.append(f"footage search: {str(e) or e.__class__.__name__}")
                time.sleep(0.2)
            if missed:
                errs.append(f"footage not read for {missed} channel{'s' if missed != 1 else ''}")
            with self._lock:
                # Saved even when partial: the next try is tomorrow, not every 15 minutes.
                self.footage[d["key"]] = {"ts": time.time(), "chans": old}
                self._save(FOOTAGE_PATH, self.footage)
        st["err"] = "; ".join(errs)
        with self._lock:
            prev = self.rec.get(d["key"]) or {}
            self.rec[d["key"]] = {**prev, **{k: v for k, v in st.items() if v is not None or k == "err"}}

    def poll_camera(self, d):
        cl = self.engine._client(d)
        st = {"ts": time.time(), "err": ""}
        try:
            st["time"] = read_time(cl)
            st["pw"] = read_password(cl)
            st["disks"] = read_disks(cl)
        except (hikisapi.LoginRefused, hikisapi.Unreachable, hikisapi.TooBig) as e:
            st["err"] = str(e) or e.__class__.__name__
        with self._lock:
            self.cam[d["key"]] = st

    def run_once(self, force=False):
        """One pass over everything that is due (force = everything now)."""
        if not self._settings()["enabled"]:
            return
        inv = self.engine.inventory(force=True)
        devs = {d["key"]: d for d in (self.engine.devices() or []) if d.get("key")}
        lists = self.engine._lists()
        now = time.time()
        for r in inv["recorders"]:
            d = devs.get(r["key"])
            if not (d and r["online"] and r["has_login"] and not r["login_refused"]):
                continue
            if not force and now < self._due.get(("rec", r["key"]), 0):
                continue
            self._due[("rec", r["key"])] = now + REC_EVERY
            rows = (lists.get(r["key"]) or (0, "", []))[2] or []
            chans = [c.get("id") for c in rows]
            names = {str(c.get("id")): c.get("name") or "" for c in rows if c.get("id") is not None}
            fp = (self.footage.get(r["key"]) or {}).get("ts", 0)
            try:
                with self.engine._sem:
                    self.poll_recorder(d, chans, footage=force or now - fp >= FOOTAGE_EVERY, names=names)
            except Exception as e:  # noqa: BLE001 - one recorder's trouble must not stop the rest
                print(f"[camera health] {r['key']}: {e}", flush=True)
        for e in inv["cameras"]:
            if not (e.get("_own_ok") and e["online"] and e.get("_d")):
                continue
            if not force and now < self._due.get(("cam", e["key"]), 0):
                continue
            self._due[("cam", e["key"])] = now + CAM_EVERY
            try:
                with self.engine._sem:
                    self.poll_camera(e["_d"])
            except Exception as ex:  # noqa: BLE001
                print(f"[camera health] {e['key']}: {ex}", flush=True)
            time.sleep(0.2)
        self._alert(inv)

    def _loop(self):
        time.sleep(START_DELAY_S)
        while True:
            try:
                self.run_once()
            except Exception as e:  # noqa: BLE001 - health must never take the site down
                print(f"[camera health] pass failed: {e}", flush=True)
            time.sleep(60)

    # ---- findings ------------------------------------------------------------------
    def _common(self, st, tz, who):
        """Clock, time zone and password findings for one device's reading."""
        out = []
        t = (st or {}).get("time") or {}
        tz_wrong = bool(t.get("tz") and tz and t["tz"].replace(" ", "") != tz.replace(" ", ""))
        drift = t.get("drift")
        if drift is not None and tz_wrong:
            # Under a wrong time zone a clock that shows the right local time is off by
            # whole hours in real time — that part IS the time-zone finding. Only what is
            # left over is the clock's own error.
            drift -= round(drift / 3600) * 3600
        if drift is not None and abs(drift) > CLOCK_WARN_S:
            out.append({"id": "clock", "sev": "warn", "short": "Clock wrong",
                        "text": f"{who} clock is {_mins(drift)} {'fast' if drift > 0 else 'slow'}",
                        "fix": "Set its time server (Phase 3 will do this from Netwatch)."})
        if tz_wrong:
            out.append({"id": "tz", "sev": "warn", "short": "Wrong time zone",
                        "text": f"{who} time zone is {t['tz']}, not {tz}",
                        "fix": "Every time stamp it sends is off. Set the time zone to South Africa."})
        pw = (st or {}).get("pw") or {}
        if pw.get("default"):
            out.append({"id": "pw", "sev": "crit", "short": "Default password", "text": f"{who} still has its default password",
                        "fix": "Change it — anyone on the network can log in."})
        elif pw.get("risky"):
            out.append({"id": "pw", "sev": "warn", "short": "Weak password", "text": f"{who} password is weak",
                        "fix": "Change it to a stronger one."})
        return out

    def annotate(self, cams, recs):
        """Add health to the camera list (tiles, camera page, recorder cards)."""
        s = self._settings()
        with self._lock:
            rec, cam, foot = dict(self.rec), dict(self.cam), dict(self.footage)
        now = time.time()
        for r in recs:
            st = rec.get(r["key"]) or {}
            hs = self._common(st, s["tz"], "The recorder's")
            disks = st.get("disks")
            if disks is not None:
                hdds = [x for x in disks if x["type"] == "hdd"]
                if not disks:
                    hs.append({"id": "disk", "sev": "crit", "short": "No disk", "text": "The recorder has no hard disk",
                               "fix": "Nothing is being recorded. Fit a disk."})
                for x in disks:
                    if x["status"] and x["status"] not in DISK_OK:
                        hs.append({"id": "disk", "sev": "crit", "short": "Disk fault",
                                   "text": f"Disk {x['id']} is '{x['status']}'" + (f" ({x['capacity_gb']} GB)" if x["capacity_gb"] else ""),
                                   "fix": "Recordings may be lost. Check or replace the disk."})
                r["disks"] = disks
                r["disk_gb"] = sum(x["capacity_gb"] or 0 for x in hdds) or None
            chans = st.get("chans") or {}
            off = [c for c, v in chans.items() if v["online"] and not v["record"]]
            r["recording_off"] = len(off)
            if off:
                nm = st.get("names") or {}
                which = ", ".join(f"D{c}" + (f" {nm[c]}" if nm.get(c) else "") for c in sorted(off, key=lambda x: int(x) if x.isdigit() else 0)[:6])
                more = f" +{len(off) - 6}" if len(off) > 6 else ""
                hs.append({"id": "rec", "sev": "crit", "short": "Not recording",
                           "text": f"{len(off)} connected camera{'s' if len(off) != 1 else ''} not recording ({which}{more})",
                           "fix": "Check the recording schedule and the disks."})
            fo = (foot.get(r["key"]) or {}).get("chans") or {}
            days = {c: round((now - t) / 86400, 1) for c, t in fo.items() if t}
            r["footage_min_days"] = min(days.values()) if days else None
            r["footage_ts"] = int((foot.get(r["key"]) or {}).get("ts") or 0) or None
            short = [c for c, dd in days.items() if dd < s["target"] and (chans.get(c) or {}).get("record", True)]
            if short:
                hs.append({"id": "footage", "sev": "warn", "short": f"{min(days[c] for c in short):.0f} days footage",
                           "text": f"{len(short)} camera{'s' if len(short) != 1 else ''} below {s['target']} days of footage (shortest {min(days[c] for c in short):.0f} days)",
                           "fix": "A bigger disk, lower bitrates or fewer cameras recording all the time."})
            if r.get("channels_offline"):
                hs.append({"id": "lost", "sev": "warn", "short": "Cameras lost",
                           "text": f"{r['channels_offline']} of {r['channels']} channels not connected",
                           "fix": "A camera is off, moved to another address, or gone. Remove channels that are no longer used."})
            if st.get("err"):
                hs.append({"id": "read", "sev": "info", "short": "Not read", "text": f"Could not read its health: {st['err']}"})
            r["health"] = hs
            r["health_ts"] = int(st.get("ts") or 0) or None
            r["_chans"], r["_days"] = chans, days
        by_rec = {r["key"]: r for r in recs}
        for c in cams:
            hs = []
            r = by_rec.get((c.get("nvr") or {}).get("key"))
            ch = (c.get("nvr") or {}).get("channel")
            if r and ch:
                v = (r.get("_chans") or {}).get(str(ch))
                c["recording"] = v["record"] if v else None
                c["bitrate_kbps"] = v["bitrate_kbps"] if v else None
                if v and v["online"] and not v["record"]:
                    hs.append({"id": "rec", "sev": "crit", "short": "Not recording", "text": "Its recorder is not recording this camera",
                               "fix": "Check the recording schedule for this channel."})
                dd = (r.get("_days") or {}).get(str(ch))
                c["footage_days"] = dd
                if dd is not None and dd < s["target"] and (not v or v["record"]):
                    hs.append({"id": "footage", "sev": "warn", "short": f"{dd:.0f} days footage",
                               "text": f"Its recorder keeps {dd:.0f} days of its footage (target {s['target']})",
                               "fix": "A bigger disk or a lower bitrate keeps more days."})
            st = cam.get(c["key"])
            if st:
                hs += self._common(st, s["tz"], "Its")
                for x in st.get("disks") or []:
                    if x["status"] and x["status"] not in DISK_OK:
                        hs.append({"id": "sd", "sev": "warn", "short": "SD card fault", "text": f"Its SD card is '{x['status']}'",
                                   "fix": "Format or replace the card."})
            c["health"] = hs
        for r in recs:
            r.pop("_chans", None)
            r.pop("_days", None)

    def problems(self, listing):
        """Findings in the Problems-panel shape (one row per device and finding)."""
        out = []
        sevmap = {"crit": "high", "warn": "medium"}
        rows = [("recorder", r) for r in listing.get("recorders", [])] + [("camera", c) for c in listing.get("cameras", [])]
        for kind, x in rows:
            for h in x.get("health") or []:
                if h["sev"] not in sevmap:
                    continue
                if kind == "camera" and h["id"] in ("rec", "footage"):
                    continue          # counted once on its recorder
                out.append({"type": "camera", "severity": sevmap[h["sev"]], "ip": x.get("ip"),
                            "detail": f"{x.get('name')}: {h['text']}", "fix": h.get("fix", ""), "metric": h["id"],
                            "devices": [{"key": x.get("key"), "ip": x.get("ip"), "name": x.get("name"),
                                         "category": "nvr" if kind == "recorder" else "camera", "online": x.get("online", True)}]})
        return out

    # ---- alerts ----------------------------------------------------------------------
    def _alert(self, inv):
        listing = self.engine.listing()
        cfg = self.cfg() or {}
        live = {}
        for r in listing.get("recorders", []):
            for h in r.get("health") or []:
                if h["sev"] == "crit" and h["id"] in ("disk", "rec"):
                    live[f"{r['key']}|{h['id']}"] = (r.get("name") or r["key"], h)
        now = time.time()
        changed = False
        for sid, (name, h) in live.items():
            st = self._alerts.get(sid) or {}
            if st.get("on") and now - st.get("ts", 0) < REALERT_S:
                continue
            notify.push(cfg.get("alerts") or {}, f"Cameras: {name}", f"{h['text']}\n\n{h.get('fix', '')}",
                        priority="high", tags=["warning"])
            self._alerts[sid] = {"on": True, "ts": now, "what": f"{name}: {h['text']}"}
            changed = True
        for sid, st in list(self._alerts.items()):
            if st.get("on") and sid not in live:
                notify.push(cfg.get("alerts") or {}, "Cameras recovered", f"Back to normal: {st.get('what') or sid}",
                            priority="low", tags=["white_check_mark"])
                self._alerts[sid] = {"on": False, "ts": now}
                changed = True
        if changed:
            self._save(ALERT_PATH, self._alerts)


health = Health()
