"""Recorder settings (Phase 4c): a camera's recording on its recorder, and the recorder's
own disk settings. tests/fixtures/hik_nvr holds REAL documents from a home NVR
(channel 1's recording track and the storage settings).

Run inside the site image (nothing leaves the box):
  docker run --rm --network none -v "$PWD/app:/app:ro" -v "$PWD/tests:/tests:ro" -e NETWATCH_DATA=/tmp/nw \\
    -e PYTHONDONTWRITEBYTECODE=1 --entrypoint python farm-netwatch:netcfg /tests/test_hikrecord.py -v
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_hikvia import Rig  # noqa: E402  (the camera + recorder rig of Phase 4b)

import creds        # noqa: E402
import hikisapi     # noqa: E402
import hiksettings  # noqa: E402

TRACK = "/ISAPI/ContentMgmt/record/tracks/101"
EXT = "/ISAPI/ContentMgmt/Storage/extension"
X = "t.CustomExtensionList/CustomExtension/"
WEEK = "t.TrackSchedule/ScheduleBlockList/ScheduleBlock"
ALL_C = "|".join(["C" * 24] * 7)


def fields(r, sec):
    return {f["id"]: f for f in next(s for s in r["sections"] if s["id"] == sec)["fields"]}


def week_of(xml):
    root = hikisapi.parse(xml.encode() if isinstance(xml, str) else xml)
    return hiksettings._week_of(hiksettings._walk(root, "TrackSchedule/ScheduleBlockList/ScheduleBlock"))


class CameraRecording(Rig):
    def test_read_through_the_recorder(self):
        r = self.s.read("aa:01", 1, "record")
        self.assertEqual((r["route"], r["routes"], [s["id"] for s in r["sections"]]), ("nvr", ["nvr"], ["record"]))
        f = fields(r, "record")
        self.assertEqual(f[WEEK]["value"], ALL_C)
        self.assertEqual(f[WEEK]["shown"], "Every day: Continuous 00–24")
        self.assertTrue(f[WEEK]["editable"])
        self.assertEqual([m["mode"] for m in f[WEEK]["modes"]][:3], ["CMR", "MOTION", "ALARM"])
        self.assertEqual((f[X + "PreRecordTimeSeconds"]["value"], f[X + "PostRecordTimeSeconds"]["value"]), ("30", "30"))
        self.assertEqual(f["t.SrcDescriptor/SrcUrl"]["shown"], "Main stream")
        self.assertEqual(len(f["t.SrcDescriptor/SrcUrl"]["opts"]), 3)
        d = f["t.Duration"]
        self.assertEqual((d["kind"], d["value"], d["max"], d["editable"], d["shown"]),
                         ("int", "0", 750, True, "No limit (until the disks are full)"))
        self.assertEqual(f[X + "enableSchedule"]["value"], "true")

    def test_own_login_still_goes_through_the_recorder(self):
        creds.set_("aa:01", "admin", "pw")
        self.e.inventory(force=True)
        self.assertEqual(self.s.read("aa:01", 1, "record")["route"], "nvr")
        self.assertEqual(self.s.read("aa:01", 1, "record", route="direct")["sections"], [])

    def test_a_new_week_is_written_read_back_and_undone(self):
        before = self.nvr.settings[TRACK]
        wk, we = "C" * 24, "M" * 6 + "-" * 12 + "M" * 6
        new = "|".join([wk] * 5 + [we] * 2)
        out = self.s.apply("aa:01", 1, "record", {WEEK: new, "t.Duration": "14"}, who="site page", confirm=True)
        self.assertTrue(out["ok"], out)
        path, body = self.nvr.puts[-1]
        self.assertEqual(path, TRACK)
        self.assertEqual(week_of(body), new)
        self.assertIn("<Duration min=\"0\" max=\"750\">P14DT0H</Duration>", body)
        self.assertEqual(body.count("<ScheduleAction>"), 5 + 2 * 2 + 1)    # + the holiday schedule's one
        self.assertIn("<DayOfWeek>Saturday</DayOfWeek>\n<TimeOfDay>18:00:00</TimeOfDay>", re.sub(r">\s+<", ">\n<", body))
        self.assertIn("<HolidaySchedule>", body)                      # the holiday schedule is kept
        self.assertIn("<ActionRecordingMode>MOTION</ActionRecordingMode>", body)
        dev, entry = self.hist[-1]
        self.assertEqual(dev, "aa:09")
        self.assertTrue(entry["section_label"].startswith("Recording — "))
        sh = {r["id"]: r["shown_after"] for r in entry["fields"]}
        self.assertEqual(sh[WEEK], "Mon–Fri: Continuous 00–24; Sat–Sun: Motion 00–06, Motion 18–24")
        self.assertEqual(sh["t.Duration"], "14 days")
        u = self.s.undo("aa:01", out["change"]["id"], who="site page")
        self.assertTrue(u["ok"], u)
        self.assertEqual(week_of(self.nvr.settings[TRACK]), week_of(before))
        self.assertIn(">P0DT0H<", self.nvr.settings[TRACK])

    def test_needs_confirm_and_refuses_bad_weeks(self):
        with self.assertRaises(hiksettings.SettingsError) as c:
            self.s.apply("aa:01", 1, "record", {"t.Duration": "7"})
        self.assertTrue(c.exception.extra.get("needs_confirm"))
        bad = ["C" * 24,                                               # one day only
               "|".join(["Z" * 24] * 7),                                # not a recording type
               "|".join(["O" * 24] * 7),                                # a type this recorder doesn't offer
               "|".join(["C-" * 12] * 7)]                               # 12 periods a day
        for v in bad:
            with self.assertRaises(hiksettings.SettingsError):
                self.s.apply("aa:01", 1, "record", {WEEK: v}, confirm=True)
        for v in ("751", "-1", "x"):
            with self.assertRaises(hiksettings.SettingsError):
                self.s.apply("aa:01", 1, "record", {"t.Duration": v}, confirm=True)
        with self.assertRaises(hiksettings.SettingsError):
            self.s.apply("aa:01", 1, "record", {X + "PreRecordTimeSeconds": "7"}, confirm=True)
        self.assertEqual(self.nvr.puts, [])

    def test_a_schedule_in_minutes_is_only_shown(self):
        self.nvr.settings[TRACK] = self.nvr.settings[TRACK].replace(
            "<TimeOfDay>00:00:00</TimeOfDay>\n</ScheduleActionStartTime>", "<TimeOfDay>06:30:00</TimeOfDay>\n</ScheduleActionStartTime>", 1)
        f = fields(self.s.read("aa:01", 1, "record"), "record")[WEEK]
        self.assertFalse(f["editable"])
        self.assertIn("minutes", f["shown"])
        with self.assertRaises(hiksettings.SettingsError):
            self.s.apply("aa:01", 1, "record", {WEEK: ALL_C}, confirm=True)

    def test_turning_recording_off_is_a_plain_switch(self):
        out = self.s.apply("aa:01", 1, "record", {X + "enableSchedule": "false"}, confirm=True)
        self.assertTrue(out["ok"], out)
        self.assertIn("<enableSchedule>false</enableSchedule>", self.nvr.puts[-1][1])
        self.assertEqual(week_of(self.nvr.puts[-1][1]), ALL_C)          # the schedule itself is untouched


class RecorderOwn(Rig):
    def test_the_recorder_reads_its_disk_settings(self):
        r = self.s.read("aa:09", 1, "recorder")
        self.assertTrue(r["recorder"])
        self.assertEqual([s["id"] for s in r["sections"]], ["name", "storage"])      # name: the recorder page, 2026-11-07
        f = fields(r, "storage")
        self.assertEqual((f["x.LoopEnable"]["kind"], f["x.LoopEnable"]["value"], f["x.LoopEnable"]["editable"]), ("bool", "true", True))

    def test_a_change_lands_in_the_recorders_history(self):
        out = self.s.apply("aa:09", 1, "storage", {"x.enableDormant": "false"}, who="hub", confirm=True)
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.nvr.puts[-1][0], EXT)
        self.assertIn("<enableDormant>false</enableDormant>", self.nvr.puts[-1][1])
        self.assertIn("<LoopEnable>true</LoopEnable>", self.nvr.puts[-1][1])
        self.assertEqual(self.hist[-1][0], "aa:09")
        self.assertTrue(self.s.undo("aa:09", out["change"]["id"])["ok"])
        self.assertIn("<enableDormant>true</enableDormant>", self.nvr.settings[EXT])

    def test_camera_and_recorder_groups_do_not_mix(self):
        for key, group in (("aa:01", "recorder"), ("aa:09", "basic"), ("aa:09", "record")):
            with self.assertRaises(hiksettings.SettingsError):
                self.s.read(key, 1, group)
        with self.assertRaises(hiksettings.SettingsError):
            self.s.apply("aa:01", 1, "storage", {"x.LoopEnable": "false"}, confirm=True)
        with self.assertRaises(hiksettings.SettingsError):
            self.s.apply("aa:09", 1, "picture", {"img.Color/brightnessLevel": "50"})
        with self.assertRaises(hiksettings.SettingsError):
            self.s.ptz("aa:09")
        self.assertEqual(self.nvr.puts, [])

    def test_no_saved_login_or_switch_off(self):
        self.cfg["features"]["camera_manage"] = False
        with self.assertRaises(hiksettings.SettingsError) as c:
            self.s.apply("aa:09", 1, "storage", {"x.LoopEnable": "false"}, confirm=True)
        self.assertEqual(c.exception.status, 403)
        creds.set_("aa:09")
        self.e.inventory(force=True)
        with self.assertRaises(hiksettings.SettingsError) as c:
            self.s.read("aa:09", 1, "recorder")
        self.assertEqual(c.exception.status, 409)


class WeekHelpers(unittest.TestCase):
    def test_segments_and_summary(self):
        self.assertEqual(hiksettings._segments("C" * 8 + "-" * 10 + "M" * 6), [(0, 8, "C"), (18, 24, "M")])
        names = {"C": "Continuous", "M": "Motion"}
        self.assertEqual(hiksettings._week_shown("|".join(["-" * 24] * 7), names), "Never")
        self.assertEqual(hiksettings._week_shown("|".join(["-" * 24] * 6 + ["C" * 24]), names),
                         "Mon–Sat: not recorded; Sun: Continuous 00–24")


if __name__ == "__main__":
    unittest.main()
