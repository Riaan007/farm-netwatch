"""The FarmCamSA standard and bulk camera jobs (Phase 5).

The standard is a fixed list of rules (RULES): each names one settings section and the
values it wants. A *check* reads every camera and recorder on the site, one device at a
time, and says per rule: meets it, off it (with what is there now), not available on
that device, or could not be read. Nothing is written by a check.

A *job* is a queue run on the site Pi, one device at a time with a pause between:

  * "rule"  — bring the chosen devices to one rule;
  * "copy"  — copy sections (picture, text on video, streams) from one camera to others
              of the same model (a value the target doesn't take is skipped, listed);
  * "undo"  — undo every change a job made, newest first.

Every device change goes through Settings.apply — the same read-back, History and Undo
as a change made by hand — so a job is only a list of ordinary changes. Rules in the
"dangerous" sections (logins and remote access) need "Risky camera work" and the typed
word, like the hand-made change. One job runs at a time per site; a job can be stopped
between devices, and can stop by itself at the first failure.
"""
import json
import os
import threading
import time
import uuid

import hiksecurity
import hikrecord
import hiksettings

DATA_DIR = os.environ.get("NETWATCH_DATA", "/data")
JOBS_PATH = os.path.join(DATA_DIR, "camera_jobs.json")
CHECK_PATH = os.path.join(DATA_DIR, "camera_standard.json")
JOBS_KEEP = 30
PAUSE_DEFAULT, PAUSE_MAX = 3, 60
MAX_ITEMS = 400

SA_TZ, NTP_HOST = hiksettings.SA_TZ, hiksettings.NTP_HOST

# id, label, why, section, wanted values, who it is for ("camera", "recorder"), per lens
RULES = [
    {"id": "tz", "label": "Time zone: South Africa (UTC+2)", "section": "time", "for": ("camera", "recorder"),
     "why": "Footage times must match the clock on the wall — many cameras ship on China time.",
     "want": {"time.timeZone": SA_TZ}},
    {"id": "ntp", "label": "Clock from the internet (pool.ntp.org, every 60 min)", "section": "time",
     "for": ("camera", "recorder"), "why": "A clock that sets itself stays right after power cuts.",
     "want": {"time.timeMode": "NTP", "ntp.NTPServer/hostName": NTP_HOST, "ntp.NTPServer/synchronizeInterval": "60"}},
    {"id": "osd_time", "label": "Date and time on video: DD-MM-YYYY, 24-hour", "section": "osd", "for": ("camera",),
     "per_lens": True, "why": "South African date order on every picture handed over as evidence.",
     "want": {"ovl.DateTimeOverlay/enabled": "true", "ovl.DateTimeOverlay/dateStyle": "DD-MM-YYYY",
              "ovl.DateTimeOverlay/timeStyle": "24hour"}},
    {"id": "osd_name", "label": "Name on video is the Netwatch name", "section": "osd", "for": ("camera",),
     "why": "One name for the camera everywhere: on the picture, in the recorder, in Netwatch.",
     "want": {"ovl.channelNameOverlay/enabled": "true", "name.name": "{netwatch_name}"}},
    {"id": "flicker", "label": "Anti-flicker: 50 Hz", "section": "picture", "for": ("camera",), "per_lens": True,
     "why": "South African mains is 50 Hz; 60 Hz makes lights roll and flicker on the picture.",
     "want": {"img.powerLineFrequency/powerLineFrequencyMode": "50hz"}},
    {"id": "sub_h264", "label": "Sub stream: H.264", "section": "sub", "for": ("camera",), "per_lens": True,
     "why": "Phones, browsers and multi-camera screens play H.264 everywhere; H.265 often not.",
     "want": {"s.Video/videoCodecType": "H.264"}},
    {"id": "main_h265", "label": "Main stream: H.265", "section": "main", "for": ("camera",), "per_lens": True,
     "why": "About half the bandwidth and disk of H.264 — more days of footage on the same disks.",
     "want": {"s.Video/videoCodecType": "H.265"}},
    {"id": "upnp_off", "label": "UPnP off", "section": "access", "for": ("camera", "recorder"), "risky": True,
     "why": "UPnP lets a device open ports on the router by itself.", "want": {"upnp.enabled": "false"}},
    {"id": "ssh_off", "label": "SSH off", "section": "access", "for": ("camera", "recorder"), "risky": True,
     "why": "A maintenance door nobody here uses.", "want": {"ssh.enabled": "false"}},
    {"id": "lock_on", "label": "Login lock on", "section": "access", "for": ("camera", "recorder"), "risky": True,
     "why": "Locks the login for a while after wrong passwords, so it can't be guessed.",
     "want": {"ll.enabled": "true"}},
]
RULE_BY = {r["id"]: r for r in RULES}
COPYABLE = {"picture": "Picture", "osd": "Text on video (not the name)", "main": "Main stream", "sub": "Sub stream"}
NO_COPY = {"name.name"}        # a camera's own name never travels


class BulkError(Exception):
    def __init__(self, msg, status=400, extra=None):
        super().__init__(msg)
        self.status = status
        self.extra = extra or {}


def _want(rule, info):
    out = {}
    for fid, v in rule["want"].items():
        if v == "{netwatch_name}":
            v = (info.get("netwatch_name") or "").strip()
            if not v:
                continue
        out[fid] = v
    return out


def judge(rule, info):
    """One rule against one device's values (Settings.values): (state, diffs, why).
    state: "ok" meets it, "off" doesn't, "na" the device (or the way to it) hasn't got it."""
    vals = (info.get("values") or {}).get(rule["section"])
    if vals is None:
        why = ("needs the camera's own login (not through the recorder)" if info.get("channel") is not None
               and rule["section"] in ("time", "access") else "this device doesn't have it")
        return "na", [], why
    diffs, missing = [], 0
    for fid, v in _want(rule, info).items():
        if fid not in vals:
            missing += 1
            continue
        now = "" if vals[fid] is None else str(vals[fid])
        if now != v:
            diffs.append({"id": fid, "now": now, "want": v})
    if missing == len(_want(rule, info)):
        return "na", [], "this device doesn't have it"
    return ("off" if diffs else "ok"), diffs, ""


class Jobs:
    def __init__(self, settings=None):
        self.settings = settings or hiksettings.settings
        self.engine = None
        self.cfg = lambda: {}
        self._lk = threading.Lock()
        self._run = None              # the job running now (one per site)
        self._stop = threading.Event()
        self.jobs = []
        self.check = None
        self.jobs_path, self.check_path = JOBS_PATH, CHECK_PATH
        self.sleep = time.sleep
        self.sync = False              # tests: run the job in the caller

    def start(self, engine, cfg, jobs_path=None, check_path=None):
        self.engine, self.cfg = engine, cfg
        self.jobs_path, self.check_path = jobs_path or JOBS_PATH, check_path or CHECK_PATH
        self.jobs = self._load(self.jobs_path, [])
        for j in self.jobs:
            if j.get("state") in ("queued", "running"):
                j.update(state="stopped", note="Netwatch restarted while it ran", finished=int(time.time()))
        self.check = self._load(self.check_path, None)
        return self

    # ---- files ------------------------------------------------------------------------
    @staticmethod
    def _load(path, empty):
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return empty

    @staticmethod
    def _save(path, data):
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, path)
        except OSError:
            pass

    def _save_jobs(self):
        with self._lk:
            self.jobs = self.jobs[-JOBS_KEEP:]
            data = json.loads(json.dumps(self.jobs))
        self._save(self.jobs_path, data)

    # ---- the devices ------------------------------------------------------------------
    def _devices(self):
        inv = self.engine.inventory()
        out = []
        for c in inv.get("cameras") or []:
            if not (c.get("_own_ok") and c.get("_d")) and not (c.get("_via_ok") and c.get("_nvr")):
                continue
            if c.get("_d") and not hiksettings.campics._hikish(c["_d"]):
                continue
            out.append({"key": c["key"], "name": c.get("name") or c["key"], "kind": "camera",
                        "model": c.get("model") or "", "online": bool(c.get("online")),
                        "lenses": max(1, len(c.get("views") or []))})
        for r in inv.get("recorders") or []:
            if r.get("has_login"):
                out.append({"key": r["key"], "name": r.get("name") or r["key"], "kind": "recorder",
                            "model": r.get("model") or "", "online": bool(r.get("online")), "lenses": 1})
        return out

    # ---- what the page shows ----------------------------------------------------------
    def overview(self):
        with self._lk:
            jobs = [self._brief(j) for j in reversed(self.jobs[-10:])]
            running = self._brief(self._run) if self._run else None
        return {"ok": True, "rules": [{k: r.get(k) for k in ("id", "label", "why", "section", "for", "risky", "per_lens")}
                                      for r in RULES],
                "copyable": COPYABLE, "check": self.check, "jobs": jobs, "running": running,
                "can_change": self.settings.enabled(), "risky": self.settings.risky(),
                "confirm_word": hiksecurity.CONFIRM_WORD}

    @staticmethod
    def _brief(j):
        if not j:
            return None
        n = {}
        for it in j.get("items") or []:
            n[it["status"]] = n.get(it["status"], 0) + 1
        return {k: j.get(k) for k in ("id", "kind", "label", "who", "state", "created", "started", "finished", "note",
                                       "undo_of", "undone_by", "rule", "risky")} | {"counts": n, "total": len(j.get("items") or [])}

    def get(self, jid):
        with self._lk:
            j = next((x for x in self.jobs if x["id"] == jid), None)
            return json.loads(json.dumps(j)) if j else None

    # ---- starting jobs ----------------------------------------------------------------
    def _need_enabled(self):
        if not self.settings.enabled():
            raise BulkError("Camera settings are turned off for this site (Settings → Cameras).", 403)

    def _begin(self, job):
        with self._lk:
            if self._run is not None:
                raise BulkError("another camera job is still running on this site; one at a time", 409)
            self._run = job
            self.jobs.append(job)
            self._stop.clear()
        self._save_jobs()
        if self.sync:
            self._worker(job)
        else:
            threading.Thread(target=self._worker, args=(job,), daemon=True, name="camera-job").start()
        return {"ok": True, "job": self._brief(job)}

    @staticmethod
    def _opts(pause_s, stop_on_fail):
        try:
            p = int(pause_s if pause_s not in (None, "") else PAUSE_DEFAULT)
        except (TypeError, ValueError):
            raise BulkError("the pause is a whole number of seconds") from None
        return max(0, min(PAUSE_MAX, p)), bool(stop_on_fail)

    def start_check(self, who=""):
        self._need_enabled()
        devs = self._devices()
        if not devs:
            raise BulkError("no camera or recorder here has a login Netwatch can use", 409)
        job = self._new("check", "Check against the FarmCamSA standard", who, 0, False,
                        [{"key": d["key"], "name": d["name"], "kind": d["kind"], "lens": 1} for d in devs])
        return self._begin(job)

    def start_rule(self, rule_id, keys, who="", pause_s=None, stop_on_fail=False, confirm_text=""):
        self._need_enabled()
        rule = RULE_BY.get(str(rule_id or ""))
        if rule is None:
            raise BulkError("unknown rule")
        if rule.get("risky"):
            self._risky_gate(confirm_text)
        items = self._pick(keys, rule["for"], per_lens=rule.get("per_lens"), section=rule["section"])
        pause, sof = self._opts(pause_s, stop_on_fail)
        job = self._new("rule", rule["label"], who, pause, sof, items)
        job.update(rule=rule["id"], risky=bool(rule.get("risky")))
        return self._begin(job)

    def start_copy(self, source, lens, section_ids, keys, who="", pause_s=None, stop_on_fail=False):
        self._need_enabled()
        secs = [s for s in dict.fromkeys(section_ids or []) if s in COPYABLE]
        if not secs:
            raise BulkError("choose what to copy: " + ", ".join(COPYABLE.values()))
        devs = {d["key"]: d for d in self._devices()}
        src = devs.get(str(source or ""))
        if not src or src["kind"] != "camera":
            raise BulkError("unknown camera to copy from", 404)
        try:
            info = self.settings.values(src["key"], int(lens or 1), secs)
        except hiksettings.SettingsError as e:
            raise BulkError(f"could not read {src['name']}: {e}", e.status) from None
        from_vals = {sid: {f: v for f, v in vals.items() if f not in NO_COPY and v is not None}
                     for sid, vals in info["values"].items()}
        if not from_vals:
            raise BulkError(f"{src['name']} has none of those settings to copy", 409)
        items = []
        for k in dict.fromkeys(keys or []):
            d = devs.get(k)
            if d is None or k == src["key"]:
                continue
            if d["kind"] != "camera" or (d["model"] or "").upper() != (src["model"] or "").upper() or not src["model"]:
                raise BulkError(f"{d['name'] if d else k} is not the same model as {src['name']} ({src['model'] or 'model unknown'})")
            for sid in from_vals:
                items.append({"key": k, "name": d["name"], "kind": "camera", "lens": int(lens or 1), "section": sid})
        if not items:
            raise BulkError("choose the cameras to copy to")
        if len(items) > MAX_ITEMS:
            raise BulkError(f"at most {MAX_ITEMS} changes in one job")
        pause, sof = self._opts(pause_s, stop_on_fail)
        job = self._new("copy", f"Copy {', '.join(COPYABLE[s] for s in from_vals).lower()} from {src['name']}",
                        who, pause, sof, items)
        job.update(source=src["key"], values=from_vals)
        return self._begin(job)

    def start_record(self, recorder, channels, want, who="", pause_s=None, stop_on_fail=False):
        """Set how ONE recorder records chosen channels (its own record tracks, so a camera
        that several recorders record is changed only on this one). want: {on: true/false,
        mode: "C" continuous / "M" motion (the whole week), stream: "main"/"sub",
        pre, post (seconds)} — only what is given is changed."""
        self._need_enabled()
        devs = {d["key"]: d for d in self._devices()}
        rec = devs.get(str(recorder or ""))
        if rec is None or rec["kind"] != "recorder":
            raise BulkError("unknown recorder", 404)
        w = want if isinstance(want, dict) else {}
        clean = {}
        if w.get("on") in (True, False):
            clean["on"] = w["on"]
        if w.get("mode") in ("C", "M"):
            clean["mode"] = w["mode"]
        if w.get("stream") in ("main", "sub"):
            clean["stream"] = w["stream"]
        for k, opts in (("pre", hikrecord.PRE), ("post", hikrecord.POST)):
            if w.get(k) not in (None, ""):
                if str(w[k]) not in opts:
                    raise BulkError(f"{k}: one of {', '.join(opts)} seconds")
                clean[k] = str(w[k])
        if not clean:
            raise BulkError("choose what to set")
        chs = sorted({int(x) for x in (channels or []) if str(x).isdigit() and 0 < int(x) <= 256})
        if not chs:
            raise BulkError("choose the channels")
        items = [{"key": rec["key"], "name": f"{rec['name']} · channel {n}", "kind": "recorder", "lens": 1,
                  "section": f"track{n}"} for n in chs]
        if len(items) > MAX_ITEMS:
            raise BulkError(f"at most {MAX_ITEMS} changes in one job")
        pause, sof = self._opts(pause_s, stop_on_fail)
        words = {"C": "continuous all week", "M": "motion all week"}
        label = "Recording on " + rec["name"] + ": " + ", ".join(filter(None, [
            ("on" if clean.get("on") else "off") if "on" in clean else "", words.get(clean.get("mode"), ""),
            f"{clean['stream']} stream" if "stream" in clean else "",
            f"{clean['pre']} s before" if "pre" in clean else "", f"{clean['post']} s after" if "post" in clean else ""]))
        job = self._new("record", label, who, pause, sof, items)
        job.update(values=clean)
        return self._begin(job)

    def start_undo(self, jid, who=""):
        self._need_enabled()
        j = self.get(jid)
        if not j:
            raise BulkError("unknown job", 404)
        if j["kind"] not in ("rule", "copy", "record"):
            raise BulkError("only a job that changed something can be undone")
        if j.get("undone_by"):
            raise BulkError("this job was already undone", 409)
        if j.get("state") in ("queued", "running"):
            raise BulkError("wait until the job has finished (or stop it)", 409)
        if j.get("risky") and not self.settings.risky():
            raise BulkError("Risky camera work is turned off for this site (Settings → Cameras).", 403)
        items = [{"key": it["key"], "name": it["name"], "kind": it["kind"], "lens": it["lens"],
                  "section": it.get("section"), "undo": it["change_id"]}
                 for it in reversed(j["items"]) if it.get("change_id")]
        if not items:
            raise BulkError("this job changed nothing, so there is nothing to undo", 409)
        job = self._new("undo", "Undo: " + j["label"], who, j.get("pause_s", PAUSE_DEFAULT), False, items)
        job.update(undo_of=jid, risky=bool(j.get("risky")))
        return self._begin(job)

    def stop(self, jid):
        with self._lk:
            if not self._run or self._run["id"] != jid:
                raise BulkError("that job is not running", 409)
            self._stop.set()
        return {"ok": True, "message": "Stopping after the device it is on now."}

    def _risky_gate(self, confirm_text):
        try:
            self.settings._risky_gate(confirm_text)
        except hiksettings.SettingsError as e:
            raise BulkError(str(e), e.status, e.extra) from None

    def _pick(self, keys, kinds, per_lens=False, section=None):
        devs = {d["key"]: d for d in self._devices()}
        items = []
        for k in dict.fromkeys(keys or []):
            d = devs.get(k)
            if d is None:
                raise BulkError(f"unknown camera or recorder: {k}", 404)
            if d["kind"] not in kinds:
                raise BulkError(f"{d['name']}: this rule is for {' and '.join(x + 's' for x in kinds)}")
            for lens in range(1, (d["lenses"] if per_lens else 1) + 1):
                items.append({"key": k, "name": d["name"] + (f" (lens {lens})" if d["lenses"] > 1 and per_lens else ""),
                              "kind": d["kind"], "lens": lens, "section": section})
        if not items:
            raise BulkError("choose the cameras and recorders to change")
        if len(items) > MAX_ITEMS:
            raise BulkError(f"at most {MAX_ITEMS} changes in one job")
        return items

    @staticmethod
    def _new(kind, label, who, pause, stop_on_fail, items):
        for it in items:
            it.update(status="pending", message="", change_id=None)
        return {"id": uuid.uuid4().hex[:12], "kind": kind, "label": label, "who": who or "", "state": "queued",
                "created": int(time.time()), "started": None, "finished": None, "pause_s": pause,
                "stop_on_fail": stop_on_fail, "items": items, "note": "", "undo_of": None, "undone_by": None}

    # ---- running ----------------------------------------------------------------------
    def _worker(self, job):
        job["state"], job["started"] = "running", int(time.time())
        results = {}
        try:
            for i, it in enumerate(job["items"]):
                if self._stop.is_set():
                    job["state"], job["note"] = "stopped", "stopped by hand"
                    break
                if i and job["pause_s"] and job["kind"] != "check":
                    self.sleep(job["pause_s"])
                it["status"] = "running"
                try:
                    self._one(job, it, results)
                except hiksettings.SettingsError as e:
                    it["status"], it["message"] = "failed", str(e)
                except Exception as e:  # noqa: BLE001 - one device must never kill the job
                    it["status"], it["message"] = "failed", f"{e.__class__.__name__}: {e}"[:300]
                self._save_jobs()
                if it["status"] == "failed" and job["stop_on_fail"]:
                    job["state"], job["note"] = "stopped", f"stopped at the first failure ({it['name']})"
                    break
            else:
                job["state"] = "done"
            for it in job["items"]:
                if it["status"] == "pending":
                    it["status"] = "skipped"
            if job["kind"] == "check":
                self.check = {"ts": int(time.time()), "job": job["id"], "devices": list(results.values())}
                self._save(self.check_path, self.check)
            if job["kind"] == "undo" and job.get("undo_of"):
                with self._lk:
                    src = next((x for x in self.jobs if x["id"] == job["undo_of"]), None)
                    if src is not None:
                        src["undone_by"] = job["id"]
                        bad = [it["name"] for it in job["items"] if it["status"] in ("failed", "partial", "skipped")]
                        if bad:
                            src["note"] = "the undo did not fully work on: " + ", ".join(bad)
        finally:
            job["finished"] = int(time.time())
            with self._lk:
                self._run = None
            self._save_jobs()

    def _one(self, job, it, results):
        s, who = self.settings, job["who"]
        if job["kind"] == "check":
            secs = {r["section"] for r in RULES if it["kind"] in r["for"]}
            row = {"key": it["key"], "name": it["name"], "kind": it["kind"], "rules": {}, "error": ""}
            results[it["key"]] = row
            try:
                info = s.values(it["key"], 1, secs)
            except hiksettings.SettingsError as e:
                row["error"] = str(e)
                it["status"], it["message"] = "failed", str(e)
                return
            row.update(route=info["route"], channel=info["channel"])
            off = 0
            for r in RULES:
                if it["kind"] not in r["for"]:
                    continue
                state, diffs, why = judge(r, info)
                row["rules"][r["id"]] = {"state": state, "diffs": diffs, "why": why}
                off += state == "off"
            it["status"], it["message"] = "ok", (f"{off} rule(s) off the standard" if off else "meets the standard")
            return
        if job["kind"] == "undo":
            r = s.undo(it["key"], it["undo"], who=who)
            self._result(it, r)
            return
        if job["kind"] == "rule":
            rule = RULE_BY[job["rule"]]
            info = {"netwatch_name": ""}
            if "{netwatch_name}" in rule["want"].values():
                info = s.values(it["key"], it["lens"], [rule["section"]])
            changes = _want(rule, info)
            r = s.apply(it["key"], it["lens"], rule["section"], changes, who=who, confirm=True,
                        confirm_text=hiksecurity.CONFIRM_WORD if rule.get("risky") else "", lenient=True)
            self._result(it, r)
            return
        if job["kind"] == "copy":
            r = s.apply(it["key"], it["lens"], it["section"], job["values"][it["section"]], who=who, confirm=True,
                        lenient=True)
            self._result(it, r)
            return
        if job["kind"] == "record":
            w, sec = job["values"], it["section"]
            cur = s.values(it["key"], 1, [sec])["values"].get(sec)
            if cur is None:
                it["status"], it["message"] = "failed", "the recorder has no recording for this channel"
                return
            T = "t." + hikrecord.X
            changes = {}
            if "on" in w:
                changes[T + "enableSchedule"] = "true" if w["on"] else "false"
            if "mode" in w:
                changes["t.TrackSchedule/ScheduleBlockList/ScheduleBlock"] = hikrecord.week_all(w["mode"])
                if w.get("on") is not False:
                    changes[T + "enableSchedule"] = "true"
            for k, f in (("pre", T + "PreRecordTimeSeconds"), ("post", T + "PostRecordTimeSeconds")):
                if k in w:
                    changes[f] = w[k]
            src = cur.get("t.SrcDescriptor/SrcUrl")
            if "stream" in w and src:
                changes["t.SrcDescriptor/SrcUrl"] = hikrecord.stream_url(src, w["stream"])
            changes = {f: v for f, v in changes.items() if str(cur.get(f)) != str(v)}
            if not changes:
                it["status"], it["message"] = "unchanged", "already set"
                return
            r = s.apply(it["key"], 1, sec, changes, who=who, confirm=True, lenient=True)
            self._result(it, r)
            return
        raise BulkError("unknown job kind")

    @staticmethod
    def _result(it, r):
        ch = r.get("change") or {}
        if ch.get("id"):
            it["change_id"] = ch["id"]
        skipped = r.get("skipped") or []
        if r.get("unchanged"):
            it["status"] = "unchanged"
        elif r.get("ok"):
            it["status"] = "ok"
        else:
            it["status"] = "partial" if it.get("change_id") else "failed"
        it["message"] = (r.get("message") or "") + (" Left out: " + "; ".join(skipped) if skipped else "")


jobs = Jobs()
