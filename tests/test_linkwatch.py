"""Telegram phase 3 on the site: app/linkwatch.py — the ladder while the hub
link is down, the diagnosis message, signed heartbeats, the outage report.
No network: probes, the site API and ntfy are fakes.

  docker run --rm --network none -e PYTHONDONTWRITEBYTECODE=1 -v "$PWD/app:/app" \
      -v "$PWD/tests:/tests" -e NETWATCH_DATA=/tmp/nw --entrypoint python \
      farm-netwatch:netcfg /tests/test_linkwatch.py -v
Addresses are documentation ranges.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import linkwatch  # noqa: E402

KEY = "ab" * 32


class FakeProbes:
    def __init__(self):
        self.dns = {"hub.example.net": ["203.0.113.9"]}
        self.web = True
        self.web_calls = []

    def resolve(self, host):
        return self.dns.get(host, [])

    def tcp(self, ip, port, timeout=5):
        self.web_calls.append((ip, port))
        return self.web

    def https(self, url, timeout=6):
        return True


class Base(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.conf = os.path.join(d, "wg0.conf")
        with open(self.conf, "w") as f:
            f.write("[Interface]\nPrivateKey = x\n[Peer]\nEndpoint = hub.example.net:51820\n")
        self.now = [1_000_000.0]
        self.alerts = []
        self.posts = []
        self.hub = {"configured": True, "connected": False, "hub_reachable": False,
                    "endpoint": "203.0.113.9:51820", "last_handshake_s": 400}
        self.inet = {"has_gateway": True, "gateway": True, "dns": True, "external": True}
        self.sys = {"uptime_s": 86400}
        self.st = {"hb_topic": "nwhb-test", "hb_key": KEY, "hb_server": "https://ntfy.example"}
        self.probes = FakeProbes()
        self.state_path = os.path.join(d, "lw.json")
        self.lw = self.make()
        p = mock.patch.object(linkwatch.requests, "post", side_effect=self.post)
        p.start()
        self.addCleanup(p.stop)

    def make(self):
        return linkwatch.LinkWatch(lambda: self.st, self.api, lambda *a: self.alerts.append(a),
                                   probes=self.probes, clock=lambda: self.now[0],
                                   conf_paths=[self.conf], state_path=self.state_path,
                                   telegram_ok=lambda: True)

    def api(self, path):
        return {"/api/hub/status": self.hub, "/api/internet": self.inet, "/api/sysinfo": self.sys}.get(path)

    def post(self, url, data=None, headers=None, timeout=None):
        self.posts.append((url, json.loads(data)))
        return mock.Mock(status_code=200)

    def run_for(self, minutes):
        out = None
        for _ in range(minutes):
            out = self.lw.tick()
            self.now[0] += 60
        return out


class Ladder(Base):
    def test_no_tunnel_no_work(self):
        self.hub = {"configured": False}
        self.assertIsNone(self.lw.tick())
        self.assertEqual(self.posts, [])

    def test_tunnel_down_internet_up_diagnosis_after_3_min(self):
        self.assertEqual(self.lw.tick(), "tunnel_down_internet_up")
        self.assertEqual(len(self.posts), 1)                  # heartbeat at once
        self.assertEqual(self.alerts, [])
        self.run_for(3)                                        # 2 min down: not yet
        self.assertEqual(self.alerts, [])
        self.run_for(1)                                        # 3 min
        self.assertEqual(len(self.alerts), 1)
        level, title, body, buttons = self.alerts[0]
        self.assertEqual(title, "Hub link down — internet is UP")
        self.assertIn("hub.example.net → 203.0.113.9", body)
        self.assertIn("only the tunnel is stuck", body)
        self.assertEqual(buttons[0][0][1], "hub")
        self.run_for(10)
        self.assertEqual(len(self.alerts), 1)                  # one diagnosis per outage

    def test_states(self):
        self.probes.dns["hub.example.net"] = ["203.0.113.50"]   # home IP changed
        self.assertEqual(self.lw.tick(), "hub_address_stale")
        self.probes.dns["hub.example.net"] = ["203.0.113.9"]
        self.probes.web = False
        self.assertEqual(self.lw.tick(), "hub_side_down")
        self.inet["dns"] = False
        self.assertEqual(self.lw.tick(), "dns_broken")
        self.inet["external"] = False
        self.assertEqual(self.lw.tick(), "internet_down")
        self.inet["gateway"] = False
        self.assertEqual(self.lw.tick(), "router_down")

    def test_no_diagnosis_while_internet_is_down(self):
        self.inet["external"] = False
        self.run_for(5)
        self.assertEqual(self.alerts, [])
        self.inet["external"] = True
        self.run_for(1)
        self.assertEqual(len(self.alerts), 1)


class Heartbeats(Base):
    def test_signed_flags_only_every_5_min(self):
        self.run_for(11)
        self.assertEqual(len(self.posts), 3)                   # at once, +5, +10 min
        url, p = self.posts[0]
        self.assertEqual(url, "https://ntfy.example/nwhb-test")
        self.assertEqual(p["sig"], linkwatch.sign(p, KEY))
        self.assertEqual(p["st"], "tunnel_down_internet_up")
        raw = json.dumps(p)
        self.assertNotIn("203.0.113", raw)                     # no addresses
        self.assertNotIn("hub.example", raw)
        self.assertGreater(self.posts[1][1]["seq"], p["seq"])

    def test_no_topic_no_heartbeat(self):
        self.st = {}
        self.run_for(3)
        self.assertEqual(self.posts, [])

    def test_daily_heartbeat_when_all_is_well(self):
        self.hub.update(connected=True, hub_reachable=True)
        self.lw.tick()
        self.assertEqual(self.posts[-1][1]["st"], "ok")
        self.run_for(10)
        self.assertEqual(len(self.posts), 1)


class Report(Base):
    def test_report_and_power_cut_survives_restart(self):
        self.run_for(30)
        # the Pi loses power: a NEW process, uptime 5 min, state from disk
        self.lw = self.make()
        self.sys["uptime_s"] = 300
        self.hub.update(connected=True, hub_reachable=True)
        self.lw.tick()
        level, title, body, _ = self.alerts[-1]
        self.assertEqual(title, "Hub link is back")
        self.assertIn("30 min", body)
        self.assertIn("Pi itself restarted", body)
        self.assertNotIn("down_since", self.lw.st)

    def test_short_blip_is_not_reported(self):
        self.run_for(2)
        self.hub.update(connected=True, hub_reachable=True)
        self.lw.tick()
        self.assertEqual([a for a in self.alerts if a[1] == "Hub link is back"], [])

    def test_endpoint_host_parsing(self):
        self.assertEqual(linkwatch.endpoint_host([self.conf]), ("hub.example.net", 51820))
        self.assertEqual(linkwatch.endpoint_host(["/nonexistent"]), (None, None))


if __name__ == "__main__":
    unittest.main()
