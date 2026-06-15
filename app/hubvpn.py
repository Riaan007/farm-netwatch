"""Browser-driven WireGuard client — joins this site to the central hub.

Netwatch runs with `network_mode: host` + NET_ADMIN, so it can own the tunnel
itself via `wg-quick` (no separate sidecar container, no SSH, and the site opens
NO inbound ports — the tunnel dials OUT to the hub).

The config is the same one wg-easy hands out on the hub. We store it at the same
path the wg-client sidecar used (data/wg-client/wg_confs/wg0.conf) so the two are
interchangeable, but only ONE may own wg0 at a time — a browser-joined site should
NOT also enable the `wg-client` compose profile.
"""
import base64
import os
import re
import subprocess

DATA_DIR = os.environ.get("NETWATCH_DATA", "/data")
WG_DIR = os.path.join(DATA_DIR, "wg-client", "wg_confs")
WG_CONF = os.path.join(WG_DIR, "wg0.conf")
IFACE = "wg0"


def _run(args, timeout=20):
    """Run a command, returning (rc, stdout+stderr). Never raises."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, f"{args[0]}: not found (is wireguard-tools installed?)"
    except subprocess.TimeoutExpired:
        return 124, f"{args[0]}: timed out"
    except Exception as e:  # noqa: BLE001 - surface anything else to the UI
        return 1, str(e)


def _sanitize(conf_text):
    """Normalise a pasted wg config so wg-quick can bring it up in a container.

    Drops the `DNS =` line: wg-quick shells out to `resolvconf` for it, which
    isn't present in the image and makes the bring-up fail. Split-tunnel sites
    (AllowedIPs = 10.8.0.0/24) don't need the tunnel's DNS anyway.
    """
    out = []
    for line in conf_text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if re.match(r"\s*DNS\s*=", line, re.IGNORECASE):
            continue
        out.append(line.rstrip())
    text = "\n".join(out).strip() + "\n"
    return text


def _b64_to_conf(b64):
    """Decode a base64 blob to a wg config, or '' if it isn't one."""
    try:
        dec = base64.b64decode(b64).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""
    return dec if "[Interface]" in dec else ""


def _extract_config(raw):
    """Accept whatever the hub gives the user and return a raw wg config.

    The hub's Add-Site wizard emits a one-paste BOOTSTRAP command, not a raw
    config — so users naturally paste that here. Pull the real config out of it:
      * already a raw config  -> use as-is
      * a bootstrap command with WG_CONF_B64="..."  -> decode that
      * a bare base64 blob of a config             -> decode it
    """
    raw = (raw or "").strip()
    if "[Interface]" in raw and "[Peer]" in raw:
        return raw
    m = re.search(r"WG_CONF_B64\s*=\s*[\"']?([A-Za-z0-9+/=]+)", raw)
    if m:
        dec = _b64_to_conf(m.group(1))
        if dec:
            return dec
    compact = re.sub(r"\s+", "", raw)
    if compact and re.fullmatch(r"[A-Za-z0-9+/=]+", compact):
        dec = _b64_to_conf(compact)
        if dec:
            return dec
    return raw


def _valid(conf_text):
    return "[Interface]" in conf_text and "[Peer]" in conf_text \
        and "PrivateKey" in conf_text and "Endpoint" in conf_text


def has_config():
    return os.path.isfile(WG_CONF)


def wg_address():
    """This site's own wg0 IPv4 (bare, no /mask), parsed from wg0.conf — or None.
    Used to bind relay listeners to the VPN address (see tunnels.py)."""
    if not has_config():
        return None
    try:
        with open(WG_CONF) as f:
            m = re.search(r"Address\s*=\s*([0-9.]+)", f.read())
            return m.group(1).strip() if m else None
    except OSError:
        return None


def is_up():
    rc, out = _run(["wg", "show", IFACE])
    return rc == 0 and out.strip() != ""


def save_config(conf_text):
    """Validate + sanitise + persist the wg config. Returns (ok, message).

    Accepts a raw config, the hub's one-paste bootstrap command, or a base64
    blob — whatever the user copied from the hub.
    """
    conf_text = _extract_config(conf_text)
    if not _valid(conf_text):
        return False, ("That doesn't look like a WireGuard config. Paste the config "
                       "from the hub's Add Site (or its install command — either works).")
    text = _sanitize(conf_text)
    os.makedirs(WG_DIR, exist_ok=True)
    # Write 0600 — the file holds the private key.
    fd = os.open(WG_CONF, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return True, "Config saved."


def up():
    """(Re)bring the tunnel up from the stored config. Returns (ok, message)."""
    if not has_config():
        return False, "No hub config saved yet."
    # Generous timeouts: wg-quick resolves the hub's (DDNS) endpoint and waits on
    # the first handshake — on a slow Pi/link this can take well over 20s. Killing
    # it early leaves a half-configured wg0 ("config saved, no handshake").
    _run(["wg-quick", "down", WG_CONF], timeout=30)  # graceful: restores routes/dns
    # Force-remove any stale/half-up interface a previous attempt may have left.
    # `wg-quick down` can't clean those up (given a bare name it looks for
    # /etc/wireguard/wg0.conf, which we don't use), so wg-quick up would fail
    # with "'wg0' already exists" forever. `ip link delete` always clears it.
    _run(["ip", "link", "delete", IFACE], timeout=10)
    rc, out = _run(["wg-quick", "up", WG_CONF], timeout=60)
    if rc == 124:
        return False, ("wg-quick timed out bringing the tunnel up — usually a DNS "
                       "or routing issue resolving the hub endpoint. Check the site "
                       "can reach the hub's WireGuard port (UDP 51820).")
    if rc != 0:
        return False, out or "wg-quick up failed."
    return True, "Tunnel up."


def down():
    rc, out = _run(["wg-quick", "down", WG_CONF], timeout=30)
    if rc != 0 and "is not a" not in out.lower():
        return False, out or "wg-quick down failed."
    return True, "Tunnel down."


def forget():
    """Bring the tunnel down and delete the stored config + keys."""
    down()
    try:
        os.remove(WG_CONF)
    except FileNotFoundError:
        pass
    return True, "Hub link removed."


def _ping(host="10.8.0.1"):
    rc, _ = _run(["ping", "-c", "1", "-W", "2", host], timeout=6)
    return rc == 0


def status():
    """Snapshot for the dashboard's Central Hub card."""
    info = {
        "configured": has_config(),
        "connected": False,
        "address": None,        # this site's 10.8.0.x
        "endpoint": None,       # the hub's public endpoint
        "last_handshake_s": None,
        "rx_bytes": None,
        "tx_bytes": None,
        "hub_reachable": False,
        "tools_available": _run(["wg", "--version"])[0] == 0,
    }
    if info["configured"]:
        try:
            with open(WG_CONF) as f:
                m = re.search(r"Address\s*=\s*([0-9./]+)", f.read())
                if m:
                    info["address"] = m.group(1).strip()
        except OSError:
            pass

    rc, out = _run(["wg", "show", IFACE, "dump"])
    if rc == 0 and out:
        lines = out.split("\n")
        # First line = interface; peer lines follow. dump peer fields:
        # pubkey presharedkey endpoint allowed-ips latest-handshake rx tx keepalive
        if len(lines) >= 2:
            f = lines[1].split("\t")
            if len(f) >= 7:
                info["endpoint"] = f[2] if f[2] != "(none)" else None
                hs = int(f[4]) if f[4].isdigit() else 0
                if hs > 0:
                    import time
                    info["last_handshake_s"] = max(0, int(time.time()) - hs)
                    info["connected"] = info["last_handshake_s"] < 180
                info["rx_bytes"] = int(f[5]) if f[5].isdigit() else 0
                info["tx_bytes"] = int(f[6]) if f[6].isdigit() else 0
    if info["connected"]:
        info["hub_reachable"] = _ping()
    return info


def boot():
    """Called once at startup: re-assert the tunnel if a config is present.

    Keeps the site joined across container/host restarts (the job the sidecar
    container used to do for terminal-joined sites).
    """
    if has_config():
        up()
