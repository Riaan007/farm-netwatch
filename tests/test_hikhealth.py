"""Camera and recorder health (app/hikhealth.py) against fake devices: disks,
recording, days of footage, clocks/time zones, passwords — where each finding
shows, what reaches the Problems panel, and that alerts fire once and recover.

Run inside the site image (nothing leaves the box):
  docker run --rm --network none -v "$PWD/app:/app:ro" -v "$PWD/tests:/tests:ro" -e NETWATCH_DATA=/tmp/nw \\
    -e PYTHONDONTWRITEBYTECODE=1 --entrypoint python farm-netwatch:netcfg /tests/test_hikhealth.py -v
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import campics      # noqa: E402
import creds        # noqa: E402
import hikhealth    # noqa: E402
import hikisapi     # noqa: E402
from hik_fake import Device   # noqa: E402

HIK = "Hangzhou Hikvision Digital Technology"
DAY = 86400


class Rig(unittest.TestCase):
    def setUp(self):
        hikisapi._seen.clear()
        for k in list(creds.keys_with_creds()):
            creds.set_(k)
        for f in (hikhealth.FOOTAGE_PATH, hikhealth.ALERT_PATH):
            if os.path.exists(f):
                os.remove(f)
        self.cam = Device().start()
        self.nvr = Device(channels=[]).start()
        self.nvr.channels += [{"id": "1", "name": "Gate", "ip": self.cam.addr, "online": True},
                              {"id": "2", "name": "Shed", "ip": "10.9.9.2", "online": True},
                              {"id": "3", "name": "Old", "ip": "10.9.9.3", "online": False}]
        now = time.time()
        self.nvr.oldest = {"1": now - 20 * DAY, "2": now - 5 * DAY}
        self.devs = [{"key": "aa:01", "ip": self.cam.addr, "category": "camera", "vendor": HIK, "online": True, "ports": [80, 8000]},
                     {"key": "aa:09", "ip": self.nvr.addr, "category": "nvr", "vendor": HIK, "online": True, "ports": [80, 8000], "name": "House NVR"}]
        creds.set_("aa:01", "admin", "pw")
        creds.set_("aa:09", "admin", "pw")
        self.cfg = {"features": {"camera_pictures": True}, "camera": {"footage_days": 14, "time_zone": "CST-2:00:00"}, "alerts": {}}
        self.e = campics.Engine()
        self.e.start(lambda: [dict(d) for d in self.devs], lambda: {}, lambda: {}, cfg=lambda: self.cfg, thread=False)
        self.e._read_nvrs()
        self.h = hikhealth.Health()
        self.h.footage, self.h._alerts = {}, {}
        self.pushes = []
        self.p = mock.patch.object(hikhealth.notify, "push", side_effect=lambda cfg, title, msg, **kw: self.pushes.append((title, msg)))
        self.p.start()
        self.h.start(self.e, lambda: self.cfg, thread=False)

    def tearDown(self):
        self.p.stop()
        self.cam.stop()
        self.nvr.stop()

    def listing(self):
        self.e._list = (0, None)
        return self.e.listing()

    def rec(self):
        return self.listing()["recorders"][0]

    def camh(self, key):
        return {c["key"]: c for c in self.listing()["cameras"]}[key]


class Health(Rig):
    def test_a_healthy_recorder_reports_footage_and_disks(self):
        self.h.run_once(force=True)
        r = self.rec()
        self.assertEqual(r["disks"][0]["status"], "ok")
        self.assertAlmostEqual(self.camh("aa:01")["footage_days"], 20, delta=0.2)
        self.assertTrue(self.camh("aa:01")["recording"])
        self.assertNotIn("disk", [h["id"] for h in r["health"]])

    def test_footage_below_target_is_a_warning_on_camera_and_recorder(self):
        self.h.run_once(force=True)
        shed = self.camh("ch:aa:09:2")
        self.assertIn("footage", [h["id"] for h in shed["health"]])
        self.assertIn("footage", [h["id"] for h in self.rec()["health"]])
        self.assertNotIn("footage", [h["id"] for h in self.camh("aa:01")["health"]])

    def test_a_connected_camera_not_recording_is_critical_and_alerts_once(self):
        self.nvr.channels[0]["record"] = False
        self.h.run_once(force=True)
        self.assertIn("rec", [h["id"] for h in self.camh("aa:01")["health"] if h["sev"] == "crit"])
        self.assertEqual(len([p for p in self.pushes if "not recording" in p[1]]), 1)
        self.h.run_once(force=True)
        self.assertEqual(len([p for p in self.pushes if "not recording" in p[1]]), 1, "re-alerted within 6 h")
        self.nvr.channels[0]["record"] = True
        self.h.run_once(force=True)
        self.assertTrue(any(t == "Cameras recovered" for t, _ in self.pushes))

    def test_a_disk_fault_is_critical_and_reaches_the_problems_panel(self):
        self.nvr.disks = [("ok", 5723166), ("error", 3815447)]
        self.h.run_once(force=True)
        self.assertIn("disk", [h["id"] for h in self.rec()["health"] if h["sev"] == "crit"])
        probs = self.h.problems(self.listing())
        self.assertTrue(any(p["type"] == "camera" and p["severity"] == "high" and "error" in p["detail"] for p in probs))

    def test_wrong_time_zone_is_one_finding_not_two(self):
        self.cam.tz, self.cam.local_offset, self.cam.clock_offset = "CST-8:00:00", "+08:00", -6 * 3600
        self.h.run_once(force=True)
        ids = [h["id"] for h in self.camh("aa:01")["health"]]
        self.assertIn("tz", ids)
        self.assertNotIn("clock", ids)

    def test_wrong_time_zone_plus_a_few_minutes_names_only_the_minutes(self):
        # Tri-Lectro: China time AND 6 min slow read as "clock 6.1 h slow" + tz.
        self.cam.tz, self.cam.local_offset, self.cam.clock_offset = "CST-8:00:00", "+08:00", -6 * 3600 - 360
        self.h.run_once(force=True)
        hs = {h["id"]: h["text"] for h in self.camh("aa:01")["health"]}
        self.assertIn("tz", hs)
        self.assertIn("6 min slow", hs["clock"])

    def test_a_not_recording_channel_is_named_on_the_recorder(self):
        self.nvr.channels[1]["record"] = False
        self.h.run_once(force=True)
        rec = [h["text"] for h in self.rec()["health"] if h["id"] == "rec"]
        self.assertIn("D2 Shed", rec[0])

    def test_a_slow_footage_search_gives_up_and_keeps_the_other_readings(self):
        # Old NVRs (V4.x) time out on a whole-disk search: the disks, clock and channel
        # readings must survive, the search stops after SEARCH_GIVE_UP and waits a day.
        self.nvr.search_delay = 1.0
        with mock.patch.object(hikhealth, "SEARCH_TIMEOUT", (2, 0.3)):
            self.h.run_once(force=True)
        r = self.rec()
        self.assertEqual(r["disks"][0]["status"], "ok")
        self.assertIsNotNone(self.camh("aa:01")["recording"])
        self.assertEqual(self.nvr.searches, hikhealth.SEARCH_GIVE_UP)
        self.assertIn("aa:09", self.h.footage, "a partial search is saved so it is not retried every 15 min")
        self.assertTrue(any(h["id"] == "read" and "footage" in h["text"] for h in r["health"]))

    def test_one_reading_timing_out_does_not_lose_the_rest(self):
        with mock.patch.object(hikhealth, "read_password", side_effect=hikisapi.Unreachable("ReadTimeout")):
            self.h.run_once(force=True)
        r = self.rec()
        self.assertEqual(r["disks"][0]["status"], "ok")
        self.assertAlmostEqual(self.camh("aa:01")["footage_days"], 20, delta=0.2)
        self.assertTrue(any(h["id"] == "read" and "pw: ReadTimeout" in h["text"] for h in r["health"]))

    def test_a_clock_off_by_minutes_is_flagged(self):
        self.cam.clock_offset = 600
        self.h.run_once(force=True)
        self.assertIn("clock", [h["id"] for h in self.camh("aa:01")["health"]])

    def test_a_default_password_is_critical(self):
        self.nvr.pw_default = True
        self.h.run_once(force=True)
        self.assertIn("pw", [h["id"] for h in self.rec()["health"] if h["sev"] == "crit"])

    def test_old_nvrs_number_ip_channels_from_33(self):
        self.nvr.chan_base = 32
        self.nvr.channels[0]["record"] = False
        self.h.run_once(force=True)
        self.assertFalse(self.camh("aa:01")["recording"])

    def test_footage_days_survive_a_restart(self):
        self.h.run_once(force=True)
        h2 = hikhealth.Health()
        self.assertIn("aa:09", h2.footage)

    def test_a_recorder_login_refused_is_not_tried_again(self):
        before = self.nvr.logins()                     # setup read its channel list with the good login
        creds.set_("aa:09", "admin", "wrong")
        self.e._list = (0, None)
        self.h.run_once(force=True)
        self.h.run_once(force=True)
        self.assertLessEqual(self.nvr.logins() - before, 1)


if __name__ == "__main__":
    unittest.main()
