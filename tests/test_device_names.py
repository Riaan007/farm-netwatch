"""A device's own display name (scanner.set_device_name / _names_pass).

Run inside the site image with a throwaway data dir:
  docker run --rm -v "$PWD/app:/app" -v "$PWD/tests:/tests" -e NETWATCH_DATA=/tmp/nw \
    --entrypoint python farm-netwatch:netcfg /tests/test_device_names.py -v
"""
import os
import sys
import tempfile
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
    """hikvision.nvr_channels joins the channel list with the NVR's channel status."""
    LIST = ('<?xml version="1.0" encoding="UTF-8" ?>\n'
            '<InputProxyChannelList version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">\n'
            '<InputProxyChannel><id>1</id><name>Kraal</name><sourceInputPortDescriptor>'
            '<proxyProtocol>HIKVISION</proxyProtocol><ipAddress>192.168.1.64</ipAddress>'
            '<model>DS-2CD2322WD-I</model></sourceInputPortDescriptor></InputProxyChannel>\n'
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
        self.assertEqual(chans, [{"id": "1", "name": "Kraal", "ip": "192.168.1.64", "online": True},
                                 {"id": "2", "name": "Dam", "ip": "192.168.1.100", "online": False}])

    def test_an_nvr_without_the_status_list_says_nothing(self):
        with mock.patch.object(scanner.hikvision.requests, "get", side_effect=self.get(404)):
            chans = scanner.hikvision.nvr_channels("192.168.1.249", "admin", "pw")
        self.assertEqual([c["online"] for c in chans], [None, None])


if __name__ == "__main__":
    unittest.main()
