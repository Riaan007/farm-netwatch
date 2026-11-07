"""Telegram phase 1 on the hub: Ops bot, site bots, pairing, site sync, mirroring.

Against tests/tg_fake.py only (127.0.0.1); site calls are mocked. Run inside
the hub image:
  docker run --rm --network none -e PYTHONDONTWRITEBYTECODE=1 -v "$PWD/hub/app:/app:ro" \
      -v "$PWD/tests:/tests:ro" -e HUB_DATA=/tmp/hub --entrypoint python \
      farm-netwatch-hub:local /tests/test_hub_telegram.py -v
"""
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ["HUB_DATA"] = tempfile.mkdtemp()
sys.path.insert(0, os.environ.get("HUB_APP", "/app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hubconfig  # noqa: E402
import tgapi  # noqa: E402
import tgoutbox  # noqa: E402
import telegram  # noqa: E402
from tg_fake import TOKEN, TOKEN2, FakeTelegram, group_message  # noqa: E402

FAKE = FakeTelegram().start()
tgapi.API = FAKE.url
SITES = [{"id": "farm1", "name": "Farm <One>", "vpn_ip": "10.8.0.99", "netwatch_port": 8090}]


class Base(unittest.TestCase):
    def setUp(self):
        FAKE.calls.clear()
        FAKE.scripted.clear()
        FAKE.updates.clear()
        d = tempfile.mkdtemp()
        for name, val in (("SECRETS_PATH", os.path.join(d, "telegram.json")),
                          ("STATE_PATH", os.path.join(d, "tg_state.json")),
                          ("DATA_DIR", d)):
            p = mock.patch.object(telegram, name, val)
            p.start()
            self.addCleanup(p.stop)
        telegram._secrets = None
        telegram._state = None
        telegram._pairing.clear()
        telegram._last_sync.clear()
        telegram._dead.clear()
        telegram.outbox = tgoutbox.Outbox(os.path.join(d, "ob.json"), telegram._resolve,
                                          on_dead=telegram._on_dead, on_migrate=telegram._on_migrate)
        p = mock.patch.object(hubconfig, "load", lambda: {"sites": SITES, "alerts": {}})
        p.start()
        self.addCleanup(p.stop)

    def drain(self):
        for _ in range(40):
            telegram.outbox._last_sent.clear()
            telegram.outbox.run_once()
            if not telegram.outbox.pending():
                return

    def pair(self, target, chat_id, title="G"):
        with telegram._lock:
            e = telegram._entry(target)
            e.update(chat_id=chat_id, chat_title=title, enabled=True)
            telegram._save_secrets()


class Tokens(Base):
    def test_token_checked_with_telegram_and_never_shown(self):
        ok, err = telegram.set_token("ops", "garbage")
        self.assertFalse(ok)
        FAKE.bots.pop("111111111:AAUnknownTokenForTests_abcdefghijk", None)
        ok, err = telegram.set_token("ops", "111111111:AAUnknownTokenForTests_abcdefghijk")
        self.assertFalse(ok)
        self.assertIn("refused", err)
        ok, bot = telegram.set_token("ops", TOKEN2)
        self.assertTrue(ok)
        self.assertEqual(bot, "ops_test_bot")
        ov = telegram.overview({})
        self.assertNotIn(TOKEN2, json.dumps(ov))
        self.assertEqual(ov["ops"]["bot"], "ops_test_bot")
        self.assertEqual(stat.S_IMODE(os.stat(telegram.SECRETS_PATH).st_mode), 0o600)

    def test_new_token_forgets_the_group(self):
        telegram.set_token("site:farm1", TOKEN)
        self.pair("site:farm1", -5)
        telegram.set_token("site:farm1", TOKEN)            # same bot: group kept
        self.assertTrue(telegram.overview({})["sites"][0]["paired"])
        FAKE.bots["555555555:AANewFarmBotTokenForTest_abcdefgh"] = "x_bot"
        telegram.set_token("site:farm1", "555555555:AANewFarmBotTokenForTest_abcdefgh")
        self.assertFalse(telegram.overview({})["sites"][0]["paired"])
        telegram.set_token("site:farm1", "")
        self.assertFalse(telegram.overview({})["sites"][0]["configured"])


class Pairing(Base):
    def run_pair(self, target):
        code, err = telegram.start_pairing(target)
        self.assertTrue(code, err)
        p = telegram._pairing[target]
        for _ in range(100):
            if p["status"] != "waiting":
                break
            time.sleep(0.05)
        return code, p

    def test_pairs_the_group_that_sends_the_code(self):
        telegram.set_token("site:farm1", TOKEN)
        with mock.patch.object(telegram, "push_to_site") as push:
            code = None
            # the code is random: queue the messages once we know it
            orig = telegram.start_pairing

            def start(t):
                c, e = orig(t)
                FAKE.updates.extend([
                    group_message(1, 77, f"/pair {c}", kind="private"),
                    group_message(2, -300, "/pair 0000-0000"),
                    group_message(3, -300, f"/pair {c}", title="Farm One alerts"),
                ])
                return c, e
            with mock.patch.object(telegram, "start_pairing", start):
                code, p = self.run_pair("site:farm1")
            self.assertEqual(p["status"], "paired", p.get("error"))
            self.assertEqual(push.call_count, 2)            # pause the site's bot, then the new group
            self.assertGreater(push.call_args_list[0][1]["pause_until"], 0)
            self.assertEqual(push.call_args_list[1][0][1]["chat_id"], -300)
        e = telegram._sec()["sites"]["farm1"]
        self.assertEqual(e["chat_id"], -300)
        self.assertEqual(e["chat_title"], "Farm One alerts")
        texts = [m["text"] for m in FAKE.sent()]
        self.assertTrue(any("not in a private chat" in t for t in texts))
        self.assertTrue(any("wrong" in t for t in texts))
        self.assertTrue(any(t.startswith("✅ Paired with <b>Farm &lt;One&gt;</b>") for t in texts))
        # the /pair message was confirmed, so a later poller never sees it
        offs = [p.get("offset") for t, m, p in FAKE.calls if m == "getUpdates"]
        self.assertIn(4, offs)
        del code

    def test_another_poller_stops_pairing(self):
        telegram.set_token("ops", TOKEN2)
        for _ in range(telegram.PAIR_409_TRIES + 1):        # it keeps conflicting: give up
            FAKE.script("getUpdates", {"ok": False, "error_code": 409,
                                       "description": "Conflict: terminated by other getUpdates request"})
        with mock.patch.object(telegram, "PAIR_409_WAIT_S", 0):
            _, p = self.run_pair("ops")
        self.assertEqual(p["status"], "failed")
        self.assertIn("Another program", p["error"])

    def test_no_token_no_pairing(self):
        code, err = telegram.start_pairing("ops")
        self.assertIsNone(code)


class Alerts(Base):
    def setUp(self):
        super().setUp()
        telegram.set_token("ops", TOKEN2)
        self.pair("ops", -1)
        telegram.set_token("site:farm1", TOKEN)
        self.pair("site:farm1", -2)

    def test_hub_alert_goes_to_ops_and_the_site_group(self):
        telegram.alert("critical", "Hub can't reach <this> site", "body & more", site="farm1",
                       ref="offline:farm1")
        self.drain()
        by_chat = {p["chat_id"]: p["text"] for p in FAKE.sent()}
        self.assertEqual(set(by_chat), {-1, -2})
        self.assertTrue(by_chat[-1].startswith("🔴 <b>Farm &lt;One&gt;</b> · Hub can't reach &lt;this&gt; site"))
        self.assertIn("seen by the FarmCamSA hub", by_chat[-2])
        kb = {p["chat_id"]: [b["callback_data"] for r in p["reply_markup"]["inline_keyboard"] for b in r]
              for p in FAKE.sent()}
        self.assertEqual(kb[-1], ["c:site:farm1", "c:visit:farm1", "c:sites", "c:ack"])  # Ops bot answers these
        self.assertEqual(kb[-2], ["c:status", "c:down", "c:ack"])       # the site's own bot
        telegram.alert("info", "Back", "", site="farm1", reply_ref="offline:farm1",
                       resolves=["offline:farm1"])
        self.drain()
        self.assertEqual(len(FAKE.sent("editMessageText")), 2)     # both groups' first message
        backs = FAKE.sent()[-2:]
        self.assertTrue(all(b.get("reply_parameters") for b in backs))
        self.assertTrue(all(b["disable_notification"] for b in backs))

    def test_device_buttons_point_at_that_device(self):
        import poller
        self.assertEqual(poller._device_buttons(["aa:bb:cc:dd:ee:ff"]),
                         [[("📶 Ping", "ping:aa:bb:cc:dd:ee:ff"), ("🧪 Test", "test:aa:bb:cc:dd:ee:ff")],
                          [("🕘 History", "history:aa:bb:cc:dd:ee:ff"),
                           ("👀 Tell me when it changes", "tellme:aa:bb:cc:dd:ee:ff")]])
        self.assertIsNone(poller._device_buttons(["a", "b"]))
        self.assertIsNone(poller._device_buttons(["x" * 60]))
        telegram.alert("warning", "Gate cam is offline", "", site="farm1",
                       site_buttons=poller._device_buttons(["aa:bb"]))
        self.drain()
        site_msg = [p for p in FAKE.sent() if p["chat_id"] == -2][-1]
        rows = site_msg["reply_markup"]["inline_keyboard"]
        self.assertEqual(rows[0][0]["callback_data"], "c:ping:aa:bb")
        self.assertEqual(rows[1][1]["callback_data"], "c:tellme:aa:bb")
        self.assertEqual(rows[-2][0]["callback_data"], "c:status")
        self.assertEqual(rows[-1][0]["callback_data"], "c:ack")              # warning: "I'm on it"

    def test_hub_only_alert_goes_to_ops_only(self):
        telegram.alert("warning", "Hub disk", "85 %")
        self.drain()
        self.assertEqual([p["chat_id"] for p in FAKE.sent()], [-1])

    def test_notify_push_routes_and_keeps_ntfy(self):
        import notify
        with mock.patch.object(notify.requests, "post") as post:
            post.return_value.status_code = 200
            notify.push({"ntfy_topic": "t"}, "Site offline: Farm", "x", priority="high",
                        site="farm1", level="critical", tg_title="Offline")
            post.assert_called_once()
            notify.push({"ntfy_topic": ""}, "ntfy test", "x", telegram_too=False)
        self.drain()
        self.assertEqual(len(FAKE.sent()), 2)
        self.assertTrue(all("· Offline" in p["text"] for p in FAKE.sent()))


class SiteSync(Base):
    def status(self, fp="", chat=None, enabled=True, boot="b1", seq=0):
        return {"telegram": {"supported": True, "token_fp": fp, "chat_id": chat, "enabled": enabled},
                "alerts_log": {"boot": boot, "seq": seq}}

    def test_pushes_only_when_the_site_differs(self):
        telegram.set_token("site:farm1", TOKEN)
        self.pair("site:farm1", -2)
        with mock.patch.object(telegram, "push_to_site", return_value=True) as push:
            telegram.after_status(SITES[0], self.status(tgapi.fingerprint(TOKEN), -2), 5)
            push.assert_not_called()
            telegram.after_status(SITES[0], self.status("", None), 5)
            push.assert_called_once()
            telegram.after_status(SITES[0], self.status("", None), 5)
            push.assert_called_once()                    # rate-limited, not every poll
            telegram.after_status(SITES[0], {"site": {}}, 5)   # old image: left alone
            push.assert_called_once()

    def test_push_body_carries_token_to_the_site_only(self):
        telegram.set_token("site:farm1", TOKEN)
        with mock.patch.object(telegram.requests, "post") as post:
            post.return_value.status_code = 200
            telegram.push_to_site(SITES[0], telegram._sec()["sites"]["farm1"])
        url = post.call_args[0][0]
        self.assertEqual(url, "http://10.8.0.99:8090/api/telegram")
        self.assertEqual(post.call_args[1]["json"]["token"], TOKEN)
        self.assertIn("X-Netwatch-Hub-Key", post.call_args[1]["headers"])

    def test_unconfigured_site_is_not_pushed(self):
        with mock.patch.object(telegram, "push_to_site") as push:
            telegram.after_status(SITES[0], self.status("", None), 5)
        push.assert_not_called()

    def test_mirror_first_look_then_new_alerts(self):
        telegram.set_token("ops", TOKEN2)
        self.pair("ops", -1)
        with mock.patch.object(telegram.requests, "get") as get:
            telegram.after_status(SITES[0], self.status(seq=7), 5)     # first look: no flood
            get.assert_not_called()
            get.return_value.status_code = 200
            get.return_value.json.return_value = {
                "boot": "b1", "seq": 9,
                "alerts": [{"seq": 8, "ts": time.time(), "level": "warning", "title": "Pi hot", "body": "77 °C"},
                           {"seq": 8, "ts": time.time(), "level": "info", "title": "New device", "body": "",
                            "ops": False},
                           {"seq": 9, "ts": time.time() - 3600, "level": "critical", "title": "Hub link DOWN",
                            "body": ""}]}
            telegram.after_status(SITES[0], self.status(seq=9), 5)
            self.assertEqual(get.call_args[1]["params"], {"boot": "b1", "after": 7})
            telegram.after_status(SITES[0], self.status(seq=9), 5)
            self.assertEqual(get.call_count, 1)                         # nothing new
        self.drain()
        texts = [p["text"] for p in FAKE.sent()]
        self.assertEqual(len(texts), 2)
        self.assertTrue(texts[0].startswith("🟠 <b>Farm &lt;One&gt;</b> · Pi hot"))
        self.assertIn("(at ", texts[1])                    # raised an hour ago (VPN was down)

    def test_api_needs_known_site_and_hides_token(self):
        from flask import Flask
        app = Flask(__name__)
        app.register_blueprint(telegram.bp)
        c = app.test_client()
        self.assertEqual(c.post("/api/hub/telegram/sites/nope", json={"token": TOKEN}).status_code, 404)
        r = c.post("/api/hub/telegram/sites/farm1", json={"token": TOKEN})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(TOKEN, r.get_data(as_text=True))
        self.assertEqual(c.post("/api/hub/telegram/ops", json={"token": "bad"}).status_code, 400)
        r = c.post("/api/hub/telegram/sites/farm1/test")
        self.assertEqual(r.status_code, 400)               # not paired yet
        with mock.patch("poller.poller") as pl:
            pl.snapshot.return_value = {}
            self.assertNotIn(TOKEN, c.get("/api/hub/telegram").get_data(as_text=True))


class People(Base):
    def test_add_change_remove_and_push(self):
        ok, err = telegram.set_person("site:farm1", 7, "Riaan", "admin")
        self.assertFalse(ok)                               # no bot yet
        telegram.set_token("site:farm1", TOKEN)
        self.assertFalse(telegram.set_person("site:farm1", "abc", "x", "viewer")[0])
        self.assertFalse(telegram.set_person("site:farm1", 7, "x", "boss")[0])
        self.assertTrue(telegram.set_person("site:farm1", 7, "Riaan", "admin")[0])
        self.pair("site:farm1", -2)
        e = telegram._sec()["sites"]["farm1"]
        fp_ok = {"telegram": {"token_fp": tgapi.fingerprint(TOKEN), "chat_id": -2, "enabled": True,
                              "people_fp": tgapi.obj_fp(e["people"])}, "alerts_log": None}
        with mock.patch.object(telegram, "push_to_site", return_value=True) as push:
            telegram.after_status(SITES[0], fp_ok, 5)
            push.assert_not_called()
            telegram.set_person("site:farm1", 8, "Farmer", "viewer")      # people changed
            telegram.after_status(SITES[0], fp_ok, 5)
            push.assert_called_once()
            self.assertIn("8", push.call_args[0][1]["people"])
        with mock.patch.object(telegram.requests, "post") as post:
            post.return_value.status_code = 200
            telegram.push_to_site(SITES[0], e)
        self.assertEqual(post.call_args[1]["json"]["people"]["7"]["role"], "admin")
        self.assertEqual(post.call_args[1]["json"]["pause_until"], 0)
        telegram.remove_person("site:farm1", 7)
        self.assertNotIn("7", telegram._sec()["sites"]["farm1"]["people"])

    def test_pictures_and_capped_switches_reach_the_site(self):
        self.assertFalse(telegram.set_option("site:farm1", "pictures", True))   # no bot yet
        telegram.set_token("site:farm1", TOKEN)
        self.assertFalse(telegram.set_option("ops", "pictures", True))
        self.assertFalse(telegram.set_option("site:farm1", "reboot", True))
        self.assertTrue(telegram.set_option("site:farm1", "pictures", True))
        e = telegram._sec()["sites"]["farm1"]
        st = {"telegram": {"token_fp": tgapi.fingerprint(TOKEN), "chat_id": None, "enabled": True,
                           "opts": {"pictures": False, "capped": False}}}
        with mock.patch.object(telegram, "push_to_site", return_value=True) as push:
            telegram.after_status(SITES[0], st, 5)
        push.assert_called_once()
        with mock.patch.object(telegram.requests, "post") as post:
            post.return_value.status_code = 200
            telegram.push_to_site(SITES[0], e)
        self.assertTrue(post.call_args[1]["json"]["pictures"])
        self.assertFalse(post.call_args[1]["json"]["actions"])
        self.assertTrue(telegram.set_option("site:farm1", "actions", True))
        with mock.patch.object(telegram, "push_to_site", return_value=True) as push:
            telegram._last_sync.clear()
            telegram.after_status(SITES[0], {"telegram": {"token_fp": tgapi.fingerprint(TOKEN), "chat_id": None,
                                                          "enabled": True, "opts": {"pictures": True, "capped": False,
                                                                                    "actions": False}}}, 5)
        push.assert_called_once()
        self.assertFalse(post.call_args[1]["json"]["capped"])

    def test_api_person_and_listener_requests(self):
        from flask import Flask
        app = Flask(__name__)
        app.register_blueprint(telegram.bp)
        c = app.test_client()
        c.post("/api/hub/telegram/sites/farm1", json={"token": TOKEN})
        r = c.post("/api/hub/telegram/sites/farm1", json={"person": {"id": "12", "name": "<x>", "role": "operator"}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["people"], [{"id": "12", "name": "<x>", "role": "operator"}])
        self.assertEqual(c.post("/api/hub/telegram/sites/farm1",
                                json={"person": {"id": "x", "role": "viewer"}}).status_code, 400)
        r = c.post("/api/hub/telegram/sites/farm1", json={"remove_person": "12"})
        self.assertEqual(r.get_json()["people"], [])
        telegram._listener["farm1"] = {"requests": [{"id": "99", "name": "Stranger", "ts": 1, "text": "/status"}]}
        with telegram._lock:
            pub = telegram._public("site:farm1")
        self.assertEqual(pub["listener"]["requests"][0]["id"], "99")


class PairingWithSitePolling(Base):
    def test_site_is_paused_then_released_on_expiry(self):
        telegram.set_token("site:farm1", TOKEN)
        pushes = []
        with mock.patch.object(telegram, "push_to_site",
                               side_effect=lambda site, e, timeout=None, pause_until=0: pushes.append(pause_until)), \
                mock.patch.object(telegram, "PAIR_S", 0.3):
            code, _ = telegram.start_pairing("site:farm1")
            p = telegram._pairing["site:farm1"]
            for _ in range(100):
                if p["status"] != "waiting":
                    break
                time.sleep(0.05)
        self.assertEqual(p["status"], "expired")
        self.assertGreater(pushes[0], time.time() - 5)     # paused first
        self.assertEqual(pushes[-1], 0)                    # released at the end

    def test_409_while_site_lets_go_is_retried(self):
        telegram.set_token("ops", TOKEN2)
        for _ in range(2):
            FAKE.script("getUpdates", {"ok": False, "error_code": 409, "description": "Conflict"})
        with mock.patch.object(telegram, "PAIR_409_WAIT_S", 0):
            code, _ = telegram.start_pairing("ops")
            FAKE.updates.append(group_message(5, -42, f"/pair {code}", title="Ops"))
            p = telegram._pairing["ops"]
            for _ in range(200):
                if p["status"] != "waiting":
                    break
                time.sleep(0.02)
        self.assertEqual(p["status"], "paired", p.get("error"))


class OpsBot(Base):
    def setUp(self):
        super().setUp()
        import hubbot
        self.hubbot = hubbot
        cards = {"farm1": {"name": "Farm <One>", "reachable": True, "latency_ms": 120.4, "monitored": 5,
                           "watched_down": 1, "conflicts": 0, "devices_total": 9, "devices_online": 8,
                           "last_scan_ts": time.time() - 120}}
        snaps = {"farm1": {"devices": {"devices": [{"name": "Gate cam", "watch": True, "online": False}]},
                           "sysinfo": {"temp_c": 55, "uptime_s": 90000, "disks": [{"used_pct": 40}]}}}
        hubbot.configure(lambda s: cards[s["id"]], lambda sid: snaps.get(sid))
        telegram.set_token("ops", TOKEN2)
        self.pair("ops", -1)
        telegram.set_person("ops", 7, "Riaan", "admin")
        d = tempfile.mkdtemp()
        self.bot = hubbot.OpsBot(telegram._ops_settings, os.path.join(d, "o.json"))

    def ask(self, text, user=7):
        n = len(FAKE.updates) + 100
        u = group_message(n, -1, text, user=user)
        u["message"]["date"] = int(time.time())
        FAKE.updates.append(u)
        self.bot.poll_once(timeout=0)
        return FAKE.sent()[-1]["text"] if FAKE.sent() else ""

    def test_sites_and_site(self):
        t = self.ask("/sites")
        self.assertIn("🟠 <b>Farm &lt;One&gt;</b> — 4/5 monitored up", t)
        kb = FAKE.sent()[-1]["reply_markup"]["inline_keyboard"]
        self.assertEqual(kb[0][0]["callback_data"], "c:site:farm1")
        t = self.ask("/site one")
        self.assertIn("VPN ✅ 120 ms", t)
        self.assertIn("Down: Gate cam", t)
        self.assertIn("55 °C", t)

    def test_commands_menu_set_once_and_strangers_refused(self):
        self.ask("/sites")
        self.ask("/sites")
        self.assertEqual(len(FAKE.sent("setMyCommands")), 1)
        self.assertEqual(FAKE.sent("setMyCommands")[0]["scope"], {"type": "chat", "chat_id": -1})
        t = self.ask("/sites", user=55)
        self.assertIn("not on this group's list", t)

    def test_overview_with_a_running_ops_bot_does_not_freeze(self):
        """17:37 2026-11-07: overview() held the lock and the Ops bot's public()
        read its settings under the same lock → the hub froze (poller too)."""
        import threading
        telegram.ops_bot = self.bot
        self.addCleanup(setattr, telegram, "ops_bot", None)
        out = {}
        t = threading.Thread(target=lambda: out.update(ov=telegram.overview({})), daemon=True)
        t.start()
        t.join(3)
        self.assertFalse(t.is_alive(), "overview() deadlocked")
        self.assertIn("listener", out["ov"]["ops"])

    def test_ops_bot_stands_aside_while_ops_pairs(self):
        telegram._pairing["ops"] = {"status": "waiting", "expires": time.time() + 60, "code": "x"}
        n = len(FAKE.calls)
        self.assertEqual(self.bot.poll_once(timeout=0), 5)
        self.assertEqual(len(FAKE.calls), n)


if __name__ == "__main__":
    unittest.main()
