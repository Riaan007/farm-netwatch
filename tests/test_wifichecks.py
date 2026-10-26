"""On-the-spot Wi-Fi checks (app/wifichecks.py) against a simulated airOS radio,
plus the `check` the diagnosis attaches to its "From the office" steps.

Run inside the site image:
  docker run --rm -v "$PWD/app:/app" -v "$PWD/tests:/tests" -e NETWATCH_DATA=/tmp/nw \
    --entrypoint python farm-netwatch:netcfg /tests/test_wifichecks.py -v
"""
import os
import sys
import tempfile
import unittest

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import airos        # noqa: E402
import wifichecks   # noqa: E402
import wifidiag     # noqa: E402

LOGIN = {"username": "ubnt", "password": "pw"}


def radio(txpower=24, uptime=500000.0, mixed=False, scan="", log=None, mode="ap-ptmp-ac"):
    """What one SSH session to a fake airOS radio prints."""
    cfg = ["netconf.1.ip=192.168.1.20", f"radio.1.txpower={txpower}", "radio.1.antenna.gain=23",
           "radio.1.cable.loss=0", "radio.1.obey=enabled", "radio.1.chanbw=40", "radio.1.countrycode=710",
           f"wireless.1.mode={mode}"]
    if mixed:
        cfg.append("radio.1.polling_11ac_11n_compat=1")
    if scan:
        cfg += ["radio.1.scan_list.status=enabled", f"radio.1.scan_list.channels={scan}"]
    status = [f"deviceName=X", f"wlanOpmode={mode}", "freq=5480", f"wlanTxPower={txpower}",
              "firmwareVersion=XC.ar934x.v8.7.11"]
    log = log if log is not None else ["Oct 26 05:11:02 wpa: ath0: associated", "Oct 26 05:12:00 sshd: login",
                                       "Oct 26 05:13:44 kernel: ath0 radar detected on 5500"]
    return ("\n".join(status) + "\n===NW-CFG===\n" + "\n".join(cfg) + "\n===NW-UP===\n"
            + f"{uptime} 1.0\n===NW-LOG===\n" + "\n".join(log) + "\n")


class FakeSSH:
    def __init__(self, by_ip):
        self.by_ip, self.calls = by_ip, []

    def __call__(self, ip, user, password, command, timeout=20, stdin_data=None):
        self.calls.append((ip, command))
        v = self.by_ip.get(ip)
        if isinstance(v, tuple):
            return v
        return (0, v, "") if v is not None else (255, "", "ssh: connect to host port 22: No route to host")


class Checks(unittest.TestCase):
    def setUp(self):
        self._ssh = airos._ssh

    def tearDown(self):
        airos._ssh = self._ssh

    def run_checks(self, kinds, radios, logins=None):
        devs = [{"key": f"k{i}", "ip": ip, "name": f"R{i}"} for i, ip in enumerate(radios)]
        return wifichecks.run(kinds, devs, lambda k: (logins or {}).get(k, LOGIN))

    def facts(self, r):
        return {f["label"]: f for f in r["facts"]}

    def test_power_both_ends_and_difference(self):
        airos._ssh = FakeSSH({"1.1.1.1": radio(txpower=24), "1.1.1.2": radio(txpower=14)})
        out = self.run_checks(["power"], ["1.1.1.1", "1.1.1.2"])
        a, b = out["results"]
        self.assertTrue(a["ok"] and b["ok"])
        self.assertEqual(self.facts(a)["Transmit power now"]["value"], "24 dBm")
        self.assertIn("47 dBm EIRP", self.facts(a)["Antenna gain / cable loss"]["value"])
        self.assertEqual(out["summary"][0]["level"], "warn")
        self.assertIn("10 dB", out["summary"][0]["value"])

    def test_uptime_flags_a_recent_restart(self):
        airos._ssh = FakeSSH({"1.1.1.1": radio(uptime=1800.5)})
        f = self.facts(self.run_checks(["uptime"], ["1.1.1.1"])["results"][0])["Running for"]
        self.assertEqual(f["level"], "bad")
        self.assertIn("restarted recently", f["value"])
        airos._ssh = FakeSSH({"1.1.1.1": radio(uptime=3 * 86400 + 7200)})
        f = self.facts(self.run_checks(["uptime"], ["1.1.1.1"])["results"][0])["Running for"]
        self.assertEqual((f["value"], f["level"]), ("3 d 2 h", "ok"))

    def test_log_keeps_only_radio_lines(self):
        airos._ssh = FakeSSH({"1.1.1.1": radio()})
        v = self.run_checks(["log"], ["1.1.1.1"])["results"][0]["facts"][0]["value"]
        self.assertIn("radar detected", v)
        self.assertIn("associated", v)
        self.assertNotIn("sshd", v)

    def test_settings_shows_mixed_mode_and_scan_list(self):
        airos._ssh = FakeSSH({"1.1.1.1": radio(mixed=True, scan="5480,5500")})
        f = self.facts(self.run_checks(["settings"], ["1.1.1.1"])["results"][0])
        self.assertEqual(f["airMAX mode"]["level"], "warn")
        self.assertEqual(f["Frequency"]["value"], "5480 MHz")
        self.assertEqual(f["Channel width"]["value"], "40 MHz")
        self.assertEqual(f["Frequency scan list"]["value"], "5480, 5500 MHz")
        self.assertIn("v8.7.11", f["Firmware"]["value"])

    def test_read_only(self):
        fake = FakeSSH({"1.1.1.1": radio()})
        airos._ssh = fake
        self.run_checks(list(wifichecks.KINDS), ["1.1.1.1"])
        self.assertEqual(len(fake.calls), 1)                 # one session per radio
        cmd = fake.calls[0][1].replace("2>/dev/null", "")
        for word in ("cfgmtd", "softrestart", "reboot", ">", "sed", "rm "):
            self.assertNotIn(word, cmd)

    def test_failures_are_explained(self):
        airos._ssh = FakeSSH({"1.1.1.2": (5, "", "Permission denied, please try again.")})
        out = self.run_checks(["power"], ["1.1.1.1", "1.1.1.2", ""],
                              logins={"k0": LOGIN, "k1": LOGIN, "k2": LOGIN})
        r0, r1, r2 = out["results"]
        self.assertIn("did not answer", r0["error"])
        self.assertIn("login was rejected", r1["error"])
        self.assertIn("no IP", r2["error"])
        out = self.run_checks(["power"], ["1.1.1.1"], logins={"k0": None})
        self.assertIn("no SSH login saved", out["results"][0]["error"])

    def test_bad_kinds_rejected(self):
        with self.assertRaises(ValueError):
            self.run_checks(["reboot"], ["1.1.1.1"])


class DiagnosisCarriesChecks(unittest.TestCase):
    def test_chk_skips_unreadable_ends(self):
        a = {"key": "aa", "monitored": True}
        b = {"key": "bb", "monitored": False}
        self.assertEqual(wifidiag._chk(["power"], a, b), {"kinds": ["power"], "radios": ["aa"]})
        self.assertIsNone(wifidiag._chk(["power"], b))

    def test_step_shape(self):
        self.assertNotIn("check", wifidiag._step("x", "site"))
        st = wifidiag._step("x", "remote", {"kinds": ["uptime"], "radios": ["aa"]})
        self.assertEqual(st["check"]["kinds"], ["uptime"])


if __name__ == "__main__":
    unittest.main()
