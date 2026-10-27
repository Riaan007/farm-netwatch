"""Site survey from a station (app/wifiscan.py): parsing iwlist, refusing an AP,
running the uploaded scan script for real against stubbed tools.

Run inside the site image:
  docker run --rm -v "$PWD/app:/app" -v "$PWD/tests:/tests" -e NETWATCH_DATA=/tmp/nw \
    --entrypoint python farm-netwatch:netcfg /tests/test_wifiscan.py -v
"""
import os
import subprocess
import sys
import tempfile
import time
import unittest

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import airos      # noqa: E402
import wifiscan   # noqa: E402

LOGIN = {"username": "ubnt", "password": "pw"}
STUBS = tempfile.mkdtemp()

SCAN = """ath0      Scan completed :
          Cell 01 - Address: 18:E8:29:AA:BB:01
                    ESSID:"Neighbour-PTP"
                    Mode:Master
                    Frequency:5.745 GHz (Channel 149)
                    Quality=40/94  Signal level=-58 dBm  Noise level=-95 dBm
                    Extra:ieee_mode=11ACVHT80
          Cell 02 - Address: 24:A4:3C:00:00:02
                    ESSID:"Farmhouse"
                    Frequency:5.18 GHz (Channel 36)
                    Quality=10/94  Signal level=-84 dBm
          Cell 03 - Address: 24:A4:3C:00:00:03
                    ESSID:"Kitchen 2.4"
                    Frequency:2.437 GHz (Channel 6)
                    Signal level=-50 dBm
          Cell 04 - Address: 24:A4:3C:00:00:04
                    ESSID:"Wide40"
                    Frequency:5.5 GHz (Channel 100)
                    Signal level=-70 dBm
                    Extra:ieee_mode=11NAHT40PLUS
"""


def cfg(mode="sta-ptp", scan=""):
    lines = ["netconf.1.ip=192.168.1.30", f"wireless.1.mode={mode}", "radio.1.freq=5480"]
    if scan:
        lines += ["radio.1.scan_list.status=enabled", f"radio.1.scan_list.channels={scan}"]
    return "\n".join(lines) + "\n"


class FakeStation:
    """Runs the uploaded scan script for real; iwlist / wstalist are stubs."""

    def __init__(self, mode="sta-ptp-ac", scan_list=""):
        self.mode, self.scan_list, self.commands = mode, scan_list, []

    def __call__(self, ip, user, password, command, timeout=20, stdin_data=None):
        self.commands.append(command)
        if command.startswith("mca-status"):
            return 0, f"wlanOpmode={self.mode}\n{airos._CFG_MARK}\n{cfg(self.mode.rsplit('-ac', 1)[0], self.scan_list)}", ""
        if command.startswith("cat > /tmp/nwscan.sh"):
            with open("/tmp/nwscan.sh", "w") as f:
                f.write(stdin_data)
            subprocess.run(["/bin/sh", "/tmp/nwscan.sh"], env={**os.environ, "PATH": f"{STUBS}:/usr/bin:/bin"},
                           timeout=30, check=False)
            return 0, "STARTED\n", ""
        if command.startswith("cat /tmp/nwscan.state"):
            with open("/tmp/nwscan.state") as f:
                return 0, f.read(), ""
        if command.startswith("cat /tmp/nwscan.out"):
            with open("/tmp/nwscan.out") as f:
                return 0, f.read(), ""
        if command.startswith("wstalist"):
            return 0, '[{"remote":{}}]', ""
        return 0, "", ""


def wait(key):
    for _ in range(100):
        if wifiscan.state(key).get("phase") == "done":
            return wifiscan.state(key)
        time.sleep(0.1)
    raise AssertionError("scan job never finished")


class Parse(unittest.TestCase):
    def test_5ghz_networks_with_their_width(self):
        nets = {n["ssid"]: n for n in wifiscan.parse(SCAN)}
        self.assertNotIn("Kitchen 2.4", nets)                       # 2.4 GHz is not our band
        n = nets["Neighbour-PTP"]
        self.assertEqual((n["freq"], n["width"], n["lo"], n["hi"], n["signal"]), (5745, 80, 5735, 5815, -58))
        f = nets["Farmhouse"]
        self.assertEqual((f["width"], f["width_known"], f["lo"], f["hi"]), (20, False, 5170, 5190))
        self.assertEqual((nets["Wide40"]["lo"], nets["Wide40"]["hi"]), (5490, 5530))   # HT40 PLUS: above the primary


class Run(unittest.TestCase):
    def setUp(self):
        self._ssh, self._wait = airos._ssh, wifiscan.POLL_S
        wifiscan.POLL_S = 0
        w = lambda p, body: (open(p, "w").write("#!/bin/sh\n" + body + "\n"), os.chmod(p, 0o755))  # noqa: E731
        scan_file = os.path.join(STUBS, "scan.txt")
        with open(scan_file, "w") as f:
            f.write(SCAN)
        w(os.path.join(STUBS, "iwlist"), f"cat {scan_file}")
        w(os.path.join(STUBS, "bgnd"), 'shift 3; "$@"')

    def tearDown(self):
        airos._ssh, wifiscan.POLL_S = self._ssh, self._wait

    def test_a_station_scans_and_the_result_is_kept(self):
        airos._ssh = FakeStation(scan_list="5480,5500")
        t = {"key": "aa:00:00:00:00:02", "ip": "192.168.1.30", "name": "Sta"}
        self.assertTrue(wifiscan.start(t, LOGIN))
        j = wait(t["key"])
        self.assertTrue(j["ok"], j)
        self.assertEqual(j["found"], 3)
        self.assertIn("Frequency Scan List", j["msg"])
        self.assertEqual(len(wifiscan.load()[t["key"]]["networks"]), 3)
        self.assertTrue(any("bgnd -r nwscan" in c for c in airos._ssh.commands))

    def test_an_access_point_is_never_scanned(self):
        airos._ssh = FakeStation(mode="ap-ptmp-ac")
        t = {"key": "aa:00:00:00:00:01", "ip": "192.168.1.20", "name": "AP"}
        wifiscan.start(t, LOGIN)
        j = wait(t["key"])
        self.assertFalse(j["ok"])
        self.assertIn("access point", j["msg"])
        self.assertFalse(any("nwscan.sh" in c for c in airos._ssh.commands))     # nothing uploaded


if __name__ == "__main__":
    unittest.main()
