"""Hub: monitored-device alerts (poller._check_monitored) and the route that
puts devices on / takes them off a site's Monitored list.

Run inside the hub image with a throwaway data dir:
  docker run --rm -v "$PWD/hub/app:/app:ro" -v "$PWD/tests:/tests:ro" -e HUB_DATA=/tmp/hub \
    --entrypoint python farm-netwatch-hub:local /tests/test_monitor_alerts.py -v
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("HUB_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("HUB_APP", "/app"))

import hubconfig             # noqa: E402
import poller as poller_mod  # noqa: E402
import server                # noqa: E402
import siteapi               # noqa: E402

SITE = {"id": "farm-a", "name": "Farm A", "vpn_ip": "10.8.0.9", "netwatch_port": 8090,
        "kuma_url": "", "kuma_status_slug": "", "enabled": True}


def dev(key, ip, online=True, watch=True, name=None, ago=0, missed=None):
    return {"key": key, "ip": ip, "online": online, "watch": watch, "name": name or f"Cam {key}",
            "last_seen": int(time.time()) - ago, "missed_scans": (0 if online else 2) if missed is None else missed}


def use_site(**alerts):
    cfg = hubconfig.load()
    cfg["sites"] = [SITE]
    cfg["alerts"].update({"ntfy_topic": "hub-topic", "notify_device_offline": True, **alerts})
    hubconfig.save(cfg)


class Alerts(unittest.TestCase):
    def setUp(self):
        if os.path.exists(poller_mod.MON_STATE_PATH):
            os.remove(poller_mod.MON_STATE_PATH)
        use_site()
        self.p = poller_mod.Poller()
        self.push = mock.patch.object(poller_mod.notify, "push").start()
        self.addCleanup(mock.patch.stopall)

    def check(self, *devices, p=None, offline_after=2):
        (p or self.p)._check_monitored("farm-a", SITE, {"devices": list(devices), "offline_after": offline_after})

    def test_first_look_is_silent_then_each_change_alerts_once(self):
        self.check(dev("a", "10.0.0.2"), dev("b", "10.0.0.3", online=False, ago=7200))
        self.push.assert_not_called()
        self.check(dev("a", "10.0.0.2", online=False, ago=900), dev("b", "10.0.0.3", online=False, ago=7500))
        self.assertEqual(self.push.call_count, 1)
        args, kw = self.push.call_args
        self.assertEqual(args[1], "Farm A: Cam a is offline")
        self.assertIn("Cam a (10.0.0.2) — last seen 15 min ago", args[2])
        self.assertEqual(kw["priority"], "high")
        self.check(dev("a", "10.0.0.2", online=False, ago=1200), dev("b", "10.0.0.3", online=False))
        self.assertEqual(self.push.call_count, 1)                      # still down: no repeat
        self.check(dev("a", "10.0.0.2"), dev("b", "10.0.0.3"))
        self.assertEqual(self.push.call_count, 2)
        args, _ = self.push.call_args
        self.assertEqual(args[1], "Farm A: 2 monitored devices back online")
        self.assertIn("Cam b (10.0.0.3) — was offline 2 h", args[2])

    def test_a_device_already_down_when_monitored_is_not_news(self):
        self.check(dev("a", "10.0.0.2", online=False, watch=False))
        self.check(dev("a", "10.0.0.2", online=False))
        self.push.assert_not_called()
        self.check(dev("a", "10.0.0.2"))
        self.assertEqual(self.push.call_args[0][1], "Farm A: Cam a is back online")

    def test_taken_off_the_list_or_forgotten_drops_silently(self):
        self.check(dev("a", "10.0.0.2", online=False), dev("b", "10.0.0.3", online=False))
        self.check(dev("a", "10.0.0.2", watch=False))                 # un-monitored, b forgotten
        self.check(dev("a", "10.0.0.2", watch=True), dev("b", "10.0.0.3"))
        self.push.assert_not_called()

    def test_many_devices_make_one_message(self):
        self.check(*[dev(f"d{i}", f"10.0.0.{i}") for i in range(1, 13)])
        self.check(*[dev(f"d{i}", f"10.0.0.{i}", online=False) for i in range(1, 13)])
        self.assertEqual(self.push.call_count, 1)
        title, body = self.push.call_args[0][1:3]
        self.assertEqual(title, "Farm A: 12 monitored devices offline")
        self.assertEqual(len(body.splitlines()), poller_mod.MON_LIST_MAX + 1)
        self.assertTrue(body.splitlines()[0].startswith("• Cam d1 (10.0.0.1)"))   # IP order
        self.assertEqual(body.splitlines()[-1], "… and 4 more")

    def test_switch_off_silences_but_keeps_tracking(self):
        use_site(notify_device_offline=False)
        self.check(dev("a", "10.0.0.2"))
        self.check(dev("a", "10.0.0.2", online=False))
        self.push.assert_not_called()
        use_site()
        self.check(dev("a", "10.0.0.2", online=False))
        self.push.assert_not_called()                                  # not a new drop

    def test_one_missed_scan_is_not_news(self):
        self.check(dev("a", "10.0.0.2"))
        self.check(dev("a", "10.0.0.2", online=False, missed=1, ago=900))
        self.push.assert_not_called()
        self.check(dev("a", "10.0.0.2"))                               # back before it counted
        self.check(dev("a", "10.0.0.2", online=False, missed=2, ago=1800))
        self.assertEqual(self.push.call_count, 1)

    def test_still_offline_is_never_back_online(self):
        self.check(dev("a", "10.0.0.2"))
        self.check(dev("a", "10.0.0.2", online=False, missed=2))
        self.assertEqual(self.push.call_count, 1)
        # the operator raises offline_after while it is down: not "back", not "down again"
        self.check(dev("a", "10.0.0.2", online=False, missed=2), offline_after=5)
        self.check(dev("a", "10.0.0.2", online=False, missed=5), offline_after=5)
        self.assertEqual(self.push.call_count, 1)
        self.check(dev("a", "10.0.0.2"), offline_after=5)
        self.assertEqual(self.push.call_args[0][1], "Farm A: Cam a is back online")

    def test_names_from_the_network_stay_on_one_line(self):
        self.check(dev("a", "10.0.0.2", name="Gate\r\nX-Evil: 1"))
        self.check(dev("a", "10.0.0.2", name="Gate\r\nX-Evil: 1", online=False))
        self.assertEqual(self.push.call_args[0][1], "Farm A: Gate X-Evil: 1 is offline")

    def test_older_sites_are_judged_by_time(self):
        self.p._snap["farm-a"] = {"status": {"scan_interval_min": 15}}
        legacy = lambda **kw: {k: v for k, v in dev("a", "10.0.0.2", **kw).items() if k != "missed_scans"}  # noqa: E731
        self.check(legacy(), offline_after=None)
        self.check(legacy(online=False, ago=29 * 60), offline_after=None)     # could still be one miss
        self.push.assert_not_called()
        self.check(legacy(online=False, ago=38 * 60), offline_after=None)     # 2.5 intervals
        self.assertEqual(self.push.call_count, 1)

    def test_state_survives_a_hub_restart(self):
        self.check(dev("a", "10.0.0.2"))
        self.check(dev("a", "10.0.0.2", online=False))
        restarted = poller_mod.Poller()
        self.check(dev("a", "10.0.0.2", online=False), p=restarted)
        self.assertEqual(self.push.call_count, 1)
        self.check(dev("a", "10.0.0.2"), p=restarted)
        self.assertEqual(self.push.call_count, 2)

    def test_only_a_fresh_list_from_a_reachable_site_is_checked(self):
        resp = mock.Mock(status_code=200, raise_for_status=lambda: None,
                         json=lambda: {"devices": [dev("a", "10.0.0.2")], "offline_after": 2})
        with mock.patch.object(poller_mod.requests, "get", return_value=resp), \
                mock.patch.object(self.p, "_save_snapshot"), \
                mock.patch.object(self.p, "_check_monitored") as chk:
            self.p._snap["farm-a"] = {"reachable": False}
            self.p._fetch_devices("farm-a", SITE, 5)
            chk.assert_not_called()
            self.p._snap["farm-a"]["reachable"] = True
            self.p._fetch_devices("farm-a", SITE, 5)
            chk.assert_called_once()


class Resp:
    def __init__(self, status, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text
        self.ok = status < 400

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class Route(unittest.TestCase):
    def setUp(self):
        use_site()
        self.c = server.app.test_client()
        with self.c.session_transaction() as s:
            s["auth"] = True
        server.poller._snap["farm-a"] = {"reachable": True, "devices": {"devices": [
            dev("a", "10.0.0.2", watch=False), dev("b", "10.0.0.3", watch=True)]}}
        server.poller._overrides.clear()
        self.post = mock.patch.object(server.requests, "post").start()
        mock.patch.object(siteapi, "key_for", return_value="hub-key").start()
        self.addCleanup(mock.patch.stopall)

    def cached(self):
        return {d["key"]: d["watch"] for d in server.poller.snapshot("farm-a")["devices"]["devices"]}

    def call(self, body, site="farm-a"):
        return self.c.post(f"/api/hub/sites/{site}/monitoring", json=body)

    def test_forwards_with_the_hub_key_and_patches_the_cache(self):
        self.post.return_value = Resp(200, {"ok": True, "changed": [{"key": "a", "monitored": True},
                                                                     {"key": "b", "monitored": False}],
                                            "unchanged": [], "unknown": []})
        r = self.call({"monitor": ["a"], "stop": ["b"]})
        self.assertEqual(r.status_code, 200)
        url = self.post.call_args[0][0]
        kw = self.post.call_args[1]
        self.assertEqual(url, "http://10.8.0.9:8090/api/monitoring")
        self.assertEqual(kw["json"], {"monitor": ["a"], "stop": ["b"]})
        self.assertEqual(kw["headers"][siteapi.HEADER], "hub-key")
        self.assertEqual(self.cached(), {"a": True, "b": False})

    def test_an_older_site_is_switched_device_by_device(self):
        self.post.side_effect = lambda url, **kw: (Resp(404, text="<h1>Not Found</h1>") if url.endswith("/api/monitoring")
                                                   else Resp(200, {"ok": True}))
        j = self.call({"monitor": ["a", "b", "zz"]}).get_json()
        self.assertTrue(j["ok"] and j["legacy"])
        self.assertEqual(j["changed"], [{"key": "a", "monitored": True}])
        self.assertEqual(j["unchanged"], ["b"])
        self.assertEqual(j["unknown"], ["zz"])
        per_device = [c for c in self.post.call_args_list if "/api/devices/" in c[0][0]]
        self.assertEqual(len(per_device), 1)
        self.assertEqual(per_device[0][0][0], "http://10.8.0.9:8090/api/devices/a")
        self.assertEqual(per_device[0][1]["json"], {"watch": True})
        self.assertEqual(self.cached(), {"a": True, "b": True})

    def test_refusals_and_bad_requests(self):
        self.post.return_value = Resp(401, {"ok": False, "error": "auth_required"})
        self.assertEqual(self.call({"monitor": ["a"]}).status_code, 502)
        self.assertEqual(self.cached(), {"a": False, "b": True})       # nothing patched
        for body in ({}, {"monitor": "a"}, {"monitor": ["a"], "stop": ["a"]}, {"monitor": [3]}):
            self.assertEqual(self.call(body).status_code, 400, body)
        self.assertEqual(self.call({"monitor": ["a"]}, site="nope").status_code, 404)
        with self.c.session_transaction() as s:
            s.clear()
        self.assertEqual(self.call({"monitor": ["a"]}).status_code, 401)

    def test_alert_setting_round_trip(self):
        self.assertTrue(self.c.get("/api/hub/alerts").get_json()["notify_device_offline"])
        self.c.post("/api/hub/alerts", json={"notify_device_offline": False})
        self.assertFalse(hubconfig.load()["alerts"]["notify_device_offline"])


if __name__ == "__main__":
    unittest.main()
