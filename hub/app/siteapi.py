"""The hub's per-site API key — how the hub proves itself to a site's Netwatch.

A site locks saved device logins, its config export/import and its settings
behind a login (app/siteauth.py). The hub gets through with the header
`X-Netwatch-Hub-Key`. Each site has its own random key, kept in
/data/site_keys.json — a separate file on purpose, so the many
load-modify-save paths on hub.json can never drop a key (a lost key locks the
hub out of that Pi until someone resets it on the Pi).

Claiming is automatic: every status poll reports the site's `auth` booleans;
a site without a key is sent ours (it only accepts that from 10.8.0.1, the
hub's VPN address). Old site images have no `auth` block and ignore the header.

Per-site state for the dashboard, `snap["api_auth"]`:
  ok        the site accepts our key
  claimed   we just claimed it
  legacy    site image predates the login (update it)
  mismatch  the site holds a DIFFERENT key — reset it on the Pi:
            docker exec netwatch python siteauth.py reset-hub-key
  refused   the site would not accept the claim (e.g. a LAN-registered site
            that the hub does not reach from 10.8.0.1 — set the key by hand)
"""
import json
import os
import secrets
import threading
import time

import requests

DATA_DIR = os.environ.get("HUB_DATA", "/data")
KEYS_PATH = os.path.join(DATA_DIR, "site_keys.json")
HEADER = "X-Netwatch-Hub-Key"
CLAIM_RETRY_S = 300

_lock = threading.Lock()
_last_claim = {}          # site id -> ts of last claim attempt


def _load():
    try:
        with open(KEYS_PATH) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def key_for(site_id):
    with _lock:
        keys = _load()
        if not keys.get(site_id):
            keys[site_id] = secrets.token_urlsafe(32)
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = KEYS_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(keys, f, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, KEYS_PATH)
        return keys[site_id]


def headers(site, extra=None):
    h = {HEADER: key_for(site["id"])}
    h.update(extra or {})
    return h


def base_url(site):
    return f"http://{site['vpn_ip']}:{site.get('netwatch_port', 8090)}"


def reconcile(site, status, timeout):
    """Given a fresh /api/status (fetched WITH our header), return the api_auth
    state, claiming the site first when it has no key."""
    auth = (status or {}).get("auth")
    if not isinstance(auth, dict):
        return "legacy"
    if auth.get("hub"):
        return "ok"
    if auth.get("hub_key_set"):
        return "mismatch"
    now = time.time()
    if now - _last_claim.get(site["id"], 0) < CLAIM_RETRY_S:
        return "refused"
    _last_claim[site["id"]] = now
    try:
        r = requests.post(base_url(site) + "/api/auth/claim-hub",
                          json={"key": key_for(site["id"])}, timeout=timeout)
    except requests.RequestException:
        return "refused"
    if r.status_code == 200:
        print(f"[siteapi] {site['id']}: claimed the site's API key", flush=True)
        return "claimed"
    print(f"[siteapi] {site['id']}: claim refused ({r.status_code})", flush=True)
    return "mismatch" if r.status_code == 409 else "refused"
