"""The ISAPI explorer (Phase 4d, part 3): any document, values only, secrets never out.

Run inside the site image (nothing leaves the box):
  docker run --rm --network none -v "$PWD/app:/app:ro" -v "$PWD/tests:/tests:ro" -e NETWATCH_DATA=/tmp/nw \\
    -e PYTHONDONTWRITEBYTECODE=1 --entrypoint python farm-netwatch:netcfg /tests/test_hikexplorer.py -v
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_hiksecurity import Rig, WORD  # noqa: E402  (camera + admin account + real security documents)

import hikexplorer  # noqa: E402
import hikisapi     # noqa: E402
import hiksettings  # noqa: E402

EZ = "/ISAPI/System/Network/EZVIZ"
LOCK = "/ISAPI/Security/illegalLoginLock"


class Explorer(Rig):
    def test_read_hides_secrets_and_says_if_it_can_be_changed(self):
        r = self.s.explore_read("aa:01", EZ)
        self.assertTrue(r["writable"])
        self.assertNotIn("ABCDEF", json.dumps(r))                     # the Hik-Connect code
        self.assertIn(f"<verificationCode>{hikexplorer.SECRET}</verificationCode>", r["doc"])
        self.assertIn("\n  <enabled>", r["doc"])                     # indented to edit
        self.assertFalse(self.s.explore_read("aa:01", "/ISAPI/Security/adminAccesses")["writable"])

    def test_a_value_change_keeps_the_secret_and_can_be_undone(self):
        doc = self.s.explore_read("aa:01", EZ)["doc"].replace("<redirect>true</redirect>", "<redirect>false</redirect>")
        p = self.s.explore_write("aa:01", EZ, doc, preview=True)
        self.assertEqual(p["changes"], [{"path": "/EZVIZ/redirect", "before": "true", "after": "false"}])
        self.assertEqual(self.dev.puts, [])
        with self.assertRaises(hiksettings.SettingsError) as c:
            self.s.explore_write("aa:01", EZ, doc)
        self.assertEqual(c.exception.extra.get("needs_word"), WORD)
        out = self.s.explore_write("aa:01", EZ, doc, confirm_text=WORD, who="site page")
        self.assertTrue(out["ok"], out)
        written = self.dev.puts[-1][1]
        self.assertIn("<redirect>false</redirect>", written)
        self.assertIn("<verificationCode>ABCDEF</verificationCode>", written)   # the device's own secret went back
        self.assertNotIn("ABCDEF", json.dumps(out) + json.dumps(self.hist))
        u = self.s.undo("aa:01", out["change"]["id"])
        self.assertTrue(u["ok"], u)
        self.assertIn("<redirect>true</redirect>", self.dev.settings[EZ])
        self.assertIn("<verificationCode>ABCDEF</verificationCode>", self.dev.settings[EZ])

    def test_undo_refuses_when_the_device_moved_on(self):
        doc = self.s.explore_read("aa:01", LOCK)["doc"].replace(">7<", ">9<")
        out = self.s.explore_write("aa:01", LOCK, doc, confirm_text=WORD)
        self.dev.settings[LOCK] = self.dev.settings[LOCK].replace(">9<", ">11<")
        with self.assertRaises(hiksettings.SettingsError) as c:
            self.s.undo("aa:01", out["change"]["id"])
        self.assertEqual(c.exception.status, 409)

    def test_only_values_no_secrets_no_risky_paths(self):
        doc = self.s.explore_read("aa:01", EZ)["doc"]
        bad = [doc.replace("<redirect>true</redirect>", ""),                                   # an element gone
               doc.replace("<redirect>true</redirect>", "<redirect>true</redirect><extra>1</extra>"),
               doc.replace(f"<verificationCode>{hikexplorer.SECRET}", "<verificationCode>NEWCODE"),
               doc.replace('version="2.0"', 'version="3.0"'),                                  # an attribute
               "<!DOCTYPE x><EZVIZ/>", "not xml"]
        for b in bad:
            with self.assertRaises(hiksettings.SettingsError):
                self.s.explore_write("aa:01", EZ, b, confirm_text=WORD)
        for path in ("/ISAPI/System/reboot", "/ISAPI/System/factoryReset", "/ISAPI/Security/users",
                     "/ISAPI/System/Network/interfaces/1", "/ISAPI/ContentMgmt/InputProxy/channels/3",
                     "/ISAPI/ContentMgmt/Storage/hdd/1/format", "/ISAPI/System/time?x=1", "/etc/passwd", "/ISAPI/../x"):
            with self.assertRaises(hiksettings.SettingsError):
                self.s.explore_write("aa:01", path, doc, confirm_text=WORD)
        for path in ("/ISAPI/System/reboot", "/ISAPI/System/updateFirmware"):
            with self.assertRaises(hiksettings.SettingsError):
                self.s.explore_read("aa:01", path)
        self.assertEqual(self.dev.puts, [])

    def test_needs_both_switches(self):
        self.cfg["features"]["camera_risky"] = False
        with self.assertRaises(hiksettings.SettingsError) as c:
            self.s.explore_read("aa:01", EZ)
        self.assertEqual(c.exception.status, 403)


class Secrets(unittest.TestCase):
    def test_which_tags_are_secret(self):
        root = hikisapi.parse(b"<a><password>x</password><userName>u</userName><sharedKey>k</sharedKey>"
                              b"<keyFrameInterval>2</keyFrameInterval><encryptionKey>e</encryptionKey><psk>p</psk>"
                              b"<verificationCode>v</verificationCode><enabled>true</enabled></a>")
        red = {hikisapi.tag(e): e.text for e in hikexplorer.redact(root)}
        self.assertEqual({k for k, v in red.items() if v == hikexplorer.SECRET},
                         {"password", "sharedKey", "encryptionKey", "psk", "verificationCode"})


if __name__ == "__main__":
    unittest.main()
