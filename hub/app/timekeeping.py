"""Time keeping: where the operator's working hours go, per farm, ready to bill.

One owner (the hub has one login). Five kinds of time:
  remote  — working on a farm from afar: its Control Center pages (a heartbeat
            the CC sends while you are actively using it), its own Netwatch page
            through the hub proxy (Caddy access log, background polls skipped), a
            device/SSH tunnel to it (tunnels.manager activity).
  onsite  — at the farm: the Arrive/Leave buttons, or the operator's phone seen
            on the farm's network by the site's own scan.
  travel  — driving to/from a farm: the buttons; km typed, or prefilled from the
            farm's usual one-way distance.
  dev     — building Netwatch itself, read from Claude Code sessions on the host
            (hub/tools/devtime.py pipes minute stamps into `import-dev` below).
  general — hub time not tied to one farm, once assigned so in review. Never billed.

Activity is one row per (minute, source, site) in `time_activity`. A builder turns
it into DRAFT entries (`time_entries`, source auto/wifi/claude) for recent days; the
operator reviews, edits, assigns and approves them. Anything approved, edited,
dismissed, typed or made with a button is FROZEN: the builder never changes it and
never lays a new draft over its time.

Billing (timesheets) counts approved entries only, per farm per month. Per day and
kind the minutes are rounded UP to `round_min`; each on-site visit counts at least
the farm's call-out minimum. Rates are per farm.
"""
import datetime as dt
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time

DATA_DIR = os.environ.get("HUB_DATA", "/data")
DB_PATH = os.path.join(DATA_DIR, "hub.db")
CADDY_LOG = os.path.join(DATA_DIR, "caddy", "logs", "access.log")
BACKUP_DIR = os.path.join(DATA_DIR, "time", "backups")

KINDS = ("remote", "onsite", "travel", "dev", "general")
REMOTE_SOURCES = ("cc", "proxy", "tunnel")
AUTO_SOURCES = ("auto", "wifi", "claude")          # builder-made; frozen once touched
# Frozen entries block the builder within their group: remote work can't be laid
# over approved remote/general time, field time over field time, dev over dev.
GROUP = {"remote": "remote", "general": "remote", "onsite": "field", "travel": "field", "dev": "dev"}
REBUILD_DAYS = 35                 # drafts are (re)built for this many days back
ACTIVITY_KEEP_DAYS = 120
BACKUP_KEEP = 60

# The site page polls these on a timer whether anyone looks or not, so a request
# to them is not a sign of work (Caddy skips logging them too — proxycfg.py).
PROXY_BACKGROUND = ("/api/status", "/api/sysinfo", "/api/hub/status", "/api/internet",
                    "/api/radio/overview", "/api/wifi/plan", "/api/radio/links",
                    "/api/devices", "/api/topology", "/api/problems", "/api/conflicts",
                    "/api/config", "/api/kuma/unowned", "/api/dhcp-guard", "/favicon.ico")

DEFAULT_GLOBAL = {
    "round_min": 15,           # billable minutes per day per kind round UP to this
    "idle_gap_min": 5,         # remote: a pause up to this long stays one session
    "wifi_gap_min": 45,        # phone on farm Wi-Fi: scans are 5-15 min apart
    "dev_gap_min": 10,         # Claude sessions on Netwatch
    "min_session_min": 2,      # shorter remote/dev blips are not shown
    "business": "FarmCamSA",
    "currency": "R",
    "dev_rate": 0,             # optional: values dev time for your own records
}
DEFAULT_FARM = {
    "rate_remote": 0, "rate_onsite": 0, "rate_travel": 0, "rate_km": 0,
    "callout_min": 60,         # each on-site visit bills at least this many minutes
    "km_one_way": 0,           # prefills a travel line
    "phone_key": "", "phone_label": "",
    "billable": True,
}
_NUM_FIELDS = {"rate_remote", "rate_onsite", "rate_travel", "rate_km", "callout_min", "km_one_way",
               "round_min", "idle_gap_min", "wifi_gap_min", "dev_gap_min", "min_session_min", "dev_rate"}
_LIMITS = {"round_min": (1, 60), "idle_gap_min": (1, 60), "wifi_gap_min": (5, 240),
           "dev_gap_min": (1, 60), "min_session_min": (0, 30), "callout_min": (0, 480),
           "km_one_way": (0, 2000)}

_local = threading.local()
_lock = threading.Lock()          # serialises builder runs and clock actions


class TimeError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---- storage ---------------------------------------------------------------------
def _conn():
    c = getattr(_local, "conn", None)
    if c is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=10)
        c.execute("PRAGMA journal_mode=WAL")
        c.row_factory = sqlite3.Row
        _local.conn = c
        _init(c)
    return c


def _init(c):
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS time_activity (
            minute   INTEGER NOT NULL,
            source   TEXT NOT NULL,
            site_id  TEXT NOT NULL DEFAULT '',
            last_ts  INTEGER NOT NULL,
            PRIMARY KEY (minute, source, site_id)
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS time_entries (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            kind      TEXT NOT NULL,
            site_id   TEXT NOT NULL DEFAULT '',
            start_ts  INTEGER NOT NULL,
            end_ts    INTEGER,
            km        REAL,
            note      TEXT NOT NULL DEFAULT '',
            source    TEXT NOT NULL,
            sources   TEXT NOT NULL DEFAULT '',
            status    TEXT NOT NULL DEFAULT 'draft',
            edited    INTEGER NOT NULL DEFAULT 0,
            created_ts INTEGER NOT NULL,
            updated_ts INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_time_entries_start ON time_entries(start_ts);
        CREATE TABLE IF NOT EXISTS time_settings (k TEXT PRIMARY KEY, v TEXT NOT NULL);
        """
    )
    c.commit()


def minute_of(ts):
    return int(ts) // 60 * 60


def mark(source, site_id="", ts=None):
    """Record that work of this source touched this minute (idempotent)."""
    ts = int(ts if ts is not None else time.time())
    c = _conn()
    c.execute("INSERT INTO time_activity (minute, source, site_id, last_ts) VALUES (?,?,?,?)"
              " ON CONFLICT(minute, source, site_id) DO UPDATE SET last_ts = max(last_ts, excluded.last_ts)",
              (minute_of(ts), source, site_id or "", ts))
    c.commit()


def mark_many(source, items):
    """items: iterable of (site_id, ts)."""
    c = _conn()
    c.executemany("INSERT INTO time_activity (minute, source, site_id, last_ts) VALUES (?,?,?,?)"
                  " ON CONFLICT(minute, source, site_id) DO UPDATE SET last_ts = max(last_ts, excluded.last_ts)",
                  [(minute_of(ts), source, sid or "", int(ts)) for sid, ts in items])
    c.commit()


# ---- settings ----------------------------------------------------------------------
def _num(k, v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise TimeError(f"{k} must be a number")
    if not math.isfinite(x) or x < 0:
        raise TimeError(f"{k} can't be negative")
    lo, hi = _LIMITS.get(k, (0, 1e7))
    if not lo <= x <= hi:
        raise TimeError(f"{k} must be between {lo:g} and {hi:g}")
    return int(x) if k.endswith("_min") else round(x, 2)


def settings():
    row = _conn().execute("SELECT v FROM time_settings WHERE k='settings'").fetchone()
    raw = json.loads(row["v"]) if row else {}
    g = {**DEFAULT_GLOBAL, **(raw.get("global") or {})}
    farms = {}
    for sid, f in (raw.get("farms") or {}).items():
        farms[sid] = {**DEFAULT_FARM, **f}
    return {"global": g, "farms": farms}


def farm_settings(site_id, st=None):
    st = st or settings()
    f = st["farms"].get(site_id)
    if f:
        return f
    # The hub's own home site is the operator's office, not a client.
    return {**DEFAULT_FARM, "billable": site_id != "home"}


def save_settings(body):
    """Merge `{global:{…}, farms:{site:{…}}}` into the stored settings (validated)."""
    if not isinstance(body, dict):
        raise TimeError("send a JSON object")
    cur = settings()
    g = dict(cur["global"])
    for k, v in (body.get("global") or {}).items():
        if k not in DEFAULT_GLOBAL:
            continue
        if k in _NUM_FIELDS:
            g[k] = _num(k, v)
        else:
            g[k] = str(v or "").strip()[:60] or DEFAULT_GLOBAL[k]
    farms = dict(cur["farms"])
    for sid, f in (body.get("farms") or {}).items():
        if not isinstance(f, dict) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(sid)):
            raise TimeError("bad farm id")
        nf = dict(farms.get(sid) or farm_settings(sid, cur))
        for k, v in f.items():
            if k not in DEFAULT_FARM:
                continue
            if k in _NUM_FIELDS:
                nf[k] = _num(k, v)
            elif k == "billable":
                nf[k] = bool(v)
            else:
                nf[k] = str(v or "").strip()[:200]
        farms[sid] = nf
    c = _conn()
    c.execute("INSERT INTO time_settings (k, v) VALUES ('settings', ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
              (json.dumps({"global": g, "farms": farms}),))
    c.commit()
    return settings()


# ---- days ----------------------------------------------------------------------------
def day_bounds(day):
    """Local-time [start, end) epoch seconds of a 'YYYY-MM-DD' day (TZ from the env)."""
    try:
        d = dt.date.fromisoformat(str(day))
    except ValueError:
        raise TimeError("date must be YYYY-MM-DD")
    a = dt.datetime.combine(d, dt.time()).timestamp()
    b = dt.datetime.combine(d + dt.timedelta(days=1), dt.time()).timestamp()
    return int(a), int(b)


def day_of(ts):
    return dt.datetime.fromtimestamp(ts).date().isoformat()


def today():
    return dt.date.today().isoformat()


def month_bounds(month):
    m = re.fullmatch(r"(\d{4})-(\d{2})", str(month or ""))
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise TimeError("month must be YYYY-MM")
    y, mo = int(m.group(1)), int(m.group(2))
    a = dt.datetime(y, mo, 1).timestamp()
    b = dt.datetime(y + (mo == 12), mo % 12 + 1, 1).timestamp()
    return int(a), int(b)


# ---- interval helpers --------------------------------------------------------------
def _subtract(a, b, holes):
    """Pieces of [a, b) not covered by any (x, y) in holes."""
    out, cur = [], a
    for x, y in sorted(holes):
        if y <= cur or x >= b:
            continue
        if x > cur:
            out.append((cur, min(x, b)))
        cur = max(cur, y)
        if cur >= b:
            break
    if cur < b:
        out.append((cur, b))
    return out


def _covered(m, intervals):
    return any(x <= m < y for x, y in intervals)


def sessions_of(minutes, gap_s):
    """Sorted minute stamps -> [[start, end)]: a pause up to gap_s joins; a session
    ends one minute after its last stamp."""
    out = []
    for m in sorted(set(minutes)):
        if out and m - out[-1][1] <= gap_s:
            out[-1][1] = m + 60
        else:
            out.append([m, m + 60])
    return out


def remote_timeline(rows, gap_s, exclude=None):
    """Remote activity rows (minute, source, site_id, last_ts) -> sessions
    [{site, start, end, sources}] that never overlap.

    Each minute goes to ONE place: a farm beats the hub's all-farm pages; between
    farms the latest activity in that minute wins. An all-farm minute sitting
    between two minutes of the same farm (each within the gap) is a glance at the
    overview during that farm's work and stays with the farm. `exclude(site, minute)`
    drops minutes already billed another way (on site at that farm)."""
    per = {}
    srcs = {}
    for r in rows:
        m, src, sid, last = r["minute"], r["source"], r["site_id"] or "", r["last_ts"]
        per.setdefault(m, {})
        per[m][sid] = max(per[m].get(sid, 0), last)
        srcs.setdefault((m, sid), set()).add(src)
    chosen = {}
    for m, cand in per.items():
        farms = {s: t for s, t in cand.items() if s}
        chosen[m] = max(farms, key=lambda s: (farms[s], s)) if farms else ""
    seq = sorted(chosen)
    for i, m in enumerate(seq):
        if chosen[m]:
            continue
        prev = next((p for p in reversed(seq[:i]) if chosen[p]), None)
        nxt = next((n for n in seq[i + 1:] if chosen[n]), None)
        if (prev is not None and nxt is not None and chosen[prev] == chosen[nxt]
                and m - prev <= gap_s and nxt - m <= gap_s):
            srcs.setdefault((m, chosen[prev]), set()).update(srcs.get((m, ""), set()))
            chosen[m] = chosen[prev]
    out = []
    for m in seq:
        site = chosen[m]
        if exclude and exclude(site, m):
            continue
        if out and out[-1]["site"] == site and m - out[-1]["end"] <= gap_s:
            out[-1]["end"] = m + 60
        else:
            out.append({"site": site, "start": m, "end": m + 60, "sources": set()})
        out[-1]["sources"] |= srcs.get((m, site), set())
    return out


# ---- the builder ---------------------------------------------------------------------
def _frozen(e):
    return (e["status"] != "draft" or e["edited"] or e["source"] not in AUTO_SOURCES)


def _entries_between(a, b):
    return [dict(r) for r in _conn().execute(
        "SELECT * FROM time_entries WHERE start_ts < ? AND COALESCE(end_ts, ?) > ? ORDER BY start_ts, id",
        (b, int(time.time()), a))]


def build_day(day, now=None):
    """(Re)build the draft entries of one day from the activity. Frozen entries
    stay as they are; drafts are updated in place where they still match."""
    now = int(now or time.time())
    a, b = day_bounds(day)
    if a > now:
        return
    st = settings()
    g = st["global"]
    gap, min_len = g["idle_gap_min"] * 60, g["min_session_min"] * 60
    c = _conn()
    with _lock:
        rows = c.execute("SELECT minute, source, site_id, last_ts FROM time_activity"
                         " WHERE minute >= ? AND minute < ?", (a, b)).fetchall()
        entries = _entries_between(a, b)
        frozen = [e for e in entries if _frozen(e)]
        holes = {"remote": [], "field": [], "dev": []}
        onsite_at = {}                      # site -> [(start, end)] of on-site time (any status)
        for e in entries:
            end = e["end_ts"] or now
            if e["status"] != "dismissed" and e["kind"] == "onsite":
                onsite_at.setdefault(e["site_id"], []).append((e["start_ts"], end))
        for e in frozen:
            holes[GROUP.get(e["kind"], "remote")].append((e["start_ts"], e["end_ts"] or now))

        want = []                           # (kind, site, start, end, source, sources)
        # on site: the operator's phone seen on the farm network
        wifi = {}
        for r in rows:
            if r["source"] == "wifi" and r["site_id"]:
                wifi.setdefault(r["site_id"], []).append(r["minute"])
        for sid, mins in wifi.items():
            for s, e in sessions_of(mins, g["wifi_gap_min"] * 60):
                for x, y in _subtract(s, e, holes["field"]):
                    want.append(("onsite", sid, x, y, "wifi", "wifi"))
                    onsite_at.setdefault(sid, []).append((x, y))
        # remote
        rem = [r for r in rows if r["source"] in REMOTE_SOURCES]

        def on_site(site, m):
            return _covered(m, onsite_at.get(site, [])) or (not site and any(
                _covered(m, iv) for iv in onsite_at.values()))

        for s in remote_timeline(rem, gap, on_site):
            for x, y in _subtract(s["start"], s["end"], holes["remote"]):
                if y - x >= max(min_len, 60):
                    want.append(("remote", s["site"], x, y, "auto", ",".join(sorted(s["sources"]))))
        # Netwatch development (Claude sessions)
        dev = [r["minute"] for r in rows if r["source"] == "dev"]
        for s, e in sessions_of(dev, g["dev_gap_min"] * 60):
            for x, y in _subtract(s, e, holes["dev"]):
                if y - x >= max(min_len, 60):
                    want.append(("dev", "", x, y, "claude", "claude"))

        # reconcile with the existing auto drafts of this day
        old = [e for e in entries if not _frozen(e) and a <= e["start_ts"] < b]
        used = set()
        ts = now
        for kind, sid, x, y, src, srcs in want:
            match = next((e for e in old if e["id"] not in used and e["kind"] == kind
                          and e["site_id"] == sid and e["start_ts"] < y and (e["end_ts"] or now) > x), None)
            if match:
                used.add(match["id"])
                if (match["start_ts"], match["end_ts"], match["sources"]) != (x, y, srcs):
                    c.execute("UPDATE time_entries SET start_ts=?, end_ts=?, sources=?, updated_ts=? WHERE id=?",
                              (x, y, srcs, ts, match["id"]))
            else:
                c.execute("INSERT INTO time_entries (kind, site_id, start_ts, end_ts, source, sources, status,"
                          " created_ts, updated_ts) VALUES (?,?,?,?,?,?,'draft',?,?)",
                          (kind, sid, x, y, src, srcs, ts, ts))
        gone = [e["id"] for e in old if e["id"] not in used]
        if gone:
            c.execute(f"DELETE FROM time_entries WHERE id IN ({','.join('?' * len(gone))})", gone)
        c.commit()


def build_recent(days=2, now=None):
    now = now or time.time()
    d = dt.date.fromtimestamp(now)
    for i in range(days):
        build_day((d - dt.timedelta(days=i)).isoformat(), now)


# ---- entries ---------------------------------------------------------------------------
def _clean_entry(body, cur=None):
    cur = cur or {}
    kind = body.get("kind", cur.get("kind"))
    if kind not in KINDS:
        raise TimeError("kind must be one of " + ", ".join(KINDS))
    sid = str(body.get("site_id", cur.get("site_id", "")) or "")
    if kind in ("dev", "general"):
        sid = ""
    elif sid and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sid):
        raise TimeError("bad farm id")
    elif kind in ("onsite", "travel") and not sid:
        raise TimeError("pick the farm")
    try:
        start = int(body.get("start_ts", cur.get("start_ts")))
        end = body.get("end_ts", cur.get("end_ts"))
        end = int(end) if end is not None else None
    except (TypeError, ValueError):
        raise TimeError("start and end must be times")
    if end is not None and end <= start:
        raise TimeError("the end must be after the start")
    if end is not None and end - start > 24 * 3600:
        raise TimeError("one entry can't be longer than 24 hours")
    if start > time.time() + 300:
        raise TimeError("that start is in the future")
    km = body.get("km", cur.get("km"))
    if km in ("", None):
        km = None
    else:
        try:
            km = round(float(km), 1)
        except (TypeError, ValueError):
            raise TimeError("km must be a number")
        if not 0 <= km <= 5000:
            raise TimeError("km must be between 0 and 5000")
    note = str(body.get("note", cur.get("note", "")) or "").strip()[:500]
    return {"kind": kind, "site_id": sid, "start_ts": start, "end_ts": end, "km": km, "note": note}


def get_entry(eid):
    r = _conn().execute("SELECT * FROM time_entries WHERE id=?", (int(eid),)).fetchone()
    if not r:
        raise TimeError("that entry is gone — the list refreshed; try again", 404)
    return dict(r)


def add_entry(body, source="manual", status="approved"):
    e = _clean_entry(body)
    if e["end_ts"] is None and source == "manual":
        raise TimeError("give an end time")
    if status == "approved" and e["kind"] == "remote" and not e["site_id"]:
        raise TimeError("pick the farm (or choose General)")
    ts = int(time.time())
    c = _conn()
    cur = c.execute("INSERT INTO time_entries (kind, site_id, start_ts, end_ts, km, note, source, sources,"
                    " status, created_ts, updated_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (e["kind"], e["site_id"], e["start_ts"], e["end_ts"], e["km"], e["note"],
                     source, source, status, ts, ts))
    c.commit()
    return get_entry(cur.lastrowid)


def update_entry(eid, body):
    """Edit fields and/or status. Any change freezes the entry (`edited`)."""
    cur = get_entry(eid)
    status = body.get("status", cur["status"])
    if status not in ("draft", "approved", "dismissed"):
        raise TimeError("bad status")
    fields = {k: body[k] for k in ("kind", "site_id", "start_ts", "end_ts", "km", "note") if k in body}
    e = _clean_entry(fields, cur) if fields else cur
    if status == "approved" and e["kind"] == "remote" and not e["site_id"]:
        raise TimeError("assign it to a farm (or General) before approving")
    if status == "approved" and e["end_ts"] is None:
        raise TimeError("this entry is still running — stop it first")
    edited = 1 if (fields or status != cur["status"]) else cur["edited"]
    c = _conn()
    c.execute("UPDATE time_entries SET kind=?, site_id=?, start_ts=?, end_ts=?, km=?, note=?, status=?,"
              " edited=?, updated_ts=? WHERE id=?",
              (e["kind"], e["site_id"], e["start_ts"], e["end_ts"], e["km"], e["note"], status,
               edited, int(time.time()), cur["id"]))
    c.commit()
    return get_entry(cur["id"])


def delete_entry(eid):
    """Typed/button entries are deleted; builder-made ones are DISMISSED so the
    builder does not bring the same time back."""
    cur = get_entry(eid)
    c = _conn()
    if cur["source"] in AUTO_SOURCES:
        c.execute("UPDATE time_entries SET status='dismissed', edited=1, updated_ts=? WHERE id=?",
                  (int(time.time()), cur["id"]))
    else:
        c.execute("DELETE FROM time_entries WHERE id=?", (cur["id"],))
    c.commit()
    return True


def approve_day(day):
    a, b = day_bounds(day)
    c = _conn()
    left = c.execute("SELECT COUNT(*) FROM time_entries WHERE status='draft' AND start_ts>=? AND start_ts<?"
                     " AND kind='remote' AND site_id=''", (a, b)).fetchone()[0]
    n = c.execute("UPDATE time_entries SET status='approved', edited=1, updated_ts=? WHERE status='draft'"
                  " AND start_ts>=? AND start_ts<? AND end_ts IS NOT NULL AND NOT (kind='remote' AND site_id='')",
                  (int(time.time()), a, b)).rowcount
    c.commit()
    return {"approved": n, "unassigned_left": left}


def open_entries():
    return [dict(r) for r in _conn().execute(
        "SELECT * FROM time_entries WHERE end_ts IS NULL ORDER BY start_ts")]


def clock(action, site_id="", km=None, now=None):
    """The field buttons. drive → a travel line opens; arrive → travel closes and a
    visit opens; leave → the visit closes (and, with `then` = drive, the trip home
    opens); home/stop → whatever is open closes."""
    now = int(now or time.time())
    if action not in ("drive", "arrive", "leave", "home", "stop", "leave_drive"):
        raise TimeError("unknown action")
    st = settings()
    c = _conn()
    with _lock:
        opened = open_entries()
        travel = [e for e in opened if e["kind"] == "travel"]
        visit = [e for e in opened if e["kind"] == "onsite"]

        def close(es):
            for e in es:
                end = max(now, e["start_ts"] + 60)
                c.execute("UPDATE time_entries SET end_ts=?, updated_ts=? WHERE id=?", (end, now, e["id"]))

        def opn(kind, sid, km_=None):
            c.execute("INSERT INTO time_entries (kind, site_id, start_ts, end_ts, km, source, sources, status,"
                      " created_ts, updated_ts) VALUES (?,?,?,NULL,?,'button','button','approved',?,?)",
                      (kind, sid, now, km_, now, now))

        if action in ("drive", "arrive") and not site_id:
            raise TimeError("pick the farm")
        if action == "drive":
            close(travel + visit)
            f = farm_settings(site_id, st)
            opn("travel", site_id, km if km is not None else (f["km_one_way"] or None))
        elif action == "arrive":
            close(travel + visit)
            opn("onsite", site_id)
        elif action in ("leave", "leave_drive"):
            if not visit:
                raise TimeError("you're not clocked in at a farm")
            close(visit)
            if action == "leave_drive":
                sid = visit[-1]["site_id"]
                f = farm_settings(sid, st)
                opn("travel", sid, km if km is not None else (f["km_one_way"] or None))
        else:
            close(travel + visit)
        c.commit()
    return open_entries()


# ---- views -------------------------------------------------------------------------------
def _dur(e, now):
    return max(0, (e["end_ts"] or now) - e["start_ts"])


def day_view(day, now=None):
    now = int(now or time.time())
    a, b = day_bounds(day)
    if a >= now - REBUILD_DAYS * 86400:
        build_day(day, now)
    entries = [dict(r) for r in _conn().execute(
        "SELECT * FROM time_entries WHERE start_ts>=? AND start_ts<? AND status!='dismissed'"
        " ORDER BY start_ts, id", (a, b))]
    acts = [dict(r) for r in _conn().execute(
        "SELECT minute, source, site_id FROM time_activity WHERE minute>=? AND minute<? ORDER BY minute", (a, b))]
    totals = {}
    for e in entries:
        key = e["site_id"] if e["kind"] in ("remote", "onsite", "travel") else e["kind"]
        t = totals.setdefault(key or "", {"remote": 0, "onsite": 0, "travel": 0, "dev": 0, "general": 0,
                                          "km": 0, "drafts": 0})
        t[e["kind"]] += _dur(e, now)
        t["km"] += e["km"] or 0
        t["drafts"] += e["status"] == "draft"
    return {"day": day, "start": a, "end": b, "now": now, "entries": entries,
            "activity": acts, "totals": totals, "open": open_entries()}


def _ceil_to(minutes, step):
    return int(math.ceil(minutes / step) * step) if minutes > 0 else 0


def bill(site_id, month, include_drafts=False, now=None, st=None):
    """One farm's month: per-day rounded minutes per kind and the amounts."""
    now = int(now or time.time())
    st = st or settings()
    g, f = st["global"], farm_settings(site_id, st)
    a, b = month_bounds(month)
    statuses = ("approved", "draft") if include_drafts else ("approved",)
    rows = [dict(r) for r in _conn().execute(
        f"SELECT * FROM time_entries WHERE site_id=? AND kind IN ('remote','onsite','travel')"
        f" AND start_ts>=? AND start_ts<? AND end_ts IS NOT NULL AND status IN ({','.join('?' * len(statuses))})"
        f" ORDER BY start_ts, id", (site_id, a, b, *statuses))]
    drafts = _conn().execute(
        "SELECT COUNT(*) FROM time_entries WHERE site_id=? AND status='draft' AND start_ts>=? AND start_ts<?",
        (site_id, a, b)).fetchone()[0]
    step = g["round_min"]
    days = {}
    for e in rows:
        d = days.setdefault(day_of(e["start_ts"]), {"entries": [], "remote": 0, "onsite": 0, "onsite_actual": 0,
                                                     "visits": 0, "travel": 0, "km": 0.0})
        mins = (e["end_ts"] - e["start_ts"]) / 60
        e["minutes"] = round(mins, 1)
        d["entries"].append(e)
        if e["kind"] == "onsite":
            d["onsite_actual"] += mins
            d["onsite"] += max(mins, f["callout_min"])
            d["visits"] += 1
        else:
            d[e["kind"]] += mins
        d["km"] += e["km"] or 0
    tot = {"remote": 0, "onsite": 0, "travel": 0, "km": 0.0, "visits": 0,
           "remote_actual": 0.0, "onsite_actual": 0.0, "travel_actual": 0.0}
    for d in days.values():
        for k in ("remote", "onsite", "travel"):
            d[k + "_billed"] = _ceil_to(d[k], step)
            tot[k] += d[k + "_billed"]
        tot["remote_actual"] += d["remote"]
        tot["travel_actual"] += d["travel"]
        tot["onsite_actual"] += d["onsite_actual"]
        tot["km"] += d["km"]
        tot["visits"] += d["visits"]
    amounts = {"remote": tot["remote"] / 60 * f["rate_remote"],
               "onsite": tot["onsite"] / 60 * f["rate_onsite"],
               "travel": tot["travel"] / 60 * f["rate_travel"],
               "km": tot["km"] * f["rate_km"]}
    amounts = {k: round(v, 2) for k, v in amounts.items()}
    return {"site_id": site_id, "month": month, "farm": f, "round_min": step, "currency": g["currency"],
            "days": dict(sorted(days.items())), "totals": tot, "amounts": amounts,
            "total": round(sum(amounts.values()), 2), "drafts": drafts, "include_drafts": include_drafts}


def month_view(month, site_ids, now=None):
    now = int(now or time.time())
    a, b = month_bounds(month)
    st = settings()
    farms = [bill(sid, month, now=now, st=st) for sid in site_ids]
    other = {}
    for r in _conn().execute(
            "SELECT kind, site_id, status, start_ts, end_ts FROM time_entries WHERE start_ts>=? AND start_ts<?"
            " AND status!='dismissed' AND (kind IN ('dev','general') OR (kind='remote' AND site_id=''))",
            (a, b)):
        k = "unassigned" if r["kind"] == "remote" else r["kind"]
        o = other.setdefault(k, {"seconds": 0, "drafts": 0})
        o["seconds"] += max(0, (r["end_ts"] or now) - r["start_ts"])
        o["drafts"] += r["status"] == "draft"
    dev_rate = st["global"]["dev_rate"]
    if "dev" in other and dev_rate:
        other["dev"]["value"] = round(other["dev"]["seconds"] / 3600 * dev_rate, 2)
    return {"month": month, "farms": farms, "other": other, "settings": st}


# ---- activity feeds ----------------------------------------------------------------------
class _ProxyLog:
    """Follows Caddy's JSON access log for the proxied site pages."""

    def __init__(self, path=CADDY_LOG):
        self.path = path
        self.ino = None
        self.pos = 0

    def read(self, port_site):
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return []
        if st.st_ino != self.ino or st.st_size < self.pos:
            self.ino, self.pos = st.st_ino, 0       # rolled or new: read from the start (marks are idempotent)
        out = []
        with open(self.path, "rb") as fh:
            fh.seek(self.pos)
            data = fh.read(8 * 1024 * 1024)
        cut = data.rfind(b"\n") + 1
        self.pos += cut
        for line in data[:cut].splitlines():
            hit = parse_proxy_line(line, port_site)
            if hit:
                out.append(hit)
        return out


def parse_proxy_line(line, port_site):
    """One Caddy access-log line -> (site_id, ts) when it is a sign of work."""
    try:
        j = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    req = j.get("request") or {}
    host = str(req.get("host") or "")
    port = host.rsplit(":", 1)[1] if ":" in host else ""
    site = port_site.get(port)
    if not site:
        return None
    uri = str(req.get("uri") or "").split("?", 1)[0]
    method = str(req.get("method") or "GET").upper()
    if method == "GET" and any(uri == p for p in PROXY_BACKGROUND):
        return None
    if int(j.get("status") or 0) == 401:
        return None                        # the browser asking for the proxy login
    try:
        ts = int(float(j.get("ts")))
    except (TypeError, ValueError):
        return None
    return site, ts


def _port_site(cfg):
    m = {}
    for s in cfg.get("sites") or []:
        for k in ("proxy_netwatch_port", "proxy_kuma_port"):
            if s.get(k):
                m[str(s[k])] = s["id"]
    return m


def _phone_marks(cfg, snapshot):
    st = settings()
    out = []
    now = time.time()
    for s in cfg.get("sites") or []:
        f = st["farms"].get(s["id"]) or {}
        key = f.get("phone_key")
        if not key:
            continue
        devs = ((snapshot(s["id"]) or {}).get("devices") or {}).get("devices") or []
        d = next((x for x in devs if x.get("key") == key), None)
        ls = d and d.get("last_seen")
        if ls and now - ls < 26 * 3600:
            out.append((s["id"], ls))
    return out


def _backup_daily(now=None):
    now = now or time.time()
    os.makedirs(BACKUP_DIR, exist_ok=True)
    name = os.path.join(BACKUP_DIR, f"time-{dt.date.fromtimestamp(now):%Y%m%d}.json")
    if os.path.exists(name):
        return None
    c = _conn()
    body = {"kind": "netwatch-time-backup", "made": int(now), "settings": settings(),
            "entries": [dict(r) for r in c.execute("SELECT * FROM time_entries ORDER BY id")]}
    tmp = name + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(body, fh)
    os.replace(tmp, name)
    files = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith("time-") and f.endswith(".json"))
    for f in files[:-BACKUP_KEEP]:
        os.remove(os.path.join(BACKUP_DIR, f))
    c.execute("DELETE FROM time_activity WHERE minute < ?", (int(now) - ACTIVITY_KEEP_DAYS * 86400,))
    c.commit()
    return name


def _loop():
    import hubconfig
    import tunnels
    from poller import poller
    proxy = _ProxyLog()
    last_build = 0
    while True:
        try:
            cfg = hubconfig.load()
            hits = proxy.read(_port_site(cfg))
            if hits:
                mark_many("proxy", hits)
            now = time.time()
            tun = [(t["site_id"], now) for t in tunnels.manager.list()
                   if now - (t.get("last_active") or 0) < 75]
            if tun:
                mark_many("tunnel", tun)
            ph = _phone_marks(cfg, poller.snapshot)
            if ph:
                mark_many("wifi", ph)
            if now - last_build > 300:
                build_recent(2, now)
                last_build = now
                if dt.datetime.fromtimestamp(now).hour >= 2:
                    _backup_daily(now)
        except Exception as e:                       # never let the feed die
            print(f"[time] loop error: {e!r}", flush=True)
        time.sleep(30)


def start():
    threading.Thread(target=_loop, name="timekeeping", daemon=True).start()


# ---- CLI: `python timekeeping.py import-dev < minutes.json` -------------------------------
def import_dev(data):
    """Minute stamps of Claude sessions working on Netwatch, from the host's
    hub/tools/devtime.py: {"minutes": [epoch, …]}."""
    mins = data.get("minutes") if isinstance(data, dict) else data
    items = []
    for m in mins or []:
        try:
            m = int(m)
        except (TypeError, ValueError):
            continue
        if 1_600_000_000 < m < time.time() + 3600:
            items.append(("", m))
    if items:
        mark_many("dev", items)
    days = sorted({day_of(m) for _, m in items})
    for d in days:
        if day_bounds(d)[0] >= time.time() - REBUILD_DAYS * 86400:
            build_day(d)
    return {"imported": len(items), "days": days}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "import-dev":
        print(json.dumps(import_dev(json.load(sys.stdin))))
    elif cmd == "rebuild":
        d = sys.argv[2] if len(sys.argv) > 2 else today()
        build_day(d)
        print(f"rebuilt {d}")
    else:
        print("usage: timekeeping.py import-dev < minutes.json | rebuild [YYYY-MM-DD]")
        sys.exit(2)
