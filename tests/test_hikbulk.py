"""The FarmCamSA standard and bulk jobs (Phase 5).

Two fake cameras serving real (cleaned) setting documents: China time, the clock set by
hand, US date order. A check must write nothing; a rule job must change only what the
rule says, one device at a time, each change read back and in History; undoing the job
must put every device back exactly.

Run inside the site image (nothing leaves the box):
  docker run --rm --network none -v "$PWD/app:/app:ro" -v "$PWD/tests:/tests:ro" -e NETWATCH_DATA=/tmp/nw \\
    -e PYTHONDONTWRITEBYTECODE=1 --entrypoint python farm-netwatch:netcfg /tests/test_hikbulk.py -v
"""
import os
import sys
import tempfile
import unittest

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import campics      # noqa: E402
import creds        # noqa: E402
import hikbulk      # noqa: E402
import hikisapi     # noqa: E402
import hiksecurity  # noqa: E402
import hiksettings  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hik_fake import Device  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
HIK = "Hangzhou Hikvision Digital Technology"
TIME = "/ISAPI/System/time"
OVL = "/ISAPI/System/Video/inputs/channels/1/overlays"
SSH = "/ISAPI/System/Network/ssh"


def cam():
    d = Device().start().load_settings(os.path.join(HERE, "fixtures", "hik_settings"))
    return d.load_settings(os.path.join(HERE, "fixtures", "hik_security"))


class Rig(unittest.TestCase):
    def setUp(self):
        hikisapi._seen.clear()
        for k in list(creds.keys_with_creds()):
            creds.set_(k)
        self.a, self.b, self.c = cam(), cam(), cam()
        self.devs = [
            {"key": "aa:01", "ip": self.a.addr, "category": "camera", "vendor": HIK, "online": True,
             "ports": [80, 8000], "name": "Gate cam", "model": "DS-2CD2046G2-IU"},
            {"key": "aa:02", "ip": self.b.addr, "category": "camera", "vendor": HIK, "online": True,
             "ports": [80, 8000], "name": "Dam cam", "model": "DS-2CD2046G2-IU"},
            {"key": "aa:03", "ip": self.c.addr, "category": "camera", "vendor": HIK, "online": True,
             "ports": [80, 8000], "name": "Shed cam", "model": "DS-2CD1043G0-I"},
        ]
        for k in ("aa:01", "aa:02", "aa:03"):
            creds.set_(k, "admin", "pw")
        self.cfg = {"features": {"camera_pictures": True, "camera_manage": True, "camera_risky": False},
                    "camera": {}, "alerts": {}}
        self.e = campics.Engine()
        self.e.start(lambda: [dict(d) for d in self.devs], lambda: {}, lambda: {}, cfg=lambda: self.cfg, thread=False)
        self.hist = []
        tmp = tempfile.mkdtemp()
        self.s = hiksettings.Settings().start(self.e, lambda: self.cfg, on_change=lambda d, e: self.hist.append(e),
                                              log_path=os.path.join(tmp, "log.json"))
        self.j = hikbulk.Jobs(self.s).start(self.e, lambda: self.cfg, jobs_path=os.path.join(tmp, "jobs.json"),
                                            check_path=os.path.join(tmp, "check.json"))
        self.j.sync = True
        hiksettings.PUT_RETRY_S = 0
        self.slept = []
        self.j.sleep = self.slept.append

    def tearDown(self):
        for d in (self.a, self.b, self.c):
            d.stop()

    def job(self, r):
        return self.j.get(r["job"]["id"])


class TestCheck(Rig):
    def test_check_reads_only_and_judges(self):
        r = self.j.start_check(who="t")
        self.assertEqual(self.job(r)["state"], "done")
        for d in (self.a, self.b, self.c):
            self.assertEqual(d.puts, [], "a check must write nothing")
        ov = self.j.overview()
        rows = {x["key"]: x for x in ov["check"]["devices"]}
        g = rows["aa:01"]["rules"]
        self.assertEqual(g["tz"]["state"], "off")
        self.assertEqual(g["tz"]["diffs"], [{"id": "time.timeZone", "now": "CST-8:00:00", "want": hikbulk.SA_TZ}])
        self.assertEqual(g["ntp"]["state"], "off")
        self.assertEqual(g["osd_time"]["state"], "off")          # MM-DD-YYYY
        self.assertEqual(g["flicker"]["state"], "ok")            # 50hz
        self.assertEqual(g["sub_h264"]["state"], "off")          # H.265
        self.assertEqual(g["main_h265"]["state"], "ok")
        self.assertEqual(g["upnp_off"]["state"], "ok")
        self.assertEqual(g["lock_on"]["state"], "ok")
        self.assertEqual(g["osd_name"]["state"], "ok")           # the fixture's name is "Gate cam"
        self.assertEqual(rows["aa:02"]["rules"]["osd_name"]["state"], "off")
        self.assertEqual(self.slept, [], "a check doesn't pause between devices")

    def test_offline_device_is_a_failed_row_not_a_dead_job(self):
        self.b.stop()
        r = self.j.start_check()
        jb = self.job(r)
        self.assertEqual(jb["state"], "done")
        st = {it["key"]: it["status"] for it in jb["items"]}
        self.assertEqual(st["aa:01"], "ok")
        self.assertEqual(st["aa:02"], "failed")

    def test_switch_off_refuses(self):
        self.cfg["features"]["camera_manage"] = False
        with self.assertRaises(hikbulk.BulkError) as cm:
            self.j.start_check()
        self.assertEqual(cm.exception.status, 403)


class TestRuleJob(Rig):
    def test_time_zone_job_and_undo(self):
        before = {k: d.settings[TIME] for k, d in (("a", self.a), ("b", self.b))}
        r = self.j.start_rule("tz", ["aa:01", "aa:02"], who="t", pause_s=2)
        jb = self.job(r)
        self.assertEqual(jb["state"], "done")
        self.assertEqual([it["status"] for it in jb["items"]], ["ok", "ok"])
        self.assertEqual(self.slept, [2], "one pause between two devices")
        for d in (self.a, self.b):
            self.assertIn("<timeZone>CST-2:00:00</timeZone>", d.settings[TIME])
            self.assertIn("<timeMode>manual</timeMode>", d.settings[TIME], "the tz rule leaves the clock mode alone")
            self.assertEqual([p for p, _b in d.puts], [TIME])
        self.assertEqual(len(self.hist), 2)
        self.assertEqual(self.c.puts, [])
        u = self.j.start_undo(jb["id"], who="t")
        ub = self.job(u)
        self.assertEqual(ub["state"], "done")
        self.assertEqual([it["key"] for it in ub["items"]], ["aa:02", "aa:01"], "newest first")
        for k, d in (("a", self.a), ("b", self.b)):
            self.assertIn("<timeZone>CST-8:00:00</timeZone>", d.settings[TIME])
        self.assertEqual(self.j.get(jb["id"])["undone_by"], ub["id"])
        with self.assertRaises(hikbulk.BulkError):
            self.j.start_undo(jb["id"])

    def test_already_meets_is_unchanged_and_not_undone(self):
        r = self.j.start_rule("flicker", ["aa:01"])
        jb = self.job(r)
        self.assertEqual(jb["items"][0]["status"], "unchanged")
        self.assertEqual(self.a.puts, [])
        with self.assertRaises(hikbulk.BulkError):
            self.j.start_undo(jb["id"])

    def test_stop_on_first_failure(self):
        self.a.put_status[TIME] = (200, 4)
        r = self.j.start_rule("tz", ["aa:01", "aa:02"], stop_on_fail=True)
        jb = self.job(r)
        self.assertEqual(jb["state"], "stopped")
        self.assertEqual([it["status"] for it in jb["items"]], ["failed", "skipped"])
        self.assertEqual(self.b.puts, [])

    def test_a_failure_does_not_stop_the_rest_by_default(self):
        self.a.put_status[TIME] = (200, 4)
        jb = self.job(self.j.start_rule("tz", ["aa:01", "aa:02"]))
        self.assertEqual(jb["state"], "done")
        self.assertEqual([it["status"] for it in jb["items"]], ["failed", "ok"])

    def test_a_refused_write_is_tried_once_more(self):
        self.a.put_fail_once[TIME] = (400, 6)
        jb = self.job(self.j.start_rule("tz", ["aa:01"]))
        self.assertEqual(jb["items"][0]["status"], "ok", jb["items"][0]["message"])
        self.a.put_status[TIME] = (400, 6)
        jb = self.job(self.j.start_rule("tz", ["aa:02", "aa:01"]))
        self.assertEqual([i["status"] for i in jb["items"]], ["ok", "unchanged"])
        self.b.put_status[TIME] = (400, 6)
        jb2 = self.job(self.j.start_rule("ntp", ["aa:02"]))
        self.assertEqual(jb2["items"][0]["status"], "partial", "the time-server document went, the clock didn't")

    def test_a_partly_failed_undo_says_so(self):
        jb = self.job(self.j.start_rule("tz", ["aa:01", "aa:02"]))
        self.b.put_status[TIME] = (400, 6)
        self.j.start_undo(jb["id"])
        self.assertIn("Dam cam", self.j.get(jb["id"])["note"])

    def test_osd_time_rule(self):
        jb = self.job(self.j.start_rule("osd_time", ["aa:01"]))
        self.assertEqual(jb["items"][0]["status"], "ok", jb["items"][0]["message"])
        self.assertIn("<dateStyle>DD-MM-YYYY</dateStyle>", self.a.settings[OVL])

    def test_name_rule_uses_each_cameras_netwatch_name(self):
        jb = self.job(self.j.start_rule("osd_name", ["aa:02"]))
        self.assertEqual(jb["items"][0]["status"], "ok", jb["items"][0]["message"])
        self.assertIn("<name>Dam cam</name>", self.b.settings["/ISAPI/System/Video/inputs/channels/1"])

    def test_risky_rule_needs_switch_and_word(self):
        self.a.settings[SSH] = self.a.settings[SSH].replace("<enabled>false</enabled>", "<enabled>true</enabled>", 1)
        with self.assertRaises(hikbulk.BulkError) as cm:
            self.j.start_rule("ssh_off", ["aa:01"], confirm_text=hiksecurity.CONFIRM_WORD)
        self.assertEqual(cm.exception.status, 403)
        self.cfg["features"]["camera_risky"] = True
        with self.assertRaises(hikbulk.BulkError) as cm:
            self.j.start_rule("ssh_off", ["aa:01"])
        self.assertIn("needs_word", cm.exception.extra)
        jb = self.job(self.j.start_rule("ssh_off", ["aa:01"], confirm_text=hiksecurity.CONFIRM_WORD))
        self.assertEqual(jb["items"][0]["status"], "ok", jb["items"][0]["message"])
        self.assertIn("<enabled>false</enabled>", self.a.settings[SSH])

    def test_one_job_at_a_time(self):
        self.j._run = {"id": "x"}
        with self.assertRaises(hikbulk.BulkError) as cm:
            self.j.start_rule("tz", ["aa:01"])
        self.assertEqual(cm.exception.status, 409)

    def test_unknown_key_refused(self):
        with self.assertRaises(hikbulk.BulkError):
            self.j.start_rule("tz", ["zz:99"])


class TestCopy(Rig):
    def test_copy_to_same_model_only(self):
        with self.assertRaises(hikbulk.BulkError):
            self.j.start_copy("aa:01", 1, ["picture"], ["aa:03"])

    def test_copy_osd_never_copies_the_name(self):
        self.a.settings[OVL] = self.a.settings[OVL].replace("MM-DD-YYYY", "DD-MM-YYYY")
        jb = self.job(self.j.start_copy("aa:01", 1, ["osd"], ["aa:02"]))
        self.assertEqual(jb["state"], "done")
        self.assertEqual(jb["items"][0]["status"], "ok", jb["items"][0]["message"])
        self.assertIn("DD-MM-YYYY", self.b.settings[OVL])
        self.assertEqual([p for p, _b in self.b.puts], [OVL])

    def test_copy_same_values_changes_nothing(self):
        jb = self.job(self.j.start_copy("aa:01", 1, ["picture", "main"], ["aa:02"]))
        self.assertEqual([it["status"] for it in jb["items"]], ["unchanged", "unchanged"])
        self.assertEqual(self.b.puts, [])


if __name__ == "__main__":
    unittest.main()
