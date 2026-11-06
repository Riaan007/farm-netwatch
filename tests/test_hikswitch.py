"""Hikvision smart switch support (app/hikswitch.py): the login hash, detection
(a DS-3E with a web server is managed, the SDK-only ones are not, the bridge's
vendor is ignored), the snapshot shape the monitor and UIs share with the
EdgeSwitch/SwOS drivers, and logins + writes against a fake switch that speaks
the switch's JSON API (tests/hikswitch_fake.py), lock-out included.

Run inside the site image:
  docker run --rm --network none -v "$PWD/app:/app" -v "$PWD/tests:/tests" -e NETWATCH_DATA=/tmp/nw \
    --entrypoint python farm-netwatch:netcfg /tests/test_hikswitch.py -v
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import credtest      # noqa: E402
import hikswitch     # noqa: E402
import restart       # noqa: E402
import switchmon     # noqa: E402
from hikswitch_fake import FakeHikSwitch  # noqa: E402

# Three switches the way Netwatch records them: a plain one, one behind an ARP-NAT radio
# bridge (its `mac` is the bridge's; the real one is in sadp) and an SDK-only DS-3E1309P-EI.
# MACs and serials are made up.
DEV_243 = {"key": "84:94:59:00:02:43", "ip": "192.168.1.243", "mac": "84:94:59:00:02:43", "online": True,
           "category": "network", "model": "DS-3E1520P-EI", "vendor": "Hangzhou Hikvision Digital Technology Co.,Ltd.",
           "ports": [80], "services": {"7681": "tcpwrapped", "80": "http"},
           "sadp": {"model": "DS-3E1520P-EI", "http_port": 80, "mac": "84:94:59:00:02:43"}}
DEV_242 = {"key": "192.168.1.242", "ip": "192.168.1.242", "mac": "e0:63:da:00:00:10", "online": True,
           "category": "network", "model": "DS-3E1520P-EI", "vendor": "Ubiquiti Networks",   # the radio bridge's
           "ports": [80], "services": {"80": "http"},
           "sadp": {"model": "DS-3E1520P-EI", "http_port": 80, "mac": "84:94:59:00:02:42"}}
DEV_241 = {"key": "2c:a5:9c:00:02:41", "ip": "192.168.1.241", "mac": "2c:a5:9c:00:02:41", "online": True,
           "category": "network", "model": "DS-3E1309P-EI", "vendor": "Hangzhou Hikvision Digital Technology",
           "ports": [8000], "services": {"8000": "http-alt"},
           "sadp": {"model": "DS-3E1309P-EI", "http_port": None, "mac": "2c:a5:9c:00:02:41"}}
CAMERA = {"key": "04:ee:cd:00:00:65", "ip": "192.168.1.65", "mac": "04:ee:cd:00:00:65", "ports": [80, 554, 8000],
          "model": "DS-2CD2T87G2P-LSU/SL", "vendor": "Hikvision", "category": "camera",
          "sadp": {"model": "DS-2CD2T87G2P-LSU/SL", "http_port": 80}}


class LoginHashTest(unittest.TestCase):
    # Reference values from the switch's OWN web app: its SHA-256 function (Z2,
    # cut out of index.1b13765a.js) run in Node, then PBKDF2-SHA256 64 bytes.
    def test_matches_the_switch_web_app(self):
        self.assertEqual(
            hikswitch.login_hash("admin", "Secret#123", "9f8e7d6c", "abcdEFGH12", "100"),
            "e62e57e6889b67c83d06b9d928cb825eb9c5f7097a43a6661887b95383a7ca97"
            "2c84aed1ea4f087652d3cd480c540db4593d27326a9de4cfb0e79f5cd60db9f7")

    def test_non_ascii_password(self):
        self.assertEqual(
            hikswitch.login_hash("admin", "pässwörd-ÿ", "challengeXYZ", "s4lt", 2),
            "2bb89dee9bed59b1c5d61974347a5d415e58270dcdff6f79c20298a69e0d497a"
            "1f57a831d84b24bf01f7fe21b3bb0fed096e7d799be1371809c025ac52dbc68c")

    def test_bad_iterations_fall_back_to_one(self):
        self.assertEqual(len(hikswitch.login_hash("a", "b", "c", "d", None)), 128)


class DetectTest(unittest.TestCase):
    def test_web_switches_are_managed(self):
        self.assertTrue(hikswitch.is_hikswitch(DEV_243))
        self.assertIs(switchmon.driver(DEV_243), hikswitch)

    def test_behind_the_bridge_the_vendor_is_ignored(self):
        self.assertTrue(hikswitch.is_hikswitch(DEV_242))
        self.assertIs(switchmon.driver(DEV_242), hikswitch)     # not taken for a Ubiquiti EdgeSwitch

    def test_sdk_only_switch_is_not(self):
        self.assertFalse(hikswitch.is_hikswitch(DEV_241))
        self.assertIsNone(switchmon.driver(DEV_241))
        self.assertIsNone(restart.method_for(DEV_241))           # no ISAPI restart attempt either

    def test_cameras_are_not(self):
        self.assertFalse(hikswitch.is_hikswitch(CAMERA))
        self.assertEqual(restart.method_for(CAMERA)["id"], "hikvision")

    def test_model_on_the_record_counts_without_sadp(self):
        d = {k: v for k, v in DEV_243.items() if k != "sadp"}
        self.assertTrue(hikswitch.is_hikswitch(d))

    def test_credtest_and_restart_plans(self):
        self.assertEqual(credtest.plan(DEV_243, [80]), ["hikswitch"])
        self.assertEqual(credtest.plan(DEV_242, [80]), ["hikswitch"])
        self.assertEqual(restart.method_for(DEV_243)["id"], "hikswitch")
        self.assertEqual(restart.method_for(DEV_243)["gate"], "switch_manage")

    def test_device_macs_include_the_real_mac_behind_a_bridge(self):
        self.assertEqual(switchmon.device_macs(DEV_242), ["e0:63:da:00:00:10", "84:94:59:00:02:42"])
        bridge = {"key": "e0:63:da:00:00:10", "mac": "e0:63:da:00:00:10", "ip": "192.168.1.10"}
        by = switchmon.devices_by_mac([bridge, DEV_242])
        self.assertIs(by["84:94:59:00:02:42"], DEV_242)
        self.assertIs(by["e0:63:da:00:00:10"], bridge)           # its own MAC wins


class LiveFakeTest(unittest.TestCase):
    def setUp(self):
        hikswitch._UNSUPPORTED.clear()
        self.sw = FakeHikSwitch()
        self.addCleanup(self.sw.close)

    def read(self, pw="Secret#123", **kw):
        return hikswitch.read(self.sw.ip, "admin", pw, **kw)

    def test_read_shape(self):
        snap = self.read()
        self.assertTrue(snap["ok"], snap)
        for k in ("device", "caps", "health", "poe", "ports", "lags", "vlans", "services", "summary"):
            self.assertIn(k, snap)
        d = snap["device"]
        self.assertEqual((d["model"], d["firmware"], d["name"]), ("DS-3E1520P-EI", "V3.3.0 build 251010", "Yard Switch"))
        self.assertEqual(d["mac"], "84:94:59:00:02:43")
        self.assertEqual(d["uptime"], 86400 * 3 + 125)
        self.assertEqual((snap["health"]["cpu"], snap["health"]["ram"]), (12.0, 41.0))
        self.assertEqual(len(snap["ports"]), 20)
        self.assertEqual(snap["caps"]["backup"], False)
        self.assertEqual(snap["caps"]["locate"], False)
        self.assertNotIn("missing", snap)

    def test_ports(self):
        ports = {p["id"]: p for p in self.read()["ports"]}
        p1, p5, p17, p19 = ports["0/1"], ports["0/5"], ports["0/17"], ports["0/19"]
        self.assertEqual((p1["up"], p1["speed"], p1["duplex"], p1["name"], p1["label"]), (True, 100, "full", "Gate cam", "Ge1"))
        self.assertEqual(p5["duplex"], "half")
        self.assertEqual((p17["speed"], p17["speed_cfg"]), (1000, "1000M-auto"))
        self.assertEqual(p19["type"], "sfp")
        self.assertFalse(ports["0/4"]["up"])
        self.assertIsNone(ports["0/4"]["speed"])
        self.assertEqual((p1["rx_bps"], p1["tx_bps"], p1["rx_bytes"]), (125000.0, 64000.0, 1000000))
        self.assertEqual(ports["0/3"]["errors"], 7)

    def test_pause_frames_are_not_errors(self):
        # Real V3.3.0 counters: inErrorPacket = CRCPacket + flowControlFrame (received PAUSE frames).
        self.sw.stats[16].update({"inErrorPacket": "1234394", "CRCPacket": "0", "flowControlFrame": "1234394"})
        self.sw.stats[2].update({"inErrorPacket": "7", "CRCPacket": "7", "flowControlFrame": "0"})
        self.sw.stats[17].update({"inErrorPacket": "5", "CRCPacket": "2", "flowControlFrame": "2"})
        ports = {p["id"]: p for p in self.read()["ports"]}
        self.assertEqual((ports["0/17"]["errors"], ports["0/17"]["pause_frames"]), (0, 1234394))
        self.assertEqual((ports["0/3"]["errors"], ports["0/3"]["pause_frames"]), (7, 0))
        self.assertEqual(ports["0/18"]["errors"], 3)          # other kinds of error still count
        self.assertIsNone(ports["0/1"]["pause_frames"])       # a firmware without the field

    def test_poe(self):
        snap = self.read()
        ports = {p["id"]: p for p in snap["ports"]}
        self.assertEqual((ports["0/1"]["poe_mode"], ports["0/1"]["poe_w"], ports["0/1"]["poe_status"]), ("auto", 6.4, "powered on (class 4)"))
        self.assertEqual(ports["0/1"]["poe_modes"], ["off", "auto", "compat"])
        self.assertTrue(ports["0/3"]["poe_fault"])
        self.assertIn("over-current", ports["0/3"]["poe_status"])
        self.assertFalse(ports["0/4"]["poe_fault"])               # failed detection = not a PoE device
        self.assertEqual(ports["0/4"]["poe_status"], "no PoE device detected: resistance too high")
        # "testing" is what a real switch says on an enabled PoE port with nothing that wants
        # power (no cable, or a laptop/uplink/NVR on it) — not an alarm
        self.assertEqual((ports["0/6"]["poe_status"], ports["0/6"]["poe_fault"]), ("waiting for load", False))
        self.assertFalse(ports["0/17"]["poe_supported"])
        self.assertEqual(snap["poe"]["budget_w"], 230.0)
        self.assertEqual(snap["poe"]["used_w"], 10.5)
        self.assertEqual(snap["poe"]["pct"], 5)
        self.assertTrue(ports["0/1"]["ping_watchdog"])
        self.assertFalse(ports["0/17"]["ping_watchdog"])

    def test_mac_table_any_format_and_port_name(self):
        ports = {p["id"]: p for p in self.read()["ports"]}
        self.assertEqual([m["mac"] for m in ports["0/1"]["macs"]], ["c0:51:7e:00:00:01"])      # via portName
        self.assertEqual([m["mac"] for m in ports["0/2"]["macs"]], ["04:ee:cd:00:00:65", "04:ee:cd:00:00:66"])
        self.assertEqual(len(ports["0/17"]["macs"]), 9)
        self.assertFalse(any(m["mac"] == "11:22:33:44:55:66" for p in ports.values() for m in p["macs"]))

    def test_paging(self):
        self.sw.page = 3                                         # every list comes in pages of 3
        snap = self.read()
        self.assertEqual(len(snap["ports"]), 20)
        self.assertEqual(sum(len(p["macs"]) for p in snap["ports"]), 12)
        macs = [b for m, p, b in self.sw.calls if p.endswith("SearchPortMacAddress")]
        self.assertTrue(any(b["data"].get("lastMacAddress") for b in macs[1:]))

    def test_loop_and_services(self):
        snap = self.read()
        ports = {p["id"]: p for p in snap["ports"]}
        self.assertTrue(ports["0/5"]["loop_blocked"])
        self.assertEqual(snap["loops"], ["0/5"])
        self.assertEqual((snap["services"]["telnet"], snap["services"]["ssh"], snap["services"]["snmp"]), (True, False, False))

    def test_one_login_per_read_and_logout(self):
        self.read()
        names = self.sw.names()
        self.assertEqual(names.count("AuthInfo"), 1)
        self.assertEqual(names.count("Login"), 1)
        self.assertEqual(names[-1], "Logout")
        login = next(b for m, p, b in self.sw.calls if p.endswith("Session/Login"))
        self.assertEqual(set(login["data"]), {"isSessionIDValidLongTerm", "password", "sessionID",
                                              "sessionIDVersion", "username", "isNeedSessionTag"})
        self.assertIsNone(self.sw.tag)

    def test_wrong_password_is_auth_failed_with_tries_left(self):
        snap = self.read(pw="wrong")
        self.assertFalse(snap["ok"])
        self.assertEqual(snap["kind"], "auth_failed")
        self.assertIn("2 tries left", snap["error"])
        self.assertEqual(self.sw.names().count("Login"), 1)       # never retried here

    def test_locked_account_says_so(self):
        self.sw.locked = True
        snap = self.read()
        self.assertEqual(snap["kind"], "auth_failed")
        self.assertIn("LOCKED", snap["error"])
        self.assertIn("30 min", snap["error"])

    def test_unreachable(self):
        self.sw.close()
        snap = self.read()
        self.assertEqual(snap["kind"], "unreachable")

    def test_optional_parts_missing_still_read(self):
        self.sw.unsupported = {"STPManage/SearchPortPrivateLoopStatus", "POE/PoEWatchdogParams"}
        snap = self.read()
        self.assertTrue(snap["ok"])
        self.assertIn("loop", snap["missing"])
        self.assertFalse(any(p["loop_blocked"] for p in snap["ports"]))

    def test_unsupported_parts_not_asked_again(self):
        # a DS-3E1520P-EI V3.3.0 answers 404 / 0x00100003 for loop status, SSH and SNMP
        self.sw.unsupported = {"STPManage/SearchPortPrivateLoopStatus", "SNMPAccess/SNMPCfg"}
        first = self.read(keep_raw=True)
        self.assertTrue(first["ok"])
        self.assertIn("not supported", first["missing"]["loop"])
        self.assertIn("services.snmp", first["missing"])
        self.assertEqual(first["raw"]["_not_supported"], ["loop", "services.snmp"])
        self.assertIsNone(first["services"]["snmp"])
        self.assertIs(first["services"]["telnet"], True)
        n0 = len(self.sw.calls)
        second = self.read()
        asked = [c[1] for c in self.sw.calls[n0:]]
        self.assertTrue(second["ok"])
        self.assertFalse(any("SearchPortPrivateLoopStatus" in a or "SNMPCfg" in a for a in asked), asked)
        self.assertTrue(any("TelnetServerCfg" in a for a in asked))
        self.assertNotIn("missing", second)
        self.assertIsNone(second["services"]["snmp"])

    def test_only_not_supported_answers_are_remembered(self):
        self.sw.attrs.pop("InfoMgr/DeviceDescription")       # -> 404 as well: counts as unsupported
        self.read()
        self.assertIn("description", hikswitch._UNSUPPORTED[self.sw.ip])
        hikswitch._UNSUPPORTED.clear()
        err = hikswitch.HikSwitchError("x: busy (HTTP 500, 0x00000001)", "error", 500, "0x00000001")
        self.assertFalse(hikswitch._not_supported(err))

    def test_full_serial_from_the_description(self):
        d = self.read()["device"]
        self.assertEqual(d["serial"], "DS-3E1520P-EI20260101GV0000001")    # as on the label and in SADP
        self.sw.attrs["InfoMgr/DeviceDescription"].pop("serialNumber")
        hikswitch._UNSUPPORTED.clear()
        self.assertEqual(self.read()["device"]["serial"], "GV0000001")

    def test_forced_poe_stays_forced(self):
        self.assertEqual(hikswitch._poe_item({"poeMode": "force", "enabled": True}, 3, "force")["poeMode"], "force")
        self.assertEqual(hikswitch._poe_item({"poeMode": "compat", "enabled": True}, 3, "auto")["poeMode"], "standard")
        self.assertEqual(hikswitch._poe_item({"poeMode": "standard", "enabled": True}, 3, "off"),
                         {"portID": 3, "enabled": False})

    def test_raw_is_kept_on_request_only(self):
        self.assertNotIn("raw", self.read())
        raw = self.read(keep_raw=True)["raw"]
        self.assertEqual(raw["summary"]["model"], "DS-3E1520P-EI")
        self.assertNotIn("password", str(raw).lower())

    def test_port_off_keeps_speed_duplex_and_flow(self):
        res = hikswitch.set_port(self.sw.ip, "admin", "Secret#123", "0/17", enabled=False)
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["before"]["enabled"], res["after"]["enabled"]), (True, False))
        body = next(b for m, p, b in self.sw.calls if p.endswith("ModifyBatchPortBasicParam"))
        self.assertEqual(body["data"]["portBasicList"], [{"portID": 17, "enabled": False, "speed": "1000M",
                                                          "duplexMode": "auto", "flowCtrlEnabled": False}])
        self.assertFalse({p["id"]: p for p in self.read()["ports"]}["0/17"]["enabled"])

    def test_rename(self):
        res = hikswitch.set_port(self.sw.ip, "admin", "Secret#123", "0/2", name="  Office cam  ")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["after"]["name"], "Office cam")
        self.assertEqual({p["id"]: p for p in self.read()["ports"]}["0/2"]["name"], "Office cam")

    def test_poe_modes(self):
        for mode, want in (("off", {"portID": 2, "enabled": False, "nonStandardEnabled": False}),
                           ("compat", {"portID": 2, "enabled": True, "nonStandardEnabled": True}),
                           ("auto", {"portID": 2, "enabled": True, "nonStandardEnabled": False})):
            self.sw.calls.clear()
            res = hikswitch.set_port(self.sw.ip, "admin", "Secret#123", "0/2", poe=mode)
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["after"]["poe"], mode)
            body = next(b for m, p, b in self.sw.calls if p.endswith("ModifyPortPoeInfo"))
            self.assertEqual(body["data"], [want])

    def test_poe_mode_field_when_the_switch_has_it(self):
        for row in self.sw.poe:
            row.pop("nonStandardEnabled")
            row["poeMode"] = "standard"
        res = hikswitch.set_port(self.sw.ip, "admin", "Secret#123", "0/1", poe="compat")
        self.assertTrue(res["ok"], res)
        body = next(b for m, p, b in self.sw.calls if p.endswith("ModifyPortPoeInfo"))
        self.assertEqual(body["data"], [{"portID": 1, "enabled": True, "poeMode": "compat"}])

    def test_bad_requests_are_refused_before_sending(self):
        self.assertFalse(hikswitch.set_port(self.sw.ip, "admin", "Secret#123", "0/2", poe="force")["ok"])
        res = hikswitch.set_port(self.sw.ip, "admin", "Secret#123", "0/17", poe="off")
        self.assertFalse(res["ok"])
        self.assertIn("no PoE", res["error"])
        res = hikswitch.set_port(self.sw.ip, "admin", "Secret#123", "0/99", enabled=False)
        self.assertIn("not found", res["error"])
        self.assertNotIn("ModifyBatchPortBasicParam", self.sw.names())

    def test_poe_cycle_restores_mode(self):
        self.sw.poe[0]["nonStandardEnabled"] = True              # port 1 on "compat"
        with mock.patch.object(hikswitch.time, "sleep") as slept:
            res = hikswitch.poe_cycle(self.sw.ip, "admin", "Secret#123", "0/1", 5)
        self.assertTrue(res["ok"], res)
        slept.assert_called_once_with(5)
        bodies = [b["data"] for m, p, b in self.sw.calls if p.endswith("ModifyPortPoeInfo")]
        self.assertEqual(bodies, [[{"portID": 1, "enabled": False, "nonStandardEnabled": True}],
                                  [{"portID": 1, "enabled": True, "nonStandardEnabled": True}]])
        self.assertEqual((self.sw.poe[0]["enabled"], self.sw.poe[0]["nonStandardEnabled"]), (True, True))

    def test_poe_cycle_refuses_a_port_with_poe_off(self):
        self.sw.poe[5]["enabled"] = False
        with mock.patch.object(hikswitch.time, "sleep"):
            res = hikswitch.poe_cycle(self.sw.ip, "admin", "Secret#123", "0/6")
        self.assertFalse(res["ok"])
        self.assertNotIn("ModifyPortPoeInfo", self.sw.names())

    def test_reboot_without_logout(self):
        self.assertTrue(hikswitch.reboot(self.sw.ip, "admin", "Secret#123")["ok"])
        self.assertTrue(self.sw.rebooted)
        self.assertNotIn("Logout", self.sw.names())

    def test_unsupported_actions_say_so(self):
        for fn in (hikswitch.locate, hikswitch.backup):
            self.assertFalse(fn(self.sw.ip, "admin", "Secret#123")["ok"])
        self.assertEqual(self.sw.calls, [])                       # no login for nothing
        # (the cable test is the switch's own Cable Detection: test_hikswitch_adv.py)

    def test_credtest_one_login(self):
        dev = dict(DEV_243, ip=self.sw.ip)
        ok = credtest.test_hikswitch(self.sw.ip, "admin", "Secret#123")
        self.assertEqual((ok["result"], ok["info"]["model"]), ("ok", "DS-3E1520P-EI"))
        bad = credtest.test_hikswitch(self.sw.ip, "admin", "nope")
        self.assertEqual(bad["result"], "auth_failed")
        self.assertIn("tries left", bad["detail"])
        self.assertEqual(self.sw.names().count("Login"), 2)
        self.assertTrue(dev)

    def test_protected_ports_and_uplinks_from_the_mac_table(self):
        snap = self.read()
        prot = switchmon.protected_ports(snap, own_macs={"04:ee:cd:00:00:66"}, gw_mac="c0:51:7e:00:00:01")
        self.assertEqual(set(prot), {"0/1", "0/2"})
        self.assertEqual(switchmon.uplink_ports(snap), {"0/17": 9})


class MonitorRulesTest(unittest.TestCase):
    def test_loop_and_poe_fault_problems(self):
        sw = FakeHikSwitch()
        self.addCleanup(sw.close)
        snap = hikswitch.read(sw.ip, "admin", "Secret#123")
        mon = switchmon.SwitchMonitor()
        mon._get_devices = lambda: [DEV_243]
        with mock.patch.object(switchmon.history, "switch_port_series", return_value=[]), \
                mock.patch.object(switchmon.history, "switch_port_baseline", return_value={}):
            probs = mon._evaluate(DEV_243["key"], DEV_243, None, snap)
        by = {(p["metric"], p["port"]): p for p in probs}
        self.assertEqual(by[("loop", "0/5")]["level"], "crit")
        self.assertEqual(by[("poe_fault", "0/3")]["level"], "crit")
        self.assertNotIn(("poe_fault", "0/4"), by)
        self.assertEqual(by[("half_duplex", "0/5")]["level"], "warn")
        self.assertIn(("telnet_on", None), by)


class ServerFlowTest(unittest.TestCase):
    """The site's own routes with the fake switch as a device: poll, the Managed
    list, the raw read, an action through the protection checks, and a device
    behind a radio bridge found on its port by its real (discovery) MAC."""
    KEY = "k" * 43

    def setUp(self):
        import creds
        import server
        import siteauth
        self.server, self.creds = server, creds
        self.sw = FakeHikSwitch()
        self.addCleanup(self.sw.close)
        if os.path.exists(siteauth.AUTH_PATH):
            os.remove(siteauth.AUTH_PATH)
        siteauth._fails.clear()
        siteauth.set_hub_key(self.KEY)
        self.hdr = {siteauth.HEADER: self.KEY}
        sc = server.scanner
        self.key = DEV_243["key"]
        cam = {"key": "192.168.1.65", "ip": "192.168.1.65", "mac": "e0:63:da:00:00:10", "online": True,
               "category": "camera", "name": "Office cam", "sadp": {"mac": "04:ee:cd:00:00:65"}}
        sc.devices = {self.key: dict(DEV_243, ip=self.sw.ip, last_seen=int(time.time())),
                      cam["key"]: dict(cam, last_seen=int(time.time()))}
        sc.registry = {self.key: {}, cam["key"]: {}}
        mock.patch.object(sc, "save_registry").start()
        mock.patch.object(sc, "trigger").start()
        mock.patch.object(server, "_sw_enabled", return_value=True).start()
        self.addCleanup(mock.patch.stopall)
        creds.set_(self.key, "admin", "Secret#123")
        self.addCleanup(creds.set_, self.key, "", "")
        m = switchmon.monitor
        m._get_devices, m._get_registry, m._get_config = sc.get_devices, (lambda: sc.registry), server.config.load
        m._last.clear()
        self.c = server.app.test_client()

    def test_poll_list_raw_action_and_bridge_device(self):
        lan = {"REMOTE_ADDR": "192.168.1.50"}
        r = self.c.post(f"/api/devices/{self.key}/switch/poll", headers=self.hdr, environ_base=lan)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])
        view = r.get_json()
        self.assertEqual((view["ok"], view["model"], len(view["ports"])), (True, "DS-3E1520P-EI", 20))
        self.assertEqual(view["name"], "Yard Switch")

        j = self.c.get("/api/switches", environ_base=lan).get_json()
        self.assertEqual([s["key"] for s in j["switches"]], [self.key])
        metrics = {(p["metric"], p["port"]) for p in j["problems"]}
        self.assertIn(("loop", "0/5"), metrics)
        p2 = next(p for p in j["switches"][0]["ports"] if p["id"] == "0/2")
        self.assertEqual([d["name"] for d in p2["devices"]], ["Office cam"])      # its real MAC, not the bridge's

        devs = self.c.get("/api/devices", environ_base=lan).get_json()
        rows = devs["devices"] if isinstance(devs, dict) else devs
        cam = next(d for d in rows if d["key"] == "192.168.1.65")
        self.assertEqual((cam["switch_port"] or {}).get("port"), "0/2")

        self.assertEqual(self.c.get(f"/api/devices/{self.key}/switch/raw", environ_base=lan).status_code, 401)
        raw = self.c.get(f"/api/devices/{self.key}/switch/raw", headers=self.hdr, environ_base=lan).get_json()
        self.assertEqual((raw["ok"], raw["driver"]), (True, "hikswitch"))
        self.assertEqual(raw["raw"]["summary"]["serialNumber"], "GV0000001")

        # an uplink (9 MACs) asks first; a PoE cycle on a camera port goes through
        r = self.c.post(f"/api/devices/{self.key}/switch/action", headers=self.hdr, environ_base=lan,
                        json={"action": "port", "port": "0/17", "enabled": False})
        self.assertTrue(r.get_json().get("needs_confirm"))
        with mock.patch.object(hikswitch.time, "sleep"):
            r = self.c.post(f"/api/devices/{self.key}/switch/action", headers=self.hdr, environ_base=lan,
                            json={"action": "poe-cycle", "port": "0/2", "off_s": 3})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])
        pokes = [b["data"] for m, p, b in self.sw.calls if p.endswith("ModifyPortPoeInfo")]
        self.assertEqual([x[0]["enabled"] for x in pokes], [False, True])


if __name__ == "__main__":
    unittest.main()
