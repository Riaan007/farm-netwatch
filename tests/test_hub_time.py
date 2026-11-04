"""Hub: time keeping (hub/app/timekeeping.py + timeapi.py).

Run inside the hub image with a throwaway data dir and no network:
  docker run --rm --network none -e TZ=Africa/Johannesburg -e PYTHONDONTWRITEBYTECODE=1 \
    -v "$PWD/hub/app:/app:ro" -v "$PWD/tests:/tests:ro" \
    --entrypoint python farm-netwatch-hub:local /tests/test_hub_time.py -v
"""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest

os.environ["HUB_DATA"] = tempfile.mkdtemp()
sys.path.insert(0, os.environ.get("HUB_APP", "/app"))

import hubconfig     # noqa: E402
import timekeeping as tk   # noqa: E402

DAY = "2026-10-20"
T0 = int(dt.datetime(2026, 10, 20, 9, 0).timestamp())          # 09:00 local
NOW = int(dt.datetime(2026, 10, 21, 12, 0).timestamp())


def at(h, m=0):
    return int(dt.datetime(2026, 10, 20, h, m).timestamp())


def wipe():
    c = tk._conn()
    c.execute("DELETE FROM time_activity")
    c.execute("DELETE FROM time_entries")
    c.execute("DELETE FROM time_settings")
    c.commit()


def entries(**kw):
    out = [dict(r) for r in tk._conn().execute("SELECT * FROM time_entries ORDER BY start_ts, id")]
    return [e for e in out if all(e[k] == v for k, v in kw.items())]


class Sessions(unittest.TestCase):
    def test_gap_joins_and_splits(self):
        mins = [T0, T0 + 60, T0 + 300, T0 + 900]       # 4-min pause joins, 10-min pause splits
        self.assertEqual(tk.sessions_of(mins, 300), [[T0, T0 + 360], [T0 + 900, T0 + 960]])

    def test_subtract(self):
        self.assertEqual(tk._subtract(0, 100, [(10, 20), (50, 200)]), [(0, 10), (20, 50)])
        self.assertEqual(tk._subtract(0, 100, []), [(0, 100)])
        self.assertEqual(tk._subtract(0, 100, [(0, 100)]), [])


class Builder(unittest.TestCase):
    def setUp(self):
        wipe()

    def test_remote_session_from_cc_and_proxy(self):
        tk.mark_many("cc", [("bennie", at(9, m)) for m in (0, 1, 2, 5)])
        tk.mark_many("proxy", [("bennie", at(9, 8))])
        tk.build_day(DAY, NOW)
        e = entries(kind="remote")
        self.assertEqual(len(e), 1)
        self.assertEqual((e[0]["site_id"], e[0]["start_ts"], e[0]["end_ts"]), ("bennie", at(9), at(9, 9)))
        self.assertEqual(e[0]["sources"], "cc,proxy")
        self.assertEqual(e[0]["status"], "draft")

    def test_two_farms_never_overlap(self):
        tk.mark_many("cc", [("bennie", at(10, m)) for m in range(0, 4)])
        tk.mark_many("cc", [("tankwa", at(10, m)) for m in range(4, 7)])
        tk.mark_many("cc", [("bennie", at(10, m)) for m in range(7, 9)])
        tk.build_day(DAY, NOW)
        e = entries(kind="remote")
        self.assertEqual([x["site_id"] for x in e], ["bennie", "tankwa", "bennie"])
        for x, y in zip(e, e[1:]):
            self.assertLessEqual(x["end_ts"], y["start_ts"])

    def test_overview_glance_stays_with_farm(self):
        tk.mark_many("cc", [("bennie", at(11, 0)), ("", at(11, 1)), ("bennie", at(11, 2))])
        tk.build_day(DAY, NOW)
        e = entries(kind="remote")
        self.assertEqual([(x["site_id"], x["end_ts"] - x["start_ts"]) for x in e], [("bennie", 180)])

    def test_unassigned_hub_time(self):
        tk.mark_many("cc", [("", at(12, m)) for m in range(5)])
        tk.build_day(DAY, NOW)
        self.assertEqual([x["site_id"] for x in entries(kind="remote")], [""])

    def test_short_blip_dropped(self):
        tk.mark("cc", "bennie", at(13))
        tk.build_day(DAY, NOW)
        self.assertEqual(entries(), [])

    def test_rebuild_updates_in_place(self):
        tk.mark_many("cc", [("bennie", at(9, m)) for m in range(3)])
        tk.build_day(DAY, NOW)
        first = entries(kind="remote")[0]["id"]
        tk.mark_many("cc", [("bennie", at(9, m)) for m in range(3, 6)])
        tk.build_day(DAY, NOW)
        e = entries(kind="remote")
        self.assertEqual(len(e), 1)
        self.assertEqual(e[0]["id"], first)
        self.assertEqual(e[0]["end_ts"], at(9, 6))

    def test_approved_is_frozen_and_not_overlaid(self):
        tk.mark_many("cc", [("bennie", at(9, m)) for m in range(10)])
        tk.build_day(DAY, NOW)
        e = entries(kind="remote")[0]
        tk.update_entry(e["id"], {"status": "approved", "end_ts": at(9, 5)})
        tk.mark_many("cc", [("bennie", at(9, m)) for m in range(10, 14)])
        tk.build_day(DAY, NOW)
        got = [(x["status"], x["start_ts"], x["end_ts"]) for x in entries(kind="remote")]
        self.assertEqual(got, [("approved", at(9), at(9, 5)), ("draft", at(9, 5), at(9, 14))])

    def test_dismissed_does_not_come_back(self):
        tk.mark_many("cc", [("bennie", at(9, m)) for m in range(10)])
        tk.build_day(DAY, NOW)
        tk.delete_entry(entries(kind="remote")[0]["id"])
        tk.build_day(DAY, NOW)
        self.assertEqual([x["status"] for x in entries()], ["dismissed"])

    def test_onsite_wins_over_remote_same_farm(self):
        tk.add_entry({"kind": "onsite", "site_id": "bennie", "start_ts": at(9), "end_ts": at(11)}, source="button")
        tk.mark_many("cc", [("bennie", at(10, m)) for m in range(10)])
        tk.mark_many("cc", [("tankwa", at(10, m)) for m in range(20, 30)])
        tk.build_day(DAY, NOW)
        self.assertEqual([x["site_id"] for x in entries(kind="remote")], ["tankwa"])

    def test_phone_on_wifi_makes_a_visit(self):
        tk.mark_many("wifi", [("tankwa", at(8, m)) for m in (0, 15, 30, 45)])
        tk.mark_many("wifi", [("tankwa", at(14))])           # later, after a long gap: another visit
        tk.build_day(DAY, NOW)
        v = entries(kind="onsite")
        self.assertEqual([(x["start_ts"], x["end_ts"], x["source"]) for x in v],
                         [(at(8), at(8, 46), "wifi"), (at(14), at(14, 1), "wifi")])

    def test_dev_billed_to_farm_does_not_come_back(self):
        tk.import_dev({"minutes": [at(15, m) for m in range(0, 30, 3)]})
        d = entries(kind="dev")[0]
        tk.update_entry(d["id"], {"kind": "remote", "site_id": "tankwa", "status": "approved"})
        tk.build_day(DAY, NOW)
        self.assertEqual([(x["kind"], x["site_id"]) for x in entries()], [("remote", "tankwa")])

    def test_dev_from_claude(self):
        out = tk.import_dev({"minutes": [at(15, m) for m in (0, 3, 8, 30, 31)]})
        self.assertEqual(out["imported"], 5)
        d = entries(kind="dev")
        # 15:00-15:09 (gap 5 min ≤ 10) and 15:30-15:32 — built by the import itself
        self.assertEqual([(x["start_ts"], x["end_ts"]) for x in d], [(at(15), at(15, 9)), (at(15, 30), at(15, 32))])


class Clock(unittest.TestCase):
    def setUp(self):
        wipe()
        tk.save_settings({"farms": {"tankwa": {"km_one_way": 140}}})

    def test_drive_arrive_leave_home(self):
        tk.clock("drive", "tankwa", now=at(7))
        o = tk.open_entries()
        self.assertEqual([(x["kind"], x["km"]) for x in o], [("travel", 140)])
        tk.clock("arrive", "tankwa", now=at(9))
        self.assertEqual([x["kind"] for x in tk.open_entries()], ["onsite"])
        tk.clock("leave_drive", now=at(12))
        tk.clock("home", now=at(14))
        self.assertEqual(tk.open_entries(), [])
        got = [(x["kind"], x["start_ts"], x["end_ts"]) for x in entries()]
        self.assertEqual(got, [("travel", at(7), at(9)), ("onsite", at(9), at(12)), ("travel", at(12), at(14))])
        self.assertTrue(all(x["status"] == "approved" for x in entries()))

    def test_leave_without_visit(self):
        with self.assertRaises(tk.TimeError):
            tk.clock("leave", now=at(9))

    def test_button_visit_blocks_wifi_draft(self):
        tk.clock("arrive", "tankwa", now=at(9))
        tk.clock("leave", now=at(11))
        tk.mark_many("wifi", [("tankwa", at(9) + m * 60) for m in range(0, 120, 15)])
        tk.build_day(DAY, NOW)
        self.assertEqual(len(entries(kind="onsite")), 1)


class Billing(unittest.TestCase):
    def setUp(self):
        wipe()
        tk.save_settings({"farms": {"tankwa": {"rate_remote": 400, "rate_onsite": 500, "rate_travel": 300,
                                                "rate_km": 5, "callout_min": 60}}})

    def test_rounding_callout_and_drafts(self):
        add = lambda **k: tk.add_entry({"site_id": "tankwa", **k})
        add(kind="remote", start_ts=at(9), end_ts=at(9, 7))           # 7 + 10 = 17 min → 30
        add(kind="remote", start_ts=at(10), end_ts=at(10, 10))
        add(kind="onsite", start_ts=at(13), end_ts=at(13, 20))        # 20 min visit → call-out 60
        add(kind="travel", start_ts=at(12), end_ts=at(12, 50), km=140)  # 50 → 60
        tk.add_entry({"kind": "remote", "site_id": "tankwa", "start_ts": at(16), "end_ts": at(17)},
                     source="auto", status="draft")
        b = tk.bill("tankwa", "2026-10")
        d = b["days"][DAY]
        self.assertEqual((d["remote_billed"], d["onsite_billed"], d["travel_billed"]), (30, 60, 60))
        self.assertEqual(b["amounts"], {"remote": 200.0, "onsite": 500.0, "travel": 300.0, "km": 700.0})
        self.assertEqual(b["total"], 1700.0)
        self.assertEqual(b["drafts"], 1)
        self.assertEqual(tk.bill("tankwa", "2026-10", include_drafts=True)["days"][DAY]["remote_billed"], 90)

    def test_running_entry_not_billed(self):
        tk.clock("arrive", "tankwa", now=at(9))
        self.assertEqual(tk.bill("tankwa", "2026-10")["total"], 0)

    def test_approve_needs_a_farm(self):
        e = tk.add_entry({"kind": "remote", "site_id": "", "start_ts": at(9), "end_ts": at(10)},
                         source="auto", status="draft")
        with self.assertRaises(tk.TimeError):
            tk.update_entry(e["id"], {"status": "approved"})
        out = tk.approve_day(DAY)
        self.assertEqual(out, {"approved": 0, "unassigned_left": 1})
        tk.update_entry(e["id"], {"kind": "general"})
        self.assertEqual(tk.approve_day(DAY)["approved"], 1)

    def test_home_site_not_billable_by_default(self):
        self.assertFalse(tk.farm_settings("home")["billable"])
        self.assertTrue(tk.farm_settings("tankwa")["billable"])

    def test_settings_validation(self):
        with self.assertRaises(tk.TimeError):
            tk.save_settings({"farms": {"tankwa": {"rate_remote": -5}}})
        with self.assertRaises(tk.TimeError):
            tk.save_settings({"global": {"round_min": 0}})
        with self.assertRaises(tk.TimeError):
            tk.save_settings({"farms": {"../x": {}}})


class ProxyLog(unittest.TestCase):
    PORTS = {"8205": "tankwa"}

    def line(self, uri, method="GET", host="192.168.88.250:8205", status=200, ts=1761000000.5):
        return json.dumps({"ts": ts, "status": status, "request": {"host": host, "uri": uri, "method": method}})

    def test_work_vs_background(self):
        self.assertEqual(tk.parse_proxy_line(self.line("/api/devices/abc/asset"), self.PORTS), ("tankwa", 1761000000))
        self.assertEqual(tk.parse_proxy_line(self.line("/"), self.PORTS), ("tankwa", 1761000000))
        self.assertIsNone(tk.parse_proxy_line(self.line("/api/status"), self.PORTS))
        self.assertIsNone(tk.parse_proxy_line(self.line("/api/devices?x=1"), self.PORTS))
        self.assertEqual(tk.parse_proxy_line(self.line("/api/config", "POST"), self.PORTS)[0], "tankwa")
        self.assertIsNone(tk.parse_proxy_line(self.line("/", status=401), self.PORTS))
        self.assertIsNone(tk.parse_proxy_line(self.line("/", host="192.168.88.250:8299"), self.PORTS))
        self.assertIsNone(tk.parse_proxy_line(b"not json", self.PORTS))

    def test_caddyfile_has_log_and_skips(self):
        import proxycfg
        hubconfig.update({"auth": {"proxy_basic_hash": "$2b$12$" + "a" * 53}})
        cfg = {"sites": [{"id": "tankwa", "vpn_ip": "10.8.0.7", "proxy_netwatch_port": 8205,
                          "proxy_kuma_port": 8206, "kuma_url": "http://10.8.0.7:3001"}]}
        text = proxycfg.render_caddyfile(cfg)
        self.assertIn("output file /etc/caddy/logs/access.log", text)
        self.assertIn("log_skip @nwbg", text)
        self.assertIn("path /api/status ", text)
        self.assertIn("request>headers delete", text)


class Api(unittest.TestCase):
    def setUp(self):
        wipe()
        import server
        self.app = server.app
        self.c = self.app.test_client()
        with self.c.session_transaction() as s:
            s["auth"] = True
        hubconfig.update({"sites": [{"id": "tankwa", "name": "Tankwa Farm", "vpn_ip": "10.8.0.7"}]})

    def test_needs_login(self):
        anon = self.app.test_client()
        self.assertEqual(anon.get("/api/hub/time/state").status_code, 401)
        self.assertEqual(anon.post("/api/hub/time/beat", json={"site": "tankwa"}).status_code, 401)

    def test_beat_unknown_site_is_general(self):
        self.assertEqual(self.c.post("/api/hub/time/beat", json={"site": "nope"}).status_code, 200)
        rows = tk._conn().execute("SELECT site_id, source FROM time_activity").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("", "cc")])

    def test_clock_and_timesheet(self):
        r = self.c.post("/api/hub/time/clock", json={"action": "arrive", "site_id": "tankwa"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.c.post("/api/hub/time/clock", json={"action": "arrive", "site_id": "x"}).status_code, 400)
        self.c.post("/api/hub/time/clock", json={"action": "stop"})
        m = dt.date.today().strftime("%Y-%m")
        pdf = self.c.get(f"/api/hub/time/timesheet?site=tankwa&month={m}")
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.data.startswith(b"%PDF"))
        csv_ = self.c.get(f"/api/hub/time/timesheet?site=tankwa&month={m}&format=csv")
        self.assertIn("Tankwa Farm", csv_.data.decode("utf-8-sig"))
        self.assertEqual(self.c.get("/api/hub/time/timesheet?site=nope").status_code, 404)

    def test_month_and_settings(self):
        r = self.c.post("/api/hub/time/settings", json={"farms": {"tankwa": {"rate_remote": "450"}}})
        self.assertEqual(r.get_json()["farms"]["tankwa"]["rate_remote"], 450)
        self.assertEqual(self.c.post("/api/hub/time/settings", json={"farms": {"ghost": {}}}).status_code, 400)
        j = self.c.get("/api/hub/time/month?month=2026-10").get_json()
        self.assertEqual([f["site_id"] for f in j["farms"]], ["tankwa"])
        self.assertEqual(self.c.get("/api/hub/time/month?month=2026-13").status_code, 400)


class SiteFeed(unittest.TestCase):
    """Minutes of the farm Pi's own page opened directly, fetched with the hub key."""

    def setUp(self):
        wipe()

    def _resp(self, code, body=None):
        from unittest import mock
        return mock.Mock(status_code=code, json=mock.Mock(return_value=body or {}))

    def test_fetch_marks_and_builds_remote(self):
        from unittest import mock
        cfg = {"sites": [{"id": "bennie", "vpn_ip": "10.8.0.9"}, {"id": "off", "vpn_ip": "10.8.0.8"}]}
        snap = {"bennie": {"reachable": True}, "off": {"reachable": False}}
        feed = tk._SiteFeed()
        mins = [at(9, m) for m in range(0, 40)]
        import requests
        with mock.patch.object(requests, "get", return_value=self._resp(200, {"minutes": mins})) as g:
            got = feed.read(cfg, snap.get, now=NOW)
        self.assertEqual(g.call_count, 1)                    # the unreachable farm is not asked
        self.assertEqual(len(got), 40)
        self.assertEqual(feed.since["bennie"], at(9, 39) - 120)
        with mock.patch.object(requests, "get") as g:          # not again within 2 minutes
            feed.read(cfg, snap.get, now=NOW + 60)
        g.assert_not_called()
        tk.mark_many("site", got)
        tk.build_day(DAY, now=NOW)
        rem = entries(kind="remote", site_id="bennie")
        self.assertEqual(len(rem), 1)
        self.assertIn("site", rem[0]["sources"])

    def test_old_farm_image_asked_hourly(self):
        from unittest import mock
        import requests
        cfg = {"sites": [{"id": "bennie", "vpn_ip": "10.8.0.9"}]}
        feed = tk._SiteFeed()
        with mock.patch.object(requests, "get", return_value=self._resp(404)):
            self.assertEqual(feed.read(cfg, lambda s: {"reachable": True}, now=NOW), [])
        with mock.patch.object(requests, "get") as g:
            feed.read(cfg, lambda s: {"reachable": True}, now=NOW + 1800)
        g.assert_not_called()


class VpnCounting(unittest.TestCase):
    """timenet.py: packets to a farm through the hub's VPN namespace."""

    CFG = {"sites": [{"id": "home", "vpn_ip": "192.168.88.250"}, {"id": "bennie", "vpn_ip": "10.8.0.2"},
                     {"id": "tankwa", "vpn_ip": "10.8.0.7"}],
           "remote_clients": [{"address": "10.8.0.3"}, {"address": "10.8.0.4"}]}

    def listing(self, chain, counts):
        rows = "\n".join(f"{n} {n * 60} RETURN 0 -- * * 0.0.0.0/0 10.8.0.2" for n in counts)
        return f"Chain {chain} (1 references)\n pkts bytes target prot opt in out source destination\n{rows}\n"

    def test_rules_count_only_operators_lan_and_ssh(self):
        import timenet
        fwd, out = timenet.desired(self.CFG)
        self.assertEqual(len(fwd), 6)                                  # 2 farms x (2 operators + LAN)
        self.assertIn("-A NW-TIME-FWD -s 10.8.0.3/32 -d 10.8.0.2/32 -j RETURN", fwd)
        self.assertIn("-A NW-TIME-FWD -d 10.8.0.7/32 ! -i wg0 -j RETURN", fwd)
        self.assertFalse(any("192.168" in r for r in fwd + out))       # home is not over the VPN
        self.assertTrue(all("RETURN" in r for r in fwd + out))         # count only, never decide
        self.assertEqual(out[0], "-A NW-TIME-OUT -d 10.8.0.2/32 -p tcp -m tcp --dport 22 -j RETURN")

    def test_deltas_need_a_baseline_and_a_minimum(self):
        from unittest import mock
        import timenet
        state = {"fwd": [0, 0, 0, 0, 0, 0], "out": [0, 0]}

        def run(args, stdin=None):
            if args[1] == "-L":
                ch = args[2]
                return 0, self.listing(ch, state["fwd"] if ch == timenet.FWD else state["out"])
            return 0, ""
        timenet._last.clear()
        with mock.patch.object(timenet, "ensure", return_value="iptables-legacy"), \
                mock.patch.object(timenet.vpnfw, "_run", side_effect=run):
            self.assertEqual(timenet.deltas(self.CFG, NOW), [])        # baseline
            state["fwd"] = [5, 0, 0, 0, 2, 3]                          # bennie +5 (below 8), tankwa +5
            state["out"] = [0, 40]                                     # SSH to tankwa
            self.assertEqual(sorted(timenet.deltas(self.CFG, NOW + 30)), [("ssh", "tankwa", NOW + 30)])
            state["fwd"] = [5, 9, 0, 0, 2, 3]                          # bennie +9 from the laptop
            self.assertEqual(timenet.deltas(self.CFG, NOW + 60), [("vpn", "bennie", NOW + 60)])
            state["fwd"] = [9, 0, 0, 0, 0, 0]                          # counters reset by wg-easy
            self.assertEqual(timenet.deltas(self.CFG, NOW + 90), [("vpn", "bennie", NOW + 90)])
        timenet._last.clear()


if __name__ == "__main__":
    unittest.main()
