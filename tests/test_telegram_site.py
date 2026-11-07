"""Telegram phase 1 on the site: tgapi + tgoutbox + tgsite + notify routing.

Against tests/tg_fake.py only (127.0.0.1). Run inside the site image:
  docker run --rm --network none -e PYTHONDONTWRITEBYTECODE=1 -v "$PWD/app:/app" \
      -v "$PWD/tests:/tests" -e NETWATCH_DATA=/tmp/nw --entrypoint python \
      farm-netwatch:netcfg /tests/test_telegram_site.py -v
"""
import json
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
os.makedirs(os.environ["NETWATCH_DATA"], exist_ok=True)
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tgapi  # noqa: E402
import tgoutbox  # noqa: E402
from tg_fake import TOKEN, FakeTelegram  # noqa: E402

FAKE = FakeTelegram().start()
tgapi.API = FAKE.url


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Base(unittest.TestCase):
    def setUp(self):
        FAKE.calls.clear()
        FAKE.scripted.clear()
        self.dir = tempfile.mkdtemp()
        self.clock = Clock()
        self.dead, self.moved = [], []
        self.chat = {"site": -100}
        self.ob = self.make()

    def make(self):
        return tgoutbox.Outbox(os.path.join(self.dir, "ob.json"),
                               lambda d: (TOKEN, self.chat[d]) if d in self.chat else None,
                               on_dead=lambda d, w: self.dead.append((d, w)),
                               on_migrate=lambda d, c: (self.moved.append(c), self.chat.update({d: c})),
                               clock=self.clock, tz_fmt=lambda ts: "HH:MM")

    def drain(self, step=4):
        for _ in range(50):
            self.ob.run_once()
            if not self.ob.pending():
                return
            self.clock.t += step


class Api(Base):
    def test_ok_false_is_a_failure_and_token_never_leaks(self):
        FAKE.script("sendMessage", {"ok": False, "error_code": 400,
                                    "description": f"Bad Request: bot{TOKEN} chat not found"})
        r = tgapi.send(TOKEN, 1, "x")
        self.assertFalse(r.ok)
        self.assertNotIn(TOKEN, r.error)
        bad = tgapi.call("not-a-token", "getMe")
        self.assertFalse(bad.ok)

    def test_connection_error_text_has_no_token(self):
        with mock.patch.object(tgapi, "API", "http://127.0.0.1:1"):
            r = tgapi.send(TOKEN, 1, "x")
        self.assertFalse(r.ok)
        self.assertNotIn(TOKEN, r.error)
        self.assertNotIn(TOKEN.split(":")[1], r.error)

    def test_escape(self):
        self.assertEqual(tgapi.esc('<a href="x">&'), '&lt;a href="x"&gt;&amp;')
        self.assertTrue(tgapi.valid_token(TOKEN))
        self.assertFalse(tgapi.valid_token("123:short"))
        self.assertEqual(len(tgapi.fingerprint(TOKEN)), 12)
        self.assertEqual(tgapi.fingerprint(""), "")


class Outbox(Base):
    def test_sends_and_file_is_private(self):
        self.ob.put("site", "<b>hello</b>", title="hello")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.dir, "ob.json")).st_mode), 0o600)
        self.drain()
        sent = FAKE.sent()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["chat_id"], -100)
        self.assertEqual(sent[0]["parse_mode"], "HTML")
        self.assertEqual(self.ob.pending(), 0)

    def test_429_waits_retry_after(self):
        FAKE.script("sendMessage", {"ok": False, "error_code": 429, "description": "Too Many Requests",
                                    "parameters": {"retry_after": 30}})
        self.ob.put("site", "x")
        self.ob.run_once()
        self.assertEqual(self.ob.pending(), 1)
        self.clock.t += 10
        self.ob.run_once()
        self.assertEqual(len(FAKE.sent()), 1)          # not retried before retry_after
        self.clock.t += 25
        self.ob.run_once()
        self.assertEqual(len(FAKE.sent()), 2)
        self.assertEqual(self.ob.pending(), 0)

    def test_network_failure_keeps_it_and_retries(self):
        with mock.patch.object(tgapi, "API", "http://127.0.0.1:1"):
            self.ob.put("site", "x")
            self.ob.run_once()
        self.assertEqual(self.ob.pending(), 1)
        self.clock.t += 11
        self.ob.run_once()
        self.assertEqual(self.ob.pending(), 0)

    def test_kicked_bot_drops_and_reports(self):
        FAKE.script("sendMessage", {"ok": False, "error_code": 403,
                                    "description": "Forbidden: bot was kicked from the group chat"})
        self.ob.put("site", "a")
        self.ob.put("site", "b")
        self.ob.run_once()
        self.assertEqual(self.ob.pending(), 0)
        self.assertEqual(self.dead[0][0], "site")

    def test_supergroup_migration_follows_the_group(self):
        FAKE.script("sendMessage", {"ok": False, "error_code": 400,
                                    "description": "Bad Request: group chat was upgraded to a supergroup chat",
                                    "parameters": {"migrate_to_chat_id": -1009}})
        self.ob.put("site", "x")
        self.drain()
        self.assertEqual(self.moved, [-1009])
        self.assertEqual(FAKE.sent()[-1]["chat_id"], -1009)

    def test_bad_html_is_resent_as_plain_text(self):
        FAKE.script("sendMessage", {"ok": False, "error_code": 400,
                                    "description": "Bad Request: can't parse entities"})
        self.ob.put("site", "<b>x &amp; y</b>")
        self.drain()
        last = FAKE.sent()[-1]
        self.assertNotIn("parse_mode", last)
        self.assertEqual(last["text"], "x & y")

    def test_old_messages_fold_into_one(self):
        with mock.patch.object(tgapi, "API", "http://127.0.0.1:1"):
            for i in range(5):
                self.ob.put("site", f"alert {i}", title=f"Alert {i}")
                self.clock.t += 60
            self.ob.run_once()
        self.clock.t += tgoutbox.FOLD_AFTER_S + 10
        self.drain()
        sent = FAKE.sent()
        self.assertEqual(len(sent), 1)
        self.assertIn("Held back", sent[0]["text"])
        for i in range(5):
            self.assertIn(f"Alert {i}", sent[0]["text"])

    def test_reply_and_resolve(self):
        self.ob.put("site", "down", ref="inc", buttons=[[("Status", "status")]])
        self.drain()
        mid = FAKE.sent()[-1]
        self.ob.put("site", "back", reply_ref="inc", resolves=["inc"])
        self.drain()
        back = FAKE.sent()[-1]
        self.assertTrue(back["reply_parameters"]["message_id"] > 0)
        edits = FAKE.sent("editMessageText")
        self.assertEqual(len(edits), 1)
        self.assertTrue(edits[0]["text"].startswith("✅ <i>Resolved"))
        self.assertTrue(edits[0]["text"].endswith("down"))
        self.assertEqual(edits[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "c:status")
        del mid

    def test_unconfigured_destination_is_dropped(self):
        self.ob.put("nowhere", "x")
        self.ob.run_once()
        self.assertEqual(self.ob.pending(), 0)
        self.assertEqual(FAKE.sent(), [])

    def test_queue_survives_a_restart(self):
        with mock.patch.object(tgapi, "API", "http://127.0.0.1:1"):
            self.ob.put("site", "kept")
            self.ob.run_once()
        again = self.make()
        self.assertEqual(again.pending(), 1)

    def test_per_chat_rate(self):
        for i in range(3):
            self.ob.put("site", str(i))
        self.ob.run_once()
        self.ob.run_once()
        self.assertEqual(len(FAKE.sent()), 1)        # the 2nd waits for the gap
        self.clock.t += tgoutbox.PER_CHAT_GAP_S + 0.01
        self.ob.run_once()
        self.assertEqual(len(FAKE.sent()), 2)


class Site(Base):
    def setUp(self):
        super().setUp()
        import tgsite
        self.tgsite = tgsite
        d = tempfile.mkdtemp()
        for name, val in (("STATE_PATH", os.path.join(d, "telegram.json")),):
            p = mock.patch.object(tgsite, name, val)
            p.start()
            self.addCleanup(p.stop)
        tgsite._state = None
        tgsite.outbox = tgoutbox.Outbox(os.path.join(d, "ob.json"), tgsite._resolve,
                                        on_dead=tgsite._on_dead, on_migrate=tgsite._on_migrate,
                                        on_sent=tgsite._on_sent, clock=self.clock)
        self.ob = tgsite.outbox
        p = mock.patch.object(tgsite, "_site_name", lambda: "Test <Farm>")
        p.start()
        self.addCleanup(p.stop)

    def test_apply_validates_and_status_has_no_token(self):
        ok, err = self.tgsite.apply({"token": "nope"})
        self.assertFalse(ok)
        ok, _ = self.tgsite.apply({"token": TOKEN, "chat_id": -42, "chat_title": "G"})
        self.assertTrue(ok)
        st = self.tgsite.status()
        self.assertNotIn(TOKEN, json.dumps(st))
        self.assertEqual(st["token_fp"], tgapi.fingerprint(TOKEN))
        self.assertEqual(st["chat_id"], -42)
        self.assertEqual(stat.S_IMODE(os.stat(self.tgsite.STATE_PATH).st_mode), 0o600)
        ok, err = self.tgsite.apply({"chat_id": "x"})
        self.assertFalse(ok)
        # a new token is a new bot: the old group no longer applies
        self.tgsite.apply({"token": TOKEN.replace("123456789", "123456780")})
        self.assertIsNone(self.tgsite.status()["chat_id"])

    def test_alert_reaches_the_group_escaped(self):
        self.tgsite.apply({"token": TOKEN, "chat_id": -42})
        self.tgsite.alert("warning", "Wi-Fi: <script>", "Link <b>weak</b>")
        self.drain()
        text = FAKE.sent()[-1]["text"]
        self.assertTrue(text.startswith("🟠 <b>Test &lt;Farm&gt;</b> · Wi-Fi: &lt;script&gt;"))
        self.assertIn("Link &lt;b&gt;weak&lt;/b&gt;", text)

    def test_alert_log_for_the_hub_even_without_telegram(self):
        mark = self.tgsite.alerts_marker()
        self.tgsite.alert("critical", "One", "a")
        self.tgsite.alert("info", "Two", "b")
        r = self.tgsite.recent(mark["boot"], mark["seq"])
        self.assertEqual([a["title"] for a in r["alerts"]], ["One", "Two"])
        self.assertEqual(self.ob.pending(), 0)            # no group: nothing queued
        r2 = self.tgsite.recent("other-boot", 10**6)      # hub saw an older process
        self.assertGreaterEqual(len(r2["alerts"]), 2)

    def test_notify_push_routes_to_telegram_without_ntfy(self):
        import notify
        self.tgsite.apply({"token": TOKEN, "chat_id": -42})
        with mock.patch.object(notify.requests, "post") as post:
            notify.push({"ntfy_topic": ""}, "Pi health", "hot", priority="high")
            notify.push({"ntfy_topic": ""}, "ntfy only", "x", telegram=False)
            post.assert_not_called()
        self.drain()
        texts = [p["text"] for p in FAKE.sent()]
        self.assertEqual(len(texts), 1)
        self.assertTrue(texts[0].startswith("🟠"))
        self.assertEqual(self.tgsite.level_for("urgent"), "critical")
        self.assertEqual(self.tgsite.level_for("default"), "info")

    def test_info_is_silent(self):
        self.tgsite.apply({"token": TOKEN, "chat_id": -42})
        self.tgsite.alert("info", "back", "")
        self.tgsite.alert("critical", "down", "")
        self.drain()
        a, b = FAKE.sent()[-2:]
        self.assertTrue(a["disable_notification"])
        self.assertFalse(b["disable_notification"])

    def test_alerts_carry_matching_buttons(self):
        self.tgsite.apply({"token": TOKEN, "chat_id": -42})
        self.tgsite.alert("warning", "🔌 Hub link DOWN", "x")
        self.tgsite.alert("warning", "Pi health warning", "hot")
        self.tgsite.alert("info", "New device on network", "x")
        self.drain()
        kbs = [[b["callback_data"] for row in p["reply_markup"]["inline_keyboard"] for b in row]
               for p in FAKE.sent()]
        self.assertEqual(kbs[0], ["c:hub", "c:internet", "c:status", "c:down", "c:ack"])
        self.assertEqual(kbs[1], ["c:pi", "c:status", "c:down", "c:ack"])
        self.assertEqual(kbs[2], ["c:status", "c:down"])

    def test_maintenance_holds_alerts_then_summarises(self):
        import time as _t
        self.tgsite.apply({"token": TOKEN, "chat_id": -42})
        self.tgsite.set_maint(_t.time() + 3600, "Riaan")
        self.tgsite.alert("warning", "Wi-Fi: Tower 2", "weak")
        self.tgsite.alert("critical", "Cameras: NVR", "not recording")
        self.drain()
        self.assertEqual(FAKE.sent(), [])                       # held
        self.assertEqual(len(self.tgsite.recent("x", 0)["alerts"]) >= 2, True)   # hub still sees them
        self.tgsite.set_maint(0, "Riaan")
        self.drain()
        t = FAKE.sent()[-1]["text"]
        self.assertIn("maintenance over", t)
        self.assertIn("2 alert(s) were held", t)
        self.assertIn("Cameras: NVR", t)
        self.tgsite.set_maint(_t.time() - 1, "Riaan")           # a window that already ran out
        self.tgsite.alert("info", "Pi health recovered", "")
        self.drain()
        self.assertIn("Pi health recovered", FAKE.sent()[-1]["text"])

    def test_actions_switch_reported(self):
        self.tgsite.apply({"token": TOKEN, "actions": True})
        self.assertTrue(self.tgsite.status()["opts"]["actions"])

    def test_http_routes_need_the_login(self):
        from flask import Flask
        import siteauth
        app = Flask(__name__)
        siteauth.init_app(app)
        app.register_blueprint(self.tgsite.bp)
        c = app.test_client()
        self.assertEqual(c.get("/api/telegram").status_code, 401)
        self.assertEqual(c.post("/api/telegram", json={"token": TOKEN}).status_code, 401)
        self.assertEqual(c.get("/api/alerts/recent").status_code, 401)
        with mock.patch.object(siteauth, "hub_ok", return_value=True):
            r = c.post("/api/telegram", json={"token": TOKEN, "chat_id": -7})
            self.assertEqual(r.status_code, 200)
            self.assertNotIn(TOKEN, r.get_data(as_text=True))
            self.assertNotIn(TOKEN, c.get("/api/telegram").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
