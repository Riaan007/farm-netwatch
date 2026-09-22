"""Restart one device — and prove it actually restarted.

A restart is the bluntest tool Netwatch has. The device drops off the network
and everything behind it goes with it: the cameras on a switch, a whole site
behind a backhaul radio. So the order is deliberate:

  1. the login is TESTED first (credtest.py) — one real attempt. A rejected
     login stops here and nothing is sent to the device.
  2. the restart goes out on the device's own protocol.
  3. Netwatch WATCHES the address: it must go quiet, then answer again. Only
     that round trip is reported as "restarted". A device that never went quiet
     most likely ignored the command, and the answer says exactly that instead
     of claiming success — several of these protocols cannot tell a reboot from
     a dropped connection, so the network is the only honest witness.

Step 3 takes minutes, so a restart is a background job: start() kicks it off and
returns at once, state() reads where it is. One job per device at a time, and
the last result is kept so the page can be closed and reopened.

One method per device family — the same families credtest.py can prove a login
for. Each names the site setting that has to be on before it may be used.
"""
import socket
import subprocess
import threading
import time

import airos
import credtest
import edgeswitch
import hikvision
import mikrotik
import swos

# Watching the address. A reboot is slower than any of these protocols' own
# timeouts, so the numbers are generous: a switch takes ~2 minutes to come back,
# a camera ~1. Two readings in a row have to agree before a state changes, so
# one flaky probe can't call a device down (or back).
PROBE_PORTS = (80, 443, 22, 8080, 8443, 554, 8000)
POLL_S = 3
DOWN_WAIT_S = 120           # how long it may take to actually go quiet
UP_WAIT_S = 360             # how long it may take to come back
AGREE = 2                   # consecutive probes needed to change our mind

MIKROTIK = {"id": "mikrotik", "label": "MikroTik RouterOS", "gate": "mikrotik_manage",
            "gate_label": "MikroTik management",
            "warn": "Everything behind this router loses its connection for about a minute."}
EDGESWITCH = {"id": "edgeswitch", "label": "Ubiquiti EdgeSwitch", "gate": "switch_manage",
              "gate_label": "Manage switches",
              "warn": "Every device on this switch loses its network (and PoE power) for about two minutes."}
SWOS = {"id": "swos", "label": "MikroTik SwOS", "gate": "switch_manage",
        "gate_label": "Manage switches",
        "warn": "Every device on this switch loses its network for about a minute."}
HIKVISION = {"id": "hikvision", "label": "Hikvision ISAPI", "gate": None,
             "warn": "The camera stops recording and goes off the network for about a minute."}
AIROS = {"id": "airos", "label": "Ubiquiti airOS (SSH)", "gate": None,
         "warn": "This radio drops its wireless link — anything reached through it goes offline "
                 "for a minute or two, and it may be how Netwatch reaches this site."}


def method_for(dev):
    """How this device restarts, or None if Netwatch has no way to restart it.
    Same order as credtest.plan(): the narrow, certain matches first."""
    if not dev:
        return None
    blob = " ".join(str(dev.get(k) or "") for k in ("vendor", "model", "hostname", "os")).lower()
    title = ((dev.get("banner") or {}).get("title") or "").lower()
    cat = dev.get("category") or ""
    ports = dev.get("ports") or []
    web = any(p in ports for p in (80, 443, 8080, 8443))
    if swos.is_swos(dev):
        return SWOS
    if edgeswitch.is_edgeswitch(dev):
        return EDGESWITCH
    if "mikrotik" in blob or "routerboard" in blob or "routeros" in blob:
        return MIKROTIK
    if "hikvision" in blob or "hangzhou" in blob or "hikvision" in title or (cat in ("camera", "nvr") and web):
        return HIKVISION
    if ("ubiquiti" in blob or "ubnt" in blob) and 22 in ports:
        return AIROS
    return None


# ---- sending the restart -------------------------------------------------------
def _send_hikvision(dev, ip, user, pw):
    got = hikvision._net_get_raw(ip, user, pw, 8)
    if got is None:
        return {"ok": False, "error": "the camera's ISAPI did not answer"}
    if got[0] == "AUTH":
        return {"ok": False, "error": "the camera rejected the saved login", "auth": True}
    scheme, auth, _text = got
    if hikvision.reboot(ip, scheme, auth):
        return {"ok": True, "msg": "Restart sent over ISAPI."}
    return {"ok": False, "error": "the camera refused the restart"}


def _send_airos(dev, ip, user, pw):
    """airOS drops the SSH session as it goes down, so a lost connection here is
    the expected reply — the watch decides. Only a refused login is a failure."""
    rc, _out, err = airos._ssh(ip, user, pw, "reboot", timeout=15)
    low = (err or "").lower()
    if "permission denied" in low or "authentication failed" in low:
        return {"ok": False, "error": "the radio rejected the saved SSH login", "auth": True}
    if rc in (0, 124, 255):
        return {"ok": True, "msg": "Restart sent over SSH."}
    return {"ok": False, "error": f"the radio answered {rc}: {(err or '').strip()[:120]}"}


def _send(method, dev, ip, user, pw):
    if method["id"] == "mikrotik":
        return mikrotik.api_reboot(ip, user or "admin", pw)
    if method["id"] == "edgeswitch":
        return edgeswitch.reboot(ip, user, pw)
    if method["id"] == "swos":
        return swos.reboot(ip, user, pw)
    if method["id"] == "hikvision":
        return _send_hikvision(dev, ip, user, pw)
    return _send_airos(dev, ip, user, pw)


# ---- watching the address ------------------------------------------------------
def _tcp(ip, port, timeout=1.2):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def watch_ports(dev):
    """Ports worth knocking on: the ones the scan found, plus the usual few. A
    reboot closes all of them, so any one answering means the device is back."""
    found = [p for p in (dev.get("ports") or []) if isinstance(p, int)]
    extra = [p for p in PROBE_PORTS if p not in found]
    return (found + extra)[:5]


def alive(ip, ports):
    """Does anything answer? TCP first (a rebooting device refuses every port),
    then one ICMP ping for the devices that keep every port shut."""
    for p in ports:
        if _tcp(ip, p):
            return True
    try:
        r = subprocess.run(["ping", "-n", "-c", "1", "-W", "1", ip],
                           capture_output=True, text=True, timeout=4)
        return r.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def _settle(ip, ports, want, deadline, tick):
    """Poll until `want` (True = answering, False = quiet) holds AGREE times in a
    row, or the deadline passes. Returns the moment it flipped, else None."""
    run = 0
    while time.time() < deadline:
        if bool(alive(ip, ports)) == want:
            run += 1
            if run >= AGREE:
                return time.time()
        else:
            run = 0
        tick()
        time.sleep(POLL_S)
    return None


# ---- the job -------------------------------------------------------------------
_jobs = {}                  # device key -> job dict
_lock = threading.Lock()


def _set(key, **fields):
    with _lock:
        job = _jobs.get(key)
        if job:
            job.update(fields)
            job["updated"] = int(time.time())


def state(key):
    with _lock:
        job = _jobs.get(key)
        return dict(job) if job else None


def running(key):
    job = state(key)
    return bool(job and not job.get("done"))


def _finish(key, verdict, msg, ok=False, **extra):
    _set(key, phase="done", done=True, verdict=verdict, msg=msg, ok=ok,
         finished=int(time.time()), **extra)


def _run(key, dev, ip, user, pw, method, on_done):
    """The whole restart, start to verdict, on its own thread."""
    ports = watch_ports(dev)
    tick = lambda: _set(key)        # noqa: E731 — proves the watcher is still alive
    try:
        # 1. prove the login before touching the device
        _set(key, phase="testing", msg="Checking the saved login still works\u2026")
        test = credtest.test(dev, user, pw)
        if test.get("result") == "auth_failed":
            return _finish(key, "auth_failed",
                           f"Not restarted \u2014 the device rejected the saved login. {test.get('detail', '')}".strip())
        if test.get("result") == "unreachable":
            return _finish(key, "unreachable",
                           f"Not restarted \u2014 {ip} does not answer. {test.get('detail', '')}".strip())
        untested = test.get("result") != "ok"

        # 2. send it
        _set(key, phase="sending", tested=test.get("result"),
             msg=f"Sending the restart over {method['label']}\u2026")
        res = _send(method, dev, ip, user, pw)
        if not res.get("ok"):
            verdict = "auth_failed" if res.get("auth") else "failed"
            return _finish(key, verdict, f"Not restarted \u2014 {res.get('error', 'the device refused the restart')}.")
        sent = int(time.time())
        _set(key, sent_at=sent, phase="waiting_down", untested=untested,
             msg="Restart sent. Waiting for the device to go quiet\u2026")

        # 3. it must go quiet, then come back \u2014 that round trip IS the confirmation
        down = _settle(ip, ports, False, sent + DOWN_WAIT_S, tick)
        if down is None:
            return _finish(key, "no_downtime",
                           f"The restart was accepted but {ip} never stopped answering in "
                           f"{DOWN_WAIT_S // 60} minutes \u2014 it most likely ignored the command. "
                           "Nothing else changed.")
        _set(key, down_at=int(down), phase="waiting_up",
             msg="It went offline \u2014 waiting for it to come back\u2026")
        up = _settle(ip, ports, True, down + UP_WAIT_S, tick)
        if up is None:
            return _finish(key, "still_down",
                           f"{ip} went offline as expected but has not come back after "
                           f"{UP_WAIT_S // 60} minutes. Check it \u2014 it may need a power cycle.")
        secs = int(up - down)
        _finish(key, "restarted", f"Restarted and back online \u2014 it was offline for {secs} seconds.",
                ok=True, up_at=int(up), seconds_down=secs)
    except Exception as e:      # noqa: BLE001 — a crashed thread must not leave the job hanging
        _finish(key, "failed", f"The restart stopped unexpectedly: {e.__class__.__name__}.")
    finally:
        if on_done:
            try:
                on_done(state(key))
            except Exception:   # noqa: BLE001
                pass


def start(dev, user, pw, method, on_done=None):
    """Kick off a restart. Returns the job, or None if one is already running."""
    key, ip = dev.get("key"), dev.get("ip")
    with _lock:
        old = _jobs.get(key)
        if old and not old.get("done"):
            return None
        _jobs[key] = {"key": key, "ip": ip, "device": dev.get("name") or dev.get("device_name") or ip,
                      "method": method["id"], "method_label": method["label"],
                      "phase": "testing", "msg": "Starting…", "done": False, "ok": False,
                      "verdict": "", "started": int(time.time()), "updated": int(time.time())}
        job = dict(_jobs[key])
    threading.Thread(target=_run, args=(key, dev, ip, user, pw, method, on_done),
                     daemon=True, name=f"restart-{ip}").start()
    return job
