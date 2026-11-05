"""Recorder channels and the camera password job (Phase 4d, part 2).

A fake recorder whose channels are connected exactly when the fake camera takes the
login the channel holds, so a password change that forgets the recorder shows up as a
lost camera. The recorder refuses a channel write without its password (Netwatch must
never blank one) and with read-only fields in it.

Run inside the site image (nothing leaves the box):
  docker run --rm --network none -v "$PWD/app:/app:ro" -v "$PWD/tests:/tests:ro" -e NETWATCH_DATA=/tmp/nw \\
    -e PYTHONDONTWRITEBYTECODE=1 --entrypoint python farm-netwatch:netcfg /tests/test_hikchannels.py -v
"""
import json
import os
import sys
import tempfile
import unittest

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import campics      # noqa: E402
import creds        # noqa: E402
import hikchannels  # noqa: E402
import hikisapi     # noqa: E402
import hiksecurity  # noqa: E402
import hiksettings  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hik_fake import Device  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
HIK = "Hangzhou Hikvision Digital Technology"
WORD = hiksecurity.CONFIRM_WORD
LOCK = "/ISAPI/Security/illegalLoginLock"
NEW = "Gate-2026!x"


class Rig(unittest.TestCase):
    def setUp(self):
        hikisapi._seen.clear()
        for k in list(creds.keys_with_creds()):
            creds.set_(k)
        hikchannels.WAIT_S, hikchannels.SETTLE_S = 0, 0
        self.cam = Device().start().load_settings(os.path.join(HERE, "fixtures", "hik_security"))
        self.cam.load_settings(os.path.join(HERE, "fixtures", "hik_settings"))
        self.cam.accounts = {"admin": {"pw": "Cam-pw-1", "level": "Administrator", "id": 1}}
        self.cam.rights = {"admin": {r: "true" for r in Device.RIGHTS}}
        self.cam2 = Device().start()
        self.cam2.accounts = {"admin": {"pw": "Other-pw-2", "level": "Administrator", "id": 1}}
        self.nvr = Device(channels=[]).start()
        self.nvr.accounts = {"admin": {"pw": "Nvr-pw-1", "level": "Administrator", "id": 1}}
        self.nvr.channels += [{"id": "1", "name": "Gate", "ip": self.cam.addr, "user": "admin", "pw": "Cam-pw-1", "cam": self.cam},
                              {"id": "3", "name": "Shed", "ip": "10.9.9.9", "user": "viewer", "pw": "x", "online": False}]
        self.nvr.known_cams = [self.cam, self.cam2]
        self.devs = [
            {"key": "aa:01", "ip": self.cam.addr, "category": "camera", "vendor": HIK, "online": True,
             "ports": [80, 8000], "name": "Gate cam", "model": "DS-2CD2046G2-IU"},
            {"key": "aa:02", "ip": self.cam2.addr, "category": "camera", "vendor": HIK, "online": True,
             "ports": [80, 8000], "name": "Dam cam", "model": "DS-2CD2046G2-IU"},
            {"key": "aa:09", "ip": self.nvr.addr, "category": "nvr", "vendor": HIK, "online": True,
             "ports": [80, 8000], "name": "House NVR", "model": "DS-7732NXI-I4/S"},
        ]
        creds.set_("aa:01", "admin", "Cam-pw-1")
        creds.set_("aa:02", "admin", "Other-pw-2")
        creds.set_("aa:09", "admin", "Nvr-pw-1")
        self.cfg = {"features": {"camera_pictures": True, "camera_manage": True, "camera_risky": True}, "camera": {}, "alerts": {}}
        self.e = campics.Engine()
        self.e.start(lambda: [dict(d) for d in self.devs], lambda: {}, lambda: {}, cfg=lambda: self.cfg, thread=False)
        self.e._read_nvrs()
        self.hist = []
        self.log = tempfile.mktemp(suffix=".json")
        self.s = hiksettings.Settings().start(self.e, lambda: self.cfg, on_change=lambda d, e: self.hist.append(e),
                                              log_path=self.log)

    def tearDown(self):
        for d in (self.cam, self.cam2, self.nvr):
            d.stop()
        if os.path.exists(self.log):
            os.remove(self.log)

    def ch(self, n):
        return next((c for c in self.nvr.channels if c["id"] == str(n)), None)

    def all_text(self, out=None):
        with open(self.log) as f:
            return json.dumps(out or {}) + json.dumps(self.hist) + f.read()


class Channels(Rig):
    def test_the_list_is_matched_to_netwatch(self):
        r = self.s.channels("aa:09")
        rows = {c["id"]: c for c in r["channels"]}
        self.assertEqual((rows[1]["online"], rows[1]["netwatch"]["key"], rows[1]["login_known"]), (True, "aa:01", True))
        self.assertEqual((rows[3]["online"], rows[3]["netwatch"], rows[3]["login_known"]), (False, None, False))
        self.assertEqual([c["key"] for c in r["addable"]], ["aa:02"])
        self.assertEqual(r["free"], 6)
        self.assertNotIn("Cam-pw-1", json.dumps(r))

    def test_fix_login_gives_the_channel_netwatchs_login(self):
        self.ch(1)["pw"] = "stale"
        self.assertFalse(self.s.channels("aa:09")["channels"][0]["online"])
        out = self.s.channel_do("aa:09", "fix_login", ch=1, confirm_text=WORD, who="site page")
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.ch(1)["pw"], "Cam-pw-1")
        m, _p, body = self.nvr.chan_calls[-1]
        self.assertEqual(m, "PUT")
        self.assertNotIn("<serialNumber>", body)                       # read-only parts left out
        self.assertNotIn("Cam-pw-1", self.all_text(out))
        with self.assertRaises(hiksettings.SettingsError):
            self.s.undo("aa:09", out["change"]["id"])

    def test_remove_then_undo_puts_the_camera_back_on_its_channel(self):
        self.nvr.load_settings(os.path.join(HERE, "fixtures", "hik_nvr"))       # channel 1's real recording track
        track = "/ISAPI/ContentMgmt/record/tracks/101"
        kept = self.nvr.settings[track]
        out = self.s.channel_do("aa:09", "remove", ch=1, confirm_text=WORD)
        self.assertTrue(out["ok"], out)
        self.assertIsNone(self.ch(1))
        u = self.s.undo("aa:09", out["change"]["id"])
        self.assertTrue(u["ok"], u)
        c = self.ch(1)
        self.assertEqual((c["ip"], c["pw"], c["user"]), (self.cam.addr, "Cam-pw-1", "admin"))
        self.assertEqual(self.nvr.puts[-1][0], track)                   # its recording was put back
        self.assertEqual(hiksettings._week_of(hiksettings._walk(hikisapi.parse(self.nvr.settings[track].encode()),
                         "TrackSchedule/ScheduleBlockList/ScheduleBlock")),
                         hiksettings._week_of(hiksettings._walk(hikisapi.parse(kept.encode()),
                         "TrackSchedule/ScheduleBlockList/ScheduleBlock")))

    def test_add_a_netwatch_camera_then_undo(self):
        out = self.s.channel_do("aa:09", "add", camera="aa:02", confirm_text=WORD)
        self.assertTrue(out["ok"], out)
        c = self.ch(2)                                                  # the first free channel
        self.assertEqual((c["ip"], c["pw"], c["name"]), (self.cam2.addr, "Other-pw-2", "Dam cam"))
        self.assertTrue(self.s.undo("aa:09", out["change"]["id"])["ok"])
        self.assertIsNone(self.ch(2))

    def test_refusals(self):
        with self.assertRaises(hiksettings.SettingsError) as c:
            self.s.channel_do("aa:09", "fix_login", ch=1)
        self.assertEqual(c.exception.extra.get("needs_word"), WORD)
        for kw in ({"action": "fix_login", "ch": 3},                    # no login in Netwatch for that camera
                   {"action": "move", "ch": 1, "ip": "300.1.1.1"},
                   {"action": "move", "ch": 1, "ip": "10.9.9.9"},       # another channel has it
                   {"action": "add", "camera": "aa:01"},                # already recorded
                   {"action": "remove", "ch": 7}):
            with self.assertRaises(hiksettings.SettingsError):
                self.s.channel_do("aa:09", confirm_text=WORD, **kw)
        self.cfg["features"]["camera_risky"] = False
        with self.assertRaises(hiksettings.SettingsError):
            self.s.channel_do("aa:09", "remove", ch=1, confirm_text=WORD)
        self.assertEqual(self.nvr.chan_calls, [])


class Waiting(Rig):
    def test_a_stale_connected_is_not_believed(self):
        seen = iter([("true", "connect"), ("false", "connecting"), ("true", "connect"), ("true", "connect")])
        calls = []

        class Cl:
            def get(self, path, max_bytes=0):
                on, why = next(seen)
                calls.append(path)
                return hikisapi.Reply(200, f"<s><online>{on}</online><chanDetectResult>{why}</chanDetectResult></s>".encode(), {})
        hikchannels.WAIT_S = 100
        self.assertTrue(hikchannels.wait_online(Cl(), 4, sleep=lambda s: None))
        self.assertEqual(len(calls), 4)                                 # connected twice in a row, after the dip


class PasswordJob(Rig):
    def test_plan_names_the_channels_that_follow(self):
        p = self.s.password_plan("aa:01")
        self.assertEqual((p["user"], p["recorders"][0]["channels"]), ("admin", [1]))

    def test_camera_and_recorder_both_get_it_and_the_lock_comes_back(self):
        out = self.s.password("aa:01", NEW, NEW, who="site page", confirm_text=WORD)
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.cam.accounts["admin"]["pw"], NEW)
        self.assertEqual(self.ch(1)["pw"], NEW)
        self.assertTrue(self.nvr._chan_on(self.ch(1)))
        self.assertEqual(creds.get("aa:01")["password"], NEW)
        locks = [b for p, b in self.cam.puts if p == LOCK]
        self.assertEqual(["<enabled>false</enabled>" in locks[0], "<enabled>true</enabled>" in locks[-1]], [True, True])
        self.assertIn("<enabled>true</enabled>", self.cam.settings[LOCK])
        self.assertNotIn(NEW, self.all_text(out))
        self.assertNotIn("Cam-pw-1", self.all_text(out))
        self.assertTrue(out["change"]["no_undo"])
        # Netwatch's normal work goes on with the new password
        self.assertTrue(self.s.read("aa:01", 1, "security")["info"]["users"])

    def test_with_the_netwatch_account_the_admin_slot_is_what_changes(self):
        self.assertTrue(self.s.account("aa:01", "create", confirm_text=WORD)["ok"])
        nw = creds.get("aa:01")
        self.assertTrue(self.s.password("aa:01", NEW, NEW, confirm_text=WORD)["ok"])
        self.assertEqual(creds.get_admin("aa:01"), {"username": "admin", "password": NEW})
        self.assertEqual(creds.get("aa:01"), nw)                       # the everyday login is untouched

    def test_bad_new_passwords_change_nothing(self):
        for new, again in (("Short1!", "Short1!"), (NEW, NEW + "x"), ("alllowercase", "alllowercase"),
                           ("Has&amp1x", "Has&amp1x"), ("myadminPw1", "myadminPw1"), ("Cam-pw-1", "Cam-pw-1")):
            with self.assertRaises(hiksettings.SettingsError):
                self.s.password("aa:01", new, again, confirm_text=WORD)
        self.assertEqual(self.cam.accounts["admin"]["pw"], "Cam-pw-1")
        self.assertEqual(self.cam.puts, [])

    def test_a_refused_change_leaves_everything_and_the_lock_on(self):
        self.cam.need_login_pw = True
        creds.set_("aa:01", "admin", "Cam-pw-1")
        orig = hikchannels._write

        def no_login_pw(cl, H, method, path, root, admin_pw):     # simulate firmware that refuses the change
            if path.startswith("/ISAPI/Security/users/"):
                return False, "HTTP 400 badParameters"
            return orig(cl, H, method, path, root, admin_pw)
        hikchannels._write = no_login_pw
        try:
            with self.assertRaises(hiksettings.SettingsError):
                self.s.password("aa:01", NEW, NEW, confirm_text=WORD)
        finally:
            hikchannels._write = orig
        self.assertEqual(self.cam.accounts["admin"]["pw"], "Cam-pw-1")
        self.assertEqual(creds.get("aa:01")["password"], "Cam-pw-1")
        self.assertIn("<enabled>true</enabled>", self.cam.settings[LOCK])
        self.assertEqual(self.ch(1)["pw"], "Cam-pw-1")


if __name__ == "__main__":
    unittest.main()
