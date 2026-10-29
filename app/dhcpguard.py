"""Rogue DHCP guard: notice a second DHCP server on the farm network.

A router plugged in the wrong way round (its LAN side into the farm network), or
a Wi-Fi unit that fell back to factory settings, starts handing out addresses
from its own network. Every device that renews then drops off the farm LAN —
cameras, radios, and the Netwatch Pi itself — and the monitoring goes dark with
no clue why (Bennie, 25-27 Oct 2026: 10.1.1.x and 192.168.10.x leases).

Every few minutes this sends ONE DHCPDISCOVER from a made-up, stable MAC and
listens for offers. DISCOVER never takes a lease (that needs a REQUEST), so the
check costs the farm nothing. Each server that answers is judged against the
site's own router (the Pi's default gateway) and the operator's allowed list:

  crit  offers an address outside every scan target network (a foreign network)
  warn  offers a farm address but is not the router and not allowed

A server stays a problem until it has missed GONE_AFTER checks in a row, so one
dropped offer on a busy radio link does not flap the alert. The server's MAC,
maker and switch port (from the switch monitor's MAC table) say where to look.

Pushes go out from the site only when it is NOT joined to a hub — a joined site
lets the hub page once, instead of twice.

State lives in /data/dhcpguard_state.json.
"""
import hashlib
import ipaddress
import json
import os
import random
import socket
import struct
import threading
import time

import history
import hubvpn
import identify
import notify

DATA_DIR = os.environ.get("NETWATCH_DATA", "/data")
STATE_PATH = os.path.join(DATA_DIR, "dhcpguard_state.json")

ETH_P_IP = 0x0800
SOL_PACKET, PACKET_ADD_MEMBERSHIP, PACKET_MR_PROMISC = 263, 1, 1
MAGIC = b"\x63\x82\x53\x63"
LISTEN_S = 8                # offers from a server behind a radio link can be slow
RESEND_S = 3                # one repeat DISCOVER, in case the first was lost
GONE_AFTER = 2              # checks in a row without an answer before "gone"
REALERT_S = 6 * 3600
FIRST_CHECK_S = 60          # after start: let the scanner and VPN settle first
TYPES = {1: "DISCOVER", 2: "OFFER", 3: "REQUEST", 4: "DECLINE", 5: "ACK", 6: "NAK", 7: "RELEASE", 8: "INFORM"}


# ---- packets ------------------------------------------------------------------
def _csum(b):
    if len(b) % 2:
        b += b"\0"
    s = sum(struct.unpack(f"!{len(b) // 2}H", b))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return ~s & 0xFFFF


def probe_mac(own_mac):
    """A stable, locally administered unicast MAC for the DISCOVER. Stable so a
    server re-offers the same address instead of parking a new one per check."""
    h = hashlib.sha256(("netwatch-dhcp-guard|" + (own_mac or "")).encode()).digest()
    return bytes([(h[0] & 0xFC) | 0x02]) + h[1:6]


def build_discover(mac, xid):
    """Ethernet frame: broadcast DHCPDISCOVER from `mac` (6 bytes), broadcast flag set."""
    opts = (b"\x35\x01\x01"                                 # 53: DISCOVER
            + b"\x3d\x07\x01" + mac                         # 61: client id
            + b"\x37\x06\x01\x03\x06\x0f\x33\x36"           # 55: mask, router, dns, domain, lease, server id
            + b"\x0c\x12" + b"netwatch-dhcpguard"           # 12: host name — makes the probe obvious in a log
            + b"\xff")
    bootp = struct.pack("!BBBBIHH4s4s4s4s16s64s128s", 1, 1, 6, 0, xid, 0, 0x8000,
                        b"\0" * 4, b"\0" * 4, b"\0" * 4, b"\0" * 4, mac + b"\0" * 10,
                        b"\0" * 64, b"\0" * 128) + MAGIC + opts
    bootp += b"\0" * max(0, 300 - len(bootp))
    udp = struct.pack("!HHHH", 68, 67, 8 + len(bootp), 0) + bootp
    iph = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), random.randint(0, 0xFFFF), 0,
                      64, 17, 0, b"\0\0\0\0", b"\xff\xff\xff\xff")
    iph = iph[:10] + struct.pack("!H", _csum(iph)) + iph[12:]
    return b"\xff" * 6 + mac + struct.pack("!H", ETH_P_IP) + iph + udp


def _options(b):
    o, i = {}, 0
    while i < len(b):
        t = b[i]
        if t == 255:
            break
        if t == 0:
            i += 1
            continue
        if i + 1 >= len(b):
            break
        n = b[i + 1]
        o[t] = b[i + 2:i + 2 + n]
        i += 2 + n
    return o


def _ips(b):
    return [socket.inet_ntoa(b[i:i + 4]) for i in range(0, len(b) - 3, 4)]


def parse_reply(frame, xid=None):
    """A server's DHCP reply in an Ethernet frame → dict, else None."""
    if len(frame) < 14 + 20 + 8 + 240 or struct.unpack("!H", frame[12:14])[0] != ETH_P_IP:
        return None
    ihl = (frame[14] & 15) * 4
    if frame[23] != 17:
        return None
    u = 14 + ihl
    sport, dport = struct.unpack("!HH", frame[u:u + 4])
    if sport != 67 or dport != 68:
        return None
    b = frame[u + 8:]
    if len(b) < 240 or b[0] != 2 or b[236:240] != MAGIC:
        return None
    if xid is not None and struct.unpack("!I", b[4:8])[0] != xid:
        return None
    o = _options(b[240:])
    mt = o.get(53, b"\0")[0]
    return {
        "type": TYPES.get(mt, str(mt)),
        "mac": ":".join(f"{x:02x}" for x in frame[6:12]),
        "ip": socket.inet_ntoa(frame[26:30]),
        "server_id": socket.inet_ntoa(o[54][:4]) if len(o.get(54, b"")) >= 4 else "",
        "offered": socket.inet_ntoa(b[16:20]),
        "mask": socket.inet_ntoa(o[1][:4]) if len(o.get(1, b"")) >= 4 else "",
        "router": _ips(o.get(3, b"")),
        "dns": _ips(o.get(6, b"")),
        "lease_s": struct.unpack("!I", o[51][:4])[0] if len(o.get(51, b"")) >= 4 else None,
    }


# ---- the Pi's own network -------------------------------------------------------
def default_route():
    """(iface, gateway ip) of the IPv4 default route, else (None, None)."""
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                p = line.split()
                if len(p) > 3 and p[1] == "00000000" and int(p[3], 16) & 2:
                    return p[0], socket.inet_ntoa(struct.pack("<I", int(p[2], 16)))
    except (OSError, ValueError):
        pass
    return None, None


def iface_mac(iface):
    try:
        with open(f"/sys/class/net/{iface}/address") as f:
            return f.read().strip().lower()
    except OSError:
        return ""


def probe(iface, mac, listen_s=LISTEN_S):
    """Send a DISCOVER on `iface` and return every distinct server that answered."""
    xid = random.getrandbits(32)
    frame = build_discover(mac, xid)
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_IP))
    try:
        s.bind((iface, ETH_P_IP))
        # The offer may come back unicast to the made-up MAC; without promiscuous
        # mode the NIC would drop it. Per-socket — ends when the socket closes.
        s.setsockopt(SOL_PACKET, PACKET_ADD_MEMBERSHIP,
                     struct.pack("iHH8s", socket.if_nametoindex(iface), PACKET_MR_PROMISC, 0, b""))
        s.settimeout(0.5)
        seen, sent2 = {}, False
        start = time.time()
        s.send(frame)
        while time.time() - start < listen_s:
            if not sent2 and time.time() - start >= RESEND_S:
                s.send(frame)
                sent2 = True
            try:
                pkt = s.recv(2048)
            except socket.timeout:
                continue
            r = parse_reply(pkt, xid)
            if r:
                seen.setdefault((r["mac"], r["server_id"] or r["ip"]), r)
        return list(seen.values())
    finally:
        s.close()


# ---- judging ------------------------------------------------------------------
def _norm_mac(m):
    h = "".join(c for c in (m or "").lower() if c in "0123456789abcdef")
    return ":".join(h[i:i + 2] for i in range(0, 12, 2)) if len(h) == 12 else ""


def _networks(targets):
    out = []
    for t in targets or []:
        try:
            out.append(ipaddress.ip_network(t.get("cidr") if isinstance(t, dict) else t, strict=False))
        except (ValueError, TypeError):
            pass
    return out


def _in(ip, nets):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(a in n for n in nets)


def judge(server, gateway, targets, allowed):
    """'ok' | 'warn' | 'crit' for one answering server."""
    allowed = {x.strip().lower() for x in allowed or [] if x and x.strip()}
    mac = _norm_mac(server.get("mac"))
    ids = {server.get("server_id"), server.get("ip")} - {"", "0.0.0.0", None}
    if (gateway and gateway in ids) or mac in {_norm_mac(a) for a in allowed} or ids & allowed:
        return "ok"
    nets = _networks(targets)
    offered = server.get("offered") or ""
    if nets and offered and offered != "0.0.0.0" and not _in(offered, nets):
        return "crit"
    return "warn"


def network_of(server):
    try:
        if server.get("offered") and server.get("mask"):
            return str(ipaddress.ip_network(f"{server['offered']}/{server['mask']}", strict=False))
    except ValueError:
        pass
    return ""


# ---- the monitor ----------------------------------------------------------------
class DhcpGuard:
    def __init__(self, probe_fn=None):
        self._lock = threading.Lock()
        self._busy = False
        self._started = False
        self._probe = probe_fn or probe
        self._get_cfg = self._get_devices = self._mac_map = None
        self._last = {}            # last check: {ts, iface, gateway, servers:[…], error}
        try:
            with open(STATE_PATH) as f:
                self._state = json.load(f)
        except (OSError, ValueError):
            self._state = {}
        self._state.setdefault("servers", {})    # mac -> tracked suspect

    # ---- running ------------------------------------------------------------
    def start(self, get_cfg, get_devices=None, mac_map=None):
        self._get_cfg, self._get_devices, self._mac_map = get_cfg, get_devices, mac_map
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        time.sleep(FIRST_CHECK_S)
        while True:
            gap = 600
            try:
                cfg = self._get_cfg()
                gap = max(2, int((cfg.get("dhcp_guard") or {}).get("interval_min", 10))) * 60
                self.check(cfg)
            except Exception as e:  # noqa: BLE001 - the guard must never die
                print(f"[dhcpguard] check failed: {e}", flush=True)
            time.sleep(gap)

    def run_now(self):
        """Start a check in the background (the API's "Check now")."""
        if self._busy or not self._get_cfg:
            return False
        threading.Thread(target=lambda: self.check(self._get_cfg()), daemon=True).start()
        return True

    def check(self, cfg, now=None):
        g = cfg.get("dhcp_guard") or {}
        if not g.get("enabled", True):
            self._last = {"ts": int(time.time()), "disabled": True}
            return []
        with self._lock:
            if self._busy:
                return self.problems()
            self._busy = True
        try:
            now = now or time.time()
            iface, gw = default_route()
            iface = (g.get("iface") or "").strip() or iface
            if not iface:
                self._last = {"ts": int(now), "error": "no default route — the Pi has no network"}
                return self.problems()
            try:
                servers = self._probe(iface, probe_mac(iface_mac(iface)))
            except Exception as e:  # noqa: BLE001 - raw sockets can fail (caps, iface gone)
                self._last = {"ts": int(now), "iface": iface, "gateway": gw, "error": str(e)}
                print(f"[dhcpguard] probe failed on {iface}: {e}", flush=True)
                return self.problems()
            for s in servers:
                s["verdict"] = judge(s, gw, cfg.get("targets"), g.get("allowed"))
            self._last = {"ts": int(now), "iface": iface, "gateway": gw, "servers": servers}
            self._track(cfg, servers, now)
            return self.problems()
        finally:
            with self._lock:
                self._busy = False

    def _track(self, cfg, servers, now):
        tracked = self._state["servers"]
        alerts_on = (cfg.get("dhcp_guard") or {}).get("alerts", True)
        answered = set()
        for s in servers:
            if s["verdict"] == "ok":
                continue
            mac = s["mac"]
            answered.add(mac)
            t = tracked.get(mac)
            fresh = not t or t.get("gone_ts")
            if fresh:
                t = tracked[mac] = {"first_seen": int(now), "alerted_ts": 0, "level": "ok"}
            escalated = t.get("level") == "warn" and s["verdict"] == "crit"
            t.update({"last_seen": int(now), "misses": 0, "gone_ts": None, "offer": {
                k: s.get(k) for k in ("ip", "server_id", "offered", "mask", "router", "dns", "lease_s")}})
            t["vendor"] = t.get("vendor") or self._vendor(cfg, mac)
            t["where"] = self._where(mac) or t.get("where")
            if fresh or escalated or now - t.get("alerted_ts", 0) >= REALERT_S:
                t["level"] = s["verdict"]
                self._event("rogue_dhcp", mac, t)
                if alerts_on:
                    self._push(cfg, f"Rogue DHCP server: {self._site(cfg)}", self._text(mac, t), "high")
                t["alerted_ts"] = int(now)
            t["level"] = s["verdict"]
        for mac, t in tracked.items():
            if mac in answered or t.get("gone_ts"):
                continue
            t["misses"] = t.get("misses", 0) + 1
            if t["misses"] >= GONE_AFTER:
                t["gone_ts"] = int(now)
                self._event("rogue_dhcp_gone", mac, t)
                if alerts_on:
                    self._push(cfg, f"Rogue DHCP server gone: {self._site(cfg)}",
                               f"{mac} has stopped answering DHCP (last offer {t.get('offer', {}).get('offered', '?')}).",
                               "low")
        # forget long-gone servers after 30 days
        for mac in [m for m, t in tracked.items() if t.get("gone_ts") and now - t["gone_ts"] > 30 * 86400]:
            tracked.pop(mac)
        self._save()

    # ---- helpers --------------------------------------------------------------
    def _vendor(self, cfg, mac):
        try:
            return identify.vendor_for_mac(mac, online_ok=(cfg.get("scan") or {}).get("online_lookup", True))
        except Exception:  # noqa: BLE001 - a maker name is cosmetic
            return ""

    def _where(self, mac):
        """Switch + port the server's MAC was learned on, if a switch monitor knows."""
        if not self._mac_map:
            return None
        try:
            m = self._mac_map().get(mac) or self._mac_map().get(mac.replace(":", ""))
        except Exception:  # noqa: BLE001
            m = None
        if not m:
            return None
        name = m.get("switch_key")
        for d in (self._get_devices() if self._get_devices else []):
            if d.get("key") == m.get("switch_key"):
                name = d.get("name") or d.get("device_name") or d.get("ip") or name
        return {"switch": name, "port": m.get("port"), "port_name": m.get("port_name"),
                "uplink": m.get("uplink", False)}

    @staticmethod
    def _site(cfg):
        return (cfg.get("site") or {}).get("name") or "site"

    @staticmethod
    def where_text(w):
        if not w:
            return ""
        port = f"port {w.get('port')}" + (f" \"{w['port_name']}\"" if w.get("port_name") else "")
        return f" — seen on {w.get('switch')} {port}" + (" (an uplink: it is further along that link)" if w.get("uplink") else "")

    def _text(self, mac, t):
        o = t.get("offer") or {}
        net = network_of(o)
        who = f"{mac}" + (f" ({t['vendor']})" if t.get("vendor") else "")
        if t.get("level") == "crit" or t.get("level") is None:
            head = f"A second DHCP server is handing out addresses from another network ({o.get('offered')}{', ' + net if net else ''})."
        else:
            head = f"A second DHCP server answers on the farm network (offers {o.get('offered')})."
        return f"{head} Server {who}, IP {o.get('server_id') or o.get('ip')}{self.where_text(t.get('where'))}."

    def _push(self, cfg, title, body, prio):
        if hubvpn.has_config():
            return              # the hub pages for joined sites — one alert, not two
        notify.push(cfg.get("alerts") or {}, title, body, priority=prio,
                    tags=["rotating_light" if prio == "high" else "white_check_mark"])

    def _event(self, etype, mac, t):
        o = t.get("offer") or {}
        dev = {"key": mac, "ip": o.get("server_id") or o.get("ip"), "mac": mac,
               "name": "DHCP server", "category": "dhcp", "vendor": t.get("vendor") or ""}
        try:
            history.log_events([history.build_event(etype, dev, {
                "offered": o.get("offered"), "network": network_of(o), "router": o.get("router"),
                "level": t.get("level"), "where": t.get("where")})])
        except Exception as e:  # noqa: BLE001 - the event log is a nice-to-have
            print(f"[dhcpguard] event log failed: {e}", flush=True)

    def _save(self):
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._state, f)
            os.replace(tmp, STATE_PATH)
        except OSError:
            pass

    def allow(self, cfg, mac):
        """Stop flagging a server that is meant to be there; returns the new cfg."""
        m = _norm_mac(mac)
        if not m:
            return None
        g = cfg.setdefault("dhcp_guard", {})
        allowed = [a for a in g.get("allowed") or [] if _norm_mac(a) != m] + [m]
        g["allowed"] = allowed
        self._state["servers"].pop(m, None)
        self._save()
        return cfg

    # ---- api ----------------------------------------------------------------------
    def problems(self):
        """Active suspects in the Problems-panel shape."""
        out = []
        for mac, t in self._state["servers"].items():
            if t.get("gone_ts"):
                continue
            o = t.get("offer") or {}
            crit = t.get("level") == "crit"
            out.append({
                "type": "rogue_dhcp",
                "severity": "high" if crit else "medium",
                "ip": o.get("server_id") or o.get("ip"),
                "mac": mac,
                "detail": (f"Hands out {o.get('offered')} ({network_of(o) or 'another network'}) — not the farm network"
                           if crit else f"A second DHCP server on the farm network (offers {o.get('offered')})")
                          + self.where_text(t.get("where")),
                "fix": ("Find this unit and switch its DHCP server off, or set it to AP/bridge mode. Usually a router "
                        "plugged in the wrong way round (its LAN side into the farm network) or a Wi-Fi unit reset to "
                        "factory settings. If it is meant to be there, mark it allowed."),
                "first_seen": t.get("first_seen"), "last_seen": t.get("last_seen"),
                "devices": [{"key": mac, "ip": o.get("server_id") or o.get("ip"), "mac": mac,
                             "name": "DHCP server", "vendor": t.get("vendor") or "",
                             "category": "", "online": True}],
            })
        return out

    def summary(self):
        """Small block for /api/status, which the hub polls."""
        last = self._last
        return {
            "checked_ts": last.get("ts"), "error": last.get("error"), "disabled": bool(last.get("disabled")),
            "servers": len(last.get("servers") or []),
            "rogues": [{"mac": p["mac"], "ip": p["ip"], "level": "crit" if p["severity"] == "high" else "warn",
                        "detail": p["detail"], "first_seen": p["first_seen"], "last_seen": p["last_seen"],
                        "vendor": p["devices"][0]["vendor"]} for p in self.problems()],
        }

    def snapshot(self):
        return {"last": self._last, "tracked": self._state["servers"], "busy": self._busy}


monitor = DhcpGuard()
