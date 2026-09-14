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


if __name__ == "__main__":
    unittest.main()
