"""Advanced settings of Hikvision smart switches (app/hikswitch_adv.py): the thing
model (what a switch has, its ranges), every reading against the fake switch
(tests/hikswitch_fake.py), each change sending exactly the body the switch's own
web app sends, the refusals, Netwatch's advice, the cable test, and the site
route /api/devices/<key>/switch/advanced with its protection checks.

Run inside the site image:
  docker run --rm --network none -v "$PWD/app:/app" -v "$PWD/tests:/tests" -e NETWATCH_DATA=/tmp/nw \
    --entrypoint python farm-netwatch:netcfg /tests/test_hikswitch_adv.py -v
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

import hikswitch      # noqa: E402
import hikswitch_adv  # noqa: E402
import switchmon      # noqa: E402
from hikswitch_fake import FakeHikSwitch  # noqa: E402

PW = "Secret#123"


def sets(sw, name):
    """Bodies the fake got for one attribute write or service command."""
    return [b["data"] for m, p, b in sw.calls if m == "POST" and p.endswith("/" + name) and isinstance(b, dict)]


class Base(unittest.TestCase):
    def setUp(self):
        hikswitch._MODELS.clear()
        hikswitch._UNSUPPORTED.clear()
        hikswitch_adv._UNSUPPORTED.clear()
        self.sw = FakeHikSwitch()
        self.addCleanup(self.sw.close)

    def read(self):
        r = hikswitch_adv.read_advanced(self.sw.ip, "admin", PW)
        self.assertTrue(r["ok"], r)
        return r

    def change(self, feature, **body):
        return hikswitch_adv.set_feature(self.sw.ip, "admin", PW, feature, body)


class ThingModelTest(Base):
    def test_caps_like_the_web_app(self):
        with hikswitch.Session(self.sw.ip, "admin", PW) as s:
            m = hikswitch.load_model(s, "V3.3.0 build 251010")
        self.assertIs(hikswitch.model_has(m, "STPManage", "STPGlobalCfg", "props"), True)
        self.assertIs(hikswitch.model_has(m, "ERPSMgr"), False)
        self.assertIs(hikswitch.model_has(None, "ERPSMgr"), None)
        self.assertEqual(hikswitch.model_caps(m, "PortRate/ModifyPortRateCfgInfo")["range"]["portRateList-rxLimitRate"],
                         {"min": 1, "max": 1000})
        self.assertEqual(hikswitch.model_caps(m, "PortRemoteTrans/ModifyPortRemoteTrans")["range"]["portID"],
                         {"min": 1, "max": 8})                      # an array input: items' properties
        self.assertEqual(hikswitch.model_caps(m, "TimeMgr/SystemDateTime")["range"]["NTPServer-synchronizeInterval"],
                         {"min": 1, "max": 1440})
        self.assertEqual(hikswitch.model_caps(m, "PortCast/PortStormGlobalCfg")["options"]["castCtrlMode"],
                         ["broadCast", "multiCast", "unknownUnicast"])
        self.assertEqual(hikswitch.model_caps(m, "Nope/Nothing"), {"default": {}, "range": {}, "options": {}})

    def test_model_read_once_per_switch_and_firmware(self):
        self.read()
        self.read()
        self.assertEqual(self.sw.names().count("GetModelFile"), 1)


class ReadTest(Base):
    def test_everything_this_switch_has(self):
        r = self.read()
        f = r["features"]
        self.assertTrue(r["model_known"])
        self.assertEqual(len(r["ports"]), 20)
        self.assertEqual((f["rstp"]["supported"], f["rstp"]["enabled"], f["rstp"]["priority"]), (True, False, 32768))
        self.assertEqual(f["rstp"]["priority_range"], [0, 61440])
        self.assertEqual((f["loop_guard"]["supported"], f["loop_guard"]["enabled"]), (True, False))
        self.assertEqual(f["erps"], {"supported": False})            # not in the thing model: never asked
        self.assertEqual(f["storm"]["global"]["mode"], "broadCast")
        self.assertEqual(f["storm"]["global"]["ratio_pct"], 10)
        self.assertFalse(any(v["enabled"] for v in f["storm"]["ports"].values()))
        self.assertEqual(f["rate_limit"]["ports"]["0/6"], {"rx_mbps": 20, "tx_mbps": None})   # "MatchResults"
        self.assertEqual((f["qos"]["enabled"], f["qos"]["can_set"], f["qos"]["port_range"]), (False, True, [1, 16]))
        self.assertEqual(f["poe_watchdog"], {"supported": True, "enabled": True})
        self.assertEqual((f["long_range"]["ports_on"], f["long_range"]["port_range"], f["long_range"]["auto"]),
                         (["0/8"], [1, 8], None))
        self.assertEqual(f["eee"], {"supported": True, "enabled": True})
        nb = f["lldp"]["neighbors"]
        self.assertEqual((nb[0]["port"], nb[0]["name"], nb[0]["peer_port"]), ("0/17", "Yard Router", "ether3"))
        self.assertEqual((f["dhcp_snooping"]["enabled"], f["dhcp_snooping"]["trusted"]), (False, []))
        self.assertEqual((f["isolation"]["ports"], f["isolation"]["port_range"]), ([], [1, 18]))
        acc = f["access"]
        self.assertEqual((acc["telnet"], acc["onvif"], acc["sadp_agent"], acc["sadp_server"], acc["http_port"]),
                         (True, True, True, True, 80))
        self.assertIsNone(acc["ssh"])                                 # no SSHCfg in this model
        self.assertIs(acc["cloud"], False)
        self.assertEqual((f["time"]["mode"], f["time"]["ntp_server"], f["time"]["interval_min"]),
                         ("manually", "time.ys7.com", 60))
        self.assertIsInstance(f["time"]["drift_s"], int)
        self.assertEqual(f["cable_test"], {"supported": True, "port_range": [1, 16]})
        self.assertEqual(f["sfp"]["modules"]["0/19"]["rx_dbm"], -9.8)
        self.assertEqual(f["sfp"]["modules"]["0/19"]["vendor"], "HIKVISION")
        self.assertEqual(f["vlan"]["ports"]["0/1"], {"type": "access", "pvid": 1, "vlans": [1]})
        self.assertEqual(f["lag"], {"supported": True, "groups": []})
        self.assertEqual(f["mirror"]["enabled"], False)
        self.assertEqual(f["port_security"]["enabled"], False)
        self.assertTrue(f["other"]["acl"] and f["other"]["ip_source_guard"] and f["other"]["arp_protection"])
        names = self.sw.names()
        for never in ("SSHCfg", "SearchLoopParams", "SNMPCfg"):
            self.assertNotIn(never, names)
        self.assertEqual(names.count("Login"), 1)                   # one login for the lot
        self.assertNotIn("missing", r)

    def test_rstp_on_shows_roles_and_the_standby_port(self):
        self.sw.attrs["STPManage/STPGlobalCfg"]["STPEnabled"] = True
        st = self.read()["features"]["rstp"]
        self.assertEqual(st["blocked"], ["0/19"])
        self.assertEqual(st["ports"]["0/17"]["role"], "root")
        self.assertEqual(st["root_id"], "4096.48:a9:8a:00:00:01")
        self.assertFalse(st["is_root"])

    def test_without_a_thing_model_each_part_is_tried(self):
        self.sw.model_file = False
        self.sw.unsupported |= {"STPManage/PrivateLoopProtocolCfg", "PortRemoteTrans/SearchPortRemoteTrans"}
        r = self.read()
        self.assertFalse(r["model_known"])
        self.assertEqual(r["features"]["loop_guard"], {"supported": False})
        self.assertEqual(r["features"]["long_range"], {"supported": False})
        self.assertIn("loop.cfg", r["not_supported"])
        self.assertIsNone(r["features"]["cable_test"]["supported"])  # unknown: tried when someone asks
        self.sw.calls.clear()
        self.read()
        self.assertNotIn("PrivateLoopProtocolCfg", self.sw.names())
        self.assertNotIn("SearchPortRemoteTrans", self.sw.names())

    def test_a_part_the_model_lacks_is_not_asked(self):
        self.sw.model_drop |= {"PortCast/PortStormGlobalCfg"}
        storm = self.read()["features"]["storm"]
        self.assertIsNone(storm["global"])
        self.assertEqual(storm["types"], ["broadcast", "multicast", "unicast"])
        self.assertEqual(storm["limit_range"], [1, 1000])
        self.assertNotIn("PortStormGlobalCfg", self.sw.names())

    def test_wrong_password_one_try(self):
        r = hikswitch_adv.read_advanced(self.sw.ip, "admin", "nope")
        self.assertEqual((r["ok"], r["kind"]), (False, "auth_failed"))
        r = hikswitch_adv.set_feature(self.sw.ip, "admin", "nope", "lldp", {"enabled": False})
        self.assertEqual((r["ok"], r["kind"]), (False, "auth_failed"))
        self.assertEqual(self.sw.names().count("Login"), 2)
        self.assertTrue(self.sw.attrs["LLDPMange/LLDPCfg"]["enabled"])


class ChangeTest(Base):
    def test_rstp_on_keeps_the_timers_and_says_rstp(self):
        r = self.change("rstp", enabled=True)
        self.assertTrue(r["ok"], r)
        self.assertEqual(sets(self.sw, "STPGlobalCfg"), [{"Value": {
            "STPEnabled": True, "STPMode": "RSTP", "sysPriority": 32768, "helloTime": 2,
            "maxAgingTime": 20, "forwardDelayTime": 15}}])
        self.assertEqual((r["before"]["enabled"], r["after"]["enabled"], r["shows"]), (False, True, "rstp"))

    def test_rstp_priority(self):
        r = self.change("rstp", enabled=True, priority=5000)
        self.assertFalse(r["ok"])
        self.assertTrue(r["refused"])
        self.assertIn("4096", r["error"])
        self.assertEqual(sets(self.sw, "STPGlobalCfg"), [])
        self.assertTrue(self.change("rstp", enabled=True, priority=4096)["ok"])
        self.assertEqual(sets(self.sw, "STPGlobalCfg")[-1]["Value"]["sysPriority"], 4096)

    def test_rstp_off_sends_just_the_switch(self):
        self.sw.attrs["STPManage/STPGlobalCfg"]["STPEnabled"] = True
        self.assertTrue(self.change("rstp", enabled=False)["ok"])
        self.assertEqual(sets(self.sw, "STPGlobalCfg"), [{"Value": {"STPEnabled": False, "STPMode": "RSTP"}}])

    def test_simple_switches(self):
        for feature, attr in (("loop_guard", "PrivateLoopProtocolCfg"), ("poe_watchdog", "PoEWatchdogParams"),
                              ("eee", "PowerSavingModeParam"), ("lldp", "LLDPCfg")):
            want = feature == "loop_guard"
            r = self.change(feature, enabled=want)
            self.assertTrue(r["ok"], (feature, r))
            self.assertEqual(sets(self.sw, attr), [{"Value": {"enabled": want}}])
            self.assertEqual(r["after"]["enabled"], want)
        self.assertFalse(self.change("lldp", enabled="yes")["ok"])     # a real bool, nothing else

    def test_storm_control(self):
        r = self.change("storm", ports=["0/1", "0/2"], enabled=True)
        self.assertTrue(r["ok"], r)
        self.assertEqual(sets(self.sw, "ModifyPortCastCfgInfo"), [[{"portID": 1, "enabled": True}, {"portID": 2, "enabled": True}]])
        self.assertTrue(r["after"]["ports"]["0/2"]["enabled"])
        self.assertTrue(self.change("storm", mode="multiCast", ratio_pct=5)["ok"])
        self.assertEqual(sets(self.sw, "PortStormGlobalCfg"), [{"Value": {"castCtrlMode": "multiCast", "maxSpeedRatio": 5}}])
        self.assertFalse(self.change("storm", mode="everything", ratio_pct=5)["ok"])
        self.assertFalse(self.change("storm", ratio_pct=0, mode="broadCast")["ok"])
        self.assertFalse(self.change("storm", ports=["0/99"], enabled=True)["ok"])

    def test_storm_control_per_port_limit(self):
        self.sw.model_drop |= {"PortCast/PortStormGlobalCfg"}
        self.assertTrue(self.change("storm", ports=["0/3"], enabled=True, type="broadcast", limit_mbps=10)["ok"])
        self.assertTrue(self.change("storm", ports=["0/4"], enabled=False)["ok"])
        self.assertEqual(sets(self.sw, "ModifyPortCastCfgInfo"), [
            [{"portID": 3, "enabled": True, "stormControlType": "broadcast", "limitRate": 10}],
            [{"portID": 4, "enabled": False}]])

    def test_bandwidth_limits(self):
        self.assertTrue(self.change("rate_limit", ports=["0/5"], rx_mbps=30, tx_mbps=None)["ok"])
        self.assertTrue(self.change("rate_limit", ports=["0/6"], rx_mbps=None, tx_mbps=None)["ok"])
        self.assertEqual(sets(self.sw, "ModifyPortRateCfgInfo"), [
            {"portRateList": [{"portID": 5, "rxEnabled": True, "txEnabled": False, "rxLimitRate": 30}]},
            {"portRateList": [{"portID": 6, "rxEnabled": False, "txEnabled": False}]}])
        r = self.change("rate_limit", ports=["0/5"], rx_mbps=5000)
        self.assertFalse(r["ok"])
        self.assertIn("1–1000", r["error"])

    def test_qos(self):
        self.assertTrue(self.change("qos", enabled=True, algorithm="WRR")["ok"])
        self.assertEqual(sets(self.sw, "QoSCfg"), [{"Value": {"QoSEnabled": True, "QoSType": "wholeQoS", "QoSAlgorithm": "WRR",
                                                             "lowPriorityWeight": 1, "highPriorityWeight": 8}}])
        r = self.change("qos", ports=["0/4"], high=True)
        self.assertTrue(r["ok"], r)
        self.assertEqual(sets(self.sw, "QOSPortPriorityBatchCfg"), [{"Value": {"QOSPortPriorityList": [{"portID": 4, "priority": 1}]}}])
        self.assertEqual(r["after"]["high_ports"], ["0/4"])
        self.assertFalse(self.change("qos", ports=["0/19"], high=True)["ok"])   # outside the QoS ports 1–16
        self.assertTrue(self.change("qos", enabled=False)["ok"])
        self.assertEqual(sets(self.sw, "QoSCfg")[-1], {"Value": {"QoSEnabled": False}})

    def test_long_range(self):
        r = self.change("long_range", ports=["0/3"], enabled=True)
        self.assertTrue(r["ok"], r)
        self.assertEqual(sets(self.sw, "ModifyPortRemoteTrans"), [[{"portID": 3, "enabled": True}]])
        self.assertEqual(r["after"]["ports_on"], ["0/3", "0/8"])
        r = self.change("long_range", ports=["0/12"], enabled=True)
        self.assertFalse(r["ok"])
        self.assertIn("1–8", r["error"])

    def test_long_range_auto_mode(self):
        self.sw.model_file = False                  # (the fake's thing model has no auto mode)
        self.sw.attrs["PortRemoteTrans/PortRemoteGlobalParams"] = {"autoPortRemoteEnable": True}
        self.assertIn("automatic", self.change("long_range", ports=["0/3"], enabled=True)["error"])
        self.assertTrue(self.change("long_range", auto=False)["ok"])
        self.assertEqual(sets(self.sw, "PortRemoteGlobalParams"), [{"Value": {"autoPortRemoteEnable": False}}])

    def test_dhcp_snooping(self):
        r = self.change("dhcp_snooping", enabled=True, trusted=["0/17"])
        self.assertTrue(r["ok"], r)
        body = sets(self.sw, "DHCPSnoopingCfg")[0]["Value"]
        self.assertTrue(body["enabled"])
        self.assertEqual(len(body["trustPortList"]), 20)
        self.assertEqual([x["portID"] for x in body["trustPortList"] if x["enabled"]], [17])
        self.assertEqual((r["after"]["enabled"], r["after"]["trusted"]), (True, ["0/17"]))
        self.assertEqual(r["after"]["entries"][0]["port"], "0/2")
        self.assertFalse(self.change("dhcp_snooping", enabled=True, trusted=[])["ok"])
        self.assertTrue(self.change("dhcp_snooping", enabled=False)["ok"])
        self.assertEqual(sets(self.sw, "DHCPSnoopingCfg")[-1], {"Value": {"enabled": False}})

    def test_isolation(self):
        self.assertTrue(self.change("isolation", ports=["0/1", "0/2"], isolated=True)["ok"])
        r = self.change("isolation", ports=["0/1"], isolated=False)
        self.assertEqual(sets(self.sw, "ModifyIsolationGroup"), [
            {"isolationGroupID": 1, "portList": [{"portID": 1}, {"portID": 2}]},
            {"isolationGroupID": 1, "portList": [{"portID": 2}]}])
        self.assertEqual(r["after"]["ports"], ["0/2"])
        self.assertFalse(self.change("isolation", ports=["0/19"], isolated=True)["ok"])   # ports 1–18 only

    def test_access(self):
        self.assertTrue(self.change("telnet", enabled=False)["ok"])
        self.assertTrue(self.change("onvif", enabled=False)["ok"])
        r = self.change("sadp_agent", enabled=False)
        self.assertTrue(r["ok"], r)
        self.assertEqual(sets(self.sw, "TelnetServerCfg"), [{"Value": {"enabled": False}}])
        self.assertEqual(sets(self.sw, "SADPCfg"), [{"Value": {"SADPClient": {"enabled": False}}}])
        self.assertEqual((r["after"]["sadp_agent"], r["after"]["sadp_server"], r["after"]["telnet"]), (False, True, False))

    def test_ntp(self):
        self.assertTrue(self.change("ntp", server="192.168.1.20")["ok"])
        r = self.change("ntp", server="pool.ntp.org", interval_min=30)
        self.assertTrue(r["ok"], r)
        self.assertEqual(sets(self.sw, "SystemDateTime"), [
            {"Value": {"dateTimeType": "NTP", "NTPServer": {"portNo": 123, "addressingFormatType": "ipaddress",
                                                            "ipV4Address": "192.168.1.20", "synchronizeInterval": 60}}},
            {"Value": {"dateTimeType": "NTP", "NTPServer": {"portNo": 123, "addressingFormatType": "hostname",
                                                            "hostName": "pool.ntp.org", "synchronizeInterval": 30}}}])
        self.assertEqual((r["after"]["mode"], r["after"]["ntp_server"]), ("NTP", "pool.ntp.org"))
        for bad in ("", "not a host", "http://x", "a" * 300):
            self.assertFalse(self.change("ntp", server=bad)["ok"], bad)

    def test_counters_and_unknown(self):
        self.assertTrue(self.change("counters")["ok"])
        self.assertEqual(self.sw.reset_count, 1)
        self.assertFalse(self.change("teleport", enabled=True)["ok"])

    def test_a_setting_the_switch_lacks_is_refused_without_a_write(self):
        self.sw.model_drop |= {"LLDPMange/LLDPCfg"}
        r = self.change("lldp", enabled=False)
        self.assertFalse(r["ok"])
        self.assertTrue(r["refused"])
        self.assertEqual(sets(self.sw, "LLDPCfg"), [])


class CableTestTest(Base):
    def test_cable_test_in_plain_words(self):
        r = hikswitch.cable_test(self.sw.ip, "admin", PW, "0/3")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"], {"good": False, "status": "abnormal", "length_m": 46, "codes": ["B_OPEN"],
                                       "text": "pair B is open (broken or not connected)"})
        self.assertTrue(hikswitch.cable_test(self.sw.ip, "admin", PW, "0/1")["result"]["good"])
        self.assertEqual(sets(self.sw, "GetPortLineStatus")[0], {"portID": 3})
        r = hikswitch.cable_test(self.sw.ip, "admin", PW, "0/19")
        self.assertFalse(r["ok"])
        self.assertIn("SFP", r["error"])
        self.assertFalse(hikswitch.cable_test(self.sw.ip, "admin", PW, "0/77")["ok"])

    def test_caps_say_cable_test_and_advanced(self):
        snap = hikswitch.read(self.sw.ip, "admin", PW)
        self.assertTrue(snap["caps"]["cable_test"] and snap["caps"]["advanced"])


class AdviceTest(Base):
    def test_suggestions_for_a_factory_switch(self):
        adv = self.read()
        prot = {"0/18": "the site's router / internet is connected through this port"}
        ups = {"0/17": 9}
        probs = [{"metric": "port_errors", "port": "0/3", "what": "Port 3: 480 errors/drops per hour", "level": "warn"},
                 {"metric": "port_flapping", "port": "0/3", "what": "Port 3 went down and up", "level": "warn"}]
        tips = hikswitch_adv.advice(adv, protected=prot, uplinks=ups, problems=probs, dhcp_ports=["0/17"],
                                    snap_ports=[{"id": "0/1", "poe_w": 6.4}])
        by = {t["feature"]: t for t in tips}
        self.assertEqual(by["loop_guard"]["change"], {"enabled": True})
        self.assertEqual((by["rstp"]["level"], by["rstp"]["change"]), ("do", {"enabled": True}))
        self.assertNotIn("poe_watchdog", by)                          # already on
        self.assertEqual(by["telnet"]["change"], {"enabled": False})
        self.assertEqual(by["cable_test"]["change"], {"port": "0/3"})
        self.assertEqual(sum(1 for t in tips if t["feature"] == "cable_test"), 1)   # one per port
        self.assertEqual(by["eee"]["change"], {"enabled": False})     # EEE on and a port with errors
        self.assertEqual(by["dhcp_snooping"]["change"], {"enabled": True, "trusted": ["0/17", "0/18"]})
        storm = by["storm"]["change"]["ports"]
        self.assertNotIn("0/18", storm)                                # the router's port
        self.assertNotIn("0/17", storm)                                # an uplink
        self.assertIn("0/1", storm)
        self.assertEqual(by["sadp_agent"]["change"], {"enabled": False})
        self.assertIsNone(by["ntp"]["change"])
        self.assertEqual([t["level"] for t in tips], sorted((t["level"] for t in tips), key=["do", "consider", "info"].index))

    def test_nothing_left_to_do(self):
        a = self.sw.attrs
        a["STPManage/STPGlobalCfg"]["STPEnabled"] = True
        a["STPManage/PrivateLoopProtocolCfg"]["enabled"] = True
        a["SystemMaintenance/TelnetServerCfg"]["enabled"] = False
        a["SADPMgr/SADPCfg"]["SADPClient"]["enabled"] = False
        a["TimeMgr/SystemDateTime"].update(dateTimeType="NTP", time=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()))
        for x in self.sw.storm:
            x["enabled"] = True
        tips = hikswitch_adv.advice(self.read(), protected={}, uplinks={}, problems=[], snap_ports=[])
        self.assertEqual([t["feature"] for t in tips if t["level"] != "info"], [])


class RouteTest(unittest.TestCase):
    """The site route with the fake switch as a device: read + advice, a change,
    a refusal on the router's port, the confirm steps, and the audit trail."""
    KEY = "k" * 43

    def setUp(self):
        import creds
        import server
        import siteauth
        hikswitch._MODELS.clear()
        hikswitch._UNSUPPORTED.clear()
        hikswitch_adv._UNSUPPORTED.clear()
        self.server = server
        self.sw = FakeHikSwitch()
        self.addCleanup(self.sw.close)
        if os.path.exists(siteauth.AUTH_PATH):
            os.remove(siteauth.AUTH_PATH)
        siteauth._fails.clear()
        siteauth.set_hub_key(self.KEY)
        self.hdr = {siteauth.HEADER: self.KEY}
        self.lan = {"REMOTE_ADDR": "192.168.1.50"}
        sc = server.scanner
        self.key = "84:94:59:00:02:43"
        sc.devices = {self.key: {"key": self.key, "ip": self.sw.ip, "mac": self.key, "online": True, "category": "network",
                                 "model": "DS-3E1520P-EI", "ports": [80], "last_seen": int(time.time()),
                                 "sadp": {"model": "DS-3E1520P-EI", "http_port": 80, "mac": self.key}}}
        sc.registry = {self.key: {}}
        mock.patch.object(sc, "save_registry").start()
        mock.patch.object(sc, "trigger").start()
        self.manage = mock.patch.object(server, "_sw_enabled", return_value=True).start()
        mock.patch.object(switchmon, "protected_ports",
                          return_value={"0/18": "the site's router / internet is connected through this port"}).start()
        mock.patch.object(switchmon.monitor, "poll_round").start()
        self.addCleanup(mock.patch.stopall)
        server._SW_ADV.clear()
        creds.set_(self.key, "admin", PW)
        self.addCleanup(creds.set_, self.key, "", "")
        self.c = server.app.test_client()

    def get(self, q=""):
        return self.c.get(f"/api/devices/{self.key}/switch/advanced{q}", headers=self.hdr, environ_base=self.lan)

    def post(self, **body):
        return self.c.post(f"/api/devices/{self.key}/switch/advanced", headers=self.hdr, environ_base=self.lan, json=body)

    def test_read_then_cached(self):
        self.assertEqual(self.c.get(f"/api/devices/{self.key}/switch/advanced", environ_base=self.lan).status_code, 401)
        j = self.get().get_json()
        self.assertTrue(j["ok"], j)
        self.assertTrue(j["manage"])
        self.assertIn("loop_guard", {t["feature"] for t in j["advice"]})
        logins = self.sw.names().count("Login")
        self.assertTrue(self.get().get_json()["cached"])
        self.assertEqual(self.sw.names().count("Login"), logins)        # no second login within 5 min
        self.get("?refresh=1")
        self.assertEqual(self.sw.names().count("Login"), logins + 1)

    def test_change_confirm_refuse_and_audit(self):
        self.get()
        r = self.post(feature="lldp", enabled=False)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])
        j = r.get_json()
        self.assertFalse(j["after"]["enabled"])
        self.assertFalse(j["advanced"]["features"]["lldp"]["enabled"])  # the cached read follows

        j = self.post(feature="rstp", enabled=True).get_json()          # a whole-switch change asks first
        self.assertTrue(j["needs_confirm"])
        self.assertIn("30 seconds", j["warning"])
        self.assertEqual(sets(self.sw, "STPGlobalCfg"), [])
        self.assertTrue(self.post(feature="rstp", enabled=True, confirm=True).get_json()["ok"])

        r = self.post(feature="long_range", ports=["0/18"], enabled=True)   # the router's port
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.get_json()["protected"])
        self.assertEqual(sets(self.sw, "ModifyPortRemoteTrans"), [])
        j = self.post(feature="long_range", ports=["0/17"], enabled=True).get_json()
        self.assertTrue(j["needs_confirm"])                              # 9 devices behind it, and 10 Mbit/s
        self.assertIn("9 devices", j["warning"])
        self.assertIn("10 Mbit/s", j["warning"])

        j = self.post(feature="dhcp_snooping", enabled=True, trusted=[]).get_json()
        self.assertTrue(j["needs_confirm"])
        self.assertIn("port 18", j["warning"])                           # the router's port, added by Netwatch
        j = self.post(feature="dhcp_snooping", enabled=True, trusted=[], confirm=True).get_json()
        self.assertEqual(j["after"]["trusted"], ["0/18"])

        self.assertEqual(self.post(feature="storm", ports=["bad"], enabled=True).status_code, 400)
        self.assertEqual(self.post(feature="warp", enabled=True).status_code, 400)
        r = self.post(feature="rate_limit", ports=["0/5"], rx_mbps=99999)
        self.assertEqual(r.status_code, 400)                             # refused by the driver, not the switch

        self.manage.return_value = False
        self.assertEqual(self.post(feature="lldp", enabled=True).status_code, 403)

        import history
        evs = history.events(key=self.key, etype="switch", since=0, limit=50)
        acts = [(e["detail"].get("action"), e["detail"].get("result")) for e in evs]
        self.assertIn(("set-lldp", "ok"), acts)
        self.assertIn(("set-long_range", "fail"), acts)                  # the refusal is logged too


if __name__ == "__main__":
    unittest.main()
