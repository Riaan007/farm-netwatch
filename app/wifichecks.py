"""Answer the Wi-Fi page's "From the office" check steps on the spot.

A diagnosis step such as "Check Output Power on both radios" carries a
`check` ({kinds, radios}); the page's "Check now" button sends it here and gets
the answer read straight off the radios. READ-ONLY: one SSH session per radio
reads mca-status, the saved config, /proc/uptime and the tail of the log —
nothing on the radio is changed.

Kinds:
  power     transmit power now + as configured (and antenna gain / cable loss);
            with two radios, how far apart they are
  uptime    how long since the radio last started (a short uptime = it rebooted)
  log       the radio's recent wireless/link/power log lines
  settings  wireless mode (incl. airMAX Mixed), frequency, channel width,
            frequency scan list, country, firmware
  login     only whether the saved SSH login works
"""
import concurrent.futures
import re
import threading

import airos

KINDS = ("power", "uptime", "log", "settings", "login")
MAX_RADIOS = 8

_MARKS = ("===NW-CFG===", "===NW-UP===", "===NW-LOG===")
# Lines worth showing from airOS's /var/log/messages
_LOG_RE = re.compile(r"wlan|ath\d|wifi|link|assoc|auth|radar|dfs|channel|reboot|restart|"
                     r"watchdog|power|eth0.*(up|down)|kernel: .*(err|fail)", re.I)
_busy = set()
_busy_lock = threading.Lock()


def _num(v):
    try:
        return float(str(v).split()[0])
    except (ValueError, IndexError, TypeError):
        return None


def _dur(s):
    s = int(s)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    return f"{d} d {h} h" if d else f"{h} h {m} min" if h else f"{m} min"


def _read(ip, user, password):
    """One SSH session → (ok, error, kv, cfg, uptime_s, log_lines)."""
    cmd = (f"mca-status 2>/dev/null; echo '{_MARKS[0]}'; cat {airos.CFG_PATH} 2>/dev/null; "
           f"echo '{_MARKS[1]}'; cat /proc/uptime 2>/dev/null; "
           f"echo '{_MARKS[2]}'; tail -n 400 /var/log/messages 2>/dev/null")
    rc, out, err = airos._ssh(ip, user, password, cmd, timeout=30)
    if rc != 0 and not out:
        detail = (err or "").strip().splitlines()
        return False, (detail[-1] if detail else f"rc={rc}"), {}, {}, None, []
    status, _, rest = out.partition(_MARKS[0])
    cfg_txt, _, rest = rest.partition(_MARKS[1])
    up_txt, _, log_txt = rest.partition(_MARKS[2])
    up = _num(up_txt.strip())
    lines = [ln.strip() for ln in log_txt.splitlines() if ln.strip() and _LOG_RE.search(ln)]
    return True, "", airos._status_kv(status), airos._kv(cfg_txt), up, lines


def _fact(label, value, level=""):
    return {"label": label, "value": value, "level": level}


def _facts(kind, kv, cfg, up, lines):
    pick = lambda name: airos._pick(kv, airos._FIELDS[name])
    if kind == "power":
        out, now = [], _num(pick("txPower"))
        conf = _num(cfg.get("radio.1.txpower"))
        gain, loss = _num(cfg.get("radio.1.antenna.gain")), _num(cfg.get("radio.1.cable.loss"))
        out.append(_fact("Transmit power now", f"{now:.0f} dBm" if now is not None else "not reported"))
        if conf is not None:
            out.append(_fact("Set in airOS", f"{conf:.0f} dBm"))
        if gain is not None:
            eirp = (conf if conf is not None else now or 0) + gain - (loss or 0)
            out.append(_fact("Antenna gain / cable loss", f"{gain:.0f} dBi / {loss or 0:.0f} dB → about {eirp:.0f} dBm EIRP"))
        obey = cfg.get("radio.1.obey")
        if obey:
            out.append(_fact("Country power limits", "obeyed" if obey == "enabled" else "NOT obeyed",
                             "" if obey == "enabled" else "warn"))
        return out, now
    if kind == "uptime":
        if up is None:
            return [_fact("Running for", "not reported")], None
        lvl = "bad" if up < 3600 else "warn" if up < 86400 else "ok"
        note = " — it restarted recently (power cut, reset or crash)" if up < 86400 else ""
        return [_fact("Running for", _dur(up) + note, lvl)], up
    if kind == "log":
        if not lines:
            return [_fact("Recent log", "nothing about the radio link in the log")], None
        return [_fact("Recent log (radio clock may be wrong)", "\n".join(lines[-12:]))], None
    if kind == "settings":
        mode = pick("mode") or cfg.get("wireless.1.mode", "")
        mixed = cfg.get("radio.1.polling_11ac_11n_compat") == "1"
        freq = pick("frequency")
        if freq.isdigit():
            freq += " MHz"
        scan = cfg.get("radio.1.scan_list.channels") if cfg.get("radio.1.scan_list.status") == "enabled" else ""
        out = [_fact("Wireless mode", airos._mode_label(mode) or mode or "—"),
               _fact("airMAX mode", "Mixed (older M radios allowed)" if mixed else "AC only", "warn" if mixed else ""),
               _fact("Frequency", freq or "—"),
               _fact("Channel width", (pick("channelWidth") or cfg.get("radio.1.chanbw", "") or "—")
                     + (" MHz" if (pick("channelWidth") or cfg.get("radio.1.chanbw", "")).isdigit() else "")),
               _fact("Frequency scan list", (scan.replace(",", ", ") + " MHz") if scan else "all frequencies")]
        if cfg.get("radio.1.countrycode"):
            out.append(_fact("Country code", cfg["radio.1.countrycode"]))
        if pick("firmware"):
            out.append(_fact("Firmware", pick("firmware")))
        return out, None
    if kind == "login":
        return [_fact("Saved SSH login", "works", "ok")], None
    return [], None


def check_radio(dev, login, kinds):
    """dev: {key, ip, name}; login: {username, password} or None."""
    res = {"key": dev["key"], "name": dev.get("name") or dev.get("ip") or dev["key"],
           "ip": dev.get("ip") or ""}
    if not res["ip"]:
        return {**res, "ok": False, "error": "no IP address known for this radio"}
    if not login:
        return {**res, "ok": False, "error": "no SSH login saved for this radio (Logins)"}
    with _busy_lock:
        if dev["key"] in _busy:
            return {**res, "ok": False, "error": "a check on this radio is already running"}
        _busy.add(dev["key"])
    try:
        ok, err, kv, cfg, up, lines = _read(res["ip"], login.get("username", ""), login.get("password", ""))
    finally:
        with _busy_lock:
            _busy.discard(dev["key"])
    if not ok:
        low = err.lower()
        hint = ("the saved login was rejected" if "denied" in low or "auth" in low else
                "SSH is switched off or this is not an airOS radio" if "refused" in low else
                "the radio did not answer over SSH")
        return {**res, "ok": False, "error": f"{hint} ({err})"}
    if not kv and not cfg:
        return {**res, "ok": False, "error": "logged in, but it gave no airOS status — not an airOS radio?"}
    facts, values = [], {}
    for k in kinds:
        f, v = _facts(k, kv, cfg, up, lines)
        facts += f
        values[k] = v
    return {**res, "ok": True, "facts": facts, "values": values}


def run(kinds, devices, login_for):
    """kinds: subset of KINDS; devices: [{key, ip, name}]; login_for(key) -> creds or None."""
    kinds = [k for k in KINDS if k in set(kinds or [])]
    if not kinds:
        raise ValueError("no known check kind")
    if not devices:
        raise ValueError("no radios to check")
    devices = devices[:MAX_RADIOS]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(devices)) as ex:
        results = list(ex.map(lambda d: check_radio(d, login_for(d["key"]), kinds), devices))
    summary = []
    pw = [(r["name"], r["values"]["power"]) for r in results
          if r.get("ok") and r["values"].get("power") is not None]
    if "power" in kinds and len(pw) >= 2:
        hi, lo = max(pw, key=lambda t: t[1]), min(pw, key=lambda t: t[1])
        gap = hi[1] - lo[1]
        summary.append(_fact("Transmit power difference",
                             f"{gap:.0f} dB ({hi[0]} {hi[1]:.0f} dBm, {lo[0]} {lo[1]:.0f} dBm)",
                             "warn" if gap >= 6 else "ok"))
    return {"ok": True, "kinds": kinds, "results": results, "summary": summary}
