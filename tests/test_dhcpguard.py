"""Rogue DHCP guard (app/dhcpguard.py): the DISCOVER it sends, reading offers,
judging servers, and the alert / gone life-cycle with a stubbed probe.

Run inside the site image:
  docker run --rm -v "$PWD/app:/app" -v "$PWD/tests:/tests" -e NETWATCH_DATA=/tmp/nw \
    --entrypoint python farm-netwatch:netcfg /tests/test_dhcpguard.py -v
"""
import os
import socket
import struct
import sys
import tempfile
import unittest

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import dhcpguard   # noqa: E402

TARGETS = [{"cidr": "192.168.88.0/24"}]
GW = "192.168.88.254"
ROUTER_MAC = "70:a7:41:45:cf:aa"
ROGUE_MAC = "d4:0d:ab:03:bc:79"


def offer_frame(server_mac, server_ip, offered, xid, mask="255.255.255.0", router=None, mtype=2, client=b"\x02" * 6):
    """A DHCP server's reply, built the way a real one is on the wire."""
    opts = bytes([53, 1, mtype, 54, 4]) + socket.inet_aton(server_ip) + bytes([1, 4]) + socket.inet_aton(mask)
    opts += bytes([3, 4]) + socket.inet_aton(router or server_ip) + bytes([6, 4]) + socket.inet_aton("8.8.8.8")
    opts += bytes([51, 4]) + struct.pack("!I", 600) + b"\xff"
    bootp = struct.pack("!BBBBIHH4s4s4s4s16s64s128s", 2, 1, 6, 0, xid, 0, 0x8000, b"\0" * 4,
                        socket.inet_aton(offered), socket.inet_aton(server_ip), b"\0" * 4,
                        client + b"\0" * 10, b"\0" * 64, b"\0" * 128) + dhcpguard.MAGIC + opts
    udp = struct.pack("!HHHH", 67, 68, 8 + len(bootp), 0) + bootp
    iph = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 1, 0, 64, 17, 0,
                      socket.inet_aton(server_ip), b"\xff\xff\xff\xff")
    mac = bytes.fromhex(server_mac.replace(":", ""))
    return b"\xff" * 6 + mac + b"\x08\x00" + iph + udp


class Packets(unittest.TestCase):
    def test_discover_is_a_valid_broadcast_discover(self):
        mac = dhcpguard.probe_mac("2c:cf:67:20:a7:ee")
        f = dhcpguard.build_discover(mac, 0x11223344)
        self.assertEqual(f[:6], b"\xff" * 6)
        self.assertEqual(f[6:12], mac)
        self.assertEqual(f[0 + 6] & 0x03, 0x02, "locally administered unicast")
        ip = f[14:34]
        self.assertEqual(dhcpguard._csum(ip), 0, "IP header checksum")
        self.assertEqual(socket.inet_ntoa(ip[16:20]), "255.255.255.255")
        self.assertEqual(struct.unpack("!HH", f[34:38]), (68, 67))
        b = f[42:]
        self.assertEqual(b[0], 1)                                   # BOOTREQUEST
        self.assertEqual(struct.unpack("!I", b[4:8])[0], 0x11223344)
        self.assertEqual(struct.unpack("!H", b[10:12])[0], 0x8000)  # broadcast flag
        self.assertEqual(b[28:34], mac)
        o = dhcpguard._options(b[240:])
        self.assertEqual(o[53], b"\x01")                            # DISCOVER, never REQUEST
        self.assertNotIn(50, o, "no requested address: a DISCOVER takes no lease")

    def test_probe_mac_is_stable_per_pi(self):
        self.assertEqual(dhcpguard.probe_mac("aa:bb"), dhcpguard.probe_mac("aa:bb"))
        self.assertNotEqual(dhcpguard.probe_mac("aa:bb"), dhcpguard.probe_mac("aa:bc"))

    def test_offer_is_read(self):
        r = dhcpguard.parse_reply(offer_frame(ROGUE_MAC, "10.1.1.1", "10.1.1.123", 7), xid=7)
        self.assertEqual(r["type"], "OFFER")
        self.assertEqual(r["mac"], ROGUE_MAC)
        self.assertEqual(r["server_id"], "10.1.1.1")
        self.assertEqual(r["offered"], "10.1.1.123")
        self.assertEqual(r["router"], ["10.1.1.1"])
        self.assertEqual(r["dns"], ["8.8.8.8"])
        self.assertEqual(r["lease_s"], 600)
        self.assertEqual(dhcpguard.network_of(r), "10.1.1.0/24")

    def test_other_traffic_is_ignored(self):
        f = offer_frame(ROGUE_MAC, "10.1.1.1", "10.1.1.123", 7)
        self.assertIsNone(dhcpguard.parse_reply(f, xid=8), "someone else's transaction")
        self.assertIsNone(dhcpguard.parse_reply(f[:60]))
        swapped = f[:34] + struct.pack("!HH", 68, 67) + f[38:]
        self.assertIsNone(dhcpguard.parse_reply(swapped), "a client's request, not a reply")


class Judge(unittest.TestCase):
    def srv(self, ip, offered, mac=ROGUE_MAC):
        return {"mac": mac, "ip": ip, "server_id": ip, "offered": offered}

    def test_the_farm_router_is_fine(self):
        self.assertEqual(dhcpguard.judge(self.srv(GW, "192.168.88.40", ROUTER_MAC), GW, TARGETS, []), "ok")

    def test_a_foreign_network_is_critical(self):
        self.assertEqual(dhcpguard.judge(self.srv("10.1.1.1", "10.1.1.123"), GW, TARGETS, []), "crit")
        self.assertEqual(dhcpguard.judge(self.srv("192.168.10.1", "192.168.10.62"), GW, TARGETS, []), "crit")

    def test_a_second_server_on_the_farm_network_is_a_warning(self):
        self.assertEqual(dhcpguard.judge(self.srv("192.168.88.2", "192.168.88.150"), GW, TARGETS, []), "warn")

    def test_allowed_by_mac_or_ip(self):
        self.assertEqual(dhcpguard.judge(self.srv("192.168.88.2", "192.168.88.150"), GW, TARGETS,
                                         [ROGUE_MAC.upper().replace(":", "-")]), "ok")
        self.assertEqual(dhcpguard.judge(self.srv("192.168.88.2", "192.168.88.150"), GW, TARGETS,
                                         ["192.168.88.2"]), "ok")


class LifeCycle(unittest.TestCase):
    def setUp(self):
        self.pushes, self.events, self.joined = [], [], False
        self._orig = (dhcpguard.default_route, dhcpguard.iface_mac, dhcpguard.notify.push,
                      dhcpguard.history.log_events, dhcpguard.hubvpn.has_config, dhcpguard.identify.vendor_for_mac)
        dhcpguard.default_route = lambda: ("eth0", GW)
        dhcpguard.iface_mac = lambda i: "2c:cf:67:20:a7:ee"
        dhcpguard.notify.push = lambda cfg, title, body, **kw: self.pushes.append((title, body, kw))
        dhcpguard.history.log_events = lambda rows: self.events.extend(r[1] for r in rows)
        dhcpguard.hubvpn.has_config = lambda: self.joined
        dhcpguard.identify.vendor_for_mac = lambda mac, online_ok=True: "Shenzhen Cudy" if mac == ROGUE_MAC else ""
        try:
            os.remove(dhcpguard.STATE_PATH)
        except OSError:
            pass
        self.answers = []
        self.g = dhcpguard.DhcpGuard(probe_fn=lambda iface, mac: [dict(a) for a in self.answers])
        self.g._mac_map = lambda: {ROGUE_MAC: {"switch_key": "sw1", "port": "0/3", "port_name": "Office AP", "uplink": False}}
        self.g._get_devices = lambda: [{"key": "sw1", "name": "Tower Switch"}]
        self.cfg = {"site": {"name": "Bennie"}, "targets": TARGETS, "alerts": {"ntfy_topic": "t"},
                    "dhcp_guard": {"enabled": True, "alerts": True, "allowed": []}}

    def tearDown(self):
        (dhcpguard.default_route, dhcpguard.iface_mac, dhcpguard.notify.push,
         dhcpguard.history.log_events, dhcpguard.hubvpn.has_config, dhcpguard.identify.vendor_for_mac) = self._orig

    ROUTER = {"mac": ROUTER_MAC, "ip": GW, "server_id": GW, "offered": "192.168.88.40", "mask": "255.255.255.0"}
    ROGUE = {"mac": ROGUE_MAC, "ip": "10.1.1.1", "server_id": "10.1.1.1", "offered": "10.1.1.123", "mask": "255.255.255.0"}

    def test_only_the_router_answers(self):
        self.answers = [self.ROUTER]
        self.assertEqual(self.g.check(self.cfg, now=1000), [])
        self.assertEqual(self.pushes, [])
        self.assertEqual(self.g.summary()["servers"], 1)

    def test_rogue_alerts_once_then_goes_after_two_quiet_checks(self):
        self.answers = [self.ROUTER, self.ROGUE]
        probs = self.g.check(self.cfg, now=1000)
        self.assertEqual(len(probs), 1)
        p = probs[0]
        self.assertEqual((p["type"], p["severity"], p["mac"], p["ip"]), ("rogue_dhcp", "high", ROGUE_MAC, "10.1.1.1"))
        self.assertIn("10.1.1.0/24", p["detail"])
        self.assertIn('Tower Switch port 0/3 "Office AP"', p["detail"])
        self.assertEqual(len(self.pushes), 1)
        self.assertIn("Shenzhen Cudy", self.pushes[0][1])
        self.assertEqual(self.events, ["rogue_dhcp"])

        self.g.check(self.cfg, now=1600)                      # still there: no repeat page
        self.assertEqual(len(self.pushes), 1)

        self.answers = [self.ROUTER]
        self.assertEqual(len(self.g.check(self.cfg, now=2200)), 1, "one quiet check is not 'gone'")
        self.assertEqual(self.g.check(self.cfg, now=2800), [])
        self.assertEqual(len(self.pushes), 2)
        self.assertIn("gone", self.pushes[1][0])
        self.assertEqual(self.events, ["rogue_dhcp", "rogue_dhcp_gone"])

    def test_it_repages_after_six_hours(self):
        self.answers = [self.ROGUE]
        self.g.check(self.cfg, now=1000)
        self.g.check(self.cfg, now=1000 + 6 * 3600 + 1)
        self.assertEqual(len(self.pushes), 2)

    def test_state_survives_a_restart(self):
        self.answers = [self.ROGUE]
        self.g.check(self.cfg, now=1000)
        g2 = dhcpguard.DhcpGuard(probe_fn=lambda i, m: [dict(self.ROGUE)])
        self.assertEqual(len(g2.problems()), 1)
        g2.check(self.cfg, now=1600)
        self.assertEqual(len(self.pushes), 1, "a container restart must not re-page")

    def test_a_joined_site_leaves_paging_to_the_hub(self):
        self.joined = True
        self.answers = [self.ROGUE]
        self.assertEqual(len(self.g.check(self.cfg, now=1000)), 1)
        self.assertEqual(self.pushes, [])
        self.assertEqual(self.g.summary()["rogues"][0]["level"], "crit")

    def test_allow_stops_the_flag(self):
        self.answers = [self.ROGUE]
        self.g.check(self.cfg, now=1000)
        cfg = self.g.allow(self.cfg, ROGUE_MAC.upper())
        self.assertEqual(cfg["dhcp_guard"]["allowed"], [ROGUE_MAC])
        self.assertEqual(self.g.problems(), [])
        self.g.check(cfg, now=1600)
        self.assertEqual(self.g.problems(), [])
        self.assertIsNone(self.g.allow(cfg, "nonsense"))

    def test_a_failed_probe_does_not_count_as_gone(self):
        self.answers = [self.ROGUE]
        self.g.check(self.cfg, now=1000)

        def boom(iface, mac):
            raise OSError("Operation not permitted")
        self.g._probe = boom
        for t in (1600, 2200, 2800):
            self.g.check(self.cfg, now=t)
        self.assertEqual(len(self.g.problems()), 1)
        self.assertIn("not permitted", self.g.summary()["error"])

    def test_disabled_does_nothing(self):
        self.cfg["dhcp_guard"]["enabled"] = False
        self.answers = [self.ROGUE]
        self.assertEqual(self.g.check(self.cfg, now=1000), [])
        self.assertTrue(self.g.summary()["disabled"])


if __name__ == "__main__":
    unittest.main()
