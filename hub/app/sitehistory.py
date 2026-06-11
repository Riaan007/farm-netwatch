"""Site reachability history in SQLite (/data/hub.db).

Every status poll records one row per site: was its Netwatch reachable over the
VPN, how fast, and the headline counts at that moment. Rollups power the
overview sparklines and the site-detail reachability strip. Same WAL +
thread-local-connection pattern as the site app's history.py.
"""
import os
import sqlite3
import threading
import time

DATA_DIR = os.environ.get("HUB_DATA", "/data")
DB_PATH = os.path.join(DATA_DIR, "hub.db")

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
        CREATE TABLE IF NOT EXISTS site_samples (
            site_id        TEXT NOT NULL,
            ts             INTEGER NOT NULL,
            reachable      INTEGER NOT NULL,
            http_ms        REAL,
            devices_total  INTEGER,
            devices_online INTEGER,
            watched_down   INTEGER,
            kuma_up        INTEGER,
            kuma_down      INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_site_samples ON site_samples(site_id, ts);
        """
    )
    c.commit()


def record(site_id, reachable, http_ms=None, devices_total=None,
           devices_online=None, watched_down=None, kuma_up=None, kuma_down=None):
    c = _conn()
    c.execute(
        "INSERT INTO site_samples (site_id, ts, reachable, http_ms, devices_total,"
        " devices_online, watched_down, kuma_up, kuma_down) VALUES (?,?,?,?,?,?,?,?,?)",
        (site_id, int(time.time()), 1 if reachable else 0, http_ms, devices_total,
         devices_online, watched_down, kuma_up, kuma_down),
    )
    c.commit()


def reachability_pct(site_id, window_s):
    c = _conn()
    since = int(time.time()) - window_s
    row = c.execute(
        "SELECT AVG(reachable)*100.0 AS pct, COUNT(*) AS n FROM site_samples"
        " WHERE site_id=? AND ts>=?",
        (site_id, since),
    ).fetchone()
    if not row or not row["n"]:
        return None
    return round(row["pct"], 1)


def summary(site_id):
    return {
        "reach_24h": reachability_pct(site_id, 86400),
        "reach_7d": reachability_pct(site_id, 7 * 86400),
        "reach_30d": reachability_pct(site_id, 30 * 86400),
        "last_reachable": last_reachable(site_id),
    }


def last_reachable(site_id):
    c = _conn()
    row = c.execute(
        "SELECT ts FROM site_samples WHERE site_id=? AND reachable=1"
        " ORDER BY ts DESC LIMIT 1", (site_id,)
    ).fetchone()
    return row["ts"] if row else None


def series(site_id, window_s=86400, buckets=48):
    """Return `buckets` reachable-fraction values across the window for a sparkline."""
    c = _conn()
    now = int(time.time())
    since = now - window_s
    step = max(1, window_s // buckets)
    rows = c.execute(
        "SELECT ts, reachable FROM site_samples WHERE site_id=? AND ts>=? ORDER BY ts",
        (site_id, since),
    ).fetchall()
    out = [None] * buckets
    agg = {}
    for r in rows:
        b = min(buckets - 1, (r["ts"] - since) // step)
        s, n = agg.get(b, (0, 0))
        agg[b] = (s + r["reachable"], n + 1)
    for b, (s, n) in agg.items():
        out[b] = round(s / n, 3) if n else None
    return out


def prune(retention_days):
    c = _conn()
    cutoff = int(time.time()) - retention_days * 86400
    c.execute("DELETE FROM site_samples WHERE ts < ?", (cutoff,))
    c.commit()
