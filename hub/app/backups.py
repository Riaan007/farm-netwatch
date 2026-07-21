"""Per-site config backups, stored on the hub.

The poller pulls each site's /api/config/export once a day (and on demand from
the site page) into /data/backups/<site_id>/<utc-ts>.json, keeping the newest
KEEP copies. A bundle contains everything needed to rebuild the site on a new
Pi — settings, device registry, obfuscated logins + key, and the wg config —
so treat the store like the wg-easy keys living in the same volume.
"""
import json
import os
import re
import time

import requests

BACKUP_DIR = os.path.join(os.environ.get("HUB_DATA", "/data"), "backups")
KEEP = 14
_NAME_RE = re.compile(r"^[0-9]{8}-[0-9]{6}\.json$")


class BackupError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def _dir(site_id):
    return os.path.join(BACKUP_DIR, re.sub(r"[^A-Za-z0-9_-]", "-", site_id))


def store(site, timeout):
    """Pull the site's export bundle and persist it. Returns the list entry."""
    base = f"http://{site['vpn_ip']}:{site.get('netwatch_port', 8090)}"
    try:
        r = requests.get(f"{base}/api/config/export", timeout=timeout)
    except requests.RequestException as e:
        raise BackupError(f"site unreachable: {e.__class__.__name__}")
    if r.status_code == 404:
        raise BackupError("this site's Netwatch is too old for backups — "
                          "update it (docker compose pull)", 501)
    try:
        r.raise_for_status()
        bundle = r.json()
    except (requests.RequestException, ValueError):
        raise BackupError("site returned a bad export")
    if bundle.get("kind") != "netwatch-backup":
        raise BackupError("site returned a bad export")

    d = _dir(site["id"])
    os.makedirs(d, exist_ok=True)
    name = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + ".json"
    tmp = os.path.join(d, name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(bundle, f)
    os.replace(tmp, os.path.join(d, name))
    for old in sorted(os.listdir(d))[:-KEEP]:
        if _NAME_RE.match(old):
            try:
                os.remove(os.path.join(d, old))
            except OSError:
                pass
    print(f"[backups] stored {site['id']}/{name}", flush=True)
    return {"name": name, "ts": int(time.time()),
            "bytes": os.path.getsize(os.path.join(d, name))}


def list_(site_id):
    d = _dir(site_id)
    out = []
    try:
        names = sorted(os.listdir(d), reverse=True)
    except OSError:
        return out
    for n in names:
        if not _NAME_RE.match(n):
            continue
        p = os.path.join(d, n)
        try:
            out.append({"name": n, "bytes": os.path.getsize(p),
                        "ts": int(os.path.getmtime(p))})
        except OSError:
            pass
    return out


def read(site_id, name):
    """Bundle bytes, or None. `name` must match the timestamp pattern (no
    traversal); name=None -> newest."""
    if name is None:
        entries = list_(site_id)
        if not entries:
            return None, None
        name = entries[0]["name"]
    if not _NAME_RE.match(name):
        return None, None
    try:
        with open(os.path.join(_dir(site_id), name), "rb") as f:
            return f.read(), name
    except OSError:
        return None, None
