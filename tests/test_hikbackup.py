"""Device backup files and settings snapshots — the site side (Phase 6, part 1).

A fake camera that, like current Hikvision firmware, refuses to export its backup file
without a file password; old firmware that doesn't know the parameter; a login that may
not export. Loading a file back is risky work and the device takes it only with the
password it was made with.

Run inside the site image (nothing leaves the box):
  docker run --rm --network none -v "$PWD/app:/app:ro" -v "$PWD/tests:/tests:ro" -e NETWATCH_DATA=/tmp/nw \\
    -e PYTHONDONTWRITEBYTECODE=1 --entrypoint python farm-netwatch:netcfg /tests/test_hikbackup.py -v
"""
import os
import sys
import tempfile
import unittest

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import campics      # noqa: E402
import creds        # noqa: E402
import hikbackup    # noqa: E402
import hikisapi     # noqa: E402
import hiksecurity  # noqa: E402
import hiksettings  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hik_fake import Device  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
HIK = "Hangzhou Hikvision Digital Technology"
IMG = "/ISAPI/Image/channels/1"


class Rig(unittest.TestCase):
    def setUp(self):
        hikisapi._seen.clear()
        for k in list(creds.keys_with_creds()):
            creds.set_(k)
        self.cam = Device().start().load_settings(os.path.join(HERE, "fixtures", "hik_settings"))
        self.cam.load_settings(os.path.join(HERE, "fixtures", "hik_security"))
        self.devs = [{"key": "aa:01", "ip": self.cam.addr, "category": "camera", "vendor": HIK, "online": True,
                      "ports": [80, 8000], "name": "Gate cam", "model": "DS-2CD2046G2-IU", "firmware": "V5.7.3"}]
        creds.set_("aa:01", "admin", "pw")
        self.cfg = {"features": {"camera_pictures": True, "camera_manage": True, "camera_risky": False},
                    "camera": {}, "alerts": {}}
        self.e = campics.Engine()
        self.e.start(lambda: [dict(d) for d in self.devs], lambda: {}, lambda: {}, cfg=lambda: self.cfg, thread=False)
        self.hist = []
        self.s = hiksettings.Settings().start(self.e, lambda: self.cfg, on_change=lambda d, e: self.hist.append(e),
                                              log_path=tempfile.mktemp(suffix=".json"))

    def tearDown(self):
        self.cam.stop()


class TestExport(Rig):
    def test_export_uses_a_fresh_file_password(self):
        data, sec, facts = hikbackup.export(self.s, "aa:01")
        self.assertEqual(len(sec), hikbackup.SECRET_LEN)
        self.assertTrue(data.startswith(b"\xef\xff\xef\xff" + sec.encode() + b"|"))
        self.assertEqual((facts["model"], facts["firmware"], facts["bytes"]), ("DS-2CD2046G2-IU", "V5.7.3", len(data)))
        _d2, sec2, _f2 = hikbackup.export(self.s, "aa:01")
        self.assertNotEqual(sec, sec2)
        self.assertEqual(self.cam.cfg_keys, [sec, sec2], "one read per export, no retries")

    def test_old_firmware_without_file_password(self):
        self.cam.cfg_mode = "old"
        data, sec, _f = hikbackup.export(self.s, "aa:01")
        self.assertEqual(sec, "")
        self.assertTrue(data.startswith(b"\xef\xff\xef\xff|"))

    def test_refused_export_is_an_error_not_a_file(self):
        self.cam.cfg_mode = "refuse"
        with self.assertRaises(hikbackup.BackupError) as cm:
            hikbackup.export(self.s, "aa:01")
        self.assertIn("invalidOperation", str(cm.exception))
        self.assertEqual(self.cam.cfg_keys if hasattr(self.cam, "cfg_keys") else [], [])

    def test_needs_camera_settings_switch(self):
        self.cfg["features"]["camera_manage"] = False
        with self.assertRaises(hikbackup.BackupError) as cm:
            hikbackup.export(self.s, "aa:01")
        self.assertEqual(cm.exception.status, 403)

    def test_secret_shape(self):
        for _ in range(50):
            s = hikbackup.new_secret()
            self.assertTrue(len(s) == 16 and s.isalnum() and any(c.isdigit() for c in s)
                            and any(c.isupper() for c in s) and any(c.islower() for c in s))


class TestRestore(Rig):
    def test_restore_is_risky_and_needs_the_word(self):
        data, sec, _f = hikbackup.export(self.s, "aa:01")
        with self.assertRaises(hiksettings.SettingsError) as cm:
            hikbackup.restore(self.s, "aa:01", data, sec, confirm_text=hiksecurity.CONFIRM_WORD)
        self.assertEqual(cm.exception.status, 403)
        self.cfg["features"]["camera_risky"] = True
        with self.assertRaises(hiksettings.SettingsError):
            hikbackup.restore(self.s, "aa:01", data, sec)
        self.assertFalse(hasattr(self.cam, "cfg_loaded"))
        r = hikbackup.restore(self.s, "aa:01", data, sec, who="t", confirm_text=hiksecurity.CONFIRM_WORD)
        self.assertTrue(r["ok"])
        self.assertEqual(self.cam.cfg_loaded, data)
        self.assertTrue(r["reboot_needed"], "the fake answers Reboot Required, like the real camera")
        self.assertIn("restart", r["message"])
        self.assertEqual(self.hist[-1]["section"], "config_restore")
        self.assertTrue(self.hist[-1]["no_undo"])

    def test_wrong_password_is_refused_by_the_device(self):
        self.cfg["features"]["camera_risky"] = True
        data, sec, _f = hikbackup.export(self.s, "aa:01")
        with self.assertRaises(hikbackup.BackupError) as cm:
            hikbackup.restore(self.s, "aa:01", data, "Zz9" + sec[3:], confirm_text=hiksecurity.CONFIRM_WORD)
        self.assertIn("cfgFileSecretKeyError", str(cm.exception))
        self.assertEqual(self.hist[-1]["result"], "failed")

    def test_not_a_file(self):
        self.cfg["features"]["camera_risky"] = True
        with self.assertRaises(hikbackup.BackupError):
            hikbackup.restore(self.s, "aa:01", b"tiny", "", confirm_text=hiksecurity.CONFIRM_WORD)


class TestSnapshot(Rig):
    def test_fingerprint_follows_the_settings(self):
        a = hikbackup.snapshot(self.s, "aa:01")
        self.assertIn("picture", a["lenses"]["1"])
        self.assertIn("time", a["lenses"]["1"])
        self.assertIn("access", a["lenses"]["1"])
        self.assertIn("Picture · Brightness", a["labels"].values())
        self.assertEqual(hikbackup.snapshot(self.s, "aa:01")["fingerprint"], a["fingerprint"])
        self.cam.settings[IMG] = self.cam.settings[IMG].replace("<brightnessLevel>50<", "<brightnessLevel>61<", 1)
        b = hikbackup.snapshot(self.s, "aa:01")
        self.assertNotEqual(b["fingerprint"], a["fingerprint"])
        self.assertEqual(b["lenses"]["1"]["picture"]["img.Color/brightnessLevel"], "61")
        self.assertEqual(self.cam.puts, [], "a snapshot writes nothing")


if __name__ == "__main__":
    unittest.main()
