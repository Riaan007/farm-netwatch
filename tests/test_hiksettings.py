"""Camera settings (app/hiksettings.py) against a fake camera serving REAL setting
documents (tests/fixtures/hik_settings, read from a home camera and cleaned):
the form comes from the camera's capabilities, writes are whole documents in the
camera's own namespace, every change is read back, logged and undoable, and the
site switch, confirm step and one-login rule hold.

Run inside the site image (nothing leaves the box):
  docker run --rm --network none -v "$PWD/app:/app:ro" -v "$PWD/tests:/tests:ro" -e NETWATCH_DATA=/tmp/nw \\
    -e PYTHONDONTWRITEBYTECODE=1 --entrypoint python farm-netwatch:netcfg /tests/test_hiksettings.py -v
"""
import os
import re
import sys
import tempfile
import unittest

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import campics      # noqa: E402
import creds        # noqa: E402
import hikisapi     # noqa: E402
import hiksettings  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hik_fake import Device  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "hik_settings")
HIK = "Hangzhou Hikvision Digital Technology"
IMG = "/ISAPI/Image/channels/1"
BRIGHT = "img.Color/brightnessLevel"
SA_PRESET = {"time.timeMode": "NTP", "time.timeZone": "CST-2:00:00",
             "ntp.NTPServer/hostName": "pool.ntp.org", "ntp.NTPServer/synchronizeInterval": "60"}


class Rig(unittest.TestCase):
    def setUp(self):
        hikisapi._seen.clear()
        for k in list(creds.keys_with_creds()):
            creds.set_(k)
        self.cam = Device().start().load_settings(FIX)
        self.devs = [{"key": "aa:01", "ip": self.cam.addr, "category": "camera", "vendor": HIK, "online": True,
                      "ports": [80, 8000], "name": "Front gate"}]
        creds.set_("aa:01", "admin", "pw")
        self.cfg = {"features": {"camera_pictures": True, "camera_manage": True}, "camera": {}, "alerts": {}}
        self.e = campics.Engine()
        self.e.start(lambda: [dict(d) for d in self.devs], lambda: {}, lambda: {}, cfg=lambda: self.cfg, thread=False)
        self.hist = []
        self.log = tempfile.mktemp(suffix=".json")
        self.s = hiksettings.Settings().start(self.e, lambda: self.cfg, on_change=lambda d, e: self.hist.append(e),
                                              log_path=self.log)

    def tearDown(self):
        self.cam.stop()
        if os.path.exists(self.log):
            os.remove(self.log)

    def fields(self, section="picture"):
        r = self.s.read("aa:01")
        return {f["id"]: f for f in next(x for x in r["sections"] if x["id"] == section)["fields"]}

    def doc_text(self, path, tag, inside=None):
        import re
        doc = self.cam.settings[path]
        if inside:
            doc = re.search(rf"<{inside}[ >].*?</{inside}>", doc, re.S).group(0)
        m = re.search(rf"<{tag}>([^<]*)</{tag}>", doc)
        return m.group(1) if m else None


class Reading(Rig):
    def test_the_form_comes_from_the_cameras_capabilities(self):
        r = self.s.read("aa:01")
        self.assertTrue(r["can_change"])
        self.assertEqual([x["id"] for x in r["sections"]], ["picture", "osd", "time", "main", "sub"])
        f = self.fields()
        self.assertEqual((f[BRIGHT]["kind"], f[BRIGHT]["min"], f[BRIGHT]["max"], f[BRIGHT]["value"]), ("int", 0, 100, "50"))
        self.assertEqual(f["img.IrcutFilter/IrcutFilterType"]["kind"], "enum")
        self.assertIn("night", [o["v"] for o in f["img.IrcutFilter/IrcutFilterType"]["opts"]])

    def test_a_setting_the_camera_does_not_describe_is_read_only(self):
        st = self.fields("main")
        self.assertFalse(st["s.Video/SmartCodec/enabled"]["editable"], "SmartCodec has no capability entry on this camera")
        self.assertIn("1920x1080", [o["v"] for o in st["s.resolution"]["opts"]])
        self.assertEqual(st["s.Video/maxFrameRate"]["shown"], "15")

    def test_time_shows_the_wrong_zone_and_offers_south_africa(self):
        t = self.fields("time")
        self.assertEqual(t["time.timeZone"]["value"], "CST-8:00:00")
        self.assertIn("CST-2:00:00", [o["v"] for o in t["time.timeZone"]["opts"]])
        self.assertIn("South Africa", t["time.timeZone"]["opts"][0]["label"])

    def test_switch_off_still_reads_but_never_writes(self):
        self.cfg["features"]["camera_manage"] = False
        self.assertFalse(self.s.read("aa:01")["can_change"])
        with self.assertRaises(hiksettings.SettingsError) as cm:
            self.s.apply("aa:01", 1, "picture", {BRIGHT: "60"})
        self.assertEqual(cm.exception.status, 403)
        self.assertEqual(self.cam.puts, [])

    def test_a_camera_that_is_not_hikvision_is_never_asked(self):
        self.devs[0].update(vendor="Dynacolor Inc", model="NH3K", ports=[80, 554])
        n = len(self.cam.hits)
        with self.assertRaises(hiksettings.SettingsError) as cm:
            self.s.read("aa:01")
        self.assertEqual(cm.exception.extra.get("reason"), "not_hikvision")
        self.assertEqual(len(self.cam.hits), n, "nothing was sent to the camera")

    def test_a_camera_only_reached_through_its_recorder_says_so(self):
        creds.set_("aa:01")
        with self.assertRaises(hiksettings.SettingsError) as cm:
            self.s.read("aa:01")
        self.assertEqual(cm.exception.status, 409)


class Writing(Rig):
    def test_a_safe_change_writes_the_whole_document_and_is_read_back(self):
        out = self.s.apply("aa:01", 1, "picture", {BRIGHT: "60"}, who="site page", expect={BRIGHT: "50"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(self.cam.puts), 1)
        path, body = self.cam.puts[0]
        self.assertEqual(path, IMG)
        self.assertNotIn("ns0:", body)
        self.assertIn('xmlns="http://www.hikvision.com/ver20/XMLSchema"', body)
        self.assertIn("<WDRLevel>50</WDRLevel>", body, "the rest of the document went back unchanged")
        self.assertEqual(body.count("<RegionCoordinates>"), 4)
        self.assertEqual(self.doc_text(IMG, "brightnessLevel", "Color"), "60")
        row = out["change"]["fields"][0]
        self.assertEqual((row["before"], row["after"], row["kept"]), ("50", "60", True))
        self.assertEqual(len(self.hist), 1)
        self.assertEqual(self.s.changes("aa:01")[0]["id"], out["change"]["id"])

    def test_careful_sections_need_confirm(self):
        with self.assertRaises(hiksettings.SettingsError) as cm:
            self.s.apply("aa:01", 1, "time", SA_PRESET)
        self.assertTrue(cm.exception.extra.get("needs_confirm"))
        self.assertEqual(self.cam.puts, [])

    def test_south_africa_time_from_the_internet_sets_server_zone_and_clock(self):
        out = self.s.apply("aa:01", 1, "time", SA_PRESET, confirm=True)
        self.assertTrue(out["ok"], out)
        self.assertEqual([p for p, _ in self.cam.puts], ["/ISAPI/System/time/ntpServers", "/ISAPI/System/time"])
        ntp, tbody = self.cam.puts[0][1], self.cam.puts[1][1]
        self.assertIn("<hostName>pool.ntp.org</hostName>", ntp)
        self.assertIn("<addressingFormatType>hostname</addressingFormatType>", ntp)
        self.assertIn("<timeZone>CST-2:00:00</timeZone>", tbody)
        self.assertIn("<timeMode>NTP</timeMode>", tbody)
        self.assertRegex(tbody, r"<localTime>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+02:00</localTime>")

    def test_a_resolution_writes_width_and_height_together(self):
        self.s.apply("aa:01", 1, "main", {"s.resolution": "1920x1080"}, confirm=True)
        body = self.cam.puts[0][1]
        self.assertIn("<videoResolutionWidth>1920</videoResolutionWidth>", body)
        self.assertIn("<videoResolutionHeight>1080</videoResolutionHeight>", body)

    def test_values_outside_the_cameras_choices_are_refused_before_anything_is_sent(self):
        for ch in ({BRIGHT: "150"}, {"img.IrcutFilter/IrcutFilterType": "disco"}, {"img.Color/nosuch": "1"}):
            with self.assertRaises(hiksettings.SettingsError):
                self.s.apply("aa:01", 1, "picture", ch)
        with self.assertRaises(hiksettings.SettingsError):
            self.s.apply("aa:01", 1, "osd", {"name.name": "<script>"})
        with self.assertRaises(hiksettings.SettingsError):
            self.s.apply("aa:01", 1, "time", {"ntp.NTPServer/hostName": "bad host;rm"}, confirm=True)
        self.assertEqual(self.cam.puts, [])

    def test_a_value_changed_on_the_camera_since_you_looked_is_a_conflict(self):
        with self.assertRaises(hiksettings.SettingsError) as cm:
            self.s.apply("aa:01", 1, "picture", {BRIGHT: "60"}, expect={BRIGHT: "40"})
        self.assertEqual(cm.exception.status, 409)
        self.assertEqual(self.cam.puts, [])

    def test_a_change_the_camera_does_not_keep_is_reported(self):
        self.cam.not_kept.add(IMG)
        out = self.s.apply("aa:01", 1, "picture", {BRIGHT: "60"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["change"]["result"], "partial")
        self.assertFalse(out["change"]["fields"][0]["kept"])

    def test_a_refused_write_changes_nothing_and_logs_nothing(self):
        self.cam.put_status[IMG] = (400, 6)
        with self.assertRaises(hiksettings.SettingsError) as cm:
            self.s.apply("aa:01", 1, "picture", {BRIGHT: "60"})
        self.assertIn("refused", str(cm.exception))
        self.assertEqual(self.s.changes("aa:01"), [])

    def test_a_change_that_needs_a_restart_says_so(self):
        self.cam.put_status["/ISAPI/Streaming/channels/101"] = (200, 7)
        out = self.s.apply("aa:01", 1, "main", {"s.Video/videoCodecType": "H.264"}, confirm=True)
        self.assertTrue(out["reboot"])
        self.assertIn("restart", out["message"])

    def test_a_refused_login_is_tried_once_only(self):
        creds.set_("aa:01", "admin", "wrong")
        for _ in range(3):
            with self.assertRaises(hiksettings.SettingsError):
                self.s.apply("aa:01", 1, "picture", {BRIGHT: "60"})
        self.assertLessEqual(self.cam.logins(), 1)


class Undo(Rig):
    def test_undo_puts_the_old_value_back_once(self):
        c = self.s.apply("aa:01", 1, "picture", {BRIGHT: "60"})["change"]
        out = self.s.undo("aa:01", c["id"], who="hub")
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.doc_text(IMG, "brightnessLevel", "Color"), "50")
        self.assertEqual(out["change"]["undo_of"], c["id"])
        self.assertTrue(self.s.log.get(c["id"])["undone_by"])
        with self.assertRaises(hiksettings.SettingsError) as cm:
            self.s.undo("aa:01", c["id"])
        self.assertEqual(cm.exception.status, 409)

    def test_undo_refuses_when_someone_changed_it_since(self):
        c = self.s.apply("aa:01", 1, "picture", {BRIGHT: "60"})["change"]
        self.cam.settings[IMG] = re.sub(r"(<Color[ >].*?<brightnessLevel>)60(<)", r"\g<1>70\2", self.cam.settings[IMG], flags=re.S)
        n = len(self.cam.puts)
        with self.assertRaises(hiksettings.SettingsError) as cm:
            self.s.undo("aa:01", c["id"])
        self.assertEqual(cm.exception.status, 409)
        self.assertEqual(len(self.cam.puts), n)

    def test_undo_of_a_time_zone_not_on_the_short_list(self):
        self.cam.settings["/ISAPI/System/time"] = self.cam.settings["/ISAPI/System/time"].replace(
            "CST-8:00:00", "CST-2:00:00DST01:00:00,M3.5.6/23:59:00,M10.5.6/23:59:00")
        c = self.s.apply("aa:01", 1, "time", {"time.timeZone": "CST-2:00:00"}, confirm=True)["change"]
        out = self.s.undo("aa:01", c["id"])
        self.assertTrue(out["ok"], out)
        self.assertIn("DST01:00:00", self.doc_text("/ISAPI/System/time", "timeZone"))


class Helpers(unittest.TestCase):
    def test_time_zone_offsets(self):
        self.assertEqual(hiksettings.tz_offset("CST-2:00:00").total_seconds(), 7200)
        self.assertEqual(hiksettings.tz_offset("CST+3:30:00").total_seconds(), -12600)
        self.assertTrue(hiksettings.local_time_now("CST-2:00:00").endswith("+02:00"))
        self.assertIn("summer time", hiksettings.tz_label("CST-2:00:00DST01:00:00,M3.5.6/23:59:00,M10.5.6/23:59:00"))


if __name__ == "__main__":
    unittest.main()
