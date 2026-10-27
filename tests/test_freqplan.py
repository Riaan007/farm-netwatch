"""Per-tower frequency plan (app/freqplan.py).

Run inside the site image:
  docker run --rm -v "$PWD/app:/app" -v "$PWD/tests:/tests" -e NETWATCH_DATA=/tmp/nw \
    --entrypoint python farm-netwatch:netcfg /tests/test_freqplan.py -v
"""
import os
import sys
import tempfile
import unittest

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import freqplan as F    # noqa: E402


def r(mode, freq, width, *peers, ok=True):
    return {"ok": ok, "mode": mode, "sample": {"mode": mode, "freq": f"{freq} MHz" if freq else "", "chanbw": str(width)},
            "links": [{"peer": p.upper()} for p in peers]} if ok else {"ok": False, "error": "ssh"}


def devs(*keys):
    return {k: {"key": k, "mac": k, "name": k.split(":")[-1]} for k in keys}


A1, S1, A2, S2, A3, S3 = ("aa:00:00:00:00:01", "aa:00:00:00:00:02", "aa:00:00:00:00:03",
                          "aa:00:00:00:00:04", "aa:00:00:00:00:05", "aa:00:00:00:00:06")
TOWERS = {"t1": {"name": "Silo", "kind": "tower", "lat": -29.0, "lon": 27.0},
          "t2": {"name": "Newberry", "kind": "tower", "lat": -29.02, "lon": 27.01},      # ~2.4 km
          "t3": {"name": "Far Hill", "kind": "tower", "lat": -29.5, "lon": 27.5},        # ~70 km
          "b1": {"name": "Office", "kind": "building"}}


class Plan(unittest.TestCase):
    def test_bands_are_built_around_the_centre(self):
        """airOS freq is the CONTROL channel; Tweespruit .201 reads freq 5495 / centerFreq 5505 at 40 MHz."""
        ap = r("ap-ptp-ac", 5495, 40, S1)
        ap["sample"]["center"] = "5505"
        radios = {A1: ap, S1: r("sta-ptp-ac", 5495, 40, A1),
                  A2: r("ap-ptp-ac", 5540, 20, S2), S2: r("sta-ptp-ac", 5540, 20, A2)}
        out = F.analyse(TOWERS, {A1: "t1", A2: "t1"}, radios, devs(A1, S1, A2, S2))
        silo = next(t for t in out["towers"] if t["name"] == "Silo")
        L = next(x for x in silo["links"] if x["ap"] == A1)
        self.assertEqual((L["freq"], L["control"], L["lo"], L["hi"], L["centre_known"]), (5505, 5495, 5485, 5525, True))
        # 5525 → 5530 is a 5 MHz gap: a clash the control frequency (5475-5515) would have hidden
        self.assertEqual([(c["level"], c["gap"]) for c in silo["clashes"]], [("warn", 5.0)])
        self.assertEqual(out["summary"]["centre_unknown"], 0)

    def test_wide_link_without_a_centre_is_flagged(self):
        radios = {A1: r("ap-ptp-ac", 5480, 40, S1), S1: r("sta-ptp-ac", 5480, 40, A1)}
        out = F.analyse(TOWERS, {A1: "t1"}, radios, devs(A1, S1))
        self.assertEqual(out["summary"]["centre_unknown"], 1)

    def test_overlap_on_one_tower_is_critical_and_gets_a_clean_move(self):
        radios = {A1: r("ap-ptmp-ac", 5480, 40, S1), S1: r("sta-ptmp-ac", 5480, 40, A1),
                  A2: r("ap-ptp-ac", 5500, 40, S2), S2: r("sta-ptp-ac", 5500, 40, A2)}
        out = F.analyse(TOWERS, {A1: "t1", A2: "t1", S1: "b1", S2: "t3"}, radios, devs(A1, S1, A2, S2))
        silo = next(t for t in out["towers"] if t["name"] == "Silo")
        self.assertEqual(len(silo["clashes"]), 1)
        self.assertEqual(silo["clashes"][0]["level"], "crit")
        self.assertTrue(all(x["clash"] for x in silo["links"]))
        self.assertEqual(len(out["moves"]), 1)
        m = out["moves"][0]
        self.assertIsNotNone(m["to"])
        self.assertFalse(m["dfs"])                                   # a non-DFS channel is preferred
        other = 5500 if m["ap"] == A1 else 5480
        self.assertGreaterEqual(F._gap(F._band(m["to"], 40), F._band(other, 40)), F.GUARD_MHZ)
        self.assertEqual(out["summary"]["clashes"], 1)

    def test_far_towers_may_reuse_a_channel(self):
        radios = {A1: r("ap-ptp-ac", 5480, 40, S1), S1: r("sta-ptp-ac", 5480, 40, A1),
                  A3: r("ap-ptp-ac", 5480, 40, S3), S3: r("sta-ptp-ac", 5480, 40, A3)}
        out = F.analyse(TOWERS, {A1: "t1", A3: "t3"}, radios, devs(A1, S1, A3, S3))
        self.assertEqual(out["summary"]["clashes"], 0)
        self.assertEqual(out["moves"], [])

    def test_near_towers_overlapping_is_a_warning(self):
        radios = {A1: r("ap-ptp-ac", 5480, 40, S1), S1: r("sta-ptp-ac", 5480, 40, A1),
                  A2: r("ap-ptp-ac", 5490, 20, S2), S2: r("sta-ptp-ac", 5490, 20, A2)}
        out = F.analyse(TOWERS, {A1: "t1", A2: "t2"}, radios, devs(A1, S1, A2, S2))
        silo = next(t for t in out["towers"] if t["name"] == "Silo")
        self.assertEqual(silo["near"], ["Newberry"])
        self.assertEqual([(c["level"], c["where"]) for c in silo["clashes"]], [("warn", "near")])

    def test_station_on_the_tower_moves_its_ap(self):
        # only the station sits on this tower; the plan moves the link's AP
        radios = {A1: r("ap-ptmp-ac", 5180, 20, S1), S1: r("sta-ptmp-ac", 5180, 20, A1),
                  A2: r("ap-ptp-ac", 5185, 20, S2), S2: r("sta-ptp-ac", 5185, 20, A2)}
        out = F.analyse(TOWERS, {S1: "t1", A2: "t1"}, radios, devs(A1, S1, A2, S2))
        self.assertTrue(out["moves"])
        self.assertTrue(all(m["ap"] in (A1, A2) for m in out["moves"]))

    def test_unreadable_ap_is_not_moved(self):
        radios = {A1: r("ap-ptp-ac", 5480, 40, S1, ok=False), S1: r("sta-ptp-ac", 5480, 40, A1),
                  A2: r("ap-ptp-ac", 5480, 40, S2, ok=False), S2: r("sta-ptp-ac", 5480, 40, A2)}
        out = F.analyse(TOWERS, {S1: "t1", S2: "t1"}, radios, devs(A1, S1, A2, S2))
        self.assertEqual(out["summary"]["clashes"], 1)               # the clash is still reported
        self.assertEqual(out["moves"], [])                            # but nothing is proposed blind

    def test_tweespruit_a_move_respects_every_tower(self):
        """Riaan, 2026-10-26: moving .201 (Hoepel Rock + Newburry) to 5230/40 would sit 5 MHz from
        Silo's 5185/40 — its Hoepel Rock end is 7.5 km from Silo, within NEAR_KM. Real channels + GPS."""
        T = {"hoep": {"name": "Hoepel Rock", "kind": "tower", "lat": -29.113113, "lon": 27.176688},
             "newb": {"name": "Newburry", "kind": "tower", "lat": -29.2528, "lon": 27.201783},
             "silo": {"name": "Silo", "kind": "tower", "lat": -29.176574, "lon": 27.14919},
             "haz": {"name": "Hazeldene - Link", "kind": "tower", "lat": -29.267595, "lon": 27.112272},
             "beth": {"name": "Bethal", "kind": "tower", "lat": -29.088951, "lon": 27.206694}}
        k = lambda n: "bb:00:00:00:%02x:%02x" % (n // 256, n % 256)       # noqa: E731
        ap201, st201, ap213, st213, ap203, st203 = k(201), k(1201), k(213), k(1213), k(203), k(1203)
        ap222, st222, ap221, st221, ap224, st224, ap212, st212, ap204, st204 = (
            k(222), k(1222), k(221), k(1221), k(224), k(1224), k(212), k(1212), k(204), k(1204))
        radios = {ap201: r("ap-ptp-ac", 5480, 40, st201), st201: r("sta-ptp-ac", 5480, 40, ap201),
                  ap213: r("ap-ptp-ac", 5480, 20, st213), ap203: r("ap-ptp-ac", 5480, 20, st203),
                  ap222: r("ap-ptp-ac", 5530, 40, st222), st222: r("sta-ptp-ac", 5530, 40, ap222),
                  ap221: r("ap-ptmp-ac", 5185, 40, st221), ap224: r("ap-ptp-ac", 5800, 20, st224),
                  st224: r("sta-ptp-ac", 5800, 20, ap224), ap212: r("ap-ptp-ac", 5700, 20, st212),
                  ap204: r("ap-ptp-ac", 5180, 20, st204)}
        group_of = {ap201: "hoep", st201: "newb", ap213: "hoep", ap203: "newb", ap222: "silo", st222: "newb",
                    ap221: "silo", ap224: "silo", st224: "haz", ap212: "hoep", st212: "beth", ap204: "haz"}
        out = F.analyse(T, group_of, radios, devs(*radios, st213, st203, st221, st212, st204))
        newb = next(t for t in out["towers"] if t["name"] == "Newburry")
        self.assertNotIn("Silo", newb["near"])                     # 9.9 km apart: the label goes by tower GPS
        self.assertIn(ap221, out["near_links"][ap201])              # but .201 (Hoepel Rock end) is 7.5 km from Silo
        moves = {m["ap"]: m for m in out["moves"]}
        self.assertTrue(moves)
        self.assertFalse([m for m in moves.values() if m["to"] == 5230 and m["width"] == 40])
        final = {ap: F._band(moves[ap]["to"], moves[ap]["width"]) if ap in moves else F._band(f, w)
                 for ap, f, w in ((ap201, 5480, 40), (ap213, 5480, 20), (ap203, 5480, 20), (ap222, 5530, 40),
                                  (ap221, 5185, 40), (ap224, 5800, 20), (ap212, 5700, 20), (ap204, 5180, 20))}
        for ap, m in moves.items():                                 # every move is clear of EVERY tower
            self.assertTrue(m["to"], m)
            self.assertIn("every tower", m["checked"])
            for other, band in final.items():
                if other != ap:
                    self.assertGreaterEqual(F._gap(final[ap], band), F.GUARD_MHZ, (m, other, band))

    def test_links_are_named_by_hostname_not_ip(self):
        # the scanner only knows the AP by IP; its station reports the AP's hostname over the link
        radios = {A1: r("ap-ptp-ac", 5480, 20, S1), S1: r("sta-ptp-ac", 5480, 20, A1)}
        radios[S1]["links"][0]["name"] = "Nubery-Link-AP"
        devices = {A1: {"key": A1, "mac": A1, "name": "192.168.1.201", "ip": "192.168.1.201"}, S1: {"key": S1, "mac": S1, "name": "Sta"}}
        out = F.analyse(TOWERS, {A1: "t1"}, radios, devices)
        self.assertEqual(out["towers"][0]["links"][0]["name"] if out["towers"][0]["links"] else
                         next(t for t in out["towers"] if t["links"])["links"][0]["name"], "Nubery-Link-AP")

    def test_other_networks_steer_a_move_to_a_quiet_channel(self):
        # a clash on Silo; the station scanned and hears a loud neighbour on the nearest free channels
        radios = {A1: r("ap-ptp-ac", 5180, 20, S1), S1: r("sta-ptp-ac", 5180, 20, A1),
                  A2: r("ap-ptp-ac", 5185, 20, S2), S2: r("sta-ptp-ac", 5185, 20, A2)}
        loud = [{"bssid": "11:22:33:44:55:%02X" % i, "ssid": "Neighbour", "freq": f, "width": 20, "lo": f - 10, "hi": f + 10,
                 "signal": -55} for i, f in enumerate(range(5200, 5350, 20))]
        scans = {S1: {"ts": 1, "name": "Sta", "networks": loud}}
        out = F.analyse(TOWERS, {A1: "t1", A2: "t1", S1: "t1"}, radios, devs(A1, S1, A2, S2), scans=scans)
        m = next(m for m in out["moves"] if m["to"])
        self.assertTrue(all(F._gap(F._band(m["to"], 20), (n["lo"], n["hi"])) >= 5 for n in loud), m)
        self.assertEqual(m["heard_to"], 0)
        silo = next(t for t in out["towers"] if t["name"] == "Silo")
        self.assertEqual(len(silo["others"]), len(loud))
        self.assertEqual(silo["scan_from"], [{"key": S1, "name": "02"}])
        self.assertEqual(out["summary"]["others"], len(loud))

    def test_a_link_on_a_busy_channel_gets_a_quieter_one(self):
        radios = {A1: r("ap-ptp-ac", 5500, 20, S1), S1: r("sta-ptp-ac", 5500, 20, A1)}
        scans = {S1: {"ts": 1, "networks": [{"bssid": "11:22:33:44:55:66", "ssid": "X", "freq": 5500, "width": 20,
                                             "lo": 5490, "hi": 5510, "signal": -60}]}}
        out = F.analyse(TOWERS, {A1: "t1", S1: "t1"}, radios, devs(A1, S1), scans=scans)
        self.assertEqual([(m["why"], m["heard_from"], m["heard_to"]) for m in out["moves"]], [("busy", 1, 0)])
        L = out["towers"][0]["links"][0] if out["towers"][0]["links"] else next(t for t in out["towers"] if t["links"])["links"][0]
        self.assertEqual(L["heard"], 1)
        self.assertTrue(L["best"] and L["best"][0]["heard"] == 0)

    def test_our_own_radios_are_not_other_networks(self):
        radios = {A1: r("ap-ptp-ac", 5500, 20, S1), S1: r("sta-ptp-ac", 5500, 20, A1)}
        radios[A1]["sample"]["ssid"] = "Tower"
        scans = {S1: {"ts": 1, "networks": [
            {"bssid": A1.upper(), "ssid": "?", "freq": 5500, "width": 20, "lo": 5490, "hi": 5510, "signal": -50},
            {"bssid": "11:22:33:44:55:77", "ssid": "Tower", "freq": 5600, "width": 20, "lo": 5590, "hi": 5610, "signal": -50}]}}
        out = F.analyse(TOWERS, {A1: "t1", S1: "t1"}, radios, devs(A1, S1), scans=scans)
        self.assertEqual(out["summary"]["others"], 0)
        self.assertEqual(out["moves"], [])

    # Tweespruit's Network-map GPS (2026-10-27): towers + the poles their stations sit on.
    TW = {"silo": {"name": "Silo", "kind": "tower", "lat": -29.176574, "lon": 27.14919},
          "west": {"name": "Westminster T", "kind": "tower", "lat": -29.175771, "lon": 27.144816},
          "n8": {"name": "603/712 N8 T", "kind": "tower", "lat": -29.189528, "lon": 27.153321},
          "haz": {"name": "Hazeldene - Link", "kind": "tower", "lat": -29.267595, "lon": 27.112272},
          "hazcam": {"name": "Hazeldene - Camera", "kind": "pole", "lat": -29.254555, "lon": 27.087842},
          "hoep": {"name": "Hoepel Rock", "kind": "tower", "lat": -29.113113, "lon": 27.176688},
          "newb": {"name": "Newburry", "kind": "tower", "lat": -29.2528, "lon": 27.201783}}
    K = staticmethod(lambda n: "cc:00:00:00:%02x:%02x" % (n // 256, n % 256))

    def _sectors(self, f221, f204, w221=40, **kw):
        """Sec120 on Silo (stations on Westminster + 603/712), Sec90 on Hazeldene - Link (station at the
        Hazeldene camera pole), and the Silo↔Hazeldene link that joins the two towers."""
        k = self.K
        ap221, w, n8, ap204, hc, ap224, st224 = k(221), k(120), k(130), k(204), k(150), k(224), k(223)
        radios = {ap221: r("ap-ptmp-ac", f221, w221, w, n8), w: r("sta-ptmp-ac", f221, w221, ap221),
                  n8: r("sta-ptmp-ac", f221, w221, ap221), ap204: r("ap-ptmp-ac", f204, 20, hc),
                  hc: r("sta-ptmp-ac", f204, 20, ap204), ap224: r("ap-ptp-ac", 5800, 20, st224),
                  st224: r("sta-ptp-ac", 5800, 20, ap224)}
        group_of = {ap221: "silo", w: "west", n8: "n8", ap204: "haz", hc: "hazcam", ap224: "silo", st224: "haz"}
        group_of.update(kw.pop("group_of", {}))
        return radios, group_of, ap221, ap204

    def test_far_sectors_may_share_a_channel_though_their_towers_are_linked(self):
        """Riaan 2026-10-27: Silo (Sec120) and Hazeldene (Sec90) are not close — closest radios 9.6 km."""
        radios, group_of, ap221, ap204 = self._sectors(5185, 5180)
        out = F.analyse(self.TW, group_of, radios, devs(*radios))
        self.assertEqual(out["summary"]["clashes"], 0, [t["clashes"] for t in out["towers"]])
        self.assertEqual(out["moves"], [])
        silo = next(t for t in out["towers"] if t["name"] == "Silo")
        self.assertNotIn("Hazeldene - Link", silo["near"])           # linked, but its links are all far
        self.assertNotIn(ap204, out["near_links"].get(ap221, []))

    def test_without_gps_linked_towers_are_still_near(self):
        bare = {g: {"name": v["name"], "kind": v["kind"]} for g, v in self.TW.items()}
        radios, group_of, ap221, ap204 = self._sectors(5185, 5180)
        out = F.analyse(bare, group_of, radios, devs(*radios))
        silo = next(t for t in out["towers"] if t["name"] == "Silo")
        self.assertEqual([(c["where"], c["level"]) for c in silo["clashes"]], [("near", "warn")])

    def test_a_link_within_8_km_still_clashes(self):
        """Riaan 2026-10-26: the Hoepel Rock–Newburry link (.201) can interfere with Sec120 Silo — 7.5 km."""
        k = self.K
        radios, group_of, ap221, ap204 = self._sectors(5230, 5180)
        ap201, st201 = k(201), k(1201)
        radios.update({ap201: r("ap-ptp-ac", 5230, 40, st201), st201: r("sta-ptp-ac", 5230, 40, ap201)})
        group_of.update({ap201: "hoep", st201: "newb"})
        out = F.analyse(self.TW, group_of, radios, devs(*radios))
        silo = next(t for t in out["towers"] if t["name"] == "Silo")
        hit = [c for c in silo["clashes"] if {c["a"], c["b"]} == {ap221, ap201}]
        self.assertEqual(len(hit), 1)
        self.assertEqual((hit[0]["where"], hit[0]["level"], hit[0]["km"]), ("near", "warn", 7.5))
        for m in out["moves"]:                                       # a move never lands within 10 MHz of it
            other = F._band(5230, 40) if m["ap"] == ap221 else F._band(5230, 40)
            self.assertGreaterEqual(F._gap(F._band(m["to"], 40), other), F.GUARD_MHZ, m)

    def test_a_device_pin_beats_its_group(self):
        """A station pinned 1 km from the Hazeldene sector makes Sec120 near it, whatever its pole says."""
        radios, group_of, ap221, ap204 = self._sectors(5185, 5180)
        out = F.analyse(self.TW, group_of, radios, devs(*radios),
                        geo={self.K(130): {"lat": -29.262, "lon": 27.110}})
        self.assertIn(ap204, out["near_links"].get(ap221, []))
        haz = next(t for t in out["towers"] if t["name"] == "Hazeldene - Link")
        self.assertEqual([({c["a"], c["b"]}, c["where"]) for c in haz["clashes"]], [({ap204, ap221}, "near")])

    def test_best_channels_may_reuse_a_far_link(self):
        """A non-DFS channel shared with a link 9.6 km away beats a clean DFS one, and says so.
        Sec120 at 80 MHz: Silo's own Hazeldene link at 5800 leaves no room in 5725-5850, so the only
        non-DFS room left (5150-5250) is where far-away Sec90 sits."""
        radios, group_of, ap221, ap204 = self._sectors(5530, 5190, w221=80)
        out = F.analyse(self.TW, group_of, radios, devs(*radios))
        silo = next(t for t in out["towers"] if t["name"] == "Silo")
        best = next(x for x in silo["links"] if x["ap"] == ap221)["best"]
        self.assertFalse(best[0]["dfs"], best)
        self.assertEqual([(r_["ap"], r_["km"]) for r_ in best[0]["reuses"]], [(ap204, 9.6)])
        self.assertTrue(all(not b["dfs"] and b["reuses"] for b in best), best)   # ahead of every clean DFS channel

    def test_candidates_stay_inside_the_band(self):
        c = F.candidates(80)
        self.assertTrue(all(any(lo <= x - 40 and x + 40 <= hi for lo, hi in F.RANGES) for x in c))
        self.assertNotIn(5400, F.candidates(20))                      # 5350-5470 is not allowed
        self.assertTrue(F.is_dfs(*F._band(5500, 20)))
        self.assertFalse(F.is_dfs(*F._band(5180, 20)))


if __name__ == "__main__":
    unittest.main()
