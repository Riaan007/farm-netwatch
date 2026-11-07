"""Telegram phase 2 on the site: the command engine (tgbotcore) + the site's
read-only commands (tgbot), against tests/tg_fake.py and a fake site API.

Run inside the site image:
  docker run --rm --network none -e PYTHONDONTWRITEBYTECODE=1 -v "$PWD/app:/app" \
      -v "$PWD/tests:/tests" -e NETWATCH_DATA=/tmp/nw --entrypoint python \
      farm-netwatch:netcfg /tests/test_telegram_bot.py -v
Addresses are documentation ranges.
"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
os.makedirs(os.environ["NETWATCH_DATA"], exist_ok=True)
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tgapi  # noqa: E402
import tgbot  # noqa: E402
import tgbotcore  # noqa: E402
from tg_fake import TOKEN, FakeTelegram, group_message  # noqa: E402

FAKE = FakeTelegram().start()
tgapi.API = FAKE.url
CHAT = -500
NOW = time.time()

API = {
    "/api/status": {"site": {"name": "Test <Farm>"}, "last_scan": "10:00"},
    "/api/internet": {"has_gateway": True, "gateway": True, "dns": True, "external": True, "ok": True,
                      "checked_ts": NOW - 30, "public_ip": {"ip": "198.51.100.7", "since": NOW - 3600}},
    "/api/hub/status": {"configured": True, "connected": False, "hub_reachable": False,
                        "last_handshake_s": 400, "endpoint": "203.0.113.5:51820", "address": "10.8.0.99/24"},
    "/api/sysinfo": {"temp_c": 61.2, "uptime_s": 7200, "mem": {"used_pct": 40},
                     "throttled": {"undervoltage_now": True},
                     "disks": [{"mount": "data", "used_pct": 50, "free_gb": 12}], "load": [0.5, 0.4, 0.3]},
    "/api/devices": {"devices": [
        {"key": "aa:01", "ip": "192.0.2.10", "name": "Tower radio", "watch": True, "online": False,
         "last_seen": NOW - 900},
        {"key": "aa:02", "ip": "192.0.2.11", "name": "Gate camera", "watch": True, "online": False,
         "last_seen": NOW - 900, "link": "aa:01"},
        {"key": "aa:03", "ip": "192.0.2.12", "name": "Gate <b>cam</b> 2", "watch": True, "online": True},
        {"key": "aa:04", "ip": "192.0.2.13", "name": "Office printer", "online": True}]},
    "/api/cameras": {"enabled": True, "cameras": [
        {"name": "Gate camera", "online": False, "recording": False},
        {"name": "Yard", "online": True, "recording": True}],
        "recorders": [{"name": "NVR", "online": True, "footage_min_days": 14, "channels_online": 7,
                       "channels_offline": 1, "disks": [{"status": "ok"}]}]},
    "/api/radio/overview": {"radios": {"a": {}, "b": {}},
                            "problems": [{"device": "Tower radio", "level": "crit", "what": "signal -84 dBm"}]},
}


class Base(unittest.TestCase):
    def setUp(self):
        FAKE.calls.clear()
        FAKE.scripted.clear()
        FAKE.updates.clear()
        self.st = {"token": TOKEN, "chat_id": CHAT, "bot": "farm_test_bot", "enabled": True,
                   "people": {"7": {"name": "Riaan", "role": "admin"},
                              "8": {"name": "Farmer", "role": "viewer"}}}
        self.t = [NOW]
        d = tempfile.mkdtemp()
        self.bot = tgbot.SiteBot(lambda: self.st, self.api, clock=lambda: self.t[0],
                                 offset_path=os.path.join(d, "off.json"))

    def api(self, path):
        if path.startswith("/api/events"):
            return {"events": [{"ts": NOW - 60, "type": "offline"}, {"ts": NOW - 4000, "type": "online"}]}
        return API.get(path)

    def say(self, text, user=7, chat=CHAT, uid=None, date=None):
        uid = uid or (len(FAKE.updates) + 1 + (self.bot._offset or 0))
        u = group_message(uid, chat, text, user=user)
        u["message"]["date"] = int(date if date is not None else self.t[0])
        FAKE.updates.append(u)
        self.bot.poll_once(timeout=0)
        return [p["text"] for p in FAKE.sent()]


class Engine(Base):
    def test_listed_person_gets_status(self):
        out = self.say("/status")
        self.assertEqual(len(out), 1)
        t = out[0]
        self.assertIn("Test &lt;Farm&gt;", t)
        self.assertIn("Hub link ❌", t)
        self.assertIn("UNDERVOLTAGE", t)
        self.assertIn("Monitored: 1/3 online", t)
        last = FAKE.sent()[-1]
        self.assertEqual(last["reply_parameters"]["message_id"], 1)
        self.assertTrue(last["reply_markup"]["inline_keyboard"])

    def test_stranger_is_told_once_and_becomes_a_request(self):
        out = self.say("/status", user=99)
        self.assertIn("not on this group's list", out[-1])
        self.say("/internet", user=99)
        self.assertEqual(len(FAKE.sent()), 1)                # told once a day only
        req = self.bot.public()["requests"]
        self.assertEqual(req[0]["id"], "99")
        self.st["people"]["99"] = {"name": "x", "role": "viewer"}
        self.assertEqual(self.bot.public()["requests"], [])  # listed now: no longer asking

    def test_whoami_works_for_anyone(self):
        out = self.say("/whoami", user=99)
        self.assertIn("<code>99</code>", out[-1])
        self.assertIn("role: <b>none</b>", out[-1])

    def test_other_chats_and_bots_are_ignored(self):
        self.say("/status", chat=-777)
        self.say("/status@some_other_bot")
        self.say("hello there")
        self.assertEqual(FAKE.sent(), [])
        self.say("/status@farm_test_bot")
        self.assertEqual(len(FAKE.sent()), 1)

    def test_stale_command_is_not_run(self):
        out = self.say("/status", date=NOW - 3600)
        self.assertIn("too old", out[-1])
        self.assertNotIn("Monitored", out[-1])

    def test_rate_limit(self):
        for _ in range(tgbotcore.RATE_N + 3):
            self.say("/pi")
        self.assertEqual(len(FAKE.sent()), tgbotcore.RATE_N)

    def test_offset_saved_and_reused(self):
        self.say("/pi")
        again = tgbot.SiteBot(lambda: self.st, self.api, offset_path=self.bot.offset_path)
        self.assertEqual(again._offset, self.bot._offset)
        gu = [p for t, m, p in FAKE.calls if m == "getUpdates"]
        self.assertTrue(gu)

    def test_409_backs_off_and_pause_is_honoured(self):
        FAKE.script("getUpdates", {"ok": False, "error_code": 409, "description": "Conflict"})
        self.assertEqual(self.bot.poll_once(timeout=0), 5)
        n = len(FAKE.calls)
        self.bot.poll_once(timeout=0)
        self.assertEqual(len(FAKE.calls), n)                 # still backing off
        self.t[0] += 61
        self.st["pause_until"] = self.t[0] + 100
        self.bot.poll_once(timeout=0)
        self.assertEqual(len(FAKE.calls), n)                 # paused for the hub

    def test_bot_added_to_another_group_leaves(self):
        FAKE.updates.append({"update_id": 50, "my_chat_member": {
            "chat": {"id": -9}, "new_chat_member": {"status": "member"}}})
        self.bot.poll_once(timeout=0)
        self.assertEqual([p["chat_id"] for p in FAKE.sent("leaveChat")], [-9])

    def test_button_press_runs_the_command(self):
        FAKE.updates.append({"update_id": 60, "callback_query": {
            "id": "q1", "from": {"id": 8, "first_name": "Farmer"}, "data": "c:internet",
            "message": {"message_id": 33, "chat": {"id": CHAT}}}})
        self.bot.poll_once(timeout=0)
        self.assertEqual(len(FAKE.sent("answerCallbackQuery")), 1)
        self.assertIn("Internet", FAKE.sent()[-1]["text"])

    def test_tellme_button_with_a_device_key(self):
        FAKE.updates.append({"update_id": 61, "callback_query": {
            "id": "q2", "from": {"id": 8, "first_name": "Farmer"}, "data": "c:tellme:aa:02",
            "message": {"message_id": 34, "chat": {"id": CHAT}}}})
        self.bot.poll_once(timeout=0)
        self.assertIn("Gate camera", FAKE.sent()[-1]["text"])
        self.assertEqual(self.bot.tellme[0]["key"], "aa:02")

    def test_min_role(self):
        self.bot.commands = dict(self.bot.commands, secret=(lambda b, a, w: ("ok", None), "admin"))
        out = self.say("/secret", user=8)
        self.assertIn("needs the <b>admin</b> role", out[-1])
        out = self.say("/secret", user=7)
        self.assertEqual(out[-1], "ok")

    def test_broken_command_answers_politely(self):
        with mock.patch.dict(tgbot.COMMANDS, {"pi": (lambda b, a, w: 1 / 0, "viewer")}):
            self.bot.commands = tgbot.COMMANDS
            out = self.say("/pi")
        self.assertIn("failed", out[-1])
        self.assertFalse(self.bot.audit[-1]["ok"])


class Commands(Base):
    def test_down_groups_behind_the_radio(self):
        t = self.say("/down")[-1]
        self.assertIn("2 of 3 monitored devices down", t)
        self.assertIn("Tower radio", t)
        self.assertIn("↳ and 1 behind it: Gate camera", t)

    def test_names_are_escaped(self):
        self.st["people"]["7"]["role"] = "admin"
        t = self.say("/history gate <b>cam</b> 2")[-1]
        self.assertIn("Gate &lt;b&gt;cam&lt;/b&gt; 2", t)
        self.assertNotIn("<b>cam</b>", t)

    def test_history_asks_which_when_ambiguous(self):
        self.say("/history gate")
        kb = FAKE.sent()[-1]["reply_markup"]["inline_keyboard"]
        self.assertEqual(len(kb), 2)
        self.assertTrue(kb[0][0]["callback_data"].startswith("c:history:"))

    def test_tellme_reports_the_change(self):
        out = self.say("/tellme gate camera")
        self.assertIn("comes back online", out[-1])
        API["/api/devices"]["devices"][1]["online"] = True
        try:
            self.bot.poll_once(timeout=0)
        finally:
            API["/api/devices"]["devices"][1]["online"] = False
        self.assertIn("back ONLINE", FAKE.sent()[-1]["text"])
        self.assertEqual(self.bot.tellme, [])

    def test_cameras_nvr_links_hub_pi(self):
        self.assertIn("1/2 online", self.say("/cameras")[-1])
        nvr = self.say("/nvr")[-1]
        self.assertIn("14 days of footage", nvr)
        self.assertIn("7/8 channels", nvr)
        self.assertIn("signal -84 dBm", self.say("/links")[-1])
        hub = self.say("/hub")[-1]
        self.assertIn("203.0.113.5:51820", hub)
        self.assertIn("last handshake 6 min ago", hub)
        self.assertIn("61.2 °C", self.say("/pi")[-1])

    def test_unknown_command_is_silent(self):
        self.say("/reboot")
        self.say("/pair 1234-5678")
        self.assertEqual(FAKE.sent(), [])


class Diagnostics(Base):
    def setUp(self):
        super().setUp()
        import commands
        self.commands = commands
        for name, val in (("ip_allowed", lambda ip, cfg=None: ip.startswith("192.0.2.")),
                          ("ping", lambda ip: f"ping {ip}: UP (0.002s latency)."),
                          ("quality_text", lambda ip: f"conn test {ip}: GOOD"),
                          ("port_check", lambda ip, port: f"port {ip}:{port}: OPEN"),
                          ("dispatch", lambda text: "quick scan started")):
            p = mock.patch.object(commands, name, val)
            p.start()
            self.addCleanup(p.stop)

    def wait_jobs(self):
        for _ in range(200):
            if not self.bot._jobs:
                return
            time.sleep(0.01)

    def test_viewer_cannot_run_tests(self):
        out = self.say("/ping gate camera", user=8)
        self.assertIn("needs the <b>operator</b> role", out[-1])

    def test_ping_by_name_runs_in_the_background(self):
        out = self.say("/ping gate camera")
        self.wait_jobs()
        out = [p["text"] for p in FAKE.sent()]
        self.assertTrue(any("⏳ /ping Gate camera (192.0.2.11)" in t for t in out))
        self.assertIn("ping 192.0.2.11: UP", FAKE.sent()[-1]["text"])

    def test_outside_networks_refused(self):
        out = self.say("/ping 8.8.8.8")
        self.assertIn("Refused", out[-1])

    def test_port_with_a_multi_word_name(self):
        self.say("/port gate camera 554")
        self.wait_jobs()
        self.assertIn("192.0.2.11:554: OPEN", FAKE.sent()[-1]["text"])

    def test_ping_button_with_key(self):
        FAKE.updates.append({"update_id": 70, "callback_query": {
            "id": "q3", "from": {"id": 7}, "data": "c:test:aa:01",
            "message": {"message_id": 40, "chat": {"id": CHAT}}}})
        self.bot.poll_once(timeout=0)
        self.wait_jobs()
        self.assertIn("conn test 192.0.2.10: GOOD", FAKE.sent()[-1]["text"])

    def test_scan_cooldown(self):
        self.assertIn("quick scan started", self.say("/scan")[-1])
        self.assertIn("ran recently", self.say("/scan")[-1])
        self.t[0] += 601
        self.assertIn("quick scan started", self.say("/scan")[-1])

    def test_down_offers_ping_buttons(self):
        self.say("/down")
        kb = FAKE.sent()[-1]["reply_markup"]["inline_keyboard"]
        self.assertEqual(kb[0][0]["callback_data"], "c:ping:aa:01")

    def test_speed_off_on_capped_site(self):
        self.st["capped"] = True
        self.assertIn("data cap", self.say("/speed")[-1])

    def test_pictures_need_the_switch_and_send_a_photo(self):
        API["/api/cameras"]["cameras"][0]["key"] = "cam1"
        self.assertIn("switched off", self.say("/pic gate camera", user=8)[-1])
        self.st["pictures"] = True
        self.bot.api_raw = lambda path: b"\xff\xd8jpegdata" if "cam1/picture" in path else None
        self.say("/pic gate camera", user=8)
        self.wait_jobs()
        photos = [p for t, m, p in FAKE.calls if m == "sendPhoto"]
        self.assertEqual(len(photos), 1)

    def test_dhcp_and_find(self):
        API["/api/status"]["dhcp_guard"] = {"servers": 2, "rogues": [
            {"ip": "192.0.2.99", "mac": "aa:bb", "vendor": "TP-Link", "detail": "offers <b>10.1.1.x</b>"}]}
        try:
            t = self.say("/dhcp")[-1]
        finally:
            API["/api/status"].pop("dhcp_guard")
        self.assertIn("1 ROGUE", t)
        self.assertIn("&lt;b&gt;10.1.1.x", t)
        self.assertIn("192.0.2.13", self.say("/find printer")[-1])

    def test_dns_query_parser(self):
        import struct
        qid = 4660
        import tgbot as tb
        with mock.patch.object(tb._random, "randint", return_value=qid), \
                mock.patch.object(tb._socket, "socket") as sock:
            q = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0) + b"\x07example\x03com\x00" + struct.pack(">HH", 1, 1)
            ans = struct.pack(">HHHHHH", qid, 0x8180, 1, 1, 0, 0) + q[12:] + \
                b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + bytes([192, 0, 2, 7])
            sock.return_value.recv.return_value = ans
            self.assertEqual(tb._dns_query("example.com", "1.1.1.1"), ["192.0.2.7"])


class Fixes(Base):
    def setUp(self):
        super().setUp()
        self.posts = []
        self.restart_info = {"ok": True, "can_restart": True, "method_label": "Hikvision ISAPI",
                             "warning": "The camera drops for about 2 minutes.", "job": {}}
        self.job = {"done": True, "ok": True, "msg": "Restarted — back after 85 s", "seconds_down": 85}
        api = self.api

        def api2(path):
            if path.endswith("/restart"):
                return self.restart_info if not self.posts else {"job": self.job}
            return api(path)
        self.bot.api_get = api2
        self.bot.api_post = lambda path, body: (self.posts.append((path, body)), {"ok": True, "job": {"done": False}})[1]

    def wait_jobs(self):
        for _ in range(300):
            if not self.bot._jobs:
                return
            time.sleep(0.01)

    def press(self, data, user=7, uid=None):
        FAKE.updates.append({"update_id": uid or (len(FAKE.updates) + 200), "callback_query": {
            "id": "q", "from": {"id": user, "first_name": "U"}, "data": data,
            "message": {"message_id": 50, "chat": {"id": CHAT}}}})
        self.bot.poll_once(timeout=0)

    def yes_button(self):
        kb = FAKE.sent()[-1]["reply_markup"]["inline_keyboard"][0]
        return kb[0]["callback_data"], kb[1]["callback_data"]

    def test_off_by_default(self):
        self.assertIn("switched off", self.say("/restart tower radio")[-1])
        self.assertEqual(self.posts, [])

    def test_restart_asks_then_runs_and_reports(self):
        self.st["actions"] = True
        q = self.say("/restart tower radio")[-1]
        self.assertIn("Restart Tower radio", q)
        self.assertIn("1 device(s) behind it drop too: Gate camera", q)
        self.assertEqual(self.posts, [])                       # nothing before Yes
        yes, no = self.yes_button()
        with mock.patch.object(tgbot, "RESTART_POLL_S", 0):
            self.press(yes)
            self.wait_jobs()
        self.assertEqual(self.posts[0], ("/api/devices/aa%3A01/restart", {"confirm": True}))
        self.assertIn("✅ <b>Tower radio</b>: Restarted — back after 85 s", FAKE.sent()[-1]["text"])
        self.press(yes)                                          # the same Yes again: closed
        self.assertIn("closed", FAKE.sent()[-1]["text"])
        self.assertEqual(len(self.posts), 1)

    def test_only_the_asker_or_an_admin_may_answer(self):
        self.st["actions"] = True
        self.st["people"]["9"] = {"name": "Tech", "role": "operator"}
        self.say("/restart tower radio", user=9)
        yes, no = self.yes_button()
        self.st["people"]["10"] = {"name": "Other", "role": "operator"}
        self.press(yes, user=10)
        self.assertIn("Only", FAKE.sent()[-1]["text"])
        self.press(no, user=7)                                   # admin may cancel
        self.assertIn("Cancelled", FAKE.sent()[-1]["text"])
        self.assertEqual(self.posts, [])

    def test_question_expires(self):
        self.st["actions"] = True
        self.say("/restart tower radio")
        yes, _ = self.yes_button()
        self.t[0] += tgbotcore.CONFIRM_S + 1
        self.press(yes)
        self.assertIn("too late", FAKE.sent()[-1]["text"])
        self.assertEqual(self.posts, [])

    def test_viewer_cannot_restart_and_unsupported_device_is_explained(self):
        self.st["actions"] = True
        self.assertIn("operator", self.say("/restart tower radio", user=8)[-1])
        self.restart_info = {"ok": True, "can_restart": False, "detail": "Restarting devices is turned off"}
        self.assertIn("turned off", self.say("/restart tower radio")[-1])

    def test_poe_uses_the_switch_port_and_refuses_uplinks(self):
        self.st["actions"] = True
        devs = API["/api/devices"]["devices"]
        devs[1]["switch_port"] = {"switch_key": "sw:01", "port": "0/3"}
        devs.append({"key": "sw:01", "ip": "192.0.2.2", "name": "Core switch", "online": True})
        try:
            q = self.say("/poe gate camera")[-1]
            self.assertIn("port 0/3 of Core switch", q)
            yes, _ = self.yes_button()
            self.bot.api_post = lambda p, b: (self.posts.append((p, b)), {"ok": True, "msg": "done"})[1]
            self.press(yes)
            self.wait_jobs()
            self.assertEqual(self.posts[-1], ("/api/devices/sw%3A01/switch/action",
                                              {"action": "poe-cycle", "port": "0/3", "confirm": True}))
            self.assertEqual(self.bot.tellme[-1]["key"], "aa:02")
            devs[1]["switch_port"]["uplink"] = True
            self.assertIn("uplink", self.say("/poe gate camera")[-1])
        finally:
            devs.pop()
            devs[1].pop("switch_port", None)

    def test_maint_and_ack(self):
        held = []
        self.bot.set_maint = lambda until, who: (held.append(until), self.st.__setitem__("maint_until", until))
        self.assertIn("No maintenance", self.say("/maint")[-1])
        self.assertIn("Maintenance for 2 h", self.say("/maint 2")[-1])
        self.assertIn("left", self.say("/maint")[-1])
        self.assertIn("ended", self.say("/maint off")[-1])
        self.assertIn("is on it", self.say("/ack", user=8)[-1])

    def test_restart_netwatch_is_admin_only_and_exits_after_yes(self):
        self.st["actions"] = True
        self.st["people"]["9"] = {"name": "Tech", "role": "operator"}
        self.assertIn("admin", self.say("/restart_netwatch", user=9)[-1])
        self.say("/restart_netwatch")
        yes, _ = self.yes_button()
        with mock.patch.object(tgbot.os, "_exit") as ex, mock.patch("threading.Timer") as timer:
            self.press(yes)
            self.wait_jobs()
            timer.assert_called_once()
            timer.call_args[0][1]()
            ex.assert_called_once_with(3)


class Phase6(Base):
    def test_digest_due_once_after_7(self):
        import time as _t
        t7 = _t.mktime((2026, 11, 8, 7, 1, 0, 0, 0, -1))
        t6 = _t.mktime((2026, 11, 8, 6, 59, 0, 0, 0, -1))
        self.assertEqual(tgbot.digest_due("2026-11-07", t7), (True, "2026-11-08"))
        self.assertFalse(tgbot.digest_due("2026-11-07", t6)[0])
        self.assertFalse(tgbot.digest_due("2026-11-08", t7)[0])

    def test_daily_report_text(self):
        import history
        with mock.patch.object(tgbot, "outages", return_value=[]):
            t = tgbot.daily_report(self.bot)
        self.assertIn("Daily report", t)
        self.assertIn("Monitored: 1/3 online", t)
        self.assertIn("Internet, last 24 h: none", t)
        del history

    def test_run_command_for_the_hub(self):
        import tgsite
        with mock.patch.object(tgsite, "bot", self.bot):
            ok, text = tgsite.run_command("status", [], "Riaan")
            self.assertTrue(ok)
            self.assertIn("Test &lt;Farm&gt;", text)
            ok, text = tgsite.run_command("restart", ["tower"], "Riaan")
            self.assertFalse(ok)
            self.assertIn("own group", text)
            self.assertFalse(tgsite.run_command("maint", ["2"], "Riaan")[0])
            ok, text = tgsite.run_command("restart_netwatch", [], "Riaan")
            self.assertFalse(ok)
            ok, text = tgsite.run_command("nope", [], "Riaan")
            self.assertFalse(ok)


class Outages(unittest.TestCase):
    def test_spans_and_gaps(self):
        db = sqlite3.connect(":memory:", check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE heartbeats (key TEXT, ts INT, online INT, rtt REAL)")
        base = 1_000_000
        rows = []
        for i in range(0, 30):                     # 30 min, one sample a minute
            ts = base + i * 60
            down = 10 <= i < 15                    # both probes down for 5 min
            rows += [("__inet__8.8.8.8", ts, 0 if down else 1), ("__inet__1.1.1.1", ts, 0 if down else 1)]
        rows += [("__inet__8.8.8.8", base + 3600, 1), ("__inet__1.1.1.1", base + 3600, 1)]   # gap: Pi off
        rows += [("__inet__8.8.8.8", base + 3660, 0), ("__inet__1.1.1.1", base + 3660, 1)]   # one down: not an outage
        db.executemany("INSERT INTO heartbeats VALUES (?,?,?,NULL)", rows)
        import history
        with mock.patch.object(history, "_conn", lambda: db):
            outs = tgbot.outages(["__inet__8.8.8.8", "__inet__1.1.1.1"], 7200, now=base + 3700)
        kinds = [(o["kind"], o["start"] - base, (o["end"] or 0) - base) for o in outs]
        self.assertEqual(kinds, [("no_samples", 29 * 60, 3600), ("down", 600, 900)])
        self.assertIn("1 outage(s)", tgbot._outage_text("Internet", outs))


if __name__ == "__main__":
    unittest.main()
