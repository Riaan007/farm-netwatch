"""Live video — the site side (Phase 7, part 1: app/camlive.py), with a fake go2rtc.

go2rtc starts only for a viewer, gets the camera's address and login over loopback
(never in a file: whatever it writes is wiped), each view ends after MAX_S or when the
viewer leaves, at most MAX_VIEWS run, and go2rtc stops when nobody watches.

Run inside the site image (nothing leaves the box):
  docker run --rm --network none -v "$PWD/app:/app:ro" -v "$PWD/tests:/tests:ro" -e NETWATCH_DATA=/tmp/nw \\
    -e PYTHONDONTWRITEBYTECODE=1 --entrypoint python farm-netwatch:netcfg /tests/test_camlive.py -v
"""
import json
import os
import sys
import tempfile
import time
import unittest

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import campics      # noqa: E402
import camlive      # noqa: E402
import creds        # noqa: E402
import hikisapi     # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from hik_fake import Device  # noqa: E402

HIK = "Hangzhou Hikvision Digital Technology"


class Rig(unittest.TestCase):
    def setUp(self):
        hikisapi._seen.clear()
        for k in list(creds.keys_with_creds()):
            creds.set_(k)
        camlive.BIN = [sys.executable, os.path.join(HERE, "go2rtc_fake.py")]
        camlive.MAX_S, camlive.IDLE_STOP_S, camlive.MAX_VIEWS = 300, 60, 2
        self.cam = Device(password="p@ss word").start()
        self.devs = [{"key": "aa:01", "ip": self.cam.addr, "category": "camera", "vendor": HIK, "online": True,
                      "ports": [80, 554, 8000], "name": "Gate cam", "model": "DS-2CD2046G2-IU", "firmware": "V5.7.3"}]
        creds.set_("aa:01", "admin", "p@ss word")
        self.cfg = {"features": {"camera_pictures": True}, "camera": {}, "alerts": {}}
        self.e = campics.Engine()
        self.e.start(lambda: [dict(d) for d in self.devs], lambda: {}, lambda: {}, cfg=lambda: self.cfg, thread=False)
        self.live = camlive.Live(self.e)

    def tearDown(self):
        self.live._stop()
        self.cam.stop()

    def streams(self):
        st, body = self.live._call("GET", "/api/streams")
        self.assertEqual(st, 200)
        return json.loads(body)


class TestLive(Rig):
    def test_info(self):
        i = self.live.info("aa:01")
        self.assertEqual((i["size"], i["max_s"], i["busy"], i["available"]), ("640x360", 300, 0, True))
        self.assertIsNone(self.live.proc, "asking does not start the relay")

    def test_view_runs_and_cleans_up(self):
        name, it = self.live.open("aa:01", who="t")
        first = next(it)
        self.assertTrue(first.startswith(b"\x00\x00\x00\x18ftyp"))
        src = self.streams()[name]
        self.assertTrue(src.startswith("rtsp://admin:p%40ss%20word@"), "login passed over loopback, URL-safe")
        self.assertTrue(src.endswith("/Streaming/Channels/102"), "the sub-stream")
        self.assertEqual(os.listdir(self.live.cwd), ["api.sock"], "no config file: go2rtc can save nothing")
        self.assertEqual(os.stat(self.live.cwd).st_mode & 0o777, 0o700, "only root reaches its API")
        args = " ".join(self.live.proc.args)
        self.assertNotIn("p%40ss", args, "no login on its command line")
        self.assertNotIn("p@ss", args)
        self.assertEqual(json.loads(args.split("-config ", 1)[1])["api"]["listen"], "", "no network port")
        it.close()
        self.assertEqual(self.live.views, {})
        self.assertEqual(self.streams(), {})

    def test_two_at_most(self):
        a = self.live.open("aa:01")[1]
        b = self.live.open("aa:01")[1]
        next(a), next(b)
        with self.assertRaises(camlive.LiveError) as cm:
            self.live.open("aa:01")
        self.assertEqual(cm.exception.status, 429)
        a.close()
        c = self.live.open("aa:01")[1]
        next(c)
        b.close(), c.close()

    def test_stops_after_max_s(self):
        camlive.MAX_S = 0.3
        _n, it = self.live.open("aa:01")
        t0 = time.time()
        n = sum(len(x) for x in it)
        self.assertLess(time.time() - t0, 3)
        self.assertGreater(n, 0)
        self.assertEqual(self.live.views, {})

    def test_viewer_gone_before_any_bytes(self):
        name, it = self.live.open("aa:01")
        self.live.close(name)          # what the route's call_on_close does
        self.assertEqual(self.live.views, {})
        self.assertEqual(self.streams(), {})

    def test_relay_stops_when_nobody_watches(self):
        camlive.IDLE_STOP_S = 0.2
        _n, it = self.live.open("aa:01")
        next(it)
        proc = self.live.proc
        it.close()
        time.sleep(0.8)
        self.assertIsNotNone(proc.poll(), "go2rtc stopped")
        self.assertIsNone(self.live.proc)

    def test_refusals(self):
        self.cfg["features"]["camera_pictures"] = False
        with self.assertRaises(camlive.LiveError) as cm:
            self.live.open("aa:01")
        self.assertEqual(cm.exception.status, 403)
        self.cfg["features"]["camera_pictures"] = True
        with self.assertRaises(camlive.LiveError) as cm:
            self.live.open("zz:99")
        self.assertEqual(cm.exception.status, 404)
        with self.assertRaises(camlive.LiveError):
            self.live.open("aa:01", view=3)
        camlive.BIN = ["/nonexistent/go2rtc"]
        with self.assertRaises(camlive.LiveError) as cm:
            self.live.open("aa:01")
        self.assertEqual(cm.exception.status, 501)
        self.assertEqual(self.live.views, {})


if __name__ == "__main__":
    unittest.main()
