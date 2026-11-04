"""Hikvision discovery (app/sadp.py) and what the site does with it: the answer
parser against hostile input, one discovery round against fake devices
(tests/sadp_fake.py), the scanner filling model/serial/firmware as source "sadp"
and filing each device in the category its model says (never over the
operator's), devices behind a proxy-ARP bridge, and the Cameras tab's recorder
and camera lists.

Run inside the site image (nothing leaves the box — the fake devices answer on
the loopback):
  docker run --rm --network none -v "$PWD/app:/app:ro" -v "$PWD/tests:/tests:ro" -e NETWATCH_DATA=/tmp/nw \\
    -e PYTHONDONTWRITEBYTECODE=1 --entrypoint python farm-netwatch:netcfg /tests/test_sadp.py -v
"""
import copy
import os
import socket
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import campics      # noqa: E402
import config       # noqa: E402
import credtest     # noqa: E402
import identify     # noqa: E402
import restart      # noqa: E402
import sadp         # noqa: E402
import scanner      # noqa: E402
from sadp_fake import Responder, Unit, probe_match   # noqa: E402

LO = ["127.0.0.1"]
HIK = "Hangzhou Hikvision Digital Technology Co.,Ltd."
UBNT = "Ubiquiti Inc"
BRIDGE_MAC = "02:00:00:00:0b:01"          # a made-up proxy-ARP bridge in front of .2 and .60
# Made-up units: (ip, SADP MAC, model, serial, firmware as SADP sends it)
NVR = ("127.0.0.2", "02-00-00-00-00-02", "DS-7608NI-K2", "DS-7608NI-K20820200101CCRRTEST0000002WCVU", "V3.4.104build 190128")
SWITCH = ("127.0.0.41", "02-00-00-00-00-41", "DS-3E1309P-EI", "DS-3E1309P-EI20210101TEST041", "V1.2.0build 210616")
TERMINAL = ("127.0.0.60", "02-00-00-00-00-60", "DS-K1T342MFWX-E1", "DS-K1T342MFWX-E120250101V043800ENTEST0060", "V4.38.0build 250307")
STATION = ("127.0.0.62", "02-00-00-00-00-62", "DS-KH6350-WTE1", "DS-KH6350-WTE10120250101TEST0062", "V2.2.100build 250114")
CAMERA = ("127.0.0.94", "02-00-00-00-00-94", "DS-2CD2346G2H-IU", "DS-2CD2346G2H-IU20240101AAWRTEST0094", "V5.7.19build 241207")


def unit(u, **kw):
    return Unit(u[0], u[1], u[2], u[3], u[4], **kw)


def colon(mac):
    return mac.replace("-", ":")


def answer(u, **kw):
    """What sadp.parse() makes of unit u's answer."""
    a = {"ip": u[0], "mac": colon(u[1]), "model": u[2], "serial": u[3],
         "firmware": u[4].replace("build", " build").replace("  ", " "), "dhcp": False,
         "gateway": "127.0.0.254", "http_port": 80, "command_port": 8000, "activated": True}
    a.update(kw)
    return a


class KindTest(unittest.TestCase):
    """identify.hik_kind: what a Hikvision model says a device is."""

    def test_each_family(self):
        for model, cat in [("DS-K1T342MFWX-E1", "access"), ("DS-K1A802AEF", "access"),
                           ("DS-KH6350-WTE1", "intercom"), ("DS-KD8003-IME1", "intercom"),
                           ("DS-KV6113-WPE1", "intercom"), ("DS-KB6003-WIP", "intercom"),
                           ("DS-3E1309P-EI", "network"), ("DS-3E1520P-EI", "network"),
                           ("DS-7608NI-K2", "nvr"), ("DS-7732NXI-I4/4S", "nvr"), ("DS-7732NI-I4(B)", "nvr"),
                           ("DS-8632NI-K8", "nvr"), ("DS-9664NI-I8", "nvr"), ("iDS-7204HUHI-M1/S", "nvr"),
                           ("iDS-9632NXI-I8/16S", "nvr"), ("DS-2CD2346G2H-IU", "camera"),
                           ("iDS-2CD7A46G0/P-IZHS(SA)", "camera"), ("DS-2DE4425IW-DE(S6)", "camera"),
                           ("DS-2SE7C425MWG-EB/26", "camera"), ("DS-2TD2617-6/PA", "camera"),
                           ("DS-2PT3122IZ-DE3", "camera"), (" ds-2cd2043g2-i ", "camera")]:
            self.assertEqual((identify.hik_kind(model) or (None,))[0], cat, model)

    def test_anything_else_says_nothing(self):
        for model in ["", None, 5, ["DS-2CD"], "DS-6704HUHI", "DS-1280ZJ-DM18", "DS-D5022QE-B",
                      "DS920+", "NanoBeam M5 16", "LiteBeam 5AC", "XDS-2CD2346", "DS-2CE16D0T"]:
            self.assertIsNone(identify.hik_kind(model), repr(model))

    def test_only_known_categories(self):
        self.assertLessEqual({c for _rx, c, _l in identify._HIK_FAMILIES}, identify.CATEGORIES)

    def test_discovery_model_first(self):
        self.assertEqual(identify.hik_kind_of({"sadp": {"model": NVR[2]}, "model": "DS-2CD2346G2H-IU"})[0], "nvr")
        self.assertEqual(identify.hik_kind_of({"sadp": {"model": "Thing"}, "model": NVR[2]})[0], "nvr")
        self.assertIsNone(identify.hik_kind_of({"sadp": "junk", "model": None}))
        self.assertIsNone(identify.hik_kind_of(None))


class ParseTest(unittest.TestCase):
    """sadp.parse: one datagram, from a device nobody vouches for."""

    def test_a_real_shaped_answer(self):
        a = sadp.parse(probe_match(*NVR, dhcp="true", activated="false", channels="8"))
        self.assertEqual(a, answer(NVR, dhcp=True, activated=False))
        self.assertEqual(a["firmware"], "V3.4.104 build 190128")      # spelt like an NVR channel list does
        self.assertEqual(sadp.parse(probe_match(*SWITCH, http=""))["http_port"], None)   # no web page

    def test_firmware_spelling(self):
        for raw, want in [("V4.1.18build 200120", "V4.1.18 build 200120"),
                          ("V3.3.0 build 251010", "V3.3.0 build 251010"), ("V5.5.0", "V5.5.0")]:
            self.assertEqual(sadp._firmware(raw), want)

    def test_doctype_and_entities_are_refused(self):
        bomb = (b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;">]>'
                b"<ProbeMatch><IPv4Address>127.0.0.2</IPv4Address><MAC>02-00-00-00-00-02</MAC>"
                b"<DeviceDescription>&b;</DeviceDescription></ProbeMatch>")
        self.assertIsNone(sadp.parse(bomb))
        self.assertIsNone(sadp.parse(probe_match(*NVR).replace(b"<ProbeMatch>", b'<!ENTITY x "y"><ProbeMatch>')))

    def test_size_and_shape(self):
        self.assertIsNone(sadp.parse(probe_match(*NVR) + b" " * sadp.MAX_DGRAM))
        self.assertIsNone(sadp.parse(b""))
        self.assertIsNone(sadp.parse(b"not xml at all"))
        self.assertIsNone(sadp.parse(probe_match(*NVR).replace(b"ProbeMatch>", b"Probe>")))
        self.assertIsNone(sadp.parse(probe_match("999.1.1.1", *NVR[1:])))
        self.assertIsNone(sadp.parse(probe_match("239.255.255.250", *NVR[1:])))
        self.assertIsNone(sadp.parse(probe_match(NVR[0], "zz-zz", *NVR[2:])))
        ns = probe_match(*NVR).replace(b"<ProbeMatch>", b'<ProbeMatch xmlns="urn:x">')
        self.assertEqual(sadp.parse(ns)["model"], NVR[2])

    def test_every_field_is_checked(self):
        a = sadp.parse(probe_match(NVR[0], NVR[1], "<script>alert(1)</script>", "S" * 65, "V1 & co",
                                   dhcp="maybe", gateway="gw", http="99999", command="80a", activated="1"))
        self.assertEqual({k: a[k] for k in ("model", "serial", "firmware", "dhcp", "gateway",
                                            "http_port", "command_port", "activated")},
                         {"model": "", "serial": "", "firmware": "", "dhcp": None, "gateway": "",
                          "http_port": None, "command_port": None, "activated": None})
        self.assertEqual(sadp.parse(probe_match(NVR[0], NVR[1], "  DS-7608NI-K2\n ", *NVR[3:]))["model"], NVR[2])
        self.assertIsNone(sadp.parse(probe_match(NVR[0], NVR[1], "DS-7608\x07", *NVR[3:])))   # not even XML


class RoundTest(unittest.TestCase):
    """sadp.discover against fake units on the loopback."""

    def setUp(self):
        self.r = None

    def tearDown(self):
        if self.r:
            self.r.stop()

    def run_round(self, *units, wait=0.8, **kw):
        self.r = Responder(*units).start()
        return sadp.discover(LO, wait=wait, **kw)

    def test_every_unit_answers_with_what_it_is(self):
        res = self.run_round(unit(NVR), unit(SWITCH), unit(TERMINAL), unit(STATION, dhcp="true"))
        self.assertTrue(res["bound"])
        self.assertEqual(res["addrs"], LO)
        self.assertEqual(res["note"], "")
        self.assertEqual(res["answers"], {NVR[0]: answer(NVR), SWITCH[0]: answer(SWITCH),
                                          TERMINAL[0]: answer(TERMINAL), STATION[0]: answer(STATION, dhcp=True)})

    def test_it_only_ever_sends_the_inquiry(self):
        self.run_round(unit(NVR))
        sent = [d for d in self.r.heard if b"<ProbeMatch>" not in d]
        self.assertTrue(sent)
        for d in sent:            # the inquiry, nothing that changes a device (activate, set IP, reset)
            self.assertRegex(d, rb"^<\?xml version=\"1.0\" encoding=\"utf-8\"\?><Probe><Uuid>[0-9A-F-]{36}</Uuid>"
                                rb"<Types>inquiry</Types></Probe>$")

    def test_a_unit_that_missed_the_first_inquiry_hears_the_second(self):
        with mock.patch.object(sadp, "RESEND_S", 0.2):
            res = self.run_round(unit(NVR, from_inquiry=2))
        self.assertEqual(list(res["answers"]), [NVR[0]])
        self.assertEqual(self.r.inquiries, 2)

    def test_repeats_count_once_and_a_clash_counts_for_nobody(self):
        twin = Unit(SWITCH[0], "02-00-00-00-99-99", "DS-2CD2346G2H-IU", "X", "V1")   # another unit claims .41
        res = self.run_round(unit(NVR, repeat=3), unit(SWITCH), twin)
        self.assertEqual(list(res["answers"]), [NVR[0]])
        self.assertEqual(res["clashes"], {SWITCH[0]: [colon(SWITCH[1]), "02:00:00:00:99:99"]})

    def test_an_answer_from_another_address_than_it_claims_is_dropped(self):
        res = self.run_round(unit(NVR), unit(SWITCH, src="127.0.0.42"))
        self.assertEqual(list(res["answers"]), [NVR[0]])
        self.assertEqual(res["dropped"]["moved"], 1)

    def test_hostile_answers_are_dropped_and_the_round_carries_on(self):
        big = probe_match(*CAMERA) + b"<!--" + b"x" * sadp.MAX_DGRAM + b"-->"
        res = self.run_round(unit(NVR), Unit("127.0.0.70", "", "", raw=big),
                             Unit("127.0.0.71", "", "", raw=b"<ProbeMatch><!DOCTYPE x></ProbeMatch>"),
                             Unit("127.0.0.72", "", "", raw=b"<ProbeMatch>half"))
        self.assertEqual(list(res["answers"]), [NVR[0]])
        self.assertEqual(res["dropped"], {"too_big": 1, "bad": 2, "moved": 0})

    def test_a_flood_is_cut_short(self):
        with mock.patch.object(sadp, "MAX_PACKETS", 20), mock.patch.object(sadp, "MAX_DEVICES", 1):
            res = self.run_round(unit(NVR, repeat=40), unit(SWITCH, repeat=40), wait=2.0)
        self.assertEqual(len(res["answers"]), 1)
        self.assertIn("stopped after 20 packets", res["note"])

    def test_the_port_taken_by_someone_else(self):
        hog = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        hog.bind(("", sadp.PORT))          # no SO_REUSEADDR: nobody may share it
        try:
            res = sadp.discover(LO, wait=0.3)
        finally:
            hog.close()
        self.assertFalse(res["bound"])
        self.assertIn("UDP 37020 is in use", res["note"])

    def test_nowhere_to_ask(self):
        self.assertEqual(sadp.discover([], wait=0.1)["note"], "no LAN address on a local scan target")
        res = sadp.discover(["192.0.2.1"], wait=0.1)          # not an address of this box
        self.assertEqual(res["addrs"], [])
        self.assertIn("could not listen", res["note"])

    def test_lan_addresses_follow_the_scan_targets(self):
        self.assertEqual(sadp.lan_addresses(["127.0.0.0/8"]), [])        # loopback is never a LAN
        out = "1: lo    inet 127.0.0.1/8 scope host lo\n2: eth0    inet 10.9.8.7/24 brd 10.9.8.255 scope global eth0\n" \
              "3: wg0    inet 10.99.0.9/24 scope global wg0\n"
        with mock.patch.object(sadp.subprocess, "run", return_value=mock.Mock(stdout=out)):
            self.assertEqual(sadp.lan_addresses(["10.9.8.0/24"]), ["10.9.8.7"])
            self.assertEqual(sadp.lan_addresses(["10.9.0.0/16", "bad", None]), ["10.9.8.7"])
            self.assertEqual(sadp.lan_addresses([]), [])


class ScannerTest(unittest.TestCase):
    """Scanner._sadp_pass / _sadp_apply: what an answer may change, and where."""
    SWITCH_K, TERM_K, CAM_K = colon(SWITCH[1]), colon(TERMINAL[1]), colon(CAMERA[1])

    def setUp(self):
        self.s = scanner.Scanner()
        self.s.registry, self.s.devices = {}, {}
        self.s.save_registry = mock.Mock()
        # .2 and .60 sit behind a marked bridge: keyed by address, the bridge's MAC and vendor
        self.add(NVR[0], NVR[0], BRIDGE_MAC, UBNT, "camera", ports=[554])
        self.add(TERMINAL[0], TERMINAL[0], BRIDGE_MAC, UBNT, "camera", ports=[80, 443, 554, 8000])
        # the rest have their own MAC; the scan guessed the switch (port 8000 only) to be an NVR
        self.add(self.SWITCH_K, SWITCH[0], self.SWITCH_K, HIK, "nvr", ports=[8000])
        self.add(self.CAM_K, CAMERA[0], self.CAM_K, HIK, "camera", ports=[80, 554, 8000])
        self.round = {"ts": time.time(), "bound": True, "addrs": LO, "note": "", "clashes": {},
                      "answers": {u[0]: answer(u) for u in (NVR, TERMINAL, SWITCH, CAMERA)}}

    def add(self, key, ip, mac, vendor, category, **kw):
        self.s.devices[key] = {"key": key, "ip": ip, "mac": mac, "vendor": vendor, "online": True,
                               "category": category, "type": "x", "first_seen": 1, **kw}

    def meta(self, key):
        r = self.s.registry.get(key, {})
        return {f: r[f] for f in ("model", "serial", "firmware") if r.get(f)}, r.get("meta_src", {})

    SADP_MARKS = {"model": "sadp", "serial": "sadp", "firmware": "sadp"}

    def apply(self, rnd=None):
        return self.s._sadp_apply(dict(self.s.devices), rnd or self.round)

    def test_what_each_device_says_it_is(self):
        self.assertEqual(set(self.apply()), {NVR[0], TERMINAL[0], self.SWITCH_K, self.CAM_K})
        self.assertEqual(self.meta(NVR[0]), ({"model": NVR[2], "serial": NVR[3], "firmware": "V3.4.104 build 190128"},
                                              self.SADP_MARKS))
        d = self.s.devices
        self.assertEqual((d[NVR[0]]["category"], d[NVR[0]]["type"]), ("nvr", "NVR / Recorder"))
        self.assertEqual((d[self.SWITCH_K]["category"], d[self.SWITCH_K]["type"]), ("network", "Network switch"))
        self.assertEqual((d[TERMINAL[0]]["category"], d[TERMINAL[0]]["type"]), ("access", "Access terminal"))
        self.assertEqual(d[self.CAM_K]["category"], "camera")
        self.assertEqual(d[NVR[0]]["model"], NVR[2])                     # the device list at once

    def test_behind_a_bridge_the_real_mac_is_kept_and_the_key_stays(self):
        self.apply()
        self.assertEqual(self.s.devices[NVR[0]]["sadp"]["mac"], colon(NVR[1]))
        self.assertEqual(self.s.devices[NVR[0]]["mac"], BRIDGE_MAC)
        self.assertNotIn(colon(NVR[1]), self.s.registry)
        self.assertNotIn(colon(NVR[1]), self.s.devices)
        self.assertEqual(self.s.devices[NVR[0]]["sadp"]["dhcp"], False)

    def test_an_answer_another_unit_could_have_sent_is_not_used(self):
        self.round["answers"][CAMERA[0]] = answer(CAMERA, mac="02:00:00:00:77:77")   # not this camera's MAC
        self.add("02:00:00:00:00:61", "127.0.0.61", "02:00:00:00:00:61", "Espressif", "iot")
        self.add("02:00:00:00:00:91", TERMINAL[0], "02:00:00:00:00:91", "Espressif", "iot")   # shares .60
        self.round["answers"]["127.0.0.61"] = answer(TERMINAL, ip="127.0.0.61", mac="02:00:00:00:00:61")
        self.s.devices[self.SWITCH_K]["online"] = False
        matched = self.apply()
        self.assertEqual(set(matched), {NVR[0], "02:00:00:00:00:61"})
        for key in (self.CAM_K, TERMINAL[0], self.SWITCH_K):
            self.assertEqual(self.meta(key), ({}, {}), key)

    def test_the_operator_category_and_values_stay(self):
        self.s.registry[TERMINAL[0]] = {"category": "camera", "model": "Front door reader"}
        self.s.registry[self.SWITCH_K] = {"category": "nvr", "type": "My label"}
        self.s.devices[TERMINAL[0]]["category"] = "camera"
        self.apply()
        self.assertEqual(self.s.devices[TERMINAL[0]]["category"], "camera")
        self.assertEqual(self.meta(TERMINAL[0]), ({"model": "Front door reader", "serial": TERMINAL[3],
                                                   "firmware": "V4.38.0 build 250307"},
                                                  {"serial": "sadp", "firmware": "sadp"}))
        self.assertEqual(self.s.devices[self.SWITCH_K]["category"], "nvr")
        self.assertEqual(self.s.devices[TERMINAL[0]]["sadp"]["model"], TERMINAL[2])
        # "Detected as" still says what it is (as the next scan's record will), and a typed label stays
        self.assertEqual(self.s.devices[TERMINAL[0]]["type"], "Access terminal")
        self.assertEqual(self.s.devices[self.SWITCH_K]["type"], "My label")

    def test_precedence_login_over_discovery_over_nvr_over_host_name(self):
        fill = self.s.fill_device_meta
        k = self.CAM_K
        self.assertTrue(fill(k, {"model": "DS-2CD2346G2H-IU", "serial": "from-the-name"}, "hostname"))
        self.assertTrue(fill(k, {"serial": "from-the-nvr"}, "nvr"))
        self.apply()
        self.assertEqual(self.meta(k), ({"model": CAMERA[2], "serial": CAMERA[3], "firmware": "V5.7.19 build 241207"},
                                        self.SADP_MARKS))
        self.assertEqual(fill(k, {"serial": "from-the-nvr"}, "nvr"), {})               # weaker than discovery
        self.assertEqual(fill(k, {"serial": "from-the-name"}, "hostname"), {})
        self.assertEqual(fill(k, {"firmware": "V5.7.19"}, "device"), {"firmware": "V5.7.19"})   # its login
        self.assertEqual(self.meta(k)[1], {"model": "sadp", "serial": "sadp"})
        self.apply()
        self.assertEqual(self.meta(k)[0]["firmware"], "V5.7.19")                     # login stays

    def test_a_new_unit_behind_the_bridge_replaces_the_old_answer(self):
        self.apply()
        other = answer(NVR, mac="02:00:00:00:02:22", serial="DS-7608NI-K20820210101CCRRTEST0000222WCVU")
        self.round["answers"][NVR[0]] = other
        self.apply()
        self.assertEqual(self.meta(NVR[0])[0]["serial"], other["serial"])
        self.assertEqual(self.s.devices[NVR[0]]["sadp"]["mac"], "02:00:00:00:02:22")

    def test_nothing_is_written_when_nothing_changed(self):
        self.apply()
        self.s.save_registry.reset_mock()
        self.apply()
        self.round["ts"] = time.time()
        self.apply()
        self.s.save_registry.assert_not_called()

    def test_the_next_scan_files_it_from_the_saved_model(self):
        self.apply()
        h = {"ip": SWITCH[0], "mac": self.SWITCH_K, "nmap_vendor": HIK, "hostname": "", "ports": [8000],
             "services": {}, "os": "", "rtt": 1.0}
        fresh = scanner.Scanner()                     # a restart: no discovery heard yet
        fresh.registry = copy.deepcopy(self.s.registry)
        with mock.patch.object(identify, "http_banner", return_value={}), \
                mock.patch.object(identify, "reverse_dns", return_value=""):
            rec = fresh._build_record(h, "127.0.0.0/24", True, False, False, self.SWITCH_K)
            self.assertEqual((rec["category"], rec["type"], rec["confidence"]), ("network", "Network switch", "high"))
            self.assertNotIn("sadp", rec)
            rec = self.s._build_record(h, "127.0.0.0/24", True, False, False, self.SWITCH_K)
            self.assertEqual(rec["sadp"]["model"], SWITCH[2])        # what discovery heard rides along
            fresh.registry[self.SWITCH_K]["category"] = "printer"    # the operator's word wins
            self.assertEqual(fresh._build_record(h, "127.0.0.0/24", True, False, False, self.SWITCH_K)["category"],
                             "printer")
        self.assertEqual(identify.classify(HIK, [8000], {}, "")[0], "nvr")   # what the guess alone says

    def test_a_partial_round_keeps_what_earlier_rounds_heard(self):
        self.apply()
        self.apply({"ts": time.time(), "bound": False, "addrs": LO, "answers": {}})
        self.assertIsNotNone(self.s._sadp_heard(NVR[0]))
        self.apply({"ts": time.time(), "bound": True, "addrs": LO, "answers": {}})   # a whole round without it
        self.assertIsNone(self.s._sadp_heard(NVR[0]))
        self.apply()
        with mock.patch.object(scanner.time, "time", return_value=time.time() + scanner.SADP_KEEP_S + 1):
            self.assertIsNone(self.s._sadp_heard(NVR[0]))

    def test_rounds_are_spaced_and_can_be_switched_off(self):
        calls = []

        def fake(addrs):
            calls.append(addrs)
            return dict(self.round, ts=time.time())
        with mock.patch.object(scanner.sadp, "discover", side_effect=fake), \
                mock.patch.object(scanner.sadp, "lan_addresses", return_value=LO):
            self.s._sadp_pass(dict(self.s.devices))
            self.s._sadp_pass(dict(self.s.devices))          # a targeted scan right after: same round
            self.assertEqual(len(calls), 1)
            self.assertEqual(self.s.devices[NVR[0]]["category"], "nvr")
            cfg = copy.deepcopy(config.load())
            cfg["scan"]["sadp"] = False
            with mock.patch.object(scanner.config, "load", return_value=cfg):
                self.s._sadp_round = None
                self.s._sadp_pass(dict(self.s.devices))
            self.assertEqual(len(calls), 1)
            self.assertEqual(self.s._sadp_by_key, {})

    def test_a_broken_round_never_breaks_the_scan(self):
        with mock.patch.object(scanner.sadp, "discover", side_effect=RuntimeError("boom")), \
                mock.patch.object(scanner.sadp, "lan_addresses", return_value=LO):
            self.s._sadp_pass(dict(self.s.devices))           # logged, not raised

    def test_end_to_end_on_the_loopback(self):
        r = Responder(unit(NVR), unit(SWITCH), unit(TERMINAL)).start()
        try:
            with mock.patch.object(scanner.sadp, "lan_addresses", return_value=LO), \
                    mock.patch.object(sadp, "WAIT_S", 0.8):
                self.s._sadp_pass(dict(self.s.devices))
        finally:
            r.stop()
        self.assertEqual({k: self.s.devices[k]["category"] for k in (NVR[0], TERMINAL[0], self.SWITCH_K, self.CAM_K)},
                         {NVR[0]: "nvr", TERMINAL[0]: "access", self.SWITCH_K: "network", self.CAM_K: "camera"})
        self.assertEqual(self.meta(self.SWITCH_K)[1], self.SADP_MARKS)

    def test_behind_the_bridge_it_is_still_hikvision(self):
        self.apply()
        term = self.s.devices[TERMINAL[0]]
        self.assertTrue(scanner._is_hik(term))
        self.assertIn("hikvision", credtest.plan(term, [80, 443, 554, 8000]))
        self.assertEqual(restart.method_for(term)["id"], "hikvision")
        bare = {"vendor": UBNT, "category": "access", "ports": [22, 80]}         # a radio's vendor, no evidence
        self.assertNotIn("hikvision", credtest.plan(bare, [22, 80]))


class CamerasTest(unittest.TestCase):
    """campics: the recorders and cameras the Cameras tab lists follow the model."""

    def inventory(self, devs, lists=None):
        e = campics.Engine()
        e.start(lambda: devs, lambda: {}, lambda: lists or {}, thread=False, cfg=lambda: {})
        return e.inventory(force=True)

    def dev(self, key, u, category, **kw):
        return {"key": key, "ip": u[0], "mac": key, "vendor": HIK, "online": True, "category": category,
                "ports": [80, 554, 8000], "model": "", **kw}

    def test_switch_is_never_a_recorder_and_an_nvr_filed_as_a_camera_is_one(self):
        devs = [self.dev("k-switch", SWITCH, "nvr", ports=[8000], sadp=answer(SWITCH)),
                self.dev("k-nvr", NVR, "camera", sadp=answer(NVR)),            # e.g. an old saved category
                self.dev("k-cam", CAMERA, "camera", model=CAMERA[2])]
        inv = self.inventory(devs)
        self.assertEqual([r["key"] for r in inv["recorders"]], ["k-nvr"])
        self.assertEqual([c["key"] for c in inv["cameras"]], ["k-cam"])

    def test_a_terminal_is_a_camera_only_once_an_nvr_records_it(self):
        term = self.dev("k-term", TERMINAL, "access", model=TERMINAL[2])
        sw = self.dev("k-switch", SWITCH, "network", model=SWITCH[2])
        self.assertEqual(self.inventory([term, sw])["cameras"], [])
        lists = {"k-nvr": (time.time(), NVR[0], [{"id": "1", "name": "Door", "ip": TERMINAL[0], "online": True},
                                                 {"id": "2", "name": "?", "ip": SWITCH[0], "online": True}])}
        inv = self.inventory([term, sw, self.dev("k-nvr", NVR, "nvr", model=NVR[2])], lists)
        self.assertEqual([(c["kind"], c["key"]) for c in inv["cameras"]],
                         [("camera", "k-term"), ("channel", "ch:k-nvr:2")])     # the switch stays a switch

    def test_no_model_keeps_the_old_rules(self):
        devs = [self.dev("k-a", NVR, "nvr"), self.dev("k-b", CAMERA, "unknown", vendor="", ports=[554, 8000])]
        inv = self.inventory(devs)
        self.assertEqual([r["key"] for r in inv["recorders"]], ["k-a"])
        self.assertEqual([c["key"] for c in inv["cameras"]], ["k-b"])


if __name__ == "__main__":
    unittest.main()
