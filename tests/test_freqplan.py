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

    def test_candidates_stay_inside_the_band(self):
        c = F.candidates(80)
        self.assertTrue(all(any(lo <= x - 40 and x + 40 <= hi for lo, hi in F.RANGES) for x in c))
        self.assertNotIn(5400, F.candidates(20))                      # 5350-5470 is not allowed
        self.assertTrue(F.is_dfs(*F._band(5500, 20)))
        self.assertFalse(F.is_dfs(*F._band(5180, 20)))


if __name__ == "__main__":
    unittest.main()
