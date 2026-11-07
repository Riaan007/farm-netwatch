"""Telegram on the hub — phase 1 of the Telegram + ntfy plan.

Two kinds of bot:
  * the Ops bot  → the "FarmCamSA Ops" group: every alert from every site,
                   the hub's own alerts, in one place;
  * one bot PER SITE → that site's own group. The SITE Pi sends its own alerts
                   there over the farm's internet (app/tgsite.py), so the group
                   still hears from it while the VPN is down. The hub sends
                   only what it alone can see (site offline, monitored devices,
                   IP conflicts, rogue DHCP) into the same group with the same
                   bot, so each group has one face.

Telegram allows ONE poller per bot (409 Conflict) and a bot never sees other
bots' messages; that is why each site has its own bot and why the hub learns
about site alerts from the site's API (`mirror_site`), not from Telegram.

Secrets: tokens in /data/telegram.json (0600), never in hub.json, never in an
API reply (only a fingerprint and the bot's @name). The hub is the master
copy of every site's token + paired group and pushes them to the site with
the hub key (`after_status`), re-pushing when the site's fingerprint or chat
differs (a rebuilt Pi gets them back by itself).

Pairing: Settings → Telegram → "Pair a group" gives a one-time code for 10
minutes; `/pair 1234-5678` in the new group binds it. During those minutes
the hub polls that bot itself (getUpdates); nothing else polls it in phase 1.
"""
import hmac
import json
import os
import re
import secrets
import threading
import time

import requests
from flask import Blueprint, jsonify, request

import heartbeats
import hubconfig
import siteapi
import tgapi
import tgbotcore
import tgoutbox

DATA_DIR = os.environ.get("HUB_DATA", "/data")
SECRETS_PATH = os.path.join(DATA_DIR, "telegram.json")
STATE_PATH = os.path.join(DATA_DIR, "tg_state.json")
OUTBOX_PATH = os.path.join(DATA_DIR, "tg_outbox.json")
OPS_OFFSET_PATH = os.path.join(DATA_DIR, "tg_ops_offset.json")
PAIR_409_TRIES = 8
PAIR_409_WAIT_S = 8
PAIR_S = 600
PAIR_MAX_WRONG = 5
SYNC_RETRY_S = 300
MIRROR_LATE_S = 300
ICON = {"critical": "🔴", "warning": "🟠", "info": "🟢"}
PAIR_RE = re.compile(r"^/pair(?:@\w+)?\s+(\d{4})-?(\d{4})\s*$", re.I)

_lock = threading.RLock()   # re-entrant: overview() holds it while the Ops bot reads its settings
_secrets = None
_state = None
_pairing = {}        # target -> {"code", "expires", "status", "error", "wrong", "chat_title"}
_last_sync = {}      # sid -> ts of the last push attempt
_dead = {}           # target -> why the group stopped taking messages
_listener = {}       # sid -> the site bot's public state (requests, audit) from its status
ops_bot = None       # hubbot.OpsBot once started


# ---- storage ---------------------------------------------------------------
def _read(path):
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path, data, secret):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    if secret:
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _sec():
    global _secrets
    if _secrets is None:
        _secrets = _read(SECRETS_PATH)
        _secrets.setdefault("ops", {})
        _secrets.setdefault("sites", {})
        _secrets.setdefault("hb", {})          # sid -> {"topic","key","server"} (phase 3)
    return _secrets


def _st():
    global _state
    if _state is None:
        _state = _read(STATE_PATH)
        _state.setdefault("mirror", {})
    return _state


def _entry(target):
    """target = "ops" or "site:<id>" → its (mutable) secrets entry, or None."""
    s = _sec()
    if target == "ops":
        return s["ops"]
    if target.startswith("site:"):
        return s["sites"].setdefault(target[5:], {})
    return None


def _save_secrets():
    _write(SECRETS_PATH, _sec(), secret=True)


# ---- outbox ----------------------------------------------------------------
def _resolve(dest):
    with _lock:
        e = _entry(dest) if dest == "ops" or dest.startswith("site:") else None
        if not e or not e.get("token") or not e.get("chat_id") or not e.get("enabled", True):
            return None
        return e["token"], e["chat_id"]


def _on_dead(dest, why):
    _dead[dest] = why
    print(f"[telegram] {dest}: group unreachable ({why}); pair it again", flush=True)


def _on_migrate(dest, chat):
    with _lock:
        e = _entry(dest)
        if e is not None:
            e["chat_id"] = chat
            _save_secrets()
    print(f"[telegram] {dest}: group became a supergroup; following it", flush=True)
    if dest.startswith("site:"):
        _last_sync.pop(dest[5:], None)


outbox = tgoutbox.Outbox(OUTBOX_PATH, _resolve, on_dead=_on_dead, on_migrate=_on_migrate,
                         on_sent=lambda dest: _dead.pop(dest, None))


def _ops_settings():
    with _lock:
        e = dict(_sec()["ops"])
    p = _pairing.get("ops")
    if p and p.get("status") == "waiting":
        e["pause_until"] = p["expires"]           # the pairing worker polls meanwhile
    return e


def start(site_card=None, snapshot=None):
    """site_card / snapshot: server._site_card and poller.snapshot (Ops bot data)."""
    global ops_bot
    outbox.start()
    try:
        import publicip
        import wgeasy

        def _wg_conf():
            for s in hubconfig.load().get("sites", []):
                if s.get("wg_client_id"):
                    return wgeasy.get_configuration(s["wg_client_id"])
            return ""
        heartbeats.start(_hb_secrets, _wg_conf, lambda: (publicip.hub_card() or {}).get("ip"), _hb_send)
    except Exception as e:
        print(f"[telegram] heartbeats not started: {e.__class__.__name__}: {e}", flush=True)
    if site_card and snapshot:
        import hubbot
        hubbot.configure(site_card, snapshot)
        ops_bot = hubbot.OpsBot(_ops_settings, OPS_OFFSET_PATH)

        def _get_report():
            with _lock:
                return _st().get("ops_report_date")

        def _put_report(d):
            with _lock:
                _st()["ops_report_date"] = d
                _write(STATE_PATH, _st(), secret=False)
        ops_bot.report_state = (_get_report, _put_report)
        ops_bot.start()


# ---- sending ---------------------------------------------------------------
def _site_name(sid):
    for s in hubconfig.load().get("sites", []):
        if s.get("id") == sid:
            return s.get("name") or sid
    return sid


def format_alert(level, title, body, where):
    head = f"{ICON.get(level, '🟢')} <b>{tgapi.esc(where)}</b> · {tgapi.esc(title)}"
    body = tgapi.clip(body or "", 3000)
    return head + ("\n" + tgapi.esc(body) if body else "")


SITE_ALWAYS = [("📋 Status", "status"), ("🔴 What's down", "down")]


def ops_buttons(site):
    if site:
        return [[("📋 This site", f"site:{site}"), ("🧰 Visit brief", f"visit:{site}"), ("🗺 All sites", "sites")]]
    return [[("🗺 All sites", "sites"), ("🖥 Hub health", "hubhealth")]]


def alert(level, title, body, *, site=None, ref=None, reply_ref=None, resolves=None,
          site_buttons=None, to_site=True):
    """An alert the HUB raised. Goes to the Ops group, and — when it is about a
    site — to that site's group too. Never raises. Both get buttons: the Ops
    group's open this site / all sites (the Ops bot answers); the site group's
    `site_buttons` rows + Status / What's down (the SITE's bot answers — over
    the farm's own internet, so they work even while the hub can't reach it)."""
    try:
        level = level if level in ICON else "info"
        title = " ".join(str(title or "Netwatch Hub").split())[:160]
        where = _site_name(site) if site else "Hub"
        text = format_alert(level, title, body, where)
        silent = level == "info"
        rs = list(resolves or [])
        ack = [[("🙋 I'm on it", "ack")]] if level in ("warning", "critical") else []
        if _resolve("ops"):
            outbox.put("ops", text, title=f"{where}: {title}", silent=silent,
                       buttons=ops_buttons(site) + ack,
                       ref=f"ops|{ref}" if ref else None,
                       reply_ref=f"ops|{reply_ref}" if reply_ref else None,
                       resolves=[f"ops|{r}" for r in rs])
        if site and to_site and _resolve(f"site:{site}"):
            outbox.put(f"site:{site}", text + "\n<i>— seen by the FarmCamSA hub</i>", title=title,
                       silent=silent, buttons=list(site_buttons or []) + [SITE_ALWAYS] + ack,
                       ref=f"site|{ref}" if ref else None,
                       reply_ref=f"site|{reply_ref}" if reply_ref else None,
                       resolves=[f"site|{r}" for r in rs])
    except Exception as e:
        print(f"[telegram] alert not queued: {e.__class__.__name__}: {e}", flush=True)


def mirror_site(sid, entries):
    """Alerts a SITE raised (its own group already has them) → the Ops group."""
    if not entries or not _resolve("ops"):
        return
    where = _site_name(sid)
    now = time.time()
    for a in [x for x in entries if x.get("ops", True) is not False][-30:]:
        title = str(a.get("title") or "")
        ts = a.get("ts") or now
        if now - ts > MIRROR_LATE_S:      # e.g. raised while its VPN was down
            title += f" (at {time.strftime('%H:%M', time.localtime(ts))})"
        lvl = a.get("level") if a.get("level") in ICON else "info"
        outbox.put("ops", format_alert(lvl, title, a.get("body"), where),
                   title=f"{where}: {title}", silent=lvl == "info", buttons=ops_buttons(sid))


# ---- the poller's hook -----------------------------------------------------
def after_status(site, status, timeout):
    """Called by the poller after every good /api/status: push the site its
    Telegram settings when they differ, and mirror its new alerts. Never raises."""
    try:
        _sync_site(site, status, timeout)
    except Exception as e:
        print(f"[telegram] {site.get('id')}: sync failed: {e.__class__.__name__}", flush=True)
    try:
        _mirror(site, status, timeout)
    except Exception as e:
        print(f"[telegram] {site.get('id')}: mirror failed: {e.__class__.__name__}", flush=True)


def _sync_site(site, status, timeout):
    sid = site["id"]
    tg = status.get("telegram")
    if not isinstance(tg, dict) or "token_fp" not in tg:
        return                          # old site image, or our key is not accepted
    with _lock:
        e = dict(_sec()["sites"].get(sid) or {})
        hb = _hb_for(site)
    if hb:
        e["_hb"] = hb
    if isinstance(tg.get("listener"), dict):
        _listener[sid] = tg["listener"]
    want_fp = tgapi.fingerprint(e.get("token"))
    hb_ok = "hb_fp" not in tg or not hb or \
        tg.get("hb_fp") == tgapi.obj_fp([hb["topic"], hb["key"], hb["server"]])
    people_ok = "people_fp" not in tg or tg.get("people_fp") == tgapi.obj_fp(e.get("people") or {})
    want_opts = {"pictures": bool(e.get("pictures")), "capped": bool(e.get("capped"))}
    for k in ("actions", "no_digest"):
        if k in (tg.get("opts") or {}):
            want_opts[k] = bool(e.get(k))
    opts_ok = "opts" not in tg or tg.get("opts") == want_opts
    if tg.get("token_fp") == want_fp and tg.get("chat_id") == e.get("chat_id") \
            and bool(tg.get("enabled", True)) == bool(e.get("enabled", True)) and people_ok and hb_ok and opts_ok:
        return
    if time.time() - _last_sync.get(sid, 0) < SYNC_RETRY_S:
        return
    _last_sync[sid] = time.time()
    push_to_site(site, e, timeout)


def _hb_for(site):
    """This VPN site's heartbeat topic + key (made once; lock held)."""
    if not heartbeats.is_vpn_site(site):
        return None
    hb = _sec()["hb"].get(site["id"])
    if not hb:
        hb = _sec()["hb"][site["id"]] = heartbeats.new_secret()
        _save_secrets()
    return dict(hb)


def _hb_secrets():
    with _lock:
        return {sid: dict(v) for sid, v in _sec()["hb"].items()}


def _hb_send(level, title, text, sid, dark):
    """A diagnosis: always to Ops; into the site's own group only when the site
    is dark (an alive site already told its group itself)."""
    alert(level, title, text, site=sid, to_site=dark, reply_ref=f"offline:{sid}" if sid else None,
          site_buttons=[[("🌐 Internet", "internet"), ("🔗 Hub link", "hub")],
                        [("🧰 Visit brief", "visit")]] if dark else None)


_standins = {}       # sid -> hubbot.StandInBot while that site is dark
STANDIN_AFTER_S = heartbeats.DIAG_AFTER_S


def on_poll(site, reachable):
    """The poller's hook after every status poll (phase 3 diagnosis, phase 6
    stand-in for a dark site's bot). Never raises."""
    try:
        import publicip
        card = publicip.hub_card() or {}
        heartbeats.on_poll(site, reachable, hubconfig.load().get("sites", []), None,
                           card.get("live") if card else None, _hb_send)
    except Exception as e:
        print(f"[telegram] diagnosis failed: {e.__class__.__name__}: {e}", flush=True)
    try:
        _standin_check(site, reachable)
    except Exception as e:
        print(f"[telegram] stand-in failed: {e.__class__.__name__}: {e}", flush=True)


def _standin_check(site, reachable, now=None):
    now = now or time.time()
    sid = site["id"]
    bot = _standins.get(sid)
    hb = heartbeats.latest(sid, now)
    down = heartbeats._down.get(sid)
    dark = (not reachable and down is not None and now - down["since"] >= STANDIN_AFTER_S
            and not (hb and hb["fresh"]))
    if bot and (not dark or not bot.active):
        bot.active = False
        _standins.pop(sid, None)
        return
    if dark and not bot:
        target = f"site:{sid}"
        with _lock:
            e = dict(_entry(target))
        if not e.get("token") or not e.get("chat_id"):
            return
        import hubbot

        def settings():
            with _lock:
                return dict(_entry(target))
        bot = hubbot.StandInBot(site, settings, os.path.join(DATA_DIR, f"tg_standin_{sid}.json"))
        _standins[sid] = bot
        print(f"[telegram] {sid} is dark: the hub answers in its group", flush=True)
        bot.start()


def push_to_site(site, e, timeout=(5, 15), pause_until=0):
    body = {"token": e.get("token") or "", "chat_id": e.get("chat_id"),
            "chat_title": e.get("chat_title") or "", "bot": e.get("bot") or "",
            "enabled": e.get("enabled", True), "people": e.get("people") or {},
            "pictures": bool(e.get("pictures")), "capped": bool(e.get("capped")),
            "actions": bool(e.get("actions")), "no_digest": bool(e.get("no_digest")),
            "pause_until": pause_until}
    hb = e.get("_hb")
    if hb is None:
        with _lock:
            hb = _hb_for(site)
    if hb:
        body.update(hb_topic=hb["topic"], hb_key=hb["key"], hb_server=hb["server"])
    try:
        r = requests.post(siteapi.base_url(site) + "/api/telegram", json=body,
                          headers=siteapi.headers(site), timeout=timeout)
        ok = r.status_code == 200
    except requests.RequestException:
        ok = False
    print(f"[telegram] {site['id']}: settings pushed to the site: {'ok' if ok else 'failed'}",
          flush=True)
    return ok


def _mirror(site, status, timeout):
    sid = site["id"]
    mark = status.get("alerts_log")
    if not isinstance(mark, dict) or not isinstance(mark.get("seq"), int):
        return
    with _lock:
        cur = dict(_st()["mirror"].get(sid) or {})
    if not cur or not _resolve("ops"):    # first look, or no Ops group: start from now, no flood
        if cur == {"boot": mark.get("boot"), "seq": mark["seq"]}:
            return
        with _lock:
            _st()["mirror"][sid] = {"boot": mark.get("boot"), "seq": mark["seq"]}
            _write(STATE_PATH, _st(), secret=False)
        return
    if cur.get("boot") == mark.get("boot") and cur.get("seq", 0) >= mark["seq"]:
        return
    r = requests.get(siteapi.base_url(site) + "/api/alerts/recent",
                     params={"boot": cur.get("boot") or "", "after": cur.get("seq", 0)},
                     headers=siteapi.headers(site), timeout=timeout)
    if r.status_code != 200:
        return
    d = r.json()
    entries = [a for a in (d.get("alerts") or []) if isinstance(a, dict)]
    with _lock:
        _st()["mirror"][sid] = {"boot": d.get("boot"), "seq": int(d.get("seq") or 0)}
        _write(STATE_PATH, _st(), secret=False)
    mirror_site(sid, entries)


# ---- settings + pairing ----------------------------------------------------
def _public(target):
    e = _entry(target) or {}
    p = _pairing.get(target) or {}
    err = outbox.last_error.get(target)
    pairing = None
    if p:
        pairing = {"status": p["status"], "error": p.get("error", ""),
                   "chat_title": p.get("chat_title", ""),
                   "expires_in": max(0, int(p["expires"] - time.time())),
                   "code": p["code"] if p["status"] == "waiting" else ""}
    return {"configured": bool(e.get("token")), "bot": e.get("bot") or "",
            "token_fp": tgapi.fingerprint(e.get("token")),
            "paired": bool(e.get("chat_id")), "chat_title": e.get("chat_title") or "",
            "pictures": bool(e.get("pictures")), "capped": bool(e.get("capped")),
            "actions": bool(e.get("actions")), "no_digest": bool(e.get("no_digest")),
            "enabled": e.get("enabled", True), "queued": outbox.pending(target),
            "last_error": err[1] if err else "", "dead": _dead.get(target, ""),
            "pairing": pairing,
            "people": [{"id": k, **v} for k, v in sorted((e.get("people") or {}).items(),
                                                         key=lambda kv: kv[1].get("name", ""))],
            "listener": (ops_bot.public() if (target == "ops" and ops_bot) else
                         _listener.get(target[5:]) if target.startswith("site:") else None)}


def overview(site_status=None):
    sites = hubconfig.load().get("sites", [])
    with _lock:
        out = {"ops": _public("ops"), "sites": []}
        for s in sites:
            row = {"id": s["id"], "name": s.get("name") or s["id"], **_public(f"site:{s['id']}")}
            st = (site_status or {}).get(s["id"])
            if isinstance(st, dict):
                tg = st.get("telegram")
                row["site_supports"] = isinstance(tg, dict)
                row["site_in_sync"] = (isinstance(tg, dict) and tg.get("token_fp") == row["token_fp"])
                if isinstance(tg, dict):
                    row["site_last_error"] = tg.get("last_error") or tg.get("dead") or ""
            out["sites"].append(row)
        # entries made by _entry() for unknown ids are not kept
        return out


def set_token(target, token):
    token = (token or "").strip()
    if token and not tgapi.valid_token(token):
        return False, "That is not a bot token. BotFather gives one like 123456789:AA…"
    bot = ""
    if token:
        me = tgapi.get_me(token)
        if not me.ok:
            return False, f"Telegram refused the token: {me.error}"
        bot = (me.data or {}).get("username") or ""
    with _lock:
        e = _entry(target)
        old = e.get("token")
        if not token:
            e.clear()
        else:
            if token != old:
                e.pop("chat_id", None)
                e.pop("chat_title", None)
            e["token"], e["bot"] = token, bot
            e.setdefault("enabled", True)
        if target.startswith("site:") and not token:
            _sec()["sites"].pop(target[5:], None)
        _save_secrets()
    _pairing.pop(target, None)
    _dead.pop(target, None)
    if not token or token != old:
        outbox.drop_dest(target)
    if target.startswith("site:"):
        _last_sync.pop(target[5:], None)
    return True, bot


def set_option(target, key, on):
    """Per-site switches: `pictures` (camera pictures in the group — POPIA, off by
    default) and `capped` (data-capped line: no speed tests)."""
    if key not in ("pictures", "capped", "actions", "no_digest") or not target.startswith("site:"):
        return False
    with _lock:
        e = _entry(target)
        if not e.get("token"):
            return False
        e[key] = bool(on)
        _save_secrets()
    _last_sync.pop(target[5:], None)
    return True


def set_person(target, uid, name, role):
    uid = str(uid).strip()
    if not uid.lstrip("-").isdigit() or len(uid) > 15:
        return False, "A Telegram user id is a number."
    if role not in tgbotcore.ROLES:
        return False, "Role must be viewer, operator or admin."
    with _lock:
        e = _entry(target)
        if not e.get("token"):
            return False, "Save the bot token first."
        ppl = dict(e.get("people") or {})
        ppl[uid] = {"name": str(name or "")[:64], "role": role}
        e["people"] = ppl
        _save_secrets()
    if target.startswith("site:"):
        _last_sync.pop(target[5:], None)
    if target == "ops" and ops_bot:
        ops_bot.requests.pop(uid, None)
    return True, ""


def remove_person(target, uid):
    with _lock:
        e = _entry(target)
        ppl = dict(e.get("people") or {})
        ppl.pop(str(uid), None)
        e["people"] = ppl
        _save_secrets()
    if target.startswith("site:"):
        _last_sync.pop(target[5:], None)


def set_enabled(target, on):
    with _lock:
        e = _entry(target)
        if not e.get("token"):
            return False
        e["enabled"] = bool(on)
        _save_secrets()
    if target.startswith("site:"):
        _last_sync.pop(target[5:], None)
    return True


def start_pairing(target):
    with _lock:
        e = _entry(target)
        token = e.get("token")
    if not token:
        return None, "Save the bot token first."
    code = f"{secrets.randbelow(10**4):04d}-{secrets.randbelow(10**4):04d}"
    p = {"code": code, "expires": time.time() + PAIR_S, "status": "waiting", "wrong": 0,
         "id": secrets.token_hex(4)}
    _pairing[target] = p
    if target.startswith("site:"):
        sid = target[5:]
        site = next((s for s in hubconfig.load().get("sites", []) if s.get("id") == sid), None)
        if site:                      # the site polls its own bot: ask it to stand aside
            with _lock:
                snapshot = dict(_entry(target))
            push_to_site(site, snapshot, pause_until=p["expires"])
    threading.Thread(target=_pair_worker, args=(target, token, p), daemon=True,
                     name=f"tgpair-{target}").start()
    return code, ""


def _pair_worker(target, token, p):
    offset = None
    conflicts = 0
    while _pairing.get(target) is p and p["status"] == "waiting":
        if time.time() > p["expires"]:
            p["status"], p["error"] = "expired", "No /pair message arrived in 10 minutes."
            _unpause(target)
            return
        res = tgapi.get_updates(token, offset, timeout=20,
                                allowed=("message",))
        if not res.ok:
            if res.code == 409 and conflicts < PAIR_409_TRIES:
                conflicts += 1            # the site's last long-poll is still open: wait it out
                time.sleep(PAIR_409_WAIT_S)
                continue
            if res.code == 409:
                p["status"] = "failed"
                p["error"] = ("Another program is reading this bot's messages "
                              "(a webhook, or an older Netwatch still polls it).")
                _unpause(target)
                return
            time.sleep(5)
            continue
        for u in res.data or []:
            offset = max(offset or 0, int(u.get("update_id", 0)) + 1)
            msg = u.get("message") or {}
            chat = msg.get("chat") or {}
            m = PAIR_RE.match((msg.get("text") or "").strip())
            if not m:
                continue
            if chat.get("type") not in ("group", "supergroup"):
                tgapi.send(token, chat.get("id"), "Pair me in the group, not in a private chat.",
                           html_mode=False)
                continue
            if not hmac.compare_digest(m.group(1) + "-" + m.group(2), p["code"]):
                p["wrong"] += 1
                tgapi.send(token, chat["id"], "That code is wrong.", html_mode=False)
                if p["wrong"] >= PAIR_MAX_WRONG:
                    p["status"], p["error"] = "failed", "Too many wrong codes; start again."
                    tgapi.call(token, "getUpdates", {"offset": offset, "timeout": 0})
                    _unpause(target)
                    return
                continue
            title = str(chat.get("title") or "")[:128]
            with _lock:
                e = _entry(target)
                e["chat_id"], e["chat_title"] = chat["id"], title
                e.setdefault("enabled", True)
                _save_secrets()
            _dead.pop(target, None)
            where = "the FarmCamSA Ops group" if target == "ops" else _site_name(target[5:])
            tgapi.send(token, chat["id"],
                       f"✅ Paired with <b>{tgapi.esc(where)}</b>. Alerts will arrive here.")
            tgapi.call(token, "getUpdates", {"offset": offset, "timeout": 0})   # confirm: never seen again
            p["status"], p["chat_title"] = "paired", title
            if target.startswith("site:"):
                sid = target[5:]
                _last_sync.pop(sid, None)
                site = next((s for s in hubconfig.load().get("sites", []) if s.get("id") == sid), None)
                if site:
                    with _lock:
                        snapshot = dict(_entry(target))
                    push_to_site(site, snapshot)
            return


def _unpause(target):
    """Let a site's bot poll again after pairing ended without a new group."""
    if not target.startswith("site:"):
        return
    sid = target[5:]
    site = next((s for s in hubconfig.load().get("sites", []) if s.get("id") == sid), None)
    if site:
        with _lock:
            snapshot = dict(_entry(target))
        push_to_site(site, snapshot)


def unpair(target):
    with _lock:
        e = _entry(target)
        e.pop("chat_id", None)
        e.pop("chat_title", None)
        _save_secrets()
    outbox.drop_dest(target)
    _pairing.pop(target, None)
    if target.startswith("site:"):
        _last_sync.pop(target[5:], None)


def send_test(target):
    t = _resolve(target)
    if not t:
        return False, "No token or no paired group."
    where = "FarmCamSA Ops" if target == "ops" else _site_name(target[5:])
    res = tgapi.send(t[0], t[1], f"🔔 <b>{tgapi.esc(where)}</b> · test message from the hub")
    return res.ok, res.error


# ---- HTTP (the whole hub API sits behind the hub login) --------------------
bp = Blueprint("telegram", __name__)


def _target(kind, sid=None):
    if kind == "ops":
        return "ops"
    if any(s.get("id") == sid for s in hubconfig.load().get("sites", [])):
        return f"site:{sid}"
    return None


@bp.route("/api/hub/telegram")
def api_overview():
    from poller import poller
    st = {}
    for s in hubconfig.load().get("sites", []):
        snap = poller.snapshot(s["id"]) or {}
        st[s["id"]] = snap.get("status")
    return jsonify(overview(st))


def _routes(kind):
    def handler(sid=None, action=None):
        target = _target(kind, sid)
        if not target:
            return jsonify({"ok": False, "error": "unknown site"}), 404
        body = request.get_json(force=True, silent=True) or {}
        if action is None and request.method == "DELETE":
            set_token(target, "")
            return jsonify({"ok": True})
        if action is None:
            if "token" in body:
                ok, res = set_token(target, body.get("token"))
                if not ok:
                    return jsonify({"ok": False, "error": res}), 400
            if "enabled" in body and not set_enabled(target, body["enabled"]):
                return jsonify({"ok": False, "error": "Save the bot token first."}), 400
            for k in ("pictures", "capped", "actions", "no_digest"):
                if k in body and not set_option(target, k, body[k]):
                    return jsonify({"ok": False, "error": "Save the bot token first."}), 400
            if isinstance(body.get("person"), dict):
                pp = body["person"]
                ok, err = set_person(target, pp.get("id"), pp.get("name"), pp.get("role"))
                if not ok:
                    return jsonify({"ok": False, "error": err}), 400
            if body.get("remove_person") not in (None, ""):
                remove_person(target, body["remove_person"])
            with _lock:
                return jsonify({"ok": True, **_public(target)})
        if action == "pair":
            code, err = start_pairing(target)
            if not code:
                return jsonify({"ok": False, "error": err}), 400
            return jsonify({"ok": True, "code": code, "expires_in": PAIR_S})
        if action == "unpair":
            unpair(target)
            return jsonify({"ok": True})
        if action == "test":
            ok, err = send_test(target)
            return jsonify({"ok": ok, "error": err}), (200 if ok else 400)
        return jsonify({"ok": False, "error": "unknown action"}), 404
    return handler


_ops = _routes("ops")
_site = _routes("site")
bp.add_url_rule("/api/hub/telegram/ops", "tg_ops", _ops, methods=["POST", "DELETE"])
bp.add_url_rule("/api/hub/telegram/ops/<action>", "tg_ops_act", _ops, methods=["POST"])
bp.add_url_rule("/api/hub/telegram/sites/<sid>", "tg_site", _site, methods=["POST", "DELETE"])
bp.add_url_rule("/api/hub/telegram/sites/<sid>/<action>", "tg_site_act", _site, methods=["POST"])
