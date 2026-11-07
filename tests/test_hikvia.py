"""Changing a camera through its recorder, and PTZ (Phase 4b).

A camera with no saved login of its own is changed through its NVR's proxy documents
(tests/fixtures/hik_nvr: REAL documents from a home NVR, channel 1). PTZ moves are
"momentary" (the camera stops by itself), with a continuous move + stop for old
firmware, and they reach History at most once per 10 minutes per camera.

Run inside the site image (nothing leaves the box):
  docker run --rm --network none -v "$PWD/app:/app:ro" -v "$PWD/tests:/tests:ro" -e NETWATCH_DATA=/tmp/nw \\
    -e PYTHONDONTWRITEBYTECODE=1 --entrypoint python farm-netwatch:netcfg /tests/test_hikvia.py -v
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

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "hik_nvr")
HIK = "Hangzhou Hikvision Digital Technology"
BRIGHT = "img.Color/brightnessLevel"
IMGP = "/ISAPI/ContentMgmt/ImageProxy/channels/1"


class Rig(unittest.TestCase):
    model = "DS-2CD2046G2-IU"
    chan_online = True

    def setUp(self):
        hikisapi._seen.clear()
        for k in list(creds.keys_with_creds()):
            creds.set_(k)
        self.cam = Device().start()
        self.nvr = Device(channels=[]).start().load_settings(FIX)
        self.nvr.channels += [{"id": "1", "name": "Gate", "ip": self.cam.addr, "online": self.chan_online}]
        self.devs = [
            {"key": "aa:01", "ip": self.cam.addr, "category": "camera", "vendor": HIK, "online": True,
             "ports": [80, 8000], "name": "Gate cam", "model": self.model},
            {"key": "aa:09", "ip": self.nvr.addr, "category": "nvr", "vendor": HIK, "online": True,
             "ports": [80, 8000], "model": "DS-7732NXI-I4/S", "name": "House NVR"},
        ]
        creds.set_("aa:09", "admin", "pw")
        self.cfg = {"features": {"camera_pictures": True, "camera_manage": True}, "camera": {}, "alerts": {}}
        self.e = campics.Engine()
        self.e.start(lambda: [dict(d) for d in self.devs], lambda: {}, lambda: {}, cfg=lambda: self.cfg, thread=False)
        self.e._read_nvrs()
        self.hist = []
        self.log = tempfile.mktemp(suffix=".json")
        self.s = hiksettings.Settings().start(self.e, lambda: self.cfg, on_change=lambda d, e: self.hist.append((d["key"], e)),
                                              log_path=self.log)

    def tearDown(self):
        self.cam.stop()
        self.nvr.stop()
        if os.path.exists(self.log):
            os.remove(self.log)


class ThroughTheRecorder(Rig):
    def test_a_camera_without_its_own_login_is_read_through_the_recorder(self):
        r = self.s.read("aa:01")
        self.assertEqual((r["route"], r["via"]["channel"], r["routes"]), ("nvr", 1, ["nvr"]))
        self.assertEqual([s["id"] for s in r["sections"]], ["picture", "osd", "main", "sub"])     # no clock
        pic = {f["id"]: f for f in r["sections"][0]["fields"]}
        self.assertTrue(pic[BRIGHT]["editable"])
        self.assertFalse(any(s["error"] for s in r["sections"]))
        self.assertFalse(any(p.startswith("/ISAPI/Image/") for _m, p, _a in self.cam.hits))  # the camera itself is never asked

    def test_a_change_goes_to_the_recorder_and_lands_in_its_history(self):
        before = pic_value(self.s.read("aa:01"))
        new = "60" if before != "60" else "70"
        out = self.s.apply("aa:01", 1, "picture", {BRIGHT: new}, who="site page", expect={BRIGHT: before})
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.nvr.puts[-1][0], IMGP)
        self.assertIn(f"<brightnessLevel>{new}</brightnessLevel>", self.nvr.puts[-1][1])
        dev, entry = self.hist[-1]
        self.assertEqual(dev, "aa:09")
        self.assertIn("through the recorder (channel 1)", entry["section_label"])
        self.assertEqual((entry["route"], entry["channel"]), ("nvr", 1))
        u = self.s.undo("aa:01", out["change"]["id"], who="site page")
        self.assertTrue(u["ok"], u)
        self.assertEqual(pic_value(self.s.read("aa:01")), before)
        self.assertEqual(self.nvr.puts[-1][0], IMGP)

    def test_detection_through_the_recorder_is_motion_and_masks(self):
        r = self.s.read("aa:01", 1, "detect")
        self.assertEqual([s["id"] for s in r["sections"]], ["motion", "masks"])
        motion = {f["id"] for f in r["sections"][0]["fields"]}
        self.assertNotIn("tr.links", motion)
        self.assertIn("md.MotionDetectionLayout/layout/gridMap", motion)

    def test_own_login_wins_but_the_recorder_can_be_asked_for(self):
        creds.set_("aa:01", "admin", "pw")
        self.e.inventory(force=True)
        self.assertEqual(self.s.read("aa:01")["route"], "direct")
        r = self.s.read("aa:01", route="nvr")
        self.assertEqual((r["route"], sorted(r["routes"])), ("nvr", ["direct", "nvr"]))

class LostChannel(Rig):
    chan_online = False

    def test_a_channel_the_recorder_lost_is_not_touched(self):
        with self.assertRaises(hiksettings.SettingsError) as c:
            self.s.apply("aa:01", 1, "picture", {BRIGHT: "55"})
        self.assertEqual(c.exception.status, 409)
        self.assertEqual(self.nvr.puts, [])


class ChannelName(Rig):
    def test_the_channel_name_is_never_written_through_the_recorder(self):
        r = self.s.read("aa:01")
        osd = {f["id"] for f in next(s for s in r["sections"] if s["id"] == "osd")["fields"]}
        self.assertNotIn("name.name", osd)
        with self.assertRaises(hiksettings.SettingsError):
            self.s.apply("aa:01", 1, "osd", {"name.name": "Hacked"})


def pic_value(r):
    return next(f["value"] for f in r["sections"][0]["fields"] if f["id"] == BRIGHT)


class Ptz(Rig):
    model = "DS-2DE4425IW-DE"

    def own(self, **ptz):
        creds.set_("aa:01", "admin", "pw")
        self.cam.ptz = {"caps": True, "presets": [(1, "Gate"), (3, "Dam")], **ptz}
        self.e.inventory(force=True)

    def test_capabilities_and_presets(self):
        self.own()
        p = self.s.ptz("aa:01")
        self.assertEqual((p["has_ptz"], p["guessed"], p["axes"]), (True, False, {"pan": True, "tilt": True, "zoom": True}))
        self.assertEqual(p["presets"], [{"id": 1, "name": "Gate", "kind": "preset"}, {"id": 3, "name": "Dam", "kind": "preset"}])
        self.assertFalse(p["can_point"])                     # the camera didn't say it can aim at a spot

    def test_special_presets_are_sorted_and_the_maintenance_ones_need_a_confirm(self):
        # Tri-Lectro's DS-2SE7C425MWG-EB/26 lists ~40 of these next to its 7 presets.
        self.own(presets=[(1, "Gate"), (33, "Auto-flip"), (36, "Call patrol 2"), (47, "Alarm light on"), (50, "Shed"),
                          (60, "Preset 60"), (94, "Remote reboot"), (96, "Stop a scan"), (120, "Far gate")])
        kinds = {p["id"]: p["kind"] for p in self.s.ptz("aa:01")["presets"]}
        self.assertEqual(kinds, {1: "preset", 33: "function", 36: "tour", 47: "function", 50: "preset",
                                 60: "preset", 94: "setup", 96: "tour", 120: "preset"})   # "Preset" is no "reset"
        with self.assertRaises(hiksettings.SettingsError) as c:
            self.s.ptz_do("aa:01", 1, "goto", preset=94, name="Remote reboot")
        self.assertTrue(c.exception.extra.get("needs_confirm"))
        self.assertFalse([p for m, p, _b in self.cam.ptz_calls if m == "PUT"], "a reboot went out without a confirm")
        self.s.ptz_do("aa:01", 1, "goto", preset=94, name="Remote reboot", confirm=True, who="hub")
        self.assertEqual(self.cam.ptz_calls[-1][:2], ("PUT", "/ISAPI/PTZCtrl/channels/1/presets/94/goto"))
        self.assertEqual(self.hist[-1][1]["fields"][0]["shown_after"], "went to preset 94 (Remote reboot)")
        self.s.ptz_do("aa:01", 1, "goto", preset=36, name="Call patrol 2")       # a patrol needs no confirm

    def test_click_to_aim_and_drag_a_box_to_zoom(self):
        self.own(point=True)
        self.assertTrue(self.s.ptz("aa:01")["can_point"])
        self.s.ptz_do("aa:01", 1, "point", box=[0.75, 0.25, 0.75, 0.25], who="site page")
        m, path, body = self.cam.ptz_calls[-1]
        self.assertEqual((m, path), ("PUT", "/ISAPI/PTZCtrl/channels/1/position3D"))
        # 0..255 from the bottom-left: right of centre and ABOVE it = a big Y.
        self.assertIn("<StartPoint><positionX>191</positionX><positionY>191</positionY></StartPoint>", body)
        self.assertIn("<EndPoint><positionX>191</positionX><positionY>191</positionY></EndPoint>", body)
        self.s.ptz_do("aa:01", 1, "point", box=[0.1, 0.1, 0.4, 0.3])
        body = self.cam.ptz_calls[-1][2]
        self.assertIn("<StartPoint><positionX>26</positionX><positionY>230</positionY></StartPoint>", body)
        self.assertIn("<EndPoint><positionX>102</positionX><positionY>178</positionY></EndPoint>", body)
        self.assertEqual(self.hist[-1][1]["fields"][0]["shown_after"], "pointed at a spot")   # one History line per session
        for bad in (None, [0.1, 0.2], [0.1, 0.2, 1.5, 0.3], ["a", 0, 0, 0]):
            with self.assertRaises(hiksettings.SettingsError):
                self.s.ptz_do("aa:01", 1, "point", box=bad)

    def test_a_move_stops_by_itself_and_is_capped(self):
        self.own()
        self.assertTrue(self.s.ptz_do("aa:01", 1, "move", pan=50, ms=9000, who="site page")["ok"])
        m, path, body = self.cam.ptz_calls[-1]
        self.assertEqual((m, path), ("PUT", "/ISAPI/PTZCtrl/channels/1/momentary"))
        self.assertIn("<pan>50</pan><tilt>0</tilt><zoom>0</zoom><Momentary><duration>1500</duration>", body)

    def test_old_firmware_gets_a_move_and_always_a_stop(self):
        self.own(momentary=False)
        self.s.ptz_do("aa:01", 1, "move", zoom=-30, ms=100)
        cont = [b for m, p, b in self.cam.ptz_calls if p.endswith("/continuous")]
        self.assertEqual(len(cont), 2)
        self.assertIn("<zoom>-30</zoom>", cont[0])
        self.assertIn("<pan>0</pan><tilt>0</tilt><zoom>0</zoom>", cont[1])

    def test_goto_a_preset_and_history_once_per_session(self):
        self.own()
        self.s.ptz_do("aa:01", 1, "move", tilt=20, who="hub")
        self.s.ptz_do("aa:01", 1, "move", tilt=-20, who="hub")
        self.s.ptz_do("aa:01", 1, "goto", preset=3, who="hub")
        self.assertEqual(self.cam.ptz_calls[-1][:2], ("PUT", "/ISAPI/PTZCtrl/channels/1/presets/3/goto"))
        lines = [e["fields"][0]["shown_after"] for _d, e in self.hist]
        self.assertEqual(lines, ["moved by hand", "went to preset 3"])

    def test_through_the_recorder_with_a_ptz_model(self):
        self.nvr.ptz = {"caps": False, "presets": [(2, "Road")]}
        p = self.s.ptz("aa:01")
        self.assertEqual((p["route"], p["has_ptz"], p["guessed"]), ("nvr", True, True))
        self.s.ptz_do("aa:01", 1, "move", pan=-40)
        self.assertEqual(self.nvr.ptz_calls[-1][1], "/ISAPI/ContentMgmt/PTZCtrlProxy/channels/1/momentary")
        self.assertEqual(self.hist[-1][0], "aa:09")

    def test_refused_without_the_switch_or_with_bad_values(self):
        self.own()
        for kw in ({"action": "move"}, {"action": "move", "pan": 101}, {"action": "goto", "preset": "x"},
                   {"action": "spin"}, {"action": "move", "pan": "a"}):
            with self.assertRaises(hiksettings.SettingsError):
                self.s.ptz_do("aa:01", 1, **kw)
        self.cfg["features"]["camera_manage"] = False
        with self.assertRaises(hiksettings.SettingsError) as c:
            self.s.ptz_do("aa:01", 1, "move", pan=10)
        self.assertEqual(c.exception.status, 403)
        self.assertFalse(any(m == "PUT" for m, _p, _b in self.cam.ptz_calls))


class NoPtz(Rig):
    def test_a_fixed_camera_has_no_ptz(self):
        creds.set_("aa:01", "admin", "pw")
        self.e.inventory(force=True)
        p = self.s.ptz("aa:01")
        self.assertEqual((p["has_ptz"], p["presets"]), (False, []))


if __name__ == "__main__":
    unittest.main()
