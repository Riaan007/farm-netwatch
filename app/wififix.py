"""Safe Wi-Fi fixes on airOS radios: preview → confirm → apply with auto-undo.

A wrong setting on a wireless backhaul can strand a whole site, so every fix
follows the recipe that worked on SwartRigens (Tankwa, 2026-09-14):

  1. PREVIEW reads the radio and builds a plan: which config keys change, from
     what to what, how many peers (stations, or the AP for a station) must be
     back afterwards, and what could go wrong. Nothing is written. The plan gets
     a short-lived token.
  2. APPLY (only with that token and confirm) re-reads the radio — the plan is
     refused if anything changed since the preview — uploads a small script and
     starts it ON THE RADIO with `bgnd`, so it keeps going when the SSH session
     (or the link Netwatch reaches it through) drops:
       backup /tmp/system.cfg → edit only the planned keys →
       `rc.softrestart test` (applies WITHOUT writing flash) →
       wait until enough peers are associated again, and still are 20 s later →
       only then write flash (`cfgmtd -w`).
       Not back in time → restore the backup + `rc.softrestart test` again.
     A reboot or a hang half-way falls back to the untouched flash.
  3. Netwatch follows the script's state file over fresh SSH sessions and then
     reads the radio once more to confirm (mode, frequency, peers).

Only keys that already exist in the radio's config AND match what the radio
reports live are edited, so a firmware that stores things differently is
refused at preview instead of being guessed at. Values are integers chosen by
the plan, never free text.

Fixes:
  ac_mode   AP in airMAX Mixed mode while every station is AC → AC only
  power     set this radio's transmit power to the other end's (match them)
  channel   move an AP to another centre frequency (and optionally width);
            refused unless every station's Frequency Scan List includes it
"""
import re
import secrets
import threading
import time

import airos

FIXES = {
    "ac_mode": {"label": "Switch the AP to airMAX AC only",
                "warn": "The AP restarts its wireless for about 30 seconds — every station on it, and everything "
                        "behind them, drops and reconnects. If they do not all come back within 2 minutes the "
                        "radio puts Mixed mode back by itself."},
    "power": {"label": "Match transmit power",
              "warn": "The radio restarts its wireless for about 30 seconds. If the link is not back within "
                      "2 minutes the old power is put back by itself."},
    "channel": {"label": "Move the AP to another channel",
                "warn": "Every station on this AP drops and must find the new channel. A DFS channel can take "
                        "over a minute to come up. If they are not all back within the wait, the radio moves "
                        "back to the old channel by itself."},
}
_DFS = ((5250, 5350), (5470, 5730))          # radar-checked bands (centre MHz, wide margins)
PLAN_TTL_S = 300
POLL_S = 6
_TMP = "/tmp/nwfix"
_SAFE_KEY = re.compile(r"^radio\.1\.[a-z0-9_.]+$")


class FixError(Exception):
    pass


def _int(v):
    try:
        return int(float(str(v).split()[0]))
    except (ValueError, IndexError, TypeError):
        return None


def is_dfs(mhz):
    return any(lo <= mhz <= hi for lo, hi in _DFS)


# ---- reading ---------------------------------------------------------------
def read(ip, user, pw):
    """One SSH session: live status, saved config, peers."""
    rc, out, err = airos._ssh(ip, user, pw, f"mca-status 2>/dev/null; echo '{airos._CFG_MARK}'; "
                                            f"cat {airos.CFG_PATH} 2>/dev/null; echo '{airos._WSTA_MARK}'; "
                                            f"wstalist 2>/dev/null", timeout=30)
    if rc != 0 and not out:
        low = (err or "").lower()
        why = ("the saved login was rejected" if "denied" in low else "the radio did not answer over SSH")
        raise FixError(f"{why} ({(err or '').strip().splitlines()[-1] if err.strip() else rc})")
    status, _, rest = out.partition(airos._CFG_MARK)
    cfg_txt, _, wsta = rest.partition(airos._WSTA_MARK)
    kv, cfg = airos._status_kv(status), airos._kv(cfg_txt)
    if "netconf." not in cfg_txt:
        raise FixError("logged in, but could not read the radio's config — not an airOS radio?")
    return {"kv": kv, "cfg": cfg, "peers": airos._stations(wsta, cfg.get("wireless.1.ssid", ""))}


def _live(r, name):
    return airos._pick(r["kv"], airos._FIELDS[name])


def _is_ap(r):
    mode = (_live(r, "mode") or r["cfg"].get("wireless.1.mode", "")).lower()
    return mode.startswith("ap") or mode == "master"


def _edit(cfg, key, new, want_old=None):
    """A planned edit, refused if the key is missing (firmware layout differs)."""
    if not _SAFE_KEY.match(key):
        raise FixError(f"refusing to touch {key}")
    if key not in cfg:
        raise FixError(f"this radio's config has no {key} — its firmware stores this differently; "
                       f"change it in airOS by hand")
    old = cfg[key]
    if want_old is not None and str(want_old) != old:
        raise FixError(f"{key} is {old} in the config but the radio reports {want_old} — "
                       f"unsaved changes on the radio? Save or discard them in airOS first")
    return {"key": key, "old": old, "new": str(int(new))}


# ---- planning --------------------------------------------------------------
def plan(kind, target, login_for, find_device=None, params=None):
    """Build a plan (nothing written). target: {key, ip, name}; login_for(key)
    -> {username, password} or None; find_device(mac=, ip=) -> {key, ip, name}
    for a peer radio Netwatch knows; params: per-kind options."""
    if kind not in FIXES:
        raise FixError("unknown fix")
    params = params or {}
    login = login_for(target["key"])
    if not login:
        raise FixError(f"no SSH login saved for {target['name']}")
    r = read(target["ip"], login["username"], login["password"])
    cfg, peers = r["cfg"], r["peers"]
    ap = _is_ap(r)
    out = {"kind": kind, "label": FIXES[kind]["label"], "warn": FIXES[kind]["warn"],
           "target": target, "edits": [], "lines": [], "notes": [], "baseline": len(peers),
           "min_peers": max(1, len(peers)), "wait_s": 120}
    if not peers:
        raise FixError(f"{target['name']} has no wireless peer connected right now — nothing to prove a "
                       f"change against, so Netwatch will not change it")

    if kind == "ac_mode":
        if not ap:
            raise FixError(f"{target['name']} is not an access point")
        if cfg.get("radio.1.polling_11ac_11n_compat") != "1":
            raise FixError(f"{target['name']} is already in airMAX AC mode")
        old_models = [p.get("model") or "?" for p in peers if "ac" not in (p.get("model") or "").lower()]
        if old_models:
            raise FixError("these stations are not AC radios and need Mixed mode: " + ", ".join(old_models))
        out["edits"].append(_edit(cfg, "radio.1.polling_11ac_11n_compat", 0))
        out["lines"] = ["airMAX mode: Mixed → AC only",
                        f"{len(peers)} station{'s' if len(peers) != 1 else ''} must reconnect: "
                        + ", ".join(p.get("name") or p.get("ip") or p.get("mac") for p in peers)]

    elif kind == "power":
        other = params.get("match")
        if not other:
            raise FixError("which radio's power should be matched?")
        ol = login_for(other["key"])
        if not ol:
            raise FixError(f"no SSH login saved for {other['name']}")
        o = read(other["ip"], ol["username"], ol["password"])
        mine, theirs = _int(cfg.get("radio.1.txpower")), _int(o["cfg"].get("radio.1.txpower"))
        if mine is None or theirs is None:
            raise FixError("transmit power is not in the radio's config (automatic power?) — set it in airOS by hand")
        if mine == theirs:
            raise FixError(f"both are already set to {mine} dBm")
        if not 1 <= theirs <= 30:
            raise FixError(f"{other['name']}'s power ({theirs} dBm) is outside what Netwatch will set")
        if cfg.get("radio.1.obey", "enabled") != "enabled":
            raise FixError(f"{target['name']} does not obey the country power limits — set its power in airOS by hand")
        live = _int(_live(r, "txPower"))
        out["edits"].append(_edit(cfg, "radio.1.txpower", theirs))
        out["lines"] = [f"Transmit power on {target['name']}: {mine} → {theirs} dBm (same as {other['name']})"]
        if live is not None and live != mine:
            out["notes"].append(f"The radio transmits {live} dBm now (country limit or automatic power), "
                                f"so the real change may be smaller")
        if theirs > mine:
            out["notes"].append("Raising power can add interference for other links on this tower")

    elif kind == "channel":
        if not ap:
            raise FixError(f"{target['name']} is not an access point — a station follows its AP's channel")
        new = _int(params.get("freq"))
        if new is None or not 4900 <= new <= 6100:
            raise FixError("give the new centre frequency in MHz (5 GHz band)")
        cur = _int(cfg.get("radio.1.freq"))
        live = _int(_live(r, "frequency"))
        out["edits"].append(_edit(cfg, "radio.1.freq", new, want_old=live if live and cur and live == cur else None))
        if new == cur:
            raise FixError(f"{target['name']} is already on {new} MHz")
        width = _int(params.get("width"))
        if width:
            if width not in (10, 20, 30, 40, 50, 60, 80):
                raise FixError("channel width must be 10, 20, 30, 40, 50, 60 or 80 MHz")
            if str(width) != cfg.get("radio.1.chanbw"):
                out["edits"].append(_edit(cfg, "radio.1.chanbw", width))
        out["lines"] = [f"Centre frequency: {cur} → {new} MHz"
                        + (f", width {cfg.get('radio.1.chanbw')} → {width} MHz" if width and str(width) != cfg.get("radio.1.chanbw") else "")]
        if is_dfs(new):
            out["wait_s"] = 300
            out["notes"].append(f"{new} MHz is a DFS (radar-checked) channel: the AP listens for about a minute "
                                f"before it transmits, so the wait is 5 minutes")
        blocked, unread = [], []
        for p in peers:
            dev = find_device(mac=p.get("mac"), ip=p.get("ip")) if find_device else None
            pname = (dev or {}).get("name") or p.get("name") or p.get("ip") or p.get("mac")
            pl = login_for(dev["key"]) if dev else None
            if not (dev and pl and dev.get("ip")):
                unread.append(pname)
                continue
            try:
                pc = read(dev["ip"], pl["username"], pl["password"])["cfg"]
            except FixError:
                unread.append(pname)
                continue
            if pc.get("radio.1.scan_list.status") == "enabled":
                allowed = {_int(x) for x in pc.get("radio.1.scan_list.channels", "").split(",") if _int(x)}
                if new not in allowed:
                    blocked.append(f"{pname} (scan list: {', '.join(str(a) for a in sorted(allowed)) or 'empty'})")
        if blocked:
            raise FixError("these stations would not follow — their Frequency Scan List does not include "
                           f"{new} MHz: " + "; ".join(blocked) + ". Add it to their scan lists first.")
        if unread and not params.get("accept_unverified"):
            raise FixError("Netwatch cannot read these stations to check their scan lists: " + ", ".join(unread)
                           + ". Save their SSH logins, or tick that you accept they may not follow.")
        if unread:
            out["notes"].append("Not checked (no login): " + ", ".join(unread)
                                + " — if they do not follow, the AP moves back by itself")
        out["lines"].append(f"{len(peers)} station{'s' if len(peers) != 1 else ''} must follow: "
                            + ", ".join(p.get("name") or p.get("ip") or p.get("mac") for p in peers))

    out["lines"].append(f"Undo by itself if fewer than {out['min_peers']} peer"
                        f"{'s are' if out['min_peers'] != 1 else ' is'} back within {out['wait_s'] // 60} min")
    return out


# ---- tokens ----------------------------------------------------------------
_plans, _plans_lock = {}, threading.Lock()


def remember(p):
    tok = secrets.token_urlsafe(12)
    with _plans_lock:
        now = time.time()
        for t in [t for t, (_, ts) in _plans.items() if now - ts > PLAN_TTL_S]:
            _plans.pop(t, None)
        _plans[tok] = (p, now)
    return tok


def take(tok):
    with _plans_lock:
        p = _plans.pop(tok or "", None)
    if not p or time.time() - p[1] > PLAN_TTL_S:
        raise FixError("this preview has expired — preview the fix again")
    return p[0]


# ---- the script that runs ON the radio ------------------------------------
def script(edits, min_peers, wait_s):
    """sh for busybox airOS. edits come from plan(): whitelisted keys, integer values."""
    for e in edits:
        if not _SAFE_KEY.match(e["key"]) or not e["new"].lstrip("-").isdigit():
            raise FixError("refusing an unsafe edit")
    sed = "\n".join(f"grep -q '^{re.escape(e['key'])}=' /tmp/system.cfg || {{ log 'FAIL missing {e['key']}'; exit 1; }}\n"
                    f"sed -i 's/^{re.escape(e['key'])}=.*/{e['key']}={e['new']}/' /tmp/system.cfg"
                    for e in edits)
    return f"""#!/bin/sh
ST={_TMP}.state; BAK={_TMP}.bak
log() {{ echo "$(cut -d' ' -f1 /proc/uptime) $*" >> $ST; }}
peers() {{ wstalist 2>/dev/null | grep -o '"remote"' | wc -l; }}
: > $ST
cp /tmp/system.cfg $BAK || {{ log 'FAIL backup'; exit 1; }}
log BACKUP
{sed}
log APPLY
/usr/etc/rc.d/rc.softrestart test >/dev/null 2>&1
ok=0; i=0; n=0
while [ $i -lt {int(wait_s)} ]; do
  sleep 5; i=$((i+5)); n=$(peers); log "PEERS $n"
  [ "$n" -ge {int(min_peers)} ] && {{ ok=1; break; }}
done
if [ $ok = 1 ]; then sleep 20; n=$(peers); log "PEERS $n"; [ "$n" -ge {int(min_peers)} ] || ok=0; fi
if [ $ok = 1 ]; then
  sort /tmp/system.cfg | tr -d '\\r' > /tmp/running.cfg
  if cfgmtd -w -f /tmp/running.cfg -p /etc/ >/dev/null 2>&1; then log 'DONE saved'; else log 'DONE notsaved'; fi
else
  cp $BAK /tmp/system.cfg
  /usr/etc/rc.d/rc.softrestart test >/dev/null 2>&1
  log "DONE reverted $n"
fi
"""


# ---- jobs --------------------------------------------------------------------
_jobs, _jobs_lock = {}, threading.Lock()


def state(key):
    with _jobs_lock:
        return dict(_jobs.get(key) or {})


def running(key):
    return state(key).get("phase") in ("starting", "applying", "verifying")


def _set(key, **kw):
    with _jobs_lock:
        _jobs.setdefault(key, {}).update(kw, ts=int(time.time()))


def _run(p, login, on_done):
    t, key = p["target"], p["target"]["key"]
    ip, user, pw = t["ip"], login["username"], login["password"]
    try:
        cfg = read(ip, user, pw)["cfg"]
        for e in p["edits"]:
            if cfg.get(e["key"]) != e["old"]:
                raise FixError(f"{e['key']} changed since the preview ({e['old']} → {cfg.get(e['key'])}) — nothing done")
        body = script(p["edits"], p["min_peers"], p["wait_s"])
        rc, _o, err = airos._ssh(ip, user, pw, f"cat > {_TMP}.sh && chmod 700 {_TMP}.sh && rm -f {_TMP}.state && "
                                               f"bgnd -r nwfix -- /bin/sh {_TMP}.sh && echo STARTED",
                                 stdin_data=body, timeout=30)
        if rc != 0:
            raise FixError(f"could not start the change on the radio ({(err or '').strip()[:120] or rc})")
        _set(key, phase="applying", msg="Applied without saving — waiting for the peers to reconnect…")
        deadline = time.time() + p["wait_s"] + 120
        last, lines = "", []
        while time.time() < deadline:
            time.sleep(POLL_S)
            rc, out, _e = airos._ssh(ip, user, pw, f"cat {_TMP}.state 2>/dev/null", timeout=15)
            if rc != 0:
                _set(key, msg="Radio not answering right now (expected while its wireless restarts)…")
                continue
            lines = [ln for ln in out.splitlines() if ln.strip()]
            last = lines[-1] if lines else ""
            peers = [ln.split()[-1] for ln in lines if " PEERS " in ln]
            if peers:
                _set(key, msg=f"{peers[-1]} of {p['min_peers']} peer(s) back…")
            if last.split(" ", 1)[-1].startswith(("DONE", "FAIL")):
                break
        verdict = last.split(" ", 1)[-1] if last else ""
        if verdict.startswith("DONE saved"):
            _set(key, phase="verifying", msg="Saved on the radio — reading it back…")
            after = read(ip, user, pw)
            changed = {e["key"]: after["cfg"].get(e["key"]) for e in p["edits"]}
            good = all(changed[e["key"]] == e["new"] for e in p["edits"]) and len(after["peers"]) >= p["min_peers"]
            res = dict(phase="done", ok=good, verdict="applied" if good else "check",
                       msg=("Done — the change is saved and every peer is back." if good else
                            "Saved on the radio, but the read-back does not match — check it in airOS."),
                       peers=len(after["peers"]))
        elif verdict.startswith("DONE reverted"):
            res = dict(phase="done", ok=False, verdict="reverted",
                       msg="The peers did not all come back, so the radio put the old settings back by itself. "
                           "Nothing was saved.")
        elif verdict.startswith("DONE notsaved"):
            res = dict(phase="done", ok=False, verdict="not_saved",
                       msg="Working, but saving to flash failed — a reboot would undo it. Save it in airOS.")
        elif "FAIL" in verdict:
            res = dict(phase="done", ok=False, verdict="failed", msg=f"Stopped before changing anything: {verdict}")
        else:
            res = dict(phase="done", ok=False, verdict="unknown",
                       msg="Lost track of the radio. If it does not come back it will undo the change by itself "
                           "(nothing is saved before the peers are back) — check it in a few minutes.")
        res["log"] = lines[-30:]
        _set(key, **res)
    except FixError as e:
        _set(key, phase="done", ok=False, verdict="refused", msg=str(e))
    except Exception as e:  # noqa: BLE001 - a job thread must always finish its state
        _set(key, phase="done", ok=False, verdict="error", msg=f"{e.__class__.__name__}: {e}")
    if on_done:
        try:
            on_done(p, state(key))
        except Exception:  # noqa: BLE001
            pass


def start(p, login, on_done=None):
    key = p["target"]["key"]
    with _jobs_lock:
        if (_jobs.get(key) or {}).get("phase") in ("starting", "applying", "verifying"):
            return None
        _jobs[key] = {"phase": "starting", "kind": p["kind"], "label": p["label"], "lines": p["lines"],
                      "msg": "Checking nothing changed since the preview…", "ts": int(time.time())}
    threading.Thread(target=_run, args=(p, login, on_done), daemon=True, name=f"wififix-{key}").start()
    return state(key)

