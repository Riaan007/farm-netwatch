"""Monitored devices (app/monitoring.py) and their API.

Run inside the site image with a throwaway data dir:
  docker run --rm -v "$PWD/app:/app" -v "$PWD/tests:/tests" -e NETWATCH_DATA=/tmp/nw \
    --entrypoint python farm-netwatch:netcfg /tests/test_monitoring.py -v
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import config      # noqa: E402
import creds       # noqa: E402
import history     # noqa: E402
import monitoring  # noqa: E402
import server      # noqa: E402
import siteauth    # noqa: E402

CAM, NVR, PHONE, RADIO = "aa:00:00:00:00:01", "aa:00:00:00:00:02", "aa:00:00:00:00:03", "aa:00:00:00:00:04"
KEY = "k" * 43
HUB = {"REMOTE_ADDR": "10.8.0.1"}
LOGIN = ("http://kuma:3001", "admin", "pw")


def kmon(mid, active=True, name="x", hostname="", type_="ping", description=""):
    return {"id": mid, "active": active, "name": name, "hostname": hostname, "type": type_,
            "description": description}


def device(key, ip, online=True, **kw):
    return {"key": key, "ip": ip, "online": online, "category": "camera",
            "last_seen": int(time.time()) - (0 if online else 600), **kw}


class Base(unittest.TestCase):
    def setUp(self):
        self.s = server.scanner
        self.s.registry = {}
        self.s.devices = {}
        self.saves = mock.patch.object(self.s, "save_registry").start()
        self.real_follow = monitoring.follow_kuma
        self.follow = mock.patch.object(monitoring, "follow_kuma").start()
        self.addCleanup(mock.patch.stopall)
        monitoring._tried.clear()
        monitoring._last_sync = time.time()         # Kuma read-back is tested on its own


class Migration(unittest.TestCase):
    def test_rev3_leaves_a_seed_marker_once(self):
        cfg = config.load()
        cfg["config_rev"] = 2
        cfg["monitoring"] = {"seed_pending": False}
        with open(config.CONFIG_PATH, "w") as f:
            json.dump(cfg, f)
        c = config.load()
        self.assertTrue(c["monitoring"]["seed_pending"])
        self.assertEqual(c["config_rev"], 3)
        config.update({"monitoring": {"seed_pending": False}})
        self.assertFalse(config.load()["monitoring"]["seed_pending"])   # not re-applied


class Seed(Base):
    def test_devices_with_a_kuma_monitor_become_monitored_once(self):
        self.s.registry = {
            CAM: {"name": "Gate", "kuma_monitor_id": 12},
            NVR: {"kuma_token": "abc", "watch": False},     # old saves wrote watch:false
            PHONE: {"name": "Phone"},
            RADIO: {"kuma_monitor_id": 0},
            "aa:00:00:00:00:09": {"kuma_monitor_id": 9, "watch": False, "kuma_paused": True},  # a later "stop"
        }
        self.s.devices = {CAM: device(CAM, "10.0.0.2")}
        config.update({"monitoring": {"seed_pending": True}})
        self.assertEqual(monitoring.seed(self.s), 2)
        self.assertTrue(self.s.registry[CAM]["watch"])
        self.assertTrue(self.s.registry[NVR]["watch"])
        self.assertNotIn("watch", self.s.registry[PHONE])
        self.assertNotIn("watch", self.s.registry[RADIO])
        self.assertFalse(self.s.registry["aa:00:00:00:00:09"]["watch"])
        self.assertTrue(self.s.devices[CAM]["watch"])
        self.assertFalse(config.load()["monitoring"]["seed_pending"])
        # an operator's later choice is never overwritten by a second run
        self.s.registry[CAM]["watch"] = False
        self.assertEqual(monitoring.seed(self.s), 0)
        self.assertFalse(self.s.registry[CAM]["watch"])


class SetMany(Base):
    def setUp(self):
        super().setUp()
        self.s.registry = {CAM: {"watch": True}, NVR: {}}
        self.s.devices = {CAM: device(CAM, "10.0.0.2"), NVR: device(NVR, "10.0.0.3", online=False),
                          PHONE: device(PHONE, "10.0.0.4")}

    def test_on_off_unchanged_unknown(self):
        res = monitoring.set_many(self.s, on=[NVR, PHONE, CAM, "zz"], off=[])
        self.assertEqual(res["changed"], [{"key": NVR, "monitored": True}, {"key": PHONE, "monitored": True}])
        self.assertEqual(res["unchanged"], [CAM])
        self.assertEqual(res["unknown"], ["zz"])
        self.assertEqual(res["summary"]["monitored"], 3)
        self.assertEqual(res["summary"]["offline"], 1)
        self.assertTrue(self.s.devices[PHONE]["watch"])
        self.assertEqual(self.saves.call_count, 1)                    # one registry write
        self.follow.assert_called_once_with(self.s, [NVR, PHONE])
        res = monitoring.set_many(self.s, off=[CAM])
        self.assertFalse(self.s.registry[CAM]["watch"])
        self.assertFalse(self.s.devices[CAM]["watch"])

    def test_every_switch_is_in_the_device_history(self):
        monitoring.set_many(self.s, on=[NVR], off=[CAM], by="the hub")
        evs = {e["key"]: e for e in history.events(etype="monitoring", since=int(time.time()) - 60)}
        self.assertEqual(evs[NVR]["detail"], {"monitored": True, "by": "the hub"})
        self.assertEqual(evs[CAM]["detail"], {"monitored": False, "by": "the hub"})
        self.assertEqual(evs[NVR]["ip"], "10.0.0.3")

    def test_nothing_to_do_writes_nothing(self):
        self.s.registry[NVR]["watch"] = False
        monitoring.set_many(self.s, on=[CAM], off=[NVR])
        self.saves.assert_not_called()
        self.follow.assert_not_called()

    def test_off_is_always_stored_explicitly(self):
        res = monitoring.set_many(self.s, off=[NVR])           # never switched either way
        self.assertEqual(res["unchanged"], [NVR])
        self.assertIs(self.s.registry[NVR]["watch"], False)
        self.saves.assert_called_once()
        self.follow.assert_not_called()


class KumaFollows(Base):
    def setUp(self):
        super().setUp()
        mock.patch.object(monitoring, "kuma_login", return_value=LOGIN).start()
        self.prov = mock.patch.object(monitoring.kuma, "provision_many",
                                      side_effect=lambda b, u, p, items, i: {k: {"ok": True, "monitor_id": 50 + n}
                                                                             for n, (k, *_rest) in enumerate(items)}).start()
        self.act = mock.patch.object(monitoring.kuma, "set_active_many",
                                     side_effect=lambda b, u, p, items: {m: {"ok": True} for m, _ in items}).start()
        self.listing = mock.patch.object(monitoring.kuma, "monitor_list", return_value=[]).start()
        self.s.devices = {CAM: device(CAM, "10.0.0.2", name="Gate"), NVR: device(NVR, "10.0.0.3"),
                          PHONE: device(PHONE, "10.0.0.4"), RADIO: device(RADIO, "10.0.0.5")}

    def test_on_creates_off_pauses_on_resumes(self):
        self.s.registry = {CAM: {"watch": True}, NVR: {"watch": False, "kuma_monitor_id": 7},
                           PHONE: {"watch": True, "kuma_monitor_id": 8, "kuma_paused": True},
                           RADIO: {"watch": True, "kuma_token": "tok"}}
        monitoring._apply(self.s, {CAM, NVR, PHONE, RADIO})
        self.prov.assert_called_once()
        self.assertEqual([i[0] for i in self.prov.call_args[0][3]], [CAM])   # the push-token one is left alone
        self.assertEqual(self.s.registry[CAM]["kuma_monitor_id"], 50)
        self.assertEqual(self.s.registry[CAM]["kuma_ip"], "10.0.0.2")
        self.assertEqual(sorted(self.act.call_args[0][3]), [(7, False), (8, True)])
        self.assertTrue(self.s.registry[NVR]["kuma_paused"])
        self.assertNotIn("kuma_paused", self.s.registry[PHONE])
        self.saves.assert_called()

    def test_a_device_nobody_switched_keeps_its_monitor(self):
        # e.g. an older backup restored without the upgrade step: no pausing spree
        self.s.registry = {NVR: {"kuma_monitor_id": 7}, PHONE: {"kuma_monitor_id": 8, "watch": False}}
        self.assertIsNone(monitoring._out_of_step(self.s, NVR, self.s.registry[NVR]))
        self.assertEqual(monitoring._out_of_step(self.s, PHONE, self.s.registry[PHONE]), "pause")

    def test_forgotten_while_creating_leaves_no_monitor(self):
        self.s.registry = {CAM: {"watch": True}}

        def create(b, u, p, items, i):
            self.s.registry.pop(CAM)                      # Forget arrives meanwhile
            return {CAM: {"ok": True, "monitor_id": 77}}
        self.prov.side_effect = create
        with mock.patch.object(monitoring.kuma, "deprovision") as dep:
            monitoring._apply(self.s, {CAM})
        dep.assert_called_once_with(*LOGIN, 77)

    def test_rekeyed_while_creating_keeps_the_monitor(self):
        self.s.registry = {"10.0.0.2": {"watch": True}}
        self.s.devices = {"10.0.0.2": device("10.0.0.2", "10.0.0.2")}

        def create(b, u, p, items, i):
            self.s.registry[CAM] = self.s.registry.pop("10.0.0.2")   # the IP-keyed device gained a MAC
            return {"10.0.0.2": {"ok": True, "monitor_id": 78}}
        self.prov.side_effect = create
        with mock.patch.object(monitoring.kuma, "deprovision") as dep:
            monitoring._apply(self.s, {"10.0.0.2"})
        dep.assert_not_called()
        self.assertEqual(self.s.registry[CAM]["kuma_monitor_id"], 78)

    def test_registry_replaced_while_creating_keeps_the_monitor(self):
        self.s.registry = {CAM: {"watch": True}}

        def create(b, u, p, items, i):
            self.s.registry = {CAM: {"watch": True, "name": "restored"}}   # a restore lands meanwhile
            return {CAM: {"ok": True, "monitor_id": 79}}
        self.prov.side_effect = create
        with mock.patch.object(monitoring.kuma, "deprovision") as dep:
            monitoring._apply(self.s, {CAM})
        dep.assert_not_called()
        self.assertEqual(self.s.registry[CAM]["kuma_monitor_id"], 79)

    def test_kuma_read_back_puts_the_registry_right(self):
        self.s.registry = {CAM: {"watch": True, "kuma_monitor_id": 1},                        # deleted in Kuma
                           NVR: {"watch": True, "kuma_monitor_id": 2},                        # paused in Kuma
                           PHONE: {"watch": False, "kuma_monitor_id": 3, "kuma_paused": True},  # resumed in Kuma
                           RADIO: {"watch": True, "kuma_monitor_id": 4}}                      # all fine
        self.listing.return_value = [kmon(2, False), kmon(3, True), kmon(4, True)]
        changed = monitoring._sync(self.s)
        self.assertEqual(changed, {CAM, NVR, PHONE})
        self.assertEqual(self.s.registry[CAM]["kuma_monitor_id"], 0)
        self.assertTrue(self.s.registry[NVR]["kuma_paused"])
        self.assertNotIn("kuma_paused", self.s.registry[PHONE])
        monitoring._apply(self.s, changed)
        self.assertEqual(self.s.registry[CAM]["kuma_monitor_id"], 50)          # made again
        self.assertEqual(sorted(self.act.call_args[0][3]), [(2, True), (3, False)])
        self.listing.return_value = None
        self.assertEqual(monitoring._sync(self.s), set())                      # Kuma unreadable: no guesses

    def test_reconcile_reads_kuma_now_and_then(self):
        self.s.registry = {CAM: {"watch": True, "kuma_monitor_id": 3}}
        monitoring._last_sync = 0
        monitoring.reconcile(self.s)
        self.follow.assert_called_once_with(self.s, [], sync=True)
        monitoring.reconcile(self.s)
        self.assertEqual(self.follow.call_count, 1)                           # not again for SYNC_S

    def test_new_monitors_carry_the_device_marker(self):
        self.s.registry = {CAM: {"watch": True}}
        monitoring._apply(self.s, {CAM})
        item = self.prov.call_args[0][3][0]
        self.assertEqual((item[0], item[4]), (CAM, "netwatch:" + CAM))

    def test_a_lost_reply_is_adopted_not_made_twice(self):
        # an earlier add timed out, but Kuma made the monitor (paused meanwhile)
        self.s.registry = {CAM: {"watch": True}, NVR: {"watch": True}}
        self.listing.return_value = [kmon(40, active=False, description="netwatch:" + CAM),
                                     kmon(41, description="netwatch:" + CAM)]      # a second copy
        monitoring._apply(self.s, {CAM, NVR})
        self.assertEqual([i[0] for i in self.prov.call_args[0][3]], [NVR])           # only NVR is made
        self.assertEqual(self.s.registry[CAM]["kuma_monitor_id"], 40)
        self.assertTrue(self.s.registry[CAM]["kuma_paused"])
        self.follow.assert_called_with(self.s, [CAM])                               # …and resumed next

    def test_read_back_adopts_a_marked_monitor(self):
        self.s.registry = {CAM: {"watch": True}, NVR: {"watch": True, "kuma_token": "tok"}}
        self.listing.return_value = [kmon(42, description="netwatch:" + CAM),
                                     kmon(43, description="netwatch:" + NVR)]       # push-token device: left alone
        self.assertEqual(monitoring._sync(self.s), {CAM})
        self.assertEqual(self.s.registry[CAM]["kuma_monitor_id"], 42)
        self.assertNotIn("kuma_monitor_id", self.s.registry[NVR])

    def test_monitor_name_prefers_the_devices_own_name(self):
        self.assertEqual(sys.modules["scanner"].Scanner._kuma_name(
            {"type": "Access Point / Switch", "device_name": "PTZ2Ap=>MainC", "vendor": "Ubiquiti"}), "PTZ2Ap=>MainC")
        self.assertEqual(sys.modules["scanner"].Scanner._kuma_name({"name": "Gate", "device_name": "X"}), "Gate")

    def test_in_step_monitors_are_not_touched(self):
        self.s.registry = {CAM: {"watch": True, "kuma_monitor_id": 3},
                           NVR: {"watch": False, "kuma_monitor_id": 4, "kuma_paused": True},
                           PHONE: {"watch": False}}
        monitoring._apply(self.s, {CAM, NVR, PHONE})
        self.prov.assert_not_called()
        self.act.assert_not_called()
        self.saves.assert_not_called()

    def test_monitor_deleted_in_kuma_is_recreated(self):
        self.act.side_effect = lambda b, u, p, items: {m: {"ok": False, "gone": True, "error": "You do not own this monitor."}
                                                       for m, _ in items}
        self.s.registry = {CAM: {"watch": True, "kuma_monitor_id": 9, "kuma_paused": True}}
        monitoring._apply(self.s, {CAM})
        self.assertEqual(self.s.registry[CAM]["kuma_monitor_id"], 50)
        self.assertNotIn("kuma_paused", self.s.registry[CAM])

    def test_failed_pause_stays_out_of_step_for_reconcile(self):
        self.act.side_effect = lambda b, u, p, items: {m: {"ok": False, "error": "timeout"} for m, _ in items}
        self.s.registry = {NVR: {"watch": False, "kuma_monitor_id": 4}}
        monitoring._apply(self.s, {NVR})
        self.assertNotIn("kuma_paused", self.s.registry[NVR])
        monitoring.reconcile(self.s)                  # tried just now: not yet
        self.follow.assert_not_called()
        monitoring._tried[NVR] -= monitoring.RETRY_S + 1
        monitoring.reconcile(self.s)
        self.follow.assert_called_once_with(self.s, [NVR], sync=False)

    def test_without_kuma_login_nothing_happens(self):
        monitoring.kuma_login.return_value = None
        self.s.registry = {CAM: {"watch": True}}
        monitoring._apply(self.s, {CAM})
        monitoring.reconcile(self.s)
        self.prov.assert_not_called()
        self.follow.assert_not_called()

    def test_worker_coalesces_and_exits(self):
        calls = []
        with mock.patch.object(monitoring, "_apply", side_effect=lambda s, keys: calls.append(set(keys))):
            self.real_follow(self.s, [CAM, NVR])          # the real queue this time
            for _ in range(50):
                if monitoring._worker is None:
                    break
                time.sleep(0.02)
        self.assertIsNone(monitoring._worker)
        self.assertEqual(calls, [{CAM, NVR}])


class Tidy(Base):
    def setUp(self):
        super().setUp()
        mock.patch.object(monitoring, "kuma_login", return_value=LOGIN).start()
        self.listing = mock.patch.object(monitoring.kuma, "monitor_list").start()
        self.s.registry = {CAM: {"watch": True, "kuma_monitor_id": 5, "name": "Gate"},
                           NVR: {"watch": True, "kuma_monitor_id": 6},
                           PHONE: {"watch": True},
                           "__internet__": {"gateway_ip": "10.0.0.1", "monitors": {"Gateway": 7}}}
        self.s.devices = {CAM: device(CAM, "10.0.0.2", name="Gate", type="IP Camera"),
                          NVR: device(NVR, "10.0.0.3"), PHONE: device(PHONE, "10.0.0.4")}
        self.listing.return_value = [
            kmon(5, name="Gate", hostname="10.0.0.2"), kmon(6), kmon(7, name="Gateway", hostname="10.0.0.1"),
            kmon(8, description="netwatch:" + CAM),                       # our spare copy
            kmon(9, description="netwatch:aa:00:00:00:00:99"),            # ours, device forgotten
            kmon(10, name="IP Camera", hostname="10.0.0.2"),              # unmarked copy under the device's label
            kmon(11, name="Custom check", hostname="10.0.0.2"),           # someone's own check
            kmon(12, name="Internet", hostname="10.0.0.1"),               # an old internet check
            kmon(13, name="Door", type_="push"),
            kmon(14, name="Website", type_="http"),
            kmon(15, description="netwatch:" + PHONE),                    # ours, adopted on the next pass
        ]

    def test_only_netwatchs_own_spare_copies_are_flagged(self):
        rows = {r["id"]: r for r in monitoring.unowned(self.s)}
        self.assertEqual(sorted(rows), [8, 9, 10, 11, 12, 13, 14, 15])       # 5, 6 and 7 belong to something
        self.assertEqual(sorted(i for i, r in rows.items() if r["leftover"]), [8, 9, 10])
        self.assertIn("#5", rows[8]["why"])
        self.assertEqual(rows[10]["device"]["ip"], "10.0.0.2")
        self.listing.return_value = None
        self.assertIsNone(monitoring.unowned(self.s))

    def test_removal_rechecks_and_never_touches_owned_monitors(self):
        with mock.patch.object(monitoring.kuma, "deprovision", return_value={"ok": True}) as dep:
            res = monitoring.remove_unowned(self.s, [5, 7, 8, 99])
        dep.assert_called_once_with(*LOGIN, 8)
        self.assertEqual(res, {"ok": True, "removed": [8], "skipped": [5, 7, 99]})


class Api(Base):
    def setUp(self):
        super().setUp()
        if os.path.exists(siteauth.AUTH_PATH):
            os.remove(siteauth.AUTH_PATH)
        siteauth.set_hub_key(KEY)
        self.c = server.app.test_client()
        self.s.registry = {CAM: {"name": "Gate", "watch": True}, NVR: {"watch": False}}
        self.s.devices = {CAM: device(CAM, "10.0.0.2", watch=False),     # stale carried-over record
                          NVR: device(NVR, "10.0.0.3", online=False, watch=False)}

    def post(self, path, body, hub=False):
        return self.c.post(path, json=body, environ_base=HUB,
                           headers={siteauth.HEADER: KEY} if hub else {})

    def test_changing_monitoring_needs_the_login_or_the_hub(self):
        self.assertEqual(self.post("/api/monitoring", {"monitor": [NVR]}).status_code, 401)
        self.assertEqual(self.post(f"/api/devices/{NVR}", {"watch": True}).status_code, 401)
        self.assertEqual(self.post(f"/api/devices/{NVR}/kuma", {"action": "create"}).status_code, 401)
        self.assertEqual(self.c.delete(f"/api/devices/{CAM}", environ_base=HUB).status_code, 401)
        self.assertEqual(self.post("/api/devices/prune", {"days": 0}).status_code, 401)
        self.assertIn(CAM, self.s.registry)
        self.assertEqual(self.post("/api/kuma/monitor-bulk", {"scope": "identified"}).status_code, 401)
        self.assertFalse(self.s.registry[NVR]["watch"])
        # the rest of the record needs the login too, and does not touch the switch
        self.assertEqual(self.post(f"/api/devices/{NVR}", {"name": "Recorder"}).status_code, 401)
        self.assertEqual(self.post(f"/api/devices/{NVR}", {"name": "Recorder"}, hub=True).status_code, 200)
        self.assertFalse(self.s.registry[NVR]["watch"])

    def test_hub_switches_many_at_once(self):
        r = self.post("/api/monitoring", {"monitor": [NVR], "stop": [CAM]}, hub=True)
        self.assertEqual(r.status_code, 200)
        ev = history.events(key=NVR, etype="monitoring", limit=1)[0]
        self.assertEqual(ev["detail"]["by"], "the hub")
        j = r.get_json()
        self.assertEqual(j["changed"], [{"key": NVR, "monitored": True}, {"key": CAM, "monitored": False}])
        self.assertEqual(j["summary"]["monitored"], 1)
        self.assertTrue(self.s.registry[NVR]["watch"])
        self.assertEqual(self.post(f"/api/devices/{CAM}", {"watch": True}, hub=True).status_code, 200)
        self.assertTrue(self.s.registry[CAM]["watch"])

    def test_kuma_tidy_needs_the_login_and_valid_ids(self):
        for method in ("get", "post"):
            r = getattr(self.c, method)("/api/kuma/unowned", json={"ids": [1]}, environ_base=HUB)
            self.assertEqual(r.status_code, 401, method)
        hub = {siteauth.HEADER: KEY}
        self.assertEqual(self.c.get("/api/kuma/unowned", environ_base=HUB, headers=hub).status_code, 400)  # no Kuma here
        with mock.patch.object(monitoring, "kuma_follows", return_value=True), \
                mock.patch.object(monitoring, "unowned", return_value=[]), \
                mock.patch.object(monitoring, "remove_unowned", return_value={"ok": True, "removed": [3], "skipped": []}) as rm:
            self.assertEqual(self.c.get("/api/kuma/unowned", environ_base=HUB, headers=hub).get_json(), {"ok": True, "monitors": []})
            for bad in ({"ids": []}, {"ids": ["3"]}, {"ids": [True]}, {"ids": [0]}, {}):
                self.assertEqual(self.c.post("/api/kuma/unowned", json=bad, environ_base=HUB, headers=hub).status_code, 400, bad)
            self.assertEqual(self.c.post("/api/kuma/unowned", json={"ids": [3]}, environ_base=HUB, headers=hub).status_code, 200)
            rm.assert_called_once_with(server.scanner, [3])

    def test_bad_bodies(self):
        for body in ({"monitor": "x"}, {"monitor": [1]}, {"monitor": [NVR], "stop": [NVR]},
                     {"monitor": ["k%d" % i for i in range(monitoring.MAX_KEYS + 1)]}):
            self.assertEqual(self.post("/api/monitoring", body, hub=True).status_code, 400, str(body)[:40])

    def test_feed_reads_the_switch_from_the_registry(self):
        self.s.miss = {NVR: 3}
        j = self.c.get("/api/devices").get_json()
        devs = {d["key"]: d for d in j["devices"]}
        self.assertTrue(devs[CAM]["watch"])
        self.assertFalse(devs[NVR]["watch"])
        self.assertEqual((devs[CAM]["missed_scans"], devs[NVR]["missed_scans"]), (0, 3))
        self.assertEqual(j["offline_after"], config.load()["alerts"]["offline_after"])
        j = self.c.get("/api/monitoring").get_json()
        self.assertEqual((j["monitored"], j["online"], j["offline"]), (1, 1, 0))


class Restore(Base):
    def test_a_bundle_from_before_config_rev_runs_the_upgrade(self):
        if os.path.exists(siteauth.AUTH_PATH):
            os.remove(siteauth.AUTH_PATH)
        siteauth.set_hub_key(KEY)
        old_cfg = {k: v for k, v in config.load().items() if k not in ("config_rev", "monitoring")}
        bundle = {"kind": "netwatch-backup", "config": old_cfg,
                  "devices": {CAM: {"name": "Gate", "kuma_monitor_id": 5}, NVR: {"name": "Rec"}}}
        with mock.patch.object(server.scanner, "trigger"), \
                mock.patch.object(monitoring, "kuma_follows", return_value=True):
            r = server.app.test_client().post("/api/config/import", json=bundle, environ_base=HUB,
                                              headers={siteauth.HEADER: KEY})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(server.scanner.registry[CAM]["watch"])          # seeded, not paused
        self.assertNotIn("watch", server.scanner.registry[NVR])
        self.assertEqual(config.load()["config_rev"], 3)
        self.assertFalse(config.load()["alerts"]["allow_commands"])     # older migrations ran too
        self.follow.assert_called_with(server.scanner, [], sync=True)  # Kuma is read back


class Prune(Base):
    def test_monitored_devices_are_never_pruned(self):
        old = int(time.time()) - 40 * 86400
        self.s.registry = {CAM: {"watch": True, "kuma_monitor_id": 5}, NVR: {"kuma_monitor_id": 6, "kuma_paused": True},
                           PHONE: {}}
        self.s.devices = {k: dict(device(k, ip, online=False), last_seen=old)
                          for k, ip in ((CAM, "10.0.0.2"), (NVR, "10.0.0.3"), (PHONE, "10.0.0.4"))}
        with mock.patch.object(self.s, "_save_state"), \
                mock.patch.object(self.s, "_drop_kuma_monitors") as drop, \
                mock.patch.object(sys.modules["scanner"].history, "delete_keys"):
            removed = self.s.prune_devices(days=30)
            for _ in range(50):
                if drop.called:
                    break
                time.sleep(0.02)
        self.assertEqual(sorted(removed), [NVR, PHONE])
        self.assertIn(CAM, self.s.registry)
        drop.assert_called_once_with([6])            # the forgotten device's paused monitor goes too


if __name__ == "__main__":
    unittest.main()
