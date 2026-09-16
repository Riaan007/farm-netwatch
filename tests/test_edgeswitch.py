"""EdgeSwitch / UISP switch support: the API client (app/edgeswitch.py) against a
fake switch, and the monitor's rules and change events (app/switchmon.py).

Run inside the site image:
  docker run --rm -v "$PWD/app:/app" -v "$PWD/tests:/tests" -e NETWATCH_DATA=/tmp/nw \
    --entrypoint python farm-netwatch:netcfg /tests/test_edgeswitch.py -v
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import edgeswitch      # noqa: E402
import edgeswitch_mock  # noqa: E402
import history         # noqa: E402
import switchmon       # noqa: E402

PI = "02:42:ac:11:00:02"
GW = "70:a7:41:45:cf:aa"


def _snap():
    f = edgeswitch_mock.fixture(pi_mac=PI, gw_mac=GW)
    st = {**f, "errors": 100}
    return edgeswitch.normalize(f["device"], f["system"], f["interfaces"], edgeswitch_mock.statistics(st),
                                f["vlans"], f["mac_table"], f["services"])


class Normalize(unittest.TestCase):
    def test_shape(self):
        s = _snap()
        self.assertTrue(s["ok"])
        self.assertEqual(s["device"]["model"], "ES-8-150W")
        self.assertEqual(s["device"]["firmware"], "v1.9.3")
        self.assertEqual(len(s["ports"]), 10)
        p3 = next(p for p in s["ports"] if p["id"] == "0/3")
        self.assertEqual((p3["speed"], p3["duplex"], p3["poe_mode"]), (100, "full", "active"))
        self.assertAlmostEqual(p3["poe_w"], 6.4)
        self.assertEqual(p3["vlans"], {"untagged": [1], "tagged": [20]})
        self.assertEqual([m["mac"] for m in p3["macs"]], ["44:19:b6:10:20:30"])
        p4 = next(p for p in s["ports"] if p["id"] == "0/4")
        self.assertFalse(p4["up"])
        self.assertIsNone(p4["speed"])
        self.assertEqual(s["poe"]["budget_w"], 150)
        self.assertEqual(s["poe"]["unmeasured"], 0)
        self.assertEqual(s["poe"]["powered"], 3)
        self.assertEqual(s["summary"]["up"], 6)
        self.assertEqual(s["health"]["temp"], 52.5)
        self.assertEqual(s["services"]["snmp_community"], "public")

    def test_ports_sorted_numerically(self):
        self.assertEqual([p["id"] for p in _snap()["ports"]][-2:], ["0/9", "0/10"])

    def test_speed(self):
        self.assertEqual(edgeswitch.speed_mbps("1000-full"), (1000, "full"))
        self.assertEqual(edgeswitch.speed_mbps("10G-full"), (10000, "full"))
        self.assertEqual(edgeswitch.speed_mbps("100-half"), (100, "half"))
        self.assertEqual(edgeswitch.speed_mbps("auto"), (None, None))

    def test_mac_table_variants(self):
        rows = edgeswitch._mac_entries([
            {"macAddress": "AA-BB-CC-DD-EE-FF", "vlanId": 3, "interface": {"id": "0/7"}},
            {"mac": "aabbccddee01", "port": "0/2"},
            {"id": 12, "mac": "aa:bb:cc:dd:ee:02", "interfaceId": "0/8"},
            {"mac": "nope"},
        ])
        self.assertEqual(rows, [("aa:bb:cc:dd:ee:ff", "0/7", 3), ("aa:bb:cc:dd:ee:01", "0/2", None),
                                ("aa:bb:cc:dd:ee:02", "0/8", None)])

    def test_is_edgeswitch(self):
        self.assertTrue(edgeswitch.is_edgeswitch({"vendor": "Ubiquiti Inc", "banner": {"title": "Ubiquiti EdgeSwitch"}}))
        self.assertTrue(edgeswitch.is_edgeswitch({"vendor": "Ubiquiti Networks", "model": "UISP-S-Plus"}))
        self.assertFalse(edgeswitch.is_edgeswitch({"vendor": "Ubiquiti Inc", "banner": {"title": "Ubiquiti"}}))
        self.assertFalse(edgeswitch.is_edgeswitch({"vendor": "MikroTik", "banner": {"title": "EdgeSwitch"}}))
        self.assertTrue(edgeswitch.is_edgeswitch({"vendor": "", "banner": {"title": "Ubiquiti EdgeSwitch"}}))

    def test_protected_and_uplinks(self):
        s = _snap()
        prot = switchmon.protected_ports(s, own_macs={PI}, gw_mac=GW)
        self.assertEqual(set(prot), {"0/1"})
        self.assertIn("Netwatch Pi", prot["0/1"])
        self.assertIn("router", prot["0/1"])
        self.assertEqual(switchmon.uplink_ports(s, min_macs=2), {"0/1": 2, "0/2": 2})


class AgainstMock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        edgeswitch_mock.Handler.state = {**edgeswitch_mock.fixture(pi_mac=PI, gw_mac=GW), "errors": 100}
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), edgeswitch_mock.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        # The client dials https://ip then http://ip on the default ports; point both at the mock.
        orig = edgeswitch.Session._bases
        cls._orig = orig
        edgeswitch.Session._bases = lambda self: [f"http://127.0.0.1:{cls.port}"]

    @classmethod
    def tearDownClass(cls):
        edgeswitch.Session._bases = cls._orig
        cls.srv.shutdown()

    def test_read(self):
        s = edgeswitch.read("127.0.0.1", "ubnt", "ubnt")
        self.assertTrue(s["ok"], s)
        self.assertEqual(s["summary"]["ports"], 10)

    def test_wrong_login(self):
        s = edgeswitch.read("127.0.0.1", "ubnt", "wrong")
        self.assertFalse(s["ok"])
        self.assertEqual(s["kind"], "auth_failed")

    def test_post_without_origin_is_not_a_wrong_password(self):
        import requests
        r = requests.post(f"http://127.0.0.1:{self.port}/api/v1.0/user/login",
                          json={"username": "ubnt", "password": "ubnt"}, timeout=5)
        self.assertEqual(r.status_code, 403)          # what the real switch does
        s = edgeswitch.Session("127.0.0.1", "ubnt", "ubnt")
        s.login()                                     # the client sends Origin/Referer
        self.assertTrue(s.http.headers.get("x-auth-token"))
        s.logout()

    def test_set_port_keeps_other_fields(self):
        r = edgeswitch.set_port("127.0.0.1", "ubnt", "ubnt", "0/7", enabled=False, name="Spare")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["after"], {"enabled": False, "poe": "off", "name": "Spare"})
        cur = next(i for i in edgeswitch_mock.Handler.state["interfaces"] if i["identification"]["id"] == "0/7")
        self.assertEqual(cur["port"]["stp"]["portPriority"], 128)      # untouched
        edgeswitch.set_port("127.0.0.1", "ubnt", "ubnt", "0/7", enabled=True)

    def test_poe_cycle_restores_mode(self):
        t0 = time.time()
        r = edgeswitch.poe_cycle("127.0.0.1", "ubnt", "ubnt", "0/6", off_s=3)
        self.assertTrue(r["ok"], r)
        self.assertGreaterEqual(time.time() - t0, 3)
        cur = next(i for i in edgeswitch_mock.Handler.state["interfaces"] if i["identification"]["id"] == "0/6")
        self.assertEqual(cur["port"]["poe"], "24v")

    def test_poe_cycle_needs_poe_on(self):
        self.assertFalse(edgeswitch.poe_cycle("127.0.0.1", "ubnt", "ubnt", "0/8", off_s=3)["ok"])

    def test_backup(self):
        r = edgeswitch.backup("127.0.0.1", "ubnt", "ubnt")
        self.assertTrue(r["ok"])
        self.assertTrue(r["content"].startswith(b"\x1f\x8b"))


class Rules(unittest.TestCase):
    KEY = "d8:b3:70:73:bc:e5"

    def setUp(self):
        c = history._conn()
        c.execute("DELETE FROM switch_ports")
        c.execute("DELETE FROM switch_samples")
        c.commit()
        self.mon = switchmon.SwitchMonitor()
        self.devs = {self.KEY: {"key": self.KEY, "ip": "192.168.88.1", "name": "Tower Switch", "online": True},
                     "44:19:b6:10:20:30": {"key": "44:19:b6:10:20:30", "mac": "44:19:b6:10:20:30", "name": "Gate camera",
                                           "category": "camera", "online": True}}
        self.mon._get_devices = lambda: list(self.devs.values())

    def _history(self, port, up=1, speed=1000, poe_w=5.0, n=20):
        now = int(time.time())
        for i in range(n):
            history.switch_record(self.KEY, {}, [{"port": port, "up": up, "speed": speed, "poe_w": poe_w}],
                                  ts=now - 7200 - i * 300)

    def test_port_down_that_is_normally_up_is_critical_for_a_camera(self):
        self._history("0/3", up=1, speed=100, poe_w=6.4)
        prev = _snap()
        prev["port_memory"] = {"0/3": {"macs": ["44:19:b6:10:20:30"], "ts": int(time.time())}}
        self.mon._last[self.KEY] = {"ok": True, "snap": prev}
        cur = _snap()
        p3 = next(p for p in cur["ports"] if p["id"] == "0/3")
        p3.update(up=False, speed=None, poe_w=0.0, macs=[])
        cur["port_memory"] = prev["port_memory"]
        self.mon._last[self.KEY] = {"ok": True, "snap": cur}
        out = self.mon._evaluate(self.KEY, self.devs[self.KEY], prev, cur)
        down = [p for p in out if p["metric"] == "port_down"]
        self.assertEqual(len(down), 1)
        self.assertEqual((down[0]["port"], down[0]["level"]), ("0/3", "crit"))
        self.assertTrue(any(p["metric"] == "poe_lost" for p in out))

    def test_speed_drop_against_own_history(self):
        self._history("0/3", speed=1000)
        out = self.mon._evaluate(self.KEY, self.devs[self.KEY], None, _snap())   # now at 100
        self.assertTrue(any(p["metric"] == "speed_drop" and p["port"] == "0/3" for p in out))

    def test_no_speed_rule_without_history(self):
        out = self.mon._evaluate(self.KEY, self.devs[self.KEY], None, _snap())
        self.assertFalse(any(p["metric"] in ("speed_drop", "port_down") for p in out))

    def test_error_rate(self):
        prev, cur = _snap(), _snap()
        cur["ts"] = prev["ts"] + 300
        next(p for p in prev["ports"] if p["id"] == "0/3")["errors"] = 100
        next(p for p in cur["ports"] if p["id"] == "0/3")["errors"] = 400
        out = self.mon._evaluate(self.KEY, self.devs[self.KEY], prev, cur)
        e = [p for p in out if p["metric"] == "port_errors"]
        self.assertEqual(len(e), 1)
        self.assertEqual(e[0]["level"], "crit")      # 300 in 5 min = 3600/h

    def test_snmp_default_is_a_note(self):
        out = self.mon._evaluate(self.KEY, self.devs[self.KEY], None, _snap())
        self.assertTrue(any(p["metric"] == "snmp_default" and p["level"] == "info" for p in out))

    def test_change_events(self):
        prev, cur = _snap(), _snap()
        p3 = next(p for p in cur["ports"] if p["id"] == "0/3")
        p3.update(up=False, speed=None, macs=[])
        p5 = next(p for p in cur["ports"] if p["id"] == "0/5")
        p5["poe_mode"] = "active"
        p6 = next(p for p in cur["ports"] if p["id"] == "0/6")
        p6["macs"] = [{"mac": "44:19:b6:10:20:30", "vlan": 1}]     # the camera is now on port 6
        cur["device"]["uptime"] = 30
        rows = self.mon._changes(self.KEY, self.devs[self.KEY], prev, cur)
        import json
        kinds = sorted(json.loads(r[-1])["switch_event"] for r in rows)
        self.assertEqual(kinds, ["device_moved", "link_down", "poe_mode", "rebooted"])
        down = next(json.loads(r[-1]) for r in rows if json.loads(r[-1])["switch_event"] == "link_down")
        self.assertEqual(down["devices"], ["Gate camera"])


if __name__ == "__main__":
    unittest.main()
