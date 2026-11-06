"""The firmware library and updates on the hub (Phase 6, part 2: hub/app/camfw.py).

A fake site Pi that takes the firmware file in pieces, checks a device, and runs an
update job. The hub must: keep a file uploaded in pieces (unpacking Hikvision's zip),
refuse what is not firmware, list which devices a file fits, take a backup first and
stop without one, send the file to the site once and resume a broken copy, follow the
site's job, mark the file proven, keep the test-unit and one-per-site rules, and not
update recorders at night.

Run inside the hub image with a throwaway data dir:
  docker run --rm --network none -v "$PWD:/src:ro" -v "$PWD/tests:/tests:ro" -e HUB_DATA=/tmp/hub \\
    -e HUB_APP=/src/hub/app -e PYTHONDONTWRITEBYTECODE=1 --entrypoint python farm-netwatch-hub:local \\
    /tests/test_hub_camfw.py -v
"""
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("HUB_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("HUB_APP", "/app"))

import camconfig    # noqa: E402
import camfw        # noqa: E402
import hubconfig    # noqa: E402
import server       # noqa: E402

FW = os.urandom(3 * 1024 * 1024 + 123)
SHA = hashlib.sha256(FW).hexdigest()
CFG = b"\xef\xff\xef\xff" + os.urandom(4000)
LISTING = {"enabled": True, "cameras": [
    {"key": "aa:01", "name": "Gate", "model": "DS-2CD2046G2-IU", "firmware": "V5.7.3 build 220112", "kind": "camera", "has_login": True, "online": True},
    {"key": "aa:02", "name": "Yard", "model": "DS-2CD2046G2-IU", "firmware": "V5.7.15 build 230425", "kind": "camera", "has_login": True, "online": True},
    {"key": "aa:03", "name": "Dome", "model": "DS-2CD1143G0-I", "firmware": "V5.5.0", "kind": "camera", "has_login": True, "online": True}],
    "recorders": [{"key": "nv:01", "name": "NVR", "model": "DS-7732NXI-I4/S", "firmware": "V4.62.325", "has_login": True, "online": True}]}


class Site(BaseHTTPRequestHandler):
    staged = {}          # sha -> bytearray
    puts = []            # offsets of firmware pieces
    started = []
    job = None
    polls = 0
    cfg_fail = False
    recorder = False
    result = "ok"

    def log_message(self, *a):
        pass

    def _send(self, code, data, ctype="application/json", extra=None):
        data = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path.startswith("/api/firmware/"):
            sha = u.path.rsplit("/", 1)[1]
            have = Site.staged.get(sha, b"")
            return self._send(200, {"ok": True, "have": len(have), "done": hashlib.sha256(have).hexdigest() == sha})
        if u.path.endswith("/config-file"):
            if Site.cfg_fail:
                return self._send(502, {"ok": False, "error": "the device did not give its backup file"})
            facts = {"name": "Gate", "model": "DS-2CD2046G2-IU", "firmware": "V5.7.3", "bytes": len(CFG), "sha256": hashlib.sha256(CFG).hexdigest()}
            return self._send(200, CFG, "application/octet-stream", {"X-Config-Secret": "Ab3dEf6hIj9kLm2n", "X-Config-Facts": json.dumps(facts)})
        if u.path.endswith("/snapshot"):
            return self._send(200, {"ok": True, "ts": int(time.time()), "lenses": {"1": {}}, "labels": {}, "fingerprint": "f" * 16})
        if u.path.endswith("/firmware/job"):
            Site.polls += 1
            j = dict(Site.job or {})
            if j and Site.polls >= 2:
                j.update(state="done", result=Site.result, message="Updated to V5.7.15 build 230425 and checked.",
                         checks=[{"what": "version", "ok": True, "text": "runs V5.7.15"}])
            return self._send(200, {"ok": True, "job": j or None})
        if u.path.endswith("/firmware"):
            probs = [] if q.get("version") == "V5.7.15 build 230425" else ["version"]
            return self._send(200, {"ok": True, "name": "Gate", "model": "DS-2CD2046G2-IU", "firmware": "V5.7.3 build 220112",
                                    "recorder": Site.recorder, "problems": probs, "models_asked": q.get("models")})
        return self._send(404, b"nope", "text/html")

    def do_PUT(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        body = self._body()
        sha = u.path.rsplit("/", 1)[1]
        cur = Site.staged.setdefault(sha, bytearray())
        off = int(q["offset"])
        Site.puts.append(off)
        if off != len(cur):
            return self._send(409, {"ok": False, "error": "piece out of order", "have": len(cur)})
        cur += body
        return self._send(200, {"ok": True, "have": len(cur), "done": hashlib.sha256(cur).hexdigest() == sha})

    def do_POST(self):
        b = json.loads(self._body() or b"{}")
        Site.started.append(b)
        Site.polls = 0
        Site.job = {"id": "j1", "state": "running", "step": "send", "sent": 0}
        return self._send(200, {"ok": True, "job": Site.job})


class Hub(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Site)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        camfw.DIR = tempfile.mkdtemp()
        camconfig.DIR = tempfile.mkdtemp()
        camfw.POLL_S = 0.05
        camfw.PIECE = 1 << 20
        camfw._jobs.clear()
        camfw._night = lambda: False
        camfw.cameras._last_list = lambda sid: (0, LISTING)
        cfg = hubconfig.load()
        cfg["sites"] = [{"id": "farm", "name": "Farm", "vpn_ip": "127.0.0.1", "netwatch_port": self.srv.server_port,
                         "kuma_url": "", "kuma_status_slug": "", "enabled": True}]
        hubconfig.save(cfg)
        Site.staged, Site.puts, Site.started, Site.job = {}, [], [], None
        Site.cfg_fail = Site.recorder = False
        Site.result = "ok"
        self.c = server.app.test_client()
        with self.c.session_transaction() as s:
            s["auth"] = True

    # ---- helpers ----
    def upload(self, data=FW, name="digicap.dav", version="V5.7.15 build 230425", models="DS-2CD2*G2*",
               region="International / EU"):
        j = self.c.post("/api/hub/firmware/uploads", json={"name": name, "bytes": len(data)}).get_json()
        self.assertTrue(j["ok"], j)
        piece = j["piece"]
        for off in range(0, len(data), piece):
            r = self.c.put(f"/api/hub/firmware/uploads/{j['id']}?offset={off}", data=data[off:off + piece],
                           content_type="application/octet-stream")
            self.assertEqual(r.status_code, 200, r.get_json())
        return self.c.post(f"/api/hub/firmware/uploads/{j['id']}/finish",
                           json={"version": version, "models": models, "region": region})

    def wait(self, key="aa:01"):
        end = time.time() + 20
        while time.time() < end:
            j = camfw.job_of("farm", key)
            if j and j["state"] == "done":
                return j
            time.sleep(0.05)
        self.fail("the job did not end")

    def go(self, key="aa:01", **kw):
        body = {"sha256": SHA, "confirm_text": "CHANGE", "region_ok": True, **kw}
        return self.c.post(f"/api/hub/sites/farm/cameras/{key}/firmware", json=body)

    # ---- the library ----
    def test_upload_and_fits(self):
        r = self.upload()
        self.assertEqual(r.status_code, 200, r.get_json())
        f = r.get_json()["file"]
        self.assertEqual((f["sha256"], f["bytes"], f["models"]), (SHA, len(FW), ["DS-2CD2*G2*"]))
        self.assertEqual(open(os.path.join(camfw.DIR, SHA + ".dav"), "rb").read(), FW)
        self.assertEqual(os.listdir(os.path.join(camfw.DIR, "uploads")), [], "the upload is cleaned up")
        lib = self.c.get("/api/hub/firmware").get_json()["files"][0]
        self.assertEqual(sorted((d["key"], d["compare"]) for d in lib["fits"]), [("aa:01", "newer"), ("aa:02", "same")])
        self.assertEqual(lib["newer"], 1)
        dev = self.c.get("/api/hub/sites/farm/cameras/aa:03/firmware").get_json()
        self.assertEqual(dev["files"], [], "not for that model")
        self.assertEqual(self.upload().status_code, 409, "already in the library")

    def test_zip_from_hikvision(self):
        z = io.BytesIO()
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("Release notes.pdf", b"%PDF-1.4 notes")
            zf.writestr("IPC_G2_EN_STD_5.7.15_230425/digicap.dav", FW)
        r = self.upload(z.getvalue(), name="IPC_G2.zip")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual((r.get_json()["file"]["sha256"], r.get_json()["file"]["inner"]), (SHA, "digicap.dav"))

    def test_refuses_what_is_not_firmware(self):
        z = io.BytesIO()
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("a/digicap.dav", FW)
            zf.writestr("b/digicap.dav", FW[::-1])
        self.assertIn("exactly one", self.upload(z.getvalue()).get_json()["error"])
        self.assertIn("not a firmware", self.upload(b"<html>" + b" " * (2 << 20)).get_json()["error"])
        self.assertIn("version", self.upload(version="latest").get_json()["error"])
        self.assertIn("models", self.upload(models="*").get_json()["error"])
        self.assertIn("region", self.upload(region="Mars").get_json()["error"])
        self.assertEqual(camfw.library(), [])

    def test_needs_a_hub_login(self):
        anon = server.app.test_client()
        self.assertEqual(anon.get("/api/hub/firmware").status_code, 401)
        self.assertEqual(anon.post("/api/hub/firmware/uploads", json={}).status_code, 401)

    # ---- updates ----
    def test_update_end_to_end(self):
        self.upload()
        self.assertEqual(self.go(confirm_text="").status_code, 400)
        self.assertEqual(self.go(region_ok=False).status_code, 400)
        r = self.go()
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(r.get_json()["job"]["test_unit"])
        j = self.wait()
        self.assertEqual(j["result"], "ok", j)
        self.assertEqual(bytes(Site.staged[SHA]), FW)
        self.assertEqual(Site.puts, [0, 1 << 20, 2 << 20, 3 << 20])
        self.assertEqual(Site.started[0]["models"], ["DS-2CD2*G2*"])
        self.assertEqual(Site.started[0]["confirm_text"], "CHANGE")
        self.assertEqual([b["why"] for b in camconfig.list_("farm", "aa:01")], ["change"], "a backup first")
        self.assertEqual(camfw.library()[0]["proven"][0]["key"], "aa:01")
        # the next device: the file is proven, and already on the site Pi
        Site.puts.clear()
        self.go("aa:02")
        self.assertEqual(self.wait("aa:02")["result"], "ok")
        self.assertEqual(Site.puts, [], "the file crosses the farm link once")

    def test_resumes_a_broken_copy(self):
        self.upload()
        Site.staged[SHA] = bytearray(FW[:1500000])
        self.go()
        self.assertEqual(self.wait()["result"], "ok")
        self.assertEqual(Site.puts[0], 1500000)
        self.assertEqual(bytes(Site.staged[SHA]), FW)

    def test_no_update_without_a_backup(self):
        self.upload()
        Site.cfg_fail = True
        self.go()
        j = self.wait()
        self.assertEqual(j["result"], "failed")
        self.assertIn("backup", j["message"])
        self.assertEqual((Site.puts, Site.started), ([], []), "the device was not touched")
        self.assertEqual(camfw.library()[0]["failed"], [], "a backup failure is not the file's fault")

    def test_test_unit_and_one_per_site(self):
        self.upload()
        camfw.POLL_S = 0.3
        self.go()
        r = self.go("aa:02")
        self.assertEqual(r.status_code, 409)
        self.assertIn("one at a time", r.get_json()["error"])
        self.wait()

    def test_test_unit_across_sites(self):
        self.upload()
        camfw._jobs[("other", "zz:01")] = {"site": "other", "key": "zz:01", "sha256": SHA, "state": "running"}
        r = self.go()
        self.assertEqual(r.status_code, 409)
        self.assertIn("test unit", r.get_json()["error"])

    def test_no_recorders_at_night(self):
        self.upload(models="DS-2CD2*G2*,DS-7732NXI*")
        Site.recorder = True
        camfw._night = lambda: True
        r = self.go()
        self.assertEqual(r.status_code, 409)
        self.assertIn("night", r.get_json()["error"])

    def test_failed_update_is_noted_on_the_file(self):
        self.upload()
        Site.result = "failed"
        self.go()
        self.assertEqual(self.wait()["result"], "failed")
        e = camfw.library()[0]
        self.assertEqual((e["proven"], [x["key"] for x in e["failed"]]), ([], ["aa:01"]))

    def test_hub_restart_mid_update(self):
        camfw._jobs.clear()
        camfw._write_json(os.path.join(camfw.DIR, "jobs.json"), [{"site": "farm", "key": "aa:01", "state": "running", "sha256": SHA}])
        camfw.init()
        j = camfw.job_of("farm", "aa:01")
        self.assertEqual((j["state"], j["result"]), ("done", "unknown"))

    def test_remove(self):
        self.upload()
        self.assertEqual(self.c.delete(f"/api/hub/firmware/{SHA}").status_code, 200)
        self.assertEqual(camfw.library(), [])
        self.assertFalse(os.path.exists(os.path.join(camfw.DIR, SHA + ".dav")))


if __name__ == "__main__":
    unittest.main()
