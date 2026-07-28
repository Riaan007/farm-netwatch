"""Ubiquiti airOS (airMAX) radios over SSH: read Wi-Fi status, change the
management IP.

get_wifi() / get_network() only read. set_ip() is EXPERIMENTAL and writes:


airOS has no clean documented API like Hikvision's ISAPI, so this edits the
running config (/tmp/system.cfg) over SSH and persists it: it rewrites the
`netconf.N.ip` (+ netmask) entries that currently hold the radio's management
IP, updates the default-route gateway, then `cfgmtd -w` + reboot.

This is a blunt instrument on WIRELESS BACKHAUL — a wrong value can drop the
link to a whole building. It is gated behind a Settings feature flag and uses
the device's saved SSH credentials. Uses the system ssh client via sshpass (no
extra Python deps), with legacy algorithms enabled for older airOS firmware.
"""
import json
import re
import subprocess

CFG_PATH = "/tmp/system.cfg"

_SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "ConnectTimeout=8",
    "-o", "NumberOfPasswordPrompts=1",
    # older airOS firmware speaks legacy key/host algorithms
    "-o", "HostKeyAlgorithms=+ssh-rsa",
    "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa",
]


def _ssh(ip, user, password, command, timeout=20, stdin_data=None):
    """Run a remote command over SSH (password auth via sshpass).
    Returns (rc, stdout, stderr); rc=255 is SSH/connection failure."""
    cmd = ["sshpass", "-p", password, "ssh", *_SSH_OPTS,
           f"{user}@{ip}", command]
    try:
        r = subprocess.run(cmd, input=stdin_data, capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except (OSError, subprocess.SubprocessError) as e:
        return 255, "", str(e)


def _read_cfg(ip, user, password):
    rc, out, err = _ssh(ip, user, password, f"cat {CFG_PATH}")
    if rc == 0 and "netconf." in out:
        return out
    return None


def _kv(text):
    d = {}
    for line in text.splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def get_network(ip, user, password):
    """Read the radio's current management IP / netmask / gateway over SSH."""
    if not ip:
        return {"ok": False, "error": "no IP"}
    if not (user and password):
        return {"ok": False, "error": "save the radio's SSH username/password first"}
    text = _read_cfg(ip, user, password)
    if text is None:
        return {"ok": False, "error": "SSH read failed — check credentials / SSH access"}
    kv = _kv(text)
    # the netconf entry whose .ip equals the address we reached = the mgmt iface
    idx = next((m.group(1) for k, v in kv.items()
                if (m := re.match(r"netconf\.(\d+)\.ip$", k)) and v == ip), None)
    mask = kv.get(f"netconf.{idx}.netmask", "") if idx else ""
    gw = next((v for k, v in kv.items() if re.match(r"route\.\d+\.gateway$", k) and v), "")
    return {"ok": True, "ipAddress": ip, "subnetMask": mask, "gateway": gw,
            "interface_index": idx}


# ---------------------------------------------------------------------------
# Read-only radio / Wi-Fi status
# ---------------------------------------------------------------------------
# Everything below only READS. It is the safe half of this module: same SSH
# path and same saved credentials as the IP change, so a successful Wi-Fi
# fetch also proves the credentials and SSH access work — worth doing BEFORE
# trusting set_ip() with a backhaul radio.

_WSTA_MARK = "===WSTA==="
_CFG_MARK = "===CFG==="
_DUMP_MARK = "===DUMP==="

# Field names drift between airOS 5.x / 6.x / XM / XW builds, so every value is
# looked up through a list of candidates (matched case-insensitively) instead of
# one hard-coded key. The full raw dump is returned as well, so anything not
# mapped here is still visible in the UI.
_FIELDS = {
    "deviceName":  ["devicename", "hostname"],
    "firmware":    ["firmwareversion", "fwversion", "version"],
    "platform":    ["platform", "devmodel", "productname"],
    "mode":        ["wlanopmode", "opmode", "wlanmode", "mode"],
    "ssid":        ["wlanssid", "ssid", "essid"],
    "frequency":   ["wlanfreq", "freq", "frequency"],
    "channelWidth": ["wlanchanbw", "chanbw", "channelwidth"],
    "signal":      ["wlanrssi", "signal", "wlansignal", "rssi"],
    "noise":       ["wlannoise", "noise", "noisef"],
    "ccq":         ["wlanccq", "ccq"],
    "txRate":      ["wlantxrate", "txrate", "txrateraw"],
    "rxRate":      ["wlanrxrate", "rxrate", "rxrateraw"],
    "txPower":     ["wlantxpower", "txpower"],
    "distance":    ["wlandistance", "distance"],
    "apMac":       ["wlanapmac", "apmac", "wlanbssid", "bssid"],
    "security":    ["wlansecurity", "security"],
    "connections": ["wlanconnections", "connections"],
    "lanSpeed":    ["lanspeed", "lan_speed"],
    "uptime":      ["uptime"],
    "cpuLoad":     ["cpuload", "loadavg"],
    "temperature": ["temperature"],
}

_MODES = {"ap": "Access Point", "sta": "Station", "ap-wds": "Access Point (WDS)",
          "sta-wds": "Station (WDS)", "aprepeater": "AP Repeater"}


def _status_kv(text):
    """Parse an mca-status dump. Some firmware emits one key=value per line,
    others cram the first block onto a single comma-separated line — accept both.
    Keys are lower-cased for the candidate lookup in _FIELDS."""
    d = {}
    for line in text.replace("\r", "").splitlines():
        for part in line.split(","):
            part = part.strip()
            if "=" in part and not part.startswith("#"):
                k, v = part.split("=", 1)
                k = k.strip()
                if k:
                    d[k.lower()] = v.strip()
    return d


def _pick(kv, names):
    for n in names:
        v = kv.get(n)
        if v not in (None, ""):
            return v
    return ""


def _stations(blob):
    """Normalise `wstalist` JSON (the connected-station table on an AP; on a
    station it describes the AP it is associated with). Missing/!JSON = []."""
    blob = (blob or "").strip()
    if not blob.startswith("["):
        return []
    try:
        raw = json.loads(blob)
    except ValueError:
        return []
    out = []
    for s in raw if isinstance(raw, list) else []:
        if not isinstance(s, dict):
            continue
        remote = s.get("remote") if isinstance(s.get("remote"), dict) else {}
        out.append({
            "mac": s.get("mac", ""),
            "ip": s.get("lastip") or remote.get("ipaddr", ""),
            "name": s.get("name") or remote.get("hostname", ""),
            "signal": s.get("signal", ""),
            "noise": s.get("noisefloor", s.get("noise", "")),
            "ccq": s.get("ccq", ""),
            "tx": s.get("tx", ""),
            "rx": s.get("rx", ""),
            "distance": s.get("distance", ""),
            "uptime": s.get("uptime", ""),
        })
    return out


def _rate_mbps(v):
    """UniFi reports station rates in kbps; airOS in Mbps. Normalise to Mbps."""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return ""
    return round(n / 1000) if n > 2000 else round(n)


def _from_mca_dump(blob):
    """Normalise UniFi's `mca-dump` JSON (UniFi APs have no mca-status/system.cfg).
    Returns the same shape as the airOS path, or None if this isn't a UniFi dump."""
    blob = (blob or "").strip()
    start = blob.find("{")
    if start < 0:
        return None
    try:
        d = json.loads(blob[start:])
    except ValueError:
        return None
    if not isinstance(d, dict) or "radio_table" not in d and "vap_table" not in d:
        return None

    radios = [r for r in d.get("radio_table", []) if isinstance(r, dict)]
    # "user" VAPs are the SSIDs people connect to; the rest are mesh/uplink.
    vaps = [v for v in d.get("vap_table", []) if isinstance(v, dict)]
    user_vaps = [v for v in vaps if v.get("usage", "user") == "user"] or vaps

    networks, stations = [], []
    for v in vaps:
        networks.append({
            "essid": v.get("essid", ""),
            "band": v.get("radio", ""),
            "channel": v.get("channel", ""),
            "clients": v.get("num_sta", 0),
            "usage": v.get("usage", ""),
        })
        for s in v.get("sta_table", []) if isinstance(v.get("sta_table"), list) else []:
            if not isinstance(s, dict):
                continue
            stations.append({
                "mac": s.get("mac", ""),
                "ip": s.get("ip", ""),
                "name": s.get("hostname") or s.get("name") or "",
                "ssid": v.get("essid", ""),
                "signal": s.get("signal", s.get("rssi", "")),
                "noise": s.get("noise", ""),
                "ccq": s.get("ccq", ""),
                "tx": _rate_mbps(s.get("tx_rate")),
                "rx": _rate_mbps(s.get("rx_rate")),
                "distance": "",
                "uptime": s.get("uptime", ""),
            })

    return {
        "source": "unifi",
        "deviceName": d.get("hostname", ""),
        "platform": d.get("model_display") or d.get("model", ""),
        "firmware": d.get("version", ""),
        "mode": "ap",
        "mode_label": "Access Point (UniFi)",
        "ssid": ", ".join(v.get("essid", "") for v in user_vaps if v.get("essid")),
        "frequency": " · ".join(f"{r.get('radio', '?')} ch{r.get('channel', '?')}" for r in radios),
        "channelWidth": " · ".join(str(r.get("ht", "")) for r in radios if r.get("ht")),
        "txPower": " · ".join(str(r.get("tx_power", "")) for r in radios if r.get("tx_power")),
        "signal": "", "noise": "", "ccq": "", "txRate": "", "rxRate": "",
        "distance": "", "apMac": "", "security": "", "country": "",
        "connections": sum(int(v.get("num_sta", 0) or 0) for v in vaps),
        "lanSpeed": "", "uptime": d.get("uptime", ""),
        "cpuLoad": (d.get("sys_stats") or {}).get("loadavg_1", ""),
        "temperature": "",
        "networks": networks,
        "stations": stations,
        "raw": {k: v for k, v in d.items() if not isinstance(v, (list, dict))},
    }


def get_wifi(ip, user, password):
    """Read a radio's wireless status over SSH — SSID, mode, frequency, signal,
    rates and the connected-station list. Read-only: no config is touched.

    One SSH session runs every read (a separate connection per command costs a
    full handshake each, which is slow over a marginal radio link). `mca-status`
    + `wstalist` cover airOS/airMAX; the saved config is the fallback for
    SSID/mode on older firmware; `mca-dump` covers UniFi APs, which have none
    of the three. Whichever answers wins — the caller gets one shape either way.
    """
    if not ip:
        return {"ok": False, "error": "no IP"}
    if not (user and password):
        return {"ok": False, "error": "save the radio's SSH username/password first"}
    cmd = (f"mca-status 2>/dev/null; echo '{_WSTA_MARK}'; wstalist 2>/dev/null; "
           f"echo '{_CFG_MARK}'; cat {CFG_PATH} 2>/dev/null; "
           f"echo '{_DUMP_MARK}'; mca-dump 2>/dev/null")
    rc, out, err = _ssh(ip, user, password, cmd, timeout=30)
    if rc != 0 and not out:
        detail = (err or "").strip().splitlines()
        hint = detail[-1] if detail else f"rc={rc}"
        return {"ok": False, "error": f"SSH read failed — check credentials / SSH access ({hint})"}

    status_txt, _, rest = out.partition(_WSTA_MARK)
    wsta_txt, _, rest = rest.partition(_CFG_MARK)
    cfg_txt, _, dump_txt = rest.partition(_DUMP_MARK)
    kv = _status_kv(status_txt)
    cfg = _kv(cfg_txt)

    if not kv and "netconf." not in cfg_txt:
        unifi = _from_mca_dump(dump_txt)
        if unifi:
            unifi.update({"ok": True, "ip": ip})
            return unifi

    res = {k: _pick(kv, names) for k, names in _FIELDS.items()}
    # Fall back to the saved config for the identity fields mca-status may not
    # have given us (older firmware, or mca-status missing entirely).
    res["ssid"] = res["ssid"] or cfg.get("wireless.1.ssid", "")
    res["mode"] = res["mode"] or cfg.get("wireless.1.mode", "")
    res["security"] = res["security"] or cfg.get("wireless.1.security.type", "")
    res["channelWidth"] = res["channelWidth"] or cfg.get("radio.1.chanbw", "")
    res["country"] = cfg.get("radio.1.countrycode", "")
    res["mode_label"] = _MODES.get(res["mode"].strip().lower(), res["mode"])
    res["stations"] = _stations(wsta_txt)
    res["networks"] = []
    res["source"] = "airos"
    if not (kv or cfg):
        return {"ok": False, "error": "logged in over SSH, but the device returned no "
                                      "wireless status (mca-status / mca-dump missing — "
                                      "not an airOS or UniFi radio?)"}
    res["ok"] = True
    res["ip"] = ip
    res["raw"] = kv        # everything mca-status reported, for the details view
    return res


def _edit_cfg(text, cur_ip, new_ip, mask, gateway):
    """Rewrite the mgmt IP everywhere netconf.*.ip holds it; update its netmask
    and the default gateway. Returns (new_text, changes[])."""
    out, changes = [], []
    for line in text.splitlines():
        m = re.match(r"(netconf\.(\d+)\.ip)=(.*)$", line)
        if m and m.group(3).strip() == cur_ip:
            out.append(f"{m.group(1)}={new_ip}")
            changes.append(f"{m.group(1)}: {cur_ip} -> {new_ip}")
            continue
        if mask:
            mm = re.match(r"(netconf\.\d+\.netmask)=(.*)$", line)
            # only touch netmask lines on an iface that had the mgmt IP — approximate
            # by updating any netmask whose value differs; safe for single-subnet radios
            if mm and mm.group(2).strip() != mask and _same_iface_as_ip(text, mm.group(1), cur_ip):
                out.append(f"{mm.group(1)}={mask}")
                changes.append(f"{mm.group(1)}: {mm.group(2).strip()} -> {mask}")
                continue
        if gateway:
            gm = re.match(r"(route\.\d+\.gateway)=(.*)$", line)
            if gm and gm.group(2).strip() != gateway:
                out.append(f"{gm.group(1)}={gateway}")
                changes.append(f"{gm.group(1)}: {gm.group(2).strip()} -> {gateway}")
                continue
        out.append(line)
    return "\n".join(out) + "\n", changes


def _same_iface_as_ip(text, netmask_key, cur_ip):
    idx = netmask_key.split(".")[1]
    return f"netconf.{idx}.ip={cur_ip}" in text


def set_ip(ip, user, password, new_ip, mask="", gateway=""):
    """Change the radio's management IP via SSH config edit + reboot. Returns
    {'ok', 'new_ip', 'changes'} or {'ok': False, 'error'}."""
    if not (ip and new_ip):
        return {"ok": False, "error": "missing current or new IP"}
    if not (user and password):
        return {"ok": False, "error": "save the radio's SSH username/password first"}
    text = _read_cfg(ip, user, password)
    if text is None:
        return {"ok": False, "error": "SSH read failed — check credentials / SSH access"}
    new_text, changes = _edit_cfg(text, ip, new_ip, mask, gateway)
    if not any(c.startswith("netconf.") and ".ip:" in c for c in changes):
        return {"ok": False, "error": f"could not find the management IP ({ip}) in the "
                f"radio's config — it may be on DHCP or an unusual layout"}
    # write the edited config back over stdin (avoids shell-quoting the whole file)
    rc, _out, err = _ssh(ip, user, password, f"cat > {CFG_PATH}", stdin_data=new_text)
    if rc != 0:
        return {"ok": False, "error": f"writing config failed (rc {rc}) {err[:120]}"}
    # persist to flash and reboot to apply (connection drops as it goes down)
    _ssh(ip, user, password, "cfgmtd -w -p /etc/ ; reboot", timeout=12)
    return {"ok": True, "new_ip": new_ip, "changes": changes,
            "msg": f"Radio is rebooting to apply {new_ip} (back in ~1–2 min)."}
