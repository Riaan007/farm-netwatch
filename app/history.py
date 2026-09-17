"""Uptime history in SQLite (/data/netwatch.db).

Every scan records one sample per known device key (MAC when on the local
segment, else IP). Rollups power the dashboard uptime %% and sparklines.
"""
import os
import sqlite3
import threading
import time
from array import array

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

        -- Ubiquiti EdgeSwitch / UISP switch telemetry (see switchmon.py): one row
        -- per switch per poll, and one per PORT — a failing cable, a dying PoE
        -- camera or a flapping uplink shows up on its port long before the
        -- device behind it drops off the network.
        CREATE TABLE IF NOT EXISTS switch_samples (
            key     TEXT NOT NULL,
            ts      INTEGER NOT NULL,
            cpu     REAL,
            ram     REAL,
            temp    REAL,
            poe_w   REAL,
            ports_up INTEGER,
            rx_bps  REAL,
            tx_bps  REAL,
            uptime  REAL
        );
        CREATE INDEX IF NOT EXISTS idx_sw_key_ts ON switch_samples(key, ts);

        CREATE TABLE IF NOT EXISTS switch_ports (
            key     TEXT NOT NULL,          -- the switch
            ts      INTEGER NOT NULL,
            port    TEXT NOT NULL,          -- '0/1'
            up      INTEGER,
            speed   INTEGER,                -- Mbps, NULL when down
            poe_w   REAL,
            rx_bps  REAL,
            tx_bps  REAL,
            errors  REAL,                   -- cumulative counters as the switch reports them
            dropped REAL,
            macs    INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_swp_key_ts ON switch_ports(key, ts);
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


# ---- switch telemetry --------------------------------------------------------
_SW_COLS = ("cpu", "ram", "temp", "poe_w", "ports_up", "rx_bps", "tx_bps", "uptime")
_SWP_COLS = ("port", "up", "speed", "poe_w", "rx_bps", "tx_bps", "errors", "dropped", "macs")


def switch_record(key, sample, ports, ts=None):
    ts = int(ts or time.time())
    c = _conn()
    c.execute(f"INSERT INTO switch_samples (key, ts, {','.join(_SW_COLS)}) "
              f"VALUES (?,?,{','.join('?' * len(_SW_COLS))})",
              (key, ts, *(sample.get(col) for col in _SW_COLS)))
    if ports:
        c.executemany(f"INSERT INTO switch_ports (key, ts, {','.join(_SWP_COLS)}) "
                      f"VALUES (?,?,{','.join('?' * len(_SWP_COLS))})",
                      [(key, ts, *(p.get(col) for col in _SWP_COLS)) for p in ports])
    c.commit()


def switch_series(key, window_s=86400, limit=5000):
    since = int(time.time()) - window_s
    rows = _conn().execute(
        "SELECT * FROM switch_samples WHERE key=? AND ts>=? ORDER BY ts DESC LIMIT ?",
        (key, since, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def switch_port_series(key, window_s=86400, port=None, limit=50000):
    since = int(time.time()) - window_s
    sql, args = "SELECT * FROM switch_ports WHERE key=? AND ts>=?", [key, since]
    if port:
        sql += " AND port=?"
        args.append(port)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in reversed(_conn().execute(sql, args).fetchall())]


def switch_port_baseline(key, port, window_s=7 * 86400, settle_s=3600):
    """How a port USUALLY looks, ignoring the last hour: share of polls with a
    link, the most common speed while up, and the median PoE draw while it drew
    any. {} when there is too little history to judge."""
    now = int(time.time())
    rows = _conn().execute(
        "SELECT up, speed, poe_w FROM switch_ports WHERE key=? AND port=? AND ts>=? AND ts<=?",
        (key, port, now - window_s, now - settle_s)).fetchall()
    if not rows:
        return {}
    ups = [r["up"] for r in rows if r["up"] is not None]
    speeds = {}
    for r in rows:
        if r["up"] and r["speed"]:
            speeds[r["speed"]] = speeds.get(r["speed"], 0) + 1
    poes = sorted(r["poe_w"] for r in rows if r["poe_w"] and r["poe_w"] >= 0.5)
    return {
        "n": len(rows),
        "up_share": (sum(1 for u in ups if u) / len(ups)) if ups else None,
        "speed": max(speeds, key=speeds.get) if speeds else None,
        "poe_w": poes[len(poes) // 2] if poes else None,
        "poe_share": len(poes) / len(rows),
    }


def switch_keys(window_s=7 * 86400):
    since = int(time.time()) - window_s
    rows = _conn().execute("SELECT key, MAX(ts) AS ts FROM switch_samples WHERE ts>=? GROUP BY key",
                           (since,)).fetchall()
    return {r["key"]: r["ts"] for r in rows}


def switch_prune(retention_days):
    cutoff = int(time.time()) - int(retention_days) * 86400
    c = _conn()
    c.execute("DELETE FROM switch_samples WHERE ts < ?", (cutoff,))
    c.execute("DELETE FROM switch_ports WHERE ts < ?", (cutoff,))
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


# Forgetting devices deletes their rows in short batches. A prune at a farm is
# ~a million rows; as one transaction it held the write lock for the whole
# delete, and the scan and heartbeat writers give up after 10 s (timeout=10).
PURGE_ROWS = (200, 1000, 50000)   # rows per batch: least, first, most
PURGE_TARGET_S = 0.5              # batch size follows how long a batch takes
PURGE_PAUSE_S = 0.05              # minimum rest between batches
_purge_lock = threading.Lock()


def delete_keys(keys, before=None):
    """Drop the uptime samples + heartbeats of devices being forgotten (Forget,
    prune) — only rows up to `before` (epoch s, default now), so a device that
    comes back while this runs keeps its new samples.

    Batches go in table (rowid) order: a scan writes every device's row side by
    side, so going device by device rewrites each page once per device on it
    (45x the disk writes on a farm-sized test). Each batch is committed on its
    own and followed by a rest at least as long as it took, so the write lock
    is never held for long and other writers always get a turn. Slow on a big
    prune: callers run it off the request thread. Deliberately leaves the
    `events` audit log intact so a forgotten device's history is preserved.
    Returns the number of rows deleted.
    """
    keys = list(dict.fromkeys(keys))
    if not keys:
        return 0
    before = int(time.time() if before is None else before)
    c = _conn()
    most = min(PURGE_ROWS[2], c.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) - 501)  # + keys + ts
    least, size = (min(n, most) for n in PURGE_ROWS[:2])
    total = 0
    with _purge_lock:          # one purge at a time
        for table in ("samples", "heartbeats"):
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                match = f"key IN ({','.join('?' * len(chunk))}) AND ts <= ?"
                ids = array("q")
                cur = c.execute(f"SELECT rowid FROM {table} WHERE {match} ORDER BY rowid",
                                (*chunk, before))
                for rows in iter(lambda: cur.fetchmany(10000), []):
                    ids.extend(r[0] for r in rows)
                done = 0
                while done < len(ids):
                    part = ids[done:done + size]
                    t0 = time.monotonic()
                    try:
                        # NOT INDEXED: look the rows up by rowid, never walk the key index
                        total += c.execute(
                            f"DELETE FROM {table} NOT INDEXED WHERE rowid IN "
                            f"({','.join('?' * len(part))}) AND {match}",
                            (*part, *chunk, before)).rowcount
                        c.commit()
                    except Exception:
                        c.rollback()
                        raise
                    done += len(part)
                    took = time.monotonic() - t0
                    if took < PURGE_TARGET_S / 2:
                        size = min(size * 2, most)
                    elif took > PURGE_TARGET_S * 2:
                        size = max(size // 2, least)
                    time.sleep(max(PURGE_PAUSE_S, took))
    return total


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


def event_summary(since=None, top=8):
    """{"counts": {type: n}, "total": n, "busiest": [{ip, events, devices, types}]}"""
    where, args = "", []
    if since:
        where, args = " WHERE ts >= ?", [int(since)]
    c = _conn()
    counts = {r["type"]: r["n"] for r in c.execute(
        "SELECT type, COUNT(*) AS n FROM events" + where + " GROUP BY type", args)}
    busy_where = (where + " AND" if where else " WHERE") + " ip IS NOT NULL AND ip <> ''"
    busiest = []
    for r in c.execute(
            "SELECT ip, COUNT(*) AS n, COUNT(DISTINCT key) AS devs, GROUP_CONCAT(DISTINCT type) AS types "
            "FROM events" + busy_where + " GROUP BY ip ORDER BY n DESC LIMIT ?", args + [int(top)]):
        busiest.append({"ip": r["ip"], "events": r["n"], "devices": r["devs"],
                        "types": sorted((r["types"] or "").split(","))})
    return {"counts": counts, "total": sum(counts.values()), "busiest": busiest}


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
