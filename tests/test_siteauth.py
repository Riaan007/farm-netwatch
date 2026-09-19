"""Site login (app/siteauth.py) against the real Flask app.

Run inside the site image with a throwaway data dir:
  docker run --rm -v "$PWD/app:/app" -v "$PWD/tests:/tests" -e NETWATCH_DATA=/tmp/nw \
    --entrypoint python farm-netwatch:netcfg /tests/test_siteauth.py -v
"""
import os
import sys
import tempfile
import unittest

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import config      # noqa: E402
import creds       # noqa: E402
import server      # noqa: E402
import siteauth    # noqa: E402

HUB = {"REMOTE_ADDR": "10.8.0.1"}
LAN = {"REMOTE_ADDR": "192.168.0.50"}
KEY = "k" * 43
PROTECTED = [
    ("get", "/api/devices/aa:bb/credentials"),
    ("post", "/api/devices/aa:bb/credentials"),
    ("post", "/api/devices/aa:bb/credentials/test"),
    ("post", "/api/credentials/bulk"),
    ("get", "/api/config/export"),
    ("post", "/api/config/import"),
    ("post", "/api/config"),
    ("get", "/api/config?full=1"),
    ("post", "/api/setup"),
    ("post", "/api/wizard"),
    ("post", "/api/hub/connect"),
    ("post", "/api/hub/disconnect"),
    ("post", "/api/kuma/test"),
    ("post", "/api/kuma/monitor-bulk"),
    ("post", "/api/kuma/repair"),
    ("post", "/api/kuma/sync-tags"),
    ("post", "/api/devices/aa:bb/kuma"),
    ("post", "/api/devices/aa:bb"),
    ("delete", "/api/devices/aa:bb"),
    ("post", "/api/devices/aa:bb/location"),
    ("post", "/api/devices/aa:bb/asset"),
    ("post", "/api/devices/aa:bb/photo"),
    ("delete", "/api/devices/aa:bb/photo"),
    ("post", "/api/bridge-macs"),
    ("post", "/api/monitoring"),
    ("get", "/api/kuma/unowned"),
    ("post", "/api/kuma/unowned"),
    ("post", "/api/network/address"),
    ("post", "/api/network/static"),
    ("post", "/api/network/static/confirm"),
    ("post", "/api/network/dhcp"),
    ("post", "/api/devices/aa:bb/hikvision"),
    ("get", "/api/devices/aa:bb/network"),
    ("post", "/api/devices/aa:bb/set-ip"),
    ("get", "/api/devices/aa:bb/airos-network"),
    ("get", "/api/devices/aa:bb/airos-wifi"),
    ("post", "/api/devices/aa:bb/airos-set-ip"),
    ("post", "/api/auth/password"),
    ("get", "/api/tunnel"),
    ("post", "/api/tunnel"),
    ("delete", "/api/tunnel/abc"),
]


class SiteAuth(unittest.TestCase):
    def setUp(self):
        for p in (siteauth.AUTH_PATH,):
            if os.path.exists(p):
                os.remove(p)
        siteauth._fails.clear()
        cfg = config.load()
        cfg["configured"] = True
        cfg["alerts"]["ntfy_topic"] = "secret-topic"
        config.save(cfg)
        creds.set_("aa:bb", "admin", "hunter22", "")
        self.c = server.app.test_client()

    def call(self, method, path, env=LAN, **kw):
        return getattr(self.c, method)(path, environ_base=env, json=kw.pop("json", {}), **kw)

    def test_everything_sensitive_is_locked_for_anonymous(self):
        for method, path in PROTECTED:
            r = self.call(method, path)
            self.assertEqual(r.status_code, 401, f"{method} {path}")
            self.assertEqual(r.get_json()["error"], "auth_required")
        self.assertNotIn(b"hunter22", self.call("get", "/api/devices/aa:bb/credentials").data)

    def test_open_endpoints_still_open(self):
        for path in ("/api/status", "/api/devices", "/api/sysinfo", "/api/auth/state",
                     "/api/devices/aa:bb/asset", "/api/bridge-macs", "/api/devices/aa:bb/kuma"):
            self.assertEqual(self.call("get", path).status_code, 200, path)

    def test_kuma_push_token_only_for_the_login_or_hub_key(self):
        # Whoever holds a push token can send the monitor fake "up" beats.
        server.scanner.registry["aa:bb"] = {"kuma_token": "push-secret-1", "kuma_monitor_id": 0}
        self.addCleanup(server.scanner.registry.pop, "aa:bb", None)
        self.call("post", "/api/auth/claim-hub", env=HUB, json={"key": KEY})
        siteauth.set_password("farm-pass-1")

        def kuma(headers=None):
            r = self.call("get", "/api/devices/aa:bb/kuma", headers=headers or {})
            self.assertEqual(r.status_code, 200)
            return r

        anon = kuma()
        self.assertNotIn(b"push-secret-1", anon.data)
        j = anon.get_json()
        self.assertEqual((j["token"], j["push_url"], j["has_token"]), ("", "", True))
        for field in ("monitor_id", "paused", "monitored", "follows", "health_url"):
            self.assertIn(field, j)                 # the page shows these before any login
        self.assertNotIn(b"push-secret-1", kuma({siteauth.HEADER: "x" * 43}).data)

        j = kuma({siteauth.HEADER: KEY}).get_json()
        self.assertEqual(j["token"], "push-secret-1")
        self.assertIn("/api/push/push-secret-1?status=up", j["push_url"])

        self.call("post", "/api/auth/login", json={"password": "farm-pass-1"})
        self.assertEqual(kuma().get_json()["token"], "push-secret-1")
        self.call("post", "/api/auth/logout")
        self.assertNotIn(b"push-secret-1", kuma().data)

        # an anonymous save is refused, so it cannot blank the token either
        self.assertEqual(self.call("post", "/api/devices/aa:bb/kuma", json={"token": ""}).status_code, 401)
        self.assertEqual(server.scanner.registry["aa:bb"]["kuma_token"], "push-secret-1")

    def test_config_redacts_topic_for_anonymous(self):
        j = self.call("get", "/api/config").get_json()
        self.assertEqual(j["alerts"]["ntfy_topic"], "")
        self.assertTrue(j["alerts"]["ntfy_topic_set"])
        self.assertTrue(j["redacted"])

    def test_claim_only_from_hub_ip_once(self):
        self.assertEqual(self.call("post", "/api/auth/claim-hub", json={"key": KEY}).status_code, 403)
        fwd = self.call("post", "/api/auth/claim-hub", env=HUB, json={"key": KEY},
                        headers={"X-Forwarded-For": "192.168.88.10"})
        self.assertEqual(fwd.status_code, 403, "proxied requests must not claim")
        self.assertEqual(self.call("post", "/api/auth/claim-hub", env=HUB, json={"key": "short"}).status_code, 400)
        self.assertEqual(self.call("post", "/api/auth/claim-hub", env=HUB, json={"key": KEY}).status_code, 200)
        # second claim with another key is refused, even from the hub address
        self.assertEqual(self.call("post", "/api/auth/claim-hub", env=HUB, json={"key": "z" * 43}).status_code, 409)
        # rotating with the current key works
        r = self.call("post", "/api/auth/claim-hub", env=HUB, json={"key": "n" * 43},
                      headers={siteauth.HEADER: KEY})
        self.assertEqual(r.status_code, 200)

    def test_hub_key_unlocks_export_and_status_reports_it(self):
        self.call("post", "/api/auth/claim-hub", env=HUB, json={"key": KEY})
        h = {siteauth.HEADER: KEY}
        exp = self.call("get", "/api/config/export", headers=h)
        self.assertEqual(exp.status_code, 200)
        self.assertEqual(exp.get_json()["kind"], "netwatch-backup")
        self.assertNotIn("auth.json", exp.get_data(as_text=True))
        self.assertTrue(self.call("get", "/api/status", headers=h).get_json()["auth"]["hub"])
        bad = self.call("get", "/api/config/export", headers={siteauth.HEADER: "x" * 43})
        self.assertEqual(bad.status_code, 401)

    def test_no_password_means_nobody_on_lan_can_set_one(self):
        self.assertEqual(self.call("post", "/api/auth/password", json={"password": "letmein99"}).status_code, 401)
        self.assertEqual(self.call("post", "/api/auth/login", json={"password": "anything1"}).status_code, 403)

    def test_hub_sets_password_then_person_logs_in(self):
        self.call("post", "/api/auth/claim-hub", env=HUB, json={"key": KEY})
        h = {siteauth.HEADER: KEY}
        self.assertEqual(self.call("post", "/api/auth/password", headers=h, json={"password": "short"}).status_code, 400)
        self.assertEqual(self.call("post", "/api/auth/password", headers=h, json={"password": "farm-pass-1"}).status_code, 200)
        self.assertEqual(self.call("post", "/api/auth/login", json={"password": "wrong-pass"}).status_code, 401)
        self.assertEqual(self.call("post", "/api/auth/login", json={"password": "farm-pass-1"}).status_code, 200)
        r = self.call("get", "/api/devices/aa:bb/credentials")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["password"], "hunter22")
        self.assertEqual(self.call("get", "/api/config?full=1").get_json()["alerts"]["ntfy_topic"], "secret-topic")
        # a password change elsewhere signs this browser out
        self.call("post", "/api/auth/password", headers=h, json={"password": "farm-pass-2"})
        self.assertEqual(self.call("get", "/api/devices/aa:bb/credentials").status_code, 401)
        # logout
        self.call("post", "/api/auth/login", json={"password": "farm-pass-2"})
        self.call("post", "/api/auth/logout")
        self.assertEqual(self.call("get", "/api/devices/aa:bb/credentials").status_code, 401)

    def test_login_throttle(self):
        siteauth.set_password("farm-pass-1")
        real_sleep = siteauth.time.sleep
        siteauth.time.sleep = lambda s: None
        self.addCleanup(setattr, siteauth.time, "sleep", real_sleep)
        for _ in range(siteauth.FAIL_MAX):
            self.call("post", "/api/auth/login", json={"password": "nope-nope"})
        r = self.call("post", "/api/auth/login", json={"password": "farm-pass-1"})
        self.assertEqual(r.status_code, 429)

    def test_first_run_setup_stays_open_until_configured(self):
        cfg = config.load()
        cfg["configured"] = False
        config.save(cfg)
        r = self.call("post", "/api/wizard", json={"targets": []})
        self.assertEqual(r.status_code, 400)   # reached the handler (validation), not 401


class DeviceLocation(unittest.TestCase):
    def setUp(self):
        server.scanner.registry["aa:bb:cc:dd:ee:01"] = {"name": "Gate cam"}
        if os.path.exists(siteauth.AUTH_PATH):
            os.remove(siteauth.AUTH_PATH)
        siteauth.set_hub_key(KEY)                  # the hub sets positions with its key
        self.c = server.app.test_client()

    def post(self, body, path="/api/devices/aa:bb:cc:dd:ee:01/location"):
        return self.c.post(path, json=body, environ_base=LAN, headers={siteauth.HEADER: KEY})

    def test_set_validate_and_clear(self):
        r = self.post({"lat": "-33.924868", "lon": "18,424055", "note": "pole at gate"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(server.scanner.registry["aa:bb:cc:dd:ee:01"]["geo"]["lon"], 18.424055)
        for bad in ({"lat": 91, "lon": 18}, {"lat": -33, "lon": 181}, {"lat": "x", "lon": 1}, {"lat": 0, "lon": 0}):
            self.assertEqual(self.post(bad).status_code, 400, bad)
        self.assertEqual(self.post({"lat": 1, "lon": 1}, path="/api/devices/nope/location").status_code, 404)
        anon = self.c.post("/api/devices/aa:bb:cc:dd:ee:01/location", json={"lat": 1, "lon": 1}, environ_base=LAN)
        self.assertEqual(anon.status_code, 401)
        self.assertEqual(self.post({"clear": True}).status_code, 200)
        self.assertNotIn("geo", server.scanner.registry["aa:bb:cc:dd:ee:01"])


class CommandsOffByDefault(unittest.TestCase):
    def test_new_config_has_commands_off(self):
        self.assertFalse(config.DEFAULTS["alerts"]["allow_commands"])

    def test_existing_config_is_migrated_off_once(self):
        import json
        cfg = config.load()
        cfg["alerts"]["allow_commands"] = True
        cfg["config_rev"] = 1                      # a site from before the change
        with open(config.CONFIG_PATH, "w") as f:
            json.dump(cfg, f)
        self.assertFalse(config.load()["alerts"]["allow_commands"])
        config.update({"alerts": {"allow_commands": True}})   # operator opts back in
        self.assertTrue(config.load()["alerts"]["allow_commands"])


if __name__ == "__main__":
    unittest.main()
