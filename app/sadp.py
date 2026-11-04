"""Hikvision discovery (SADP, UDP 37020) — the inquiry Hikvision's own "SADP"
tool sends. Every Hikvision device on the LAN — cameras, recorders, access and
time-attendance terminals, intercom stations, PoE switches — answers it with its
model, serial number, firmware, real MAC and network settings. No login is
needed and nothing on a device changes: this module only ever sends the
inquiry, never the SADP commands that activate a device, change its address or
reset its password.

One round: bind UDP 37020 (devices send their answers to the multicast group on
that port, so a socket on any other port only sees the few a device sends
straight back), join 239.255.255.250 on each of the Pi's LAN addresses, send the
inquiry out of each, and collect <ProbeMatch> answers for a few seconds. The
port is shared (SO_REUSEADDR); when something else holds it alone, the round
still runs on a spare port and says so in its note.

An answer names the address it claims (IPv4Address). It only counts when it
also came FROM that address — a device that says one thing and speaks from
another can't be matched to anything. Two devices claiming one address are a
clash: neither counts.

Site data is hostile: an answer bigger than MAX_DGRAM is dropped, one with a
DOCTYPE or ENTITY is refused (hikisapi.parse — the same rule as ISAPI), every
field is checked and bounded, and a round reads at most MAX_PACKETS datagrams
and keeps at most MAX_DEVICES answers.

Runs in the netwatch container's host-network namespace, so the multicast
reaches the real LAN. Read-only look from a site Pi:
  docker exec netwatch python /app/sadp.py 192.168.0.0/24
"""
import ipaddress
import json
import re
import socket
import struct
import subprocess
import sys
import time
import uuid

import hikisapi
import identify

GROUP = "239.255.255.250"
PORT = 37020
WAIT_S = 4.0                 # how long a round listens
RESEND_S = 1.5               # the inquiry goes out a second time after this (lost packets)
MAX_DGRAM = 16 << 10         # real answers are 1.5-2.5 KB
MAX_PACKETS = 4096           # datagrams read in one round, whatever they are
MAX_DEVICES = 1024           # answers kept in one round

_SAFE_TEXT = re.compile(r"^[A-Za-z0-9 ._/()+-]{1,64}$")   # model, serial, firmware


def inquiry():
    """The probe Hikvision's SADP tool sends (a fresh UUID each time)."""
    return ('<?xml version="1.0" encoding="utf-8"?><Probe><Uuid>%s</Uuid>'
            '<Types>inquiry</Types></Probe>' % str(uuid.uuid4()).upper()).encode()


# ---- one answer --------------------------------------------------------------------
def _text(v):
    """A model / serial / firmware value, or "" when it isn't plain short text."""
    v = re.sub(r"\s+", " ", v or "").strip()
    return v if _SAFE_TEXT.match(v) else ""


def _firmware(v):
    """'V4.1.18build 200120' -> 'V4.1.18 build 200120' (how an NVR's channel list
    spells the same firmware)."""
    return _text(re.sub(r"\s*build\s*", " build ", v or "", flags=re.I))


def _ipv4(v):
    """A unicast IPv4 address as text, else "". (A loopback one can only come
    from this host, so it matches no scanned device; the tests use them.)"""
    try:
        a = ipaddress.IPv4Address((v or "").strip())
    except ValueError:
        return ""
    if a.is_unspecified or a.is_multicast or a.is_reserved:
        return ""
    return str(a)


def _flag(v):
    v = (v or "").strip().lower()
    return True if v == "true" else False if v == "false" else None


def _port(v):
    v = (v or "").strip()
    return int(v) if v.isdigit() and 0 < int(v) < 65536 else None


def parse(data):
    """One datagram -> {ip, mac, model, serial, firmware, dhcp, gateway,
    http_port, command_port, activated}, or None when it isn't a usable answer.
    `mac` is in the scan's form (aa:bb:cc:dd:ee:ff); flags are True/False/None."""
    if not data or len(data) > MAX_DGRAM:
        return None
    root = hikisapi.parse(data)
    if root is None or hikisapi.tag(root) != "ProbeMatch":
        return None

    def f(name):
        return hikisapi.child_text(root, name)

    ip, mac = _ipv4(f("IPv4Address")), identify.normalize_mac(f("MAC"))
    if not ip or not mac:
        return None
    return {"ip": ip, "mac": ":".join(mac[i:i + 2] for i in range(0, 12, 2)),
            "model": _text(f("DeviceDescription")), "serial": _text(f("DeviceSN")),
            "firmware": _firmware(f("SoftwareVersion")), "dhcp": _flag(f("DHCP")),
            "gateway": _ipv4(f("IPv4Gateway")), "http_port": _port(f("HttpPort")),
            "command_port": _port(f("CommandPort")), "activated": _flag(f("Activated"))}


# ---- where to ask ------------------------------------------------------------------
def lan_addresses(cidrs):
    """The Pi's own IPv4 addresses on the networks in `cidrs` (the local scan
    targets): SADP is link-local multicast, so it is asked on those only."""
    nets = []
    for c in cidrs or ():
        try:
            nets.append(ipaddress.ip_network(str(c), strict=False))
        except ValueError:
            pass
    try:
        out = subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in out.splitlines():
        parts = line.split()
        if "inet" not in parts or parts.index("inet") + 1 >= len(parts):
            continue
        try:
            itf = ipaddress.ip_interface(parts[parts.index("inet") + 1])
        except ValueError:
            continue
        ip = str(itf.ip)
        if itf.ip.is_loopback or ip in found:
            continue
        if any(itf.network.overlaps(n) for n in nets if n.version == 4):
            found.append(ip)
    return found


# ---- one round ---------------------------------------------------------------------
def discover(addrs, wait=None, port=None, group=None):
    """Ask on each of the Pi's addresses in `addrs` and collect the answers
    (`wait` seconds, default WAIT_S). Never raises. Returns {ts, bound, addrs
    (the ones asked on), note, answers {ip: answer}, clashes {ip: [macs]},
    dropped {too_big, bad, moved}}."""
    wait = WAIT_S if wait is None else wait
    port, group = port or PORT, group or GROUP
    res = {"ts": time.time(), "bound": False, "addrs": [], "note": "", "answers": {},
           "clashes": {}, "dropped": {"too_big": 0, "bad": 0, "moved": 0}}
    if not addrs:
        res["note"] = "no LAN address on a local scan target"
        return res
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    except OSError as e:
        res["note"] = f"no UDP socket ({e.strerror or e})"
        return res
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("", port))
            res["bound"] = True
        except OSError as e:
            s.bind(("", 0))
            res["note"] = (f"UDP {port} is in use ({e.strerror or e}) — "
                           "only answers sent straight back are seen")
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
        for a in addrs:
            try:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                             struct.pack("4s4s", socket.inet_aton(group), socket.inet_aton(a)))
                res["addrs"].append(a)
            except OSError:
                pass                      # not an address here any more / no multicast on it
        if not res["addrs"]:
            res["note"] = "could not listen for Hikvision discovery on " + ", ".join(addrs)
            return res
        _listen(s, res, port, group, wait)
    except OSError as e:
        res["note"] = f"Hikvision discovery failed ({e.strerror or e})"
    finally:
        s.close()
    return res


def _send(s, addrs, port, group):
    sent = 0
    for a in addrs:
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(a))
            s.sendto(inquiry(), (group, port))
            sent += 1
        except OSError:
            pass
    return sent


def _listen(s, res, port, group, wait):
    if not _send(s, res["addrs"], port, group):
        res["note"] = "could not send the inquiry"
        return
    start = time.monotonic()
    resend, deadline = start + RESEND_S, start + wait
    macs, packets = {}, 0                 # ip -> {macs that claimed it}
    answers, dropped = res["answers"], res["dropped"]
    while packets < MAX_PACKETS:
        now = time.monotonic()
        if now >= deadline:
            break
        if resend and now >= resend:
            _send(s, res["addrs"], port, group)
            resend = None
        s.settimeout(max(0.05, min(deadline, resend or deadline) - now))
        try:
            data, src = s.recvfrom(MAX_DGRAM + 1)
        except socket.timeout:
            continue
        except OSError:
            break
        packets += 1
        if len(data) > MAX_DGRAM:
            dropped["too_big"] += 1
            continue
        if b"ProbeMatch" not in data:
            continue                      # another tool's inquiry, or noise
        a = parse(data)
        if a is None:
            dropped["bad"] += 1
            continue
        if a["ip"] != src[0]:
            dropped["moved"] += 1         # claims one address, speaks from another
            continue
        macs.setdefault(a["ip"], set()).add(a["mac"])
        if a["ip"] not in answers and len(answers) < MAX_DEVICES:
            answers[a["ip"]] = a
    if packets >= MAX_PACKETS:
        res["note"] = f"stopped after {MAX_PACKETS} packets"
    for ip, ms in macs.items():
        if len(ms) > 1:
            res["clashes"][ip] = sorted(ms)
            answers.pop(ip, None)


if __name__ == "__main__":       # read-only: prints what answered, changes nothing
    if len(sys.argv) < 2:
        sys.exit("usage: sadp.py <LAN network, e.g. 192.168.0.0/24> [...]")
    r = discover(lan_addresses(sys.argv[1:]))
    print(json.dumps({k: v for k, v in r.items() if k != "answers"}), flush=True)
    for ip in sorted(r["answers"], key=lambda x: tuple(int(p) for p in x.split("."))):
        a = r["answers"][ip]
        print(json.dumps(dict(a, kind=identify.hik_kind(a["model"]))), flush=True)
