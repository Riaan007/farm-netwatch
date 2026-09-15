"""MikroTik / RouterOS discovery and management.

Three transports, mirroring the airos.py philosophy (one path, read-safe vs
write-gated), but here the FIND and the LOGIN are both meant to work from just
the MAC address, the way WinBox does:

  * MNDP  (UDP 5678) — the MikroTik Neighbor Discovery Protocol. Routers
    broadcast their identity/version/board/uptime/IP unsolicited; we also send a
    solicit so the list fills instantly. No credentials, no IP: this is the
    "find it by MAC, like WinBox's Neighbors tab" half. Pure Python, no deps.

  * MAC-Telnet (UDP 20561) — WinBox's "connect by MAC" login. Reaches the
    router over Layer 2 even when its IP is wrong / unknown / on another subnet.
    RouterOS 6.43+ replaced the old MD5 login with an elliptic-curve secure
    login ("mtwei", Curve25519 EC-SRP), so the ancient packaged mactelnet
    segfaults — we ship a current build of haakonnessjoen/MAC-Telnet in the
    image and drive it under a pseudo-terminal (see mt_run).

  * RouterOS API (TCP 8728) — used over the IP that MNDP hands us (the operator
    never types an IP; it is learned from the MAC broadcast). Fully structured
    reads and writes. The richer/faster path whenever the IP is reachable.

Preference order honours "MAC first, IP last": the terminal logs in over
MAC-Telnet; the structured status panel uses the API when the IP answers and
falls back to parsing `print terse` over MAC-Telnet when it does not, so the
whole feature still works from nothing but a MAC.

Writes (rename, enable/disable a port, PoE, reboot, backup, export) and the
free-form terminal are gated behind the `mikrotik_manage` feature flag and the
site login, and are logged — this is config power over a client's router.
"""
import fcntl
import hashlib
import ipaddress
import os
import pty
import re
import select
import signal
import socket
import struct
import subprocess
import termios
import time


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def normalize_mac(mac):
    """Return a lower-case colon MAC ('08:55:31:60:b6:76') or '' if not 12 hex."""
    if not mac:
        return ""
    h = re.sub(r"[^0-9a-fA-F]", "", mac).lower()
    return ":".join(h[i:i + 2] for i in range(0, 12, 2)) if len(h) == 12 else ""


def _mac_bytes(mac):
    h = re.sub(r"[^0-9a-fA-F]", "", mac or "")
    return bytes.fromhex(h) if len(h) == 12 else b""


def _valid_ip(ip):
    try:
        ipaddress.ip_address(ip)
        return True
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# MNDP — MikroTik Neighbor Discovery (find by MAC, no login)
# ---------------------------------------------------------------------------
MNDP_PORT = 5678

# TLV type -> field name (from the reference protocol.h MT_MNDPTYPE_*).
_MNDP_TLV = {
    1: "mac", 5: "identity", 7: "version", 8: "platform",
    10: "uptime", 11: "software_id", 12: "board", 16: "interface",
    14: "unpack", 15: "ipv6", 17: "ipv4",
}


def _parse_mndp(data):
    """Parse one MNDP datagram. 4-byte header, then big-endian type/len TLVs.
    Uptime is a little-endian u32; ipv4 is 4 raw bytes; strings are ASCII."""
    out = {}
    i = 4
    n = len(data)
    while i + 4 <= n:
        t, ln = struct.unpack_from(">HH", data, i)
        i += 4
        if i + ln > n:
            break
        v = data[i:i + ln]
        i += ln
        name = _MNDP_TLV.get(t, "t%d" % t)
        if name == "mac" and len(v) == 6:
            out["mac"] = ":".join("%02x" % b for b in v)
        elif name == "uptime":
            out["uptime"] = struct.unpack("<I", v)[0] if len(v) == 4 \
                else int.from_bytes(v, "little")
        elif name == "ipv4" and len(v) == 4:
            out["ipv4"] = ".".join(str(b) for b in v)
        elif name in ("identity", "version", "platform", "software_id",
                      "board", "interface"):
            out[name] = v.decode("ascii", "replace").strip("\x00").strip()
    return out


def discover(timeout=4.0, solicit=True):
    """Listen for MNDP announcements and return a list of neighbours, each:
    {mac, identity, version, board, platform, ipv4, interface, software_id,
     uptime}. Sends a broadcast solicit first so the list fills at once.

    Binds UDP 5678 on-demand (SO_REUSEADDR) so it never holds the port. Runs in
    the netwatch container's host-network namespace, so it sees the real LAN."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        s.bind(("0.0.0.0", MNDP_PORT))
    except OSError as e:
        s.close()
        return {"ok": False, "error": "cannot listen for MNDP (port 5678 busy: %s)" % e,
                "neighbors": []}
    if solicit:
        for _ in range(2):
            try:
                s.sendto(b"\x00\x00\x00\x00", ("255.255.255.255", MNDP_PORT))
            except OSError:
                pass
            time.sleep(0.15)
    seen = {}
    s.settimeout(0.5)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data, addr = s.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        if len(data) < 8:            # our own solicit echoes back
            continue
        d = _parse_mndp(data)
        mac = d.get("mac")
        if not mac:
            continue
        d.setdefault("ipv4", addr[0])
        # keep the freshest sighting per MAC
        seen[mac] = d
    s.close()
    return {"ok": True, "neighbors": sorted(seen.values(),
            key=lambda d: (d.get("identity") or "").lower())}


# ---------------------------------------------------------------------------
# RouterOS binary API (TCP 8728) — structured reads and writes over IP
# ---------------------------------------------------------------------------
API_PORT = 8728


class ApiError(Exception):
    pass


class RosApi:
    """Minimal RouterOS API client (length-prefixed word framing). Context
    manager: `with RosApi(ip, user, pw) as api: api.talk('/system/identity/print')`.

    Login handles both the post-6.43 plain login (returns !done directly) and
    the legacy MD5 challenge (=ret= then =response=). RouterOS 6.45 accepts the
    plain login; older boxes fall through to the challenge."""

    def __init__(self, ip, user="admin", password="", port=API_PORT, timeout=6.0):
        self.ip = ip
        self.user = user or "admin"
        self.password = password or ""
        self.port = port
        self.timeout = timeout
        self.sock = None

    # ---- framing ----
    @staticmethod
    def _enc_len(n):
        if n < 0x80:
            return bytes([n])
        if n < 0x4000:
            return (n | 0x8000).to_bytes(2, "big")
        if n < 0x200000:
            return (n | 0xC00000).to_bytes(3, "big")
        if n < 0x10000000:
            return (n | 0xE0000000).to_bytes(4, "big")
        return b"\xf0" + n.to_bytes(4, "big")

    def _recv(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ApiError("connection closed")
            buf += chunk
        return buf

    def _read_len(self):
        c = self._recv(1)[0]
        if c < 0x80:
            return c
        if c < 0xC0:
            return ((c & 0x3f) << 8) | self._recv(1)[0]
        if c < 0xE0:
            return ((c & 0x1f) << 16) | int.from_bytes(self._recv(2), "big")
        if c < 0xF0:
            return ((c & 0x0f) << 24) | int.from_bytes(self._recv(3), "big")
        return int.from_bytes(self._recv(4), "big")

    def _send(self, words):
        out = b""
        for w in words:
            b = w.encode("utf-8")
            out += self._enc_len(len(b)) + b
        out += b"\x00"
        self.sock.sendall(out)

    def _read_sentence(self):
        words = []
        while True:
            ln = self._read_len()
            if ln == 0:
                return words
            words.append(self._recv(ln).decode("utf-8", "replace"))

    def talk(self, *words):
        """Send one command sentence, collect the reply sentences until
        !done/!fatal. Returns list of {} rows from !re sentences. Raises ApiError
        on !trap/!fatal."""
        self._send(words)
        rows, error = [], None
        while True:
            s = self._read_sentence()
            if not s:
                continue
            tag = s[0]
            attrs = {}
            for w in s[1:]:
                if w.startswith("="):
                    k, _, v = w[1:].partition("=")
                    attrs[k] = v
            if tag == "!re":
                rows.append(attrs)
            elif tag == "!trap":
                error = attrs.get("message", "command failed")
            elif tag == "!fatal":
                raise ApiError(attrs.get("message") or (s[1] if len(s) > 1 else "fatal"))
            elif tag == "!done":
                if error:
                    raise ApiError(error)
                if attrs:
                    rows.append(attrs)     # e.g. =ret= from a scalar command
                return rows

    # ---- lifecycle ----
    def open(self):
        self.sock = socket.create_connection((self.ip, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        # plain login (6.43+). Reads the reply directly; if the box answered with
        # a legacy =ret= challenge, do the MD5 dance.
        self._send(["/login", "=name=" + self.user, "=password=" + self.password])
        rep, challenge = [], None
        while True:
            s = self._read_sentence()
            if not s:
                continue
            for w in s[1:]:
                if w.startswith("=ret="):
                    challenge = w[5:]
            if s[0] == "!fatal":
                raise ApiError("login rejected: " + (s[1] if len(s) > 1 else ""))
            if s[0] == "!trap":
                raise ApiError("login rejected")
            if s[0] == "!done":
                break
        if challenge:
            md = hashlib.md5(b"\x00" + self.password.encode() +
                             bytes.fromhex(challenge)).hexdigest()
            self.talk("/login", "=name=" + self.user, "=response=00" + md)
        return self

    def close(self):
        if self.sock:
            try:
                self._send(["/quit"])
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *a):
        self.close()


def _one(rows):
    return rows[0] if rows else {}


def api_status(ip, user="admin", password=""):
    """Read a RouterOS device's identity, hardware, resources, interfaces, IP
    addresses, DHCP leases and PoE over the API. Read-only. Returns a normalised
    dict (source='api') or {'ok': False, 'error': ...}."""
    if not _valid_ip(ip):
        return {"ok": False, "error": "no usable IP for the router"}
    try:
        with RosApi(ip, user, password) as api:
            ident = _one(api.talk("/system/identity/print"))
            res = _one(api.talk("/system/resource/print"))
            rb = _one(api.talk("/system/routerboard/print"))
            try:
                health = api.talk("/system/health/print")
            except ApiError:
                health = []
            ifaces = api.talk("/interface/print")
            addrs = api.talk("/ip/address/print")
            try:
                leases = api.talk("/ip/dhcp-server/lease/print")
            except ApiError:
                leases = []
            try:
                poe = api.talk("/interface/ethernet/poe/print")
            except ApiError:
                poe = []
    except (OSError, ApiError, ValueError) as e:
        return {"ok": False, "error": "RouterOS API on %s failed: %s" % (ip, e)}

    return {
        "ok": True,
        "source": "api",
        "ip": ip,
        "identity": ident.get("name", ""),
        "board": res.get("board-name") or rb.get("model", ""),
        "model": rb.get("model") or res.get("board-name", ""),
        "serial": rb.get("serial-number", ""),
        "version": res.get("version", ""),
        "firmware": rb.get("current-firmware") or rb.get("upgrade-firmware", ""),
        "factory_firmware": rb.get("factory-firmware", ""),
        "uptime": res.get("uptime", ""),
        "cpu": res.get("cpu-load", ""),
        "cpu_freq": res.get("cpu-frequency", ""),
        "cpu_count": res.get("cpu-count", ""),
        "free_memory": res.get("free-memory", ""),
        "total_memory": res.get("total-memory", ""),
        "free_hdd": res.get("free-hdd-space", ""),
        "total_hdd": res.get("total-hdd-space", ""),
        "arch": res.get("architecture-name", ""),
        "health": _norm_health(health),
        "interfaces": [_norm_iface(i) for i in ifaces],
        "addresses": [{"address": a.get("address", ""), "interface": a.get("interface", ""),
                       "disabled": a.get("disabled") == "true"} for a in addrs],
        "leases": [_norm_lease(l) for l in leases],
        "poe": [_norm_poe(p, ifaces) for p in poe],
    }


def _norm_health(rows):
    """RouterOS 6 returns health as one row of columns; RouterOS 7 as name/value
    rows. Accept both. Small boxes (hEX PoE lite) may report nothing."""
    out = {}
    if len(rows) == 1 and "name" not in rows[0]:
        for k, v in rows[0].items():
            if k not in ("nextid", ".id"):
                out[k] = v
    else:
        for r in rows:
            if r.get("name"):
                out[r["name"]] = r.get("value", "")
    return out


def _norm_iface(i):
    return {
        "name": i.get("name", ""),
        "type": i.get("type", ""),
        "running": i.get("running") == "true",
        "disabled": i.get("disabled") == "true",
        "mac": i.get("mac-address", ""),
        "comment": i.get("comment", ""),
        "rx": i.get("rx-byte", "0"),
        "tx": i.get("tx-byte", "0"),
        "mtu": i.get("mtu", ""),
        "id": i.get(".id", ""),
    }


def _norm_lease(l):
    return {
        "address": l.get("address", ""),
        "mac": l.get("mac-address", ""),
        "host": l.get("host-name", ""),
        "status": l.get("status", ""),
        "server": l.get("server", ""),
        "dynamic": l.get("dynamic") == "true",
        "expires": l.get("expires-after", ""),
        "comment": l.get("comment", ""),
    }


def _norm_poe(p, ifaces):
    name = p.get("name", "")
    if not name and p.get("interface"):
        name = p["interface"]
    return {
        "name": name,
        "poe_out": p.get("poe-out", ""),
        "power": p.get("power-measurement") or p.get("poe-out-power", ""),
        "current": p.get("current-measurement", ""),
        "voltage": p.get("voltage-measurement", ""),
        "id": p.get(".id", ""),
    }


# ---------------------------------------------------------------------------
# API write actions (gated at the route layer). Structured and reversible where
# possible; a reboot/backup/export are the only non-reversible ones.
# ---------------------------------------------------------------------------
def api_set_identity(ip, user, password, name):
    name = (name or "").strip()
    if not name or len(name) > 32 or not re.match(r"^[\w.\- ]+$", name):
        return {"ok": False, "error": "identity must be 1-32 chars (letters, digits, . _ - space)"}
    try:
        with RosApi(ip, user, password) as api:
            api.talk("/system/identity/set", "=name=" + name)
    except (OSError, ApiError, ValueError) as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "msg": "Identity set to %r." % name}


def api_set_interface(ip, user, password, iface_id, enable):
    """Enable/disable an interface by its .id (or name). enable=True|False."""
    if not iface_id:
        return {"ok": False, "error": "no interface given"}
    cmd = "/interface/enable" if enable else "/interface/disable"
    try:
        with RosApi(ip, user, password) as api:
            api.talk(cmd, "=numbers=" + iface_id)
    except (OSError, ApiError, ValueError) as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "msg": "%s %s." % (iface_id, "enabled" if enable else "disabled")}


def api_set_poe(ip, user, password, poe_id, mode):
    """Set an ethernet port's poe-out (off | auto-on | forced-on)."""
    if mode not in ("off", "auto-on", "forced-on"):
        return {"ok": False, "error": "poe mode must be off / auto-on / forced-on"}
    if not poe_id:
        return {"ok": False, "error": "no PoE port given"}
    try:
        with RosApi(ip, user, password) as api:
            api.talk("/interface/ethernet/set", "=numbers=" + poe_id, "=poe-out=" + mode)
    except (OSError, ApiError, ValueError) as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "msg": "PoE on %s set to %s." % (poe_id, mode)}


def api_reboot(ip, user, password):
    try:
        with RosApi(ip, user, password) as api:
            api.talk("/system/reboot")
    except (OSError, ApiError, ValueError) as e:
        # a reboot may drop the socket before !done — treat a clean close as ok
        if "connection closed" in str(e):
            return {"ok": True, "msg": "Router is rebooting (back in ~30-60s)."}
        return {"ok": False, "error": str(e)}
    return {"ok": True, "msg": "Router is rebooting (back in ~30-60s)."}


def api_export(ip, user, password):
    """Return the running config as text (/export). Read-only; nothing is
    written on the router. Passwords are not included by RouterOS export."""
    try:
        with RosApi(ip, user, password) as api:
            rows = api.talk("/export")
    except (OSError, ApiError, ValueError) as e:
        return {"ok": False, "error": str(e)}
    # /export streams the config in =section= words across !re rows on 6.x, or a
    # single =ret= blob. Join whatever came back.
    text = ""
    for r in rows:
        text += r.get("section", "") or r.get("ret", "")
    return {"ok": True, "config": text}


def _safe(api, *words):
    """A read that returns [] instead of raising when the menu doesn't exist on
    this model (no wireless, no switch, no PoE, …)."""
    try:
        return api.talk(*words)
    except ApiError:
        return []


def _rows_by(rows, key):
    out = {}
    for r in rows:
        k = r.get(key)
        if k:
            out[k] = r
    return out


def _b(v):
    return v == "true"


def api_report(ip, user="admin", password=""):
    """The senior-admin snapshot for the management console. One API session
    reads: system + health, every port (link status, negotiated rate, live
    rx/tx bit-rate, PoE mode/status/power/current/voltage), IP addresses, the
    connected-device tables (DHCP leases + ARP + bridge/switch host FDB, merged
    per-MAC so you see which device sits on which port), routes, DNS, firewall
    filter+NAT, wireless registrations, discovered neighbours and recent logs.
    Read-only. Sections a model lacks come back empty rather than failing."""
    if not _valid_ip(ip):
        return {"ok": False, "error": "no usable IP for the router"}
    try:
        with RosApi(ip, user, password) as api:
            res = _one(api.talk("/system/resource/print"))
            rb = _one(_safe(api, "/system/routerboard/print"))
            ident = _one(api.talk("/system/identity/print"))
            health = _safe(api, "/system/health/print")
            clock = _one(_safe(api, "/system/clock/print"))
            ifaces = api.talk("/interface/print")
            eth = _safe(api, "/interface/ethernet/print")
            eth_names = ",".join(e["name"] for e in eth if e.get("name"))
            ethmon = _safe(api, "/interface/ethernet/monitor", "=numbers=" + eth_names,
                           "=once=") if eth_names else []
            poe_cfg = _safe(api, "/interface/ethernet/poe/print")
            poe_ids = ",".join(p[".id"] for p in poe_cfg if p.get(".id"))
            poemon = _safe(api, "/interface/ethernet/poe/monitor", "=numbers=" + poe_ids,
                           "=once=") if poe_ids else []
            if_names = ",".join(i["name"] for i in ifaces if i.get("name"))
            trafmon = _safe(api, "/interface/monitor-traffic", "=interface=" + if_names,
                            "=once=") if if_names else []
            addrs = api.talk("/ip/address/print")
            leases = _safe(api, "/ip/dhcp-server/lease/print")
            arp = _safe(api, "/ip/arp/print")
            hosts = _safe(api, "/interface/bridge/host/print")
            routes = _safe(api, "/ip/route/print")
            dns = _one(_safe(api, "/ip/dns/print"))
            fw_filter = _safe(api, "/ip/firewall/filter/print")
            fw_nat = _safe(api, "/ip/firewall/nat/print")
            wl = _safe(api, "/interface/wireless/print")
            wl_reg = _safe(api, "/interface/wireless/registration-table/print")
            neigh = _safe(api, "/ip/neighbor/print")
            logs = _safe(api, "/log/print")
    except (OSError, ApiError, ValueError) as e:
        return {"ok": False, "error": "RouterOS API on %s failed: %s" % (ip, e)}

    iface_by = _rows_by(ifaces, "name")
    ethmon_by = _rows_by(ethmon, "name")
    traf_by = _rows_by(trafmon, "name")
    poemon_by = _rows_by(poemon, "name")     # poe monitor rows carry name, not .id

    ports = []
    for e in eth:
        name = e.get("name", "")
        i = iface_by.get(name, {})
        m = ethmon_by.get(name, {})
        t = traf_by.get(name, {})
        pcfg = next((p for p in poe_cfg if (p.get("name") or "") == name), None)
        poe = None
        if pcfg:
            pm = poemon_by.get(name, {})
            poe = {"id": pcfg.get(".id", ""), "mode": pcfg.get("poe-out", ""),
                   "priority": pcfg.get("poe-priority", ""),
                   "status": pm.get("poe-out-status", ""), "power": pm.get("poe-out-power", ""),
                   "current": pm.get("poe-out-current", ""), "voltage": pm.get("poe-out-voltage", "")}
        ports.append({
            "name": name, "id": e.get(".id", ""), "type": i.get("type", "ether"),
            "running": _b(i.get("running")) or m.get("status") == "link-ok",
            "disabled": _b(e.get("disabled")), "link": m.get("status", ""),
            "rate": m.get("rate", ""), "full_duplex": _b(m.get("full-duplex")),
            "auto_neg": _b(e.get("auto-negotiation")), "comment": e.get("comment") or i.get("comment", ""),
            "switch": e.get("switch", ""), "mac": e.get("orig-mac-address") or i.get("mac-address", ""),
            "rx": i.get("rx-byte", "0"), "tx": i.get("tx-byte", "0"),
            "rx_rate": t.get("rx-bits-per-second", ""), "tx_rate": t.get("tx-bits-per-second", ""),
            "poe": poe,
        })
    eth_set = {e.get("name") for e in eth}
    for i in ifaces:                       # non-ethernet ifaces (bridge, wlan, pppoe…)
        if i.get("name") in eth_set:
            continue
        t = traf_by.get(i.get("name"), {})
        ports.append({
            "name": i.get("name", ""), "id": i.get(".id", ""), "type": i.get("type", ""),
            "running": _b(i.get("running")), "disabled": _b(i.get("disabled")), "link": "",
            "rate": "", "full_duplex": False, "auto_neg": False, "comment": i.get("comment", ""),
            "switch": "", "mac": i.get("mac-address", ""), "rx": i.get("rx-byte", "0"),
            "tx": i.get("tx-byte", "0"), "rx_rate": t.get("rx-bits-per-second", ""),
            "tx_rate": t.get("tx-bits-per-second", ""), "poe": None,
        })

    leases_n = [_norm_lease(l) for l in leases]
    arp_n = [{"address": a.get("address", ""), "mac": a.get("mac-address", ""),
              "interface": a.get("interface", ""), "complete": _b(a.get("complete")),
              "dynamic": _b(a.get("dynamic"))} for a in arp]
    hosts_n = [{"mac": h.get("mac-address", ""), "interface": h.get("on-interface", ""),
                "bridge": h.get("bridge", ""), "dynamic": _b(h.get("dynamic")),
                "local": _b(h.get("local"))} for h in hosts]

    # Merge the three tables into one device-per-MAC list — the "what's on my
    # switch, and where" view.
    by_mac = {}

    def slot(mac):
        mac = (mac or "").lower()
        return by_mac.setdefault(mac, {"mac": mac, "ip": "", "hostname": "", "port": "",
                                       "iface": "", "dynamic": None, "src": []})
    for l in leases_n:
        if l["mac"]:
            s = slot(l["mac"]); s["ip"] = s["ip"] or l["address"]; s["hostname"] = s["hostname"] or l["host"]
            s["dynamic"] = l["dynamic"]; s["src"].append("dhcp")
    for a in arp_n:
        if a["mac"]:
            s = slot(a["mac"]); s["ip"] = s["ip"] or a["address"]; s["iface"] = s["iface"] or a["interface"]
            s["src"].append("arp")
    for h in hosts_n:
        if h["mac"] and not h["local"]:
            s = slot(h["mac"]); s["port"] = s["port"] or h["interface"]; s["src"].append("bridge")
    connected = sorted(by_mac.values(), key=lambda d: _ip_key(d["ip"]))

    return {
        "ok": True, "source": "api", "ip": ip,
        "system": {
            "identity": ident.get("name", ""), "board": res.get("board-name", ""),
            "model": rb.get("model", ""), "serial": rb.get("serial-number", ""),
            "version": res.get("version", ""), "firmware": rb.get("current-firmware", ""),
            "uptime": res.get("uptime", ""), "cpu": res.get("cpu-load", ""),
            "cpu_count": res.get("cpu-count", ""), "cpu_freq": res.get("cpu-frequency", ""),
            "free_memory": res.get("free-memory", ""), "total_memory": res.get("total-memory", ""),
            "free_hdd": res.get("free-hdd-space", ""), "total_hdd": res.get("total-hdd-space", ""),
            "arch": res.get("architecture-name", ""), "health": _norm_health(health),
            "time": clock.get("time", ""), "date": clock.get("date", ""),
        },
        "ports": ports,
        "addresses": [{"address": a.get("address", ""), "interface": a.get("interface", ""),
                       "disabled": _b(a.get("disabled")), "network": a.get("network", "")} for a in addrs],
        "connected": connected,
        "leases": leases_n, "arp": arp_n, "hosts": hosts_n,
        "routes": [{"dst": r.get("dst-address", ""), "gateway": r.get("gateway", ""),
                    "distance": r.get("distance", ""), "active": _b(r.get("active")),
                    "static": _b(r.get("static")), "dynamic": _b(r.get("dynamic"))} for r in routes],
        "dns": {"servers": dns.get("servers", ""), "dynamic_servers": dns.get("dynamic-servers", ""),
                "cache_used": dns.get("cache-used", "")},
        "firewall": {
            "filter": [{"chain": f.get("chain", ""), "action": f.get("action", ""),
                        "disabled": _b(f.get("disabled")), "comment": f.get("comment", ""),
                        "bytes": f.get("bytes", ""), "protocol": f.get("protocol", ""),
                        "dst_port": f.get("dst-port", ""), "src_address": f.get("src-address", ""),
                        "dst_address": f.get("dst-address", "")} for f in fw_filter],
            "nat": [{"chain": f.get("chain", ""), "action": f.get("action", ""),
                     "disabled": _b(f.get("disabled")), "comment": f.get("comment", ""),
                     "to_addresses": f.get("to-addresses", ""), "dst_port": f.get("dst-port", "")} for f in fw_nat],
        },
        "wireless": {
            "interfaces": [{"name": w.get("name", ""), "ssid": w.get("ssid", ""),
                            "band": w.get("band", ""), "frequency": w.get("frequency", ""),
                            "mode": w.get("mode", ""), "disabled": _b(w.get("disabled")),
                            "running": _b(w.get("running"))} for w in wl],
            "registrations": [{"mac": r.get("mac-address", ""), "interface": r.get("interface", ""),
                               "signal": r.get("signal-strength", ""), "tx_rate": r.get("tx-rate", ""),
                               "rx_rate": r.get("rx-rate", ""), "uptime": r.get("uptime", "")} for r in wl_reg],
        },
        "neighbors": [{"address": n.get("address", ""), "mac": n.get("mac-address", ""),
                       "identity": n.get("identity", ""), "platform": n.get("platform", ""),
                       "board": n.get("board", ""), "interface": n.get("interface", ""),
                       "version": n.get("version", "")} for n in neigh],
        "logs": [{"time": l.get("time", ""), "topics": l.get("topics", ""),
                  "message": l.get("message", "")} for l in logs[-80:]],
    }


def _ip_key(ip):
    try:
        return tuple(int(x) for x in ip.split("."))
    except (ValueError, AttributeError):
        return (999, 999, 999, 999)


def api_poe_cycle(ip, user, password, port, duration=5):
    """Power-cycle PoE on one ethernet port — reboots the powered device (camera,
    AP, phone) on it. Uses RouterOS's own power-cycle where available, else
    off → wait → auto-on."""
    port = (port or "").strip()
    if not port:
        return {"ok": False, "error": "no port given"}
    try:
        dur = max(1, min(int(duration), 30))
    except (TypeError, ValueError):
        dur = 5
    try:
        with RosApi(ip, user, password) as api:
            try:
                api.talk("/interface/ethernet/poe/power-cycle", "=numbers=" + port,
                         "=duration=" + str(dur) + "s")
                return {"ok": True, "msg": "Power-cycled PoE on %s (%ss) — the device on it reboots." % (port, dur)}
            except ApiError:
                pass
            api.talk("/interface/ethernet/set", "=numbers=" + port, "=poe-out=off")
            time.sleep(min(dur, 8))
            api.talk("/interface/ethernet/set", "=numbers=" + port, "=poe-out=auto-on")
            return {"ok": True, "msg": "Cycled PoE on %s (off %ss, back on)." % (port, dur)}
    except (OSError, ApiError, ValueError) as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# MAC-Telnet — WinBox-style login by MAC (works with no reachable IP)
# ---------------------------------------------------------------------------
# We drive the compiled mac-telnet binary under a pseudo-terminal. The binary
# handles the mtwei EC-SRP login; we handle the RouterOS console: wait for the
# prompt, feed commands, auto-answer the pager, and stop at the prompt again.
MACTELNET_BIN = os.environ.get("MACTELNET_BIN", "mactelnet")

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[=>NO]|\x1b\][^\x07]*\x07")
_PROMPT = re.compile(r"\[[^\]@]*@[^\]]*\][^>\n]*>\s*$")   # [admin@Identity] > or [..] /iface>
_PAGER = re.compile(r"\[Q quit\b|-- \[?Q ", re.I)
_LOGIN_FAIL = re.compile(r"login failed|incorrect (?:username|password)|bad username", re.I)


def mactelnet_available():
    from shutil import which
    return which(MACTELNET_BIN) is not None


def _clean(text):
    text = _ANSI.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\n{3,}", "\n\n", text)     # RouterOS dumb mode double-spaces


def _set_winsize(fd, rows=1000, cols=200):
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


def mt_run(mac, user, password, commands, timeout=30, connect_timeout=12):
    """Open a MAC-Telnet session to `mac` and run each console command, returning
    the combined output. `commands` is a list of RouterOS console lines (a final
    '/quit' is appended automatically). Returns:
        {ok, source:'mactelnet', mac, output, per: [{cmd, out}], error?}

    A very tall pty (1000 rows) keeps short output off the pager; the pager is
    auto-answered with space for anything long. Login failure and a missing
    binary are reported as errors, not hangs."""
    mac = normalize_mac(mac)
    if not mac:
        return {"ok": False, "error": "no MAC address for this device"}
    if not mactelnet_available():
        return {"ok": False, "error": "mac-telnet is not installed in this image"}
    cmds = list(commands) + ["/quit"]

    pid, fd = pty.fork()
    if pid == 0:                                   # child
        # The '+cte' suffix is parsed by RouterOS before auth (the user stays
        # `user`): c=no colours, t=no terminal auto-detect, e=dumb terminal —
        # which gives ANSI-free, UNPAGED output, the key to clean parsing.
        # -A ignores any ~/.mactelnet autologin file. TERM=dumb reinforces it.
        env = dict(os.environ, TERM="dumb", LANG="C")
        argv = [MACTELNET_BIN, mac, "-u", (user or "admin") + "+cte",
                "-p", password or "", "-A"]
        try:
            os.execvpe(MACTELNET_BIN, argv, env)
        except OSError:
            os._exit(127)
    _set_winsize(fd)

    buf = ""
    per = []
    sent = 0
    logged_in = False
    cur_cmd = None
    cur_start = 0
    start = time.time()
    last = time.time()
    deadline = start + timeout
    err = None

    def alive():
        try:
            return os.waitpid(pid, os.WNOHANG)[0] == 0
        except ChildProcessError:
            return False

    try:
        while time.time() < deadline:
            r, _, _ = select.select([fd], [], [], 0.2)
            if fd in r:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk.decode("utf-8", "replace")
                last = time.time()
                tail = _clean(buf)[-400:]
                if _LOGIN_FAIL.search(tail):
                    err = "login failed — check the router's username/password"
                    break
                if _PAGER.search(tail):            # answer the pager
                    os.write(fd, b" ")
                    last = time.time()
                continue

            idle = time.time() - last
            tail = _clean(buf)[-200:]
            ready = bool(_PROMPT.search(tail))
            if not logged_in:
                if ready:
                    logged_in = True
                elif not alive() or (time.time() - start) > connect_timeout:
                    err = err or "could not reach the router by MAC (no console)"
                    break
                else:
                    continue
            # logged in: finish the previous command's capture, then send next
            if cur_cmd is not None and ready and idle >= 0.35:
                lines = _clean(buf[cur_start:]).split("\n")
                cc = cur_cmd.strip()
                # drop leading blank/echo lines and any trailing prompt/blank lines
                while lines and (not lines[0].strip() or (cc and cc in lines[0]) or "] >" in lines[0]):
                    lines.pop(0)
                while lines and (not lines[-1].strip() or "] >" in lines[-1]):
                    lines.pop()
                per.append({"cmd": cur_cmd, "out": "\n".join(lines)})
                cur_cmd = None
            if cur_cmd is None and ready and idle >= 0.3:
                if sent < len(cmds):
                    cur_cmd = cmds[sent]
                    sent += 1
                    cur_start = len(buf)
                    os.write(fd, (cur_cmd + "\r").encode())
                    last = time.time()
                    if cur_cmd == "/quit":
                        cur_cmd = None
                        time.sleep(0.2)
                        break
                else:
                    break
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, ChildProcessError):
            pass
        try:
            os.waitpid(pid, 0)
        except (ChildProcessError, OSError):
            pass

    output = _clean(buf)
    if err and not per:
        return {"ok": False, "source": "mactelnet", "mac": mac, "error": err,
                "output": output}
    return {"ok": True, "source": "mactelnet", "mac": mac, "output": output, "per": per}


# --- structured status over MAC-Telnet (paging-proof :put scripting) ---------
_MK = "__NW__"


def _mt_script_lines():
    """RouterOS console lines that print one marked, delimited record per value,
    so the output parses without any pager or column alignment."""
    L = [
        ':put ("%s|identity|" . [/system identity get name])' % _MK,
        ':put ("%s|version|" . [/system resource get version])' % _MK,
        ':put ("%s|board|" . [/system resource get board-name])' % _MK,
        ':put ("%s|uptime|" . [/system resource get uptime])' % _MK,
        ':put ("%s|cpu|" . [/system resource get cpu-load])' % _MK,
        ':put ("%s|free_memory|" . [/system resource get free-memory])' % _MK,
        ':put ("%s|total_memory|" . [/system resource get total-memory])' % _MK,
        ':put ("%s|arch|" . [/system resource get architecture-name])' % _MK,
        # routerboard fields can be absent on x86/CHR — guard with :do on-error
        ':do {:put ("%s|model|" . [/system routerboard get model])} on-error={}' % _MK,
        ':do {:put ("%s|serial|" . [/system routerboard get serial-number])} on-error={}' % _MK,
        ':do {:put ("%s|firmware|" . [/system routerboard get current-firmware])} on-error={}' % _MK,
        ':foreach i in=[/interface find] do={:put ("%s|iface|" . [/interface get $i name] '
        '. "~" . [/interface get $i type] . "~" . [/interface get $i running] '
        '. "~" . [/interface get $i disabled] . "~" . [/interface get $i rx-byte] '
        '. "~" . [/interface get $i tx-byte])}' % _MK,
        ':foreach a in=[/ip address find] do={:put ("%s|addr|" . [/ip address get $a address] '
        '. "~" . [/ip address get $a interface])}' % _MK,
    ]
    return L


def mt_status(mac, user="admin", password=""):
    """Read a RouterOS device's status BY MAC (no IP). Same shape as api_status,
    source='mactelnet'. Skips PoE/leases/health (the API path carries those)."""
    res = mt_run(mac, user, password, _mt_script_lines(), timeout=25)
    if not res.get("ok"):
        return {"ok": False, "source": "mactelnet", "error": res.get("error", "failed"),
                "output": res.get("output", "")}
    fields, ifaces, addrs = {}, [], []
    for line in res["output"].split("\n"):
        line = line.strip()
        if not line.startswith(_MK + "|"):
            continue
        parts = line.split("|", 2)          # __NW__ | key | value
        if len(parts) != 3:
            continue
        key, val = parts[1], parts[2]
        if key == "iface":
            f = val.split("~")
            if len(f) >= 6:
                ifaces.append({"name": f[0], "type": f[1], "running": f[2] == "true",
                               "disabled": f[3] == "true", "rx": f[4], "tx": f[5],
                               "mac": "", "comment": "", "mtu": "", "id": f[0]})
        elif key == "addr":
            f = val.split("~")
            if f:
                addrs.append({"address": f[0], "interface": f[1] if len(f) > 1 else "",
                              "disabled": False})
        else:
            fields[key] = val
    # A bare login with no parsed fields (slow box past the timeout, or :put
    # scripting restricted) must NOT read as success, or snapshot() would report
    # a blank panel instead of falling back to the API.
    if not (fields.get("identity") or fields.get("version") or ifaces):
        return {"ok": False, "source": "mactelnet",
                "error": "logged in by MAC but read no status (the console script returned nothing)",
                "output": res.get("output", "")}
    return {
        "ok": True, "source": "mactelnet", "mac": mac,
        "identity": fields.get("identity", ""),
        "board": fields.get("board", ""),
        "model": fields.get("model") or fields.get("board", ""),
        "serial": fields.get("serial", ""),
        "version": fields.get("version", ""),
        "firmware": fields.get("firmware", ""),
        "uptime": fields.get("uptime", ""),
        "cpu": fields.get("cpu", ""),
        "free_memory": fields.get("free_memory", ""),
        "total_memory": fields.get("total_memory", ""),
        "arch": fields.get("arch", ""),
        "health": {}, "interfaces": ifaces, "addresses": addrs,
        "leases": [], "poe": [],
    }


# ---------------------------------------------------------------------------
# SSH console fallback (IP, last resort) — RouterOS runs a passed command
# ---------------------------------------------------------------------------
_SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
    "-o", "ConnectTimeout=8", "-o", "NumberOfPasswordPrompts=1",
    "-o", "HostKeyAlgorithms=+ssh-rsa", "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa",
    "-o", "KexAlgorithms=+diffie-hellman-group1-sha1,diffie-hellman-group14-sha1",
]


def ssh_run(ip, user, password, command, timeout=20):
    """Run one RouterOS console command over SSH (password auth via sshpass).
    RouterOS executes a command passed on the ssh command line and returns its
    output. Last-resort transport when MAC-Telnet can't be used."""
    if not _valid_ip(ip):
        return {"ok": False, "error": "no usable IP"}
    # `--` ends option parsing so a saved username starting with '-' can't be read
    # by ssh as an option (e.g. -oProxyCommand=) and run code on the Pi.
    cmd = ["sshpass", "-p", password or "", "ssh", *_SSH_OPTS,
           "--", "%s@%s" % (user or "admin", ip), command]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "SSH timed out"}
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "error": str(e)}
    if r.returncode != 0 and not r.stdout:
        hint = (r.stderr or "").strip().splitlines()
        return {"ok": False, "error": "SSH failed: " + (hint[-1] if hint else "rc %d" % r.returncode)}
    return {"ok": True, "source": "ssh", "output": _clean(r.stdout)}


# ---------------------------------------------------------------------------
# Unified entry points — honour "MAC first, IP last"
# ---------------------------------------------------------------------------
def snapshot(mac="", ip="", user="admin", password="", prefer="mac"):
    """Structured router status. prefer='mac' tries MAC-Telnet first (works with
    no IP) and falls back to the API on the discovered IP; prefer='ip' does the
    reverse. Returns the normalised status dict with 'source' saying which won,
    plus 'tried' listing attempts."""
    tried = []

    def by_mac():
        if not (mac and mactelnet_available()):
            return None
        tried.append("mactelnet")
        r = mt_status(mac, user, password)
        return r if r.get("ok") else None

    def by_ip():
        if not _valid_ip(ip):
            return None
        tried.append("api")
        r = api_status(ip, user, password)
        return r if r.get("ok") else None

    order = (by_mac, by_ip) if prefer == "mac" else (by_ip, by_mac)
    for fn in order:
        r = fn()
        if r:
            # MAC-Telnet gives identity/resource/interfaces but not PoE/leases/
            # health — pull those from the API when we also have a reachable IP,
            # so a MAC-first read is still a complete panel.
            if r.get("source") == "mactelnet" and _valid_ip(ip):
                _backfill_from_api(r, ip, user, password)
            r["tried"] = tried
            return r
    return {"ok": False, "error": "could not reach the router by MAC or IP",
            "tried": tried}


def _backfill_from_api(r, ip, user, password):
    """Fill the fields MAC-Telnet can't cheaply read (PoE, DHCP leases, health,
    and per-interface mac/comment/mtu) from one extra API read. Best-effort."""
    if r.get("poe") and r.get("health") and r.get("leases"):
        return
    a = api_status(ip, user, password)
    if not a.get("ok"):
        return
    for k in ("poe", "leases", "health"):
        if not r.get(k):
            r[k] = a.get(k, r.get(k))
    if a.get("interfaces"):
        by_name = {i["name"]: i for i in a["interfaces"]}
        for i in r.get("interfaces", []):
            ai = by_name.get(i["name"])
            if ai:
                for k in ("mac", "comment", "mtu"):
                    if not i.get(k):
                        i[k] = ai.get(k, "")
    r["backfilled"] = True


# Commands that can strand a client's router (lose the management path, wipe the
# config, take it offline). We WARN and require an explicit confirm rather than
# hard-block — an operator may genuinely need them — but never let one through
# on a stray Enter. Matched on the cleaned command text.
# RouterOS accepts a menu path written with spaces ("/system reboot") OR slashes
# ("/system/reboot" — the usual one-liner form), so every separator below is
# [\s/]+, matching both. Otherwise the slash form would slip straight past.
_DANGER = [
    (r"reset-configuration|/system[\s/]+reset\b", "resets the router to defaults — wipes the whole config"),
    (r"/system[\s/]+(shutdown|routerboard[\s/]+(upgrade|downgrade))", "powers off or reflashes the router"),
    (r"/interface\b.*\b(disable|remove)\b", "disables or removes an interface — you may lose access"),
    (r"/ip[\s/]+address\b.*\bremove\b", "removes an IP address — could drop the management IP"),
    (r"/ip[\s/]+(service|firewall)\b.*\b(disable|remove|add\b.*\bdrop)", "changes services/firewall — could lock you out"),
    (r"/user\b.*\b(remove|set|add|disable)\b", "changes router logins — could lock you out"),
    (r"/system[\s/]+(reboot|package)\b", "reboots the router / changes packages"),
    (r"/file\b.*\bremove\b", "deletes files on the router"),
]


def dangerous_command(command):
    """Return a human warning if a console command looks like it could strand the
    router, else ''. Used to require an explicit confirm in the terminal. Matches
    both the space and slash forms of a RouterOS menu path."""
    c = re.sub(r"\s+", " ", (command or "").strip().lower())
    for pat, why in _DANGER:
        if re.search(pat, c):
            return "This command " + why + "."
    return ""


def run_console(mac="", ip="", user="admin", password="", command="", prefer="mac"):
    """Run one free-form console command. prefer='mac' uses MAC-Telnet first,
    SSH-over-IP as the fallback."""
    command = (command or "").strip()
    if not command:
        return {"ok": False, "error": "no command"}
    tried = []

    def by_mac():
        if not (mac and mactelnet_available()):
            return None
        tried.append("mactelnet")
        r = mt_run(mac, user, password, [command], timeout=30)
        if not r.get("ok"):
            return None
        out = r["per"][0]["out"] if r.get("per") else r.get("output", "")
        return {"ok": True, "source": "mactelnet", "output": out}

    def by_ip():
        if not _valid_ip(ip):
            return None
        tried.append("ssh")
        r = ssh_run(ip, user, password, command)
        return r if r.get("ok") else None

    order = (by_mac, by_ip) if prefer == "mac" else (by_ip, by_mac)
    for fn in order:
        r = fn()
        if r:
            r["tried"] = tried
            return r
    return {"ok": False, "error": "command did not run (MAC-Telnet and SSH both failed)",
            "tried": tried}
