"""Disks: a camera's SD card and a recorder's hard disks — read, and format (Phase 6, part 3).

Read: /ISAPI/ContentMgmt/Storage (each <hdd>: status, size, free, model, type).
Format: PUT /ISAPI/ContentMgmt/Storage/hdd/<id>/format, progress from
…/hdd/<id>/formatStatus (<formating>, <percent> — Hikvision's spelling), then the disk
is read again and must say "ok".

Formatting is risky work ("Risky camera work" + the typed word, one job at a time on
the site): every recording on that disk is gone. The page also asks you to tick that
you understand that. A recorder with one disk stops recording until it is done.
"""
import threading
import time
import uuid

import hikbackup
import hikisapi
import hiksettings

POLL_S = 3
FORMAT_TIMEOUT = (5, 600)
DONE_WAIT_S = 30 * 60
PLAIN = {
    "ok": "working", "unformatted": "not formatted — nothing can be recorded on it",
    "uninitialized": "not formatted — nothing can be recorded on it", "error": "faulty",
    "idle": "asleep (idle)", "formating": "being formatted", "formatting": "being formatted",
    "mismatch": "from another device — format it to use it here", "offline": "not found",
    "abnormal": "faulty", "smartfailed": "failing (its own health check)", "reparing": "being repaired",
    "notexist": "not found", "full": "full",
}


class DiskError(Exception):
    def __init__(self, msg, status=400, extra=None):
        super().__init__(msg)
        self.status = status
        self.extra = extra or {}


def read(cl):
    """[{id, name, type, status, plain, capacity_gb, free_gb, model, format_type}] or None."""
    try:
        r = cl.get("/ISAPI/ContentMgmt/Storage", max_bytes=256 << 10)
    except hikisapi.Unreachable:
        return None
    root = hikisapi.parse(r.content) if r.ok else None
    if root is None:
        return None
    out = []
    for el in root.iter():
        if hikisapi.tag(el) != "hdd":
            continue
        cap, free = hikisapi.child_text(el, "capacity"), hikisapi.child_text(el, "freeSpace")
        st = hikisapi.child_text(el, "status").lower()
        out.append({"id": hikisapi.child_text(el, "id"), "name": hikisapi.child_text(el, "hddName"),
                    "type": hikisapi.child_text(el, "hddType"), "status": st, "plain": PLAIN.get(st, st or "unknown"),
                    "capacity_mb": int(cap) if cap.isdigit() else None,
                    "capacity_gb": round(int(cap) / 1024, 1) if cap.isdigit() else None,
                    "free_gb": round(int(free) / 1024, 1) if free.isdigit() else None,
                    "model": hikisapi.child_text(el, "hddModel"), "format_type": hikisapi.child_text(el, "formatType")})
    return out


def _format_status(cl, disk_id):
    try:
        r = cl.request("GET", f"/ISAPI/ContentMgmt/Storage/hdd/{disk_id}/formatStatus", timeout=(3, 8))
    except (hikisapi.Unreachable, hikisapi.LoginRefused):
        return None
    root = hikisapi.parse(r.content) if r.status == 200 and r.content else None
    if root is None:
        return None
    pct = hikisapi.text(root, "percent")
    busy = (hikisapi.text(root, "formating") or hikisapi.text(root, "formatting")).lower() == "true"
    return {"busy": busy, "percent": int(pct) if pct.isdigit() else None}


class Disks:
    def __init__(self, settings):
        self.s = settings
        self.jobs = {}                  # key -> the newest format job
        self._lk = threading.Lock()

    def job(self, key):
        j = self.jobs.get(key)
        return dict(j, log=list(j["log"])) if j else None

    def list(self, key):
        e, cl, sem, d = hikbackup._device(self.s, key)
        with sem:
            disks = read(cl)
        if disks is None:
            raise DiskError("the device did not list its disks", 502)
        return {"ok": True, "key": key, "name": e.get("name") or "", "recorder": bool(e.get("_recorder")),
                "disks": disks, "risky": self.s.risky(), "job": self.job(key)}

    def format(self, key, disk_id, who="", confirm_text="", wipe_ok=False):
        self.s._risky_gate(confirm_text)
        if not wipe_ok:
            raise DiskError("tick that you understand every recording on this disk is deleted", 400, {"needs": "wipe_ok"})
        disk_id = str(disk_id or "")
        if not disk_id.isdigit():
            raise DiskError("unknown disk", 404)
        e, cl, sem, d = hikbackup._device(self.s, key)
        with sem:
            disks = read(cl)
        disk = next((x for x in disks or [] if x["id"] == disk_id), None)
        if disk is None:
            raise DiskError("the device has no such disk now", 404)
        with self._lk:
            if any(j["state"] == "running" for j in self.jobs.values()):
                raise DiskError("a disk is being formatted on this site; one at a time", 409)
        if not self.s._risky_lock.acquire(blocking=False):
            raise DiskError("another risky job is running on this site; one at a time", 409)
        job = {"id": uuid.uuid4().hex[:12], "key": key, "name": e.get("name") or "", "disk": disk_id,
               "before": disk, "after": None, "recorder": bool(e.get("_recorder")), "who": who or "",
               "state": "running", "percent": None, "started": int(time.time()), "finished": None,
               "result": "", "message": "", "log": []}
        with self._lk:
            self.jobs[key] = job
        threading.Thread(target=self._run, args=(job, cl, d), daemon=True, name=f"format-{key}").start()
        return job

    def _say(self, job, text):
        job["log"].append([int(time.time()), text])
        del job["log"][:-30]

    def _run(self, job, cl, d):
        try:
            self._steps(job, cl, d)
        except Exception as ex:  # noqa: BLE001 - a job must end with a state
            job.update(state="done", result="failed", message=f"Stopped: {ex}")
        finally:
            job["finished"] = int(time.time())
            self.s._risky_lock.release()
            self._history(job, d)

    def _steps(self, job, cl, d):
        disk_id = job["disk"]
        res = {}

        def send():
            try:
                res["r"] = cl.request("PUT", f"/ISAPI/ContentMgmt/Storage/hdd/{disk_id}/format",
                                      max_bytes=64 << 10, timeout=FORMAT_TIMEOUT)
            except Exception as ex:  # noqa: BLE001
                res["err"] = ex

        self._say(job, f"formatting disk {disk_id}")
        t = threading.Thread(target=send, daemon=True)
        t.start()
        watch = self.s._admin_client(d)
        while t.is_alive():
            t.join(POLL_S)
            fs = _format_status(watch, disk_id)
            if fs and fs["percent"] is not None:
                job["percent"] = fs["percent"]
        if "err" in res:
            ex = res["err"]
            if isinstance(ex, hikisapi.LoginRefused):
                raise DiskError(f"the device refused the login ({ex})", 403)
            self._say(job, f"no answer to the format request ({ex.__class__.__name__}); following its progress")
        else:
            ok, _reboot, msg = hiksettings._status(res["r"])
            if not ok:
                job.update(state="done", result="failed", message=f"The device refused to format the disk: {msg}. Nothing was deleted.")
                self._say(job, job["message"])
                return
        # some devices answer at once and format afterwards: follow the progress
        end = time.time() + DONE_WAIT_S
        while time.time() < end:
            fs = _format_status(watch, disk_id)
            if fs and not fs["busy"]:
                break
            if fs and fs["percent"] is not None:
                job["percent"] = fs["percent"]
            time.sleep(POLL_S)
        after = None
        for _ in range(10):
            disks = read(watch)
            after = next((x for x in disks or [] if x["id"] == disk_id), None)
            if after and after["status"] not in ("formating", "formatting"):
                break
            time.sleep(POLL_S)
        job["after"] = after
        job["percent"] = 100 if after and after["status"] == "ok" else job["percent"]
        if after and after["status"] == "ok":
            job.update(state="done", result="ok",
                       message=f"Disk {disk_id} is formatted and working ({after['capacity_gb']} GB)."
                       + (" The recorder records on it again." if job["recorder"] else
                          " The camera can record to it if its recording is set to the card."))
        else:
            job.update(state="done", result="failed",
                       message=f"After formatting, disk {disk_id} says: {after['plain'] if after else 'nothing (not listed)'}. "
                       "Try once more; if it stays like this, replace the disk or card.")
        self._say(job, job["message"])

    def _history(self, job, d):
        after = job.get("after") or {}
        entry = {"id": uuid.uuid4().hex[:12], "ts": int(time.time()), "who": job["who"], "key": job["key"],
                 "ip": d.get("ip"), "mac": d.get("mac"), "name": job["name"], "lens": 1, "route": "direct",
                 "channel": None, "section": "disk_format", "risk": "dangerous", "section_label": "Disk formatted",
                 "fields": [{"id": f"disk{job['disk']}", "label": f"Disk {job['disk']}",
                             "before": job["before"].get("plain"), "after": after.get("plain") or ""}],
                 "result": "ok" if job["result"] == "ok" else "failed",
                 "error": "" if job["result"] == "ok" else job["message"], "reboot": False,
                 "undo_of": None, "undone_by": None, "no_undo": True}
        try:
            self.s.log.add(entry)
            if self.s.on_change:
                self.s.on_change(d, entry)
        except Exception:  # noqa: BLE001 - history must never fail a job
            pass
