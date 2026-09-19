"""MikroTik SwOS switches (RB260GS/GSP = CSS106-1G-4P-1S, CSS106-5G-1S, CSS326 …).

SwOS is NOT RouterOS: there is no API on 8728, no SSH and no MAC-Telnet, only the
switch's own web page. That page reads and writes a handful of small files over
HTTP digest auth, and so does this module:

  GET  /sys.b      identity, board, firmware, serial, MAC, uptime (1/100 s), volt, temp
  GET  /link.b     per port: enabled bitmask, names, link, speed, duplex, PoE
  POST /link.b     the writable link fields back (how the page saves a port)
  GET  /!stats.b   byte/error counters per port
  GET  /!dhost.b   the learned MAC table (port index, VLAN)
  GET  /fwd.b, /vlan.b, /snmp.b
  POST /reboot "*";  GET /backup.swb (the config file the Backup button saves)

The format is a JavaScript object literal, not JSON: bare keys, numbers as hex
(0x3f), strings as hex-encoded bytes in single quotes ('506f727431' = "Port1"),
and bitmasks with one bit per port. Field meanings come from the switch's own
engine.js (read off the home CSS106-1G-4P-1S, SwOS 2.11, 2026-09-19):
  poe  = off | auto | on | calibr
  poes = unavailable | disabled | waiting for load | powered on | overload |
         short circuit | voltage too low | current too low | power cycle |
         voltage too high | controller error
  pwr  = deciwatts, curr = mA, volt = decivolts, spd = index into 10/100/1000.

The snapshot has the SAME shape as edgeswitch.normalize(), so the switch monitor,
its problems, events, protected ports and both UIs work unchanged. Port ids are
"0/<n>" (1-based) to match the EdgeSwitch ids the UI and routes expect.
Reads are always allowed; writes are gated in server.py like the EdgeSwitch's.
"""
import re
import time

import requests
from requests.auth import HTTPDigestAuth

TIMEOUT = (5, 15)
POE_OFF = "off"
POE_MODES = ["off", "auto", "on", "calibr"]
POE_STATUS = ["", "disabled", "waiting for load", "powered on", "overload", "short circuit",
              "voltage too low", "current too low", "power cycle", "voltage too high",
              "controller error"]
SPEEDS = [10, 100, 1000]
# What the page itself sends back when a port is saved (read-only fields such as
# lnk/spd/dpx/poes/curr/pwr are never posted).
LINK_WRITABLE = ("en", "nm", "an", "spdc", "dpxc", "fct", "poe", "prio")
DEFAULT_IDENTITY = "mikrotik"
RATE_SAMPLE_S = 2.0


class SwosError(Exception):
    def __init__(self, msg, kind="error", status=None):
        super().__init__(msg)
        self.kind = kind            # auth_failed | unreachable | error
        self.status = status


def is_swos(dev):
    """The scanner's HTTP banner carries the page title "MikroTik SwOS", and nmap
    names the httpd "MikroTik RouterBoard 250GS httpd"."""
    title = ((dev.get("banner") or {}).get("title") or "").lower()
    if "swos" in title:
        return True
    svc = " ".join(str(v) for v in (dev.get("services") or {}).values()).lower()
    if "swos" in svc or re.search(r"routerboard \d+gs", svc):
        return True
    return (dev.get("model") or "").upper().startswith(("CSS", "RB260"))


def normalize_mac(mac):
    m = re.sub(r"[^0-9a-f]", "", str(mac or "").lower())
    return ":".join(m[i:i + 2] for i in range(0, 12, 2)) if len(m) == 12 else ""


# ---- the page's data format ------------------------------------------------------
_TOKEN = re.compile(r"\s*(0x[0-9a-fA-F]+|'[^']*'|[A-Za-z_!][A-Za-z0-9_]*|[{}\[\],:])")


def parse(text):
    """SwOS object literal -> Python. Numbers stay ints; strings stay hex (decode
    with hexstr()) because the same quoted form also carries MACs."""
    toks = [t for t in _TOKEN.findall(text or "")]
    pos = 0

    def val():
        nonlocal pos
        t = toks[pos]
        pos += 1
        if t == "{":
            out = {}
            while toks[pos] != "}":
                k = toks[pos]
                if toks[pos + 1] != ":":
                    raise ValueError("expected ':'")
                pos += 2
                out[k] = val()
                if toks[pos] == ",":
                    pos += 1
            pos += 1
            return out
        if t == "[":
            out = []
            while toks[pos] != "]":
                out.append(val())
                if toks[pos] == ",":
                    pos += 1
            pos += 1
            return out
        if t.startswith("0x"):
            return int(t, 16)
        if t.startswith("'"):
            return t[1:-1]
        raise ValueError(f"unexpected {t!r}")

    if not toks:
        raise ValueError("empty answer")
    return val()


def dump(v):
    """Python -> the literal the page posts (the inverse of parse)."""
    if isinstance(v, dict):
        return "{" + ",".join(f"{k}:{dump(x)}" for k, x in v.items()) + "}"
    if isinstance(v, list):
        return "[" + ",".join(dump(x) for x in v) + "]"
    if isinstance(v, bool):
        v = int(v)
    if isinstance(v, int):
        h = format(v, "x")
        return "0x" + ("0" + h if len(h) % 2 else h)
    return "'" + str(v) + "'"


def hexstr(s):
    try:
        return bytes.fromhex(s or "").decode("utf-8", "replace")
    except (ValueError, TypeError):
        return str(s or "")


def strhex(s):
    return str(s).encode("utf-8").hex()


def _ip(n):
    """sys.b stores the IP little-endian: 0x2358a8c0 -> 192.168.88.35."""
    try:
        return ".".join(str((int(n) >> s) & 255) for s in (0, 8, 16, 24))
    except (TypeError, ValueError):
        return ""


def _at(lst, i, default=None):
    return lst[i] if isinstance(lst, list) and i < len(lst) else default


def _bit(mask, i):
    return bool((int(mask or 0) >> i) & 1)


def _pid(i):
    return f"0/{i + 1}"


def _index(port_id, count):
    m = re.match(r"^0/(\d+)$", str(port_id or ""))
    i = int(m.group(1)) - 1 if m else -1
    if not 0 <= i < count:
        raise SwosError(f"port {port_id} not found on the switch")
    return i


# ---- session ------------------------------------------------------------------------
class Session:
    """Digest auth is per request, so there is no login to release; the class
    keeps one HTTP connection and turns answers into SwosError kinds."""

    def __init__(self, ip, username, password, timeout=TIMEOUT):
        self.ip = ip
        self.base = f"http://{ip}"
        self.timeout = timeout
        self.http = requests.Session()
        self.http.auth = HTTPDigestAuth(username or "admin", password or "")
        self.http.headers["User-Agent"] = "Netwatch"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.http.close()

    def _req(self, method, path, data=None, timeout=None):
        try:
            r = self.http.request(method, self.base + path, data=data,
                                  headers={"Content-Type": "text/plain"} if data is not None else None,
                                  timeout=timeout or self.timeout)
        except requests.RequestException as e:
            raise SwosError(f"switch not reachable: {e}", "unreachable")
        if r.status_code == 401:
            raise SwosError("the switch rejected the saved username/password", "auth_failed", 401)
        if r.status_code >= 400:
            raise SwosError(f"{method} {path} failed (HTTP {r.status_code})", "error", r.status_code)
        return r

    def get(self, path):
        text = self._req("GET", path).text
        try:
            return parse(text)
        except (ValueError, IndexError) as e:
            raise SwosError(f"could not read {path}: {e}")

    def post(self, path, obj):
        return self._req("POST", path, data=obj if isinstance(obj, str) else dump(obj))


# ---- read + normalise --------------------------------------------------------------
def _bytes(stats, lo, hi, i):
    return (int(_at(stats.get(hi), i, 0) or 0) << 32) + int(_at(stats.get(lo), i, 0) or 0)


def normalize(sysb, link, stats, hosts=None, fwd=None, vlans=None, snmp=None, stats2=None, dt=None):
    """The switch's raw answers -> the snapshot shape of edgeswitch.normalize()."""
    sysb, link, stats = sysb or {}, link or {}, stats or {}
    names = link.get("nm") or []
    count = len(names) or len(link.get("spd") or [])
    poe_cfg, poe_st = link.get("poe"), link.get("poes")
    has_poe = isinstance(poe_st, list)

    macs_by_port, seen = {}, set()
    for h in hosts or []:
        if not isinstance(h, dict):
            continue
        mac, prt = normalize_mac(h.get("adr")), h.get("prt")
        if not mac or not isinstance(prt, int) or (mac, prt) in seen:
            continue
        seen.add((mac, prt))
        macs_by_port.setdefault(_pid(prt), []).append({"mac": mac, "vlan": h.get("vid") or None})

    dvid = (fwd or {}).get("dvid") or []
    vlan_on = any(v for v in (fwd or {}).get("vlan") or [])
    ports = []
    for i in range(count):
        pid = _pid(i)
        enabled = _bit(link.get("en"), i)
        up = _bit(link.get("lnk"), i) and enabled
        spd, spdc = _at(link.get("spd"), i), _at(link.get("spdc"), i)
        name = hexstr(_at(names, i, ""))
        st = _at(poe_st, i, 0) if has_poe else 0
        poe_supported = has_poe and bool(st)             # 0 = no PoE-out on this port
        mode = POE_MODES[_at(poe_cfg, i, 0)] if poe_supported and _at(poe_cfg, i, 0) < len(POE_MODES) else ""
        powered = st == 3
        rx_b, tx_b = _bytes(stats, "rb", "rbh", i), _bytes(stats, "tb", "tbh", i)
        rx_bps = tx_bps = None
        if stats2 and dt:
            rx_bps = max(0, _bytes(stats2, "rb", "rbh", i) - rx_b) * 8 / dt
            tx_bps = max(0, _bytes(stats2, "tb", "tbh", i) - tx_b) * 8 / dt
            rx_b, tx_b = _bytes(stats2, "rb", "rbh", i), _bytes(stats2, "tb", "tbh", i)
        errs = int(_at(stats.get("rte"), i, 0) or 0) + int(_at(stats.get("tte"), i, 0) or 0)
        is_sfp = name.upper().startswith("SFP") or i == link.get("sfpo", -1)
        ports.append({
            "id": pid, "type": "sfp" if is_sfp else "port", "name": name, "mac": "",
            "enabled": enabled, "up": up,
            "speed": SPEEDS[spd] if up and isinstance(spd, int) and spd < len(SPEEDS) else None,
            "duplex": ("full" if _bit(link.get("dpx"), i) else "half") if up else None,
            "speed_cfg": "auto" if _bit(link.get("an"), i) else
                         str(SPEEDS[spdc]) if isinstance(spdc, int) and spdc < len(SPEEDS) else "",
            "mtu": None,
            "poe_supported": poe_supported,
            "poe_mode": mode,
            "poe_modes": POE_MODES[:3] if poe_supported else [],
            "poe_status": POE_STATUS[st] if poe_supported and st < len(POE_STATUS) else "",
            "poe_w": round(int(_at(link.get("pwr"), i, 0) or 0) / 10, 1) if poe_supported else None,
            "poe_ma": int(_at(link.get("curr"), i, 0) or 0) if poe_supported else None,
            "poe_fault": poe_supported and st >= 4 and st != 8,
            "rx_bps": rx_bps, "tx_bps": tx_bps, "rx_bytes": rx_b, "tx_bytes": tx_b,
            "errors": errs, "dropped": 0,
            "stp_state": "", "isolated": False, "ping_watchdog": False,
            "sfp": None,
            "vlans": {"untagged": [_at(dvid, i)] if vlan_on and _at(dvid, i) else [], "tagged": []},
            "macs": macs_by_port.get(pid, []),
        })
        if not powered and poe_supported:
            ports[-1]["poe_w"] = 0.0

    board = hexstr(sysb.get("brd"))
    identity = hexstr(sysb.get("id"))
    volt = sysb.get("volt")
    temp = sysb.get("temp")
    if isinstance(temp, int) and temp >= 1 << 31:        # signed 32-bit
        temp -= 1 << 32
    health = {"cpu": None, "ram": None,
              "temps": [{"name": "board", "value": float(temp)}] if isinstance(temp, int) else [],
              "temp": float(temp) if isinstance(temp, int) else None,
              "fans": [],
              "psu": [{"type": "input", "connected": True, "voltage": round(volt / 10, 1), "power": None}]
              if isinstance(volt, int) and volt else []}
    poe_ports = [p for p in ports if p["poe_supported"]]
    used = round(sum(p["poe_w"] or 0 for p in poe_ports), 1)
    ver = hexstr(sysb.get("ver"))
    snmp = snmp or {}
    snap = {
        "ok": True, "ts": int(time.time()), "os": "swos",
        "device": {
            "model": board, "product": "MikroTik SwOS", "family": "swos",
            "mac": normalize_mac(sysb.get("mac")),
            # "MikroTik" is the factory identity — not a name anyone chose.
            "name": identity if identity.strip().lower() != DEFAULT_IDENTITY else "",
            "firmware": ver, "serial": hexstr(sysb.get("sid")),
            "uptime": int(sysb["upt"]) // 100 if isinstance(sysb.get("upt"), int) else None,
            "ip": _ip(sysb.get("ip")),
        },
        "caps": {"locate": False, "cable_test": False, "backup": True, "reboot": True,
                 "poe_cycle": bool(poe_ports), "rename_port": True},
        "health": health,
        "poe": {"budget_w": None, "used_w": used, "pct": None,
                "powered": sum(1 for p in poe_ports if p["poe_status"] == "powered on"),
                "unmeasured": 0},
        "ports": ports, "lags": [],
        "vlans": [{"id": v.get("vid"), "name": ""} for v in vlans or [] if isinstance(v, dict)],
        "services": {"http": True, "ssh": False, "telnet": False,
                     "snmp": bool(snmp.get("en")) if snmp else None,
                     "snmp_community": hexstr(snmp.get("com")) if snmp else None},
        "neighbors": [],
        "stats_ts": None,
    }
    snap["summary"] = {
        "ports": len(ports), "up": sum(1 for p in ports if p["up"]),
        "disabled": sum(1 for p in ports if not p["enabled"]),
        "rx_bps": sum(p["rx_bps"] or 0 for p in ports), "tx_bps": sum(p["tx_bps"] or 0 for p in ports),
        "errors": sum(p["errors"] or 0 for p in ports),
    }
    return snap


def read(ip, username, password, with_neighbors=False, keep_raw=False, rate_s=RATE_SAMPLE_S):
    """Read everything a dashboard needs. Never raises: {ok: False, error, kind} on
    failure. Traffic rates come from two counter reads rate_s apart — the switch's
    own rate field jumps around too much from one second to the next."""
    t0 = time.time()
    raw = {}
    try:
        with Session(ip, username, password) as s:
            raw["sys"] = s.get("/sys.b")
            raw["link"] = s.get("/link.b")
            raw["stats"] = s.get("/!stats.b")
            t1 = time.time()
            for name, path in (("hosts", "/!dhost.b"), ("fwd", "/fwd.b"), ("vlans", "/vlan.b"),
                               ("snmp", "/snmp.b")):
                try:
                    raw[name] = s.get(path)
                except SwosError as e:
                    if e.kind == "auth_failed":
                        raise
                    raw[name] = None
                    raw.setdefault("_missing", {})[name] = str(e)
            if rate_s:
                time.sleep(max(0.0, rate_s - (time.time() - t1)))
                raw["stats2"] = s.get("/!stats.b")
                raw["dt"] = time.time() - t1
    except SwosError as e:
        return {"ok": False, "error": str(e), "kind": e.kind, "ip": ip}
    snap = normalize(raw["sys"], raw["link"], raw["stats"], raw.get("hosts"), raw.get("fwd"),
                     raw.get("vlans"), raw.get("snmp"), raw.get("stats2"), raw.get("dt"))
    snap["ip"] = ip
    snap["read_s"] = round(time.time() - t0, 1)
    if raw.get("_missing"):
        snap["missing"] = raw["_missing"]
    if keep_raw:
        snap["raw"] = raw
    return snap


def identify(ip, username="admin", password="", timeout=(4, 8)):
    """Board/firmware/identity with the given login (SwOS has no public page).
    Used for a switch with no saved login: the factory login is admin/blank."""
    try:
        with Session(ip, username, password, timeout=timeout) as s:
            sysb = s.get("/sys.b")
        return {"ok": True, "model": hexstr(sysb.get("brd")), "firmware": hexstr(sysb.get("ver")),
                "serial": hexstr(sysb.get("sid")), "identity": hexstr(sysb.get("id"))}
    except SwosError as e:
        return {"ok": False, "error": str(e), "kind": e.kind}


# ---- writes (gated in server.py) ------------------------------------------------------
def _write_link(s, mutate):
    """Read link.b, change it, post back ONLY the writable fields — exactly what the
    switch's page sends when a port is saved. Returns (before, after) link.b."""
    link = s.get("/link.b")
    body = {k: (list(link[k]) if isinstance(link[k], list) else link[k]) for k in LINK_WRITABLE if k in link}
    mutate(body, len(link.get("nm") or link.get("spd") or []), link)
    s.post("/link.b", body)
    return link, s.get("/link.b")


def _port_state(link, i):
    poe = link.get("poe")
    return {"enabled": _bit(link.get("en"), i), "name": hexstr(_at(link.get("nm"), i, "")),
            "poe": POE_MODES[_at(poe, i)] if isinstance(poe, list) and _at(poe, i, 99) < len(POE_MODES) else None}


def set_port(ip, username, password, port_id, enabled=None, poe=None, name=None):
    """Change one port: enabled (bool), poe ('off' | 'auto' | 'on'), name."""
    if poe is not None and poe not in POE_MODES[:3]:
        return {"ok": False, "error": f"PoE mode must be one of {', '.join(POE_MODES[:3])}"}
    try:
        with Session(ip, username, password) as s:
            idx = {}

            def mutate(body, count, link):
                i = idx["i"] = _index(port_id, count)
                if enabled is not None:
                    body["en"] = (body["en"] | (1 << i)) if enabled else (body["en"] & ~(1 << i))
                if poe is not None:
                    # poes 0 = no PoE out here (the PoE-in port, the SFP cage)
                    if not isinstance(body.get("poe"), list) or not _at(link.get("poes"), i, 0):
                        raise SwosError(f"port {port_id} has no PoE")
                    body["poe"][i] = POE_MODES.index(poe)
                if name is not None:
                    body["nm"][i] = strhex(str(name)[:16])     # SwOS keeps short port names
            before, after = _write_link(s, mutate)
        i = idx["i"]
        return {"ok": True, "port": port_id, "before": _port_state(before, i), "after": _port_state(after, i)}
    except SwosError as e:
        return {"ok": False, "error": str(e), "kind": e.kind}


def poe_cycle(ip, username, password, port_id, off_s=8):
    """PoE off, wait, the old mode back — restarts a hung camera or radio."""
    off_s = max(3, min(int(off_s or 8), 60))
    try:
        with Session(ip, username, password) as s:
            link = s.get("/link.b")
            i = _index(port_id, len(link.get("nm") or []))
            poes = _at(link.get("poes"), i, 0)
            mode_i = _at(link.get("poe"), i, 0)
            if not poes:
                return {"ok": False, "error": f"port {port_id} has no PoE"}
            if not mode_i:
                return {"ok": False, "error": "PoE is not switched on on this port"}
            mode = POE_MODES[mode_i]

            def to(m):
                def mutate(body, count, link):
                    body["poe"][i] = m
                return mutate
            _write_link(s, to(0))
            time.sleep(off_s)
            try:
                _write_link(s, to(mode_i))
            except SwosError as e:
                return {"ok": False, "error": f"PoE was switched OFF but turning it back on failed: {e} "
                                              f"— set port {port_id} PoE to '{mode}' again", "kind": e.kind}
        return {"ok": True, "port": port_id, "mode": mode, "off_s": off_s}
    except SwosError as e:
        return {"ok": False, "error": str(e), "kind": e.kind}


def reboot(ip, username, password):
    try:
        with Session(ip, username, password) as s:
            s.post("/reboot", "*")
        return {"ok": True}
    except SwosError as e:
        return {"ok": False, "error": str(e), "kind": e.kind}


def locate(ip, username, password, on=True):
    return {"ok": False, "error": "SwOS switches have no find-me LEDs"}


def cable_test(ip, username, password, port_id):
    return {"ok": False, "error": "SwOS on this switch has no cable test"}


def backup(ip, username, password):
    """The switch's own configuration file (.swb — what its Backup button saves)."""
    try:
        with Session(ip, username, password) as s:
            r = s._req("GET", "/backup.swb", timeout=(5, 30))
        if not r.content:
            return {"ok": False, "error": "the switch sent an empty backup"}
        return {"ok": True, "content": r.content, "filename": "backup.swb", "ext": ".swb"}
    except SwosError as e:
        return {"ok": False, "error": str(e), "kind": e.kind}
