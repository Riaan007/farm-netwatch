"""Telegram phase 3 on the hub: hub/app/heartbeats.py — verify heartbeats,
diagnose a site the hub cannot reach, the DDNS truth check. No network.

  docker run --rm --network none -e PYTHONDONTWRITEBYTECODE=1 -v "$PWD/hub/app:/app:ro" \
      -v "$PWD/tests:/tests:ro" -e HUB_DATA=/tmp/hub --entrypoint python \
      farm-netwatch-hub:local /tests/test_hub_heartbeats.py -v
Addresses are documentation ranges.
"""
import hashlib
import hmac
import json
import os
import sys
import tempfile
import unittest

os.environ["HUB_DATA"] = tempfile.mkdtemp()
sys.path.insert(0, os.environ.get("HUB_APP", "/app"))

import heartbeats  # noqa: E402

KEY = "cd" * 32
NOW = 2_000_000.0


def signed(p, key=KEY):
    raw = json.dumps(p, sort_keys=True, separators=(",", ":")).encode()
    return {**p, "sig": hmac.new(key.encode(), raw, hashlib.sha256).hexdigest()}


SITES = [{"id": "a", "vpn_ip": "10.8.0.2"}, {"id": "b", "vpn_ip": "10.8.0.5"},
         {"id": "home", "vpn_ip": "192.168.88.250"}]


class Base(unittest.TestCase):
    def setUp(self):
        heartbeats._last.clear()
        heartbeats._down.clear()
        heartbeats._fleet["alerted"] = False
        heartbeats._ddns.update(name=None, name_ts=0, bad_since=None, alerted=False, last=None)
        self.sent = []

    def send(self, *a):
        self.sent.append(a)


class Verify(Base):
    def test_signature_age_and_order(self):
        p = signed({"v": 1, "t": NOW - 60, "seq": 2, "st": "tunnel_down_internet_up"})
        self.assertTrue(heartbeats.accept("a", p, KEY, now=NOW))
        self.assertFalse(heartbeats.accept("a", p, KEY, now=NOW))              # replay
        forged = dict(p, st="ok", t=NOW)
        self.assertFalse(heartbeats.accept("a", forged, KEY, now=NOW))         # bad signature
        self.assertFalse(heartbeats.accept("a", signed({"t": NOW - 60}, "ff" * 32), KEY, now=NOW))
        self.assertFalse(heartbeats.accept("a", signed({"t": NOW - 7 * 3600}), KEY, now=NOW))  # too old
        self.assertTrue(heartbeats.latest("a", now=NOW)["fresh"])
        self.assertFalse(heartbeats.latest("a", now=NOW + 3600)["fresh"])

    def test_site_and_hub_sign_the_same_way(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
        try:
            import linkwatch
        except ImportError:
            self.skipTest("site code not mounted")
        p = {"v": 1, "t": NOW, "seq": 1, "st": "ok", "up": 5}
        p["sig"] = linkwatch.sign(p, KEY)
        self.assertTrue(heartbeats.verify(p, KEY))

    def test_secret_and_vpn_sites(self):
        s = heartbeats.new_secret()
        self.assertTrue(s["topic"].startswith("nwhb-") and len(s["key"]) == 64)
        self.assertTrue(heartbeats.is_vpn_site(SITES[0]))
        self.assertFalse(heartbeats.is_vpn_site(SITES[2]))


class Diagnose(Base):
    def poll(self, sid, reachable, t, inet=True):
        site = next(s for s in SITES if s["id"] == sid)
        heartbeats.on_poll(site, reachable, SITES, None, inet, self.send, now=t)

    def test_alive_site_ops_only(self):
        heartbeats.accept("a", signed({"t": NOW, "seq": 1, "st": "tunnel_down_internet_up", "up": 7200}),
                          KEY, now=NOW)
        self.poll("a", False, NOW)
        self.poll("b", True, NOW)
        self.assertEqual(self.sent, [])                          # not yet: 6 min grace
        self.poll("a", False, NOW + 400)
        level, title, text, sid, dark = self.sent[0]
        self.assertEqual((level, sid, dark), ("warning", "a", False))
        self.assertIn("ALIVE", title)
        self.assertIn("only the tunnel is stuck", text)
        self.poll("a", False, NOW + 800)
        self.assertEqual(len(self.sent), 1)                      # once per outage

    def test_dark_site_also_to_its_group(self):
        self.poll("a", False, NOW)
        self.poll("b", True, NOW)
        self.poll("a", False, NOW + 400)
        level, title, text, sid, dark = self.sent[0]
        self.assertEqual((level, dark), ("critical", True))
        self.assertIn("DARK", title)
        self.assertIn("No heartbeat", text)

    def test_all_sites_down_is_one_hub_message(self):
        self.poll("a", False, NOW)
        self.poll("b", False, NOW)
        self.poll("a", False, NOW + 400)
        self.poll("b", False, NOW + 400)
        self.assertEqual(len(self.sent), 1)
        self.assertIsNone(self.sent[0][3])
        self.assertIn("Every site", self.sent[0][1])

    def test_hub_internet_down_says_nothing_until_back(self):
        self.poll("a", False, NOW)
        self.poll("b", True, NOW)
        self.poll("a", False, NOW + 400, inet=False)
        self.assertEqual(self.sent, [])
        self.poll("a", False, NOW + 460, inet=True)
        self.assertEqual(len(self.sent), 1)

    def test_recovery_resets(self):
        self.poll("a", False, NOW)
        self.poll("b", True, NOW)
        self.poll("a", False, NOW + 400)
        self.poll("a", True, NOW + 500)
        self.poll("a", False, NOW + 600)
        self.poll("a", False, NOW + 1000)
        self.assertEqual(len(self.sent), 2)

    def test_lan_site_is_ignored(self):
        self.poll("home", False, NOW)
        self.poll("home", False, NOW + 900)
        self.assertEqual(self.sent, [])


class Ddns(Base):
    def test_mismatch_alerts_after_grace_then_fixed(self):
        r = lambda n: ["203.0.113.1"]  # noqa: E731
        self.assertFalse(heartbeats.ddns_check("hub.example.net", "203.0.113.2", self.send, now=NOW, resolver=r))
        self.assertEqual(self.sent, [])
        heartbeats.ddns_check("hub.example.net", "203.0.113.2", self.send, now=NOW + 700, resolver=r)
        self.assertEqual(self.sent[0][0], "critical")
        self.assertIn("WRONG", self.sent[0][1])
        heartbeats.ddns_check("hub.example.net", "203.0.113.2", self.send, now=NOW + 900, resolver=r)
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(heartbeats.ddns_check("hub.example.net", "203.0.113.2", self.send, now=NOW + 1000,
                                              resolver=lambda n: ["203.0.113.2"]))
        self.assertEqual(self.sent[-1][1], "DDNS fixed")

    def test_dns_failure_or_ip_name_proves_nothing(self):
        self.assertIsNone(heartbeats.ddns_check("hub.example.net", "203.0.113.2", self.send, now=NOW,
                                                resolver=lambda n: []))
        self.assertIsNone(heartbeats.ddns_check("203.0.113.2", "203.0.113.2", self.send, now=NOW))

    def test_endpoint_name_from_a_client_config(self):
        conf = "[Interface]\nAddress = 10.8.0.9/24\n[Peer]\nEndpoint = hub.example.net:51820\n"
        self.assertEqual(heartbeats.endpoint_name(lambda: conf), "hub.example.net")


if __name__ == "__main__":
    unittest.main()
