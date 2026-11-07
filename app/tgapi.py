"""The one door to the Telegram Bot API (site and hub keep IDENTICAL copies:
app/tgapi.py == hub/app/tgapi.py, checked by tests/test_tg_same.py).

Plain `requests`, no bot framework: one HTTPS call per method, so it costs a
1 GB farm Pi nothing.

Rules this file enforces, learnt the hard way elsewhere:
  * Telegram answers HTTP 200 with {"ok": false} for a refused message. ONLY
    `ok: true` counts as sent (the WorkSync sendTelegramPhoto trap).
  * 429 carries `retry_after`; a group upgraded to a supergroup answers 400
    with `migrate_to_chat_id`. Both come back in the Result so the outbox can
    act on them.
  * The bot token is part of the URL, and requests puts the URL into its
    exception text. No error string from here ever contains the token.

TELEGRAM_API (env) points the tests at a fake server; never at the real API.
"""
import hashlib
import html
import json
import os
import re

import requests

API = os.environ.get("TELEGRAM_API", "https://api.telegram.org").rstrip("/")
MAX_TEXT = 4096
TOKEN_RE = re.compile(r"^\d{5,16}:[A-Za-z0-9_-]{30,64}$")

_session = requests.Session()        # keep-alive: one TLS handshake, not one per call


class Result:
    __slots__ = ("ok", "data", "error", "code", "retry_after", "migrate_to")

    def __init__(self, ok, data=None, error="", code=None, retry_after=None, migrate_to=None):
        self.ok, self.data, self.error, self.code = ok, data, error, code
        self.retry_after, self.migrate_to = retry_after, migrate_to

    def __repr__(self):
        return f"<tg {'ok' if self.ok else 'fail'} {self.code} {self.error!r}>"


def valid_token(token):
    return bool(token) and bool(TOKEN_RE.match(token))


def fingerprint(token):
    """Short, one-way tag of a token: lets hub and site compare tokens
    without ever sending one in a status reply."""
    return hashlib.sha256((token or "").encode()).hexdigest()[:12] if token else ""


def obj_fp(obj):
    """Short fingerprint of a JSON-able value (hub and site compare people lists)."""
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]


def esc(value):
    """Escape any value for parse_mode=HTML. EVERY value from a site or a
    device goes through this (a device name is hostile data)."""
    return html.escape("" if value is None else str(value), quote=False)


def clip(text, limit=MAX_TEXT - 200):
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _scrub(text, token):
    text = str(text)
    if token:
        text = text.replace(token, "<token>")
    return re.sub(r"bot\d{5,16}:[A-Za-z0-9_-]+", "bot<token>", text)[:200]


def call(token, method, params=None, timeout=15):
    """POST one Bot API method. Never raises."""
    if not valid_token(token):
        return Result(False, error="no valid bot token")
    try:
        r = _session.post(f"{API}/bot{token}/{method}", json=params or {}, timeout=timeout)
    except requests.RequestException as e:
        return Result(False, error=_scrub(f"{e.__class__.__name__}: {e}", token))
    return _parse(r, token)


def _parse(r, token):
    try:
        body = r.json()
    except ValueError:
        return Result(False, error=f"HTTP {r.status_code}, not JSON", code=r.status_code)
    if not isinstance(body, dict):
        return Result(False, error="odd reply", code=r.status_code)
    if body.get("ok") is True:
        return Result(True, data=body.get("result"), code=r.status_code)
    p = body.get("parameters") if isinstance(body.get("parameters"), dict) else {}
    return Result(False, error=_scrub(body.get("description") or f"HTTP {r.status_code}", token),
                  code=body.get("error_code") or r.status_code,
                  retry_after=p.get("retry_after"), migrate_to=p.get("migrate_to_chat_id"))


def send_photo(token, chat_id, jpeg, caption="", *, reply_to=None, silent=False):
    """A JPEG (bytes) with an HTML caption (max 1024 characters)."""
    if not valid_token(token):
        return Result(False, error="no valid bot token")
    data = {"chat_id": str(chat_id), "caption": clip(caption, 1000), "parse_mode": "HTML",
            "disable_notification": "true" if silent else "false"}
    if reply_to:
        data["reply_parameters"] = json.dumps({"message_id": reply_to, "allow_sending_without_reply": True})
    try:
        r = _session.post(f"{API}/bot{token}/sendPhoto", data=data,
                          files={"photo": ("picture.jpg", jpeg, "image/jpeg")}, timeout=40)
    except requests.RequestException as e:
        return Result(False, error=_scrub(f"{e.__class__.__name__}: {e}", token))
    return _parse(r, token)


def get_me(token):
    return call(token, "getMe", timeout=10)


def send(token, chat_id, text, *, reply_to=None, silent=False, html_mode=True, buttons=None):
    params = {"chat_id": chat_id, "text": clip(text), "disable_web_page_preview": True,
              "disable_notification": bool(silent)}
    if html_mode:
        params["parse_mode"] = "HTML"
    if reply_to:
        params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    if buttons:
        params["reply_markup"] = {"inline_keyboard": buttons}
    return call(token, "sendMessage", params)


def keyboard(rows):
    """Rows of (label, data) → an inline keyboard. data goes back as the
    callback "c:<data>" (tgbotcore runs it as /<command> [<arg>])."""
    return [[{"text": str(label)[:40], "callback_data": ("c:" + str(data))[:64]} for label, data in row]
            for row in rows if row] if rows else None


def edit(token, chat_id, message_id, text, *, html_mode=True, buttons=None):
    params = {"chat_id": chat_id, "message_id": message_id, "text": clip(text),
              "disable_web_page_preview": True}
    if html_mode:
        params["parse_mode"] = "HTML"
    if buttons:                         # without it Telegram drops the keyboard
        params["reply_markup"] = {"inline_keyboard": buttons}
    res = call(token, "editMessageText", params)
    if not res.ok and "not modified" in res.error:
        return Result(True, data=None, code=res.code)      # same text: nothing to do
    return res


def get_updates(token, offset=None, timeout=20, allowed=("message", "callback_query", "my_chat_member")):
    params = {"timeout": timeout, "allowed_updates": list(allowed)}
    if offset is not None:
        params["offset"] = offset
    return call(token, "getUpdates", params, timeout=timeout + 10)


def strip_tags(text):
    """Plain-text fallback when Telegram refuses our HTML."""
    return html.unescape(re.sub(r"<[^>]+>", "", text or ""))
