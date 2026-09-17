"""Prune and Forget: the request answers as soon as the device list is saved,
and the devices' uptime history is deleted in the background, in batches that
let the scan and heartbeat writers in (a prune at Tankwa was ~a million rows
and the HTTP call did not answer within 120 s).

Run inside the site image with a throwaway data dir:
  docker run --rm -v "$PWD/app:/app" -v "$PWD/tests:/tests" -e NETWATCH_DATA=/tmp/nw \
    --entrypoint python farm-netwatch:netcfg /tests/test_prune.py -v
"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import config      # noqa: E402
import history     # noqa: E402
import server      # noqa: E402
import siteauth    # noqa: E402

scanner_mod = sys.modules["scanner"]

OLD, RECENT, WATCHED, UP = "aa:00:00:00:00:01", "aa:00:00:00:00:02", "aa:00:00:00:00:03", "aa:00:00:00:00:04"
KEY = "k" * 43
HUB = {"REMOTE_ADDR": "10.8.0.1"}
NOW = int(time.time())


def device(key, ip, online, last_seen):
    return {"key": key, "ip": ip, "online": online, "category": "camera", "last_seen": last_seen}


def add_history(key, n, newest):
    """n samples + n heartbeats, 5 min apart, the newest at `newest`."""
    stamps = [(key, newest - i * 300) for i in range(n)]
    c = history._conn()
    c.executemany("INSERT INTO samples (key, ts, online) VALUES (?, ?, 0)", stamps)
    c.executemany("INSERT INTO heartbeats (key, ts, online) VALUES (?, ?, 0)", stamps)
    c.commit()


def count_rows(key):
    c = history._conn()
    return tuple(c.execute(f"SELECT COUNT(*) FROM {t} WHERE key = ?", (key,)).fetchone()[0]
                 for t in ("samples", "heartbeats"))


def clear_tables():
    c = history._conn()
    for t in ("samples", "heartbeats", "events"):
        c.execute(f"DELETE FROM {t}")
    c.commit()


def wait_for_purges():
    for t in [t for t in threading.enumerate() if t.name == "history-purge"]:
        t.join(30)
        if t.is_alive():
            raise AssertionError("the history cleanup is still running")


class Routes(unittest.TestCase):
    """The cleanup is held back until the test lets it go: the answer must not
    wait for it."""

    def setUp(self):
        cfg = config.load()
        cfg["configured"] = True
        config.save(cfg)
        if os.path.exists(siteauth.AUTH_PATH):
            os.remove(siteauth.AUTH_PATH)
        siteauth.set_hub_key(KEY)
        self.c = server.app.test_client()
        self.s = server.scanner
        self.s.registry = {OLD: {"name": "Old phone"}, WATCHED: {"name": "Gate", "watch": True}}
        self.s.devices = {OLD: device(OLD, "10.0.0.1", False, NOW - 40 * 86400),
                          RECENT: device(RECENT, "10.0.0.2", False, NOW - 3600),
                          WATCHED: device(WATCHED, "10.0.0.3", False, NOW - 40 * 86400),
                          UP: device(UP, "10.0.0.4", True, NOW)}
        self.s.seen_keys = set(self.s.devices)
        clear_tables()
        for k, d in self.s.devices.items():
            add_history(k, 50, NOW - 600)
            history.log_events([history.build_event("new", d)])

        self.gate = threading.Event()
        real = history.delete_keys

        def held(keys, before=None):
            self.gate.wait(20)
            return real(keys, before=before)

        patch = mock.patch.object(history, "delete_keys", side_effect=held)
        self.purge = patch.start()
        self.addCleanup(patch.stop)          # cleanups run last-first:
        self.addCleanup(wait_for_purges)
        self.addCleanup(self.gate.set)       # let the cleanup go, wait for it, unpatch

    def call(self, method, path, body=None):
        t0 = time.monotonic()
        r = getattr(self.c, method)(path, json=body, environ_base=HUB, headers={siteauth.HEADER: KEY})
        return r, time.monotonic() - t0

    def saved(self):
        with open(scanner_mod.REGISTRY_PATH) as f:
            reg = json.load(f)
        with open(scanner_mod.STATE_PATH) as f:
            state = json.load(f)["devices"]
        return reg, state

    def test_prune_answers_before_the_history_is_deleted(self):
        r, took = self.call("post", "/api/devices/prune", {"days": 7})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json(), {"ok": True, "removed": 1, "keys": [OLD]})
        self.assertLess(took, 5)
        reg, state = self.saved()                       # the device list is saved first …
        self.assertNotIn(OLD, reg)
        self.assertNotIn(OLD, state)
        self.assertIn(RECENT, state)                    # (offline for an hour only)
        self.assertEqual(count_rows(OLD), (50, 50))     # … its history is still waiting
        self.gate.set()
        wait_for_purges()
        self.assertEqual(self.purge.call_args.args[0], [OLD])
        self.assertEqual(count_rows(OLD), (0, 0))
        for k in (RECENT, WATCHED, UP):
            self.assertEqual(count_rows(k), (50, 50), k)
        self.assertEqual(len(history.events(key=OLD)), 1)   # the event log stays

    def check_every_offline_device_goes(self, days):
        r, took = self.call("post", "/api/devices/prune", {"days": days})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertLess(took, 5)
        self.assertEqual(sorted(r.get_json()["keys"]), [OLD, RECENT])
        self.assertIn(WATCHED, self.s.registry)         # monitored: never pruned
        self.assertEqual(set(self.s.devices), {WATCHED, UP})
        self.gate.set()
        wait_for_purges()
        self.assertEqual(count_rows(OLD), (0, 0))
        self.assertEqual(count_rows(RECENT), (0, 0))
        self.assertEqual(count_rows(WATCHED), (50, 50))
        self.assertEqual(count_rows(UP), (50, 50))
        for k in (OLD, RECENT):
            self.assertEqual(len(history.events(key=k)), 1)

    def test_null_days_removes_every_offline_device_but_no_monitored_one(self):
        self.check_every_offline_device_goes(None)

    def test_zero_days_is_the_same_as_null(self):
        self.check_every_offline_device_goes(0)

    def test_forget_answers_before_the_history_is_deleted(self):
        r, took = self.call("delete", f"/api/devices/{OLD}")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertLess(took, 5)
        reg, state = self.saved()
        self.assertNotIn(OLD, reg)
        self.assertNotIn(OLD, state)
        self.assertEqual(count_rows(OLD), (50, 50))
        self.gate.set()
        wait_for_purges()
        self.assertEqual(self.purge.call_args.args[0], [OLD])
        self.assertEqual(count_rows(OLD), (0, 0))
        self.assertEqual(count_rows(RECENT), (50, 50))
        self.assertEqual(len(history.events(key=OLD)), 1)


class Batches(unittest.TestCase):
    def setUp(self):
        wait_for_purges()
        clear_tables()

    def test_other_writers_get_in_between_batches(self):
        add_history(OLD, 1050, NOW - 60)
        add_history(RECENT, 3, NOW - 60)
        rests, real_sleep = [], time.sleep

        def rest(s):
            if threading.current_thread() is not threading.main_thread():
                return real_sleep(s)
            rests.append(s)
            # a scan writing now must not have to wait at all
            other = sqlite3.connect(history.DB_PATH, timeout=0)
            try:
                other.execute("INSERT INTO samples (key, ts, online) VALUES ('scan', ?, 1)", (NOW,))
                other.commit()
            finally:
                other.close()

        with mock.patch.object(history, "PURGE_ROWS", (100, 100, 100)), \
                mock.patch.object(history.time, "sleep", side_effect=rest):
            n = history.delete_keys([OLD])
        self.assertEqual(n, 2100)
        self.assertEqual(count_rows(OLD), (0, 0))
        self.assertEqual(len(rests), 22)            # 10 full batches + 1 part, per table
        self.assertTrue(all(s >= history.PURGE_PAUSE_S for s in rests))
        self.assertEqual(count_rows("scan"), (22, 0))
        self.assertEqual(count_rows(RECENT), (3, 3))

    def test_batch_size_follows_how_long_a_batch_takes(self):
        add_history(OLD, 3000, NOW - 60)

        slow, quick = history.PURGE_TARGET_S * 4, history.PURGE_TARGET_S / 50

        def clock():                     # read at the start and end of each batch
            t, n = 0.0, 0
            while True:
                yield t
                n += 1
                t += slow if n <= 2 else quick   # two slow batches, then quick ones
                yield t

        left, sizes, rests = [6000], [], []

        def rest(s):
            now = sum(count_rows(OLD))
            sizes.append(left[0] - now)
            left[0] = now
            rests.append(s)

        with mock.patch.object(history.time, "monotonic", side_effect=clock()), \
                mock.patch.object(history.time, "sleep", side_effect=rest):
            self.assertEqual(history.delete_keys([OLD]), 6000)
        # halved while slow, doubled while quick; the size carries on to the heartbeats
        self.assertEqual(sizes, [1000, 500, 250, 500, 750, 2000, 1000])
        self.assertEqual(rests[:3], [slow, slow, history.PURGE_PAUSE_S])   # rests as long as it took

    def test_a_device_that_came_back_keeps_its_new_samples(self):
        add_history(OLD, 10, NOW - 1200)            # from before it was forgotten
        add_history(OLD, 1, NOW - 100)              # since it came back
        add_history(OLD, 1, NOW)
        self.assertEqual(history.delete_keys([OLD], before=NOW - 1000), 20)
        self.assertEqual(count_rows(OLD), (2, 2))

    def test_many_devices_at_once(self):
        keys = [f"10.1.{i // 250}.{i % 250}" for i in range(1201)]
        c = history._conn()
        c.executemany("INSERT INTO samples (key, ts, online) VALUES (?, ?, 0)", [(k, NOW - 60) for k in keys])
        c.commit()
        add_history(RECENT, 2, NOW - 60)
        self.assertEqual(history.delete_keys(keys + keys[:3]), 1201)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 2)
        self.assertEqual(history.delete_keys([]), 0)


if __name__ == "__main__":
    unittest.main()
