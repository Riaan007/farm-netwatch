"""Store-and-forward queue for Telegram messages (site and hub keep IDENTICAL
copies: app/tgoutbox.py == hub/app/tgoutbox.py, checked by tests/test_tg_same.py).

Why a queue: a farm link drops, Telegram rate-limits, ServerPI's internet
goes away. An alert must not be lost, and must not hold up the code that
raised it. `put()` only appends; one worker thread sends.

  * Retries with backoff (10 s → 5 min). 429 waits exactly `retry_after`.
  * `ok:false` is a failure (tgapi enforces it).
  * A message refused for bad HTML is sent again as plain text, once.
  * 403 / "chat not found" = the bot was removed: the item is dropped and
    `on_dead(dest, why)` is told, so the settings page can say so.
  * Group → supergroup: `on_migrate(dest, new_chat_id)`, then sent again.
  * After an outage, messages older than FOLD_AFTER_S for the same chat are
    folded into ONE summary instead of 40 stale alerts.
  * At most ~19 messages a minute per chat (Telegram allows about 20 per group).
  * The file on disk is written only when the queue or the message map
    changes (SD cards on farm Pis).

Destinations are names ("site", "ops", "site:bennie"); `resolve(dest)` turns
one into (token, chat_id), or None when it is not configured (item dropped).

`ref` names a message so a later one can reply to it, and `resolves=[refs]`
edits those earlier messages with a "Resolved" line — an incident's first
message shows its outcome instead of a scroll of up/down lines.
"""
import json
import os
import threading
import time

import tgapi

FOLD_AFTER_S = 1800
EXPIRE_S = 24 * 3600
MAX_ITEMS = 300
MAX_REFS = 400
PER_CHAT_GAP_S = 3.2
BACKOFF = (10, 30, 60, 120, 300)
DEAD_CODES = (403,)


def _fmt_span(seconds):
    m = int(seconds // 60)
    if m < 60:
        return f"{max(1, m)} min"
    h, m = divmod(m, 60)
    return f"{h} h {m} min" if h < 48 else f"{h // 24} days"


class Outbox:
    def __init__(self, path, resolve, on_dead=None, on_migrate=None, on_sent=None,
                 clock=time.time, tz_fmt=None):
        self.path = path
        self.resolve = resolve
        self.on_dead = on_dead or (lambda dest, why: None)
        self.on_migrate = on_migrate or (lambda dest, chat: None)
        self.on_sent = on_sent or (lambda dest: None)
        self.clock = clock
        self.tz_fmt = tz_fmt or (lambda ts: time.strftime("%H:%M", time.localtime(ts)))
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._items = []
        self._refs = {}           # ref -> {"dest", "mid", "text", "ts"}
        self._last_sent = {}      # dest -> ts
        self._seq = 0
        self.last_error = {}      # dest -> (ts, text)
        self._load()

    # ---- persistence -------------------------------------------------------
    def _load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            self._items = [i for i in d.get("items", []) if isinstance(i, dict)][-MAX_ITEMS:]
            self._refs = d.get("refs", {}) if isinstance(d.get("refs"), dict) else {}
            self._seq = max([int(i.get("id", 0)) for i in self._items] or [0])
        except (OSError, ValueError, TypeError):
            pass

    def _save(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({"items": self._items, "refs": self._refs}, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError as e:
            print(f"[tgoutbox] cannot save {self.path}: {e}", flush=True)

    # ---- public ------------------------------------------------------------
    def put(self, dest, text, *, title="", ref=None, reply_ref=None, resolves=None,
            silent=False, buttons=None):
        """Queue one message. `title` is the plain-text line used if it is ever
        folded into a summary; `buttons` = rows of (label, data), see
        tgapi.keyboard. Never raises, never blocks on the network."""
        now = self.clock()
        with self._lock:
            self._seq += 1
            self._items.append({"id": self._seq, "dest": dest, "text": text,
                                "title": (title or "")[:120], "ref": ref,
                                "reply_ref": reply_ref, "resolves": list(resolves or []),
                                "silent": bool(silent), "ts": now, "tries": 0,
                                "next": now, "plain": False,
                                "buttons": tgapi.keyboard(buttons)})
            if len(self._items) > MAX_ITEMS:
                self._items = self._items[-MAX_ITEMS:]
            self._save()
        self._wake.set()

    def pending(self, dest=None):
        with self._lock:
            return sum(1 for i in self._items if dest is None or i["dest"] == dest)

    def drop_dest(self, dest):
        with self._lock:
            self._items = [i for i in self._items if i["dest"] != dest]
            self._refs = {k: v for k, v in self._refs.items() if v.get("dest") != dest}
            self._save()

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="tgoutbox").start()

    # ---- worker ------------------------------------------------------------
    def _loop(self):
        while True:
            try:
                wait = self.run_once()
            except Exception as e:              # never let the thread die
                print(f"[tgoutbox] {e.__class__.__name__}: {e}", flush=True)
                wait = 30
            self._wake.wait(wait)
            self._wake.clear()

    def _fold(self, now):
        """Fold stale messages per chat into one summary item (lock held)."""
        changed = False
        for item in [i for i in self._items if now - i["ts"] > EXPIRE_S]:
            self._items.remove(item)
            changed = True
        by_dest = {}
        for i in self._items:
            if now - i["ts"] > FOLD_AFTER_S and not i.get("folded"):
                by_dest.setdefault(i["dest"], []).append(i)
        for dest, old in by_dest.items():
            if len(old) < 2:
                continue
            first, last = old[0]["ts"], old[-1]["ts"]
            lines = [f"• {tgapi.esc(self.tz_fmt(i['ts']))} {tgapi.esc(i['title'] or tgapi.strip_tags(i['text']).splitlines()[0])}"
                     for i in old[:40]]
            more = f"\n… and {len(old) - 40} more" if len(old) > 40 else ""
            text = (f"🕓 <b>Held back while Telegram could not be reached</b> "
                    f"({tgapi.esc(self.tz_fmt(first))}–{tgapi.esc(self.tz_fmt(last))}, "
                    f"{_fmt_span(last - first)}):\n" + "\n".join(lines) + more)
            ids = {i["id"] for i in old}
            pos = self._items.index(old[0])
            self._items = [i for i in self._items if i["id"] not in ids]
            self._seq += 1
            self._items.insert(pos, {"id": self._seq, "dest": dest, "text": text,
                                     "title": "Held-back alerts", "ref": None,
                                     "reply_ref": None, "resolves": [], "silent": False,
                                     "ts": now, "tries": 0, "next": now, "plain": False,
                                     "folded": True})
            changed = True
        return changed

    def run_once(self):
        """Send whatever is due. Returns seconds until the next look."""
        now = self.clock()
        with self._lock:
            changed = self._fold(now)
            due = None
            for i in self._items:
                if i["next"] <= now and now - self._last_sent.get(i["dest"], 0) >= PER_CHAT_GAP_S:
                    due = dict(i)
                    break
            if changed:
                self._save()
        if due is None:
            with self._lock:
                if not self._items:
                    return 60
                soonest = min(max(i["next"], self._last_sent.get(i["dest"], 0) + PER_CHAT_GAP_S)
                              for i in self._items)
            return max(0.5, min(60, soonest - now))
        self._send(due)
        return 0.1

    def _finish(self, item_id, ref_entry=None, ref=None):
        with self._lock:
            self._items = [i for i in self._items if i["id"] != item_id]
            if ref and ref_entry:
                self._refs[ref] = ref_entry
                if len(self._refs) > MAX_REFS:
                    for k in sorted(self._refs, key=lambda k: self._refs[k].get("ts", 0))[:len(self._refs) - MAX_REFS]:
                        del self._refs[k]
            self._save()

    def _retry(self, item, res):
        with self._lock:
            for i in self._items:
                if i["id"] == item["id"]:
                    i["tries"] += 1
                    if res.retry_after:
                        i["next"] = self.clock() + float(res.retry_after) + 1
                    else:
                        i["next"] = self.clock() + BACKOFF[min(i["tries"] - 1, len(BACKOFF) - 1)]
                    break
            self.last_error[item["dest"]] = (self.clock(), res.error)

    def _send(self, item):
        dest = item["dest"]
        target = self.resolve(dest)
        if not target:
            self._finish(item["id"])                # not configured (any more): drop
            return
        token, chat = target
        reply_to = None
        if item.get("reply_ref"):
            r = self._refs.get(item["reply_ref"])
            if r and r.get("dest") == dest:
                reply_to = r.get("mid")
        self._last_sent[dest] = self.clock()
        res = tgapi.send(token, chat, item["text"], reply_to=reply_to, silent=item["silent"],
                         html_mode=not item["plain"], buttons=item.get("buttons"))
        if res.ok:
            self.last_error.pop(dest, None)
            mid = (res.data or {}).get("message_id") if isinstance(res.data, dict) else None
            entry = {"dest": dest, "mid": mid, "text": item["text"], "ts": item["ts"],
                     "buttons": item.get("buttons")} if mid else None
            self._finish(item["id"], entry, item.get("ref"))
            self.on_sent(dest)
            for ref in item.get("resolves") or []:
                self._resolve_ref(ref, token, chat, dest)
            return
        if res.migrate_to:
            self.on_migrate(dest, res.migrate_to)
            with self._lock:
                for i in self._items:
                    if i["id"] == item["id"]:
                        i["next"] = self.clock()
            return
        if res.code in DEAD_CODES or "chat not found" in res.error.lower():
            self.last_error[dest] = (self.clock(), res.error)
            self.on_dead(dest, res.error)
            self.drop_dest(dest)
            return
        if res.code == 400 and "parse" in res.error.lower() and not item["plain"]:
            with self._lock:
                for i in self._items:
                    if i["id"] == item["id"]:
                        i["plain"] = True
                        i["text"] = tgapi.strip_tags(i["text"])
            return
        if res.code == 400:                         # refused for good: don't loop on it
            self.last_error[dest] = (self.clock(), res.error)
            self._finish(item["id"])
            return
        self._retry(item, res)

    def _resolve_ref(self, ref, token, chat, dest):
        entry = self._refs.get(ref)
        if not entry or entry.get("dest") != dest or not entry.get("mid"):
            return
        now = self.clock()
        line = (f"✅ <i>Resolved {tgapi.esc(self.tz_fmt(now))} "
                f"(after {_fmt_span(now - entry.get('ts', now))})</i>\n")
        tgapi.edit(token, chat, entry["mid"], line + entry.get("text", ""), buttons=entry.get("buttons"))
        with self._lock:
            self._refs.pop(ref, None)
            self._save()
