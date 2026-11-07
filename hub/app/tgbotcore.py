"""The Telegram command engine shared by the site bot and the hub's Ops bot
(site and hub keep IDENTICAL copies: app/tgbotcore.py == hub/app/tgbotcore.py,
checked by tests/test_tg_same.py). The commands themselves live elsewhere:
app/tgbot.py (a site's own group) and hub/app/hubbot.py (the Ops group).

What the engine guarantees, whatever the commands are (plan section 9.1):
  * only the ONE paired group is served; a bot added to any other group leaves it;
  * only people on the list (Telegram user id → viewer / operator / admin) are
    answered; anyone else is told once a day, and kept as an access request
    the hub shows in Settings → Telegram with an "add" button;
  * a command older than STALE_S is NOT run (Telegram keeps undelivered
    messages for a day: "reboot" sent yesterday must not run today);
  * per person at most RATE_N commands per RATE_WINDOW_S;
  * a command may require a minimum role (`min_role`);
  * the offset is saved only after a batch with something in it (SD card);
  * 409 (someone else polls this bot — the hub while pairing) backs off;
  * `pause_until` in the settings stops polling (the hub pairs a group).

Commands: {name: (fn, min_role[, cooldown_s])}; fn(bot, args, who) returns
(html text, buttons), or a Job for slow work (the bot says "⏳ …" at once and
posts the result when the worker thread finishes; at most MAX_JOBS at a
time), or {"photo": jpeg bytes, "caption": html}. cooldown_s is per command
for the whole group (heavy scans, speed tests).
Buttons: rows of (label, data); data goes back as callback "c:<data>", where
data = "<command>[:<arg>]".
"""
import json
import os
import threading
import time

import tgapi

STALE_S = 300
RATE_N, RATE_WINDOW_S = 20, 600
POLL_S = 50
ROLES = ("viewer", "operator", "admin")
MAX_JOBS = 2


CONFIRM_S = 120


class Job:
    """Slow work: `start` is said at once, `work()` → (text, buttons) or a photo dict later."""
    def __init__(self, start, work):
        self.start, self.work = start, work


class Confirm:
    """A change: `question` is asked with [Yes] [Cancel]; only the person who asked
    (or an admin) may answer, within CONFIRM_S; Yes runs `job` (a Job)."""
    def __init__(self, question, job):
        self.question, self.job = question, job
NAG_EVERY_S = 86400


def role_at_least(role, need):
    return role in ROLES and ROLES.index(role) >= ROLES.index(need)


def hm(ts):
    return time.strftime("%H:%M", time.localtime(ts)) if ts else "?"


def dhm(ts):
    return time.strftime("%a %H:%M", time.localtime(ts)) if ts else "?"


def ago(seconds):
    if seconds is None:
        return "never"
    s = int(max(0, seconds))
    if s < 90:
        return f"{s} s"
    if s < 5400:
        return f"{s // 60} min"
    if s < 172800:
        h, m = divmod(s // 60, 60)
        return f"{h} h {m} min" if m else f"{h} h"
    return f"{s // 86400} days"


class Bot:
    def __init__(self, settings, commands, offset_path, *, clock=time.time, name="tgbot"):
        """settings() → {token, chat_id, bot, enabled, people, pause_until}."""
        self.settings = settings
        self.commands = commands
        self.offset_path = offset_path
        self.clock = clock
        self.name = name
        self._offset = self._load_offset()
        self._rate = {}
        self._nagged = {}
        self.requests = {}         # user id (str) -> {"name","username","ts","text"}
        self.audit = []            # last commands: {"ts","user","name","cmd","ok"}
        self.health = {"last_poll": None, "last_error": "", "conflict_until": 0}
        self._stop = False
        self._cool = {}            # command -> ts of last run
        self._pending = {}         # nonce -> {"uid","cmd","job","until","q"}
        self._jobs = 0
        self._jobs_lock = threading.Lock()

    # ---- hooks ------------------------------------------------------------------
    def tick(self):
        """Called before every poll (e.g. one-shot watches)."""

    # ---- sending ----------------------------------------------------------------
    def send(self, text, reply_to=None, buttons=None):
        st = self.settings()
        if not st.get("token") or not st.get("chat_id"):
            return None
        kb = tgapi.keyboard(buttons)
        res = tgapi.send(st["token"], st["chat_id"], text, reply_to=reply_to, buttons=kb)
        if not res.ok and res.code == 400 and "parse" in res.error.lower():
            res = tgapi.send(st["token"], st["chat_id"], tgapi.strip_tags(text), reply_to=reply_to,
                             html_mode=False, buttons=kb)
        return res

    # ---- offset -----------------------------------------------------------------
    def _load_offset(self):
        try:
            with open(self.offset_path) as f:
                return int(json.load(f).get("offset") or 0) or None
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def _save_offset(self):
        tmp = self.offset_path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({"offset": self._offset}, f)
            os.replace(tmp, self.offset_path)
        except OSError:
            pass

    # ---- the loop ---------------------------------------------------------------
    def start(self):
        threading.Thread(target=self.loop, daemon=True, name=self.name).start()

    def loop(self):
        while not self._stop:
            try:
                wait = self.poll_once()
            except Exception as e:                       # never let the thread die
                print(f"[{self.name}] {e.__class__.__name__}: {e}", flush=True)
                wait = 30
            if wait:
                time.sleep(wait)

    def poll_once(self, timeout=POLL_S):
        st = self.settings()
        token, chat = st.get("token"), st.get("chat_id")
        if not token or not chat or not st.get("enabled", True):
            return 10
        now = self.clock()
        if now < float(st.get("pause_until") or 0) or now < self.health["conflict_until"]:
            return 5
        try:
            self.tick()
        except Exception as e:
            print(f"[{self.name}] tick: {e.__class__.__name__}: {e}", flush=True)
        res = tgapi.get_updates(token, self._offset, timeout=timeout)
        self.health["last_poll"] = now
        if not res.ok:
            self.health["last_error"] = res.error
            if res.code == 409:              # someone else polls (the hub pairing): let it
                self.health["conflict_until"] = self.clock() + 60
                return 5
            return float(res.retry_after or 30) if res.code == 429 else 30
        self.health["last_error"] = ""
        ups = res.data or []
        for u in ups:
            self._offset = max(self._offset or 0, int(u.get("update_id", 0)) + 1)
            try:
                self.handle(u, st)
            except Exception as e:
                print(f"[{self.name}] update failed: {e.__class__.__name__}: {e}", flush=True)
        if ups:
            self._save_offset()
        return 0

    # ---- dispatch ---------------------------------------------------------------
    def role(self, st, uid):
        p = (st.get("people") or {}).get(str(uid))
        return p.get("role") if isinstance(p, dict) and p.get("role") in ROLES else None

    def _limited(self, uid):
        now = self.clock()
        hits = [t for t in self._rate.get(uid, []) if now - t < RATE_WINDOW_S]
        hits.append(now)
        self._rate[uid] = hits
        return len(hits) > RATE_N

    def handle(self, u, st):
        token, chat = st["token"], st["chat_id"]
        if u.get("my_chat_member"):
            m = u["my_chat_member"]
            c = (m.get("chat") or {}).get("id")
            new = (m.get("new_chat_member") or {}).get("status")
            if c and c != chat and new in ("member", "administrator"):
                tgapi.call(token, "leaveChat", {"chat_id": c})       # only our own group
            return
        cb = u.get("callback_query")
        if cb:
            tgapi.call(token, "answerCallbackQuery", {"callback_query_id": cb.get("id")})
            msg = cb.get("message") or {}
            data = str(cb.get("data") or "")
            if (msg.get("chat") or {}).get("id") != chat or not data.startswith("c:"):
                return
            cmd, _, arg = data[2:].partition(":")
            self.run(st, cb.get("from") or {}, cmd, [arg] if arg else [],
                     sent_at=self.clock(), reply_to=msg.get("message_id"))
            return
        msg = u.get("message") or {}
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return
        c = msg.get("chat") or {}
        if c.get("id") != chat:
            if c.get("type") == "private" and text.split()[0].lower() in ("/start", "/help"):
                tgapi.send(token, c.get("id"), "I only answer in my FarmCamSA group.", html_mode=False)
            return
        head, *args = text.split()
        cmd, _, at = head[1:].partition("@")
        bot = st.get("bot") or ""
        if at and bot and at.lower() != bot.lower():
            return                                              # meant for another bot
        self.run(st, msg.get("from") or {}, cmd.lower(), args, sent_at=msg.get("date") or 0,
                 reply_to=msg.get("message_id"))

    def run(self, st, who, cmd, args, *, sent_at, reply_to):
        uid = who.get("id")
        name = " ".join(x for x in (who.get("first_name"), who.get("last_name")) if x) or \
            who.get("username") or str(uid)
        if cmd in ("y", "n") and args:
            return self._answer(st, uid, name, cmd, args[0], reply_to)
        if cmd not in self.commands and cmd != "whoami":
            return                                   # /pair and anything unknown: not ours
        if self.clock() - float(sent_at or 0) > STALE_S:
            self.send(f"⏳ Ignored /{tgapi.esc(cmd)} sent at {hm(sent_at)} — too old. "
                      "Send it again.", reply_to=reply_to)
            return
        role = self.role(st, uid)
        if cmd == "whoami":
            self.send(f"You are <b>{tgapi.esc(name)}</b>, Telegram id <code>{uid}</code>, "
                      f"role: <b>{role or 'none'}</b>.", reply_to=reply_to)
            return
        if not role:
            now = self.clock()
            self.requests[str(uid)] = {"name": name[:64], "username": (who.get("username") or "")[:64],
                                       "ts": int(now), "text": f"/{cmd}"[:40]}
            if len(self.requests) > 50:
                oldest = min(self.requests, key=lambda k: self.requests[k]["ts"])
                self.requests.pop(oldest, None)
            if now - self._nagged.get(uid, 0) > NAG_EVERY_S:
                self._nagged[uid] = now
                self.send(f"Sorry {tgapi.esc(name)}, you are not on this group's list yet. "
                          "FarmCamSA can see your request and add you.", reply_to=reply_to)
            return
        spec = self.commands[cmd]
        fn, need = spec[0], spec[1]
        cool = spec[2] if len(spec) > 2 else 0
        if not role_at_least(role, need):
            self.send(f"/{tgapi.esc(cmd)} needs the <b>{need}</b> role; you are {role}.",
                      reply_to=reply_to)
            return
        if self._limited(uid):
            return
        if cool and self.clock() - self._cool.get(cmd, 0) < cool:
            left = int(cool - (self.clock() - self._cool[cmd]))
            self.send(f"/{tgapi.esc(cmd)} ran recently — try again in {left // 60 + 1} min.", reply_to=reply_to)
            return
        try:
            out = fn(self, args, name)
            ok = True
        except Exception as e:
            print(f"[{self.name}] /{cmd} failed: {e.__class__.__name__}: {e}", flush=True)
            out, ok = (f"Sorry, /{tgapi.esc(cmd)} failed ({tgapi.esc(e.__class__.__name__)}).", None), False
        self.audit = (self.audit + [{"ts": int(self.clock()), "user": uid, "name": name[:64],
                                     "cmd": cmd, "ok": ok}])[-40:]
        if isinstance(out, Confirm):
            import secrets
            nonce = secrets.token_hex(5)
            now = self.clock()
            self._pending = {k: v for k, v in self._pending.items() if v["until"] > now}
            self._pending[nonce] = {"uid": uid, "cmd": cmd, "job": out.job, "until": now + CONFIRM_S,
                                    "name": name, "cool": cool}
            self.send(out.question + f"\n<i>Only {tgapi.esc(name)} (or an admin) can answer · "
                                     f"{CONFIRM_S // 60} min</i>", reply_to=reply_to,
                      buttons=[[("✅ Yes, do it", f"y:{nonce}"), ("✖ Cancel", f"n:{nonce}")]])
            return
        if isinstance(out, Job):
            with self._jobs_lock:
                busy = self._jobs >= MAX_JOBS
                if not busy:
                    self._jobs += 1
            if busy:
                self.send("⏳ Two tests are already running — try again in a minute.", reply_to=reply_to)
                return
            if cool:
                self._cool[cmd] = self.clock()
            self.send(out.start, reply_to=reply_to)
            threading.Thread(target=self._run_job, args=(cmd, out, reply_to), daemon=True).start()
            return
        if ok and cool:
            self._cool[cmd] = self.clock()
        self.deliver(out, reply_to)

    def _answer(self, st, uid, name, yn, nonce, reply_to):
        p = self._pending.get(nonce)
        role = self.role(st, uid)
        if not p:
            self.send("That question is closed (answered, cancelled or too old).", reply_to=reply_to)
            return
        if uid != p["uid"] and role != "admin":
            self.send(f"Only {tgapi.esc(p['name'])} or an admin can answer that.", reply_to=reply_to)
            return
        self._pending.pop(nonce, None)
        if self.clock() > p["until"]:
            self.send(f"/{tgapi.esc(p['cmd'])}: too late — ask again.", reply_to=reply_to)
            return
        if yn == "n":
            self.send(f"✖ Cancelled by {tgapi.esc(name)}.", reply_to=reply_to)
            return
        self.audit = (self.audit + [{"ts": int(self.clock()), "user": uid, "name": name[:64],
                                     "cmd": p["cmd"] + " (confirmed)", "ok": True}])[-40:]
        with self._jobs_lock:
            busy = self._jobs >= MAX_JOBS
            if not busy:
                self._jobs += 1
        if busy:
            self.send("⏳ Two jobs are already running — try again in a minute.", reply_to=reply_to)
            return
        if p.get("cool"):
            self._cool[p["cmd"]] = self.clock()
        self.send(p["job"].start + f" (confirmed by {tgapi.esc(name)})", reply_to=reply_to)
        threading.Thread(target=self._run_job, args=(p["cmd"], p["job"], reply_to), daemon=True).start()

    def deliver(self, out, reply_to):
        if isinstance(out, dict) and out.get("photo"):
            st = self.settings()
            res = tgapi.send_photo(st.get("token"), st.get("chat_id"), out["photo"],
                                   out.get("caption", ""), reply_to=reply_to)
            if not res.ok:
                self.send(f"Could not send the picture: {tgapi.esc(res.error)}", reply_to=reply_to)
            return
        text, buttons = out
        if text:
            self.send(text, reply_to=reply_to, buttons=buttons)

    def _run_job(self, cmd, job, reply_to):
        try:
            try:
                out = job.work()
            except Exception as e:
                print(f"[{self.name}] /{cmd} job failed: {e.__class__.__name__}: {e}", flush=True)
                out = (f"Sorry, /{tgapi.esc(cmd)} failed ({tgapi.esc(e.__class__.__name__)}).", None)
            self.deliver(out, reply_to)
        finally:
            with self._jobs_lock:
                self._jobs -= 1             # only once the answer is out

    def public(self):
        """Safe for the settings page: health, requests, recent commands."""
        listed = set((self.settings().get("people") or {}).keys())
        return {"poll": self.health.get("last_poll"), "error": self.health.get("last_error", ""),
                "requests": [{"id": k, **v} for k, v in sorted(self.requests.items(),
                                                                key=lambda kv: -kv[1]["ts"])
                             if k not in listed][:20],
                "audit": list(self.audit[-15:])}
