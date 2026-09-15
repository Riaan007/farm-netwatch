"""MikroTik helpers (app/mikrotik.py) — the network-free logic: MNDP parsing,
RouterOS API length framing, the console danger guard, and the MAC-Telnet
`:put` status parser.

Run inside the site image:
  docker run --rm -v "$PWD/app:/app" -v "$PWD/tests:/tests" -e NETWATCH_DATA=/tmp/nw \
    --entrypoint python farm-netwatch:netcfg /tests/test_mikrotik.py -v
"""
import os
import struct
import sys
import tempfile
import unittest

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import mikrotik    # noqa: E402


def _tlv(t, v):
    return struct.pack(">HH", t, len(v)) + v


def _mndp_packet(mac, identity, version, board, ip, uptime, iface):
    body = b"\x00\x00\x00\x00"                       # 4-byte header
    body += _tlv(1, bytes.fromhex(mac.replace(":", "")))
    body += _tlv(5, identity.encode())
    body += _tlv(7, version.encode())
    body += _tlv(8, b"MikroTik")
    body += _tlv(10, struct.pack("<I", uptime))
    body += _tlv(12, board.encode())
    body += _tlv(16, iface.encode())
    body += _tlv(17, bytes(int(x) for x in ip.split(".")))
    return body


class TestMndp(unittest.TestCase):
    def test_parse_real_shape(self):
        pkt = _mndp_packet("08:55:31:60:b6:76", "MikroTik", "6.45.9 (long-term)",
                           "RB750UPr2", "192.168.88.46", 1472, "bridgeLocal/ether1")
        d = mikrotik._parse_mndp(pkt)
        self.assertEqual(d["mac"], "08:55:31:60:b6:76")
        self.assertEqual(d["identity"], "MikroTik")
        self.assertEqual(d["version"], "6.45.9 (long-term)")
        self.assertEqual(d["board"], "RB750UPr2")
        self.assertEqual(d["ipv4"], "192.168.88.46")
        self.assertEqual(d["uptime"], 1472)
        self.assertEqual(d["interface"], "bridgeLocal/ether1")

    def test_truncated_packet_is_safe(self):
        # a body that claims a longer TLV than it carries must not raise
        bad = b"\x00\x00\x00\x00" + struct.pack(">HH", 5, 99) + b"short"
        self.assertIsInstance(mikrotik._parse_mndp(bad), dict)

    def test_normalize_mac(self):
        self.assertEqual(mikrotik.normalize_mac("0855.3160.B676"), "08:55:31:60:b6:76")
        self.assertEqual(mikrotik.normalize_mac("08-55-31-60-b6-76"), "08:55:31:60:b6:76")
        self.assertEqual(mikrotik.normalize_mac("nope"), "")


class TestApiFraming(unittest.TestCase):
    def test_enc_len_boundaries(self):
        self.assertEqual(mikrotik.RosApi._enc_len(0x05), b"\x05")
        self.assertEqual(mikrotik.RosApi._enc_len(0x7f), b"\x7f")
        self.assertEqual(mikrotik.RosApi._enc_len(0x80), b"\x80\x80")
        self.assertEqual(mikrotik.RosApi._enc_len(0x4000), b"\xc0\x40\x00")

    def test_enc_len_round_trip(self):
        # decode with the same rules the client reads with, over a fake socket
        class FakeSock:
            def __init__(self, data): self.data = data
            def recv(self, n):
                out, self.data = self.data[:n], self.data[n:]
                return out
        for n in (0, 1, 0x7f, 0x80, 0x1234, 0x4000, 0x1FFFFF, 0x200000):
            api = mikrotik.RosApi("0.0.0.0")
            api.sock = FakeSock(mikrotik.RosApi._enc_len(n))
            self.assertEqual(api._read_len(), n, "len %d did not round-trip" % n)


class TestDangerGuard(unittest.TestCase):
    def test_flags_bricking_commands(self):
        for c in ["/system reset-configuration", "/system reset",
                  "/interface disable ether1", "/interface remove bridge",
                  "/ip address remove 0", "/user set admin password=x",
                  "/user remove admin", "/system shutdown",
                  "/system routerboard upgrade", "  /SYSTEM   RESET  "]:
            self.assertTrue(mikrotik.dangerous_command(c), "should flag: %r" % c)

    def test_flags_slash_delimited_form(self):
        # the RouterOS one-liner (slash) form must NOT bypass the guard
        for c in ["/system/reboot", "/system/shutdown", "/system/reset",
                  "/ip/address/remove numbers=0", "/ip/firewall/filter/remove numbers=0",
                  "/system/routerboard/upgrade", "/interface/disable ether2"]:
            self.assertTrue(mikrotik.dangerous_command(c), "slash form should flag: %r" % c)

    def test_passes_read_commands(self):
        for c in ["/system identity print", "/system resource print",
                  "/ip address print", "/interface print", "/log print",
                  "/system routerboard print"]:
            self.assertEqual(mikrotik.dangerous_command(c), "", "should pass: %r" % c)


class TestMtStatusParse(unittest.TestCase):
    CANNED = "\n".join([
        "__NW__|identity|MikroTik",
        "__NW__|version|6.45.9 (long-term)",
        "__NW__|board|hEX PoE lite",
        "__NW__|uptime|01:23:15",
        "__NW__|cpu|3",
        "__NW__|free_memory|44298240",
        "__NW__|total_memory|67108864",
        "__NW__|arch|mipsbe",
        "__NW__|model|RB750UPr2",
        "__NW__|serial|D1630D332D79",
        "__NW__|firmware|6.45.9",
        "__NW__|iface|ether1~ether~true~false~22657774~9040211",
        "__NW__|iface|ether2~ether~false~false~0~0",
        "__NW__|addr|192.168.88.46/24~bridgeLocal",
    ])

    def test_parse(self):
        orig = mikrotik.mt_run
        mikrotik.mt_run = lambda *a, **k: {"ok": True, "output": self.CANNED, "per": []}
        try:
            s = mikrotik.mt_status("08:55:31:60:b6:76", "admin", "")
        finally:
            mikrotik.mt_run = orig
        self.assertTrue(s["ok"])
        self.assertEqual(s["source"], "mactelnet")
        self.assertEqual(s["identity"], "MikroTik")
        self.assertEqual(s["model"], "RB750UPr2")
        self.assertEqual(s["board"], "hEX PoE lite")
        self.assertEqual(s["serial"], "D1630D332D79")
        self.assertEqual(s["arch"], "mipsbe")
        self.assertEqual(len(s["interfaces"]), 2)
        self.assertEqual(s["interfaces"][0]["name"], "ether1")
        self.assertTrue(s["interfaces"][0]["running"])
        self.assertFalse(s["interfaces"][1]["running"])
        self.assertEqual(s["addresses"], [{"address": "192.168.88.46/24",
                                           "interface": "bridgeLocal", "disabled": False}])


class TestClean(unittest.TestCase):
    def test_collapses_blank_lines_and_ansi(self):
        raw = "a\r\n\r\n\r\n\r\nb\x1b[32mc\x1b[0m"
        out = mikrotik._clean(raw)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\n\n\n", out)
        self.assertIn("bc", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
