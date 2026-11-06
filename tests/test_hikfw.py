"""Firmware updates — the site side (Phase 6, part 2: app/hikfw.py).

The hub sends the file in pieces (they resume, and the whole file must match its
SHA-256); the update sends it to the device, restarts it when it asks or doesn't
restart by itself, waits, and checks the version and a picture. A device that refuses
the file (another region) keeps its old firmware and is not restarted.

Run inside the site image (nothing leaves the box):
  docker run --rm --network none -v "$PWD/app:/app:ro" -v "$PWD/tests:/tests:ro" -e NETWATCH_DATA=/tmp/nw \\
    -e PYTHONDONTWRITEBYTECODE=1 --entrypoint python farm-netwatch:netcfg /tests/test_hikfw.py -v
"""
import hashlib
import os
import sys
import tempfile
import time
import unittest

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import campics      # noqa: E402
import creds        # noqa: E402
import hikfw        # noqa: E402
import hikisapi     # noqa: E402
import hiksettings  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hik_fake import Device  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
HIK = "Hangzhou Hikvision Digital Technology"
FW = os.urandom(1536 * 1024)
SHA = hashlib.sha256(FW).hexdigest()
MODELS = ["DS-2CD2*G2*"]

hikfw.POLL_S, hikfw.SELF_RESTART_S, hikfw.UP_WAIT_S, hikfw.SETTLE_S, hikfw.NVR_WAIT_S = 0.05, 2, 10, 0, 1


def stage(sha=SHA, data=FW, piece=512 * 1024):
    for off in range(0, len(data), piece):
        st = hikfw.stage_put(sha, off, len(data), data[off:off + piece])
    return st


class TestVersions(unittest.TestCase):
    def test_compare(self):
        self.assertEqual(hikfw.compare("V5.7.3 build 220112", "V5.7.15 build 230425"), "newer")
        self.assertEqual(hikfw.compare("V5.7.15 build 230425", "V5.7.3"), "older")
        self.assertEqual(hikfw.compare("V5.7.15", "V5.7.15"), "same")
        self.assertEqual(hikfw.compare("V5.7.15 build 230425", "V5.7.15 build 231101"), "newer")
        self.assertEqual(hikfw.compare("V4.1.18", "V4.1.64"), "newer")
        self.assertEqual(hikfw.compare("", "V5.7.15"), "unknown")

    def test_models(self):
        self.assertTrue(hikfw.model_fits("DS-2CD2046G2-IU", ["DS-2CD2*G2*"]))
        self.assertTrue(hikfw.model_fits("ds-2cd2046g2-iu", ["DS-2CD2046G2-IU"]))
        self.assertFalse(hikfw.model_fits("DS-2CD2T47G1-L", ["DS-2CD2*G2*"]))
        self.assertFalse(hikfw.model_fits("", ["*"]))


class TestStaging(unittest.TestCase):
    def setUp(self):
        hikfw.DIR = tempfile.mkdtemp()

    def test_pieces_resume_and_check(self):
        self.assertEqual(hikfw.stage_status(SHA)["have"], 0)
        hikfw.stage_put(SHA, 0, len(FW), FW[:500000])
        with self.assertRaises(hikfw.FirmwareError) as cm:
            hikfw.stage_put(SHA, 0, len(FW), FW[:500000])
        self.assertEqual((cm.exception.status, cm.exception.extra["have"]), (409, 500000))
        st = hikfw.stage_put(SHA, 500000, len(FW), FW[500000:])
        self.assertTrue(st["done"])
        self.assertEqual(open(os.path.join(hikfw.DIR, SHA + ".dav"), "rb").read(), FW)

    def test_damaged_copy_is_thrown_away(self):
        bad = bytearray(FW)
        bad[1000] ^= 1
        with self.assertRaises(hikfw.FirmwareError) as cm:
            stage(data=bytes(bad))
        self.assertEqual(cm.exception.status, 422)
        self.assertEqual(os.listdir(hikfw.DIR), [])

    def test_one_file_kept(self):
        stage()
        other = os.urandom(1 << 20)
        stage(hashlib.sha256(other).hexdigest(), other)
        self.assertEqual(os.listdir(hikfw.DIR), [hashlib.sha256(other).hexdigest() + ".dav"])

    def test_bad_ids(self):
        for sha in ("../x", "ABC", ""):
            with self.assertRaises(hikfw.FirmwareError):
                hikfw.stage_status(sha)


class Rig(unittest.TestCase):
    def setUp(self):
        hikfw.DIR = tempfile.mkdtemp()
        hikisapi._seen.clear()
        for k in list(creds.keys_with_creds()):
            creds.set_(k)
        self.cam = Device().start().load_settings(os.path.join(HERE, "fixtures", "hik_settings"))
        self.devs = [{"key": "aa:01", "ip": self.cam.addr, "category": "camera", "vendor": HIK, "online": True,
                      "ports": [80, 8000], "name": "Gate cam", "model": "DS-2CD2046G2-IU", "firmware": "V5.7.3"}]
        creds.set_("aa:01", "admin", "pw")
        self.cfg = {"features": {"camera_pictures": True, "camera_manage": True, "camera_risky": True},
                    "camera": {}, "alerts": {}}
        self.e = campics.Engine()
        self.e.start(lambda: [dict(d) for d in self.devs], lambda: {}, lambda: {}, cfg=lambda: self.cfg, thread=False)
        self.hist = []
        self.s = hiksettings.Settings().start(self.e, lambda: self.cfg, on_change=lambda d, e: self.hist.append(e),
                                              log_path=tempfile.mktemp(suffix=".json"))
        self.fw = hikfw.Firmware(self.s)
        stage()

    def tearDown(self):
        self.cam.stop()

    def run_job(self, **kw):
        self.fw.start("aa:01", SHA, "V5.7.15 build 230425", MODELS, who="t", confirm_text="CHANGE", **kw)
        end = time.time() + 30
        while time.time() < end:
            j = self.fw.job("aa:01")
            if j["state"] == "done":
                return j
            time.sleep(0.05)
        self.fail("the job did not end")


class TestPreflight(Rig):
    def test_fits_and_newer(self):
        p = self.fw.preflight("aa:01", "V5.7.15 build 230425", MODELS)
        self.assertEqual((p["model"], p["firmware"], p["compare"], p["fits"], p["problems"]),
                         ("DS-2CD2046G2-IU", "V5.7.3 build 220112", "newer", True, []))

    def test_problems(self):
        self.assertIn("already runs", " ".join(self.fw.preflight("aa:01", "V5.7.3 build 220112", MODELS)["problems"]))
        self.assertIn("OLDER", " ".join(self.fw.preflight("aa:01", "V5.6.0", MODELS)["problems"]))
        self.assertIn("not listed", " ".join(self.fw.preflight("aa:01", "V5.7.15", ["DS-2DE*"])["problems"]))
        self.cfg["features"]["camera_risky"] = False
        self.assertIn("Risky", " ".join(self.fw.preflight("aa:01", "V5.7.15", MODELS)["problems"]))

    def test_start_gates(self):
        with self.assertRaises(hiksettings.SettingsError):
            self.fw.start("aa:01", SHA, "V5.7.15", MODELS, confirm_text="")
        with self.assertRaises(hikfw.FirmwareError) as cm:
            self.fw.start("aa:01", "0" * 64, "V5.7.15", MODELS, confirm_text="CHANGE")
        self.assertEqual(cm.exception.extra["reason"], "not_staged")
        with self.assertRaises(hikfw.FirmwareError):
            self.fw.start("aa:01", SHA, "V5.7.15", ["DS-2DE*"], confirm_text="CHANGE")
        self.assertFalse(hasattr(self.cam, "fw_loaded"), "nothing was sent")


class TestUpdate(Rig):
    def test_asks_for_restart(self):
        j = self.run_job()
        self.assertEqual(j["result"], "ok", j)
        self.assertEqual(self.cam.fw_loaded, [len(FW)])
        self.assertEqual(self.cam.reboots, 1)
        self.assertEqual(j["now"], "V5.7.15 build 230425")
        self.assertEqual([c["what"] for c in j["checks"]], ["version", "picture"])
        self.assertEqual(self.hist[-1]["section"], "firmware")
        self.assertEqual(self.hist[-1]["fields"][0]["before"], "V5.7.3 build 220112")
        self.assertTrue(self.s._risky_lock.acquire(blocking=False), "the lock is free again")
        self.s._risky_lock.release()

    def test_restarts_by_itself(self):
        self.cam.fw_mode = "self"
        j = self.run_job()
        self.assertEqual(j["result"], "ok", j)
        self.assertEqual(getattr(self.cam, "reboots", 0), 0, "no extra restart")

    def test_refused_region_keeps_old_firmware(self):
        self.cam.fw_mode = "refuse"
        j = self.run_job()
        self.assertEqual(j["result"], "failed")
        self.assertIn("region", j["message"])
        self.assertEqual(getattr(self.cam, "reboots", 0), 0)
        self.assertEqual(self.hist[-1]["result"], "failed")

    def test_did_not_take(self):
        self.cam.fw_mode = "keep"
        j = self.run_job()
        self.assertEqual(j["result"], "failed")
        self.assertIn("did not take", j["message"])

    def test_one_at_a_time(self):
        self.cam.down_s = 1.5
        self.fw.start("aa:01", SHA, "V5.7.15 build 230425", MODELS, confirm_text="CHANGE")
        with self.assertRaises(hikfw.FirmwareError) as cm:
            self.fw.start("aa:01", SHA, "V5.7.15 build 230425", MODELS, confirm_text="CHANGE")
        self.assertEqual(cm.exception.extra.get("reason"), "busy")
        end = time.time() + 30
        while self.fw.job("aa:01")["state"] != "done" and time.time() < end:
            time.sleep(0.05)


if __name__ == "__main__":
    unittest.main()
