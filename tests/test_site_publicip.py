"""Site app/publicip.py: the site's own public IP for the page header.

  docker run --rm -v "$PWD/app:/app" -v "$PWD/tests:/tests" -e NETWATCH_DATA=/tmp/nw \
      ghcr.io/riaan007/farm-netwatch:latest python -m unittest /tests/test_site_publicip.py
Addresses are documentation ranges — never real sites.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import publicip  # noqa: E402


class SitePublicIp(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = mock.patch.object(publicip, "STATE_PATH", os.path.join(d, "public_ip.json"))
        p.start()
        self.addCleanup(p.stop)
        publicip._state = None

    def test_lookup_reads_trace_and_falls_back(self):
        err = publicip.requests.exceptions.ConnectionError("down")
        good = mock.Mock(status_code=200, text="fl=1\nip=198.51.100.9\nts=2\n")
        with mock.patch.object(publicip.requests, "get", side_effect=[err, good]) as g:
            self.assertEqual(publicip.lookup(), "198.51.100.9")
        self.assertTrue(g.call_args_list[0][0][0].startswith("https://1.1.1.1/"))
        bad = mock.Mock(status_code=200, text="ip=nonsense\n")
        with mock.patch.object(publicip.requests, "get", side_effect=[bad, err]):
            self.assertIsNone(publicip.lookup())

    def test_first_change_and_history(self):
        self.assertIsNone(publicip.current())
        self.assertEqual(publicip.record("203.0.113.7", now=1000), "new")
        c = publicip.current(now=1000)
        self.assertEqual((c["ip"], c["since"], c["first"], c["fresh"], c["prev"]),
                         ("203.0.113.7", None, 1000, True, []))
        self.assertEqual(publicip.record("203.0.113.7", now=1900), "same")
        self.assertEqual(publicip.record("203.0.113.99", now=2800), "changed")
        c = publicip.current(now=2800)
        self.assertEqual(c["since"], 2800)
        self.assertEqual(c["prev"], [{"ip": "203.0.113.7", "from": 1000, "from_known": False, "to": 1900}])
        with open(publicip.STATE_PATH) as f:
            self.assertEqual(json.load(f)["ip"], "203.0.113.99")

    def test_stale_after_missed_lookups_and_survives_restart(self):
        publicip.record("203.0.113.7", now=1000)
        self.assertFalse(publicip.current(now=1000 + publicip.STALE_S + 1)["fresh"])
        publicip._state = None
        self.assertEqual(publicip.current(now=1000)["ip"], "203.0.113.7")

    def test_history_capped_and_bad_file(self):
        with open(publicip.STATE_PATH, "w") as f:
            f.write("{broken")
        for i in range(1, 20):
            publicip.record(f"203.0.113.{i}", now=i * 1000)
        self.assertEqual(len(publicip._load()["prev"]), publicip.PREV_MAX)
        self.assertEqual(len(publicip.current(now=20000)["prev"]), 5)


if __name__ == "__main__":
    unittest.main()
