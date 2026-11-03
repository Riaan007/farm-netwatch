#!/usr/bin/env python3
"""Netwatch development time from Claude Code sessions -> the hub's time keeping.

Runs on the HOST (the transcripts live in ~/.claude/projects, the hub container
can't see them) from cron every 15 minutes:

    */15 * * * * flock -n /tmp/nw-devtime.lock python3 ~/farm-netwatch/hub/tools/devtime.py >> ~/.local/state/netwatch-devtime.log 2>&1

What counts: every prompt-and-answer TURN of a session that touched Netwatch — the
session was working in ~/farm-netwatch, or Claude's own messages/tool calls in
that turn name it (farm-netwatch, netwatch-hub, /opt/netwatch, "Netwatch"). All
minutes of such a turn are Netwatch time; the hub joins them into sessions (pause
up to `dev_gap_min`) and shows them as drafts to review.

Only minute stamps leave this script — never transcript text. Reading is
incremental (byte offset per file in ~/.local/state/netwatch-devtime.json);
minutes the hub has not taken yet stay in the state's outbox and are re-sent.
"""
import datetime as dt
import glob
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.expanduser("~/.claude/projects")
STATE = os.path.expanduser("~/.local/state/netwatch-devtime.json")
LOOKBACK_DAYS = 45
NW_CWD = os.path.expanduser("~/farm-netwatch")
NW_TEXT = re.compile(r"farm-netwatch|netwatch-hub|/opt/netwatch|netwatch", re.I)
HUB = ["docker", "exec", "-i", "netwatch-hub", "python", "timekeeping.py", "import-dev"]


def load_state():
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return {"files": {}, "outbox": []}


def save_state(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(st, fh)
    os.replace(tmp, STATE)


def _ts(s):
    try:
        return int(dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()) // 60 * 60
    except ValueError:
        return None


def scan_file(path, fs, emit):
    """Read new complete lines of one transcript; fs = its saved state."""
    with open(path, "rb") as fh:
        fh.seek(fs.get("offset", 0))
        while True:
            line = fh.readline()
            if not line or not line.endswith(b"\n"):
                break                                  # partial last line: next run
            fs["offset"] = fh.tell()
            if b'"type":"user"' not in line and b'"type":"assistant"' not in line:
                continue
            try:
                j = json.loads(line)
            except ValueError:
                continue
            kind = j.get("type")
            if kind not in ("user", "assistant") or j.get("isSidechain"):
                continue
            pid = j.get("promptId")
            if kind == "user" and pid and pid != fs.get("turn"):
                fs.update(turn=pid, nw=False, pending=[])
            m = _ts(j.get("timestamp"))
            if m is None:
                continue
            if not fs.get("nw"):
                cwd = str(j.get("cwd") or "")
                if cwd.startswith(NW_CWD) or (kind == "assistant" and NW_TEXT.search(line.decode("utf-8", "ignore"))):
                    fs["nw"] = True
                    emit.extend(fs.get("pending") or [])
                    fs["pending"] = []
            if fs.get("nw"):
                emit.append(m)
            else:
                p = fs.setdefault("pending", [])
                if not p or p[-1] != m:
                    p.append(m)


def main():
    st = load_state()
    cutoff = time.time() - LOOKBACK_DAYS * 86400
    emit = []
    for path in glob.glob(os.path.join(ROOT, "*", "*.jsonl")):
        try:
            sz, mt = os.path.getsize(path), os.path.getmtime(path)
        except OSError:
            continue
        fs = st["files"].get(path)
        if fs is None:
            if mt < cutoff:
                continue
            fs = st["files"][path] = {"offset": 0}
        if sz < fs.get("offset", 0):
            fs.clear()
            fs["offset"] = 0                           # rewritten: start over (marks are idempotent)
        if sz == fs.get("offset"):
            continue
        scan_file(path, fs, emit)
    for path in [p for p in st["files"] if not os.path.exists(p)]:
        del st["files"][path]
    out = sorted(set(st.get("outbox", [])) | {m for m in emit if m >= cutoff})
    st["outbox"] = out
    if DRY:
        days = {}
        for m in out:
            days.setdefault(dt.date.fromtimestamp(m).isoformat(), []).append(m)
        for d, ms in sorted(days.items()):
            print(f"{d}: {len(ms)} active minutes, {dt.datetime.fromtimestamp(ms[0]):%H:%M}–"
                  f"{dt.datetime.fromtimestamp(ms[-1]):%H:%M}")
        return 0
    save_state(st)                                     # offsets advance only with the outbox saved
    if not out:
        return 0
    try:
        r = subprocess.run(HUB, input=json.dumps({"minutes": out}).encode(), capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"{dt.datetime.now():%F %T} hub not reached ({e}); {len(out)} minutes kept for next run", flush=True)
        return 1
    if r.returncode != 0:
        print(f"{dt.datetime.now():%F %T} hub import failed: {r.stderr.decode()[-300:]}", flush=True)
        return 1
    st["outbox"] = []
    save_state(st)
    print(f"{dt.datetime.now():%F %T} sent {len(out)} minutes: {r.stdout.decode().strip()}", flush=True)
    return 0


DRY = "--dry-run" in sys.argv          # print minutes per day; no state written, nothing sent

if __name__ == "__main__":
    if DRY:
        STATE = "/nonexistent/netwatch-devtime.json"
    sys.exit(main())
