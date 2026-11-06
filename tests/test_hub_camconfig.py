"""Camera and recorder backups on the hub (Phase 6, part 1: hub/app/camconfig.py).

A fake site Pi that hands out a device's backup file (with its file password) and
settings snapshots. The hub must keep the file encrypted, give it back unchanged, keep a
snapshot only when it differs, take a backup before risky work it forwards, and take
the big file at night only when something changed.

Run inside the hub image with a throwaway data dir:
  docker run --rm --network none -v "$PWD:/src:ro" -v "$PWD/tests:/tests:ro" -e HUB_DATA=/tmp/hub \\
    -e HUB_APP=/src/hub/app -e PYTHONDONTWRITEBYTECODE=1 --entrypoint python farm-netwatch-hub:local \\
    /tests/test_hub_camconfig.py -v
"""
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("HUB_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("HUB_APP", "/app"))

import camconfig    # noqa: E402
import hubconfig    # noqa: E402
import server       # noqa: E402
import siteapi      # noqa: E402

FILE = b"\xef\xff\xef\xff" + os.urandom(4000)
SECRET = "Ab3dEf6hIj9kLm2n"


class Site(BaseHTTPRequestHandler):
    calls = []
    snap = {"picture": {"img.Color/brightnessLevel": "50"}}
    loaded = []

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

    def do_GET(self):
        Site.calls.append(("GET", self.path, self.headers.get(siteapi.HEADER)))
        if self.path.endswith("/config-file"):
            facts = {"name": "Gate cam", "model": "DS-2CD2046G2-IU", "firmware": "V5.7.3", "bytes": len(FILE),
                     "sha256": hashlib.sha256(FILE).hexdigest()}
            return self._send(200, FILE, "application/octet-stream",
                              {"X-Config-Secret": SECRET, "X-Config-Facts": json.dumps(facts)})
        if self.path.endswith("/snapshot"):
            fp = hashlib.sha256(json.dumps(Site.snap, sort_keys=True).encode()).hexdigest()[:16]
            return self._send(200, {"ok": True, "ts": int(time.time()), "name": "Gate cam", "lenses": {"1": Site.snap},
                                    "labels": {"img.Color/brightnessLevel": "Picture · Brightness"}, "fingerprint": fp})
        return self._send(404, b"nope", "text/html")

    def do_PUT(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        Site.calls.append(("PUT", self.path, self.headers.get("X-Config-Secret"), self.headers.get("X-Confirm-Text")))
        if self.headers.get("X-Confirm-Text") != "CHANGE":
            return self._send(400, {"ok": False, "error": "type CHANGE to confirm this change"})
        Site.loaded.append(body)
        return self._send(200, {"ok": True, "message": "restarting"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        Site.calls.append(("POST", self.path, body))
        return self._send(200, {"ok": True, "echo": body})


class HubBackups(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Site)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        camconfig.DIR = tempfile.mkdtemp()
        cfg = hubconfig.load()
        cfg["sites"] = [{"id": "farm", "name": "Farm", "vpn_ip": "127.0.0.1", "netwatch_port": self.srv.server_port,
                         "kuma_url": "", "kuma_status_slug": "", "enabled": True}]
        hubconfig.save(cfg)
        self.site = hubconfig.get_site("farm")
        Site.calls.clear()
        Site.loaded.clear()
        Site.snap = {"picture": {"img.Color/brightnessLevel": "50"}}
        self.c = server.app.test_client()
        with self.c.session_transaction() as s:
            s["auth"] = True
        camconfig.PAUSE = 0

    def files(self):
        out = []
        for root, _d, fs in os.walk(camconfig.DIR):
            out += [os.path.join(root, f) for f in fs if f.endswith(".hcb")]
        return out

    def test_back_up_now_keeps_it_encrypted_and_gives_it_back(self):
        r = self.c.post("/api/hub/sites/farm/cameras/aa:01/backups").get_json()
        self.assertTrue(r["ok"], r)
        name = r["backup"]["name"]
        self.assertTrue(name.endswith("-hand.hcb"))
        [path] = self.files()
        raw = open(path, "rb").read()
        self.assertNotIn(FILE[100:200], raw, "the device's file is not stored in the clear")
        self.assertNotIn(SECRET.encode(), raw, "nor its password")
        self.assertNotIn(SECRET, open(os.path.join(os.path.dirname(path), "index.json")).read())
        got = self.c.get(f"/api/hub/sites/farm/cameras/aa:01/backups/{name}")
        self.assertEqual(got.data, FILE)
        self.assertIn("attachment", got.headers["Content-Disposition"])
        self.assertEqual(self.c.get(f"/api/hub/sites/farm/cameras/aa:01/backups/{name}/password").get_json()["password"], SECRET)
        lst = self.c.get("/api/hub/sites/farm/cameras/aa:01/backups").get_json()
        self.assertEqual([b["name"] for b in lst["backups"]], [name])
        self.assertEqual(lst["backups"][0]["model"], "DS-2CD2046G2-IU")
        self.assertNotIn("secret", lst["backups"][0])

    def test_needs_a_hub_login_and_no_traversal(self):
        anon = server.app.test_client()
        self.assertEqual(anon.get("/api/hub/sites/farm/cameras/aa:01/backups").status_code, 401)
        self.assertEqual(self.c.get("/api/hub/sites/farm/cameras/aa:01/backups/..%2Findex.json").status_code, 404)
        self.assertEqual(self.c.get("/api/hub/sites/farm/cameras/aa:01/backups/x.hcb/password").status_code, 404)

    def test_restore_sends_the_file_and_its_password(self):
        name = self.c.post("/api/hub/sites/farm/cameras/aa:01/backups").get_json()["backup"]["name"]
        r = self.c.post(f"/api/hub/sites/farm/cameras/aa:01/backups/{name}/restore", json={})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Site.loaded, [])
        r = self.c.post(f"/api/hub/sites/farm/cameras/aa:01/backups/{name}/restore", json={"confirm_text": "CHANGE"})
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(Site.loaded, [FILE])
        self.assertEqual(Site.calls[-1][2], SECRET)

    def test_another_devices_backup_is_not_restored(self):
        name = self.c.post("/api/hub/sites/farm/cameras/aa:01/backups").get_json()["backup"]["name"]
        r = self.c.post(f"/api/hub/sites/farm/cameras/aa:02/backups/{name}/restore", json={"confirm_text": "CHANGE"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(Site.loaded, [])

    def test_a_different_key_is_refused(self):
        name = self.c.post("/api/hub/sites/farm/cameras/aa:01/backups").get_json()["backup"]["name"]
        with self.assertRaises(camconfig.CamBackupError) as cm:
            camconfig.open_("farm", "aa:01", name) and camconfig.unseal(open(self.files()[0], "rb").read(), os.urandom(32))
        self.assertEqual(cm.exception.status, 409)

    def test_snapshots_only_when_changed_and_what_changed(self):
        camconfig.snapshot(self.site, "aa:01")
        _s, changed = camconfig.snapshot(self.site, "aa:01")
        self.assertFalse(changed)
        Site.snap = {"picture": {"img.Color/brightnessLevel": "61"}}
        _s, changed = camconfig.snapshot(self.site, "aa:01")
        self.assertTrue(changed)
        ch = self.c.get("/api/hub/sites/farm/cameras/aa:01/backups").get_json()["changes"]
        self.assertEqual(ch[0]["fields"], [{"lens": "1", "id": "img.Color/brightnessLevel", "before": "50", "after": "61",
                                            "label": "Picture · Brightness"}])
        self.assertEqual(len(camconfig._snaps("farm", "aa:01")), 2)

    def test_keeps_the_newest_and_the_last_nightly(self):
        camconfig.take(self.site, "aa:01", "auto")
        for i in range(camconfig.KEEP + 2):
            time.sleep(1.05)
            camconfig.take(self.site, "aa:01", "hand")
        idx = camconfig.list_("farm", "aa:01")
        self.assertEqual(len(idx), camconfig.KEEP)
        self.assertEqual(idx[0]["why"], "auto")
        self.assertEqual(len(self.files()), camconfig.KEEP)

    def test_backup_before_risky_work_once(self):
        self.c.post("/api/hub/sites/farm/cameras/aa:01/account", json={"action": "create", "confirm_text": "CHANGE"})
        self.c.post("/api/hub/sites/farm/cameras/aa:01/settings", json={"section": "access", "changes": {}})
        self.c.post("/api/hub/sites/farm/cameras/aa:01/settings", json={"section": "picture", "changes": {}})
        self.c.post("/api/hub/sites/farm/cameras/aa:01/isapi", json={"path": "/ISAPI/System/time", "doc": "", "preview": True})
        got = [c for c in Site.calls if c[0] == "GET" and c[1].endswith("/config-file")]
        self.assertEqual(len(got), 1, "one backup, then the recent one counts")
        self.assertEqual([b["why"] for b in camconfig.list_("farm", "aa:01")], ["change"])

    def test_nightly_takes_the_file_only_when_changed(self):
        self.assertEqual(camconfig.nightly_one(self.site, "aa:01"), "file taken")
        camconfig._snaps("farm", "aa:01")
        # a week on, nothing changed: a snapshot, no file
        p = os.path.join(camconfig._dev_dir("farm", "aa:01"), "snapshots.json")
        s = json.load(open(p))
        s[-1]["seen"] -= camconfig.SNAP_EVERY + 10
        json.dump(s, open(p, "w"))
        self.assertEqual(camconfig.nightly_one(self.site, "aa:01"), "unchanged")
        self.assertEqual(camconfig.nightly_one(self.site, "aa:01"), "snapshot recent")
        s = json.load(open(p))
        s[-1]["seen"] -= camconfig.SNAP_EVERY + 10
        json.dump(s, open(p, "w"))
        Site.snap = {"picture": {"img.Color/brightnessLevel": "70"}}
        time.sleep(1.05)
        self.assertEqual(camconfig.nightly_one(self.site, "aa:01"), "file taken")
        self.assertEqual(len(camconfig.list_("farm", "aa:01")), 2)


if __name__ == "__main__":
    unittest.main()
