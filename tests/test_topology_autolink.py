"""Switch ports linked to the device plugged into them (app/topology.py):
the "first device behind a busy port" rules and drawing wired links straight
away (auto_link_wired) — deleting one keeps it deleted.

Run inside the site image:
  docker run --rm -v "$PWD/app:/app" -v "$PWD/tests:/tests" -e NETWATCH_DATA=/tmp/nw \
    --entrypoint python farm-netwatch:netcfg /tests/test_topology_autolink.py -v
"""
import os
import sys
import tempfile
import time
import unittest

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import topology as T      # noqa: E402

NOW = int(time.time())
SW = "bb:00:00:00:00:01"
PI = "dc:a6:32:00:00:99"


def dev(key, ip, cat, name, rtt=None, vendor="Ubiquiti", model="", **kw):
    d = {"key": key, "ip": ip, "mac": key, "category": cat, "name": name, "online": True, "last_seen": NOW,
         "vendor": vendor, "model": model, "rtt": rtt}
    d.update(kw)
    return d


BASE = [dev(SW, "10.0.0.1", "network", "Tower Switch", 0.4, model="ES-8-150W", is_switch=True)]


def radio(mode, *peers):
    return {"ok": True, "ts": NOW, "mode": mode, "sample": {"mode": mode},
            "links": [{"peer": p.upper(), "signal": -60.0} for p in peers]}


def port(pid, *macs):
    return {"id": pid, "name": pid, "macs": list(macs)}


def build(devices, ports, radios, d=None, self_macs=(), uplinks=None):
    d = d or T.normalize({})
    sw = {SW: {"ts": NOW, "uplinks": uplinks or {}, "ports": ports}}
    return d, T.build(d, BASE + devices, {}, radios=radios, switches=sw, now=NOW, self_macs=self_macs)


def wired(g):
    return {(s["a_port"], s["b"]): s for s in g["suggestions"] if s["type"] == "link" and s["medium"] == "ethernet"}


class FirstBehind(unittest.TestCase):
    def test_sector_ap_on_a_busy_port(self):
        ap, s1, s2 = "cc:00:00:00:00:01", "cc:00:00:00:00:02", "cc:00:00:00:00:03"
        devices = [dev(ap, "10.0.0.11", "network", "Sector 120", model="LAP-120"),
                   dev(s1, "10.0.0.12", "network", "Gate", model="LiteBeam 5AC"),
                   dev(s2, "10.0.0.13", "network", "Dam", model="PowerBeam 5AC"),
                   dev("cd:00:00:00:00:01", "10.0.0.20", "internet-ap", "Gate hotspot", vendor="Cudy"),
                   dev("ee:00:00:00:00:01", "10.0.0.31", "camera", "Cam 1", vendor="Hikvision"),
                   dev("ee:00:00:00:00:02", "10.0.0.32", "camera", "Cam 2", vendor="Hikvision")]
        radios = {ap: radio("ap-ptmp-ac", s1, s2), s1: radio("sta-ptmp-ac", ap), s2: radio("sta-ptmp-ac", ap)}
        _, g = build(devices, [port("0/6", *[x["key"] for x in devices])], radios)
        w = wired(g)
        self.assertIn(("0/6", ap), w)
        self.assertIn("access point serving the other 2 radios", w[("0/6", ap)]["evidence"])
        self.assertEqual(len(w), 1)

    def test_a_managed_switch_on_the_port_means_no_guess(self):
        ap, s1 = "cc:00:00:00:00:01", "cc:00:00:00:00:02"
        devices = [dev(ap, "10.0.0.11", "network", "Sector", model="LAP-120"),
                   dev(s1, "10.0.0.12", "network", "Gate", model="LiteBeam 5AC"),
                   dev("bb:00:00:00:00:02", "10.0.0.2", "network", "Office Switch", model="ES-8-150W", is_switch=True)]
        radios = {ap: radio("ap-ptmp-ac", s1), s1: radio("sta-ptmp-ac", ap)}
        _, g = build(devices, [port("0/6", *[x["key"] for x in devices])], radios)
        self.assertEqual(wired(g), {})

    def ptp(self, rtt_a, rtt_b, pi_behind):
        a, b = "cc:00:00:00:00:0a", "cc:00:00:00:00:0b"
        devices = [dev(a, "10.0.0.42", "network", "Main->Office", rtt_a, model="PowerBeam 5AC"),
                   dev(b, "10.0.0.28", "network", "Office->Main", rtt_b, model="PowerBeam 5AC"),
                   dev("ee:00:00:00:00:09", "10.0.0.50", "camera", "Office cam", vendor="Hikvision")]
        radios = {a: radio("ap-ptp-ac", b), b: radio("sta-ptp-ac", a)}
        macs = [a, b, "ee:00:00:00:00:09"] + ([PI] if pi_behind else [])
        _, g = build(devices, [port("0/3", *macs)], radios, self_macs=[PI] if pi_behind else [])
        return {k[1] for k in wired(g)}

    def test_ptp_pair_uses_response_time_and_where_the_pi_is(self):
        # Pi elsewhere: the nearer radio is on the switch
        self.assertEqual(self.ptp(0.6, 3.1, pi_behind=False), {"cc:00:00:00:00:0a"})
        # Pi behind the port (Bennie): the radio near the Pi answers fastest, so the switch has the FARTHER one
        self.assertEqual(self.ptp(2.4, 0.2, pi_behind=True), {"cc:00:00:00:00:0a"})
        # too close to tell → nothing drawn
        self.assertEqual(self.ptp(1.0, 1.5, pi_behind=False), set())

    def test_one_network_device_on_an_uplink_port(self):
        r = "cc:00:00:00:00:21"
        devices = [dev(r, "10.0.0.21", "network", "PTZ link", model="PowerBeam 5AC")] + \
                  [dev(f"ee:00:00:00:01:{i:02x}", f"10.0.1.{i}", "camera", f"Cam {i}", vendor="Hikvision") for i in range(7)]
        _, g = build(devices, [port("0/2", *[x["key"] for x in devices])], {}, uplinks={"0/2": 8})
        self.assertIn(("0/2", r), wired(g))


class AutoLink(unittest.TestCase):
    def setUp(self):
        self.nvr = "ee:00:00:00:00:21"
        self.ap, self.sta = "cc:00:00:00:00:31", "cc:00:00:00:00:32"
        self.devices = [dev(self.nvr, "10.0.0.250", "nvr", "NVR", vendor="Hikvision"),
                        dev(self.ap, "10.0.0.31", "network", "AP", model="PowerBeam 5AC"),
                        dev(self.sta, "10.0.0.32", "network", "STA", model="PowerBeam 5AC")]
        self.radios = {self.ap: radio("ap-ptp-ac", self.sta)}
        self.ports = [port("0/2", self.nvr)]

    def test_wired_links_are_drawn_radio_links_stay_suggestions(self):
        d, g = build(self.devices, self.ports, self.radios)
        devmap = {x["key"]: x for x in BASE + self.devices}
        self.assertEqual(T.auto_link_wired(d, g["suggestions"], devmap, self.radios), 1)
        links = list(d["links"].values())
        self.assertEqual(len(links), 1)
        L = links[0]
        self.assertEqual((L["a"], L["a_port"], L["b"], L["source"]), (SW, "0/2", self.nvr, "discovered"))
        self.assertTrue(L["sug"].startswith("s-"))
        _, g2 = build(self.devices, self.ports, self.radios, d=d)
        self.assertFalse([s for s in g2["suggestions"] if s.get("b") == self.nvr])      # no duplicate
        self.assertTrue([s for s in g2["suggestions"] if s["medium"] == "wireless"])   # still a suggestion
        self.assertIs(T.auto_link_wired(d, g2["suggestions"], devmap, self.radios), T.NOCHANGE)

    def test_deleting_a_drawn_link_keeps_it_deleted(self):
        d, g = build(self.devices, self.ports, self.radios)
        devmap = {x["key"]: x for x in BASE + self.devices}
        T.auto_link_wired(d, g["suggestions"], devmap, self.radios)
        lid = next(iter(d["links"]))
        T.delete_link(d, lid)
        _, g2 = build(self.devices, self.ports, self.radios, d=d)
        self.assertIs(T.auto_link_wired(d, g2["suggestions"], devmap, self.radios), T.NOCHANGE)
        self.assertEqual(d["links"], {})


if __name__ == "__main__":
    unittest.main()
