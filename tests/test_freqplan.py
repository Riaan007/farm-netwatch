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
        Silo's 5185/40 — Silo is 8.5 km from Newburry but joined to it by a link. Real channels + GPS."""
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
        self.assertIn("Silo", newb["near"])                        # joined by the .222 link, 8.5 km apart
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

    def test_candidates_stay_inside_the_band(self):
        c = F.candidates(80)
        self.assertTrue(all(any(lo <= x - 40 and x + 40 <= hi for lo, hi in F.RANGES) for x in c))
        self.assertNotIn(5400, F.candidates(20))                      # 5350-5470 is not allowed
        self.assertTrue(F.is_dfs(*F._band(5500, 20)))
        self.assertFalse(F.is_dfs(*F._band(5180, 20)))


if __name__ == "__main__":
    unittest.main()
