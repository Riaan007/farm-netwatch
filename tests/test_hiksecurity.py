"""Risky camera work (Phase 4d, part 1): logins, remote access and the Netwatch account.

tests/fixtures/hik_security holds REAL documents from a home camera (login lock, SSH,
UPnP, ports, Hik-Connect, platform access, security capabilities) with the secrets
replaced. The fake device keeps real users: logins, Operator rights, double
verification (a recorder wants the admin password inside user changes).

Run inside the site image (nothing leaves the box):
  docker run --rm --network none -v "$PWD/app:/app:ro" -v "$PWD/tests:/tests:ro" -e NETWATCH_DATA=/tmp/nw \\
    -e PYTHONDONTWRITEBYTECODE=1 --entrypoint python farm-netwatch:netcfg /tests/test_hiksecurity.py -v
"""
import json
import os
import string
import sys
import tempfile
import unittest

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import campics      # noqa: E402
import creds        # noqa: E402
import hikisapi     # noqa: E402
import hiksecurity  # noqa: E402
import hiksettings  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hik_fake import Device  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
HIK = "Hangzhou Hikvision Digital Technology"
WORD = hiksecurity.CONFIRM_WORD


class Rig(unittest.TestCase):
    category = "camera"

    def setUp(self):
        hikisapi._seen.clear()
        for k in list(creds.keys_with_creds()):
            creds.set_(k)
        self.dev = Device().start().load_settings(os.path.join(HERE, "fixtures", "hik_settings"))
        self.dev.load_settings(os.path.join(HERE, "fixtures", "hik_security"))
        self.dev.accounts = {"admin": {"pw": "pw", "level": "Administrator", "id": 1}}
        self.dev.rights = {"admin": {r: "true" for r in Device.RIGHTS}}
        if self.category == "nvr":
            self.dev.channels = []
        self.devs = [{"key": "aa:01", "ip": self.dev.addr, "category": self.category, "vendor": HIK, "online": True,
                      "ports": [80, 8000], "name": "Gate cam", "model": "DS-2CD2046G2-IU" if self.category == "camera" else "DS-7732NXI-I4/S"}]
        creds.set_("aa:01", "admin", "pw", "on the pole")
        self.cfg = {"features": {"camera_pictures": True, "camera_manage": True, "camera_risky": True}, "camera": {}, "alerts": {}}
        self.e = campics.Engine()
        self.e.start(lambda: [dict(d) for d in self.devs], lambda: {}, lambda: {}, cfg=lambda: self.cfg, thread=False)
        if self.category == "nvr":
            self.e._read_nvrs()
        self.hist = []
        self.log = tempfile.mktemp(suffix=".json")
        self.s = hiksettings.Settings().start(self.e, lambda: self.cfg, on_change=lambda d, e: self.hist.append(e),
                                              log_path=self.log)

    def tearDown(self):
        self.dev.stop()
        if os.path.exists(self.log):
            os.remove(self.log)

    def fields(self, r):
        return {f["id"]: f for f in r["sections"][0]["fields"]}


class SecurityTab(Rig):
    def test_read_shows_users_ports_and_the_three_settings(self):
        r = self.s.read("aa:01", 1, "security")
        i = r["info"]
        self.assertEqual(i["users"], [{"id": 1, "name": "admin", "level": "Admin"}])
        self.assertIn({"protocol": "HTTPS", "port": 443, "on": True}, i["ports"])
        self.assertEqual(i["hik_connect"], {"on": False, "registered": False})
        self.assertEqual(i["login"], {"uses": "admin", "netwatch": False, "admin_kept": False})
        self.assertTrue(any("logs in as admin" in w for w in i["warnings"]))
        self.assertNotIn("ABCDEF", json.dumps(r))                      # the Hik-Connect code is never read out
        self.assertEqual((r["risky"], r["confirm_word"], r["route"]), (True, WORD, "direct"))
        f = self.fields(r)
        self.assertEqual((f["ll.enabled"]["value"], f["ll.maxIllegalLoginTimes"]["value"], f["ssh.enabled"]["value"]),
                         ("true", "7", "false"))
        self.assertTrue(all(x["editable"] for x in f.values()))

    def test_a_change_needs_the_switch_and_the_typed_word(self):
        with self.assertRaises(hiksettings.SettingsError) as c:
            self.s.apply("aa:01", 1, "access", {"ssh.enabled": "true"}, confirm=True)
        self.assertEqual(c.exception.extra.get("needs_word"), WORD)
        self.cfg["features"]["camera_risky"] = False
        with self.assertRaises(hiksettings.SettingsError) as c:
            self.s.apply("aa:01", 1, "access", {"ssh.enabled": "true"}, confirm=True, confirm_text=WORD)
        self.assertEqual(c.exception.status, 403)
        self.assertEqual(self.dev.puts, [])

    def test_ssh_on_then_undo_without_the_word(self):
        out = self.s.apply("aa:01", 1, "access", {"ssh.enabled": "true", "ll.maxIllegalLoginTimes": "5"},
                           confirm=True, confirm_text=WORD, who="site page")
        self.assertTrue(out["ok"], out)
        self.assertIn("<enabled>true</enabled>", self.dev.settings["/ISAPI/System/Network/ssh"])
        self.assertIn("<maxIllegalLoginTimes>5</maxIllegalLoginTimes>", self.dev.settings["/ISAPI/Security/illegalLoginLock"])
        self.assertEqual(self.hist[-1]["risk"], "dangerous")
        self.assertTrue(self.s.undo("aa:01", out["change"]["id"])["ok"])
        self.assertIn("<enabled>false</enabled>", self.dev.settings["/ISAPI/System/Network/ssh"])
        for bad in ("2", "21"):
            with self.assertRaises(hiksettings.SettingsError):
                self.s.apply("aa:01", 1, "access", {"ll.maxIllegalLoginTimes": bad}, confirm=True, confirm_text=WORD)


class Account(Rig):
    def create(self):
        return self.s.account("aa:01", "create", who="site page", confirm_text=WORD)

    def test_create_switches_netwatch_to_its_own_operator_account(self):
        out = self.create()
        self.assertTrue(out["ok"], out)
        acc = self.dev.accounts["netwatch"]
        self.assertEqual((acc["level"], acc["id"]), ("Operator", 2))
        r = self.dev.rights["netwatch"]
        self.assertEqual({k for k, v in r.items() if v == "true"}, set(hiksecurity.RIGHTS_ON))
        saved = creds.get("aa:01")
        self.assertEqual((saved["username"], saved["password"], saved["notes"]), ("netwatch", acc["pw"], "on the pole"))
        self.assertEqual(creds.get_admin("aa:01"), {"username": "admin", "password": "pw"})
        # the password is nowhere in what Netwatch says or keeps about the change
        with open(self.log) as f:
            everything = json.dumps(out) + json.dumps(self.hist) + f.read()
        self.assertNotIn(acc["pw"], everything)
        self.assertEqual([r["shown_after"] for r in out["change"]["fields"]], ["netwatch (Operator)", "netwatch"])
        # Netwatch's normal work now goes in as netwatch
        before = self.dev.logins()
        self.s.read("aa:01")
        self.assertGreater(self.dev.logins(), before)
        info = self.s.read("aa:01", 1, "security")["info"]       # the risky tab still uses admin
        self.assertEqual(info["login"], {"uses": "netwatch", "netwatch": True, "admin_kept": True})
        self.assertEqual([u["name"] for u in info["users"]], ["admin", "netwatch"])

    def test_undo_removes_it_and_goes_back_to_admin(self):
        out = self.create()
        u = self.s.undo("aa:01", out["change"]["id"], who="site page")
        self.assertTrue(u["ok"], u)
        self.assertNotIn("netwatch", self.dev.accounts)
        self.assertEqual(creds.get("aa:01"), {"username": "admin", "password": "pw", "notes": "on the pole"})
        self.assertEqual(creds.get_admin("aa:01"), {"username": "", "password": ""})
        self.assertTrue(u["change"]["no_undo"])
        with self.assertRaises(hiksettings.SettingsError):
            self.s.undo("aa:01", u["change"]["id"])

    def test_needs_both_switches_and_the_word(self):
        with self.assertRaises(hiksettings.SettingsError):
            self.s.account("aa:01", "create")
        self.cfg["features"]["camera_risky"] = False
        with self.assertRaises(hiksettings.SettingsError) as c:
            self.s.account("aa:01", "create", confirm_text=WORD)
        self.assertEqual(c.exception.status, 403)
        self.assertEqual(self.dev.user_calls, [])

    def test_a_failure_after_the_user_was_made_removes_it_again(self):
        self.dev.refuse_rights = True
        with self.assertRaises(hiksettings.SettingsError) as c:
            self.create()
        self.assertIn("removed again", str(c.exception))
        self.assertNotIn("netwatch", self.dev.accounts)
        self.assertEqual(creds.get("aa:01")["username"], "admin")
        self.assertEqual(creds.get_admin("aa:01")["username"], "")
        self.assertEqual(self.hist, [])

    def test_refuses_a_netwatch_user_it_did_not_make_and_a_non_admin_login(self):
        self.dev.accounts["netwatch"] = {"pw": "other", "level": "Operator", "id": 5}
        with self.assertRaises(hiksettings.SettingsError) as c:
            self.create()
        self.assertIn("already exists", str(c.exception))
        del self.dev.accounts["netwatch"]
        self.dev.accounts["viewer1"] = {"pw": "v", "level": "Viewer", "id": 3}
        creds.set_("aa:01", "viewer1", "v")
        with self.assertRaises(hiksettings.SettingsError):
            self.create()
        self.assertEqual([c for c in self.dev.user_calls if c[0] == "POST"], [])

    def test_the_admin_login_has_its_own_one_try(self):
        self.create()
        self.dev.accounts["admin"]["pw"] = "changed-on-the-device"
        with self.assertRaises(hiksettings.SettingsError) as c:
            self.s.read("aa:01", 1, "security")
        self.assertEqual(c.exception.status, 403)
        n = len(self.dev.hits)
        self.assertTrue(self.s.read("aa:01")["sections"])            # everyday work is untouched
        with self.assertRaises(hiksettings.SettingsError):
            self.s.read("aa:01", 1, "security")
        sent = [h for h in self.dev.hits[n:] if h[2] and "/Security/" in h[1]]
        self.assertEqual(sent, [])                                     # the refused admin login is not sent again


class RecorderAccount(Rig):
    category = "nvr"

    def test_double_verification_and_bound_lists(self):
        self.dev.need_login_pw = True
        self.dev.bond_lists = True
        self.dev.accounts["admin"]["pw"] = "Adm!n@1x"                  # changed by %-encoding: must go raw
        creds.set_("aa:01", "admin", "Adm!n@1x")
        out = self.s.account("aa:01", "create", confirm_text=WORD)
        self.assertTrue(out["ok"], out)
        post = next(b for m, p, b in self.dev.user_calls if m == "POST")
        self.assertIn("<bondIpAddressList>", post)
        self.assertIn("<loginPassword>Adm!n@1x</loginPassword>", post)
        self.assertEqual(creds.get("aa:01")["username"], "netwatch")
        self.assertTrue(self.s.account("aa:01", "remove", confirm_text=WORD)["ok"])
        self.assertEqual(self.dev.deletes, [True])                    # the admin password went with the delete
        self.assertNotIn("netwatch", self.dev.accounts)
        self.assertEqual(creds.get("aa:01")["username"], "admin")


    def test_a_big_rights_list_and_missing_rights(self):
        self.dev.rights_pad = 400_000                                   # a 32-channel recorder: ~430 KB
        self.assertTrue(self.s.account("aa:01", "create", confirm_text=WORD)["ok"])
        self.assertEqual(self.dev.rights["netwatch"]["parameterConfig"], "true")
        self.assertTrue(self.s.account("aa:01", "remove", confirm_text=WORD)["ok"])
        self.dev.hide_new_rights = True
        with self.assertRaises(hiksettings.SettingsError) as c:
            self.s.account("aa:01", "create", confirm_text=WORD)
        self.assertIn("rights", str(c.exception))
        self.assertNotIn("netwatch", self.dev.accounts)
        self.assertEqual(creds.get("aa:01")["username"], "admin")

    def test_an_admin_password_that_cant_travel_raw_is_refused_up_front(self):
        self.dev.accounts["admin"]["pw"] = "a&b#c"
        creds.set_("aa:01", "admin", "a&b#c")
        with self.assertRaises(hiksettings.SettingsError) as c:
            self.s.account("aa:01", "create", confirm_text=WORD)
        self.assertIn("can't", str(c.exception))
        self.assertEqual([x for x in self.dev.user_calls if x[0] != "GET"], [])


class Pieces(unittest.TestCase):
    def test_passwords(self):
        seen = set()
        for _ in range(300):
            p = hiksecurity.new_password(16)
            seen.add(p)
            self.assertEqual(len(p), 16)
            for pool in (string.ascii_uppercase, string.ascii_lowercase, string.digits, hiksecurity._PW_SPECIAL):
                self.assertGreaterEqual(sum(ch in pool for ch in p), 2)
            self.assertFalse(set(p) & set("&<>\"' "))
        self.assertEqual(len(seen), 300)
        self.assertEqual(len(hiksecurity.new_password(40)), 16)

    def test_saving_a_login_keeps_the_admin_slot_and_clearing_drops_it(self):
        creds.set_("zz:01", "netwatch", "a")
        creds.set_admin("zz:01", "admin", "b")
        creds.set_("zz:01", "netwatch", "c", "note")
        self.assertEqual(creds.get_admin("zz:01"), {"username": "admin", "password": "b"})
        self.assertNotIn("b", json.dumps(creds.get("zz:01")))
        creds.set_("zz:01")
        self.assertEqual(creds.get_admin("zz:01"), {"username": "", "password": ""})


if __name__ == "__main__":
    unittest.main()
