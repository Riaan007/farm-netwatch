"""Same-MAC problem names the device itself (app/ubnt.py + Scanner._find_mac_owners):
reading a Ubiquiti discovery reply, and the problem card's owner / detail / fix.

Run inside the site image:
  docker run --rm -v "$PWD/app:/app" -v "$PWD/tests:/tests" -e NETWATCH_DATA=/tmp/nw \
    --entrypoint python farm-netwatch:netcfg /tests/test_bridge_owner.py -v
"""
import os
import struct
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import scanner   # noqa: E402
import tunnels   # noqa: E402
import ubnt      # noqa: E402

BRIDGE = "e063da409178"
IPS = ["192.168.1.10", "192.168.1.151", "192.168.1.166"]


def tlv(t, v):
    return bytes([t]) + struct.pack(">H", len(v)) + v


def reply(mac=BRIDGE, ip="192.168.1.151", host=b"Pole-3 Station", model=b"LiteBeam 5AC Gen2"):
    """A v1 discovery reply, laid out like a real airOS one."""
    m = bytes.fromhex(mac)
    body = (tlv(0x02, m + bytes(int(o) for o in ip.split(".")))
            + tlv(0x01, m) + tlv(0x0A, struct.pack(">I", 3600))
            + tlv(0x0B, host) + tlv(0x0C, b"LBE-5AC-Gen2") + tlv(0x03, b"WA.ar934x.v8.7.11")
            + tlv(0x0D, b"TriLectro-PTP") + tlv(0x14, model))
    return b"\x01\x00" + struct.pack(">H", len(body)) + body


class ParseTest(unittest.TestCase):
    def test_reads_every_field(self):
        r = ubnt.parse(reply())
        self.assertEqual(r["mac"], BRIDGE)
        self.assertEqual(r["ips"], ["192.168.1.151"])
        self.assertEqual(r["hostname"], "Pole-3 Station")
        self.assertEqual(r["model"], "LiteBeam 5AC Gen2")
        self.assertEqual(r["essid"], "TriLectro-PTP")
        self.assertEqual(r["uptime_s"], 3600)

    def test_rejects_junk(self):
        for junk in (b"", b"\x01\x00\x00\x00", b"\x00\x00\x00\x00", b"\x01\x00\x00\x09\x02\x00\x40"):
            self.assertIsNone(ubnt.parse(junk))

    def test_truncated_tlv_does_not_crash(self):
        self.assertEqual(ubnt.parse(reply()[:40])["mac"], BRIDGE)


class OwnerTest(unittest.TestCase):
    def setUp(self):
        self.s = scanner.Scanner()
        self.s.mac_multi_ip = {BRIDGE: IPS}

    def problem(self):
        return next(p for p in self.s.problems() if p["type"] == "same_mac_multi_ip")

    def test_ubiquiti_reply_names_the_bridge(self):
        found = {BRIDGE: dict(ubnt.parse(reply()), mac=BRIDGE, ip="192.168.1.151"),
                 "18e829607153": {"mac": "18e829607153", "ip": "192.168.1.5", "ips": []}}
        with mock.patch.object(ubnt, "discover", return_value=found) as d, \
                mock.patch.object(scanner.mikrotik, "discover") as mt:
            self.s._find_mac_owners()
        self.assertEqual(sorted(d.call_args.kwargs["unicast"]), sorted(IPS))
        mt.assert_not_called()                      # Ubiquiti answered — no MNDP needed
        p = self.problem()
        self.assertEqual(p["owner"]["ip"], "192.168.1.151")
        self.assertEqual(p["owner"]["model"], "LiteBeam 5AC Gen2")
        self.assertTrue(p["detail"].startswith("The device with this MAC is 192.168.1.151"))
        self.assertIn("Log in to 192.168.1.151", p["fix"])
        self.assertEqual(list(self.s.mac_owner), [BRIDGE])   # the other radio isn't an owner

    def test_mikrotik_fallback(self):
        nb = [{"mac": "E0:63:DA:40:91:78", "ipv4": "192.168.1.10", "identity": "Bridge-RB",
               "board": "RBSXTsq5", "version": "6.49.10"}]
        with mock.patch.object(ubnt, "discover", return_value={}), \
                mock.patch.object(scanner.mikrotik, "discover", return_value={"ok": True, "neighbors": nb}):
            self.s._find_mac_owners()
        self.assertEqual(self.problem()["owner"]["hostname"], "Bridge-RB")

    def test_nothing_answers(self):
        with mock.patch.object(ubnt, "discover", return_value={}), \
                mock.patch.object(scanner.mikrotik, "discover", return_value={"ok": False, "neighbors": []}):
            self.s._find_mac_owners()
        p = self.problem()
        self.assertIsNone(p["owner"])
        self.assertIn("ARP / DHCP lease table", p["fix"])

    def test_hostile_strings_are_bounded(self):
        found = {BRIDGE: {"mac": BRIDGE, "ip": "192.168.1.151", "ips": [], "hostname": "x" * 500}}
        with mock.patch.object(ubnt, "discover", return_value=found):
            self.s._find_mac_owners()
        self.assertEqual(len(self.s.mac_owner[BRIDGE]["hostname"]), 64)

    def test_no_problem_no_probe(self):
        self.s.mac_multi_ip = {}
        with mock.patch.object(ubnt, "discover") as d:
            self.s._find_mac_owners()
        d.assert_not_called()

    def test_marked_bridge_still_named(self):
        """Once marked as a bridge there is no problem card, but Settings still
        shows which IP is the radio — and a scan that didn't touch it keeps it."""
        self.s.mac_multi_ip = {}
        self.s._bridge_macs = {BRIDGE}
        self.s._bridge_ips = {BRIDGE: IPS}
        found = {BRIDGE: dict(ubnt.parse(reply()), mac=BRIDGE, ip="192.168.1.151")}
        with mock.patch.object(ubnt, "discover", return_value=found):
            self.s._find_mac_owners()
        self.assertEqual(self.s.mac_owner[BRIDGE]["ip"], "192.168.1.151")
        self.assertFalse([p for p in self.s.problems() if p["type"] == "same_mac_multi_ip"])
        self.s._bridge_ips = {}                       # targeted scan elsewhere
        with mock.patch.object(ubnt, "discover") as d:
            self.s._find_mac_owners()
        d.assert_not_called()
        self.assertEqual(self.s.mac_owner[BRIDGE]["ip"], "192.168.1.151")
        self.s._bridge_macs = set()                   # un-marked: forgotten
        self.s._find_mac_owners()
        self.assertEqual(self.s.mac_owner, {})

    def test_tunnel_reaches_ips_behind_the_bridge(self):
        self.s.mac_owner = {BRIDGE: {"ip": "192.168.1.151"}}
        with mock.patch.object(scanner, "scanner", self.s):
            self.assertTrue(tunnels._known_device("192.168.1.166"))
            self.assertTrue(tunnels._known_device("192.168.1.151"))
            self.assertFalse(tunnels._known_device("192.168.1.250"))


if __name__ == "__main__":
    unittest.main()
