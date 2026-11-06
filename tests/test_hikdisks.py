"""Disks: read a device's SD card or hard disks, and format one (Phase 6, part 3: app/hikdisks.py).

Formatting is risky work: "Risky camera work", the typed word AND a tick that every
recording on the disk is deleted. The job follows the device's progress and reads
the disk again: it must say "ok".

Run inside the site image (nothing leaves the box):
  docker run --rm --network none -v "$PWD/app:/app:ro" -v "$PWD/tests:/tests:ro" -e NETWATCH_DATA=/tmp/nw \\
    -e PYTHONDONTWRITEBYTECODE=1 --entrypoint python farm-netwatch:netcfg /tests/test_hikdisks.py -v
"""
import os
import sys
import tempfile
import time
import unittest

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import campics      # noqa: E402
import creds        # noqa: E402
import hikdisks     # noqa: E402
import hikisapi     # noqa: E402
import hiksettings  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hik_fake import Device  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
HIK = "Hangzhou Hikvision Digital Technology"
hikdisks.POLL_S = 0.05


class Rig(unittest.TestCase):
    def setUp(self):
        hikisapi._seen.clear()
        for k in list(creds.keys_with_creds()):
            creds.set_(k)
        self.cam = Device().start().load_settings(os.path.join(HERE, "fixtures", "hik_settings"))
        self.cam.disks = [("unformatted", 30560)]
        self.devs = [{"key": "aa:01", "ip": self.cam.addr, "category": "camera", "vendor": HIK, "online": True,
                      "ports": [80, 8000], "name": "Living cam", "model": "DS-2CD2955FWD-IS", "firmware": "V5.4.52"}]
        creds.set_("aa:01", "admin", "pw")
        self.cfg = {"features": {"camera_pictures": True, "camera_manage": True, "camera_risky": True},
                    "camera": {}, "alerts": {}}
        self.e = campics.Engine()
        self.e.start(lambda: [dict(d) for d in self.devs], lambda: {}, lambda: {}, cfg=lambda: self.cfg, thread=False)
        self.hist = []
        self.s = hiksettings.Settings().start(self.e, lambda: self.cfg, on_change=lambda d, e: self.hist.append(e),
                                              log_path=tempfile.mktemp(suffix=".json"))
        self.d = hikdisks.Disks(self.s)

    def tearDown(self):
        self.cam.stop()

    def run_job(self, disk="1"):
        self.d.format("aa:01", disk, who="t", confirm_text="CHANGE", wipe_ok=True)
        end = time.time() + 20
        while time.time() < end:
            j = self.d.job("aa:01")
            if j["state"] == "done":
                return j
            time.sleep(0.05)
        self.fail("the job did not end")


class TestDisks(Rig):
    def test_list(self):
        r = self.d.list("aa:01")
        [x] = r["disks"]
        self.assertEqual((x["id"], x["status"], x["capacity_gb"]), ("1", "unformatted", 29.8))
        self.assertIn("not formatted", x["plain"])
        self.assertEqual(self.cam.puts, [], "reading writes nothing")

    def test_gates(self):
        with self.assertRaises(hiksettings.SettingsError):
            self.d.format("aa:01", "1", confirm_text="", wipe_ok=True)
        with self.assertRaises(hikdisks.DiskError) as cm:
            self.d.format("aa:01", "1", confirm_text="CHANGE", wipe_ok=False)
        self.assertEqual(cm.exception.extra["needs"], "wipe_ok")
        with self.assertRaises(hikdisks.DiskError) as cm:
            self.d.format("aa:01", "2", confirm_text="CHANGE", wipe_ok=True)
        self.assertEqual(cm.exception.status, 404)
        self.cfg["features"]["camera_risky"] = False
        with self.assertRaises(hiksettings.SettingsError):
            self.d.format("aa:01", "1", confirm_text="CHANGE", wipe_ok=True)
        self.assertFalse(hasattr(self.cam, "formats"), "nothing was formatted")

    def test_format(self):
        j = self.run_job()
        self.assertEqual(j["result"], "ok", j)
        self.assertEqual(self.cam.formats, [1])
        self.assertEqual(j["after"]["status"], "ok")
        self.assertEqual(self.hist[-1]["section"], "disk_format")
        self.assertIn("not formatted", self.hist[-1]["fields"][0]["before"])
        self.assertTrue(self.s._risky_lock.acquire(blocking=False))
        self.s._risky_lock.release()

    def test_refused(self):
        self.cam.format_mode = "refuse"
        j = self.run_job()
        self.assertEqual(j["result"], "failed")
        self.assertIn("Nothing was deleted", j["message"])

    def test_still_bad_after(self):
        self.cam.format_mode = "bad"
        j = self.run_job()
        self.assertEqual(j["result"], "failed")
        self.assertIn("replace", j["message"])

    def test_one_at_a_time(self):
        self.cam.format_s = 1.0
        self.d.format("aa:01", "1", confirm_text="CHANGE", wipe_ok=True)
        with self.assertRaises(hikdisks.DiskError) as cm:
            self.d.format("aa:01", "1", confirm_text="CHANGE", wipe_ok=True)
        self.assertEqual(cm.exception.status, 409)
        end = time.time() + 20
        while self.d.job("aa:01")["state"] != "done" and time.time() < end:
            time.sleep(0.05)


if __name__ == "__main__":
    unittest.main()
