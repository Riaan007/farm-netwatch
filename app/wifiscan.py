"""Other Wi-Fi around a tower: a site survey run from one STATION radio on it.

Riaan, 2026-10-27: "there is also other WiFi equipment around us — show the best
frequency for my devices". Only a radio can hear them, and a radio that scans
leaves its own channel while it listens, so:

  * only a STATION scans, never an access point (an AP scanning would drop every
    station on it); only that one link blinks, for roughly 10-30 s;
  * only when someone presses Scan (no schedule) — Riaan's choice;
  * the scan runs ON THE RADIO with `bgnd`, writing to /tmp, because Netwatch
    often reaches the station through the very link that drops while it scans.
    Netwatch then reads the result over a fresh SSH session and checks the
    station is associated again.

Results are kept per radio (wifiscan.json) and read by freqplan.py, which places
the other networks on each tower's lane and keeps moves away from loud ones.
Nothing here changes a setting on a radio.
"""
import json
import os
import re
import threading
import time

import airos

DATA_DIR = os.environ.get("NETWATCH_DATA", "/data")
STORE = os.path.join(DATA_DIR, "wifiscan.json")
_TMP = "/tmp/nwscan"
WAIT_S = 100          # longest the scan + reconnect may take before we give up following it
POLL_S = 6
KEEP_DAYS = 60

# Standard 5 GHz 40/80 MHz blocks: a wide neighbour reported by its primary channel
# sits in one of these.
_BLOCKS = {40: [(5170 + 40 * i, 5210 + 40 * i) for i in range(8)] + [(5490 + 40 * i, 5530 + 40 * i) for i in range(6)]
           + [(5735, 5775), (5775, 5815)],
           80: [(5170, 5250), (5250, 5330), (5490, 5570), (5570, 5650), (5650, 5730), (5735, 5815)],
           160: [(5170, 5330), (5490, 5650)]}


class ScanError(Exception):
    pass


# ---- parsing -----------------------------------------------------------------
def _band(freq, width, mode):
    """(lo, hi) a neighbour occupies, from its primary channel and width."""
    m = (mode or "").upper()
    if width == 40 and "PLUS" in m:
        return freq - 10, freq + 30
    if width == 40 and "MINUS" in m:
        return freq - 30, freq + 10
    for lo, hi in _BLOCKS.get(width, []):
        if lo <= freq - 10 and freq + 10 <= hi:
            return lo, hi
    return freq - width / 2, freq + width / 2


def parse(text):
    """`iwlist <if> scanning` → [{bssid, ssid, freq, width, width_known, lo, hi, signal}]."""
    out = []
    for cell in re.split(r"\n\s*Cell \d+ - ", "\n" + (text or ""))[1:]:
        g = lambda rx: (lambda m: m.group(1) if m else None)(re.search(rx, cell, re.I))  # noqa: E731
        bssid = (g(r"Address:\s*([0-9A-F:]{17})") or "").upper()
        f = g(r"Frequency[:=]\s*([\d.]+)\s*GHz")
        freq = round(float(f) * 1000) if f else None
        if freq is None:
            mhz = g(r"Frequency[:=]\s*(\d{4})\s*MHz")
            freq = int(mhz) if mhz else None
        if not bssid or not freq or not 4900 <= freq <= 6000:
            continue                                  # 2.4 GHz or unreadable: not our band
        sig = g(r"Signal level[:=]\s*(-?\d+)")
        mode = g(r"ieee_mode=(\S+)") or g(r"(VHT\d+|HT\d+\S*)") or ""
        w = re.search(r"(?:VHT|HT)(\d+)", mode.upper())
        width = int(w.group(1)) if w else None
        lo, hi = _band(freq, width or 20, mode)
        out.append({"bssid": bssid, "ssid": g(r'ESSID:"([^"]*)"') or "", "freq": freq,
                    "width": width or 20, "width_known": bool(width), "lo": lo, "hi": hi,
                    "signal": int(sig) if sig else None})
    return out


# ---- store -------------------------------------------------------------------
_lock = threading.Lock()


def load():
    try:
        with open(STORE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, STORE)


def remember(key, rec):
    with _lock:
        d = load()
        cut = time.time() - KEEP_DAYS * 86400
        d = {k: v for k, v in d.items() if (v or {}).get("ts", 0) > cut}
        d[key] = rec
        _save(d)


# ---- running a scan ------------------------------------------------------------
def script():
    """sh for busybox airOS: scan in the background, then say DONE. Writes only /tmp."""
    return f"""#!/bin/sh
ST={_TMP}.state; OUT={_TMP}.out
: > $ST; : > $OUT
echo START >> $ST
IF=ath0; [ -d /sys/class/net/ath0 ] || IF=wifi0
iwlist $IF scanning > $OUT 2>&1; rc=$?
echo "DONE $rc" >> $ST
"""


_jobs, _jobs_lock = {}, threading.Lock()


def state(key):
    with _jobs_lock:
        return dict(_jobs.get(key) or {})


def running(key):
    return state(key).get("phase") in ("starting", "scanning", "reading")


def _set(key, **kw):
    with _jobs_lock:
        _jobs.setdefault(key, {}).update(kw, ts=int(time.time()))


def _mode_and_scanlist(ip, user, pw):
    rc, out, err = airos._ssh(ip, user, pw, f"mca-status 2>/dev/null; echo '{airos._CFG_MARK}'; "
                                            f"cat {airos.CFG_PATH} 2>/dev/null", timeout=30)
    if rc != 0 and not out:
        low = (err or "").lower()
        raise ScanError("the saved login was rejected" if "denied" in low else "the radio did not answer over SSH")
    status, _, cfg_txt = out.partition(airos._CFG_MARK)
    kv, cfg = airos._status_kv(status), airos._kv(cfg_txt)
    if "netconf." not in cfg_txt:
        raise ScanError("logged in, but this does not look like an airOS radio")
    mode = (airos._pick(kv, airos._FIELDS["mode"]) or cfg.get("wireless.1.mode", "")).lower()
    scan = cfg.get("radio.1.scan_list.channels", "") if cfg.get("radio.1.scan_list.status") == "enabled" else ""
    return mode, scan


def _run(target, login, on_done):
    key, ip = target["key"], target["ip"]
    user, pw = login["username"], login["password"]
    try:
        mode, scan_list = _mode_and_scanlist(ip, user, pw)
        if mode.startswith("ap") or mode == "master":
            raise ScanError("this radio is an access point — scanning would drop every station on it. "
                            "Scan from a station on the same tower instead")
        rc, _o, err = airos._ssh(ip, user, pw, f"cat > {_TMP}.sh && chmod 700 {_TMP}.sh && rm -f {_TMP}.state && "
                                               f"bgnd -r nwscan -- /bin/sh {_TMP}.sh && echo STARTED",
                                 stdin_data=script(), timeout=30)
        if rc != 0:
            raise ScanError(f"could not start the scan on the radio ({(err or '').strip()[:120] or rc})")
        _set(key, phase="scanning", msg="Scanning — this link drops for a few seconds…")
        deadline, text, verdict = time.time() + WAIT_S, "", ""
        while time.time() < deadline:
            time.sleep(POLL_S)
            rc, out, _e = airos._ssh(ip, user, pw, f"cat {_TMP}.state 2>/dev/null", timeout=15)
            if rc != 0:
                _set(key, msg="Radio not answering — expected while it scans…")
                continue
            last = [ln for ln in out.splitlines() if ln.strip()][-1:] or [""]
            if last[0].startswith("DONE"):
                verdict = last[0]
                break
        if not verdict:
            raise ScanError("the scan did not finish in time — the station should come back by itself; "
                            "try again later")
        _set(key, phase="reading", msg="Reading what it heard…")
        rc, text, _e = airos._ssh(ip, user, pw, f"cat {_TMP}.out 2>/dev/null; rm -f {_TMP}.sh {_TMP}.out {_TMP}.state",
                                  timeout=30)
        nets = parse(text)
        if not nets and verdict != "DONE 0":
            raise ScanError("the radio could not scan: " + ((text or "").strip().splitlines() or ["no output"])[-1][:160])
        rc, wsta, _e = airos._ssh(ip, user, pw, "wstalist 2>/dev/null", timeout=20)
        back = rc == 0 and '"remote"' in (wsta or "")
        rec = {"ts": int(time.time()), "radio": key, "name": target.get("name") or ip, "networks": nets,
               "scan_list": scan_list, "raw_tail": (text or "")[-1500:] if not nets else ""}
        remember(key, rec)
        note = (f" It only looks at its Frequency Scan List ({scan_list.replace(',', ', ')} MHz), so it may "
                f"miss networks elsewhere." if scan_list else "")
        _set(key, phase="done", ok=True, found=len(nets), back=back,
             msg=f"Heard {len(nets)} network{'s' if len(nets) != 1 else ''} in 5 GHz."
                 + ("" if back else " The station has not reconnected yet — check it in a minute.") + note)
    except ScanError as e:
        _set(key, phase="done", ok=False, msg=str(e))
    except Exception as e:  # noqa: BLE001 - a job thread must always finish its state
        _set(key, phase="done", ok=False, msg=f"{e.__class__.__name__}: {e}")
    if on_done:
        try:
            on_done(target, state(key))
        except Exception:  # noqa: BLE001
            pass


def start(target, login, on_done=None):
    key = target["key"]
    with _jobs_lock:
        if (_jobs.get(key) or {}).get("phase") in ("starting", "scanning", "reading"):
            return None
        _jobs[key] = {"phase": "starting", "msg": "Checking the radio is a station…", "ts": int(time.time())}
    threading.Thread(target=_run, args=(target, login, on_done), daemon=True, name=f"wifiscan-{key}").start()
    return state(key)
