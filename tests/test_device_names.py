"""A device's own display name (scanner.set_device_name / _names_pass), and the
model/serial/firmware filled in for cameras without a login (_fill_meta).

Run inside the site image with a throwaway data dir:
  docker run --rm -v "$PWD/app:/app" -v "$PWD/tests:/tests" -e NETWATCH_DATA=/tmp/nw \
    --entrypoint python farm-netwatch:netcfg /tests/test_device_names.py -v
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import creds       # noqa: E402
import scanner     # noqa: E402

CAM = "aa:bb:cc:00:00:01"


class FilterTest(unittest.TestCase):
    def test_factory_defaults_are_not_names(self):
        for raw, model in [("IP CAMERA", "DS-2CD2143G2-I"), ("IPCamera", None),
                           ("Network Video Recorder", "DS-7608NI"), ("ubnt", ""),
                           ("LiteBeam 5AC Gen2", "LiteBeam 5AC Gen2"),
                           ("DS-2CD2143G2-I", "ds 2cd2143g2 i"), ("", None), (None, None),
                           ("AcuSense", "DS-7732NXI-I4/S"), ("Camera 01", None), ("D1", None)]:
            self.assertEqual(scanner._useful_device_name(raw, model), "", raw)

    def test_real_names_are_kept_and_tidied(self):
        self.assertEqual(scanner._useful_device_name("  Front   Gate ", "DS-2CD"), "Front Gate")
        self.assertEqual(scanner._useful_device_name("Dam-Pump PTP", "LiteBeam 5AC"), "Dam-Pump PTP")
        self.assertEqual(len(scanner._useful_device_name("x" * 200)), 64)


class OwnNameTest(unittest.TestCase):
    def test_osd_name_beats_the_default_device_name(self):
        self.assertEqual(scanner.hik_own_name({"deviceName": "IP CAMERA", "channelName": "Agter Kombuis"}),
                         "Agter Kombuis")
        self.assertEqual(scanner.hik_own_name({"deviceName": "Stoor", "channelName": "Camera 01"}), "Stoor")


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.s = scanner.Scanner()
        self.s.registry = {CAM: {"name": "My label"}}
        self.s.devices = {CAM: {"key": CAM, "ip": "192.168.1.64", "online": True,
                                "vendor": "Hangzhou Hikvision", "category": "camera",
                                "name": "My label"}}
        self.s.save_registry = mock.Mock()

    def test_stored_apart_from_the_operator_label(self):
        self.assertEqual(self.s.set_device_name(CAM, "Front Gate", model="DS-2CD"), "Front Gate")
        self.assertEqual(self.s.registry[CAM], {"name": "My label", "device_name": "Front Gate",
                                              "device_name_src": "device"})
        self.assertEqual(self.s.devices[CAM]["device_name"], "Front Gate")
        self.assertEqual(self.s.devices[CAM]["name"], "My label")

    def test_reset_to_default_clears_it(self):
        self.s.set_device_name(CAM, "Front Gate")
        self.s.save_registry.reset_mock()
        self.assertEqual(self.s.set_device_name(CAM, "IP CAMERA"), "")
        self.assertNotIn("device_name", self.s.registry[CAM])
        self.s.save_registry.assert_called_once()

    def test_unchanged_name_does_not_rewrite_the_registry(self):
        self.s.set_device_name(CAM, "Front Gate")
        self.s.save_registry.reset_mock()
        self.s.set_device_name(CAM, "Front Gate")
        self.s.save_registry.assert_not_called()

    def test_names_pass_reads_cameras_with_a_login_once(self):
        fetch = mock.Mock(return_value={"ok": True, "info": {"deviceName": "Kraal", "model": "DS-2CD"}})
        with mock.patch.object(creds, "keys_with_creds", return_value={CAM}), \
                mock.patch.object(creds, "get", return_value={"username": "admin", "password": "pw"}), \
                mock.patch.object(scanner.hikvision, "fetch", fetch):
            self.s._names_pass(dict(self.s.devices))
            self.s._names_pass(dict(self.s.devices))      # within NAME_REFRESH_S: skipped
        fetch.assert_called_once_with("192.168.1.64", "admin", "pw", timeout=5)
        self.assertEqual(self.s.devices[CAM]["device_name"], "Kraal")

    def test_nvr_channel_names_cameras_without_a_login(self):
        nvr, cam2 = "aa:bb:cc:00:00:09", "aa:bb:cc:00:00:02"
        self.s.devices.update({
            nvr: {"key": nvr, "ip": "192.168.1.249", "online": True, "category": "nvr",
                  "vendor": "Hangzhou Hikvision"},
            cam2: {"key": cam2, "ip": "192.168.1.65", "online": True, "category": "camera",
                   "vendor": "Hangzhou Hikvision"}})

        def fetch(ip, *a, **k):
            if ip == "192.168.1.249":
                return {"ok": True, "info": {"deviceName": "AcuSense", "deviceType": "NVR"}}
            return {"ok": True, "info": {"deviceName": "IP CAMERA"}}
        chans = [{"id": "1", "name": "Kraal", "ip": "192.168.1.64"},
                 {"id": "2", "name": "Dam", "ip": "192.168.1.65"},
                 {"id": "3", "name": "Camera 01", "ip": "192.168.1.66"}]
        with mock.patch.object(creds, "keys_with_creds", return_value={CAM, nvr}), \
                mock.patch.object(creds, "get", return_value={"username": "admin", "password": "pw"}), \
                mock.patch.object(scanner.hikvision, "fetch", side_effect=fetch), \
                mock.patch.object(scanner.hikvision, "nvr_channels", return_value=chans):
            self.s._names_pass(dict(self.s.devices))
        self.assertEqual(self.s.devices[CAM]["device_name"], "Kraal")       # own name is only a default
        self.assertEqual(self.s.devices[cam2]["device_name"], "Dam")        # no login at all
        self.assertEqual(self.s.devices[nvr]["device_name"], "")
        self.assertEqual(self.s.registry[cam2]["device_name_src"], "nvr")
        # The camera's next own-name read is still a default: the NVR's name stays.
        self.assertEqual(self.s.set_device_name(CAM, "IP CAMERA"), "Kraal")
        # A real name set on the camera itself replaces it, and the NVR can't take it back.
        self.assertEqual(self.s.set_device_name(CAM, "Front Kraal"), "Front Kraal")
        self.assertEqual(self.s.set_device_name(CAM, "Kraal", src="nvr"), "Front Kraal")

    def test_names_pass_skips_devices_without_a_login(self):
        fetch = mock.Mock()
        with mock.patch.object(creds, "keys_with_creds", return_value=set()), \
                mock.patch.object(scanner.hikvision, "fetch", fetch):
            self.s._names_pass(dict(self.s.devices))
        fetch.assert_not_called()


def ch(cid, name, ip, online):
    return {"id": cid, "name": name, "ip": "192.168.88" + ip, "online": online}


class NvrConflictTest(unittest.TestCase):
    """Two NVRs name one camera differently. Bennie, 2026-09-18: a recorder nobody
    maintains still listed its washbay channel at .9, where a PTZ is now, and
    whichever NVR answered first named the camera."""
    MAIN, OLD = "aa:bb:cc:00:00:0a", "aa:bb:cc:00:00:0b"
    PTZ, WASH = "aa:bb:cc:00:00:09", "aa:bb:cc:00:00:29"

    def setUp(self):
        self.s = scanner.Scanner()
        self.s.registry = {}
        self.s.save_registry = mock.Mock()
        hik = {"online": True, "vendor": "Hangzhou Hikvision"}
        self.s.devices = {
            self.MAIN: {"key": self.MAIN, "ip": "192.168.88.250", "category": "nvr", **hik},
            self.OLD: {"key": self.OLD, "ip": "192.168.88.252", "category": "nvr", **hik},
            self.PTZ: {"key": self.PTZ, "ip": "192.168.88.9", "category": "camera", **hik},
            self.WASH: {"key": self.WASH, "ip": "192.168.88.29", "category": "camera", **hik}}
        # the main recorder: current addresses, all connected
        self.main = [ch("7", "Washbay", ".29", True), ch("31", "PTZ in wildskamp hoek", ".9", True),
                     ch("1", "Kantoor Binne", ".2", True)]
        # the neglected one: still lists the washbay at .9, plus addresses nothing answers
        self.old = [ch("27", "Washbay", ".9", True), ch("5", "Bloemhof PTZ", ".100", False),
                    ch("6", "Skuur Binne", ".105", False), ch("8", "Skuur voor", ".103", False)]

    def names_pass(self, lists):
        with mock.patch.object(creds, "keys_with_creds", return_value={self.MAIN, self.OLD}), \
                mock.patch.object(creds, "get", return_value={"username": "admin", "password": "pw"}), \
                mock.patch.object(scanner.hikvision, "fetch",
                                  return_value={"ok": True, "info": {"deviceName": "AcuSense", "deviceType": "NVR"}}), \
                mock.patch.object(scanner.hikvision, "nvr_channels",
                                  side_effect=lambda ip, *a, **k: lists.get(ip, [])):
            self.s._names_pass(dict(self.s.devices))

    def name(self, key):
        return self.s.devices[key].get("device_name")

    def test_the_maintained_nvr_wins_whatever_the_order(self):
        both = {"192.168.88.250": self.main, "192.168.88.252": self.old}
        for lists in ({"m": ("192.168.88.250", self.main), "o": ("192.168.88.252", self.old)},
                      {"o": ("192.168.88.252", self.old), "m": ("192.168.88.250", self.main)}):
            self.assertEqual(scanner.nvr_names(lists)["192.168.88.9"], "PTZ in wildskamp hoek")
        self.names_pass(both)
        self.assertEqual(self.name(self.PTZ), "PTZ in wildskamp hoek")
        self.assertEqual(self.name(self.WASH), "Washbay")                    # the name is not handed out twice

    def test_a_wrong_nvr_name_is_put_right(self):
        self.s.set_device_name(self.PTZ, "Washbay", src="nvr")               # what the old rule left behind
        self.names_pass({"192.168.88.250": self.main, "192.168.88.252": self.old})
        self.assertEqual(self.name(self.PTZ), "PTZ in wildskamp hoek")

    def test_an_offline_channel_names_nothing(self):
        lists = {"o": ("192.168.88.252", [ch("5", "Bloemhof PTZ", ".9", False), ch("1", "Kraal", ".2", True)])}
        self.assertEqual(scanner.nvr_names(lists), {"192.168.88.2": "Kraal"})

    def test_a_quiet_nvr_keeps_its_say(self):
        self.names_pass({"192.168.88.250": self.main, "192.168.88.252": self.old})
        self.s._name_checked.clear()                                         # both due again…
        self.names_pass({"192.168.88.252": self.old})                        # …but the main one doesn't answer
        self.assertEqual(self.name(self.PTZ), "PTZ in wildskamp hoek")
        ts, ip, chans = self.s._nvr_lists[self.MAIN]
        self.s._nvr_lists[self.MAIN] = (ts - scanner.NVR_LIST_KEEP_S - 1, ip, chans)
        self.s._name_checked.clear()
        self.names_pass({"192.168.88.252": self.old})                        # gone for too long: no say
        self.assertEqual(self.name(self.PTZ), "Washbay")

    def test_connected_beats_unknown_then_address_then_first_channel(self):
        lists = {"a": ("192.168.88.250", [ch("3", "Unknown state", ".9", None)]),
                 "b": ("192.168.88.252", [ch("4", "Connected", ".9", True)])}
        self.assertEqual(scanner.nvr_names(lists)["192.168.88.9"], "Connected")
        lists = {"a": ("192.168.88.252", [ch("1", "From .252", ".9", True)]),
                 "b": ("192.168.88.250", [ch("2", "Lens 2", ".9", True), ch("1", "Lens 1", ".9", True)])}
        self.assertEqual(scanner.nvr_names(lists)["192.168.88.9"], "Lens 1")
        lists["a"][1].append(ch("9", "Camera 01", ".7", True))                # factory names never count
        self.assertNotIn("192.168.88.7", scanner.nvr_names(lists))


class NvrChannelsTest(unittest.TestCase):
    """hikvision.nvr_channels joins the channel list with the NVR's channel status,
    and keeps what the NVR says about the camera behind each channel."""
    LIST = ('<?xml version="1.0" encoding="UTF-8" ?>\n'
            '<InputProxyChannelList version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">\n'
            '<InputProxyChannel><id>1</id><name>Kraal </name><sourceInputPortDescriptor>'
            '<proxyProtocol>HIKVISION</proxyProtocol><ipAddress>192.168.1.64</ipAddress>'
            '<model>DS-2CD2322WD-I</model><serialNumber>DS-2CD2322WD-I20151013BBWR545974939</serialNumber>'
            '<firmwareVersion>V5.5.0 build 170725</firmwareVersion><deviceID></deviceID>'
            '</sourceInputPortDescriptor><devIndex>9662D98AC8384688</devIndex></InputProxyChannel>\n'
            '<InputProxyChannel><id>2</id><name>Dam</name><sourceInputPortDescriptor>'
            '<ipAddress>192.168.1.100</ipAddress></sourceInputPortDescriptor></InputProxyChannel>\n'
            '</InputProxyChannelList>')
    STATUS = ('<?xml version="1.0" encoding="UTF-8" ?>\n'
              '<InputProxyChannelStatusList version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">\n'
              '<InputProxyChannelStatus><id>1</id><sourceInputPortDescriptor><ipAddress>192.168.1.64'
              '</ipAddress></sourceInputPortDescriptor><online>true</online>'
              '<chanDetectResult>connect</chanDetectResult></InputProxyChannelStatus>\n'
              '<InputProxyChannelStatus><id>2</id><sourceInputPortDescriptor><ipAddress>192.168.1.100'
              '</ipAddress></sourceInputPortDescriptor><online>false</online>'
              '<chanDetectResult>notExist</chanDetectResult></InputProxyChannelStatus>\n'
              '</InputProxyChannelStatusList>')

    def get(self, status_code=200):
        def fake(url, **kw):
            if url.endswith("/channels"):
                return mock.Mock(status_code=200, text=self.LIST)
            return mock.Mock(status_code=status_code, text=self.STATUS)
        return fake

    def test_online_comes_from_the_status_list(self):
        with mock.patch.object(scanner.hikvision.requests, "get", side_effect=self.get()):
            chans = scanner.hikvision.nvr_channels("192.168.1.249", "admin", "pw")
        self.assertEqual(chans, [{"id": "1", "name": "Kraal", "ip": "192.168.1.64", "online": True,
                                  "model": "DS-2CD2322WD-I", "serial": "DS-2CD2322WD-I20151013BBWR545974939",
                                  "firmware": "V5.5.0 build 170725"},
                                 {"id": "2", "name": "Dam", "ip": "192.168.1.100", "online": False,
                                  "model": "", "serial": "", "firmware": ""}])

    def test_an_nvr_without_the_status_list_says_nothing(self):
        with mock.patch.object(scanner.hikvision.requests, "get", side_effect=self.get(404)):
            chans = scanner.hikvision.nvr_channels("192.168.1.249", "admin", "pw")
        self.assertEqual([c["online"] for c in chans], [None, None])


def dch(cid, ip, online, model="", serial="", firmware="", name="Camera 01"):
    """A channel with what its NVR says about the camera behind it."""
    return {"id": cid, "name": name, "ip": "192.168.88" + ip, "online": online,
            "model": model, "serial": serial, "firmware": firmware}


def fields(model, serial="", firmware=""):
    return {k: v for k, v in (("model", model), ("serial", serial), ("firmware", firmware)) if v}


# The home NVR's channels, 2026-09-19: 19 is a camera with no login and a factory
# channel name, 16 and 17 the two lenses of a thermal camera without a login, 2 a
# camera with one. The host names are the ones those cameras advertise.
DOME = ("DS-2CD2125FWD-I", "DS-2CD2125FWD-I20190919AAWRD64522222", "V5.6.2 build 190701")
THERMAL = ("DS-2TD2617-6/PA", "DS-2TD2617-6/PA20210526AAWRG08738380", "V5.5.48 build 220126")
KOMBUIS = ("DS-2CD2335FWD-I", "DS-2CD2335FWD-I20170816AAWR814829024", "V5.4.5 build 170124")
# Bennie .4's host name; B4 is what an NVR connected to it would say.
B4_HOST = "DS-2CD2T46G2P-ISU-SL20240617AAWRFG0192309"
B4 = ("DS-2CD2T46G2P-ISU/SL", "DS-2CD2T46G2P-ISU/SL20240617AAWRFG0192309", "V5.7.20 build 241120")


class NvrDetailsTest(unittest.TestCase):
    def test_only_channels_the_nvr_reports_online(self):
        lists = {"n": ("192.168.88.249", [dch("19", ".15", True, *DOME),       # factory name: still counts
                                          dch("20", ".40", None, *KOMBUIS),     # the NVR doesn't say
                                          dch("21", ".41", False, *KOMBUIS)])}  # not connected
        self.assertEqual(scanner.nvr_details(lists), {"192.168.88.15": ("n", fields(*DOME))})

    def test_the_nvr_trusted_for_names_is_trusted_for_details(self):
        # Bennie: both recorders say they're connected to .9; the neglected one's
        # other channels point at addresses where nothing answers.
        main = [dch("31", ".9", True, "DS-2DE4425IW-DE", "DS-2DE4425IW-DE20230410AAWRL12345678", "V5.7.3")]
        old = [dch("27", ".9", True, *B4), dch("5", ".100", False), dch("6", ".105", False)]
        for lists in ({"m": ("192.168.88.250", main), "o": ("192.168.88.252", old)},
                      {"o": ("192.168.88.252", old), "m": ("192.168.88.250", main)}):
            key, det = scanner.nvr_details(lists)["192.168.88.9"]
            self.assertEqual((key, det["model"]), ("m", "DS-2DE4425IW-DE"))


class HostnameTest(unittest.TestCase):
    """A Hikvision camera's host name is its serial number, with "/", "(" and ")"
    shown as "-"."""

    def test_kept_as_the_host_name_has_it_when_no_spelling_is_known(self):
        self.assertEqual(scanner.hik_hostname_ident(B4_HOST), {"model": "DS-2CD2T46G2P-ISU-SL", "serial": B4_HOST})

    def test_a_model_the_site_knows_puts_the_slash_and_brackets_back(self):
        # the home ANPR camera: iDS-2CD7A46G0/P-IZHS(SA) calls itself iDS-2CD7A46G0-P-IZHS-SA-2024…
        sp = scanner._model_spellings({"n": ("192.168.88.249", [dch("1", ".40", False, B4[0])])},
                                      {CAM: {"model": "iDS-2CD7A46G0/P-IZHS(SA)"}})
        self.assertEqual(scanner.hik_hostname_ident(B4_HOST + ".local", sp), fields(B4[0], B4[1]))
        self.assertEqual(scanner.hik_hostname_ident("iDS-2CD7A46G0-P-IZHS-SA-20240318AAWRFB7233024", sp),
                         fields("iDS-2CD7A46G0/P-IZHS(SA)", "iDS-2CD7A46G0/P-IZHS(SA)20240318AAWRFB7233024"))

    def test_two_models_that_fit_leave_it_as_it_is(self):
        sp = scanner._model_spellings({}, {"a": {"model": "DS-2CD2T46G2P-ISU/SL"},
                                           "b": {"model": "DS-2CD2T46G2P(ISU)SL"}})
        self.assertEqual(scanner.hik_hostname_ident(B4_HOST, sp)["model"], "DS-2CD2T46G2P-ISU-SL")

    def test_other_names_are_not_serial_numbers(self):
        for name in ["IP CAMERA", "HIKVISION DS-2DF8A442IXS-AEL - L31022674", "Z4SF-D", "", None,
                     B4_HOST + "-2",                                    # renamed after an mDNS name clash
                     "DS-2CD2T46G2P-ISU-SL20241317AAWRFG0192309",       # no month 13
                     "android-20240617abcdefghij"]:
            self.assertIsNone(scanner.hik_hostname_ident(name), name)

    def test_same_unit(self):
        self.assertTrue(scanner._same_unit(B4[1], B4_HOST))
        self.assertTrue(scanner._same_unit("FG0192309", B4_HOST))              # a short serial
        self.assertFalse(scanner._same_unit("FG0192316", B4_HOST))
        self.assertFalse(scanner._same_unit("9", B4_HOST))


class FillRulesTest(unittest.TestCase):
    """Who may fill a field over whom (Scanner.fill_device_meta)."""

    def test_better_sources_replace_weaker_ones_and_nobody_replaces_the_operator(self):
        s = scanner.Scanner()
        s.registry, s.devices, s.save_registry = {CAM: {}}, {}, mock.Mock()
        fill = s.fill_device_meta
        self.assertEqual(fill(CAM, {"model": "DS-1-SL"}, "hostname"), {"model": "DS-1-SL"})
        self.assertEqual(fill(CAM, {"model": "DS-1/SL"}, "nvr"), {"model": "DS-1/SL"})
        self.assertEqual(fill(CAM, {"model": "DS-1-SL"}, "hostname"), {})          # weaker than the NVR
        self.assertEqual(fill(CAM, {"model": "DS-2/SL"}, "nvr"), {"model": "DS-2/SL"})  # its own newer word
        self.assertEqual(s.registry[CAM]["meta_src"], {"model": "nvr"})
        self.assertEqual(fill(CAM, {"model": "DS-2/SL"}, "device"), {"model": "DS-2/SL"})  # the camera vouches
        self.assertEqual(s.registry[CAM], {"model": "DS-2/SL"})
        self.assertEqual(fill(CAM, {"model": "DS-3/SL"}, "nvr"), {})
        self.assertEqual(fill(CAM, {"model": "DS-3/SL"}, "device"), {})   # unmarked: it may be the operator's
        s.save_registry.reset_mock()
        self.assertEqual(fill(CAM, {"model": "DS-2/SL", "serial": "  ", "firmware": None}, "device"), {})
        s.save_registry.assert_not_called()


class FillTest(unittest.TestCase):
    """Model/serial/firmware for cameras without a login, in the names pass."""
    NVR, DOME_K, THERM_K, KOMB_K, B4_K = ("aa:bb:cc:00:02:49", "aa:bb:cc:00:00:15", "aa:bb:cc:00:00:17",
                                          "aa:bb:cc:00:00:03", "aa:bb:cc:00:00:04")

    def setUp(self):
        self.s = scanner.Scanner()
        self.s.registry, self.s.devices = {}, {}
        self.s.save_registry = mock.Mock()
        self.add(self.NVR, ".249", category="nvr")
        self.add(self.DOME_K, ".15", hostname=DOME[1])
        self.add(self.THERM_K, ".17", hostname="DS-2TD2617-6-PA20210526AAWRG08738380")
        self.add(self.KOMB_K, ".3", hostname=KOMBUIS[1])
        self.chans = [dch("2", ".3", True, *KOMBUIS, name="Agter Kombuis"),
                      dch("16", ".17", True, *THERMAL, name="Camera 8mm"),
                      dch("17", ".17", True, *THERMAL, name="Camera Thermal 6mm"),
                      dch("19", ".15", True, *DOME)]
        # what the Kombuis camera reads out with its own login: the firmware without the build
        self.own = {"192.168.88.3": {"deviceName": "IP CAMERA", "model": KOMBUIS[0],
                                     "serialNumber": KOMBUIS[1], "firmwareVersion": "V5.4.5"}}

    def add(self, key, ip, category="camera", **kw):
        self.s.devices[key] = {"key": key, "ip": "192.168.88" + ip, "online": True, "category": category,
                               "vendor": "Hangzhou Hikvision", "first_seen": 1, **kw}

    def names_pass(self, logins=()):
        """One pass after a scan. The NVR has a login, and so do the cameras in `logins`."""
        def fetch(ip, *a, **k):
            if ip == "192.168.88.249":
                return {"ok": True, "info": {"deviceName": "AcuSense", "deviceType": "NVR"}}
            return {"ok": True, "info": self.own.get(ip, {"deviceName": "IP CAMERA"})}
        with mock.patch.object(creds, "keys_with_creds", return_value={self.NVR, *logins}), \
                mock.patch.object(creds, "get", return_value={"username": "admin", "password": "pw"}), \
                mock.patch.object(scanner.hikvision, "fetch", side_effect=fetch), \
                mock.patch.object(scanner.hikvision, "nvr_channels", return_value=self.chans):
            self.s._names_pass(dict(self.s.devices))

    def again(self, logins=()):
        """The next pass that reads the NVR (and the cameras with a login) again."""
        self.s._name_checked.clear()
        self.names_pass(logins)

    def meta(self, key):
        """(fields, marks) as the registry has them."""
        r = self.s.registry.get(key, {})
        return fields(*(r.get(f, "") for f in ("model", "serial", "firmware"))), r.get("meta_src", {})

    NVR_MARKS = {"model": "nvr", "serial": "nvr", "firmware": "nvr"}

    def test_a_camera_without_a_login_gets_what_its_nvr_says(self):
        self.names_pass()
        self.assertEqual(self.meta(self.DOME_K), (fields(*DOME), self.NVR_MARKS))
        self.assertEqual(self.s.devices[self.DOME_K]["firmware"], DOME[2])      # the device list at once
        self.assertEqual(self.meta(self.THERM_K), (fields(*THERMAL), self.NVR_MARKS))   # "/PA", not "-PA"
        self.assertEqual(self.s.devices[self.DOME_K].get("device_name", ""), "")   # "Camera 01" is still no name

    def test_what_a_login_read_or_the_operator_typed_is_never_overwritten(self):
        self.s.registry[self.KOMB_K] = fields(KOMBUIS[0], KOMBUIS[1], "V5.4.5")   # a deep scan read it
        self.s.registry[self.DOME_K] = {"model": "Dome by the gate"}               # typed by the operator
        self.names_pass()
        self.assertEqual(self.meta(self.KOMB_K), (fields(KOMBUIS[0], KOMBUIS[1], "V5.4.5"), {}))
        self.assertEqual(self.meta(self.DOME_K), (fields("Dome by the gate", DOME[1], DOME[2]),
                                                  {"serial": "nvr", "firmware": "nvr"}))

    def test_the_camera_own_read_comes_first_and_takes_over_from_the_nvr(self):
        self.names_pass()                                   # no login yet: the NVR's word
        self.assertEqual(self.meta(self.KOMB_K), (fields(*KOMBUIS), self.NVR_MARKS))
        self.again(logins={self.KOMB_K})                    # a login is saved: its own read
        self.assertEqual(self.meta(self.KOMB_K), (fields(KOMBUIS[0], KOMBUIS[1], "V5.4.5"), {}))
        self.again()                                        # and the NVR can't take it back
        self.assertEqual(self.meta(self.KOMB_K), (fields(KOMBUIS[0], KOMBUIS[1], "V5.4.5"), {}))

    def test_the_host_name_is_only_the_fallback(self):
        self.add(self.B4_K, ".4", hostname=B4_HOST)
        self.names_pass()
        self.assertEqual(self.meta(self.B4_K), (fields("DS-2CD2T46G2P-ISU-SL", B4_HOST),
                                                {"model": "hostname", "serial": "hostname"}))
        self.assertEqual(self.meta(self.DOME_K)[1], self.NVR_MARKS)             # it has both: the NVR's
        self.chans.append(dch("40", ".4", True, *B4))       # an NVR connects to it and knows better
        self.again()
        self.assertEqual(self.meta(self.B4_K), (fields(*B4), self.NVR_MARKS))

    def test_the_host_name_is_spelt_like_a_model_the_site_knows(self):
        self.chans.append(dch("40", ".40", False, B4[0]))   # a channel that points elsewhere still spells it
        self.add(self.B4_K, ".4", hostname=B4_HOST)
        self.names_pass()
        self.assertEqual(self.meta(self.B4_K)[0], fields(B4[0], B4[1]))
        self.chans.pop()                                    # that channel is gone: the spelling stays
        self.s._nvr_lists.clear()
        self.s.save_registry.reset_mock()
        self.again()
        self.assertEqual(self.meta(self.B4_K)[0], fields(B4[0], B4[1]))
        self.s.save_registry.assert_not_called()

    def test_nothing_is_written_when_nothing_changed(self):
        self.add(self.B4_K, ".4", hostname=B4_HOST)
        self.names_pass()
        self.s.save_registry.reset_mock()
        self.names_pass()                                   # the NVR isn't due: its last list still counts
        self.again()                                        # read again: the same answers
        self.s.save_registry.assert_not_called()
        self.again(logins={self.KOMB_K})                    # its own read replaces the NVR's firmware...
        self.s.save_registry.reset_mock()
        self.again(logins={self.KOMB_K})                    # ...once
        self.s.save_registry.assert_not_called()

    def test_an_nvr_list_read_before_the_camera_showed_up_is_not_used(self):
        # The list has the unit that used to be at .4...
        self.chans.append(dch("40", ".4", True, B4[0], B4[1][:-2] + "16", B4[2]))
        self.names_pass()
        ts, ip, chans = self.s._nvr_lists[self.NVR]
        self.s._nvr_lists[self.NVR] = (ts - 60, ip, chans)
        self.add(self.B4_K, ".4", first_seen=time.time() - 30)   # ...then a new unit takes its place
        self.names_pass()                                   # the NVR isn't due: its list is older than the camera
        self.assertEqual(self.meta(self.B4_K), ({}, {}))
        self.chans[-1] = dch("40", ".4", True, *B4)
        self.again()                                        # read again, it describes the new unit
        self.assertEqual(self.meta(self.B4_K), (fields(*B4), self.NVR_MARKS))

    def test_an_nvr_the_camera_host_name_contradicts_is_not_used(self):
        self.chans.append(dch("40", ".4", True, B4[0], B4[1][:-2] + "16", B4[2]))
        self.add(self.B4_K, ".4", hostname=B4_HOST)
        self.names_pass()
        self.assertEqual(self.meta(self.B4_K), (fields(B4[0], B4[1]), {"model": "hostname", "serial": "hostname"}))

    def test_a_shared_address_an_offline_camera_and_an_nvr_host_name_are_left_alone(self):
        self.add("aa:bb:cc:00:00:99", ".15", category="unknown", vendor="Espressif")   # an IP clash at .15
        self.s.devices[self.THERM_K]["online"] = False
        self.s.devices[self.NVR]["hostname"] = "DS-7732NXI-I4-S-1620250731CCRRGD6530507WCVU"   # not model+date
        self.names_pass()
        for key in (self.DOME_K, self.THERM_K, self.NVR):
            self.assertEqual(self.meta(key), ({}, {}), key)

    def test_the_operator_form_keeps_the_mark_until_a_value_changes(self):
        self.names_pass()
        self.s.set_device_meta(self.DOME_K, name="Gate", serial=DOME[1], model=DOME[0])   # sent back unchanged
        self.assertEqual(self.meta(self.DOME_K)[1], self.NVR_MARKS)
        self.s.set_device_meta(self.DOME_K, model="DS-2CD2125FWD-IS")                     # a correction
        self.assertEqual(self.meta(self.DOME_K)[1], {"serial": "nvr", "firmware": "nvr"})
        self.again()
        self.assertEqual(self.meta(self.DOME_K)[0]["model"], "DS-2CD2125FWD-IS")

    def test_a_deep_scan_stores_the_firmware_and_clears_the_marks(self):
        self.names_pass()
        info = {"deviceName": "IP CAMERA", "model": DOME[0], "serialNumber": DOME[1], "firmwareVersion": "V5.6.2"}
        with mock.patch.object(creds, "get", return_value={"username": "admin", "password": "pw"}), \
                mock.patch.object(scanner.hikvision, "fetch", return_value={"ok": True, "info": info}):
            self.s._enrich_hik(dict(self.s.devices[self.DOME_K]))
        self.assertEqual(self.meta(self.DOME_K), (fields(DOME[0], DOME[1], "V5.6.2"), {}))


if __name__ == "__main__":
    unittest.main()
