"""Hub publicip.py: each site's public IP from the WireGuard tunnels.

Run inside the hub image (or with HUB_APP pointing at hub/app):
  docker run --rm -v "$PWD":/src -w /src -e HUB_APP=/src/hub/app \
      farm-netwatch-hub:local python -m unittest tests/test_hub_publicip.py
Addresses are documentation ranges (RFC 5737 / 3849) — never real sites.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ["HUB_DATA"] = tempfile.mkdtemp()
sys.path.insert(0, os.environ.get("HUB_APP", "/app"))

import publicip  # noqa: E402

_real_lookup = publicip.hub_lookup     # kept: Base patches it out (no network in tests)

ENDPOINTS = ("KEYa=\t203.0.113.7:34984\n"
             "KEYb=\t(none)\n"
             "KEYc=\t[2001:db8::5]:51820\n"
             "KEYd=\t192.168.88.254:41235\n")
ALLOWED = ("KEYa=\t10.8.0.5/32\n"
           "KEYb=\t10.8.0.6/32\n"
           "KEYc=\t10.8.0.7/32 192.168.50.0/24\n"
           "KEYd=\t10.8.0.3/32\n")
SHAKES = ("KEYa=\t1000\n"
          "KEYb=\t0\n"
          "KEYc=\t900\n"
          "KEYd=\t990\n")

SITES = [
    {"id": "home", "vpn_ip": "192.168.88.250"},
    {"id": "farm", "vpn_ip": "10.8.0.5"},
    {"id": "never", "vpn_ip": "10.8.0.6"},
    {"id": "six", "vpn_ip": "10.8.0.7"},
]


def peers(ip="203.0.113.7", hs=1000):
    return {"10.8.0.5": {"ip": ip, "handshake": hs}}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        p = mock.patch.object(publicip, "STATE_PATH", os.path.join(self.dir, "public_ip.json"))
        p.start()
        self.addCleanup(p.stop)
        h = mock.patch.object(publicip, "hub_lookup", return_value=None)   # no network in tests
        self.lookup = h.start()
        self.addCleanup(h.stop)
        publicip._state = None
        publicip._saved_at = 0.0
        publicip._hub_checked = 0.0
        publicip._error = ""

    def saved(self):
        with open(publicip.STATE_PATH) as f:
            return json.load(f)


class Parsing(unittest.TestCase):
    def test_endpoint_ip(self):
        self.assertEqual(publicip.endpoint_ip("203.0.113.7:51820"), "203.0.113.7")
        self.assertEqual(publicip.endpoint_ip("[2001:db8::5]:51820"), "2001:db8::5")
        self.assertIsNone(publicip.endpoint_ip("(none)"))
        self.assertIsNone(publicip.endpoint_ip(""))
        self.assertIsNone(publicip.endpoint_ip(None))
        self.assertIsNone(publicip.endpoint_ip("not-an-ip:51820"))
        self.assertIsNone(publicip.endpoint_ip("[2001:db8::5:51820"))

    def test_parse_peers_keyed_by_tunnel_address(self):
        p = publicip.parse_peers(ENDPOINTS, ALLOWED, SHAKES)
        self.assertEqual(p["10.8.0.5"], {"ip": "203.0.113.7", "handshake": 1000})
        self.assertEqual(p["10.8.0.6"], {"ip": None, "handshake": None})
        self.assertEqual(p["10.8.0.7"], {"ip": "2001:db8::5", "handshake": 900})
        self.assertNotIn("192.168.50.0", p)          # routed subnets are not peers

    def test_site_kind(self):
        self.assertEqual(publicip.site_kind("10.8.0.5"), "vpn")
        self.assertEqual(publicip.site_kind("192.168.88.250"), "lan")
        self.assertIsNone(publicip.site_kind("8.8.8.8"))       # a public address: neither
        self.assertIsNone(publicip.site_kind(""))
        self.assertIsNone(publicip.site_kind("farm.example"))

    def test_reads_only_safe_wg_views(self):
        """Never `wg show wg0 dump`: its first line is the server's private key."""
        calls = []

        def run(args, **kw):
            calls.append(args)
            out = {"endpoints": ENDPOINTS, "allowed-ips": ALLOWED,
                   "latest-handshakes": SHAKES}[args[3]]
            return mock.Mock(returncode=0, stdout=out, stderr="")
        with mock.patch.object(publicip.subprocess, "run", side_effect=run):
            p = publicip.read_peers()
        self.assertEqual(p["10.8.0.5"]["ip"], "203.0.113.7")
        self.assertEqual({tuple(c[:3]) for c in calls}, {("wg", "show", "wg0")})
        self.assertEqual({c[3] for c in calls}, {"endpoints", "allowed-ips", "latest-handshakes"})

    def test_hub_lookup_reads_trace(self):
        ok = mock.Mock(status_code=200, text="fl=1\nh=1.1.1.1\nip=198.51.100.9\nts=1\n")
        with mock.patch.object(publicip.requests, "get", return_value=ok):
            self.assertEqual(_real_lookup(), "198.51.100.9")

    def test_hub_lookup_falls_back_then_gives_up(self):
        bad = mock.Mock(status_code=200, text="ip=garbage\n")
        err = publicip.requests.exceptions.ConnectionError("down")
        with mock.patch.object(publicip.requests, "get", side_effect=[err, bad]):
            self.assertIsNone(_real_lookup())
        good = mock.Mock(status_code=200, text="ip=198.51.100.9\n")
        with mock.patch.object(publicip.requests, "get", side_effect=[err, good]):
            self.assertEqual(_real_lookup(), "198.51.100.9")


class Tracking(Base):
    def test_first_sight(self):
        publicip.refresh(SITES, now=1010, peers=peers(), hub_ip="198.51.100.9")
        c = publicip.card("farm", now=1010)
        self.assertEqual(c["ip"], "203.0.113.7")
        self.assertEqual(c["source"], "vpn")
        self.assertIsNone(c["since"])                # unknown: first time the hub saw it
        self.assertEqual(c["first"], 1010)
        self.assertEqual(c["seen"], 1000)            # the tunnel's last handshake
        self.assertTrue(c["live"])
        self.assertEqual(c["prev"], [])
        self.assertIsNone(publicip.card("never", now=1010))     # no endpoint yet
        self.assertEqual(self.saved()["sites"]["farm"]["ip"], "203.0.113.7")

    def test_home_site_gets_the_hubs_address(self):
        publicip.refresh(SITES, now=1010, peers=peers(), hub_ip="198.51.100.9")
        c = publicip.card("home", now=1010)
        self.assertEqual((c["ip"], c["source"]), ("198.51.100.9", "hub"))
        self.assertEqual(publicip.hub_card(now=1010)["ip"], "198.51.100.9")
        # still "in use" between the 15-minute checks, not long after
        self.assertTrue(publicip.card("home", now=1010 + 1800)["live"])
        self.assertFalse(publicip.card("home", now=1010 + 4000)["live"])

    def test_change_keeps_the_old_address(self):
        publicip.refresh(SITES, now=1010, peers=peers(), hub_ip="198.51.100.9")
        publicip.refresh(SITES, now=1070, peers=peers(hs=1060))
        publicip.refresh(SITES, now=1130, peers=peers("203.0.113.99", hs=1125))
        c = publicip.card("farm", now=1130)
        self.assertEqual(c["ip"], "203.0.113.99")
        self.assertEqual(c["since"], 1130)
        self.assertEqual(c["prev"], [{"ip": "203.0.113.7", "from": 1010, "from_known": False, "to": 1060}])
        publicip.refresh(SITES, now=2000, peers=peers("203.0.113.7", hs=1990))
        c = publicip.card("farm", now=2000)
        self.assertEqual(c["prev"][0], {"ip": "203.0.113.99", "from": 1130, "from_known": True, "to": 1125})
        self.assertEqual(len(c["prev"]), 2)
        self.assertEqual(self.saved()["sites"]["farm"]["ip"], "203.0.113.7")   # changes save at once

    def test_offline_site_keeps_its_last_address(self):
        publicip.refresh(SITES, now=1010, peers=peers(), hub_ip="198.51.100.9")
        c = publicip.card("farm", now=1000 + 600)
        self.assertEqual(c["ip"], "203.0.113.7")
        self.assertFalse(c["live"])                  # no handshake for 10 min
        # wg-easy restarted: endpoint gone; the tunnels could not be read at all
        publicip.refresh(SITES, now=2000, peers={"10.8.0.5": {"ip": None, "handshake": None}})
        publicip.refresh(SITES, now=2100, peers=None)
        self.assertEqual(publicip.card("farm", now=2100)["ip"], "203.0.113.7")

    def test_unreadable_tunnels_do_not_raise(self):
        with mock.patch.object(publicip, "_wg", side_effect=FileNotFoundError("wg")):
            publicip.refresh(SITES, now=1010, hub_ip="198.51.100.9")
        self.assertIn("wg is not installed", publicip._error)
        self.assertIsNone(publicip.card("farm", now=1010))
        self.assertEqual(publicip.card("home", now=1010)["ip"], "198.51.100.9")

    def test_removed_site_is_forgotten(self):
        publicip.refresh(SITES, now=1010, peers=peers(), hub_ip="198.51.100.9")
        publicip.refresh([s for s in SITES if s["id"] != "farm"], now=1070, peers=peers())
        self.assertIsNone(publicip.card("farm", now=1070))
        self.assertNotIn("farm", self.saved()["sites"])

    def test_last_seen_is_saved_sparingly(self):
        publicip.refresh(SITES, now=1010, peers=peers(), hub_ip="198.51.100.9")
        publicip.refresh(SITES, now=1070, peers=peers(hs=1065))
        self.assertEqual(self.saved()["sites"]["farm"]["seen"], 1000)      # not yet on disk
        self.assertEqual(publicip.card("farm", now=1070)["seen"], 1065)    # but shown
        publicip.refresh(SITES, now=1010 + publicip.SAVE_EVERY_S, peers=peers(hs=1900))
        self.assertEqual(self.saved()["sites"]["farm"]["seen"], 1900)

    def test_survives_a_hub_restart(self):
        publicip.refresh(SITES, now=1010, peers=peers(), hub_ip="198.51.100.9")
        publicip.refresh(SITES, now=1130, peers=peers("203.0.113.99", hs=1125))
        publicip._state = None                       # a new process reads the file
        c = publicip.card("farm", now=1130)
        self.assertEqual((c["ip"], c["prev"][0]["ip"]), ("203.0.113.99", "203.0.113.7"))

    def test_bad_state_file_starts_clean(self):
        with open(publicip.STATE_PATH, "w") as f:
            f.write("{not json")
        publicip.refresh(SITES, now=1010, peers=peers(), hub_ip="198.51.100.9")
        self.assertEqual(publicip.card("farm", now=1010)["ip"], "203.0.113.7")

    def test_history_is_capped(self):
        publicip.refresh(SITES, now=1000, peers=peers("203.0.113.1", hs=1000))
        for i in range(2, 20):
            publicip.refresh(SITES, now=1000 + i * 100, peers=peers(f"203.0.113.{i}", hs=1000 + i * 100))
        st = publicip._load()["sites"]["farm"]
        self.assertEqual(len(st["prev"]), publicip.PREV_MAX)
        self.assertEqual(len(publicip.card("farm", now=3000)["prev"]), 5)

    def test_hub_lookup_only_every_15_minutes(self):
        self.lookup.return_value = "198.51.100.9"
        publicip.refresh(SITES, now=1000, peers=peers())
        publicip.refresh(SITES, now=1060, peers=peers())
        publicip.refresh(SITES, now=1000 + publicip.HUB_EVERY_S, peers=peers())
        self.assertEqual(self.lookup.call_count, 2)


if __name__ == "__main__":
    unittest.main()
