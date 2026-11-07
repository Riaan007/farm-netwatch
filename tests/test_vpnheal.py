"""Hub tunnel self-repair (vpnheal.py): when to act, what it runs, and that it
never deletes or restarts wg0.

Run inside the site image with a throwaway data dir:
  docker run --rm --network none -e PYTHONDONTWRITEBYTECODE=1 \
    -v "$PWD/app:/app:ro" -v "$PWD/tests:/tests:ro" -e NETWATCH_DATA=/tmp/nw \
    --entrypoint python farm-netwatch:netcfg /tests/test_vpnheal.py -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import hubvpn    # noqa: E402
import vpnheal   # noqa: E402

CFG = {"vpn": {"self_heal": True, "heal_after_min": 5, "heal_repeat_min": 15}}
CONF = "[Interface]\nPrivateKey = x\nAddress = 10.8.0.8/24\n\n[Peer]\nPublicKey = y\nEndpoint = hub.example.net:51820\nAllowedIPs = 10.8.0.0/24\n"
PEER = "PEERKEY="
T0 = 1_800_000_000


class Fake:
    def __init__(self):
        self.calls = []

    def run(self, args, timeout=20):
        self.calls.append(list(args))
        if args[:4] == ["wg", "show", "wg0", "peers"]:
            return 0, PEER + "\n"
        return 0, ""


class Heal(unittest.TestCase):
    def setUp(self):
        vpnheal.reset()
        self.fake = Fake()
        d = tempfile.mkdtemp()
        self.conf = os.path.join(d, "wg0.conf")
        with open(self.conf, "w") as f:
            f.write(CONF)
        self.p = [mock.patch.object(hubvpn, "_run", self.fake.run),
                  mock.patch.object(hubvpn, "WG_CONF", self.conf),
                  mock.patch.object(hubvpn, "has_config", return_value=True),
                  mock.patch.object(hubvpn, "is_up", return_value=True),
                  mock.patch.object(vpnheal.socket, "getaddrinfo",
                                    return_value=[(2, 2, 17, "", ("203.0.113.9", 51820))])]
        for x in self.p:
            x.start()
        self.logged = []
        self.notes = []

    def tearDown(self):
        for x in self.p:
            x.stop()

    def tick(self, minute, hub_up=False, internet=True, cfg=CFG):
        return vpnheal.check(cfg, hub_up, internet, now=T0 + minute * 60,
                             log=lambda w, d: self.logged.append((w, d)),
                             notify_fn=self.notes.append)

    def wg_sets(self):
        return [c for c in self.fake.calls if c[:2] == ["wg", "set"]]

    def test_waits_then_rehandshakes_then_moves_port_then_repeats(self):
        self.assertIsNone(self.tick(0))
        self.assertIsNone(self.tick(4))
        self.assertEqual(self.tick(5), "rehandshake")
        self.assertEqual(self.wg_sets()[-1], ["wg", "set", "wg0", "peer", PEER, "endpoint", "203.0.113.9:51820"])
        self.assertIsNone(self.tick(9))
        self.assertEqual(self.tick(10), "new_port")
        port = int(self.wg_sets()[-1][4])
        self.assertEqual(self.wg_sets()[-1][:4], ["wg", "set", "wg0", "listen-port"])
        self.assertTrue(20000 <= port <= 60999)
        self.assertIsNone(self.tick(24))
        self.assertEqual(self.tick(25), "new_port")
        self.assertEqual([w for w, _ in self.logged], ["rehandshake", "new_port", "new_port"])

    def test_recovery_is_logged_and_notified_once(self):
        self.tick(0); self.tick(5)
        self.assertIsNone(self.tick(7, hub_up=True))
        self.assertEqual(self.logged[-1][0], "recovered")
        self.assertEqual(self.logged[-1][1]["down_min"], 7)
        self.assertEqual(len(self.notes), 1)
        self.tick(8, hub_up=True)
        self.assertEqual(len(self.notes), 1)
        self.assertIsNone(vpnheal.status()["down_since"])

    def test_short_blip_heals_itself_silently(self):
        self.tick(0); self.tick(3, hub_up=True)
        self.assertEqual(self.logged, [])
        self.assertEqual(self.notes, [])

    def test_no_action_while_internet_is_down(self):
        for m in range(0, 30, 1):
            self.assertIsNone(self.tick(m, internet=False))
        self.assertEqual(self.wg_sets(), [])
        # the clock starts when the internet comes back, not before
        self.assertIsNone(self.tick(30))
        self.assertIsNone(self.tick(34))
        self.assertEqual(self.tick(35), "rehandshake")

    def test_switched_off(self):
        off = {"vpn": {"self_heal": False}}
        for m in range(0, 40, 5):
            self.assertIsNone(self.tick(m, cfg=off))
        self.assertEqual(self.wg_sets(), [])

    def test_no_tunnel_no_action(self):
        with mock.patch.object(hubvpn, "is_up", return_value=False):
            for m in range(0, 40, 5):
                self.assertIsNone(self.tick(m))
        self.assertEqual(self.fake.calls, [])

    def test_never_deletes_or_restarts_wg0(self):
        for m in range(0, 120, 1):
            self.tick(m)
        flat = [" ".join(c) for c in self.fake.calls]
        self.assertFalse([c for c in flat if "wg-quick" in c or "link delete" in c or " down" in c], flat)

    def test_unresolvable_endpoint_is_logged_not_fatal(self):
        with mock.patch.object(vpnheal.socket, "getaddrinfo", side_effect=vpnheal.socket.gaierror("no dns")):
            self.tick(0)
            self.assertEqual(self.tick(5), "rehandshake")
        self.assertFalse(self.logged[-1][1]["ok"])
        self.assertEqual(self.tick(10), "new_port")

    def test_settings_floor(self):
        silly = {"vpn": {"heal_after_min": 1, "heal_repeat_min": 1}}
        self.assertEqual(vpnheal._settings(silly), (True, 2, 5))


if __name__ == "__main__":
    unittest.main()
