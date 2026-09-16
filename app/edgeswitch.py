"""Ubiquiti EdgeSwitch / UISP Switch management over the switch's own JSON API.

The newer EdgeSwitch firmware (ES-8-150W, ES-10X, EP-S16, UISP-S …) serves one
React page and does everything through a REST API under /api/v1.0 — the same
calls its web page makes, so reading and changing the switch never needs the
web page itself:

  POST /user/login {username, password}  -> token in the `x-auth-token` header
  GET  /device            identification + firmware + capabilities per port
  GET  /system            hostname, services, NTP, syslog …
  GET  /interfaces        per-port config and link status
  PUT  /interfaces        [full interface objects]   (how the page saves a port)
  GET  /statistics        [{timestamp, device{…}, interfaces[{id, statistics}]}]
  GET  /vlans, /services, /tools/mac-table
  POST /tools/discovery/neighbors, /tools/cable-test, /system/reboot,
       /device/locate/start|stop;  GET /system/backup (tar.gz)
  GET  /public/device     model WITHOUT a login

The switch refuses any POST that does not say which page it came from: without
an Origin/Referer header even the login gets lighttpd's HTML 403 before the
credentials are looked at (found live on Bennie's ES-8-150W, 2026-09-16). With
the header, a wrong login is a JSON 401 "User account invalid" — the only real
"wrong password" answer — and a missing/expired token is also 401.
Found by reading the switch's own web code (2026-09-15). Firmware drifts, so
every field is read through candidate lists and the raw answers stay available.

Reads are always allowed. Writes are gated by the site's `switch_manage` flag in
server.py, and ports that carry the Netwatch Pi or the site's router are
refused there (switchmon.protected_ports) — switching those off would cut the
site off with nobody able to switch them back on.
"""
import re
import time

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

API = "/api/v1.0"
TIMEOUT = (6, 25)
POE_OFF = "off"
_MAC_RE = re.compile(r"^[0-9a-f]{2}([:-]?[0-9a-f]{2}){5}$", re.I)


class SwitchError(Exception):
    def __init__(self, msg, kind="error", status=None):
        super().__init__(msg)
        self.kind = kind            # auth_failed | unreachable | error
        self.status = status


def is_edgeswitch(dev):
    """A Ubiquiti switch that runs the JSON-API firmware. The scanner's HTTP
    banner shows the page title ("Ubiquiti EdgeSwitch"); a model we learned
    from the switch itself (ES-…, EP-S…, UISP-S…) counts too."""
    vendor = (dev.get("vendor") or "").lower()
    title = ((dev.get("banner") or {}).get("title") or "").lower()
    # The page title names Ubiquiti itself — newer MAC blocks (d8:b3:70…) are
    # missing from the offline OUI list, so the vendor can be blank.
    if "ubiquiti" not in vendor and "ubnt" not in vendor and "ubiquiti" not in title:
        return False
    model = (dev.get("model") or "").upper()
    return ("edgeswitch" in title or "uisp switch" in title
            or model.startswith(("ES-", "EP-S", "UISP-S")))


def normalize_mac(mac):
    m = re.sub(r"[^0-9a-f]", "", str(mac or "").lower())
    return ":".join(m[i:i + 2] for i in range(0, 12, 2)) if len(m) == 12 else ""


def poe_budget_w(model, product=""):
    """PoE budget from the model name: ES-8-150W -> 150. None when unknown."""
    m = re.search(r"(\d{2,3})\s*W\b", f"{model} {product}", re.I)
    return int(m.group(1)) if m else None


def _get(d, path, default=None):
    """Nested lookup: _get(x, "status.plugged")."""
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _first(d, *paths):
    for p in paths:
        v = _get(d, p)
        if v not in (None, ""):
            return v
    return None


def _num(v):
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        return float(str(v).strip().split()[0].rstrip("%"))
    except (ValueError, IndexError):
        return None


def speed_mbps(s):
    """'1000-full' / '100-half' / '10G-full' / 'auto' -> (mbps, duplex)."""
    s = str(s or "").lower()
    m = re.match(r"(\d+(?:\.\d+)?)\s*(g?)", s)
    if not m:
        return None, None
    mbps = float(m.group(1)) * (1000 if m.group(2) == "g" else 1)
    duplex = "half" if "half" in s else "full" if "full" in s else None
    return int(mbps), duplex


# ---- session ------------------------------------------------------------------
class Session:
    """One logged-in conversation with a switch. Use as a context manager so the
    token is released (the switch keeps a small table of sessions)."""

    def __init__(self, ip, username, password, timeout=TIMEOUT):
        self.ip, self.username, self.password = ip, username or "", password or ""
        self.timeout = timeout
        self.base = None
        self.http = requests.Session()
        self.http.verify = False
        self.http.headers["User-Agent"] = "Netwatch"

    # The page is served on both 80 and 443; HTTPS first, plain HTTP only when
    # 443 is closed (an old firmware with HTTPS switched off).
    def _bases(self):
        return [f"https://{self.ip}", f"http://{self.ip}"]

    def _page_headers(self, base):
        # What the switch's own page sends. Without them every POST is a 403.
        self.http.headers["Origin"] = base
        self.http.headers["Referer"] = base + "/"

    def login(self):
        last = None
        for base in self._bases():
            self._page_headers(base)
            try:
                r = self.http.post(base + API + "/user/login",
                                   json={"username": self.username, "password": self.password},
                                   timeout=self.timeout)
            except requests.RequestException as e:
                last = e
                continue
            tok = r.headers.get("x-auth-token")
            if r.status_code == 200 and tok:
                self.base = base
                self.http.headers["x-auth-token"] = tok
                return self
            if r.status_code == 401:
                raise SwitchError("the switch rejected the saved username/password",
                                  "auth_failed", r.status_code)
            if r.status_code == 403:
                raise SwitchError("the switch refused the login request (HTTP 403) — its web "
                                  "server blocked it before checking the password", "error", 403)
            raise SwitchError(f"login failed (HTTP {r.status_code})", "error", r.status_code)
        raise SwitchError(f"switch not reachable: {last}", "unreachable")

    def logout(self):
        if self.base:
            try:
                self.http.post(self.base + API + "/user/logout", timeout=(3, 5))
            except requests.RequestException:
                pass
        self.http.close()

    def __enter__(self):
        return self.login()

    def __exit__(self, *exc):
        self.logout()

    def call(self, method, path, body=None, timeout=None, raw=False):
        try:
            r = self.http.request(method, self.base + API + path, json=body,
                                  timeout=timeout or self.timeout)
        except requests.RequestException as e:
            raise SwitchError(f"{method} {path}: {e}", "unreachable")
        if r.status_code == 401:
            raise SwitchError("the switch ended the session", "auth_failed", 401)
        if r.status_code >= 400:
            detail = ""
            try:
                j = r.json()
                detail = j.get("message") or j.get("detail") or ""
            except ValueError:
                pass
            raise SwitchError(f"{method} {path} failed (HTTP {r.status_code}) {detail}".strip(),
                              "error", r.status_code)
        if raw:
            return r
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return r.text

    def get(self, path, **kw):
        return self.call("GET", path, **kw)


def public_device(ip, timeout=(4, 8)):
    """Model/product without any login — safe to call on any Ubiquiti switch."""
    for base in (f"https://{ip}", f"http://{ip}"):
        try:
            r = requests.get(base + API + "/public/device", verify=False, timeout=timeout)
            if r.ok:
                j = r.json()
                ident = j.get("identification") or {}
                return {"ok": True, "model": ident.get("model") or "",
                        "product": ident.get("product") or "", "family": ident.get("family") or "",
                        "factory_default": bool(j.get("isFactoryDefault"))}
        except (requests.RequestException, ValueError):
            continue
    return {"ok": False}


# ---- read + normalise ------------------------------------------------------------
def _list(x):
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        for k in ("items", "data", "entries", "table", "neighbors", "vlans"):
            if isinstance(x.get(k), list):
                return x[k]
    return []


def _latest_stats(stats):
    rows = [s for s in _list(stats) if isinstance(s, dict)]
    if isinstance(stats, dict) and not rows:
        rows = [stats]
    return max(rows, key=lambda s: s.get("timestamp") or 0) if rows else {}


_PORT_ID_RE = re.compile(r"^\d+/\d+$")


def _mac_entries(table):
    """MAC table rows -> [(mac, port_id, vlan)], whatever the field names: the
    MAC is the MAC-looking string, the port the 'n/m' id (plain or nested)."""
    out = []
    for e in _list(table):
        if not isinstance(e, dict):
            continue
        mac = port = vlan = None
        for k, v in e.items():
            lk = k.lower()
            if isinstance(v, dict):
                nested = str(v.get("id") or _get(v, "identification.id") or "")
                if _PORT_ID_RE.match(nested) and ("port" in lk or "interface" in lk):
                    port = nested
            elif isinstance(v, str) and _MAC_RE.match(v) and (mac is None or "mac" in lk):
                mac = v
            elif isinstance(v, (str, int)) and "vlan" in lk:
                vlan = v
            elif isinstance(v, str) and _PORT_ID_RE.match(v) and ("port" in lk or "interface" in lk or port is None):
                port = v
        if mac and port:
            out.append((normalize_mac(mac), port, vlan))
    return out


def _health(dev_stats):
    """CPU / RAM / temperatures / fans / PSU from a statistics sample's device part."""
    h = {}
    cpu = dev_stats.get("cpu")
    if isinstance(cpu, list):
        vals = [_num(c.get("usage")) for c in cpu if isinstance(c, dict)]
        vals = [v for v in vals if v is not None]
        h["cpu"] = round(sum(vals) / len(vals)) if vals else None
    elif cpu is not None:
        h["cpu"] = _num(cpu if not isinstance(cpu, dict) else cpu.get("usage"))
    ram = dev_stats.get("ram")
    if isinstance(ram, dict):
        h["ram"] = _num(ram.get("usage"))
        if h["ram"] is None and _num(ram.get("total")) and _num(ram.get("free")) is not None:
            h["ram"] = round(100 * (1 - _num(ram["free"]) / _num(ram["total"])))
    temps = []
    for t in _list(dev_stats.get("temperatures") or dev_stats.get("temperature")):
        if isinstance(t, dict) and _num(t.get("value")) is not None:
            temps.append({"name": t.get("name") or t.get("type") or "", "value": _num(t.get("value"))})
    h["temps"] = temps
    h["temp"] = max((t["value"] for t in temps), default=None)
    fans = [_num(f.get("value")) for f in _list(dev_stats.get("fanSpeeds") or dev_stats.get("fans"))
            if isinstance(f, dict)]
    h["fans"] = [f for f in fans if f is not None]
    psus = []
    for p in _list(dev_stats.get("power")):
        if isinstance(p, dict):
            psus.append({"type": p.get("psuType") or p.get("type") or "", "connected": p.get("connected"),
                         "voltage": _num(p.get("voltage")), "power": _num(p.get("power"))})
    h["psu"] = psus
    h["uptime"] = _num(dev_stats.get("uptime"))
    return h


def normalize(device, system, interfaces, statistics, vlans=None, mac_table=None,
              services=None, neighbors=None):
    """The switch's raw answers -> one snapshot shape the monitor and both UIs use."""
    device = device if isinstance(device, dict) else {}
    system = system if isinstance(system, dict) else {}
    ident = device.get("identification") or {}
    latest = _latest_stats(statistics)
    dev_stats = latest.get("device") or {}
    health = _health(dev_stats)

    caps = {}
    for c in _list(_get(device, "capabilities.interfaces")):
        if isinstance(c, dict) and c.get("id") is not None:
            caps[str(c["id"])] = c
    stats_by_id = {}
    for i in _list(latest.get("interfaces")):
        if isinstance(i, dict):
            iid = str(i.get("id") or _get(i, "identification.id") or "")
            stats_by_id[iid] = i.get("statistics") or i

    # VLAN participation per port
    vlan_list, port_vlans = [], {}
    for v in _list(vlans):
        if not isinstance(v, dict):
            continue
        vid = v.get("id") if v.get("id") is not None else v.get("vlanID")
        vlan_list.append({"id": vid, "name": v.get("name") or ""})
        for p in _list(v.get("participation")):
            if not isinstance(p, dict):
                continue
            pid = str(_get(p, "interface.id") or p.get("id") or "")
            mode = p.get("mode")
            if pid and mode:
                port_vlans.setdefault(pid, {"untagged": [], "tagged": []})
                port_vlans[pid]["untagged" if mode == "untagged" else "tagged"].append(vid)

    # The real table repeats a MAC once per address it has seen (IPv4, IPv6
    # link-local…): one entry per MAC per port, or every device shows twice.
    macs_by_port, seen = {}, set()
    for mac, port, vlan in _mac_entries(mac_table):
        if (mac, port) in seen:
            continue
        seen.add((mac, port))
        macs_by_port.setdefault(port, []).append({"mac": mac, "vlan": vlan})

    ports, lags = [], []
    for itf in _list(interfaces):
        if not isinstance(itf, dict):
            continue
        iid = str(_get(itf, "identification.id") or "")
        itype = (_get(itf, "identification.type") or "port").lower()
        if not iid:
            continue
        st = stats_by_id.get(iid) or {}
        cap = caps.get(iid) or itf.get("capabilities") or {}
        cur = _first(itf, "status.currentSpeed", "status.speed")
        mbps, duplex = speed_mbps(cur) if _get(itf, "status.plugged") else (None, None)
        poe_mode = _get(itf, "port.poe")
        sfp = _get(itf, "port.sfp") or {}
        entry = {
            "id": iid,
            "type": itype,
            "name": _get(itf, "identification.name") or "",
            "mac": _get(itf, "identification.mac") or "",
            "enabled": bool(_get(itf, "status.enabled", True)),
            "up": bool(_get(itf, "status.plugged")) and bool(_get(itf, "status.enabled", True)),
            "speed": mbps, "duplex": duplex,
            "speed_cfg": _get(itf, "status.speed") or "",
            "mtu": _get(itf, "status.mtu"),
            "poe_supported": bool(cap.get("supportPOE")) or poe_mode not in (None, ""),
            "poe_mode": poe_mode or "",
            "poe_modes": [m for m in (cap.get("poeValues") or []) if isinstance(m, str)],
            "poe_w": _num(st.get("poePower")),
            "rx_bps": _num(st.get("rxRate")), "tx_bps": _num(st.get("txRate")),
            "rx_bytes": _num(st.get("rxBytes")), "tx_bytes": _num(st.get("txBytes")),
            "errors": _num(st.get("errors")), "dropped": _num(st.get("dropped")),
            "stp_state": _get(itf, "port.stp.state") or "",
            "isolated": bool(_get(itf, "port.isolated")),
            "ping_watchdog": bool(_get(itf, "port.pingWatchdog.enabled")),
            "sfp": {"present": bool(sfp.get("present")), "vendor": sfp.get("vendor") or "",
                    "part": sfp.get("part") or "",
                    "temp": _num(_get(st, "sfp.temperature")),
                    "rx_power": _num(_get(st, "sfp.rxPower")),
                    "tx_power": _num(_get(st, "sfp.txPower"))} if sfp else None,
            "vlans": port_vlans.get(iid) or {"untagged": [], "tagged": []},
            "macs": macs_by_port.get(iid, []),
        }
        if itype == "lag":
            entry["members"] = [str(_get(m, "id") or m) for m in _list(_get(itf, "lag.interfaces"))]
            lags.append(entry)
        elif itype in ("port", "sfp", "sfp+") or re.match(r"^\d+/\d+$", iid):
            if not iid.startswith("3/"):          # 3/x are LAGs on this firmware
                ports.append(entry)

    def _pkey(p):
        a, _, b = p["id"].partition("/")
        try:
            return (int(a), int(b))
        except ValueError:
            return (99, 0)
    ports.sort(key=_pkey)

    model = ident.get("model") or ""
    budget = poe_budget_w(model, ident.get("product") or "")
    poe_used = round(sum(p["poe_w"] or 0 for p in ports), 1)
    # Passive PoE (24v/48v…) is switched on but never measured: no poePower at all
    # on those ports (ES-8-150W at Bennie, 2026-09-16). Count them separately so the
    # budget isn't shown as "0 W used" while six radios run off the switch.
    poe_on = [p for p in ports if p["up"] and p["poe_mode"] not in ("", "off")]
    unmeasured = sum(1 for p in poe_on if p["poe_w"] is None)
    svc = services if isinstance(services, dict) else {}
    snap = {
        "ok": True, "ts": int(time.time()),
        "device": {
            "model": model, "product": ident.get("product") or "",
            "family": ident.get("family") or "",
            "mac": normalize_mac(ident.get("mac")),
            "name": system.get("hostname") or ident.get("name") or "",
            "firmware": ident.get("firmwareVersion") or _first(device, "firmware.current", "firmwareVersion") or "",
            "serial": ident.get("serialNumber") or ident.get("serial") or "",
            "uptime": health.pop("uptime", None),
        },
        "health": health,
        "poe": {"budget_w": budget, "used_w": poe_used,
                "pct": round(100 * poe_used / budget) if budget and not unmeasured else None,
                "powered": sum(1 for p in poe_on if p["poe_w"] is None or p["poe_w"] >= 0.5),
                "unmeasured": unmeasured},
        "ports": ports, "lags": lags, "vlans": vlan_list,
        "services": {
            "ssh": _get(svc, "sshServer.enabled"), "telnet": _get(svc, "telnetServer.enabled"),
            "http": _get(svc, "webServer.enabled"), "snmp": _get(svc, "snmpAgent.enabled"),
            "snmp_community": _get(svc, "snmpAgent.community"),
            "ntp": _get(svc, "ntpClient.enabled"), "syslog": _get(svc, "systemLog.enabled"),
            "unms": _get(svc, "unms.enabled"),
        } if svc else {},
        "neighbors": [
            {"mac": normalize_mac(n.get("mac") or _get(n, "identification.mac")),
             "ip": n.get("ip") or _first(n, "addresses.0.address") or "",
             "name": n.get("hostname") or n.get("name") or _get(n, "identification.hostname") or "",
             "model": n.get("model") or _get(n, "identification.model") or n.get("platform") or "",
             "port": str(n.get("interface") or n.get("port") or "")}
            for n in _list(neighbors) if isinstance(n, dict)],
        "stats_ts": latest.get("timestamp"),
    }
    snap["summary"] = {
        "ports": len(ports), "up": sum(1 for p in ports if p["up"]),
        "disabled": sum(1 for p in ports if not p["enabled"]),
        "rx_bps": sum(p["rx_bps"] or 0 for p in ports), "tx_bps": sum(p["tx_bps"] or 0 for p in ports),
        "errors": sum((p["errors"] or 0) + (p["dropped"] or 0) for p in ports),
    }
    return snap


def read(ip, username, password, with_neighbors=False, keep_raw=False):
    """Log in and read everything a dashboard needs. Never raises: returns
    {ok: False, error, kind} on failure (kind = auth_failed | unreachable | error)."""
    t0 = time.time()
    try:
        with Session(ip, username, password) as s:
            raw = {}
            for name, path in (("device", "/device"), ("system", "/system"),
                               ("interfaces", "/interfaces"), ("statistics", "/statistics")):
                raw[name] = s.get(path)
            # Nice-to-have parts: a firmware without one of them still gives a dashboard.
            for name, path, method in (("vlans", "/vlans", "GET"), ("services", "/services", "GET"),
                                       ("mac_table", "/tools/mac-table", "GET")):
                try:
                    raw[name] = s.call(method, path)
                except SwitchError as e:
                    raw[name] = None
                    raw.setdefault("_missing", {})[name] = str(e)
            if with_neighbors:
                try:
                    raw["neighbors"] = s.call("POST", "/tools/discovery/neighbors", timeout=(6, 20))
                except SwitchError:
                    raw["neighbors"] = None
    except SwitchError as e:
        return {"ok": False, "error": str(e), "kind": e.kind, "ip": ip}
    snap = normalize(raw["device"], raw["system"], raw["interfaces"], raw["statistics"],
                     raw.get("vlans"), raw.get("mac_table"), raw.get("services"), raw.get("neighbors"))
    snap["ip"] = ip
    snap["read_s"] = round(time.time() - t0, 1)
    if raw.get("_missing"):
        snap["missing"] = raw["_missing"]
    if keep_raw:
        snap["raw"] = raw
    return snap


# ---- writes (gated in server.py) ---------------------------------------------------
def _find_interface(interfaces, port_id):
    for itf in _list(interfaces):
        if isinstance(itf, dict) and str(_get(itf, "identification.id")) == str(port_id):
            return itf
    return None


def _put_port(s, port_id, mutate):
    itf = _find_interface(s.get("/interfaces"), port_id)
    if itf is None:
        raise SwitchError(f"port {port_id} not found on the switch")
    before = {"enabled": _get(itf, "status.enabled"), "poe": _get(itf, "port.poe"),
              "name": _get(itf, "identification.name")}
    mutate(itf)
    s.call("PUT", "/interfaces", [itf])
    return before


def set_port(ip, username, password, port_id, enabled=None, poe=None, name=None):
    """Change one port: enabled (bool), poe (mode string, e.g. 'off' / 'active' /
    '24v'), name. Only the given fields change; the rest of the port is sent back
    exactly as the switch reported it (that is how its own page saves)."""
    def mutate(itf):
        if enabled is not None:
            itf.setdefault("status", {})["enabled"] = bool(enabled)
        if poe is not None:
            itf.setdefault("port", {})["poe"] = poe
        if name is not None:
            itf.setdefault("identification", {})["name"] = str(name)[:64]
    try:
        with Session(ip, username, password) as s:
            before = _put_port(s, port_id, mutate)
            after = _find_interface(s.get("/interfaces"), port_id) or {}
        return {"ok": True, "port": port_id, "before": before,
                "after": {"enabled": _get(after, "status.enabled"), "poe": _get(after, "port.poe"),
                          "name": _get(after, "identification.name")}}
    except SwitchError as e:
        return {"ok": False, "error": str(e), "kind": e.kind}


def poe_cycle(ip, username, password, port_id, off_s=8):
    """Turn PoE off, wait, and put back the mode the port had — restarts a hung
    camera or radio. The switch is told twice in one session; if the second call
    fails the port stays OFF, so the error says exactly that."""
    off_s = max(3, min(int(off_s or 8), 60))
    try:
        with Session(ip, username, password) as s:
            itf = _find_interface(s.get("/interfaces"), port_id)
            if itf is None:
                raise SwitchError(f"port {port_id} not found on the switch")
            mode = _get(itf, "port.poe")
            if not mode or mode == POE_OFF:
                return {"ok": False, "error": "PoE is not switched on on this port"}
            _put_port(s, port_id, lambda i: i.setdefault("port", {}).__setitem__("poe", POE_OFF))
            time.sleep(off_s)
            try:
                _put_port(s, port_id, lambda i: i.setdefault("port", {}).__setitem__("poe", mode))
            except SwitchError as e:
                return {"ok": False, "error": f"PoE was switched OFF but turning it back on failed: {e} "
                                              f"— set port {port_id} PoE to '{mode}' again", "kind": e.kind}
        return {"ok": True, "port": port_id, "mode": mode, "off_s": off_s}
    except SwitchError as e:
        return {"ok": False, "error": str(e), "kind": e.kind}


def reboot(ip, username, password):
    try:
        with Session(ip, username, password) as s:
            s.call("POST", "/system/reboot")
        return {"ok": True}
    except SwitchError as e:
        return {"ok": False, "error": str(e), "kind": e.kind}


def locate(ip, username, password, on=True):
    """Blink the switch's LEDs so someone on site finds the right box."""
    try:
        with Session(ip, username, password) as s:
            s.call("POST", "/device/locate/" + ("start" if on else "stop"), {})
        return {"ok": True, "locating": bool(on)}
    except SwitchError as e:
        return {"ok": False, "error": str(e), "kind": e.kind}


def cable_test(ip, username, password, port_id):
    """TDR cable test on one port. The link drops for a few seconds while it runs."""
    try:
        with Session(ip, username, password) as s:
            itf = _find_interface(s.get("/interfaces"), port_id)
            if itf is None:
                raise SwitchError(f"port {port_id} not found on the switch")
            res = s.call("POST", "/tools/cable-test", itf.get("identification") or {"id": port_id},
                         timeout=(6, 60))
        return {"ok": True, "port": port_id, "result": res}
    except SwitchError as e:
        return {"ok": False, "error": str(e), "kind": e.kind}


def backup(ip, username, password):
    """The switch's own configuration backup (a .tar.gz) as bytes."""
    try:
        with Session(ip, username, password) as s:
            r = s.call("GET", "/system/backup", raw=True, timeout=(6, 60))
            name = ""
            m = re.search(r'filename="?([^";]+)', r.headers.get("Content-Disposition", ""))
            if m:
                name = m.group(1)
        return {"ok": True, "content": r.content, "filename": name}
    except SwitchError as e:
        return {"ok": False, "error": str(e), "kind": e.kind}
