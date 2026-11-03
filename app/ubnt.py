"""Ubiquiti discovery (UDP 10001) — the probe the UISP / "Ubiquiti Device
Discovery Tool" uses. Every airOS radio, UniFi device and EdgeRouter answers a
4-byte broadcast with its own MAC, IP(s), hostname, model and firmware. No
credentials needed.

Used to name the device behind a "same MAC, multiple IPs" problem: a proxy-ARP
wireless station answers ARP for every client behind it with ITS OWN MAC, so
the scan can't tell which of those IPs is the radio itself — but the radio
answers this probe with its real address. Runs in the netwatch container's
host-network namespace, so the broadcast reaches the real LAN."""

import socket
import struct
import time

import identify

PORT = 10001
_PROBE_V1 = b"\x01\x00\x00\x00"
_TEXT = {0x03: "firmware", 0x0B: "hostname", 0x0C: "platform", 0x0D: "essid",
         0x14: "model", 0x15: "model_short", 0x16: "version"}


def parse(data):
    """One discovery reply -> {mac, ips, hostname, model, platform, firmware,
    essid, uptime_s}, or None if it isn't one."""
    if len(data) < 4 or data[0] not in (1, 2) or data[1] != 0:
        return None
    end = min(len(data), 4 + struct.unpack(">H", data[2:4])[0])
    out = {"ips": []}
    off = 4
    while off + 3 <= end:
        t = data[off]
        ln = struct.unpack(">H", data[off + 1:off + 3])[0]
        v = data[off + 3:off + 3 + ln]
        off += 3 + ln
        if t == 0x01 and ln == 6:
            out.setdefault("mac", v.hex())
        elif t == 0x02 and ln == 10:
            out.setdefault("mac", v[:6].hex())
            ip = ".".join(str(b) for b in v[6:])
            if ip not in out["ips"]:
                out["ips"].append(ip)
        elif t in _TEXT:
            out[_TEXT[t]] = v.decode("utf-8", "replace").strip("\x00").strip()
        elif t == 0x0A and ln == 4:
            out["uptime_s"] = struct.unpack(">I", v)[0]
    return out if out.get("mac") else None


def discover(timeout=3.0, unicast=()):
    """Broadcast the probe (plus a unicast copy to each IP in `unicast`, which
    reaches a radio whose broadcast domain we're not on) and collect replies.
    Returns {bare-hex MAC: info}; info["ip"] is the address it answered from."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    seen = {}
    try:
        s.bind(("0.0.0.0", 0))
        for dst in ["255.255.255.255", *unicast]:
            try:
                s.sendto(_PROBE_V1, (dst, PORT))
            except OSError:
                pass
        s.settimeout(0.5)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            info = parse(data)
            mac = identify.normalize_mac((info or {}).get("mac", ""))
            if not mac:
                continue
            info["mac"] = mac
            info["ip"] = addr[0]
            seen[mac] = info
    finally:
        s.close()
    return seen
