"""The Ops bot against the hub's REAL site card (hub/app/server.py), not a fake:
2026-11-07 17:44 /site and /sites answered "RuntimeError" because _site_card
reads request.host, and the bot runs outside any web request.

  docker run --rm --network none -e PYTHONDONTWRITEBYTECODE=1 -v "$PWD/hub/app:/app:ro" \
      -v "$PWD/tests:/tests:ro" -e HUB_DATA=/tmp/hub --entrypoint python \
      farm-netwatch-hub:local /tests/test_hub_opsbot_real.py -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ["HUB_DATA"] = tempfile.mkdtemp()
sys.path.insert(0, os.environ.get("HUB_APP", "/app"))

import hubconfig  # noqa: E402
import server  # noqa: E402
import hubbot  # noqa: E402

SITES = [{"id": "farm1", "name": "Farm One", "vpn_ip": "10.8.0.99", "netwatch_port": 8090, "enabled": True}]


class RealCard(unittest.TestCase):
    def test_card_outside_a_request_and_the_commands(self):
        cfg = hubconfig.load()
        cfg["sites"] = SITES
        with mock.patch.object(hubconfig, "load", lambda: cfg):
            card = server._site_card_bg(SITES[0])
            self.assertEqual(card["id"], "farm1")
            with self.assertRaises(RuntimeError):
                server._site_card(SITES[0])          # the bug this guards against
            hubbot.configure(server._site_card_bg, server.poller.snapshot)
            text, rows = hubbot.c_sites(None, [], "Riaan")
            self.assertIn("Farm One", text)
            self.assertEqual(rows[0][0][1], "site:farm1")
            text, _ = hubbot.c_site(None, ["farm1"], "Riaan")
            self.assertIn("Farm One", text)
            text, _ = hubbot.c_vpn(None, [], "Riaan")
            self.assertIn("Farm One", text)


if __name__ == "__main__":
    unittest.main()
