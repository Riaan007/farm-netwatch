"""Hub: the camera-picture proxy (hub/app/cameras.py) against a fake site.

What it promises: nothing without the hub login; the site's key on every forward;
a short memory cache so one picture crosses the farm link once; only JPEGs pass;
and while a site is offline its last camera list and the hub's saved pictures
still show what each camera looked at.

Run inside the hub image with a throwaway data dir:
  docker run --rm --network none -v "$PWD/hub/app:/app:ro" -v "$PWD/tests:/tests:ro" -e HUB_DATA=/tmp/hub \\
    -e PYTHONDONTWRITEBYTECODE=1 --entrypoint python farm-netwatch-hub:local /tests/test_hub_cameras.py -v
"""
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("HUB_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("HUB_APP", "/app"))

import cameras      # noqa: E402
import hubconfig    # noqa: E402
import server       # noqa: E402
import siteapi      # noqa: E402


def jpeg(w, h, pad=0):
    sof = bytes([0xFF, 0xC0, 0x00, 0x11, 0x08]) + h.to_bytes(2, "big") + w.to_bytes(2, "big") \
        + bytes([3, 1, 0x22, 0, 2, 0x11, 1, 3, 0x11, 1])
    com = (bytes([0xFF, 0xFE]) + (pad + 2).to_bytes(2, "big") + b"\0" * pad) if pad else b""
    return b"\xff\xd8" + com + sof + b"\xff\xd9"


LISTING = {"ok": True, "enabled": True, "refresh_min": 30, "cameras": [
    {"key": "aa:01", "name": "Gate", "route": "direct", "online": True, "picture": {"ts": 1, "route": "direct"}},
    {"key": "ch:aa:09:4", "name": "Pump", "route": "nvr", "online": True, "picture": {"ts": 1, "route": "nvr"}},
], "recorders": []}


class FakeSite(BaseHTTPRequestHandler):
    calls = []
    mode = "ok"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        FakeSite.calls.append((self.path, self.headers.get(siteapi.HEADER)))
        if FakeSite.mode == "legacy":
            return self._send(404, b"<html>Not Found</html>", "text/html")
        if self.path.startswith("/api/cameras/") and "/picture" in self.path:
            if FakeSite.mode == "html":
                return self._send(200, b"<html>hello</html>", "text/html")
            if FakeSite.mode == "failed":
                return self._send(502, {"ok": False, "error": "camera timed out", "why": "failed"})
            return self._send(200, jpeg(640, 360, pad=3000), "image/jpeg",
                              {"X-Picture-Ts": "1700000000", "X-Picture-Route": "direct", "X-Picture-Size": "640x360",
                               "X-Picture-Stale": "0"})
        if self.path.startswith("/api/cameras"):
            return self._send(200, LISTING)
        return self._send(404, b"nope", "text/html")


def _post(self):
    n = int(self.headers.get("Content-Length") or 0)
    body = json.loads(self.rfile.read(n) or b"{}")
    FakeSite.calls.append((self.path, self.headers.get(siteapi.HEADER), body))
    if FakeSite.mode == "legacy":
        return self._send(405, b"<html>Method Not Allowed</html>", "text/html")
    if FakeSite.mode == "refuse":
        return self._send(403, {"ok": False, "error": "Camera settings are turned off for this site"})
    return self._send(200, {"ok": True, "echo": body})


FakeSite.do_POST = _post


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class HubCameras(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeSite)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def point(self, port):
        cfg = hubconfig.load()
        cfg["sites"] = [{"id": "farm", "name": "Farm", "vpn_ip": "127.0.0.1", "netwatch_port": port,
                         "kuma_url": "", "kuma_status_slug": "", "enabled": True}]
        hubconfig.save(cfg)

    def setUp(self):
        self.point(self.srv.server_port)
        FakeSite.calls.clear()
        FakeSite.mode = "ok"
        cameras._lists.clear()
        cameras._pics.clear()
        cameras._saved_at.clear()
        cameras._list_saved_at.clear()
        self.c = server.app.test_client()
        with self.c.session_transaction() as s:
            s["auth"] = True

    def test_needs_a_hub_login(self):
        anon = server.app.test_client()
        self.assertEqual(anon.get("/api/hub/sites/farm/cameras").status_code, 401)
        self.assertEqual(anon.get("/api/hub/sites/farm/cameras/aa:01/picture").status_code, 401)
        self.assertEqual(FakeSite.calls, [])

    def test_the_list_is_forwarded_with_the_key_and_cached(self):
        self.assertEqual(self.c.get("/api/hub/sites/farm/cameras").get_json()["cameras"][0]["key"], "aa:01")
        self.assertEqual(self.c.get("/api/hub/sites/farm/cameras").status_code, 200)
        self.assertEqual(len(FakeSite.calls), 1)
        self.assertEqual(FakeSite.calls[0][1], siteapi.headers(hubconfig.get_site("farm"))[siteapi.HEADER])

    def test_a_picture_is_forwarded_once_with_its_headers(self):
        r = self.c.get("/api/hub/sites/farm/cameras/aa:01/picture")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "image/jpeg")
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertEqual((r.headers["X-Picture-Route"], r.headers["X-Picture-Size"]), ("direct", "640x360"))
        self.c.get("/api/hub/sites/farm/cameras/aa:01/picture")
        self.assertEqual(len([c for c in FakeSite.calls if "/picture" in c[0]]), 1, "a cached picture crossed the link again")
        self.assertIn("aa%3A01", FakeSite.calls[0][0])         # the key is quoted on the way to the site

    def test_only_pictures_pass(self):
        FakeSite.mode = "html"
        r = self.c.get("/api/hub/sites/farm/cameras/aa:01/picture")
        self.assertEqual(r.status_code, 502)

    def test_an_old_site_is_told_to_update(self):
        FakeSite.mode = "legacy"
        self.assertEqual(self.c.get("/api/hub/sites/farm/cameras").status_code, 501)
        self.assertEqual(self.c.get("/api/hub/sites/farm/cameras/aa:01/picture").status_code, 501)

    def test_settings_calls_are_forwarded_with_the_key(self):
        self.c.get("/api/hub/sites/farm/cameras/aa:01/settings?lens=2")
        path, key = FakeSite.calls[-1][:2]
        self.assertTrue(path.startswith("/api/cameras/aa%3A01/settings") and "lens=2" in path, path)
        self.assertEqual(key, siteapi.headers(hubconfig.get_site("farm"))[siteapi.HEADER])
        body = {"lens": 1, "section": "picture", "changes": {"img.Color/brightnessLevel": "60"}}
        r = self.c.post("/api/hub/sites/farm/cameras/aa:01/settings", json=body)
        self.assertEqual(r.get_json()["echo"], body)
        r = self.c.post("/api/hub/sites/farm/cameras/aa:01/changes/abc123/undo", json={"confirm": True})
        self.assertEqual(FakeSite.calls[-1][0], "/api/cameras/aa%3A01/changes/abc123/undo")
        self.assertEqual(r.get_json()["echo"], {"confirm": True})

    def test_ptz_and_detection_calls_are_forwarded(self):
        self.c.get("/api/hub/sites/farm/cameras/aa:01/settings?lens=1&group=detect&route=nvr")
        path = FakeSite.calls[-1][0]
        self.assertTrue("group=detect" in path and "route=nvr" in path, path)
        self.c.get("/api/hub/sites/farm/cameras/aa:01/ptz?lens=1")
        self.assertTrue(FakeSite.calls[-1][0].startswith("/api/cameras/aa%3A01/ptz"))
        body = {"lens": 1, "action": "move", "pan": 30, "ms": 400}
        r = self.c.post("/api/hub/sites/farm/cameras/aa:01/ptz", json=body)
        self.assertEqual((FakeSite.calls[-1][0], r.get_json()["echo"]), ("/api/cameras/aa%3A01/ptz", body))
        anon = server.app.test_client()
        self.assertEqual(anon.post("/api/hub/sites/farm/cameras/aa:01/ptz", json=body).status_code, 401)

    def test_channels_and_the_password_job_are_forwarded(self):
        self.c.get("/api/hub/sites/farm/cameras/aa:09/channels")
        self.assertTrue(FakeSite.calls[-1][0].startswith("/api/cameras/aa%3A09/channels"))
        body = {"action": "fix_login", "ch": 3, "confirm_text": "CHANGE"}
        r = self.c.post("/api/hub/sites/farm/cameras/aa:09/channels", json=body)
        self.assertEqual((FakeSite.calls[-1][0], r.get_json()["echo"]), ("/api/cameras/aa%3A09/channels", body))
        body = {"new": "x", "again": "x", "confirm_text": "CHANGE"}
        r = self.c.post("/api/hub/sites/farm/cameras/aa:01/password", json=body)
        self.assertEqual((FakeSite.calls[-1][0], r.get_json()["echo"]), ("/api/cameras/aa%3A01/password", body))
        anon = server.app.test_client()
        self.assertEqual(anon.post("/api/hub/sites/farm/cameras/aa:01/password", json=body).status_code, 401)

    def test_the_explorer_is_forwarded(self):
        self.c.get("/api/hub/sites/farm/cameras/aa:01/isapi?path=/ISAPI/System/time")
        path = FakeSite.calls[-1][0]
        self.assertTrue(path.startswith("/api/cameras/aa%3A01/isapi") and "System" in path, path)
        body = {"path": "/ISAPI/System/time", "doc": "<Time/>", "preview": True}
        r = self.c.post("/api/hub/sites/farm/cameras/aa:01/isapi", json=body)
        self.assertEqual((FakeSite.calls[-1][0], r.get_json()["echo"]), ("/api/cameras/aa%3A01/isapi", body))

    def test_the_standard_and_jobs_are_forwarded(self):
        self.c.get("/api/hub/sites/farm/camera-standard")
        self.assertEqual(FakeSite.calls[-1][0], "/api/camera-standard")
        self.c.post("/api/hub/sites/farm/camera-standard", json={})
        self.assertEqual(FakeSite.calls[-1][0], "/api/camera-standard")
        body = {"kind": "rule", "rule": "tz", "keys": ["aa:01"]}
        r = self.c.post("/api/hub/sites/farm/camera-jobs", json=body)
        self.assertEqual((FakeSite.calls[-1][0], r.get_json()["echo"]), ("/api/camera-jobs", body))
        self.c.get("/api/hub/sites/farm/camera-jobs/abc123")
        self.assertEqual(FakeSite.calls[-1][0], "/api/camera-jobs/abc123")
        self.c.post("/api/hub/sites/farm/camera-jobs/abc123/undo", json={})
        self.assertEqual(FakeSite.calls[-1][0], "/api/camera-jobs/abc123/undo")
        n = len(FakeSite.calls)
        self.assertEqual(self.c.post("/api/hub/sites/farm/camera-jobs/abc123/delete", json={}).status_code, 404)
        self.assertEqual(len(FakeSite.calls), n, "an unknown action never reaches the site")
        anon = server.app.test_client()
        self.assertEqual(anon.post("/api/hub/sites/farm/camera-jobs", json=body).status_code, 401)

    def test_the_account_job_is_forwarded(self):
        body = {"action": "create", "confirm_text": "CHANGE"}
        r = self.c.post("/api/hub/sites/farm/cameras/aa:01/account", json=body)
        self.assertEqual((FakeSite.calls[-1][0], r.get_json()["echo"]), ("/api/cameras/aa%3A01/account", body))
        anon = server.app.test_client()
        self.assertEqual(anon.post("/api/hub/sites/farm/cameras/aa:01/account", json=body).status_code, 401)

    def test_a_site_refusal_passes_through_and_an_old_site_is_told_to_update(self):
        FakeSite.mode = "refuse"
        r = self.c.post("/api/hub/sites/farm/cameras/aa:01/settings", json={"section": "picture"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("turned off", r.get_json()["error"])
        FakeSite.mode = "legacy"
        self.assertEqual(self.c.post("/api/hub/sites/farm/cameras/aa:01/settings", json={}).status_code, 501)
        anon = server.app.test_client()
        self.assertEqual(anon.post("/api/hub/sites/farm/cameras/aa:01/settings", json={}).status_code, 401)

    def test_an_offline_site_still_shows_its_cameras(self):
        self.c.get("/api/hub/sites/farm/cameras")
        self.c.get("/api/hub/sites/farm/cameras/aa:01/picture")           # saved on the hub
        cameras._lists.clear()                                             # as after a hub restart …
        cameras._pics.clear()
        self.point(_free_port())                                           # … with the site gone
        j = self.c.get("/api/hub/sites/farm/cameras").get_json()
        self.assertTrue(j["stale"])
        self.assertEqual([c["key"] for c in j["cameras"]], ["aa:01", "ch:aa:09:4"])
        r = self.c.get("/api/hub/sites/farm/cameras/aa:01/picture")
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.headers["X-Picture-Stale"], r.headers["X-Picture-Route"]), ("1", "hub-saved"))
        r2 = self.c.get("/api/hub/sites/farm/cameras/ch:aa:09:4/picture")  # never fetched: nothing to show
        self.assertEqual(r2.status_code, 502)

    def test_a_site_that_cant_read_a_camera_falls_back_to_the_saved_copy(self):
        self.c.get("/api/hub/sites/farm/cameras/aa:01/picture")
        cameras._pics.clear()
        FakeSite.mode = "failed"
        r = self.c.get("/api/hub/sites/farm/cameras/aa:01/picture?fresh=1")
        self.assertEqual((r.status_code, r.headers.get("X-Picture-Stale")), (200, "1"))

    def test_saved_files_stay_in_the_camera_folder(self):
        self.c.get("/api/hub/sites/farm/cameras/aa:01/picture")
        p = cameras._pic_path("farm", "../../etc/passwd")
        self.assertTrue(os.path.realpath(p).startswith(os.path.realpath(cameras.PIC_DIR) + os.sep))


if __name__ == "__main__":
    unittest.main()
