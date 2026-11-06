"""Live video through the hub (Phase 7, part 1: hub/app/cameras.py live routes).

A fake site Pi that streams MP4 bytes. The hub passes them on, needs its login, keeps
at most LIVE_PER_SITE views per site (a slot is freed when a viewer leaves, even
before any bytes), and says "too old" for a site without the route.

Run inside the hub image:
  docker run --rm --network none -v "$PWD:/src:ro" -v "$PWD/tests:/tests:ro" -e HUB_DATA=/tmp/hub \\
    -e HUB_APP=/src/hub/app -e PYTHONDONTWRITEBYTECODE=1 --entrypoint python farm-netwatch-hub:local \\
    /tests/test_hub_live.py -v
"""
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

import cameras      # noqa: E402
import hubconfig    # noqa: E402
import server       # noqa: E402
import siteapi      # noqa: E402


class Site(BaseHTTPRequestHandler):
    old = False
    keys = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        Site.keys.append(self.headers.get(siteapi.HEADER))
        if Site.old or not self.path.split("?")[0].endswith("/live.mp4"):
            body = b"nope" if Site.old else json.dumps({"ok": True, "codec": "H.265", "max_s": 300}).encode()
            self.send_response(404 if Site.old else 200)
            self.send_header("Content-Type", "text/html" if Site.old else "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.end_headers()
        try:
            for _ in range(50):
                self.wfile.write(b"\x00\x00\x10\x00moof" + b"\x01" * 4088)
                self.wfile.flush()
                time.sleep(0.01)
        except (BrokenPipeError, ConnectionResetError):
            pass


class HubLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Site)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        cfg = hubconfig.load()
        cfg["sites"] = [{"id": "farm", "name": "Farm", "vpn_ip": "127.0.0.1", "netwatch_port": self.srv.server_port,
                         "kuma_url": "", "kuma_status_slug": "", "enabled": True}]
        hubconfig.save(cfg)
        Site.old = False
        cameras._live_count.clear()
        self.c = server.app.test_client()
        with self.c.session_transaction() as s:
            s["auth"] = True
        self.url = "/api/hub/sites/farm/cameras/aa:01/live.mp4"

    def test_passes_the_video_on(self):
        r = self.c.get(self.url)
        self.assertEqual((r.status_code, r.mimetype), (200, "video/mp4"))
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertEqual(len(r.get_data()), 50 * 4096)
        r.close()
        self.assertEqual(cameras._live_count["farm"], 0)
        self.assertTrue(Site.keys[-1], "the hub key goes to the site")
        self.assertEqual(self.c.get("/api/hub/sites/farm/cameras/aa:01/live").get_json()["codec"], "H.265")

    def test_needs_a_hub_login(self):
        self.assertEqual(server.app.test_client().get(self.url).status_code, 401)

    def test_two_per_site_and_slots_come_back(self):
        a = self.c.get(self.url, buffered=False)
        b = self.c.get(self.url, buffered=False)
        self.assertEqual(self.c.get(self.url).status_code, 429)
        a.close()                     # left before reading anything
        c = self.c.get(self.url, buffered=False)
        self.assertEqual(c.status_code, 200)
        b.close(), c.close()
        self.assertEqual(cameras._live_count["farm"], 0)

    def test_old_site(self):
        Site.old = True
        r = self.c.get(self.url)
        self.assertEqual(r.status_code, 501)
        self.assertIn("too old", r.get_json()["error"])
        self.assertEqual(cameras._live_count["farm"], 0)


if __name__ == "__main__":
    unittest.main()
