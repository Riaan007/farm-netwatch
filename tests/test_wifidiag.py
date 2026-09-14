"""Wireless link diagnosis (app/wifidiag.py) on synthetic radio telemetry.

Run inside the site image:
  docker run --rm -v "$PWD/app:/app" -v "$PWD/tests:/tests" -e NETWATCH_DATA=/tmp/nw \
    --entrypoint python farm-netwatch:netcfg /tests/test_wifidiag.py -v
"""
import os
import sys
import tempfile
import unittest

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import wifidiag    # noqa: E402

NOW = 1_800_000_000
STEP = 900
N = 7 * 96                          # a week of 15-minute polls
AP, STA, STA2, BLIND = "58:d6:1f:00:00:01", "0c:ea:14:00:00:02", "0c:ea:14:00:00:03", "0c:ea:14:00:00:04"
HOUR = lambda ts: (ts // 3600) % 24          # noqa: E731 - UTC hours keep the test deterministic


def ts_list():
    return [NOW - (N - 1 - i) * STEP for i in range(N)]


def link_rows(peer, name, model, sig, rsig, dl, ul, dist=1350, skip=()):
    out = []
    for i, ts in enumerate(ts_list()):
        if i in skip:
            continue
        v = lambda x: x(i, ts) if callable(x) else x   # noqa: E731
        out.append({"ts": ts, "peer": peer.upper(), "name": name, "model": model, "ip": "",
                    "signal": v(sig), "remote_signal": v(rsig), "score_dl": v(dl), "score_ul": v(ul),
                    "tx": 144.4, "rx": 144.4, "latency": 2, "distance": dist})
    return out


def radio_rows(mode, c0=-60, c1=-61, airtime=5, cap=110000, noise=-93):
    return [{"ts": ts, "mode": mode, "chain0": c0, "chain1": c1, "airtime": airtime,
             "cap_dl": cap, "noise": noise, "freq": "5600 MHz", "chanbw": "20"} for ts in ts_list()]


def radio(key, name, model, mode, rows, links, ok=True, error=None):
    cur = dict(rows[-1]) if rows else {}
    return {"key": key, "name": name, "ip": "192.168.0." + key[-1], "model": model, "ok": ok, "error": error,
            "mode": "Access Point" if mode.startswith("ap") else "Station", "current": cur,
            "stats": {"noise": wifidiag._stat(rows, "noise", NOW), "airtime": wifidiag._stat(rows, "airtime", NOW)},
            "rows": rows, "links": [{"peer": ln[0]["peer"], "name": ln[0]["name"], "model": ln[0]["model"],
                                     "rows": ln} for ln in links]}


def build(radios):
    return wifidiag.build(radios, now=NOW, hour_of=HOUR)


def ids(link):
    return {f["id"]: f for f in link["findings"]}


class PairingTest(unittest.TestCase):
    def test_both_views_of_one_link_become_one_link(self):
        ap = radio(AP, "192.168.0.1", "LiteAP AC", "ap-ptmp-ac", radio_rows("ap-ptmp-ac"),
                   [link_rows(STA, "Kliphuis", "PowerBeam 5AC", -55, -56, 94, 92)])
        sta = radio(STA, "", "", "sta-ptmp-ac", radio_rows("sta-ptmp-ac"),
                    [link_rows(AP, "SwartRigens", "LiteAP AC", -56, -55, 94, 92)])
        d = build([ap, sta])
        self.assertEqual(len(d["links"]), 1)
        L = d["links"][0]
        self.assertEqual((L["ap"]["name"], L["sta"]["name"]), ("SwartRigens", "Kliphuis"))
        self.assertEqual(L["name"], "SwartRigens → Kliphuis")
        self.assertEqual(L["grade"], "good")
        self.assertEqual(L["findings"], [])
        self.assertEqual(L["quality"], 92)
        self.assertEqual(L["metrics"]["signal_ap"]["now"], -55)
        self.assertEqual(L["metrics"]["signal_sta"]["now"], -56)
        self.assertEqual(sta["model"], "PowerBeam 5AC")          # learned from what the AP sees
        self.assertLessEqual(len(L["series"]["ts"]), wifidiag.MAX_POINTS)
        self.assertGreater(len(L["series"]["ts"]), wifidiag.MAX_POINTS // 2)

    def test_unreadable_far_end_is_filled_from_the_other_view(self):
        ap = radio(AP, "AP", "LiteAP AC", "ap-ptmp-ac", radio_rows("ap-ptmp-ac"),
                   [link_rows(BLIND, "B1", "PowerBeam 5AC", -52, -55, 94, 90)])
        L = build([ap])["links"][0]
        self.assertFalse(L["sta"]["monitored"])
        self.assertEqual(L["metrics"]["signal_sta"]["now"], -55)
        self.assertIn("blind_end", ids(L))
        self.assertEqual(L["grade"], "good")         # a missing login alone is not a fault


class RulesTest(unittest.TestCase):
    def test_strong_signal_poor_score_points_away_from_alignment(self):
        ap = radio(AP, "AP", "LiteAP AC", "ap-ptmp-mixed", radio_rows("ap-ptmp-mixed"),
                   [link_rows(STA, "Kliphuis", "PowerBeam 5AC", -58, -58, 58, 55),
                    link_rows(STA2, "Kliphuis-Pole", "PowerBeam 5AC Gen2", -59, -60, 60, 56)])
        sta = radio(STA, "Kliphuis", "PowerBeam 5AC", "sta-ptmp-ac", radio_rows("sta-ptmp-ac", c0=-67, c1=-61),
                    [link_rows(AP, "SwartRigens", "LiteAP AC", -58, -58, 58, 55)])
        d = build([ap, sta])
        L = next(x for x in d["links"] if x["sta"]["name"] == "Kliphuis")
        f = ids(L)
        self.assertIn("poor_quality", f)
        self.assertNotIn("weak_signal", f)
        self.assertEqual(f["chain_gap"]["level"], "info")         # 6 dB: watch, not alert
        self.assertTrue(f["poor_quality"]["causes"][0].startswith("The antenna chains differ"))
        self.assertTrue(any("Mixed" in c for c in f["poor_quality"]["causes"]))
        ap_out = next(r for r in d["radios"] if r["key"] == AP)
        self.assertEqual([x["id"] for x in ap_out["findings"]], ["mixed_mode"])
        self.assertEqual(d["shared"], [{"ap": "AP", "stations": ["Kliphuis", "Kliphuis-Pole"]}])
        self.assertEqual(d["fixes"][0]["where"], "remote")          # office work comes first
        self.assertEqual([f["kind"] for f in d["fixes"] if f["level"] == "warn"][:2], ["radio", "shared"])
        self.assertEqual(sum(1 for f in d["fixes"] if f.get("link") == L["id"]), 1)   # one entry per link

    def test_mixed_mode_is_left_alone_when_an_old_station_needs_it(self):
        ap = radio(AP, "AP", "LiteAP AC", "ap-ptmp-mixed", radio_rows("ap-ptmp-mixed"),
                   [link_rows(STA, "Old", "NanoStation M5", -60, -60, 90, 90)])
        self.assertEqual(build([ap])["radios"][0]["findings"], [])

    def test_weak_signal_is_critical_below_minus_82(self):
        ap = radio(AP, "AP", "PowerBeam 5AC", "ap-ptp-ac", radio_rows("ap-ptp-ac"),
                   [link_rows(STA, "Far", "PowerBeam 5AC", -84, -83, 40, 38, dist=9000)])
        L = build([ap])["links"][0]
        self.assertEqual(ids(L)["weak_signal"]["level"], "crit")
        self.assertEqual(L["grade"], "crit")
        self.assertEqual(L["fresnel"], wifidiag.fresnel(9000, 5600))

    def test_one_sided_signal_drop_blames_the_quiet_transmitter(self):
        # The station hears the AP 12 dB less than it used to; the AP still hears the station fine.
        drop = lambda i, ts: -55 if ts < NOW - 2 * 3600 else -67   # noqa: E731
        ap = radio(AP, "AP", "LiteAP AC", "ap-ptmp-ac", radio_rows("ap-ptmp-ac"),
                   [link_rows(STA, "Sta", "PowerBeam 5AC", -55, drop, 90, 90)])
        f = ids(build([ap])["links"][0])["signal_drop"]
        self.assertEqual(f["level"], "crit")
        self.assertIn("AP is transmitting less", f["causes"][0])
        self.assertEqual(f["steps"][0]["where"], "remote")

    def test_both_ends_dropping_means_path_or_aim(self):
        drop = lambda i, ts: -52 if ts < NOW - 2 * 3600 else -60   # noqa: E731
        ap = radio(AP, "AP", "LiteAP AC", "ap-ptmp-ac", radio_rows("ap-ptmp-ac"),
                   [link_rows(STA, "Sta", "PowerBeam 5AC", drop, drop, 90, 90)])
        f = ids(build([ap])["links"][0])["signal_drop"]
        self.assertIn("path or the aim", f["causes"][0])

    def test_upload_worse_than_download_points_at_the_ap(self):
        ap = radio(AP, "AP", "LiteAP AC", "ap-ptmp-ac", radio_rows("ap-ptmp-ac"),
                   [link_rows(STA, "Sta", "PowerBeam 5AC", -55, -55, 90, 45)])
        f = ids(build([ap])["links"][0])["asymmetric_quality"]
        self.assertEqual(f["title"], "Upload is worse than download")
        self.assertIn("received at AP", f["evidence"][1])

    def test_afternoon_dips_suggest_wind(self):
        dips = lambda i, ts: 30 if 13 <= HOUR(ts) < 17 else 85       # noqa: E731
        ap = radio(AP, "AP", "LiteAP AC", "ap-ptmp-ac", radio_rows("ap-ptmp-ac"),
                   [link_rows(STA, "Sta", "PowerBeam 5AC", -55, -55, 90, dips)])
        L = build([ap])["links"][0]
        wh = L["metrics"]["worst_hours"]
        self.assertEqual((wh["kind"], wh["label"]), ("day", "13:00–17:00"))
        f = ids(L)["unstable"]
        self.assertIn("wind", f["causes"][0])
        self.assertEqual(f["level"], "info")          # 4 h of 24 = 17 %

    def test_drops_are_counted_as_episodes(self):
        rows = link_rows(STA, "Sta", "PowerBeam 5AC", -55, -55, 90, 90, skip={100, 101, 102, 400})
        ap = radio(AP, "AP", "LiteAP AC", "ap-ptmp-ac", radio_rows("ap-ptmp-ac"), [rows])
        L = build([ap])["links"][0]
        self.assertEqual(L["metrics"]["drops"]["count"], 2)
        self.assertEqual(L["metrics"]["drops"]["episodes"][0]["minutes"], 45)
        self.assertEqual(ids(L)["drops"]["level"], "warn")

    def test_power_mismatch(self):
        ap = radio(AP, "AP", "LiteAP AC", "ap-ptmp-ac", radio_rows("ap-ptmp-ac"),
                   [link_rows(STA, "Sta", "PowerBeam 5AC", -52, -63, 90, 90)])
        f = ids(build([ap])["links"][0])["power_mismatch"]
        self.assertIn("AP is set to lower output power", f["causes"][0])

    def test_unreadable_radio_advice_depends_on_the_error(self):
        r = radio("1c:6a:1b:00:00:09", "x", "", "", [], [], ok=False,
                  error="SSH read failed — check credentials / SSH access (ssh: connect to host port 22: Connection refused)")
        f = build([r])["radios"][0]["findings"][0]
        self.assertIn("SSH is switched off", f["causes"][0])


class HelpersTest(unittest.TestCase):
    def test_fresnel(self):
        fz = wifidiag.fresnel(1350, 5600)          # 8.656 * sqrt(1.35 km / 5.6 GHz) = 4.25 m
        self.assertAlmostEqual(fz["radius_m"], 4.25, delta=0.06)
        self.assertAlmostEqual(fz["clear_m"], 2.55, delta=0.06)
        self.assertIsNone(wifidiag.fresnel(None, 5600))

    def test_worst_hours_needs_a_clear_pattern(self):
        self.assertIsNone(wifidiag.worst_hours([20] * 24))                  # poor all day: no pattern
        self.assertIsNone(wifidiag.worst_hours([None] * 20 + [50, 60, 70, 80]))
        self.assertIsNone(wifidiag.worst_hours([2] * 24))                   # hardly ever poor

    def test_worst_hours_from_the_share_of_poor_readings(self):
        # Tankwa's Kliphuis link, 2026-09: poor by day, much better at night.
        kliphuis = [29, 29, 19, 0, 23, 30, 22, 19, 44, 23, 38, 25, 29, 36, 41, 47, 43, 36, 30, 38, 14, 17, 14, 24]
        wh = wifidiag.worst_hours(kliphuis)
        self.assertEqual(wh["kind"], "day")
        self.assertEqual(wh["label"], "08:00–17:00")
        night = [45, 50, 48, 40, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 42, 44, 47, 49, 46]
        self.assertEqual(wifidiag.worst_hours(night)["label"], "19:00–04:00")

    def test_one_odd_reading_does_not_raise_a_drop(self):
        blip = lambda i, ts: -62 if ts == NOW else -52                  # noqa: E731
        ap = radio(AP, "AP", "LiteAP AC", "ap-ptmp-ac", radio_rows("ap-ptmp-ac"),
                   [link_rows(STA, "Sta", "PowerBeam 5AC", blip, blip, 90, 90)])
        self.assertNotIn("signal_drop", ids(build([ap])["links"][0]))


if __name__ == "__main__":
    unittest.main()
