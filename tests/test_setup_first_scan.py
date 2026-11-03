"""Setup: a new Pi waits for its first scan, and the wizard's fixed-IP step.

Run inside the site image with a throwaway data dir:
  docker run --rm --network none -e PYTHONDONTWRITEBYTECODE=1 \
    -v "$PWD/app:/app:ro" -v "$PWD/tests:/tests:ro" -e NETWATCH_DATA=/tmp/nw \
    --entrypoint python farm-netwatch:netcfg /tests/test_setup_first_scan.py -v
"""
import ipaddress
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import config      # noqa: E402
import server      # noqa: E402
import setupip     # noqa: E402

scanner_mod = sys.modules["scanner"]
WIZARD = {"site": {"name": "Test", "location": ""},
          "targets": [{"cidr": "192.168.1.0/24", "label": "Main"}]}
PRIMARY = {"iface": "eth0", "address": "192.168.1.91/24", "mac": "b8:27:eb:00:00:01", "dhcp": True}
SWEEP = """<?xml version="1.0"?><nmaprun>
<host><status state="up"/><address addr="192.168.1.20" addrtype="ipv4"/><address addr="AA:BB:CC:00:00:01" addrtype="mac" vendor="MikroTik"/></host>
<host><status state="up"/><address addr="192.168.1.250" addrtype="ipv4"/><address addr="AA:BB:CC:00:00:02" addrtype="mac" vendor="Hikvision"/></host>
<host><status state="down"/><address addr="192.168.1.249" addrtype="ipv4"/></host>
<host><status state="up"/><address addr="192.168.1.91" addrtype="ipv4"/></host>
</nmaprun>"""


def fresh_config(**extra):
    path = config.CONFIG_PATH
    if os.path.exists(path):
        os.remove(path)
    if extra:
        config.update(extra)


class FirstScanHold(unittest.TestCase):
    def setUp(self):
        fresh_config()
        self.c = server.app.test_client()

    def test_wizard_on_new_pi_holds_scanning(self):
        with mock.patch.object(scanner_mod.scanner, "trigger") as trig, \
                mock.patch.object(scanner_mod.scanner, "wake") as wake:
            r = self.c.post("/api/wizard", json=WIZARD)
        self.assertEqual(r.status_code, 200)
        trig.assert_not_called()
        wake.assert_not_called()
        cfg = config.load()
        self.assertTrue(cfg["configured"])
        self.assertTrue(cfg["first_scan_pending"])
        self.assertTrue(self.c.get("/api/status").get_json()["first_scan_pending"])

    def test_rerunning_wizard_on_a_live_pi_does_not_hold(self):
        fresh_config(configured=True)
        with mock.patch.object(server.siteauth, "allowed", return_value=True), \
                mock.patch.object(scanner_mod.scanner, "trigger"):
            self.c.post("/api/wizard", json=WIZARD)
        self.assertFalse(config.load().get("first_scan_pending"))

    def test_loop_skips_while_pending(self):
        fresh_config(configured=True, first_scan_pending=True)
        sc = scanner_mod.scanner
        calls = []

        def stop_after_one_wait(timeout=None):
            sc._stop = True
            return True
        with mock.patch.object(sc, "run_scan", side_effect=lambda *a, **k: calls.append(a)), \
                mock.patch.object(sc._wake, "wait", side_effect=stop_after_one_wait):
            sc._stop = False
            sc.loop()
        sc._stop = False
        self.assertEqual(calls, [])

    def test_network_scan_clears_hold_but_single_host_does_not(self):
        fresh_config(configured=True, first_scan_pending=True)
        sc = scanner_mod.scanner
        with mock.patch.object(sc, "_run_scan"):
            sc.run_scan("quick", only_hosts=["192.168.1.5"])
            self.assertTrue(config.load()["first_scan_pending"])
            sc.run_scan("quick")
        self.assertFalse(config.load()["first_scan_pending"])


class FixedIp(unittest.TestCase):
    def setUp(self):
        fresh_config()
        self.c = server.app.test_client()

    def test_sweep_parse_and_suggestion(self):
        used = setupip._up_hosts(SWEEP)
        self.assertEqual(set(used), {"192.168.1.20", "192.168.1.250", "192.168.1.91"})
        self.assertEqual(used["192.168.1.250"]["vendor"], "Hikvision")
        net = ipaddress.ip_network("192.168.1.0/24")
        # .251-.254 are skipped, .250 is taken → .249
        self.assertEqual(setupip.suggest(net, set(used)), "192.168.1.249")

    def test_plan_never_saves_devices(self):
        with mock.patch.object(setupip, "primary", return_value=PRIMARY), \
                mock.patch.object(setupip, "_nmap", return_value=SWEEP), \
                mock.patch.object(setupip, "_connection", return_value=None), \
                mock.patch.object(server, "default_gateway", return_value="192.168.1.20"):
            j = self.c.get("/api/setup/ip-plan").get_json()
        self.assertTrue(j["ok"])
        self.assertEqual(j["ip"], "192.168.1.91")
        self.assertEqual(j["suggested"], "192.168.1.249")
        self.assertNotIn("192.168.1.91", [d["ip"] for d in j["used"]])
        self.assertFalse(j["can_apply"])
        self.assertEqual(scanner_mod.scanner.get_devices(), [])

    def test_check_refuses_router_taken_and_outside(self):
        with mock.patch.object(setupip, "primary", return_value=PRIMARY), \
                mock.patch.object(setupip, "_nmap", return_value=SWEEP):
            self.assertFalse(setupip.check("192.168.1.20", "192.168.1.20")[0])
            self.assertFalse(setupip.check("192.168.1.250", "192.168.1.20")[0])
            self.assertFalse(setupip.check("10.0.0.5", "192.168.1.20")[0])
            self.assertFalse(setupip.check("192.168.1.255", "192.168.1.20")[0])
            self.assertTrue(setupip.check("192.168.1.91", "192.168.1.20")[0])
        with mock.patch.object(setupip, "primary", return_value=PRIMARY), \
                mock.patch.object(setupip, "_nmap", return_value="<nmaprun/>"):
            self.assertTrue(setupip.check("192.168.1.240", "192.168.1.20")[0])

    def test_wizard_without_networkmanager_gives_reservation(self):
        with mock.patch.object(setupip, "primary", return_value=PRIMARY), \
                mock.patch.object(setupip, "_nmap", return_value="<nmaprun/>"), \
                mock.patch.object(setupip, "_connection", return_value=None), \
                mock.patch.object(server, "default_gateway", return_value="192.168.1.20"):
            j = self.c.post("/api/wizard", json=dict(WIZARD, fixed_ip="192.168.1.240")).get_json()
        self.assertEqual(j["fixed_ip"], {"applied": False, "ip": "192.168.1.240",
                                         "mac": "b8:27:eb:00:00:01", "reason": "reservation"})

    def test_wizard_refuses_a_taken_address_and_saves_nothing(self):
        with mock.patch.object(setupip, "primary", return_value=PRIMARY), \
                mock.patch.object(setupip, "_nmap", return_value=SWEEP), \
                mock.patch.object(server, "default_gateway", return_value="192.168.1.20"):
            r = self.c.post("/api/wizard", json=dict(WIZARD, fixed_ip="192.168.1.250"))
        self.assertEqual(r.status_code, 400)
        self.assertFalse(config.load()["configured"])

    def test_apply_with_networkmanager_needs_the_token_to_keep_it(self):
        applied = []
        with mock.patch.object(setupip, "primary", return_value=PRIMARY), \
                mock.patch.object(setupip, "_nmap", return_value="<nmaprun/>"), \
                mock.patch.object(setupip, "_connection", return_value="Wired connection 1"), \
                mock.patch.object(setupip.netcfg, "device_runtime", return_value={"dns": "192.168.1.20"}), \
                mock.patch.object(setupip.netcfg, "apply_static_with_revert",
                                  side_effect=lambda *a: applied.append(a) or (True, "", 0)), \
                mock.patch.object(setupip.netcfg, "confirm_static") as conf, \
                mock.patch.object(setupip.threading, "Timer",
                                  side_effect=lambda d, fn: mock.Mock(start=fn)):
            out = setupip.apply("192.168.1.240", "192.168.1.20")
            self.assertTrue(out["applied"])
            self.assertEqual(applied[0][:4], ("Wired connection 1", "192.168.1.240/24",
                                              "192.168.1.20", "192.168.1.20"))
            self.assertEqual(self.c.post("/api/setup/confirm-ip", json={"token": "nope"}).status_code, 403)
            conf.assert_not_called()
            self.assertEqual(self.c.post("/api/setup/confirm-ip", json={"token": out["token"]}).status_code, 200)
            conf.assert_called_once()
            # one-time
            self.assertEqual(self.c.post("/api/setup/confirm-ip", json={"token": out["token"]}).status_code, 403)

    def test_setup_routes_need_login_once_configured(self):
        fresh_config(configured=True)
        server.siteauth.set_password("correct horse 1")
        try:
            self.assertEqual(self.c.get("/api/setup/ip-plan").status_code, 401)
            self.assertEqual(self.c.post("/api/setup/ip-check", json={"ip": "192.168.1.5"}).status_code, 401)
        finally:
            os.remove(os.path.join(os.environ["NETWATCH_DATA"], "auth.json"))


if __name__ == "__main__":
    unittest.main()
