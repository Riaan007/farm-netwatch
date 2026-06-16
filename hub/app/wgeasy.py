"""Thin client for wg-easy's HTTP API (the same API its own web UI uses).

The hub shares wg-easy's network namespace, so the API is always reachable at
http://localhost:51821. Auth: POST the admin password (WG_PASSWORD env — the
plain text matching WG_PASSWORD_HASH) to /api/session for a session cookie.

Used by the add-site wizard: create a client, read its assigned 10.8.0.x
address, download its wg0.conf, and delete the client when a site is removed.
"""
import os
import re

import requests

BASE = os.environ.get("WGEASY_URL", "http://localhost:51821")
TIMEOUT = (3, 15)   # createClient rewrites + syncs the wg config; allow a moment


class WgEasyError(Exception):
    pass


def _session():
    pw = os.environ.get("WG_PASSWORD", "")
    if not pw:
        raise WgEasyError("WG_PASSWORD is not set — add it to hub/.env "
                          "(plain text of WG_PASSWORD_HASH) and restart the hub")
    s = requests.Session()
    try:
        r = s.post(f"{BASE}/api/session", json={"password": pw}, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise WgEasyError(f"cannot reach wg-easy: {e.__class__.__name__}")
    if r.status_code == 401:
        raise WgEasyError("wg-easy rejected WG_PASSWORD (does it match the hash?)")
    if not r.ok:
        raise WgEasyError(f"wg-easy login failed: HTTP {r.status_code}")
    return s


def _clients(s):
    r = s.get(f"{BASE}/api/wireguard/client", timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def list_clients():
    return _clients(_session())


def create_client(name):
    """Create a client and return its record (wg-easy assigns the next free IP).

    POST returns only {success}, so the new client is found by name in the
    list, newest first (names are not unique in wg-easy).
    """
    s = _session()
    r = s.post(f"{BASE}/api/wireguard/client", json={"name": name}, timeout=TIMEOUT)
    if not r.ok:
        raise WgEasyError(f"create client failed: HTTP {r.status_code}")
    matches = [c for c in _clients(s) if c.get("name") == name]
    if not matches:
        raise WgEasyError("client created but not found in the list")
    return max(matches, key=lambda c: c.get("createdAt") or "")


def get_configuration(client_id):
    s = _session()
    r = s.get(f"{BASE}/api/wireguard/client/{client_id}/configuration",
              timeout=TIMEOUT)
    if not r.ok:
        raise WgEasyError(f"download configuration failed: HTTP {r.status_code}")
    return r.text


def get_client(client_id):
    return next((c for c in list_clients() if c.get("id") == client_id), None)


def remote_config(client_id, allowed_ips):
    """A client .conf rewritten for a road-warrior laptop/phone: replace the (global,
    split) AllowedIPs with `allowed_ips` so the device routes the office LAN + VPN
    subnet (or 0.0.0.0/0 for full tunnel). Endpoint/keys are untouched. The change is
    client-side only — the wg-easy server doesn't care what a client routes."""
    conf = get_configuration(client_id)
    out, replaced = [], False
    for line in conf.splitlines():
        if re.match(r"\s*AllowedIPs\s*=", line, re.IGNORECASE):
            out.append(f"AllowedIPs = {allowed_ips}")
            replaced = True
        else:
            out.append(line)
    if not replaced:                       # no AllowedIPs line — add one under [Peer]
        out2 = []
        for line in out:
            out2.append(line)
            if line.strip() == "[Peer]":
                out2.append(f"AllowedIPs = {allowed_ips}")
        out = out2
    return "\n".join(out).strip() + "\n"


def delete_client(client_id):
    s = _session()
    r = s.delete(f"{BASE}/api/wireguard/client/{client_id}", timeout=TIMEOUT)
    if not r.ok:
        raise WgEasyError(f"delete client failed: HTTP {r.status_code}")
    return True
