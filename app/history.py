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
        """
    )
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
