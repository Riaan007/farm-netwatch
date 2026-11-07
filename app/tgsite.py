"""Telegram on the site Pi — phase 1: this site's alerts go to its own group.

Each site has its OWN bot (Telegram allows one poller per bot, and a bot
never sees another bot's messages), and sends to Telegram straight over the
farm's own internet, so its group still hears from it when the VPN is down.

The hub keeps the master copy of the token and the paired chat and pushes
them here with the hub key (POST /api/telegram); the site never shows the
token again (GET only gives a fingerprint). Kept in /data/telegram.json
(0600), NOT in config.json, so GET /api/config and the plain Export bundle
never carry it.

Every alert this site raises is also kept in a small in-memory log
(`recent()`), with a per-process `boot` id and a sequence number in
/api/status. The hub reads new entries and mirrors them into the Ops group —
whether or not this site has a Telegram group of its own.

Levels: critical / warning / info. `notify.push` maps the old ntfy
priorities onto them (urgent/max → critical, high → warning, else info).

Phase 2: `tgbot.SiteBot` long-polls this site's bot and answers read-only
commands in the paired group, for the people the hub lists (`people`).
"""
import collections
import json
import re
import os
import secrets
import threading
import time

from flask import Blueprint, jsonify, request

import config
import linkwatch
import siteauth
import tgapi
import tgbot
import tgoutbox

DATA_DIR = os.environ.get("NETWATCH_DATA", "/data")
STATE_PATH = os.path.join(DATA_DIR, "telegram.json")
OUTBOX_PATH = os.path.join(DATA_DIR, "tg_outbox.json")
LEVELS = ("critical", "warning", "info")
ICON = {"critical": "🔴", "warning": "🟠", "info": "🟢"}

_lock = threading.RLock()   # re-entrant: never deadlock on a nested call (hub incident 17:37)
_state = None
_log = collections.deque(maxlen=60)
_seq = 0
BOOT = secrets.token_hex(4)
_health = {"last_ok": None, "dead": ""}


# ---- settings --------------------------------------------------------------
def _load():
    global _state
    if _state is None:
        try:
            with open(STATE_PATH) as f:
                d = json.load(f)
            _state = d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            _state = {}
    return _state


def _save(st):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_PATH)


def settings():
    with _lock:
        return dict(_load())


def _resolve(dest):
    if dest != "site":
        return None
    st = settings()
    if not st.get("enabled", True) or not st.get("token") or not st.get("chat_id"):
        return None
    return st["token"], st["chat_id"]


def _on_dead(dest, why):
    _health["dead"] = why
    print(f"[telegram] group unreachable, alerts stop until it is paired again: {why}", flush=True)


def _on_migrate(dest, new_chat):
    with _lock:
        st = _load()
        st["chat_id"] = new_chat
        _save(st)
    print("[telegram] group became a supergroup; following it", flush=True)


def _on_sent(dest):
    _health["last_ok"] = time.time()
    _health["dead"] = ""


outbox = tgoutbox.Outbox(OUTBOX_PATH, _resolve, on_dead=_on_dead, on_migrate=_on_migrate,
                         on_sent=_on_sent)


def status():
    """Safe to show anyone with the login or the hub key: no token."""
    st = settings()
    err = outbox.last_error.get("site")
    return {
        "configured": bool(st.get("token")),
        "token_fp": tgapi.fingerprint(st.get("token")),
        "bot": st.get("bot") or "",
        "chat_id": st.get("chat_id"),
        "chat_title": st.get("chat_title") or "",
        "enabled": st.get("enabled", True),
        "queued": outbox.pending("site"),
        "last_ok": _health["last_ok"],
        "last_error": err[1] if err else "",
        "dead": _health["dead"],
        "people": len(st.get("people") or {}),
        "people_fp": tgapi.obj_fp(st.get("people") or {}),
        "opts": {"pictures": bool(st.get("pictures")), "capped": bool(st.get("capped")),
                 "actions": bool(st.get("actions")), "no_digest": bool(st.get("no_digest"))},
        "maint_until": st.get("maint_until") or 0,
        "hb_fp": tgapi.obj_fp([st.get("hb_topic") or "", st.get("hb_key") or "", st.get("hb_server") or ""]),
        "link": (watch.st if watch else None),
        "listener": bot.public() if bot else None,
    }


def apply(body):
    """Hub (or a logged-in person) sets token / chat / switch. Returns (ok, error)."""
    if not isinstance(body, dict):
        return False, "expected a JSON object"
    with _lock:
        st = dict(_load())
        if "token" in body:
            tok = (body.get("token") or "").strip()
            if tok and not tgapi.valid_token(tok):
                return False, "that is not a bot token (BotFather gives 123456789:AA…)"
            if tok != st.get("token"):
                st["token"] = tok
                st.pop("chat_id", None)          # a new bot is not in the old group
                st.pop("chat_title", None)
        if "chat_id" in body:
            cid = body.get("chat_id")
            if cid in (None, ""):
                st.pop("chat_id", None)
            elif isinstance(cid, int) and not isinstance(cid, bool):
                st["chat_id"] = cid
            else:
                return False, "chat_id must be a number"
        for k, n in (("chat_title", 128), ("bot", 64)):
            if k in body:
                st[k] = str(body.get(k) or "")[:n]
        if "enabled" in body:
            st["enabled"] = bool(body["enabled"])
        for k in ("pictures", "capped", "actions", "no_digest"):   # pictures; capped line; fixes; daily report off
            if k in body:
                st[k] = bool(body[k])
        if "people" in body:
            ppl = body.get("people")
            if not isinstance(ppl, dict) or len(ppl) > 200:
                return False, "people must be an object {user id: {name, role}}"
            clean = {}
            for uid, p in ppl.items():
                if not re.fullmatch(r"-?\d{1,15}", str(uid)) or not isinstance(p, dict) \
                        or p.get("role") not in tgbot.ROLES:
                    return False, f"bad person entry {str(uid)[:20]}"
                clean[str(uid)] = {"name": str(p.get("name") or "")[:64], "role": p["role"]}
            st["people"] = clean
        for k, pat in (("hb_topic", r"[A-Za-z0-9_-]{8,64}"), ("hb_key", r"[0-9a-f]{32,128}"),
                       ("hb_server", r"https://[A-Za-z0-9.-]+(:\d+)?")):
            if k in body:
                v = str(body.get(k) or "")
                if v and not re.fullmatch(pat, v):
                    return False, f"bad {k}"
                st[k] = v
        if "pause_until" in body:
            try:
                st["pause_until"] = min(float(body.get("pause_until") or 0), time.time() + 1200)
            except (TypeError, ValueError):
                return False, "pause_until must be a number"
        if not st.get("token"):
            st = {"enabled": st.get("enabled", True), "people": st.get("people") or {},
                  "pictures": bool(st.get("pictures")), "capped": bool(st.get("capped")),
                  "actions": bool(st.get("actions")), "no_digest": bool(st.get("no_digest")),
                  **{k: st[k] for k in ("hb_topic", "hb_key", "hb_server") if st.get(k)}}
        _save(st)
        global _state
        _state = st
    _health["dead"] = ""
    if not st.get("chat_id"):
        outbox.drop_dest("site")
    return True, ""


# ---- alerts ----------------------------------------------------------------
def level_for(priority):
    p = str(priority or "default").lower()
    if p in ("urgent", "max", "5"):
        return "critical"
    if p in ("high", "4"):
        return "warning"
    return "info"


def _site_name():
    try:
        return (config.load().get("site") or {}).get("name") or "This site"
    except Exception:
        return "This site"


# Buttons on every alert in the site's group: the commands that answer the
# question the alert raises, picked from the title (no change in the modules
# that raise alerts). Pressing one runs it like typing it (tgbotcore).
_BUTTONS = (
    (("hub link",), [("🔗 Hub link", "hub"), ("🌐 Internet", "internet")]),
    (("internet",), [("🌐 Internet", "internet"), ("📉 Outages", "outages")]),
    (("pi health", "temperature", "undervolt", "disk", "memory", "clock", "smart"), [("🖥 Pi", "pi")]),
    (("wi-fi", "radio", "link"), [("📡 Radio links", "links")]),
    (("camera", "recorder", "nvr", "footage", "recording"), [("📷 Cameras", "cameras"), ("💽 Recorders", "nvr")]),
)
_ALWAYS = [("📋 Status", "status"), ("🔴 What's down", "down")]


def buttons_for(title, level="info"):
    t = (title or "").lower()
    rows = [_ALWAYS]
    for words, row in _BUTTONS:
        if any(w in t for w in words):
            rows = [row, _ALWAYS]
            break
    if level in ("warning", "critical"):
        rows.append([("🙋 I'm on it", "ack")])
    return rows


def format_alert(level, title, body, site_name):
    head = f"{ICON.get(level, '🟢')} <b>{tgapi.esc(site_name)}</b> · {tgapi.esc(title)}"
    body = tgapi.clip(body or "", 3000)
    return head + ("\n" + tgapi.esc(body) if body else "")


def alert(level, title, body, ops=True, buttons=None):
    """Log the alert for the hub, and queue it for this site's group. Never raises."""
    global _seq
    try:
        level = level if level in LEVELS else "info"
        title = " ".join(str(title or "Netwatch").split())[:160]
        body = str(body or "")[:3000]
        with _lock:
            _seq += 1
            _log.append({"seq": _seq, "ts": int(time.time()), "level": level,
                         "title": title, "body": body, "ops": bool(ops)})
        if _resolve("site"):
            if _in_maintenance():
                with _lock:
                    _held.append((int(time.time()), level, title))
                    del _held[:-200]
                return
            outbox.put("site", format_alert(level, title, body, _site_name()), title=title,
                       silent=(level == "info"), buttons=buttons or buttons_for(title, level))
    except Exception as e:
        print(f"[telegram] alert not queued: {e.__class__.__name__}: {e}", flush=True)


# ---- maintenance (phase 5): hold this group's alerts while someone works on site ----
_held = []


def _in_maintenance():
    until = float(settings().get("maint_until") or 0)
    if until and time.time() >= until:
        set_maint(0, "time")                    # it ran out: summarise what was held
        return False
    return until > time.time()


def set_maint(until, who):
    with _lock:
        st = dict(_load())
        was = float(st.get("maint_until") or 0)
        if until:
            st["maint_until"], st["maint_by"] = int(until), str(who)[:64]
        else:
            st.pop("maint_until", None)
            st.pop("maint_by", None)
        _save(st)
        global _state
        _state = st
        held = list(_held) if (was and not until) else []
        if was and not until:
            _held.clear()
    if was and not until and _resolve("site"):
        lines = [f"• {time.strftime('%H:%M', time.localtime(t))} {tgapi.esc(title)}" for t, lvl, title in held[:30]]
        outbox.put("site", f"🛠 <b>{tgapi.esc(_site_name())}</b> · maintenance over"
                   + (f" ({'ran out' if who == 'time' else 'ended by ' + tgapi.esc(who)})")
                   + (f" — {len(held)} alert(s) were held:\n" + "\n".join(lines) if held else " — nothing happened."),
                   title="Maintenance over", buttons=[_ALWAYS])


def alerts_marker():
    with _lock:
        return {"boot": BOOT, "seq": _seq}


def recent(boot, after):
    with _lock:
        if boot != BOOT:
            after = 0                       # we restarted: everything we still have is new
        return {"boot": BOOT, "seq": _seq,
                "alerts": [dict(a) for a in _log if a["seq"] > after]}


def send_test():
    target = _resolve("site")
    if not target:
        return False, "no token or no paired group"
    res = tgapi.send(target[0], target[1],
                     f"🔔 <b>{tgapi.esc(_site_name())}</b> · test message\n"
                     "This site's alerts will arrive in this group.")
    if res.ok:
        _on_sent("site")
    return res.ok, res.error


# ---- the bot (phase 2) ------------------------------------------------------
_app = None
bot = None
_cmds_for = None


def _api_get(path):
    """This site's own API, in-process (siteauth's internal secret)."""
    if _app is None:
        return None
    r = _app.test_client().get(path, headers={siteauth.INTERNAL_HEADER: siteauth.INTERNAL_SECRET},
                               environ_base={"REMOTE_ADDR": "127.0.0.1"})
    return r.get_json(silent=True) if r.status_code == 200 else None


def _api_raw(path):
    """Raw bytes from this site's own API (camera pictures), or None."""
    if _app is None:
        return None
    r = _app.test_client().get(path, headers={siteauth.INTERNAL_HEADER: siteauth.INTERNAL_SECRET},
                               environ_base={"REMOTE_ADDR": "127.0.0.1"})
    return r.get_data() if r.status_code == 200 and r.mimetype == "image/jpeg" else None


def _api_post(path, body):
    """POST to this site's own API, in-process; the JSON answer (any status) or None."""
    if _app is None:
        return None
    r = _app.test_client().post(path, json=body, headers={siteauth.INTERNAL_HEADER: siteauth.INTERNAL_SECRET},
                                environ_base={"REMOTE_ADDR": "127.0.0.1"})
    return r.get_json(silent=True)


def _set_commands(st):
    """The group's "/" menu. Once per bot + group."""
    global _cmds_for
    key = (tgapi.fingerprint(st.get("token")), st.get("chat_id"))
    if _cmds_for == key:
        return
    res = tgapi.call(st["token"], "setMyCommands", {
        "commands": [{"command": c, "description": d} for c, d in tgbot.BOT_COMMANDS],
        "scope": {"type": "chat", "chat_id": st["chat_id"]}})
    if res.ok:
        _cmds_for = key


watch = None


def _telegram_ok():
    if not bot or not settings().get("chat_id"):
        return None
    lp = bot.health.get("last_poll")
    return bool(lp) and time.time() - lp < 120 and not bot.health.get("last_error")


def start(app=None):
    global _app, bot, watch
    _app = app
    outbox.start()
    if app is not None:
        bot = _Bot(settings, _api_get, api_raw=_api_raw)
        bot.api_post = _api_post
        bot.start()
        import hubvpn
        watch = linkwatch.LinkWatch(settings, _api_get,
                                    lambda lvl, title, body, buttons: alert(lvl, title, body, buttons=buttons),
                                    conf_paths=[hubvpn.WG_CONF, os.path.join(DATA_DIR, "wg-client", "wg_confs", "wg0.conf")],
                                    telegram_ok=_telegram_ok)
        watch.start()


def _maybe_digest(b, st):
    """07:00 daily report into this site's group (phase 6), once a day, unless the
    hub switched it off (no_digest) or the group is in maintenance."""
    if not st.get("chat_id") or st.get("no_digest") or _in_maintenance():
        return
    due, today = tgbot.digest_due(st.get("digest_date"), time.time())
    first = not st.get("digest_date")          # just switched on / just deployed: start tomorrow
    if not due and not first:
        return
    with _lock:
        s2 = dict(_load())
        s2["digest_date"] = today
        _save(s2)
        global _state
        _state = s2
    if first:
        return
    outbox.put("site", tgbot.daily_report(b), title="Daily report", silent=True,
               buttons=[[("📋 Status", "status"), ("📉 Outages", "outages")]])


class _Bot(tgbot.SiteBot):
    def tick(self):
        st = settings()
        if st.get("token") and st.get("chat_id"):
            _set_commands(st)
        _in_maintenance()                       # ends a maintenance window on time
        _maybe_digest(self, st)
        super().tick()

    def set_maint(self, until, who):
        set_maint(until, who)


# ---- HTTP ------------------------------------------------------------------
bp = Blueprint("telegram", __name__)


@bp.route("/api/telegram", methods=["GET", "POST"])
@siteauth.required
def api_telegram():
    if request.method == "POST":
        ok, err = apply(request.get_json(force=True, silent=True))
        if not ok:
            return jsonify({"ok": False, "error": err}), 400
        return jsonify({"ok": True, **status()})
    return jsonify(status())


@bp.route("/api/telegram/test", methods=["POST"])
@siteauth.required
def api_telegram_test():
    ok, err = send_test()
    return jsonify({"ok": ok, "error": err}), (200 if ok else 400)


def run_command(cmd, args, who):
    """Phase 6: the hub's Ops group runs one of this site's LOOK or TEST commands
    (/run <site> <cmd>). Fixes, pictures and anything needing a Yes are refused here:
    they belong in the site's own group, where the people of that site can see them."""
    import tgbotcore
    spec = tgbot.COMMANDS.get(cmd)
    if cmd in tgbot.SITE_GROUP_ONLY:
        return False, f"/{tgapi.esc(cmd)} only works in the site's own group"
    if not spec or cmd in ("help", "start", "menu"):
        return False, f"/{tgapi.esc(cmd)} is not a site command (try status, internet, down, ping …)"
    if not tgbotcore.role_at_least("operator", spec[1]):
        return False, f"/{tgapi.esc(cmd)} needs the site's own group"
    if bot is None:
        return False, "this site's bot is not running"
    out = spec[0](bot, list(args)[:6], who)
    if isinstance(out, tgbotcore.Confirm):
        return False, f"/{tgapi.esc(cmd)} changes something — run it in the site's own group (it asks Yes/Cancel there)"
    if isinstance(out, tgbotcore.Job):
        out = out.work()
    if isinstance(out, dict):
        return False, "pictures go to the site's own group only"
    return True, out[0]


@bp.route("/api/telegram/run", methods=["POST"])
@siteauth.required
def api_telegram_run():
    body = request.get_json(force=True, silent=True) or {}
    cmd = str(body.get("cmd") or "").strip().lstrip("/").lower()[:32]
    args = [str(a)[:80] for a in (body.get("args") or []) if isinstance(a, (str, int))][:6]
    try:
        ok, text = run_command(cmd, args, str(body.get("who") or "the hub")[:64])
    except Exception as e:
        ok, text = False, f"failed on the Pi ({e.__class__.__name__})"
    return jsonify({"ok": ok, "text": text}), (200 if ok else 400)


@bp.route("/api/alerts/recent")
@siteauth.required
def api_alerts_recent():
    try:
        after = int(request.args.get("after") or 0)
    except ValueError:
        after = 0
    return jsonify(recent(request.args.get("boot") or "", after))
