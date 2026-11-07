"""Telegram phase 6 on the hub: the visit brief, the stand-in bot for a DARK
site, Ops /run, the 07:00 Ops report. Fake Telegram (tests/tg_fake.py), no network.

  docker run --rm --network none -e PYTHONDONTWRITEBYTECODE=1 -v "$PWD/hub/app:/app:ro" \
      -v "$PWD/tests:/tests:ro" -e HUB_DATA=/tmp/hub --entrypoint python \
      farm-netwatch-hub:local /tests/test_hub_phase6.py -v
Addresses are documentation ranges.
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ["HUB_DATA"] = tempfile.mkdtemp()
sys.path.insert(0, os.environ.get("HUB_APP", "/app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import heartbeats  # noqa: E402
import hubbot  # noqa: E402
import hubconfig  # noqa: E402
import telegram  # noqa: E402
import tgapi  # noqa: E402
import tgoutbox  # noqa: E402
import visit  # noqa: E402
from tg_fake import TOKEN, TOKEN2, FakeTelegram, group_message  # noqa: E402

FAKE = FakeTelegram().start()
tgapi.API = FAKE.url
SITE = {"id": "farm1", "name": "Farm <One>", "vpn_ip": "10.8.0.99", "netwatch_port": 8090, "enabled": True}
SNAP = {"reachable": False, "status_fetched": time.time() - 3600,
        "status": {"site": {"name": "Farm <One>", "location": "Near the dam", "lat": -28.1, "lon": 25.4}},
        "devices": {"devices": [{"name": "Gate cam", "ip": "192.0.2.5", "watch": True, "online": False,
                                 "category": "camera", "geo": {"lat": -28.2, "lon": 25.5}, "model": "DS-2CD"}]},
        "sysinfo": {"model": "Raspberry Pi 3 Model B Plus Rev 1.3", "temp_c": 71,
                    "throttled": {"undervoltage_ever": True}}}


class Base(unittest.TestCase):
    def setUp(self):
        FAKE.calls.clear()
        FAKE.scripted.clear()
        FAKE.updates.clear()
        d = tempfile.mkdtemp()
        self.d = d
        for name, val in (("SECRETS_PATH", os.path.join(d, "telegram.json")),
                          ("STATE_PATH", os.path.join(d, "tg_state.json")), ("DATA_DIR", d)):
            p = mock.patch.object(telegram, name, val)
            p.start()
            self.addCleanup(p.stop)
        telegram._secrets = None
        telegram._state = None
        telegram._standins.clear()
        heartbeats._last.clear()
        heartbeats._down.clear()
        telegram.outbox = tgoutbox.Outbox(os.path.join(d, "ob.json"), telegram._resolve)
        p = mock.patch.object(hubconfig, "load", lambda: {"sites": [SITE], "alerts": {}})
        p.start()
        self.addCleanup(p.stop)
        hubbot.configure(lambda s: {"name": "Farm <One>", "reachable": False, "error": "ConnectTimeout"},
                         lambda sid: SNAP)
        telegram.set_token("site:farm1", TOKEN)
        with telegram._lock:
            e = telegram._entry("site:farm1")
            e.update(chat_id=-2, people={"7": {"name": "Riaan", "role": "admin"}})


class Visit(Base):
    def test_brief_has_what_where_and_what_to_bring(self):
        with mock.patch.object(visit.backups, "list_", return_value=[{"ts": time.time() - 7200}]):
            t = visit.brief(SITE, SNAP)
        self.assertIn("Farm &lt;One&gt;", t)
        self.assertIn("Near the dam", t)
        self.assertIn("maps.google.com/?q=-28.200000,25.500000", t)
        self.assertIn("Gate cam", t)
        self.assertIn("micro-USB supply", t)             # the Pi 3's spares
        self.assertIn("PoE injector", t)                   # a camera is down
        self.assertIn("undervoltage", t)
        self.assertIn("2 h", t)
        self.assertIn("No heartbeat", t)


class StandIn(Base):
    def test_dark_site_gets_a_stand_in_that_lets_go(self):
        now = time.time()
        heartbeats._down["farm1"] = {"since": now - 1000, "diag": True}
        with mock.patch("hubbot.StandInBot.start") as start:
            telegram._standin_check(SITE, False, now)
            start.assert_called_once()
        bot = telegram._standins["farm1"]
        u = group_message(1, -2, "/status", user=7)
        u["message"]["date"] = int(time.time())
        FAKE.updates.append(u)
        bot.poll_once(timeout=0)
        t = FAKE.sent()[-1]["text"]
        self.assertTrue(t.startswith("🛰 <i>Answered by the FarmCamSA hub — Farm &lt;One&gt; itself is not reachable"))
        self.assertIn("VPN ❌", t)
        telegram._standin_check(SITE, True, now)          # the hub reaches it again
        self.assertNotIn("farm1", telegram._standins)
        self.assertFalse(bot.active)

    def test_alive_site_gets_no_stand_in(self):
        now = time.time()
        heartbeats._down["farm1"] = {"since": now - 1000, "diag": True}
        heartbeats._last["farm1"] = {"p": {"t": now - 60, "st": "tunnel_down_internet_up"}, "rx": now}
        telegram._standin_check(SITE, False, now)
        self.assertNotIn("farm1", telegram._standins)

    def test_409_means_the_site_is_back(self):
        bot = hubbot.StandInBot(SITE, lambda: {"token": TOKEN, "chat_id": -2, "people": {}},
                                os.path.join(self.d, "s.json"))
        FAKE.script("getUpdates", {"ok": False, "error_code": 409, "description": "Conflict"})
        bot.loop()                                          # returns by itself
        self.assertFalse(bot.active)


class Run(Base):
    def test_run_posts_to_the_site_and_reports(self):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"ok": True, "text": "🌐 <b>Internet</b>\nRouter ✅"}
        with mock.patch("requests.post", return_value=resp) as post:
            job = hubbot.c_run(None, ["farm1", "internet"], "Riaan")
            text, _ = job.work()
        self.assertEqual(post.call_args[0][0], "http://10.8.0.99:8090/api/telegram/run")
        self.assertEqual(post.call_args[1]["json"], {"cmd": "internet", "args": [], "who": "Riaan"})
        self.assertIn("X-Netwatch-Hub-Key", post.call_args[1]["headers"])
        self.assertIn("Farm &lt;One&gt;</b> · /internet", text)
        self.assertEqual(hubbot.COMMANDS["run"][1], "operator")

    def test_run_unreachable_site(self):
        import requests
        with mock.patch("requests.post", side_effect=requests.ConnectionError("x")):
            text, _ = hubbot.c_run(None, ["farm1", "status"], "Riaan").work()
        self.assertIn("not reachable over the VPN", text)


class Report(Base):
    def test_ops_report_once_a_day_and_not_on_first_start(self):
        store = {}
        bot = hubbot.OpsBot(lambda: {"token": TOKEN2, "chat_id": -1, "people": {}}, os.path.join(self.d, "o.json"))
        bot.report_state = (lambda: store.get("d"), lambda d: store.__setitem__("d", d))
        with mock.patch.object(hubbot.backups, "list_", return_value=[]), \
                mock.patch.object(hubbot.time, "localtime", return_value=time.struct_time((2026, 11, 8, 7, 5, 0, 6, 312, 0))):
            bot.maybe_report({"chat_id": -1})
            self.assertEqual(FAKE.sent(), [])             # first start: only notes the date
            store["d"] = "2026-11-07"
            bot.maybe_report({"chat_id": -1})
            bot.maybe_report({"chat_id": -1})
        sent = FAKE.sent()
        self.assertEqual(len(sent), 1)
        self.assertIn("Daily report", sent[0]["text"])
        self.assertIn("Config backup older than a day", sent[0]["text"])


if __name__ == "__main__":
    unittest.main()
