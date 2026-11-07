"""The recorder page (2026-11-07): every channel's recording on THIS recorder and a job
that sets chosen channels, the recorder's own alarms, its name, and a restart.

Run inside the site image (nothing leaves the box):
  docker run --rm --network none -v "$PWD/app:/app:ro" -v "$PWD/tests:/tests:ro" -e NETWATCH_DATA=/tmp/nw \\
    -e PYTHONDONTWRITEBYTECODE=1 --entrypoint python farm-netwatch:netcfg /tests/test_recorderpage.py -v
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_hikvia import Rig  # noqa: E402  (a camera + a recorder serving real home-NVR documents)

import hikbulk      # noqa: E402
import hikrecord    # noqa: E402
import hiksecurity  # noqa: E402
import hiksettings  # noqa: E402

TRACK1, TRACK2 = "/ISAPI/ContentMgmt/record/tracks/101", "/ISAPI/ContentMgmt/record/tracks/201"
X = "t.CustomExtensionList/CustomExtension/"
WEEK = "t.TrackSchedule/ScheduleBlockList/ScheduleBlock"
NS = 'xmlns="http://www.isapi.org/ver20/XMLSchema"'


def trigger(ev, methods):
    items = "".join(f"<EventTriggerNotification><id>{m}</id><notificationMethod>{m}</notificationMethod>"
                    "</EventTriggerNotification>" for m in methods)
    return (f'<?xml version="1.0" encoding="UTF-8"?><EventTrigger {NS}><id>{ev}</id><eventType>{ev}</eventType>'
            f"<EventTriggerNotificationList>{items}</EventTriggerNotificationList></EventTrigger>")


CAP = ("<EventTriggersCap><DiskfullTriggerCap><isSupportBeep>true</isSupportBeep><isSupportCenter>true</isSupportCenter>"
       "<isSupportEmail>true</isSupportEmail></DiskfullTriggerCap><DiskerrorTriggerCap><isSupportBeep>true</isSupportBeep>"
       "<isSupportCenter>true</isSupportCenter><isSupportIO>true</isSupportIO></DiskerrorTriggerCap>"
       "<NicbrokenTriggerCap><isSupportBeep>true</isSupportBeep><isSupportEmail>true</isSupportEmail></NicbrokenTriggerCap>"
       "</EventTriggersCap>")


class Page(Rig):
    def setUp(self):
        super().setUp()
        # a second channel, recorded on the sub stream by motion only
        self.nvr.channels.append({"id": "2", "name": "Dam", "ip": "10.9.9.2", "online": False})
        self.nvr.settings[TRACK2] = (self.nvr.settings[TRACK1].replace("101", "201").replace("<ActionRecordingMode>CMR<", "<ActionRecordingMode>MOTION<")
                                     .replace("channels/201</SrcUrl>", "channels/202</SrcUrl>"))
        self.nvr.settings[TRACK2 + "/capabilities"] = self.nvr.settings[TRACK1 + "/capabilities"].replace("channels/10", "channels/20")

    def test_every_channel_and_how_this_recorder_records_it(self):
        r = self.s.recording("aa:09")
        rows = {c["id"]: c for c in r["channels"]}
        t1, t2 = rows[1]["track"], rows[2]["track"]
        self.assertEqual((t1["on"], t1["schedule"]["kind"], t1["stream"], t1["pre"], t1["post"]), (True, "continuous", "main", "30", "30"))
        self.assertEqual((t2["schedule"]["kind"], t2["stream"]), ("motion", "sub"))
        self.assertEqual(rows[2]["online"], False)
        self.assertEqual([p for p, _b in self.nvr.puts], [], "reading writes nothing")

    def test_a_channel_track_is_changed_on_the_recorder_itself(self):
        out = self.s.apply("aa:09", 1, "track2", {WEEK: hikrecord.week_all("C")}, confirm=True, who="site page")
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.nvr.puts[-1][0], TRACK2)
        dev, entry = self.hist[-1]
        self.assertEqual((dev, entry["section_label"]), ("aa:09", "Recording — channel 2"))
        self.assertTrue(self.s.undo("aa:09", out["change"]["id"])["ok"])
        self.assertIn("<ActionRecordingMode>MOTION<", self.nvr.settings[TRACK2])
        with self.assertRaises(hiksettings.SettingsError):          # not a section of a camera
            self.s.apply("aa:01", 1, "track2", {WEEK: hikrecord.week_all("C")}, confirm=True)

    def test_a_record_job_sets_chosen_channels_and_undoes_as_one(self):
        tmp = tempfile.mkdtemp()
        j = hikbulk.Jobs(self.s).start(self.e, lambda: self.cfg, jobs_path=os.path.join(tmp, "j.json"),
                                       check_path=os.path.join(tmp, "c.json"))
        j.sync, j.sleep = True, (lambda s: None)
        r = j.start_record("aa:09", [1, 2], {"mode": "M", "stream": "main", "post": "60"}, who="t", pause_s=0)
        jb = j.get(r["job"]["id"])
        self.assertEqual((jb["state"], [it["status"] for it in jb["items"]]), ("done", ["ok", "ok"]), jb)
        self.assertIn("motion all week", jb["label"])
        rows = {c["id"]: c["track"] for c in self.s.recording("aa:09")["channels"]}
        self.assertEqual([(rows[n]["schedule"]["kind"], rows[n]["stream"], rows[n]["post"]) for n in (1, 2)],
                         [("motion", "main", "60"), ("motion", "main", "60")])
        u = j.get(j.start_undo(jb["id"], who="t")["job"]["id"])
        self.assertEqual(u["state"], "done")
        rows = {c["id"]: c["track"] for c in self.s.recording("aa:09")["channels"]}
        self.assertEqual([(rows[n]["schedule"]["kind"], rows[n]["stream"]) for n in (1, 2)], [("continuous", "main"), ("motion", "sub")])
        for bad in ({}, {"mode": "X"}, {"pre": "7"}):
            with self.assertRaises(hikbulk.BulkError):
                j.start_record("aa:09", [1], bad)
        with self.assertRaises(hikbulk.BulkError):
            j.start_record("aa:01", [1], {"mode": "C"})           # a camera is not a recorder

    def test_alarms_offer_only_what_the_recorder_takes_and_keep_other_actions(self):
        self.nvr.settings["/ISAPI/Event/triggersCap"] = CAP
        self.nvr.settings["/ISAPI/Event/triggers/diskfull"] = trigger("diskfull", [])
        self.nvr.settings["/ISAPI/Event/triggers/diskerror"] = trigger("diskerror", ["beep", "IO"])
        self.nvr.settings["/ISAPI/Event/triggers/nicbroken"] = trigger("nicbroken", [])
        a = {x["id"]: x for x in self.s.alarms("aa:09")["events"]}
        self.assertEqual(set(a), {"diskfull", "diskerror", "nicbroken"})
        self.assertEqual((a["diskfull"]["methods"], a["nicbroken"]["can"]), ([], ["beep", "email"]))
        out = self.s.alarm_set("aa:09", "diskerror", ["beep", "center"], who="site page")
        self.assertTrue(out["ok"], out)
        body = self.nvr.settings["/ISAPI/Event/triggers/diskerror"]
        self.assertIn("<notificationMethod>IO</notificationMethod>", body, "an action Netwatch doesn't show is kept")
        self.assertEqual(body.count("<notificationMethod>"), 3)
        self.assertEqual(self.hist[-1][1]["fields"][0]["shown_after"], "Beep, Tell the app")
        self.assertTrue(self.s.undo("aa:09", out["change"]["id"])["ok"])
        self.assertNotIn(">center<", self.nvr.settings["/ISAPI/Event/triggers/diskerror"])
        self.assertTrue(self.s.alarm_set("aa:09", "diskerror", ["beep"])["unchanged"])
        for ev, m in (("nope", []), ("diskfull", ["siren"])):
            with self.assertRaises(hiksettings.SettingsError):
                self.s.alarm_set("aa:09", ev, m)
        self.cfg["features"]["camera_manage"] = False
        with self.assertRaises(hiksettings.SettingsError):
            self.s.alarm_set("aa:09", "diskfull", ["beep"])

    def test_the_recorder_gets_a_name(self):
        self.nvr.settings["/ISAPI/System/deviceInfo"] = (f'<?xml version="1.0" encoding="UTF-8"?><DeviceInfo {NS}>'
                                                         "<deviceName>AcuSense</deviceName><model>DS-7732NXI-I4/S</model></DeviceInfo>")
        r = self.s.read("aa:09", 1, "recorder")
        f = {x["id"]: x for s in r["sections"] if s["id"] == "name" for x in s["fields"]}
        self.assertEqual(f["d.deviceName"]["value"], "AcuSense")
        out = self.s.apply("aa:09", 1, "name", {"d.deviceName": "Office NVR"}, who="t")
        self.assertTrue(out["ok"], out)
        self.assertIn("<deviceName>Office NVR</deviceName>", self.nvr.settings["/ISAPI/System/deviceInfo"])

    def test_restart_needs_the_switch_and_the_word(self):
        with self.assertRaises(hiksettings.SettingsError):
            self.s.restart("aa:09", confirm_text=hiksecurity.CONFIRM_WORD)        # risky work is off
        self.cfg["features"]["camera_risky"] = True
        with self.assertRaises(hiksettings.SettingsError):
            self.s.restart("aa:09", confirm_text="nope")
        self.assertFalse(getattr(self.nvr, "reboots", 0))
        out = self.s.restart("aa:09", confirm_text=hiksecurity.CONFIRM_WORD, who="t")
        self.assertTrue(out["ok"])
        self.assertEqual(self.nvr.reboots, 1)


if __name__ == "__main__":
    unittest.main()
