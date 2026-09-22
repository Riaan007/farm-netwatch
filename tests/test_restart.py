"""Restarting a device (app/restart.py): which method, and what counts as proof.

The point of these tests is the promise the feature makes — a restart is only
reported when the device actually dropped off the network and came back, and a
rejected login never reaches the device at all.

Run inside the site image:
  docker run --rm -v "$PWD/app:/app" -v "$PWD/tests:/tests" -e NETWATCH_DATA=/tmp/nw \\
    --entrypoint python farm-netwatch:netcfg /tests/test_restart.py -v
"""
import itertools
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import restart    # noqa: E402

CAM = {"key": "aa:1", "ip": "10.0.0.5", "name": "Front camera", "category": "camera",
       "vendor": "Hangzhou Hikvision Digital Technology", "ports": [80, 443, 554]}
RADIO = {"key": "aa:2", "ip": "10.0.0.6", "name": "Tower LiteAP", "category": "network",
         "vendor": "Ubiquiti Networks Inc.", "ports": [22, 80, 443]}
ESW = {"key": "aa:3", "ip": "10.0.0.7", "name": "Tower Switch", "category": "network",
       "vendor": "Ubiquiti Inc.", "model": "ES-8-150W", "ports": [22, 80, 443]}
SWOS = {"key": "aa:4", "ip": "10.0.0.8", "name": "CSS106", "category": "network",
        "vendor": "Routerboard.com", "banner": {"title": "MikroTik SwOS"}, "ports": [80]}
ROUTER = {"key": "aa:5", "ip": "10.0.0.9", "name": "hEX", "category": "network",
          "vendor": "Routerboard.com", "model": "RB960PGS", "ports": [22, 80, 8291]}
PRINTER = {"key": "aa:6", "ip": "10.0.0.10", "name": "Printer", "category": "printer",
           "vendor": "Hewlett Packard", "ports": [9100]}


class MethodTest(unittest.TestCase):
    def test_each_family_gets_its_own_method(self):
        for dev, want in ((CAM, "hikvision"), (RADIO, "airos"), (ESW, "edgeswitch"),
                          (SWOS, "swos"), (ROUTER, "mikrotik")):
            self.assertEqual(restart.method_for(dev)["id"], want, dev["name"])

    def test_a_switch_is_a_switch_before_it_is_a_mikrotik(self):
        # SwOS runs on RouterBoard hardware: the vendor says MikroTik, but it has
        # no RouterOS API, so the switch method has to win.
        self.assertEqual(restart.method_for(SWOS)["id"], "swos")

    def test_unknown_devices_have_no_method(self):
        self.assertIsNone(restart.method_for(PRINTER))
        self.assertIsNone(restart.method_for(None))

    def test_routers_and_switches_name_the_setting_that_gates_them(self):
        self.assertEqual(restart.method_for(ROUTER)["gate"], "mikrotik_manage")
        self.assertEqual(restart.method_for(ESW)["gate"], "switch_manage")
        self.assertIsNone(restart.method_for(CAM)["gate"])


class WatchTest(unittest.TestCase):
    def test_the_scanned_ports_are_knocked_on_first(self):
        ports = restart.watch_ports(CAM)
        self.assertEqual(ports[:3], [80, 443, 554])

    def test_a_device_with_no_known_ports_still_gets_probed(self):
        self.assertTrue(restart.watch_ports({"ip": "10.0.0.1"}))

    def test_one_flap_does_not_count_as_a_change(self):
        # answering, one miss, answering again: not enough to call it down.
        readings = script([True, False, True])
        with mock.patch.object(restart, "alive", lambda ip, p: next(readings)), \
             mock.patch.object(restart, "POLL_S", 0):
            self.assertIsNone(restart._settle("1.2.3.4", [80], False, time.time() + 0.3, lambda: None))

    def test_two_readings_in_a_row_settle_it(self):
        readings = script([True, False, False])
        with mock.patch.object(restart, "alive", lambda ip, p: next(readings)), \
             mock.patch.object(restart, "POLL_S", 0):
            self.assertIsNotNone(restart._settle("1.2.3.4", [80], False, time.time() + 5, lambda: None))


def script(readings):
    """The scripted readings, then that last state forever — a test must never
    depend on the network changing just because the list ran out."""
    return itertools.chain(readings, itertools.repeat(readings[-1]))


def run_job(dev, readings, test_result="ok", send=None):
    """Drive one whole restart with a scripted view of the network."""
    restart._jobs.pop(dev["key"], None)
    seen = {"sent": 0}

    def fake_send(method, d, ip, user, pw):
        seen["sent"] += 1
        return send if send is not None else {"ok": True, "msg": "sent"}

    it = script(readings)
    with mock.patch.object(restart.credtest, "test", lambda *a: {"result": test_result, "detail": ""}), \
         mock.patch.object(restart, "_send", fake_send), \
         mock.patch.object(restart, "alive", lambda ip, p: next(it)), \
         mock.patch.object(restart, "POLL_S", 0), \
         mock.patch.object(restart, "DOWN_WAIT_S", 2), \
         mock.patch.object(restart, "UP_WAIT_S", 2):
        restart.start(dev, "admin", "pw", restart.method_for(dev))
        for _ in range(200):
            job = restart.state(dev["key"])
            if job and job.get("done"):
                break
            time.sleep(0.02)
    return restart.state(dev["key"]), seen


class JobTest(unittest.TestCase):
    def test_down_then_up_is_the_only_thing_called_restarted(self):
        job, seen = run_job(CAM, [False, False, True, True])
        self.assertEqual(job["verdict"], "restarted")
        self.assertTrue(job["ok"])
        self.assertEqual(seen["sent"], 1)

    def test_a_rejected_login_never_reaches_the_device(self):
        job, seen = run_job(CAM, [True], test_result="auth_failed")
        self.assertEqual(job["verdict"], "auth_failed")
        self.assertFalse(job["ok"])
        self.assertEqual(seen["sent"], 0, "a restart was sent after the login was rejected")

    def test_an_unreachable_device_is_not_restarted(self):
        job, seen = run_job(CAM, [False], test_result="unreachable")
        self.assertEqual(job["verdict"], "unreachable")
        self.assertEqual(seen["sent"], 0)

    def test_a_device_that_never_goes_quiet_is_not_reported_as_restarted(self):
        job, _ = run_job(CAM, [True])
        self.assertEqual(job["verdict"], "no_downtime")
        self.assertFalse(job["ok"])

    def test_a_device_that_does_not_come_back_says_so(self):
        job, _ = run_job(CAM, [False])
        self.assertEqual(job["verdict"], "still_down")
        self.assertFalse(job["ok"])

    def test_a_refused_restart_is_a_failure_not_a_wait(self):
        job, _ = run_job(CAM, [True], send={"ok": False, "error": "nope"})
        self.assertEqual(job["verdict"], "failed")
        self.assertIn("nope", job["msg"])

    def test_the_time_offline_is_reported(self):
        job, _ = run_job(CAM, [False, False, True, True])
        self.assertIn("seconds_down", job)

    def test_only_one_restart_per_device_at_a_time(self):
        restart._jobs.pop(RADIO["key"], None)
        with mock.patch.object(restart.credtest, "test", lambda *a: time.sleep(0.4) or {"result": "ok"}), \
             mock.patch.object(restart, "_send", lambda *a: {"ok": True}), \
             mock.patch.object(restart, "alive", lambda ip, p: True), \
             mock.patch.object(restart, "POLL_S", 0), \
             mock.patch.object(restart, "DOWN_WAIT_S", 1):
            first = restart.start(RADIO, "u", "p", restart.method_for(RADIO))
            second = restart.start(RADIO, "u", "p", restart.method_for(RADIO))
            self.assertIsNotNone(first)
            self.assertIsNone(second, "a second restart started while the first was running")
            for _ in range(200):
                if (restart.state(RADIO["key"]) or {}).get("done"):
                    break
                time.sleep(0.02)


class SendTest(unittest.TestCase):
    def test_a_radio_that_refuses_the_ssh_login_is_flagged_as_auth(self):
        with mock.patch.object(restart.airos, "_ssh", lambda *a, **k: (255, "", "Permission denied, please try again.")):
            res = restart._send_airos(RADIO, "10.0.0.6", "ubnt", "wrong")
        self.assertFalse(res["ok"])
        self.assertTrue(res["auth"])

    def test_a_dropped_ssh_session_is_the_expected_reply(self):
        # airOS kills the session as it reboots — that is not a failure.
        with mock.patch.object(restart.airos, "_ssh", lambda *a, **k: (255, "", "Connection closed by remote host")):
            self.assertTrue(restart._send_airos(RADIO, "10.0.0.6", "ubnt", "pw")["ok"])

    def test_a_camera_that_rejects_the_login_is_flagged_as_auth(self):
        with mock.patch.object(restart.hikvision, "_net_get_raw", lambda *a: ("AUTH", None, None)):
            res = restart._send_hikvision(CAM, "10.0.0.5", "admin", "wrong")
        self.assertFalse(res["ok"])
        self.assertTrue(res["auth"])

    def test_a_camera_that_cannot_be_reached_is_not_an_auth_problem(self):
        with mock.patch.object(restart.hikvision, "_net_get_raw", lambda *a: None):
            res = restart._send_hikvision(CAM, "10.0.0.5", "admin", "pw")
        self.assertFalse(res["ok"])
        self.assertNotIn("auth", res)


if __name__ == "__main__":
    unittest.main(verbosity=2)
