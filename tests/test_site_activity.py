"""Site: hands-on minutes for the hub's time keeping (app/useractivity.py + routes).

  docker run --rm -v "$PWD/app:/app" -v "$PWD/tests:/tests" -e NETWATCH_DATA=/tmp/nw \
    --entrypoint python farm-netwatch:local /tests/test_site_activity.py -v
"""
import os
import sys
import tempfile
import unittest

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import server        # noqa: E402
import siteauth      # noqa: E402
import useractivity  # noqa: E402

HUB = {"REMOTE_ADDR": "10.8.0.1"}
LAN = {"REMOTE_ADDR": "192.168.0.50"}
KEY = "k" * 43


class SiteActivity(unittest.TestCase):
    def setUp(self):
        for p in (siteauth.AUTH_PATH, useractivity.PATH):
            if os.path.exists(p):
                os.remove(p)
        siteauth._fails.clear()
        useractivity._minutes = None
        self.c = server.app.test_client()
        self.c.post("/api/auth/claim-hub", environ_base=HUB, json={"key": KEY})
        siteauth.set_password("farm-pass-1")

    def beat(self, **kw):
        return self.c.post("/api/activity/beat", environ_base=LAN, **kw).get_json()

    def minutes(self, key=KEY, since=0):
        return self.c.get(f"/api/activity?since={since}", environ_base=HUB,
                          headers={siteauth.HEADER: key})

    def test_only_a_logged_in_direct_session_counts(self):
        self.assertFalse(self.beat()["counted"])                   # not logged in: a farm worker
        self.c.post("/api/auth/login", environ_base=LAN, json={"password": "farm-pass-1"})
        self.assertFalse(self.beat(headers={"X-Netwatch-Kuma-Port": "8205"})["counted"])  # via hub
        self.assertTrue(self.beat()["counted"])
        ms = self.minutes().get_json()["minutes"]
        self.assertEqual(len(ms), 1)
        self.assertEqual(ms[0] % 60, 0)

    def test_minutes_are_for_the_hub_only(self):
        self.assertEqual(self.minutes(key="x" * 43).status_code, 403)
        self.assertEqual(self.c.get("/api/activity", environ_base=LAN).status_code, 403)

    def test_store_dedups_trims_and_survives_restart(self):
        now = 1_800_000_000
        self.assertTrue(useractivity.beat(now - 8 * 86400))
        self.assertTrue(useractivity.beat(now))
        self.assertFalse(useractivity.beat(now + 5))               # same minute
        useractivity._minutes = None                               # a restart
        self.assertEqual(useractivity.since(0), [now // 60 * 60])  # the 8-day-old one is gone
        self.assertEqual(useractivity.since(now + 60), [])


if __name__ == "__main__":
    unittest.main()
