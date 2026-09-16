"""Hub: the network-view proxy (hub/app/topology.py) against a fake site.

Run inside the hub image with a throwaway data dir:
  docker run --rm -v "$PWD/hub/app:/app:ro" -v "$PWD/tests:/tests:ro" -e HUB_DATA=/tmp/hub \
    --entrypoint python farm-netwatch-hub:local /tests/test_hub_topology.py -v
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("HUB_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("HUB_APP", "/app"))

import hubconfig    # noqa: E402
import server       # noqa: E402
import siteapi      # noqa: E402
import topology     # noqa: E402

PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8cf"
                    "c0f00f00050001ff5ca4d5f40000000049454e44ae426082")


class FakeSite(BaseHTTPRequestHandler):
    calls = []
    mode = "ok"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _record(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"null") if n else None
        FakeSite.calls.append((self.command, self.path, self.headers.get(siteapi.HEADER), body))

    def do_GET(self):
        self._record()
        if FakeSite.mode == "legacy":
            return self._send(404, b"<html>Not Found</html>", "text/html")
        if FakeSite.mode == "nokey":
            return self._send(401, {"ok": False, "error": "auth_required"})
        if self.path.startswith("/api/topology/icons/i-00000001"):
            return self._send(200, PNG, "image/png")
        if self.path.startswith("/api/topology/icons/i-00000002"):
            return self._send(200, b"<svg onload=alert(1)/>", "image/svg+xml")
        if self.path.startswith("/api/topology"):
            return self._send(200, {"ok": True, "rev": len(FakeSite.calls), "nodes": [], "groups": [], "links": []})
        return self._send(404, b"nope", "text/html")

    def do_POST(self):
        self._record()
        return self._send(200, {"ok": True, "result": {"id": "g-1"}, "graph": {"ok": True, "rev": 99, "nodes": []}})

    def do_DELETE(self):
        self._record()
        return self._send(200, {"ok": True, "result": {}})


class HubTopology(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeSite)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cfg = hubconfig.load()
        cfg["sites"] = [{"id": "farm", "name": "Farm", "vpn_ip": "127.0.0.1", "netwatch_port": cls.srv.server_port,
                         "kuma_url": "", "kuma_status_slug": "", "enabled": True}]
        hubconfig.save(cfg)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        FakeSite.calls.clear()
        FakeSite.mode = "ok"
        topology._CACHE.clear()
        topology._ICONS.clear()
        self.c = server.app.test_client()
        with self.c.session_transaction() as s:
            s["auth"] = True

    def test_needs_a_hub_login(self):
        anon = server.app.test_client()
        self.assertEqual(anon.get("/api/hub/sites/farm/topology").status_code, 401)
        self.assertEqual(anon.post("/api/hub/sites/farm/topology/groups", json={}).status_code, 401)

    def test_read_is_forwarded_with_the_key_and_cached(self):
        r = self.c.get("/api/hub/sites/farm/topology")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(FakeSite.calls[0][2], siteapi.key_for("farm"))
        self.c.get("/api/hub/sites/farm/topology")
        self.assertEqual(len(FakeSite.calls), 1, "second read within the TTL is served from cache")
        self.c.get("/api/hub/sites/farm/topology?fresh=1")
        self.assertEqual(len(FakeSite.calls), 2)
        self.c.get("/api/hub/sites/farm/topology?scope=all")
        self.assertIn("scope=all", FakeSite.calls[-1][1])
        self.assertEqual(self.c.get("/api/hub/sites/nope/topology").status_code, 404)

    def test_writes_forward_body_and_graph_and_drop_the_cache(self):
        self.c.get("/api/hub/sites/farm/topology")
        r = self.c.post("/api/hub/sites/farm/topology/nodes/02:11:00:00:00:01?graph=1", json={"group": "g-1"})
        self.assertEqual(r.status_code, 200)
        method, path, key, body = FakeSite.calls[-1]
        self.assertEqual((method, body, key), ("POST", {"group": "g-1"}, siteapi.key_for("farm")))
        self.assertTrue(path.startswith("/api/topology/nodes/02:11:00:00:00:01") and "graph=1" in path)
        # the reply's graph now answers the next read without asking the site
        n = len(FakeSite.calls)
        self.assertEqual(self.c.get("/api/hub/sites/farm/topology").get_json()["rev"], 99)
        self.assertEqual(len(FakeSite.calls), n)
        self.assertEqual(self.c.delete("/api/hub/sites/farm/topology/links/l-12345678").status_code, 200)
        self.assertEqual(FakeSite.calls[-1][0], "DELETE")

    def test_writes_must_be_json(self):
        r = self.c.post("/api/hub/sites/farm/topology/arrange", data="x=1",
                        content_type="application/x-www-form-urlencoded")
        self.assertEqual(r.status_code, 415)
        r = self.c.post("/api/hub/sites/farm/topology/suggestions/accept-all", data="", content_type="text/plain")
        self.assertEqual(r.status_code, 415)
        self.assertEqual(FakeSite.calls, [])
        self.assertEqual(self.c.post("/api/hub/sites/farm/topology/orphans/clear", json={}).status_code, 200)

    def test_only_topology_actions_are_forwarded(self):
        for bad in ("config", "groups/..", "groups/../../config", "links/.", "nodes/a%2F..", "x"):
            r = self.c.post(f"/api/hub/sites/farm/topology/{bad}", json={})
            self.assertEqual(r.status_code, 404, bad)
        self.assertEqual(FakeSite.calls, [])

    def test_old_site_and_missing_key(self):
        FakeSite.mode = "legacy"
        r = self.c.get("/api/hub/sites/farm/topology")
        self.assertEqual(r.status_code, 501)
        self.assertTrue(r.get_json()["legacy"])
        FakeSite.mode = "nokey"
        r = self.c.get("/api/hub/sites/farm/topology?fresh=1")
        self.assertEqual(r.status_code, 502)
        self.assertIn("key", r.get_json()["error"])

    def test_icons_are_raster_only_and_cached(self):
        r = self.c.get("/api/hub/sites/farm/topology/icons/i-00000001")
        self.assertEqual((r.status_code, r.mimetype), (200, "image/png"))
        self.assertIn("sandbox", r.headers["Content-Security-Policy"])
        self.c.get("/api/hub/sites/farm/topology/icons/i-00000001")
        self.assertEqual(len(FakeSite.calls), 1)
        self.assertEqual(self.c.get("/api/hub/sites/farm/topology/icons/i-00000002").status_code, 404)
        n = len(FakeSite.calls)
        self.assertIn(self.c.get("/api/hub/sites/farm/topology/icons/../../x").status_code, (404, 405))
        self.assertEqual(self.c.get("/api/hub/sites/farm/topology/icons/i-zz").status_code, 404)
        self.assertEqual(len(FakeSite.calls), n, "nothing but a well-formed icon id reaches the site")
        self.c.delete("/api/hub/sites/farm/topology/icons/i-00000001")
        self.assertNotIn(("farm", "i-00000001"), topology._ICONS)


if __name__ == "__main__":
    unittest.main()
