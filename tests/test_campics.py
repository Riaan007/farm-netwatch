"""Camera pictures (app/campics.py + app/camera_routes.py) against fake Hikvision
devices (tests/hik_fake.py): which way each camera's picture comes, the small-size
search, the NVR fallback, the one saved picture per camera, the switch that turns
it all off, and that no picture leaves the site without a login.

Run inside the site image (nothing leaves the box):
  docker run --rm --network none -v "$PWD/app:/app:ro" -v "$PWD/tests:/tests:ro" -e NETWATCH_DATA=/tmp/nw \\
    -e PYTHONDONTWRITEBYTECODE=1 --entrypoint python farm-netwatch:netcfg /tests/test_campics.py -v
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import campics      # noqa: E402
import creds        # noqa: E402
import hikisapi     # noqa: E402
from hik_fake import Device   # noqa: E402

HIK = "Hangzhou Hikvision Digital Technology"


class Rig(unittest.TestCase):
    """A wide camera with its own login, an old (V5.4) camera with its own login, a
    camera with no login, and an NVR with all three plus two cameras the scan
    can't see (one connected, one the NVR has lost)."""

    def setUp(self):
        hikisapi._seen.clear()
        shutil.rmtree(campics.PIC_DIR, ignore_errors=True)
        for k in list(creds.keys_with_creds()):
            creds.set_(k)
        self.wide = Device(main=(5120, 1440), sub=(1920, 536), sizes=[(640, 360), (1920, 536)]).start()
        self.old = Device(main=(2048, 1536), sub=(704, 576), size_mode="503").start()
        self.nolog = Device().start()
        self.nvr = Device(channels=[]).start()
        self.nvr.channels += [
            {"id": "1", "name": "Gate", "ip": self.wide.addr, "online": True},
            {"id": "2", "name": "Shed", "ip": self.old.addr, "online": True},
            {"id": "3", "name": "Dam", "ip": self.nolog.addr, "online": True},
            {"id": "4", "name": "Pump", "ip": "10.9.9.4", "online": True},
            {"id": "5", "name": "Silo", "ip": "10.9.9.5", "online": False},
            {"id": "6", "name": "Dam thermal", "ip": self.nolog.addr, "online": True},   # a 2nd view of camera 3
        ]
        self.devs = [
            {"key": "aa:01", "ip": self.wide.addr, "category": "camera", "vendor": HIK, "online": True, "ports": [80, 554, 8000], "name": "Gate cam"},
            {"key": "aa:02", "ip": self.old.addr, "category": "camera", "vendor": HIK, "online": True, "ports": [80, 554, 8000]},
            {"key": "aa:03", "ip": self.nolog.addr, "category": "camera", "vendor": HIK, "online": True, "ports": [80, 554, 8000]},
            {"key": "aa:09", "ip": self.nvr.addr, "category": "nvr", "vendor": HIK, "online": True, "ports": [80, 554, 8000], "model": "DS-7732NXI-I4/S"},
            {"key": "bb:01", "ip": "10.0.0.50", "category": "printer", "vendor": "HP", "online": True},
        ]
        for k in ("aa:01", "aa:02", "aa:09"):
            creds.set_(k, "admin", "pw")
        self.cfg = {"features": {"camera_pictures": True}, "camera": {"refresh_min": 30, "save_hours": 6}}
        self.reg = {}
        self.e = campics.Engine()
        self.e.start(lambda: [dict(d) for d in self.devs], lambda: self.reg, lambda: {}, cfg=lambda: self.cfg, thread=False)
        self.e._read_nvrs()

    def tearDown(self):
        for d in (self.wide, self.old, self.nolog, self.nvr):
            d.stop()

    def cams(self):
        return {c["key"]: c for c in self.e.listing()["cameras"]}

    def pic(self, key, size="thumb", fresh=False):
        p, flags, err, status = self.e.picture(key, size, fresh)
        return p, flags, err, status


class Inventory(Rig):
    def test_each_camera_gets_its_way_to_a_picture(self):
        c = self.cams()
        self.assertEqual(c["aa:01"]["route"], "direct")
        self.assertEqual(c["aa:03"]["route"], "nvr")
        self.assertEqual(c["aa:03"]["nvr"]["channel"], "3")
        self.assertEqual(c["aa:01"]["name"], "Gate cam")         # the operator's name wins
        self.assertEqual(c["aa:03"]["name"], "Dam")              # else the NVR's channel name
        self.assertNotIn("bb:01", c)                             # a printer is not a camera
        self.assertNotIn("aa:09", c)                             # the recorder is listed apart

    def test_nvr_channels_the_scan_cant_see_are_listed_too(self):
        c = self.cams()
        pump, silo = c["ch:aa:09:4"], c["ch:aa:09:5"]
        self.assertEqual((pump["kind"], pump["route"], pump["ip"]), ("channel", "nvr", "10.9.9.4"))
        self.assertFalse(silo["online"])
        self.assertEqual(silo["why"], "offline")

    def test_a_camera_with_two_views_gets_a_tile_for_each(self):
        c = self.cams()
        self.assertEqual(c["aa:03"]["nvr"]["channel"], "3")                # the camera itself: its first channel
        self.assertEqual((c["ch:aa:09:6"]["name"], c["ch:aa:09:6"]["route"]), ("Dam thermal", "nvr"))
        p, *_ = self.pic("ch:aa:09:6")
        self.assertEqual(p.route, "nvr")
        self.assertTrue(any(h[1] == "/ISAPI/Streaming/channels/602/picture" for h in self.nvr.hits))

    def test_recorder_summary(self):
        r = self.e.listing()["recorders"][0]
        self.assertEqual((r["key"], r["channels"], r["channels_online"], r["channels_offline"]), ("aa:09", 6, 5, 1))

    def test_no_login_anywhere_says_so(self):
        creds.set_("aa:09")                   # the NVR's login removed
        self.e._list = (0, None)
        c = self.cams()
        self.assertIsNone(c["aa:03"]["route"])
        self.assertEqual(c["aa:03"]["why"], "no_login")
        self.assertTrue(c["aa:03"]["why_text"])

    def test_listing_never_carries_internal_fields_or_secrets(self):
        out = repr(self.e.listing())
        self.assertNotIn("_d", [k for c in self.e.listing()["cameras"] for k in c])
        self.assertNotIn("'pw'", out)


class Pictures(Rig):
    def test_a_wide_camera_is_asked_for_a_size_it_supports_and_it_is_remembered(self):
        p, flags, err, st = self.pic("aa:01")
        self.assertEqual(st, 200)
        self.assertEqual((p.w, p.h, p.route), (640, 360, "direct"))
        self.assertEqual(self.cams()["aa:01"]["aspect"], round(5120 / 1440, 4))   # drawn in its own shape
        before = len(self.wide.hits)
        time.sleep(campics.FRESH_MIN_S + 0.05)
        p, *_ = self.pic("aa:01", fresh=True)
        self.assertEqual((p.w, p.h), (640, 360))
        pics = [h for h in self.wide.hits[before:] if "/picture" in h[1] and h[2]]   # h[2]: carried the login
        self.assertEqual(len(pics), 1, "the second picture should go straight to the remembered size")
        self.assertIn("videoResolutionWidth=640", pics[0][1])

    def test_v57_firmware_refuses_sizes_but_sends_its_sub_stream_picture(self):
        cam = Device(size_mode="403", sub_picture=True, main=(2688, 1520), sub=(640, 360)).start()
        try:
            self.devs.append({"key": "aa:05", "ip": cam.addr, "category": "camera", "vendor": HIK, "online": True, "ports": [80, 554, 8000]})
            creds.set_("aa:05", "admin", "pw")
            self.e._list = (0, None)
            p, *_ = self.pic("aa:05")
            self.assertEqual((p.w, p.h, p.route), (640, 360, "direct"))
            self.assertEqual(self.e._caps["aa:05"]["thumb"], "sub")
            time.sleep(campics.FRESH_MIN_S + 0.05)
            before = len(cam.hits)
            p, *_ = self.pic("aa:05", fresh=True)
            asked = [h[1] for h in cam.hits[before:] if "/picture" in h[1] and h[2]]
            self.assertEqual(asked, ["/ISAPI/Streaming/channels/102/picture"])
        finally:
            cam.stop()

    def test_old_firmware_gets_its_full_picture_then_goes_through_the_nvr(self):
        p, *_ = self.pic("aa:02")
        self.assertEqual((p.w, p.h, p.route), (2048, 1536, "direct"))   # 503 to any size: the full one
        self.assertEqual(self.cams()["aa:02"]["route"], "nvr")
        time.sleep(campics.FRESH_MIN_S + 0.05)       # a younger picture is served even to fresh=1
        p, *_ = self.pic("aa:02", fresh=True)
        self.assertEqual((p.w, p.h, p.route), (704, 576, "nvr"))
        self.assertTrue(any(h[1] == "/ISAPI/Streaming/channels/202/picture" for h in self.nvr.hits))

    def test_through_the_nvr_small_and_full(self):
        p, *_ = self.pic("aa:03")
        self.assertEqual((p.w, p.h, p.route), (704, 576, "nvr"))
        p, *_ = self.pic("aa:03", size="full")
        self.assertEqual((p.w, p.h), (1920, 1088))
        self.assertTrue(any(h[1].startswith("/ISAPI/Streaming/channels/301/picture?") for h in self.nvr.hits))
        self.assertEqual(self.nolog.hits, [], "a camera with no login was contacted directly")

    def test_an_old_nvr_that_refuses_the_small_picture_sends_the_main_one(self):
        self.nvr.channels[3]["no_sub"] = True              # channel 4 (Pump): 400 on 402/picture
        p, *_ = self.pic("ch:aa:09:4")
        self.assertEqual((p.w, p.route), (704, "nvr"))
        self.assertEqual(self.e._caps["ch:aa:09:4"]["nvr_pic"], "main")
        time.sleep(campics.FRESH_MIN_S + 0.05)
        before = len(self.nvr.hits)
        self.pic("ch:aa:09:4", fresh=True)
        asked = [h[1] for h in self.nvr.hits[before:] if "/picture" in h[1] and h[2]]
        self.assertEqual(asked, ["/ISAPI/Streaming/channels/401/picture"], "the remembered main picture was not used directly")

    def test_a_refused_camera_login_falls_back_to_the_nvr_and_is_never_tried_again(self):
        creds.set_("aa:01", "admin", "wrong")
        self.e._list = (0, None)
        p, *_ = self.pic("aa:01")
        self.assertEqual(p.route, "nvr")
        self.assertEqual(self.wide.logins(), 1)
        self.e._list = (0, None)
        c = self.cams()["aa:01"]
        self.assertEqual(c["route"], "nvr")
        for _ in range(3):
            self.pic("aa:01", fresh=True)
            time.sleep(campics.FRESH_MIN_S + 0.05)
        self.assertEqual(self.wide.logins(), 1, "the refused camera login was sent again")

    def test_a_cached_picture_serves_views_without_asking_the_camera(self):
        self.pic("aa:01")
        n = len(self.wide.hits)
        for _ in range(5):
            p, *_ = self.pic("aa:01")
        self.assertEqual(len(self.wide.hits), n)

    def test_an_answer_that_is_not_a_picture_fails_cleanly(self):
        self.wide.picture_body = b"<html>login</html>"
        creds.set_("aa:09")                   # no NVR to fall back to
        self.e._list = (0, None)
        p, flags, err, st = self.pic("aa:01")
        self.assertIsNone(p)
        self.assertEqual(st, 502)
        self.assertEqual(err["why"], "failed")
        self.assertEqual(self.cams()["aa:01"]["why"], "failed")


class Saved(Rig):
    def test_one_picture_per_camera_is_kept_and_not_rewritten_every_refresh(self):
        self.pic("aa:01")
        path = campics.Engine._path("aa:01")
        self.assertTrue(os.path.exists(path))
        m = os.path.getmtime(path)
        time.sleep(campics.FRESH_MIN_S + 0.05)
        self.pic("aa:01", fresh=True)
        self.assertEqual(os.path.getmtime(path), m, "the saved picture was rewritten within save_hours")
        self.assertFalse(os.path.exists(campics.Engine._path("aa:01:full")))

    def test_an_offline_camera_still_shows_where_it_points(self):
        self.pic("aa:03")
        e2 = campics.Engine()                 # a restart: memory gone, the saved picture remains
        devs = [dict(d, online=(d["key"] != "aa:03")) for d in self.devs]
        e2.start(lambda: devs, lambda: self.reg, lambda: {}, cfg=lambda: self.cfg, thread=False)
        p, flags, err, st = e2.picture("aa:03")
        self.assertEqual(st, 200)
        self.assertEqual((p.route, p.w), ("saved", 704))
        self.assertTrue(flags["stale"])
        self.assertEqual({c["key"]: c for c in e2.listing()["cameras"]}["aa:03"]["picture"]["route"], "saved")

    def test_switching_pictures_off_refuses_them_and_deletes_the_saved_ones(self):
        self.pic("aa:01")
        self.cfg["features"]["camera_pictures"] = False
        p, flags, err, st = self.pic("aa:01")
        self.assertEqual((p, st, err["why"]), (None, 403, "off"))
        self.assertFalse(self.e.listing()["enabled"])
        self.assertEqual(self.e.listing()["cameras"], [])
        self.e._round()
        self.assertFalse(os.path.exists(campics.Engine._path("aa:01")))


class Routes(Rig):
    """The HTTP side: nothing without the site login or the hub key."""

    def setUp(self):
        super().setUp()
        import siteauth
        from flask import Flask
        import camera_routes
        hub_key = "k" * 40
        siteauth.set_hub_key(hub_key)
        camera_routes.engine = self.e
        app = Flask(__name__)
        siteauth.init_app(app)
        app.register_blueprint(camera_routes.bp)
        self.c = app.test_client()
        self.key = {siteauth.HEADER: hub_key}

    def test_nothing_without_a_login(self):
        self.assertEqual(self.c.get("/api/cameras").status_code, 401)
        self.assertEqual(self.c.get("/api/cameras/aa:01/picture").status_code, 401)
        self.assertEqual(self.c.post("/api/cameras/refresh").status_code, 401)
        self.assertEqual(self.wide.hits, [])

    def test_the_hub_key_gets_the_list_and_a_picture(self):
        j = self.c.get("/api/cameras", headers=self.key).get_json()
        self.assertTrue(j["enabled"])
        self.assertIn("aa:01", [c["key"] for c in j["cameras"]])
        r = self.c.get("/api/cameras/aa:01/picture", headers=self.key)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "image/jpeg")
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertEqual((r.headers["X-Picture-Route"], r.headers["X-Picture-Size"], r.headers["X-Picture-Stale"]),
                         ("direct", "640x360", "0"))
        self.assertTrue(r.data.startswith(b"\xff\xd8"))

    def test_unknown_camera(self):
        r = self.c.get("/api/cameras/zz:99/picture", headers=self.key)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["why"], "unknown")


class Jpeg(unittest.TestCase):
    def test_size_from_the_frame_header(self):
        from hik_fake import jpeg
        self.assertEqual(campics.jpeg_size(jpeg(640, 360, pad=70000)), (640, 360))
        self.assertIsNone(campics.jpeg_size(b"<html>"))
        self.assertIsNone(campics.jpeg_size(b"\xff\xd8\xff\xda\x00\x02"))

    def test_candidates_follow_the_camera_shape(self):
        self.assertEqual(campics._candidates({"main": (2592, 1944), "sub": (640, 480)})[0], (640, 480))
        self.assertEqual(campics._candidates({"main": (5120, 1440), "sub": (1920, 536)}),
                         [(640, 360), (640, 480), (704, 576), (1920, 536)])


if __name__ == "__main__":
    unittest.main()
