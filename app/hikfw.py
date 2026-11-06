"""Firmware updates for Hikvision cameras and recorders (Phase 6, part 2).

The hub keeps the firmware library; the site Pi does the device work:

  * a STAGED copy of one firmware file on the site Pi (data/firmware/<sha256>.dav),
    sent by the hub in pieces that can resume, so a 50–150 MB file crosses the
    farm link once, not once per device. Only one file is kept: a new one removes
    the old.
  * a check ("preflight"): the device's own login, its model fits the file, the file
    is newer than what it runs, "Risky camera work" is on.
  * the update job, one at a time on the whole site (it holds the risky-work lock):
      send the file on the LAN (PUT /ISAPI/System/updateFirmware) while reading
      /ISAPI/System/upgradeStatus → the device's answer (it refuses a file for
      another model or region itself) → restart (sent by Netwatch when the device
      asks "Reboot Required" or doesn't restart by itself) → wait until it answers
      again → check the new version, a picture (camera) or its channels (recorder),
      and that its recorder sees a camera again.

A camera reached only through its recorder is not updated here (no login of its own).
"""
import fnmatch
import hashlib
import os
import re
import shutil
import threading
import time
import uuid

import hikbackup
import hikisapi
import hiksettings

DIR = os.path.join(os.environ.get("NETWATCH_DATA", "/data"), "firmware")
MAX_FILE = 512 << 20
MIN_FILE = 1 << 20
CHUNK_MAX = 8 << 20
SPARE = 200 << 20                  # free space to leave on the card after the file
UPLOAD_TIMEOUT = (5, 900)          # the device answers after it has written the file
POLL_S = 5
SELF_RESTART_S = 150               # wait this long for a device that restarts by itself
UP_WAIT_S = 15 * 60
SETTLE_S = 20                      # after it answers again, before the checks
NVR_WAIT_S = 4 * 60
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_stage_lock = threading.Lock()

# Hikvision's own reasons for refusing a file, in plain words
WHY = {
    "languageMismatch": "the file is for another region or language than this device (a Chinese unit needs Chinese firmware, and the other way round)",
    "upgradeVersionMismatch": "the device won't take this version (too old, or not for this model)",
    "deviceTypeMismatch": "the file is for another kind of device",
    "upgradePackageMismatch": "the file is not for this model",
    "upgradeError": "the device could not install the file",
    "badFlash": "the device's flash memory refused the file",
    "lowMemory": "the device ran out of memory taking the file",
    "upgrading": "the device is already being updated",
}


class FirmwareError(Exception):
    def __init__(self, msg, status=400, extra=None):
        super().__init__(msg)
        self.status = status
        self.extra = extra or {}


# ---- versions ---------------------------------------------------------------------------
def version_key(s):
    """'V5.7.15 build 230425' -> ((5, 7, 15), 230425). Unknown parts are 0."""
    s = str(s or "")
    m = re.search(r"[Vv]?(\d+(?:\.\d+)+)", s)
    nums = tuple(int(x) for x in m.group(1).split(".")) if m else ()
    b = re.search(r"build\s*(\d{6})", s, re.I)
    return nums + (0,) * (4 - len(nums)) if nums else (), int(b.group(1)) if b else 0


def compare(device_fw, file_version):
    """'newer' (the file is newer), 'same', 'older' or 'unknown'."""
    dv, db = version_key(device_fw)
    fv, fb = version_key(file_version)
    if not dv or not fv:
        return "unknown"
    if fv != dv:
        return "newer" if fv > dv else "older"
    if fb and db and fb != db:
        return "newer" if fb > db else "older"
    return "same"


def model_fits(model, patterns):
    m = str(model or "").strip().upper()
    if not m:
        return False
    for p in patterns or []:
        p = str(p or "").strip().upper()
        if p and (fnmatch.fnmatchcase(m, p) or m == p):
            return True
    return False


# ---- the staged file --------------------------------------------------------------------
def _paths(sha):
    if not _SHA_RE.match(sha or ""):
        raise FirmwareError("bad file id", 400)
    return os.path.join(DIR, sha + ".dav"), os.path.join(DIR, sha + ".part")


def stage_status(sha):
    done, part = _paths(sha)
    if os.path.exists(done):
        return {"ok": True, "sha256": sha, "have": os.path.getsize(done), "done": True}
    return {"ok": True, "sha256": sha, "have": os.path.getsize(part) if os.path.exists(part) else 0, "done": False}


def _drop_others(keep_sha):
    try:
        names = os.listdir(DIR)
    except OSError:
        return
    for n in names:
        if not n.startswith(keep_sha) and (n.endswith(".dav") or n.endswith(".part")):
            try:
                os.remove(os.path.join(DIR, n))
            except OSError:
                pass


def stage_put(sha, offset, total, chunk):
    """Add one piece at `offset` (must be where the copy ends now). When the copy is
    `total` bytes long its SHA-256 must be `sha`, then it becomes the staged file."""
    done, part = _paths(sha)
    if not MIN_FILE <= total <= MAX_FILE:
        raise FirmwareError("that is not a firmware file (size)", 400)
    with _stage_lock:
        if os.path.exists(done):
            return stage_status(sha)
        os.makedirs(DIR, exist_ok=True)
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if offset == 0 and have == 0:
            _drop_others(sha)
            free = shutil.disk_usage(DIR).free
            if free < total + SPARE:
                raise FirmwareError(f"the site Pi has too little free space for this file ({free >> 20} MB free)", 507)
        if offset != have:
            raise FirmwareError("piece out of order", 409, {"have": have})
        if have + len(chunk) > total:
            raise FirmwareError("too many bytes", 400)
        with open(part, "ab") as f:
            f.write(chunk)
        have += len(chunk)
        if have == total:
            h = hashlib.sha256()
            with open(part, "rb") as f:
                for c in iter(lambda: f.read(1 << 20), b""):
                    h.update(c)
            if h.hexdigest() != sha:
                os.remove(part)
                raise FirmwareError("the file was damaged on the way; it will be sent again", 422)
            os.replace(part, done)
    return stage_status(sha)


# ---- the device ------------------------------------------------------------------------
def _info(cl):
    """{model, firmware, build} from /ISAPI/System/deviceInfo, or None if it doesn't answer."""
    try:
        r = cl.request("GET", "/ISAPI/System/deviceInfo", timeout=(4, 8))
    except hikisapi.Unreachable:
        return None
    if r.status != 200:
        return None
    root = hikisapi.parse(r.content)
    if root is None:
        return None
    fw, build = hikisapi.text(root, "firmwareVersion"), hikisapi.text(root, "firmwareReleasedDate")
    return {"model": hikisapi.text(root, "model"), "firmware": fw, "build": build,
            "full": (fw + (" " + build if build else "")).strip()}


def _upgrade_status(cl):
    try:
        r = cl.request("GET", "/ISAPI/System/upgradeStatus", timeout=(3, 6))
    except (hikisapi.Unreachable, hikisapi.LoginRefused):
        return None
    root = hikisapi.parse(r.content) if r.status == 200 and r.content else None
    if root is None:
        return None
    pct = hikisapi.text(root, "percent")
    return {"upgrading": hikisapi.text(root, "upgrading").lower() == "true",
            "percent": int(pct) if pct.isdigit() else None}


def _channels_online(cl):
    try:
        r = cl.request("GET", "/ISAPI/ContentMgmt/InputProxy/channels/status", timeout=(4, 15))
    except hikisapi.Unreachable:
        return None
    if r.status != 200:
        return None
    root = hikisapi.parse(r.content)
    if root is None:
        return None
    return sum(1 for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "online" and (el.text or "").strip() == "true")


def _picture_ok(cl):
    try:
        r = cl.request("GET", "/ISAPI/Streaming/channels/101/picture",
                       params={"videoResolutionWidth": 640, "videoResolutionHeight": 360}, max_bytes=8 << 20, timeout=(4, 20))
    except (hikisapi.Unreachable, hikisapi.TooBig):
        return False
    return r.status == 200 and r.content[:2] == b"\xff\xd8"


class _Counting:
    """A file that counts what has been read from it (the upload's progress)."""

    def __init__(self, path):
        self.f = open(path, "rb")
        self.size = os.path.getsize(path)
        self.sent = 0

    def __len__(self):
        return self.size - self.f.tell()

    def read(self, n=-1):
        b = self.f.read(n if n and n > 0 else 1 << 20)
        self.sent = self.f.tell()
        return b

    def seek(self, *a):
        r = self.f.seek(*a)
        self.sent = self.f.tell()
        return r

    def tell(self):
        return self.f.tell()

    def close(self):
        self.f.close()


class Firmware:
    """The site's update jobs. One runs at a time; the last job per device is kept."""

    def __init__(self, settings):
        self.s = settings
        self.jobs = {}                 # key -> job dict (the newest per device)
        self._lk = threading.Lock()

    # ---- checks ----
    def preflight(self, key, version="", models=None):
        """What the device is and whether this file may go on it."""
        e, cl, _sem, d = hikbackup._device(self.s, key)
        info = _info(cl) or {}
        model = info.get("model") or d.get("model") or ""
        cur = info.get("full") or d.get("firmware") or ""
        out = {"ok": True, "key": key, "name": e.get("name") or "", "model": model, "firmware": cur,
               "recorder": bool(e.get("_recorder")), "risky": self.s.risky(), "problems": []}
        if not info:
            out["problems"].append("the device did not answer its device information")
        if version:
            out["compare"] = compare(cur, version)
            if out["compare"] == "same":
                out["problems"].append("the device already runs this version")
            elif out["compare"] == "older":
                out["problems"].append("this file is OLDER than what the device runs")
            elif out["compare"] == "unknown":
                out["problems"].append("Netwatch can't compare the versions")
        if models is not None:
            out["fits"] = model_fits(model, models)
            if not out["fits"]:
                out["problems"].append(f"the file is not listed for this model ({model or 'unknown'})")
        if not out["risky"]:
            out["problems"].append('"Risky camera work" is off for this site')
        return out

    # ---- the job ----
    def start(self, key, sha, version, models, who="", confirm_text=""):
        self.s._risky_gate(confirm_text)
        path, _part = _paths(sha)
        if not os.path.exists(path):
            raise FirmwareError("the firmware file is not on the site Pi yet", 409, {"reason": "not_staged"})
        with self._lk:
            cur = self.jobs.get(key)
            if cur and cur["state"] == "running":
                raise FirmwareError("this device is already being updated", 409, {"reason": "busy"})
            if any(j["state"] == "running" for j in self.jobs.values()):
                raise FirmwareError("another device on this site is being updated; one at a time", 409, {"reason": "busy"})
        pre = self.preflight(key, version, models)
        if pre["problems"]:
            raise FirmwareError("Not started: " + "; ".join(pre["problems"]) + ".", 409, {"preflight": pre})
        if not self.s._risky_lock.acquire(blocking=False):
            raise FirmwareError("another risky job is running on this site; one at a time", 409)
        job = {"id": uuid.uuid4().hex[:12], "key": key, "name": pre["name"], "model": pre["model"], "sha256": sha,
               "from": pre["firmware"], "to": version, "recorder": pre["recorder"], "who": who or "",
               "state": "running", "step": "send", "sent": 0, "bytes": os.path.getsize(path), "percent": None,
               "started": int(time.time()), "finished": None, "result": "", "message": "", "checks": [], "log": []}
        with self._lk:
            self.jobs[key] = job
        threading.Thread(target=self._run, args=(job, path), daemon=True, name=f"firmware-{key}").start()
        return job

    def job(self, key):
        j = self.jobs.get(key)
        return dict(j, log=list(j["log"]), checks=list(j["checks"])) if j else None

    def _say(self, job, text):
        job["log"].append([int(time.time()), text])
        del job["log"][:-40]

    def _run(self, job, path):
        try:
            self._steps(job, path)
        except Exception as ex:  # noqa: BLE001 - a job must end with a state
            job.update(state="done", result="failed", message=f"Stopped: {ex}")
            self._say(job, f"stopped: {ex}")
        finally:
            job["finished"] = int(time.time())
            self.s._risky_lock.release()
            self._history(job)

    def _client(self, d):
        return self.s._admin_client(d)

    def _steps(self, job, path):
        e, cl, sem, d = hikbackup._device(self.s, job["key"])
        before_chans = _channels_online(cl) if job["recorder"] else None
        # 1. send the file, reading the device's progress on a second connection
        body = _Counting(path)
        res = {}

        def send():
            try:
                with sem:
                    res["r"] = cl.request("PUT", "/ISAPI/System/updateFirmware", data=body,
                                          headers={"Content-Type": "application/octet-stream"},
                                          max_bytes=64 << 10, timeout=UPLOAD_TIMEOUT)
            except Exception as ex:  # noqa: BLE001
                res["err"] = ex

        self._say(job, f"sending {job['bytes'] >> 20} MB to the device")
        t = threading.Thread(target=send, daemon=True)
        t.start()
        watch = self._client(d)
        while t.is_alive():
            t.join(POLL_S)
            job["sent"] = body.sent
            if body.sent >= body.size:
                job["step"] = "install"
                u = _upgrade_status(watch)
                if u and u["percent"] is not None:
                    job["percent"] = u["percent"]
        body.close()
        job["sent"] = body.size if "r" in res else body.sent
        restart = True
        if "err" in res:
            ex = res["err"]
            if isinstance(ex, hikisapi.LoginRefused):
                raise FirmwareError(f"the device refused the login ({ex})", 403)
            if body.sent < body.size:
                raise FirmwareError(f"the device stopped answering while the file was being sent ({ex.__class__.__name__}); "
                                    "nothing was installed — check it answers, then try again")
            # the whole file went; the connection broke as it restarted
            self._say(job, "the device dropped the connection after taking the file (restarting)")
            restart = False
        else:
            ok, reboot, msg = hiksettings._status(res["r"])
            if not ok:
                sub = msg.split(" ", 2)[-1] if msg else ""
                why = next((v for k, v in WHY.items() if k.lower() in (msg or "").lower()), "")
                raise FirmwareError(f"the device refused the file — {why or msg}" + (f" ({sub})" if why and sub else "")
                                    + ". It still runs its old firmware.")
            self._say(job, "the device took the file" + (" and asks for a restart" if reboot else ""))
            restart = bool(reboot)
        # 2. restart: sent when asked, or when it doesn't restart by itself
        job["step"] = "restart"
        went_down = False
        if not restart:
            end = time.time() + SELF_RESTART_S
            while time.time() < end:
                time.sleep(POLL_S)
                if _info(self._client(d)) is None:
                    went_down = True
                    break
            if not went_down:
                self._say(job, "it did not restart by itself; Netwatch restarts it")
                restart = True
        if restart:
            try:
                self._client(d).request("PUT", "/ISAPI/System/reboot", timeout=(4, 10))
            except hikisapi.Unreachable:
                pass
            self._say(job, "restart sent")
        # 3. wait for it to answer again
        job["step"] = "wait"
        t0 = time.time()
        if not went_down:
            while time.time() - t0 < 180:
                time.sleep(POLL_S)
                if _info(self._client(d)) is None:
                    break
        info = None
        while time.time() - t0 < UP_WAIT_S:
            time.sleep(POLL_S)
            info = _info(self._client(d))
            if info:
                break
        if not info:
            raise FirmwareError(f"the device has not answered for {UP_WAIT_S // 60} minutes after the update — "
                                "check it on site (power, the network port)")
        self._say(job, f"back after {int(time.time() - t0)} s")
        # 4. checks
        job["step"] = "check"
        time.sleep(SETTLE_S)
        info = _info(self._client(d)) or info
        good = compare(info["full"], job["to"]) == "same" or version_key(info["full"])[0] == version_key(job["to"])[0]
        job["now"] = info["full"]
        job["checks"].append({"what": "version", "ok": good, "text": f"runs {info['full'] or '?'}"})
        if job["recorder"]:
            after = _channels_online(self._client(d))
            for _ in range(int(NVR_WAIT_S / POLL_S)):
                if after is not None and before_chans is not None and after >= before_chans:
                    break
                time.sleep(POLL_S)
                after = _channels_online(self._client(d))
            ok = after is not None and (before_chans is None or after >= before_chans)
            job["checks"].append({"what": "cameras", "ok": ok,
                                  "text": f"{after if after is not None else '?'} cameras connected (before: {before_chans if before_chans is not None else '?'})"})
        else:
            ok = _picture_ok(self._client(d))
            job["checks"].append({"what": "picture", "ok": ok, "text": "a picture comes through" if ok else "no picture yet"})
            if e.get("_nvr"):
                seen = self._recorder_sees(job["key"])
                job["checks"].append({"what": "recorder", "ok": seen,
                                      "text": "its recorder sees it again" if seen else "its recorder does not see it yet"})
        if not good:
            job.update(state="done", result="failed",
                       message=f"The device came back but runs {info['full'] or 'an unknown version'}, not {job['to']}: the update did not take.")
        elif all(c["ok"] for c in job["checks"]):
            job.update(state="done", result="ok", message=f"Updated to {info['full']} and checked.")
        else:
            job.update(state="done", result="warn",
                       message=f"Updated to {info['full']}, but not every check passed — look at the device.")
        self._say(job, job["message"])

    def _recorder_sees(self, key):
        end = time.time() + NVR_WAIT_S
        while True:
            try:
                inv = self.s.engine.inventory(force=True)
            except Exception:  # noqa: BLE001
                inv = {}
            e = next((c for c in (inv or {}).get("cameras") or [] if c.get("key") == key), None)
            if e and (e.get("nvr") or {}).get("chan_online") is not False:
                return True
            if time.time() >= end:
                return False
            time.sleep(POLL_S * 3)

    def _history(self, job):
        d = {"key": job["key"]}
        try:
            _e, _cl, _sem, d = hikbackup._device(self.s, job["key"])
        except Exception:  # noqa: BLE001
            pass
        entry = {"id": uuid.uuid4().hex[:12], "ts": int(time.time()), "who": job["who"], "key": job["key"],
                 "ip": d.get("ip"), "mac": d.get("mac"), "name": job["name"], "lens": 1, "route": "direct",
                 "channel": None, "section": "firmware", "risk": "dangerous", "section_label": "Firmware update",
                 "fields": [{"id": "firmware", "label": "Firmware", "before": job["from"], "after": job.get("now") or ""}],
                 "result": "ok" if job["result"] in ("ok", "warn") else "failed",
                 "error": "" if job["result"] == "ok" else job["message"], "reboot": True,
                 "undo_of": None, "undone_by": None, "no_undo": True}
        try:
            self.s.log.add(entry)
            if self.s.on_change:
                self.s.on_change(d, entry)
        except Exception:  # noqa: BLE001 - history must never fail a job
            pass
