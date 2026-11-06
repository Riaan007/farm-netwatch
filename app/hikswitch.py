"""Hikvision smart switches with the V3.x web app (DS-3E1520P-EI and friends).

The switch's own web page talks JSON to a "thing model" API, and so does this
module:

  POST /iot/global/0-global/model/service/operate/<Group>/<Command>
       body {"data": {...}}              -> {"status", "code", "errorMsg", "data"}
  GET  /iot/global/0-global/model/attribute/get/<Group>/<Attr>
                                         -> the same envelope, payload in data.Value

Login is two calls: Session/AuthInfo {username, random} hands out a challenge,
salt, iteration count and session id; Session/Login takes
PBKDF2-SHA256(sha256hex(user + salt + password) + challenge, salt) and answers
with a sessionTag, which goes in a `SessionTag` header on every later call.
Session/Logout ends it. Everything here — URLs, field names, the hash — comes
from the switch's own web app (framework V5.1.16_R0604, firmware V3.3.0 build
251010, read 2026-11-04 off a DS-3E1520P-EI). Field names the web
app does not show were not guessed; the first read of a real switch must be
checked (GET /api/devices/<key>/switch/raw shows the switch's own answers).

NOT covered: the older smart switches (DS-3E1309P-EI V1.2.0) have no web server
at all, only Hikvision's private SDK port 8000, and are managed through the
Hik-Partner Pro cloud only. is_hikswitch() leaves them out.

!! A wrong password LOCKS the account (the answer carries lockStatus,
retryLoginTime and unlockTime). The switch monitor tries a saved login once and
not again until the login changes; never loop logins here.

The snapshot has the SAME shape as edgeswitch.normalize()/swos.normalize(), so
the switch monitor, its problems, events, protected ports and both UIs work
unchanged. Port ids are "0/<n>", n = the switch's own portID.

The switch's thing model (load_model: what it has, with ranges and options) and
attribute writes (Session.set_attr) are here too; the advanced settings built on
them — RSTP, loop guard, storm control, PoE watchdog, DHCP snooping and the rest —
live in hikswitch_adv.py. The cable test is the switch's own Cable Detection.
"""
import hashlib
import json
import random
import re
import time
import uuid

import requests

API = "/iot/global/0-global/model"
TIMEOUT = (5, 15)
OK_CODE = "0x00000000"
POE_OFF = "off"
POE_MODES = ["off", "auto", "compat"]          # what Netwatch offers; "force" is shown, never set
SPEED = {1: 10, 3: 100, 4: 1000, 5: 10000}     # the switch's rate codes (web app: 1:1e7 … 5:1e10)
RATE_TXT = {0: "auto", 1: "10M", 3: "100M", 4: "1000M", 5: "10G"}
DX_TXT = {0: "half", 1: "full", 3: "auto"}
POE_STATUS = {"normal": "powered on", "down": "off", "nonStandardPD": "non-standard device",
              "testing": "waiting for load", "abnormal": "power fault", "detectAbnormal": "no PoE device detected",
              "powerupAbnormal": "power-up fault", "classAbnormal": "PoE class fault"}
# A port that stopped powering a device it had. "detectAbnormal" is NOT here: a laptop
# or an uplink on a PoE port fails detection too, and that is normal.
POE_FAULTS = {"abnormal", "powerupAbnormal", "classAbnormal"}
PD_DETAIL = {
    "bigCapacitance": "capacitance too high", "smallResistance": "resistance too low",
    "bigResistance": "resistance too high", "classMismatch": "class mismatch",
    "classOvercurrent": "class over-current", "classUnderVoltageInterrupt": "under-voltage",
    "classOverVoltageInterrupt": "over-voltage", "mosfetFault": "MOSFET fault",
    "icutFault": "over-current cut-off", "ilmitFault": "current limit", "highTempShutdownInterrupt":
    "over-temperature shut-down", "highTempAlarmInterrupt": "over-temperature warning",
    "inrushAbnormal": "inrush current",
}
MAX_PAGES = 40
# ip -> parts of a read the switch answered "not supported" (HTTP 404 / 0x00100003; e.g.
# loop status, SSH and SNMP on a DS-3E1520P-EI V3.3.0). Not asked again until a restart:
# fewer calls per poll matter on a switch behind a thin radio link.
_UNSUPPORTED = {}


class HikSwitchError(Exception):
    def __init__(self, msg, kind="error", status=None, code=None):
        super().__init__(msg)
        self.kind = kind            # auth_failed | unreachable | error
        self.status = status
        self.code = code


def _not_supported(e):
    return e.status == 404 or str(e.code or "").lower() == "0x00100003"


def _model_of(dev):
    sp = dev.get("sadp")
    m = (sp.get("model") if isinstance(sp, dict) else None) or dev.get("model") or ""
    return str(m).strip().upper()


def is_hikswitch(dev):
    """A Hikvision DS-3E smart switch WITH a web server. The model comes from
    Hikvision discovery (SADP) or the record — never from the vendor, because a
    switch behind a proxy-ARP radio bridge shows the bridge's (Ubiquiti) vendor.
    The V1.x smart switches answer only on SDK port 8000: no web, no driver."""
    if not _model_of(dev).startswith("DS-3E"):
        return False
    sp = dev.get("sadp") if isinstance(dev.get("sadp"), dict) else {}
    if sp.get("http_port"):
        return True
    ports = dev.get("ports") or []
    svc = dev.get("services") or {}
    return 80 in ports or 443 in ports or "80" in svc or 80 in svc


def normalize_mac(mac):
    m = re.sub(r"[^0-9a-f]", "", str(mac or "").lower())
    return ":".join(m[i:i + 2] for i in range(0, 12, 2)) if len(m) == 12 else ""


def login_hash(username, password, challenge, salt, iterations):
    """What Session/Login wants in "password" (the web app's _pwdToAESKey):
    PBKDF2-HMAC-SHA256 over sha256hex(user+salt+password)+challenge, salted
    with the salt, 64 bytes, as 128 hex digits."""
    inner = hashlib.sha256((str(username) + str(salt) + str(password)).encode("utf-8")).hexdigest()
    try:
        rounds = max(1, int(iterations or 1))
    except (TypeError, ValueError):
        rounds = 1
    return hashlib.pbkdf2_hmac("sha256", (inner + str(challenge)).encode("utf-8"),
                               str(salt).encode("utf-8"), rounds, 64).hex()


def _int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _on(v):
    """The switch says 1/0, true/false or "true"/"false" depending on the field."""
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "on", "enable", "enabled")
    return bool(v)


def _pid(n):
    return f"0/{n}"


def _port_no(port_id):
    m = re.match(r"^0/(\d+)$", str(port_id or ""))
    return int(m.group(1)) if m else None


# ---- session ------------------------------------------------------------------------
class Session:
    """One login, then calls with the session tag; logs out when done."""

    def __init__(self, ip, username, password, timeout=TIMEOUT):
        self.ip = ip
        self.base = f"http://{ip}"
        self.username = username or "admin"
        self.password = password or ""
        self.timeout = timeout
        self.tag = None
        self.http = requests.Session()
        # what the switch's own page sends (the EdgeSwitch lesson: some web servers
        # refuse a POST without Origin/Referer before the login is even checked)
        self.http.headers.update({"User-Agent": "Netwatch", "Accept": "application/json",
                                  "Origin": self.base, "Referer": self.base + "/"})

    def __enter__(self):
        self.login()
        return self

    def __exit__(self, *exc):
        try:
            if self.tag:
                self._send("POST", "/service/operate/Session/Logout", {}, timeout=(3, 5))
        except HikSwitchError:
            pass
        finally:
            self.tag = None
            self.http.close()

    # -- transport --
    def _send(self, method, path, data=None, timeout=None):
        headers = {"SessionTag": self.tag} if self.tag else {}
        kw = {}
        if method == "POST":
            headers["Content-Type"] = "application/json"
            kw["json"] = {"data": data if data is not None else {}}
        try:
            r = self.http.request(method, self.base + API + path, headers=headers,
                                  timeout=timeout or self.timeout, **kw)
        except requests.RequestException as e:
            raise HikSwitchError(f"switch not reachable: {e}", "unreachable")
        try:
            body = r.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise HikSwitchError(f"{path} gave no JSON (HTTP {r.status_code})", "error", r.status_code)
        code = body.get("code")
        payload = body.get("data")
        if isinstance(payload, dict) and "Value" in payload:
            payload = payload.get("Value")
        if r.status_code == 200 and (code in (None, OK_CODE)):
            return payload
        err = HikSwitchError(f"{path.rsplit('/', 1)[-1]}: {body.get('errorMsg') or 'refused'} "
                             f"(HTTP {r.status_code}, {code})", "error", r.status_code, code)
        err.data = payload if isinstance(payload, dict) else {}
        raise err

    def svc(self, group, cmd, data=None, timeout=None):
        return self._send("POST", f"/service/operate/{group}/{cmd}", data, timeout=timeout)

    def attr(self, group, name):
        return self._send("GET", f"/attribute/get/{group}/{name}")

    def set_attr(self, group, name, value, timeout=None):
        """An attribute write: POST attribute/set/… with {"data": {"Value": …}} (the
        web app's request wrapper does exactly this for every POST to an attribute)."""
        return self._send("POST", f"/attribute/set/{group}/{name}", {"Value": value}, timeout=timeout)

    def search(self, group, cmd, list_key="matchResults", page=100, extra=None, mac_paging=False):
        """Every row of a paged Search… command (responseStatus "MORE" = ask again)."""
        rows, pos, last_mac = [], 1, ""
        for _ in range(MAX_PAGES):
            body = {"searchID": str(uuid.uuid4()), "searchResultPosition": pos, "maxResults": page}
            if extra:
                body.update(extra)
            if mac_paging and last_mac:
                body["lastMacAddress"] = last_mac
            res = self.svc(group, cmd, body)
            got = (res.get(list_key) or []) if isinstance(res, dict) else []
            got = [x for x in got if isinstance(x, dict)]
            rows += got
            if not isinstance(res, dict) or res.get("responseStatus") != "MORE" or not got:
                break
            pos += _int(res.get("numOfMatches"), len(got)) or len(got)
            if mac_paging:
                last_mac = got[-1].get("macAddress") or ""
        return rows

    # -- login --
    def login(self):
        rnd = str(random.randint(10_000_000, 99_999_999))
        try:
            info = self._send("POST", "/service/operate/Session/AuthInfo",
                              {"username": self.username, "random": rnd}) or {}
        except HikSwitchError as e:
            if e.status == 401:
                raise HikSwitchError("the switch rejected the saved username", "auth_failed", 401, e.code)
            raise
        if not isinstance(info, dict) or not info.get("challenge"):
            raise HikSwitchError("the switch did not offer a login challenge (is it activated?)")
        pw = login_hash(self.username, self.password, info.get("challenge"), info.get("salt") or "",
                        info.get("iterations"))
        try:
            res = self._send("POST", "/service/operate/Session/Login", {
                "isSessionIDValidLongTerm": False, "password": pw, "sessionID": info.get("sessionID"),
                "sessionIDVersion": info.get("SessionIDVersion", info.get("sessionIDVersion")),
                "username": self.username, "isNeedSessionTag": True}) or {}
        except HikSwitchError as e:
            if e.kind == "unreachable":
                raise
            d = getattr(e, "data", {}) or {}
            if str(d.get("lockStatus") or "").lower() == "lock":
                mins = max(1, round((_int(d.get("unlockTime"), 0) or 0) / 60))
                msg = f"the switch has LOCKED this account after failed logins (unlocks in about {mins} min)"
            elif e.status == 401 or "retryLoginTime" in d or "lockStatus" in d:
                left = _int(d.get("retryLoginTime"))
                msg = "the switch rejected the saved username/password" + (
                    f" ({left} tries left before it locks the account)" if left is not None else "")
            else:
                # not a password verdict (a refused request shape, a busy switch):
                # say what the switch said instead of blaming the password
                raise HikSwitchError(f"the switch refused the login request: {e}", "error", e.status, e.code)
            raise HikSwitchError(msg, "auth_failed", e.status, e.code)
        tag = res.get("sessionTag") if isinstance(res, dict) else None
        if not tag:
            raise HikSwitchError("the switch accepted the login but gave no session", "error")
        self.tag = tag
        return res


# ---- the switch's thing model ---------------------------------------------------------
# (ip, firmware) -> {domain identifier: domain} — the list the switch's own web app
# builds its menus from (DynamicCapability/GetModelFile). None = the switch has none
# (then every part is just tried). Read once per switch and firmware.
_MODELS = {}


def load_model(s, firmware=""):
    key = (s.ip, str(firmware or ""))
    if key in _MODELS:
        return _MODELS[key]
    model = None
    try:
        res = s.svc("DynamicCapability", "GetModelFile", {"filePath": ""}, timeout=(5, 40)) or {}
        doc = res.get("filePath") if isinstance(res, dict) else None
        if isinstance(doc, str) and str(res.get("filePathType") or "text") == "text":
            doc = json.loads(doc)
        doms = ((doc.get("resources") or [{}])[0].get("domains") or []) if isinstance(doc, dict) else []
        model = {d["identifier"]: d for d in doms if isinstance(d, dict) and d.get("identifier")} or None
    except HikSwitchError as e:
        if e.kind == "unreachable":
            raise
        if not _not_supported(e):
            return None                       # a hiccup: ask again next time
    except (ValueError, TypeError, AttributeError, IndexError):
        model = None
    _MODELS[key] = model
    return model


def model_has(model, group, name=None, kind=None):
    """True/False from the thing model; None when there is no model to ask."""
    if model is None:
        return None
    d = model.get(group)
    if not isinstance(d, dict):
        return False
    if name is None:
        return True
    kinds = (kind,) if kind else ("props", "actions", "events")
    return any(isinstance(x, dict) and x.get("identifier") == name for k in kinds for x in d.get(k) or [])


def _schema_caps(props, prefix=""):
    """The web app's formatData(): {default, range, options} keyed like
    "portRateList-rxLimitRate" for nested fields."""
    out = {"default": {}, "range": {}, "options": {}}
    for k, sch in (props or {}).items():
        if not isinstance(sch, dict):
            continue
        key = f"{prefix}-{k}" if prefix else k
        if "default" in sch:
            out["default"][key] = sch["default"]
        if isinstance(sch.get("enum"), list):
            out["options"][key] = sch["enum"]
        if sch.get("minimum") is not None and sch.get("maximum") is not None:
            out["range"].setdefault(key, {}).update(min=sch["minimum"], max=sch["maximum"])
        for sub in (sch.get("items") if isinstance(sch.get("items"), dict) else {}, sch):
            if sub is not None and isinstance(sub.get("properties"), dict):
                deeper = _schema_caps(sub["properties"], key)
                for part in out:
                    out[part].update(deeper[part])
    return out


def model_caps(model, path):
    """getCaps("<Group>/<Name>"): ranges, options and defaults of one setting."""
    group, _, name = str(path).partition("/")
    d = (model or {}).get(group) if isinstance(model, dict) else None
    e = None
    for k in ("props", "actions", "events"):
        for x in (d or {}).get(k) or []:
            if isinstance(x, dict) and x.get("identifier") == name:
                e = e or x
    if not e:
        return {"default": {}, "range": {}, "options": {}}
    if isinstance(e.get("schema"), dict):
        return _schema_caps(e["schema"].get("properties"))
    props = {}
    for side in ("input", "output"):
        sch = (e.get(side) or {}).get("schema") if isinstance(e.get(side), dict) else None
        if isinstance(sch, dict):
            props.update(sch.get("properties") or (sch.get("items") or {}).get("properties") or {})
    return _schema_caps(props)


# ---- read + normalise -----------------------------------------------------------------
def _poe_mode(e):
    if not _on(e.get("enabled")):
        return "off"
    pm = str(e.get("poeMode") or "")
    if pm:
        return {"standard": "auto", "compat": "compat", "force": "force"}.get(pm, pm)
    return "compat" if _on(e.get("nonStandardEnabled")) else "auto"


def _poe_modes(e, current):
    modes = list(POE_MODES[:2])
    if "poeMode" in e or "nonStandardEnabled" in e:
        modes.append("compat")
    if current not in modes:
        modes.append(current)              # e.g. "force", set on the switch itself
    return modes


def _pd_detail(e):
    pd = e.get("PDTestInfo") if isinstance(e.get("PDTestInfo"), dict) else {}
    for k in ("detectAbnormal", "classAbnormal", "powerAbnormal", "powerupAbnormal", "errInfo",
              "nonStandardPDStat"):
        v = pd.get(k)
        if v:
            return PD_DETAIL.get(v, str(v))
    return ""


def _pd_class(e):
    pd = e.get("PDTestInfo") if isinstance(e.get("PDTestInfo"), dict) else {}
    m = re.fullmatch(r"class\s*(\d)", str(pd.get("testLevel") or "").strip().lower())
    return int(m.group(1)) if m else None


def normalize(raw):
    """The switch's answers -> the snapshot shape of edgeswitch.normalize()."""
    raw = raw or {}
    summ = raw.get("summary") if isinstance(raw.get("summary"), dict) else {}
    status = sorted((r for r in raw.get("status") or [] if _int(r.get("ID")) is not None),
                    key=lambda r: _int(r.get("ID")))
    basic = {_int(r.get("ID")): r for r in raw.get("basic") or []}
    stats = {_int(r.get("ID")): r for r in raw.get("stats") or []}
    poe = {_int(r.get("portID")): r for r in raw.get("poe") or []}
    loops = {_int(r.get("portID")): r for r in raw.get("loop") or []}
    by_name = {str(r.get("name")): _int(r.get("ID")) for r in status if r.get("name")}
    watchdog = _on((raw.get("poe_watchdog") or {}).get("enabled")) if isinstance(raw.get("poe_watchdog"), dict) else False

    macs_by_port, seen = {}, set()
    for m in raw.get("macs") or []:
        mac = normalize_mac(m.get("macAddress") or m.get("mac"))
        n = _int(m.get("portID"))
        if n is None and m.get("portName") is not None:
            n = by_name.get(str(m.get("portName")))
        if not mac or not n or (mac, n) in seen:
            continue
        seen.add((mac, n))
        vlan = _int(m.get("VLANID", m.get("vlanID", m.get("vid"))))
        macs_by_port.setdefault(_pid(n), []).append({"mac": mac, "vlan": vlan or None})

    ports = []
    for st in status:
        n = _int(st.get("ID"))
        b, s, e = basic.get(n) or {}, stats.get(n) or {}, poe.get(n)
        pid = _pid(n)
        enabled = _on(b.get("en", st.get("en", 1)))
        up = _int(st.get("lnkSta")) == 1 and enabled
        rate, dx = _int(st.get("rate")), _int(st.get("dx"))
        desc = str(b.get("des") or st.get("describe") or st.get("des") or "").strip()
        if desc == "--":
            desc = ""
        poe_w = None
        mode, modes, pstat, fault = "", [], "", False
        if e is not None:
            mode = _poe_mode(e)
            modes = _poe_modes(e, mode)
            w = _num(e.get("poePower"))
            if w is None:
                w = _num(st.get("poePow"))
            poe_w = round(max(0.0, w), 1) if w is not None else (0.0 if mode == "off" else None)
            code = str(e.get("portStatus") or "")
            fault = code in POE_FAULTS
            pstat = POE_STATUS.get(code, code)
            if code == "normal" and mode != "off" and (poe_w or 0) < 0.5:
                pstat = "waiting for load"
            elif code == "normal" and _pd_class(e) is not None:
                pstat = f"{pstat} (class {_pd_class(e)})"
            detail = _pd_detail(e)
            if detail and (fault or code == "detectAbnormal"):
                pstat = f"{pstat}: {detail}"
        cfg_rate, cfg_dx = _int(b.get("rate")), _int(b.get("dx"))
        speed_cfg = ""
        if cfg_rate is not None:
            speed_cfg = "auto" if cfg_rate == 0 and cfg_dx in (3, None) else \
                "-".join(x for x in (RATE_TXT.get(cfg_rate, ""), DX_TXT.get(cfg_dx, "")) if x)
        loop = loops.get(n) or {}
        blocked = str(loop.get("loopProtectionStatus") or "").lower() == "block"
        ports.append({
            "id": pid, "type": "sfp" if _int(st.get("lnkMode")) in (1, 4, 5) else "port",
            "name": desc, "label": str(st.get("name") or ""), "mac": "",
            "enabled": enabled, "up": up,
            "speed": SPEED.get(rate) if up else None,
            "duplex": ({1: "full", 0: "half"}.get(dx)) if up else None,
            "speed_cfg": speed_cfg, "mtu": None,
            "poe_supported": e is not None,
            "poe_mode": mode, "poe_modes": modes,
            "poe_status": pstat, "poe_w": poe_w, "poe_ma": None, "poe_fault": bool(fault),
            "rx_bps": _num(s.get("recvBytesSpeed")), "tx_bps": _num(s.get("sendBytesSpeed")),
            "rx_bytes": _int(s.get("recvBytes")), "tx_bytes": _int(s.get("sendBytes")),
            "errors": _int(s.get("inErrorPacket"), _int(s.get("CRCPacket"))),
            "dropped": _int(s.get("inLossPkt")),
            "stp_state": "blocked by loop protection" if blocked else "",
            "loop_blocked": blocked,
            "isolated": False, "ping_watchdog": bool(watchdog and e is not None),
            "sfp": None,
            "vlans": {"untagged": [], "tagged": []},
            "macs": macs_by_port.get(pid, []),
        })

    def first(lst, key):
        if isinstance(lst, list) and lst and isinstance(lst[0], dict):
            return _num(lst[0].get(key))
        return None

    desc = raw.get("description") if isinstance(raw.get("description"), dict) else {}
    model = str(summ.get("model") or raw.get("model_hint") or "")
    name = str(desc.get("deviceName") or summ.get("deviceName") or "").strip()
    if name.upper() in ("", model.upper(), "SWITCH", "HIKVISION"):
        name = ""
    serial = str(summ.get("serialNumber") or "")
    full = str(desc.get("serialNumber") or "").strip()
    if full and (not serial or full.endswith(serial)):
        serial = full
    budget = _num(summ.get("maxPower"))
    poe_ports = [p for p in ports if p["poe_supported"]]
    used = _num(summ.get("power"))
    if used is None:
        used = round(sum(p["poe_w"] or 0 for p in poe_ports), 1)
    services = raw.get("services") if isinstance(raw.get("services"), dict) else {}

    def svc_on(k):
        v = services.get(k)
        return _on(v.get("enabled")) if isinstance(v, dict) and "enabled" in v else None

    snap = {
        "ok": True, "ts": int(time.time()), "os": "hikvision",
        "device": {
            "model": model, "product": "Hikvision smart switch", "family": "hikswitch",
            "mac": normalize_mac(summ.get("macAddress")), "name": name,
            "firmware": str(summ.get("softwareVersion") or ""), "serial": serial,
            "hardware": str(summ.get("hardwareVersion") or ""),
            "uptime": _int(summ.get("deviceUpTime")), "ip": str(summ.get("ipv4Address") or ""),
        },
        "caps": {"locate": False, "cable_test": True, "backup": False, "reboot": True,
                 "poe_cycle": bool(poe_ports), "rename_port": True, "advanced": True},
        "health": {"cpu": first(summ.get("CPUList"), "CPUUtilization"),
                   "ram": first(summ.get("memoryList"), "memoryUtilization"),
                   "temps": [], "temp": None, "fans": [], "psu": []},
        "poe": {"budget_w": budget if budget else None, "used_w": round(used, 1) if used is not None else None,
                "pct": round(used / budget * 100) if budget and used is not None else None,
                "powered": sum(1 for p in poe_ports if (p["poe_w"] or 0) >= 0.5),
                "unmeasured": 0, "peak_7d_w": _num(summ.get("day7PeakPower")),
                "watchdog": watchdog},
        "ports": ports, "lags": [], "vlans": [],
        "services": {"http": True, "ssh": svc_on("ssh"), "telnet": svc_on("telnet"),
                     "snmp": svc_on("snmp"), "snmp_community": None,
                     "cloud": _on(summ.get("EZEnabled")) if "EZEnabled" in summ else None},
        "neighbors": [],
        "stats_ts": None,
        "loops": [p["id"] for p in ports if p["loop_blocked"]],
    }
    snap["summary"] = {
        "ports": len(ports), "up": sum(1 for p in ports if p["up"]),
        "disabled": sum(1 for p in ports if not p["enabled"]),
        "rx_bps": sum(p["rx_bps"] or 0 for p in ports), "tx_bps": sum(p["tx_bps"] or 0 for p in ports),
        "errors": sum(p["errors"] or 0 for p in ports),
    }
    return snap


# (raw key, how to read it) — optional parts; a switch without one still reads
_OPTIONAL = (
    ("basic", lambda s: s.search("PortMgr", "SearchPortBasicParam", page=1000)),
    ("stats", lambda s: s.search("PortMgr", "SearchPortStatistics", page=100)),
    ("poe", lambda s: s.search("POE", "SearchPortPoeInfo", list_key="portPoeInfoList", page=100)),
    ("macs", lambda s: s.search("L2TableMgr", "SearchPortMacAddress", page=512, mac_paging=True)),
    ("loop", lambda s: s.search("STPManage", "SearchPortPrivateLoopStatus", page=1000)),
    ("poe_watchdog", lambda s: s.attr("POE", "PoEWatchdogParams")),
    ("description", lambda s: s.attr("InfoMgr", "DeviceDescription")),
)
_SERVICES = (("ssh", "SystemMaintenance", "SSHCfg"), ("telnet", "SystemMaintenance", "TelnetServerCfg"),
             ("snmp", "SNMPAccess", "SNMPCfg"))


def read(ip, username, password, with_neighbors=False, keep_raw=False):
    """Read everything a dashboard needs. Never raises: {ok: False, error, kind}."""
    t0 = time.time()
    raw = {}
    try:
        with Session(ip, username, password) as s:
            raw["summary"] = s.svc("PortMgr", "GetSwitchSumInfo") or {}
            raw["status"] = s.search("PortMgr", "SearchPortStatus", page=1000)
            skip = _UNSUPPORTED.setdefault(ip, set())
            for name, fn in _OPTIONAL:
                if name in skip:
                    raw[name] = None
                    continue
                try:
                    raw[name] = fn(s)
                except HikSwitchError as e:
                    if e.kind == "unreachable":
                        raise
                    raw[name] = None
                    raw.setdefault("_missing", {})[name] = str(e)
                    if _not_supported(e):
                        skip.add(name)
            raw["services"] = {}
            for key, group, attr in _SERVICES:
                if "services." + key in skip:
                    continue
                try:
                    raw["services"][key] = s.attr(group, attr)
                except HikSwitchError as e:
                    if e.kind == "unreachable":
                        raise
                    raw.setdefault("_missing", {})["services." + key] = str(e)
                    if _not_supported(e):
                        skip.add("services." + key)
            if skip:
                raw["_not_supported"] = sorted(skip)
    except HikSwitchError as e:
        return {"ok": False, "error": str(e), "kind": e.kind, "ip": ip}
    if not raw.get("status"):
        return {"ok": False, "error": "the switch listed no ports", "kind": "error", "ip": ip}
    snap = normalize(raw)
    snap["ip"] = ip
    snap["read_s"] = round(time.time() - t0, 1)
    if raw.get("_missing"):
        snap["missing"] = raw["_missing"]
    if keep_raw:
        snap["raw"] = raw
    return snap


def identify(ip, username="admin", password="", timeout=(4, 8)):
    """Model/firmware/name with the given login (used by the login test)."""
    try:
        with Session(ip, username, password, timeout=timeout) as s:
            summ = s.svc("PortMgr", "GetSwitchSumInfo") or {}
        return {"ok": True, "model": str(summ.get("model") or ""), "firmware": str(summ.get("softwareVersion") or ""),
                "serial": str(summ.get("serialNumber") or ""), "identity": str(summ.get("deviceName") or "")}
    except HikSwitchError as e:
        return {"ok": False, "error": str(e), "kind": e.kind}


# ---- writes (gated in server.py) --------------------------------------------------------
def _ports(s):
    return {_int(r.get("ID")): r for r in s.search("PortMgr", "SearchPortStatus", page=1000)}


def _check_port(s, port_id):
    n = _port_no(port_id)
    if n is None or n not in _ports(s):
        raise HikSwitchError(f"port {port_id} not found on the switch")
    return n


def _basic_row(s, n):
    for r in s.search("PortMgr", "SearchPortBasicParam", page=1000):
        if _int(r.get("ID")) == n:
            return r
    raise HikSwitchError(f"the switch has no settings for port {n}")


def _poe_row(s, n):
    for r in s.search("POE", "SearchPortPoeInfo", list_key="portPoeInfoList", page=100):
        if _int(r.get("portID")) == n:
            return r
    return None


def _port_state(s, n):
    b = _basic_row(s, n)
    e = _poe_row(s, n)
    return {"enabled": _on(b.get("en")), "name": str(b.get("des") or "").strip(),
            "poe": _poe_mode(e) if e is not None else None}


def _modify_basic(s, n, enabled=None, describe=None):
    """Port on/off or its description — the same list the switch's own page sends,
    with the speed/duplex/flow-control it already has, so nothing else changes."""
    b = _basic_row(s, n)
    item = {"portID": n,
            "enabled": _on(b.get("en")) if enabled is None else bool(enabled),
            "speed": RATE_TXT.get(_int(b.get("rate")), "auto"),
            "duplexMode": DX_TXT.get(_int(b.get("dx")), "auto"),
            "flowCtrlEnabled": _on(b.get("flowCtrlEn"))}
    if describe is not None:
        item["describe"] = describe[:32]
    s.svc("PortMgr", "ModifyBatchPortBasicParam", {"portBasicList": [item]})


def _poe_item(e, n, mode):
    item = {"portID": n, "enabled": mode != POE_OFF}
    if mode == POE_OFF:
        if "nonStandardEnabled" in e:
            item["nonStandardEnabled"] = _on(e.get("nonStandardEnabled"))
        return item
    if "poeMode" in e:
        item["poeMode"] = {"compat": "compat", "force": "force"}.get(mode, "standard")
    elif "nonStandardEnabled" in e:
        item["nonStandardEnabled"] = mode == "compat"
    elif mode == "compat":
        raise HikSwitchError("this switch has no PoE mode for non-standard devices")
    return item


def set_port(ip, username, password, port_id, enabled=None, poe=None, name=None):
    """Change one port: enabled (bool), poe ('off' | 'auto' | 'compat'), name."""
    if poe is not None and poe not in POE_MODES:
        return {"ok": False, "error": f"PoE mode must be one of {', '.join(POE_MODES)}"}
    try:
        with Session(ip, username, password) as s:
            n = _check_port(s, port_id)
            before = _port_state(s, n)
            if enabled is not None or name is not None:
                _modify_basic(s, n, enabled=enabled, describe=None if name is None else str(name).strip())
            if poe is not None:
                e = _poe_row(s, n)
                if e is None:
                    raise HikSwitchError(f"port {port_id} has no PoE")
                s.svc("POE", "ModifyPortPoeInfo", [_poe_item(e, n, poe)])
            after = _port_state(s, n)
        return {"ok": True, "port": port_id, "before": before, "after": after}
    except HikSwitchError as e:
        return {"ok": False, "error": str(e), "kind": e.kind}


def poe_cycle(ip, username, password, port_id, off_s=8):
    """PoE off, wait, the old mode back — restarts a hung camera or radio."""
    off_s = max(3, min(int(off_s or 8), 60))
    try:
        with Session(ip, username, password) as s:
            n = _check_port(s, port_id)
            e = _poe_row(s, n)
            if e is None:
                return {"ok": False, "error": f"port {port_id} has no PoE"}
            mode = _poe_mode(e)
            if mode == POE_OFF:
                return {"ok": False, "error": "PoE is not switched on on this port"}
            back = dict(e)
            s.svc("POE", "ModifyPortPoeInfo", [_poe_item(e, n, POE_OFF)])
            time.sleep(off_s)
            try:
                if mode == "force":                    # put back exactly what was there
                    s.svc("POE", "ModifyPortPoeInfo", [{"portID": n, "enabled": True, "poeMode": "force"}])
                else:
                    s.svc("POE", "ModifyPortPoeInfo", [_poe_item(back, n, mode)])
            except HikSwitchError as err:
                return {"ok": False, "error": f"PoE was switched OFF but turning it back on failed: {err} "
                                              f"— switch PoE on again on port {port_id}", "kind": err.kind}
        return {"ok": True, "port": port_id, "mode": mode, "off_s": off_s}
    except HikSwitchError as e:
        return {"ok": False, "error": str(e), "kind": e.kind}


def reboot(ip, username, password):
    try:
        s = Session(ip, username, password)
        s.login()
        try:
            s.svc("SystemMaintenance", "SystemReboot", timeout=(5, 10))
        except HikSwitchError as e:
            if e.kind != "unreachable":        # a switch that drops the line is rebooting
                raise
        finally:
            s.tag = None                       # no logout: it is going down
            s.http.close()
        return {"ok": True}
    except HikSwitchError as e:
        return {"ok": False, "error": str(e), "kind": e.kind}


def locate(ip, username, password, on=True):
    return {"ok": False, "error": "This Hikvision switch has no find-me LEDs"}


# What the cable test answers (description codes, the switch's own English).
CABLE_TEXT = {
    "NORMAL": "cable is normal",
    "RX_SHORT": "the receive pair is short-circuited", "TX_SHORT": "the transmit pair is short-circuited",
    "RX_OPEN": "the receive pair is open (broken or not connected)",
    "TX_OPEN": "the transmit pair is open (broken or not connected)",
    "A_SHORT": "pair A is short-circuited", "B_SHORT": "pair B is short-circuited",
    "C_SHORT": "pair C is short-circuited", "D_SHORT": "pair D is short-circuited",
    "A_OPEN": "pair A is open (broken or not connected)", "B_OPEN": "pair B is open (broken or not connected)",
    "C_OPEN": "pair C is open (broken or not connected)", "D_OPEN": "pair D is open (broken or not connected)",
    "A_HIIMPEDANCE": "pair A has high impedance (bad crimp, corrosion or a damaged core)",
    "B_HIIMPEDANCE": "pair B has high impedance (bad crimp, corrosion or a damaged core)",
    "C_HIIMPEDANCE": "pair C has high impedance (bad crimp, corrosion or a damaged core)",
    "D_HIIMPEDANCE": "pair D has high impedance (bad crimp, corrosion or a damaged core)",
    "OTHER": "another cable fault", "FAIL": "the test failed",
}
CABLE_ERRORS = {
    "0x004000bb": "The switch can't test a cable while the port runs at 10 Mbit/s (long-range ports run at 10).",
    "0x004000bc": "This port has no cable tester (the SFP ports don't).",
    "0x004000bd": "The cable test failed — try again.",
    "0x004000be": "A cable test is already running on this switch — try again in a minute.",
}


def cable_result(res):
    """GetPortLineStatus {lineStatus, lineLength, description} -> plain words."""
    res = res if isinstance(res, dict) else {}
    codes = [c for c in re.split(r"[\s,;|]+", str(res.get("description") or "").strip()) if c]
    good = bool(codes) and all(c.upper() == "NORMAL" for c in codes)
    return {"good": good, "status": str(res.get("lineStatus") or ""), "length_m": _num(res.get("lineLength")),
            "codes": codes, "text": "; ".join(CABLE_TEXT.get(c.upper(), c) for c in codes) or "no answer"}


def cable_test(ip, username, password, port_id):
    """The switch's own cable tester (Cable Detection) on one copper port. The link
    drops for a few seconds while it measures; the server refuses it on the Pi's
    and the router's port."""
    n = _port_no(port_id)
    if n is None:
        return {"ok": False, "error": f"bad port {port_id!r}"}
    try:
        with Session(ip, username, password, timeout=(5, 45)) as s:
            _check_port(s, port_id)
            res = s.svc("PortMgr", "GetPortLineStatus", {"portID": n}, timeout=(5, 45)) or {}
        return {"ok": True, "port": port_id, "result": cable_result(res)}
    except HikSwitchError as e:
        known = CABLE_ERRORS.get(str(e.code or "").lower())
        if not known and _not_supported(e):
            known = "This switch has no cable tester."
        return {"ok": False, "error": known or str(e), "kind": e.kind}


def backup(ip, username, password):
    return {"ok": False, "error": "Config backups of Hikvision switches aren't supported yet "
                                  "(the switch encrypts the file)"}
