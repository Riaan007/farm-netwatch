"""Detection and deterrence settings (Phase 4a: app/hikdetect.py through hiksettings)
against a fake camera serving REAL documents (tests/fixtures/hik_detect, read from a
home camera): motion grid, intrusion areas, line crossing, tampering, privacy masks,
alarm actions, and the strobe/siren JSON documents. Areas are drawn in page
coordinates (0..1000, origin top-left) and written in the camera's own (its normalized
screen, origin bottom-left); every change is read back, logged and undoable.

Run inside the site image (nothing leaves the box):
  docker run --rm --network none -v "$PWD/app:/app:ro" -v "$PWD/tests:/tests:ro" -e NETWATCH_DATA=/tmp/nw \\
    -e PYTHONDONTWRITEBYTECODE=1 --entrypoint python farm-netwatch:netcfg /tests/test_hikdetect.py -v
"""
import json
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

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "hik_detect")
HIK = "Hangzhou Hikvision Digital Technology"
MD = "/ISAPI/System/Video/inputs/channels/1/motionDetection"
FD = "/ISAPI/Smart/FieldDetection/1"
LD = "/ISAPI/Smart/LineDetection/1"
PM = "/ISAPI/System/Video/inputs/channels/1/privacyMask"
VMD = "/ISAPI/Event/triggers/VMD-1"
WL = "/ISAPI/Event/triggers/notifications/channels/1/whiteLightAlarm"
AU = "/ISAPI/Event/triggers/notifications/AudioAlarm"
AREA1 = "fd.FieldDetectionRegionList/FieldDetectionRegion[id=1]/area"
AREA3 = "fd.FieldDetectionRegionList/FieldDetectionRegion[id=3]/area"
LINE1 = "ld.LineItemList/LineItem[id=1]/line"


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

    def read(self):
        return self.s.read("aa:01", 1, "detect")

    def fields(self, section):
        r = self.read()
        return {f["id"]: f for f in next(x for x in r["sections"] if x["id"] == section)["fields"]}

    def apply(self, section, changes, **kw):
        kw.setdefault("confirm", True)
        return self.s.apply("aa:01", 1, section, changes, who="test", **kw)

    def body(self, path):
        return [b for p, b in self.cam.puts if p == path][-1]


class Reading(Rig):
    def test_each_kind_of_detection_gets_a_tab(self):
        r = self.read()
        self.assertEqual(r["group"], "detect")
        self.assertEqual([s["id"] for s in r["sections"]], ["motion", "intrusion", "line", "tamper", "masks", "deter"])
        self.assertFalse(any(s["error"] for s in r["sections"]))
        # the basic group is untouched by this
        self.assertNotIn("motion", [s["id"] for s in self.s.read("aa:01")["sections"]])

    def test_a_camera_without_line_crossing_has_no_such_tab(self):
        for p in (LD, LD + "/capabilities", "/ISAPI/Event/triggers/linedetection-1"):
            del self.cam.settings[p]
        self.assertNotIn("line", [s["id"] for s in self.read()["sections"]])

    def test_motion_form_comes_from_the_camera(self):
        f = self.fields("motion")
        self.assertEqual(f["md.enabled"]["kind"], "bool")
        sens = f["md.MotionDetectionLayout/sensitivityLevel"]
        self.assertEqual((sens["min"], sens["max"], sens["step"], sens["value"]), (0, 100, 20, "60"))
        tgt = f["md.MotionDetectionLayout/targetType"]
        self.assertEqual((tgt["kind"], tgt["value"], [o["v"] for o in tgt["opts"]]), ("set", "human,vehicle", ["human", "vehicle"]))
        grid = f["md.MotionDetectionLayout/layout/gridMap"]
        self.assertEqual((grid["rows"], grid["cols"], grid["editable"]), (18, 22, True))
        self.assertEqual(grid["shown"], "396 of 396 squares")
        links = f["tr.links"]
        self.assertEqual(links["value"], "center,record")
        self.assertEqual([o["v"] for o in links["opts"]], ["center", "record", "beep", "whiteLight", "FTP"])

    def test_areas_are_turned_to_page_coordinates(self):
        a = self.fields("intrusion")[AREA1]
        # camera: (252,2) (309,597) (0,469) (4,4) on 1000x1000, origin bottom-left
        self.assertEqual(a["value"], "252,998 309,403 0,531 4,996")
        self.assertEqual((a["min"], a["max"], a["editable"]), (4, 4, True))
        self.assertEqual(self.fields("intrusion")[AREA3]["value"], "")      # never drawn
        self.assertNotIn("_region", a)                                       # internals stay on the Pi

    def test_line_and_its_direction(self):
        f = self.fields("line")
        self.assertEqual(f[LINE1]["value"], "333,883 5,862")
        d = f["ld.LineItemList/LineItem[id=2]/directionSensitivity"]
        self.assertEqual(d["shown"], "Only A → B")

    def test_strobe_and_siren_from_the_json_documents(self):
        f = self.fields("deter")
        self.assertEqual(f["wl.frequency"]["value"], "normallyOn")
        self.assertEqual((f["wl.durationTime"]["min"], f["wl.durationTime"]["max"]), (1, 60))
        snd = f["au.audioID"]
        self.assertEqual((snd["kind"], snd["value"], len(snd["opts"])), ("enum", "10", 12))
        self.assertEqual(snd["opts"][0], {"v": "1", "label": "Siren"})

    def test_switch_off_reads_but_never_writes(self):
        self.cfg["features"]["camera_manage"] = False
        self.assertFalse(self.read()["can_change"])
        with self.assertRaises(hiksettings.SettingsError) as c:
            self.apply("motion", {"md.enabled": "false"})
        self.assertEqual(c.exception.status, 403)
        self.assertEqual(self.cam.puts, [])


class Changing(Rig):
    def test_detection_is_careful(self):
        with self.assertRaises(hiksettings.SettingsError) as c:
            self.apply("motion", {"md.enabled": "false"}, confirm=False)
        self.assertTrue(c.exception.extra.get("needs_confirm"))
        self.assertEqual(self.cam.puts, [])

    def test_drawing_an_area_writes_camera_coordinates_and_undo_puts_them_back(self):
        r = self.apply("intrusion", {AREA1: "100,100 900,100 900,900 100,900"})
        self.assertTrue(r["ok"], r)
        body = self.body(FD)
        reg = re.search(r"<FieldDetectionRegion[ >].*?</FieldDetectionRegion>", body, re.S).group(0)
        xy = [(int(x), int(y)) for x, y in re.findall(r"<positionX>(\d+)</positionX><positionY>(\d+)</positionY>", reg)]
        self.assertEqual(xy, [(100, 900), (900, 900), (900, 100), (100, 100)])
        self.assertIn("<id>2</id>", body)                       # the other areas went back too
        self.assertEqual(r["change"]["fields"][0]["shown_after"], "4 points drawn")
        u = self.s.undo("aa:01", r["change"]["id"], who="test")
        self.assertTrue(u["ok"], u)
        self.assertEqual(self.fields("intrusion")[AREA1]["value"], "252,998 309,403 0,531 4,996")

    def test_the_wrong_number_of_points_is_refused_before_anything_is_sent(self):
        for bad in ("100,100 900,100 900,900", "100,100 900,100 900,900 1001,5", "a,b", "100,100 100,100 9,9 8,8"):
            with self.assertRaises(hiksettings.SettingsError):
                self.apply("intrusion", {AREA1: bad})
        self.assertEqual(self.cam.puts, [])

    def test_drawing_an_area_that_had_none_and_undoing_it(self):
        r = self.apply("intrusion", {AREA3: "10,10 500,10 500,500 10,500",
                                     "fd.FieldDetectionRegionList/FieldDetectionRegion[id=3]/enabled": "true"})
        self.assertTrue(r["ok"], r)
        body = self.body(FD)
        reg3 = re.search(r"<FieldDetectionRegion[^>]*>\s*<id>3</id>.*?</FieldDetectionRegion>", body, re.S).group(0)
        self.assertLess(reg3.index("RegionCoordinatesList"), reg3.index("detectionTarget"))
        u = self.s.undo("aa:01", r["change"]["id"], who="test")
        self.assertTrue(u["ok"], u)
        self.assertEqual(self.fields("intrusion")[AREA3]["value"], "")

    def test_a_line_needs_two_ends(self):
        r = self.apply("line", {LINE1: "0,500 1000,500"})
        self.assertTrue(r["ok"], r)
        self.assertIn("<positionX>1000</positionX><positionY>500</positionY>", self.body(LD))
        with self.assertRaises(hiksettings.SettingsError):
            self.apply("line", {LINE1: "0,500"})

    def test_alarm_actions_add_and_remove_without_touching_the_rest(self):
        r = self.apply("motion", {"tr.links": "record,beep,whiteLight"})
        self.assertTrue(r["ok"], r)
        body = self.body(VMD)
        self.assertNotIn("<notificationMethod>center</notificationMethod>", body)
        self.assertIn("<id>record-1</id>", body)
        self.assertIn("<notificationMethod>beep</notificationMethod>", body)
        self.assertIn("<WhiteLightAction><whiteLightDurationTime>0</whiteLightDurationTime></WhiteLightAction>", body)
        self.assertEqual(r["change"]["fields"][0]["shown_after"], "Record, Siren / voice warning, Strobe light")
        u = self.s.undo("aa:01", r["change"]["id"], who="test")
        self.assertTrue(u["ok"], u)
        self.assertEqual(self.fields("motion")["tr.links"]["value"], "center,record")

    def test_an_action_the_camera_cant_do_for_that_alarm_is_refused(self):
        with self.assertRaises(hiksettings.SettingsError):
            self.apply("tamper", {"td.enabled": "true"}, expect={"td.enabled": "true"})   # conflict: it is off
        with self.assertRaises(hiksettings.SettingsError):
            self.apply("motion", {"tr.links": "center,record,email"})
        self.assertEqual(self.cam.puts, [])

    def test_privacy_mask_box_in_the_cameras_704_by_576_screen(self):
        r = self.apply("masks", {"pm.enabled": "true", "pm.PrivacyMaskRegionList": "0,0,500,250"})
        self.assertTrue(r["ok"], r)
        body = self.body(PM)
        self.assertIn("<PrivacyMaskRegion><id>1</id><enabled>true</enabled><RegionCoordinatesList>", body)
        xy = [(int(x), int(y)) for x, y in re.findall(r"<positionX>(\d+)</positionX><positionY>(\d+)</positionY>", body)]
        self.assertEqual(xy, [(0, 432), (0, 576), (352, 576), (352, 432)])   # top-left quarter-ish, y up
        self.assertEqual(self.fields("masks")["pm.PrivacyMaskRegionList"]["value"], "0,0,500,250")
        u = self.s.undo("aa:01", r["change"]["id"], who="test")
        self.assertTrue(u["ok"], u)
        f = self.fields("masks")
        self.assertEqual((f["pm.enabled"]["value"], f["pm.PrivacyMaskRegionList"]["value"]), ("false", ""))

    def test_too_many_or_tiny_boxes_are_refused(self):
        for bad in (";".join(["0,0,100,100"] * 5), "0,0,5,5", "0,0,100"):
            with self.assertRaises(hiksettings.SettingsError):
                self.apply("masks", {"pm.PrivacyMaskRegionList": bad})
        self.assertEqual(self.cam.puts, [])

    def test_motion_grid(self):
        empty = "000000" * 18
        r = self.apply("motion", {"md.MotionDetectionLayout/layout/gridMap": empty})
        self.assertTrue(r["ok"], r)
        self.assertIn(f"<gridMap>{empty}</gridMap>", self.body(MD))
        self.assertEqual(r["change"]["fields"][0]["shown_after"], "0 of 396 squares")
        with self.assertRaises(hiksettings.SettingsError):          # padding bits beyond 22 columns
            self.apply("motion", {"md.MotionDetectionLayout/layout/gridMap": "ffffff" * 18})
        with self.assertRaises(hiksettings.SettingsError):
            self.apply("motion", {"md.MotionDetectionLayout/layout/gridMap": "fffffc" * 17})

    def test_people_vehicle_filter_and_sensitivity_steps(self):
        r = self.apply("motion", {"md.MotionDetectionLayout/targetType": "vehicle,human"})
        self.assertTrue(r["unchanged"] if "unchanged" in r else r["ok"])     # same set, camera order
        r = self.apply("motion", {"md.MotionDetectionLayout/targetType": "human"})
        self.assertIn("<targetType>human</targetType>", self.body(MD))
        with self.assertRaises(hiksettings.SettingsError):
            self.apply("motion", {"md.MotionDetectionLayout/sensitivityLevel": "55"})
        with self.assertRaises(hiksettings.SettingsError):
            self.apply("motion", {"md.MotionDetectionLayout/targetType": "cat"})

    def test_strobe_writes_json_and_keeps_numbers_numbers(self):
        r = self.apply("deter", {"wl.frequency": "high", "wl.durationTime": "20"})
        self.assertTrue(r["ok"], r)
        doc = json.loads(self.body(WL))["WhiteLightAlarm"]
        self.assertEqual((doc["frequency"], doc["durationTime"], doc["channelID"]), ("high", 20, 1))
        self.assertIsInstance(doc["TimeRangeList"], list)               # the schedule went back untouched
        r = self.apply("deter", {"au.audioID": "1", "au.audioVolume": "50"})
        self.assertTrue(r["ok"], r)
        doc = json.loads(self.body(AU))["AudioAlarm"]
        self.assertEqual((doc["audioID"], doc["audioVolume"]), (1, 50))
        with self.assertRaises(hiksettings.SettingsError):
            self.apply("deter", {"au.audioID": "99"})
        with self.assertRaises(hiksettings.SettingsError):
            self.apply("deter", {"wl.durationTime": "61"})

    def test_a_refused_json_write_reports_the_cameras_reason(self):
        self.cam.put_status[WL] = (400, 6)
        with self.assertRaises(hiksettings.SettingsError) as c:
            self.apply("deter", {"wl.frequency": "low"})
        self.assertIn("invalidContent", str(c.exception))
        self.assertEqual(self.hist, [])

    def test_history_gets_every_detection_change(self):
        self.apply("motion", {"md.enabled": "false"})
        self.assertEqual([h["section"] for h in self.hist], ["motion"])
        self.assertEqual(self.hist[0]["fields"][0]["shown_after"], "Off")


class Coordinates(unittest.TestCase):
    def test_round_trip_in_a_704_by_576_screen(self):
        for u, v in [(0, 0), (1000, 1000), (333, 777), (1, 999)]:
            x, y = hiksettings._to_dev(u, v, 704, 576)
            u2, v2 = hiksettings._to_ui(x, y, 704, 576)
            self.assertLessEqual(abs(u2 - u) + abs(v2 - v), 4)

    def test_json_round_trip_is_exact(self):
        with open(os.path.join(FIX, "Event_triggers_notifications_AudioAlarm.json"), "rb") as f:
            raw = f.read()
        root = hiksettings._parse_json(raw)
        self.assertEqual(json.loads(hiksettings.serialize_json(root)), json.loads(raw))


if __name__ == "__main__":
    unittest.main()
