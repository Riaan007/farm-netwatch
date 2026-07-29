"""Uptime history in SQLite (/data/netwatch.db).

Every scan records one sample per known device key (MAC when on the local
segment, else IP). Rollups power the dashboard uptime %% and sparklines.
"""
import os
import sqlite3
import threading
import time

DATA_DIR = os.environ.get("NETWATCH_DATA", "/data")
DB_PATH = os.path.join(DATA_DIR, "netwatch.db")

_local = threading.local()


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
        CREATE TABLE IF NOT EXISTS samples (
            key   TEXT NOT NULL,
            ts    INTEGER NOT NULL,
            online INTEGER NOT NULL,
            ip    TEXT,
            rtt   REAL
        );
        CREATE INDEX IF NOT EXISTS idx_samples_key_ts ON samples(key, ts);

        -- Append-only audit log of device events per IP. Independent of the live
        -- device list, so a device's history survives even after it's forgotten.
        CREATE TABLE IF NOT EXISTS events (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            ts    INTEGER NOT NULL,
            type  TEXT NOT NULL,          -- new | offline | online | ip_change
            key   TEXT,                   -- device key (MAC, else IP)
            ip    TEXT,
            mac   TEXT,
            name  TEXT,
            category TEXT,
            vendor TEXT,
            hostname TEXT,
            detail TEXT                   -- JSON snapshot (ports, model, serial, prev device, …)
        );
        CREATE INDEX IF NOT EXISTS idx_events_ip_ts  ON events(ip, ts);
        CREATE INDEX IF NOT EXISTS idx_events_key_ts ON events(key, ts);
        CREATE INDEX IF NOT EXISTS idx_events_ts     ON events(ts);

        -- Fine-grained latency heartbeats (~60s) for the Uptime-Kuma-style chart.
        -- Short retention (a few days); `samples` stays for long-term uptime %.
        CREATE TABLE IF NOT EXISTS heartbeats (
            key    TEXT NOT NULL,
            ts     INTEGER NOT NULL,
            online INTEGER NOT NULL,
            rtt    REAL
        );
        CREATE INDEX IF NOT EXISTS idx_hb_key_ts ON heartbeats(key, ts);

        -- Wireless telemetry from Ubiquiti radios (see radiomon.py). One row per
        -- radio per poll, plus one row per wireless LINK — a link's health is a
        -- property of the pair, not of either radio, and it is where a failing
        -- backhaul shows up first.
        CREATE TABLE IF NOT EXISTS radio_samples (
            key    TEXT NOT NULL,
            ts     INTEGER NOT NULL,
            ip     TEXT,
            mode   TEXT,
            ssid   TEXT,
            freq   TEXT,
            chanbw TEXT,
            signal REAL,
            noise  REAL,
            chain0 REAL,
            chain1 REAL,
            airtime REAL,
            cap_dl REAL,
            cap_ul REAL,
            tx_rate REAL,
            rx_rate REAL,
            links  INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_radio_key_ts ON radio_samples(key, ts);

        CREATE TABLE IF NOT EXISTS radio_links (
            key    TEXT NOT NULL,          -- the radio we polled
            ts     INTEGER NOT NULL,
            peer   TEXT NOT NULL,          -- far-end MAC
            name   TEXT,
            ip     TEXT,
            model  TEXT,
            signal REAL,                   -- what we hear
            remote_signal REAL,            -- what the far end hears back
            score_dl REAL,
            score_ul REAL,
            tx     REAL,
            rx     REAL,
            latency REAL,
            distance REAL
        );
        CREATE INDEX IF NOT EXISTS idx_rlink_key_ts  ON radio_links(key, ts);
        CREATE INDEX IF NOT EXISTS idx_rlink_peer_ts ON radio_links(peer, ts);
        """
    )
    c.commit()


# ---- radio telemetry --------------------------------------------------------
_RADIO_COLS = ("ip", "mode", "ssid", "freq", "chanbw", "signal", "noise", "chain0",
               "chain1", "airtime", "cap_dl", "cap_ul", "tx_rate", "rx_rate", "links")
_LINK_COLS = ("peer", "name", "ip", "model", "signal", "remote_signal", "score_dl",
              "score_ul", "tx", "rx", "latency", "distance")


def radio_record(key, sample, links, ts=None):
    """Store one poll: `sample` and each `links` entry are dicts keyed by the
    column names above; anything missing lands as NULL."""
    ts = int(ts or time.time())
    c = _conn()
    c.execute(
        f"INSERT INTO radio_samples (key, ts, {','.join(_RADIO_COLS)}) "
        f"VALUES (?,?,{','.join('?' * len(_RADIO_COLS))})",
        (key, ts, *(sample.get(col) for col in _RADIO_COLS)),
    )
    if links:
        c.executemany(
            f"INSERT INTO radio_links (key, ts, {','.join(_LINK_COLS)}) "
            f"VALUES (?,?,{','.join('?' * len(_LINK_COLS))})",
            [(key, ts, *(ln.get(col) for col in _LINK_COLS)) for ln in links],
        )
    c.commit()


def radio_series(key, window_s=86400, limit=500):
    """Radio-level samples for a key, oldest first."""
    since = int(time.time()) - window_s
    rows = _conn().execute(
        "SELECT * FROM radio_samples WHERE key=? AND ts>=? ORDER BY ts DESC LIMIT ?",
        (key, since, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def radio_link_series(key, peer=None, window_s=86400, limit=2000):
    """Per-link samples for a radio (optionally one peer), oldest first."""
    since = int(time.time()) - window_s
    sql = "SELECT * FROM radio_links WHERE key=? AND ts>=?"
    args = [key, since]
    if peer:
        sql += " AND peer=?"
        args.append(peer)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    rows = _conn().execute(sql, args).fetchall()
    return [dict(r) for r in reversed(rows)]


def radio_baseline(key, column, window_s=7 * 86400, settle_s=3600, peer=None):
    """Median of `column` over the window, IGNORING the most recent `settle_s`.

    Excluding the fresh samples matters: a slow degradation would otherwise creep
    into its own baseline and never trip a threshold. Returns (median, n).
    """
    if column not in (set(_RADIO_COLS) | set(_LINK_COLS)):
        raise ValueError(f"unknown column {column!r}")
    table = "radio_links" if peer is not None else "radio_samples"
    now = int(time.time())
    sql = (f"SELECT {column} AS v FROM {table} WHERE key=? AND ts>=? AND ts<=? "
           f"AND {column} IS NOT NULL")
    args = [key, now - window_s, now - settle_s]
    if peer:
        sql += " AND peer=?"
        args.append(peer)
    vals = sorted(r["v"] for r in _conn().execute(sql, args).fetchall())
    if not vals:
        return None, 0
    mid = len(vals) // 2
    med = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0
    return med, len(vals)


def radio_peers_seen(key, window_s=86400):
    """Peers this radio has had a link with in the window: {peer: (name, last_ts)}."""
    since = int(time.time()) - window_s
    rows = _conn().execute(
        "SELECT peer, MAX(ts) AS last_ts, name FROM radio_links "
        "WHERE key=? AND ts>=? GROUP BY peer", (key, since)).fetchall()
    return {r["peer"]: (r["name"], r["last_ts"]) for r in rows}


def radio_keys(window_s=7 * 86400):
    """{key: newest ts} for every radio with telemetry in the window."""
    since = int(time.time()) - window_s
    rows = _conn().execute(
        "SELECT key, MAX(ts) AS ts FROM radio_samples WHERE ts>=? GROUP BY key",
        (since,)).fetchall()
    return {r["key"]: r["ts"] for r in rows}


def radio_latest(key):
    """The most recent stored reading for a radio: (sample, links). Used to warm
    the in-memory cache after a restart so the dashboards aren't blank until the
    next poll — the samples were never lost, only the process holding them."""
    row = _conn().execute(
        "SELECT * FROM radio_samples WHERE key=? ORDER BY ts DESC LIMIT 1", (key,)).fetchone()
    if not row:
        return None, []
    links = _conn().execute(
        "SELECT * FROM radio_links WHERE key=? AND ts=?", (key, row["ts"])).fetchall()
    return dict(row), [dict(x) for x in links]


def radio_prune(retention_days):
    cutoff = int(time.time()) - int(retention_days) * 86400
    c = _conn()
    c.execute("DELETE FROM radio_samples WHERE ts < ?", (cutoff,))
    c.execute("DELETE FROM radio_links WHERE ts < ?", (cutoff,))
    c.commit()


def record(samples):
    """samples: iterable of (key, online_bool, ip, rtt_or_None)."""
    ts = int(time.time())
    c = _conn()
    c.executemany(
        "INSERT INTO samples (key, ts, online, ip, rtt) VALUES (?,?,?,?,?)",
        [(k, ts, 1 if up else 0, ip, rtt) for (k, up, ip, rtt) in samples],
    )
    c.commit()


def uptime_pct(key, window_s):
    c = _conn()
    since = int(time.time()) - window_s
    row = c.execute(
        "SELECT AVG(online)*100.0 AS pct, COUNT(*) AS n FROM samples WHERE key=? AND ts>=?",
        (key, since),
    ).fetchone()
    if not row or not row["n"]:
        return None
    return round(row["pct"], 1)


def summary(key):
    return {
        "uptime_24h": uptime_pct(key, 86400),
        "uptime_7d": uptime_pct(key, 7 * 86400),
        "uptime_30d": uptime_pct(key, 30 * 86400),
        "last_seen": last_seen(key),
    }


def last_seen(key):
    c = _conn()
    row = c.execute(
        "SELECT ts FROM samples WHERE key=? AND online=1 ORDER BY ts DESC LIMIT 1", (key,)
    ).fetchone()
    return row["ts"] if row else None


def series(key, window_s=86400, buckets=48):
    """Return `buckets` online-fraction values across the window for a sparkline."""
    c = _conn()
    now = int(time.time())
    since = now - window_s
    step = max(1, window_s // buckets)
    rows = c.execute(
        "SELECT ts, online FROM samples WHERE key=? AND ts>=? ORDER BY ts", (key, since)
    ).fetchall()
    out = [None] * buckets
    agg = {}
    for r in rows:
        b = min(buckets - 1, (r["ts"] - since) // step)
        s, n = agg.get(b, (0, 0))
        agg[b] = (s + r["online"], n + 1)
    for b, (s, n) in agg.items():
        out[b] = round(s / n, 3) if n else None
    return out


def prune(retention_days):
    c = _conn()
    cutoff = int(time.time()) - retention_days * 86400
    c.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
    c.commit()


# ---- fine-grained latency heartbeats (for the Kuma-style chart) ----------

def record_beats(rows):
    """rows: iterable of (key, online_bool, rtt_or_None). One commit, stamped now."""
    rows = list(rows)
    if not rows:
        return
    ts = int(time.time())
    c = _conn()
    c.executemany(
        "INSERT INTO heartbeats (key, ts, online, rtt) VALUES (?,?,?,?)",
        [(k, ts, 1 if up else 0, rtt) for (k, up, rtt) in rows],
    )
    c.commit()


def beats(key, window_s, max_points=120):
    """Timestamped latency/up series for `key` over the window, bucketed to at most
    `max_points`: [{ts (bucket center), up (0..1 fraction), rtt (avg of online, else
    None)}]. Powers the per-device latency chart's 30m/1h/12h/24h ranges."""
    c = _conn()
    now = int(time.time())
    since = now - window_s
    buckets = max(1, min(max_points, window_s // 60))   # ~1 point per 60s, capped
    step = max(1, window_s // buckets)
    rows = c.execute(
        "SELECT ts, online, rtt FROM heartbeats WHERE key=? AND ts>=? ORDER BY ts",
        (key, since),
    ).fetchall()
    agg = {}
    for r in rows:
        b = min(buckets - 1, (r["ts"] - since) // step)
        on, n, rs, rn = agg.get(b, (0, 0, 0.0, 0))
        on += r["online"]; n += 1
        if r["online"] and r["rtt"] is not None:
            rs += r["rtt"]; rn += 1
        agg[b] = (on, n, rs, rn)
    out = []
    for b in sorted(agg):
        on, n, rs, rn = agg[b]
        out.append({"ts": since + b * step + step // 2,
                    "up": round(on / n, 3) if n else None,
                    "rtt": round(rs / rn, 1) if rn else None})
    return out


def prune_beats(retention_days):
    c = _conn()
    cutoff = int(time.time()) - int(retention_days * 86400)
    c.execute("DELETE FROM heartbeats WHERE ts < ?", (cutoff,))
    c.commit()


def latest_beat(key):
    """Most recent heartbeat for a key, or None (used by the internet check)."""
    c = _conn()
    r = c.execute("SELECT ts, online, rtt FROM heartbeats WHERE key=? "
                  "ORDER BY ts DESC LIMIT 1", (key,)).fetchone()
    return dict(r) if r else None


def delete_key(key):
    """Drop all uptime samples + heartbeats for a device that's being forgotten."""
    c = _conn()
    c.execute("DELETE FROM samples WHERE key = ?", (key,))
    c.execute("DELETE FROM heartbeats WHERE key = ?", (key,))
    c.commit()


def delete_keys(keys):
    """Drop samples for many devices in a single commit (used by prune).

    Deliberately leaves the `events` audit log intact so a forgotten device's
    history is preserved.
    """
    keys = list(keys)
    if not keys:
        return
    c = _conn()
    c.executemany("DELETE FROM samples WHERE key = ?", [(k,) for k in keys])
    c.executemany("DELETE FROM heartbeats WHERE key = ?", [(k,) for k in keys])
    c.commit()


# ---- event log -----------------------------------------------------------

import json as _json  # noqa: E402 - local to the event helpers


def build_event(etype, dev, detail_extra=None):
    """Build an events-table row tuple from a device record + event type."""
    detail = {
        "ports": dev.get("ports"),
        "model": dev.get("model"),
        "serial": dev.get("serial"),
        "type_label": dev.get("type"),
        "os": dev.get("os"),
        "rtt": dev.get("rtt"),
        "confidence": dev.get("confidence"),
    }
    if detail_extra:
        detail.update(detail_extra)
    return (
        int(time.time()), etype, dev.get("key"), dev.get("ip"), dev.get("mac"),
        dev.get("name"), dev.get("category"), dev.get("vendor"), dev.get("hostname"),
        _json.dumps({k: v for k, v in detail.items() if v not in (None, "", [])}),
    )


def log_events(rows):
    """Insert pre-built event rows (from _event_row) in one commit."""
    rows = list(rows)
    if not rows:
        return
    c = _conn()
    c.executemany(
        "INSERT INTO events (ts,type,key,ip,mac,name,category,vendor,hostname,detail) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    c.commit()


def events(ip=None, key=None, etype=None, since=None, limit=300):
    """Query the event log, newest first. Filter by ip / key / type / since-ts."""
    where, args = [], []
    if ip:
        where.append("ip = ?"); args.append(ip)
    if key:
        where.append("key = ?"); args.append(key)
    if etype:
        where.append("type = ?"); args.append(etype)
    if since:
        where.append("ts >= ?"); args.append(int(since))
    sql = "SELECT ts,type,key,ip,mac,name,category,vendor,hostname,detail FROM events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC, id DESC LIMIT ?"
    args.append(int(limit))
    c = _conn()
    out = []
    for r in c.execute(sql, args):
        d = dict(r)
        try:
            d["detail"] = _json.loads(d["detail"]) if d["detail"] else {}
        except (ValueError, TypeError):
            d["detail"] = {}
        out.append(d)
    return out


def ip_history():
    """One row per IP ever seen: the most-recent device there, how many distinct
    devices have used it, the last event type/time, and current online guess."""
    c = _conn()
    rows = c.execute(
        """
        SELECT e.ip AS ip,
               COUNT(DISTINCT e.key) AS device_count,
               COUNT(*) AS event_count,
               MAX(e.ts) AS last_ts
        FROM events e
        WHERE e.ip IS NOT NULL AND e.ip <> ''
        GROUP BY e.ip
        ORDER BY last_ts DESC
        """).fetchall()
    out = []
    for r in rows:
        ip = r["ip"]
        last = c.execute(
            "SELECT type,key,mac,name,category,vendor,hostname FROM events "
            "WHERE ip = ? ORDER BY ts DESC, id DESC LIMIT 1", (ip,)).fetchone()
        d = {"ip": ip, "device_count": r["device_count"],
             "event_count": r["event_count"], "last_ts": r["last_ts"]}
        if last:
            d.update({"last_type": last["type"], "key": last["key"], "mac": last["mac"],
                      "name": last["name"], "category": last["category"],
                      "vendor": last["vendor"], "hostname": last["hostname"]})
        out.append(d)
    return out


def prune_events(retention_days):
    c = _conn()
    cutoff = int(time.time()) - retention_days * 86400
    c.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
    c.commit()
