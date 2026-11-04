"""Minutes someone logged in to this Pi was actively using its page — for the hub's
time keeping (hub/app/timekeeping.py, source "site").

The site page sends a beat about once a minute while a person is using it (input in
the last two minutes, tab visible: static/activity.js). A beat counts only from a
session logged in with this Pi's password, and not when the page came through the
hub's proxy (the hub already counts those minutes from its own access log). So what
lands here is work done on the Pi directly: its LAN address on site, the VPN, or
its own port forward.

Only minute stamps are kept (/data/user_activity.json, last KEEP_DAYS days). The hub
reads them with its key: GET /api/activity?since=<epoch>.
"""
import json
import os
import threading
import time

DATA_DIR = os.environ.get("NETWATCH_DATA", "/data")
PATH = os.path.join(DATA_DIR, "user_activity.json")
KEEP_DAYS = 7

_lock = threading.Lock()
_minutes = None


def _load():
    global _minutes
    if _minutes is None:
        try:
            with open(PATH) as fh:
                _minutes = {int(m) for m in json.load(fh).get("minutes", [])}
        except (FileNotFoundError, ValueError, TypeError, AttributeError):
            _minutes = set()
    return _minutes


def _save(ms):
    tmp = PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"minutes": sorted(ms)}, fh)
    os.replace(tmp, PATH)


def beat(now=None):
    """Record the current minute. Returns True when it was new."""
    now = int(now or time.time())
    m = now // 60 * 60
    with _lock:
        ms = _load()
        if m in ms:
            return False
        ms.add(m)
        cut = now - KEEP_DAYS * 86400
        for old in [x for x in ms if x < cut]:
            ms.discard(old)
        try:
            _save(ms)
        except OSError:
            pass                       # kept in memory; written with the next beat
        return True


def since(ts):
    with _lock:
        return sorted(m for m in _load() if m >= ts)
