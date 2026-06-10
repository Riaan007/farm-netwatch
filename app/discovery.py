"""Layer-2 discovery to enrich (and extend) the nmap results on the LOCAL
segment — the WiFiman-style part. Finds devices that ignore ARP/ping but answer
service-discovery protocols, and pulls friendly names / models / serials:

  * SSDP / UPnP (UDP 1900)   -> friendlyName, manufacturer, modelName, serialNumber
  * mDNS / Bonjour (5353)    -> .local hostname + service type (via zeroconf)
  * NetBIOS (UDP 137)        -> Windows/SMB workstation name
  * ARP table (/proc/net/arp)-> IP↔MAC the Pi already knows (no probe needed)

All best-effort; failures are swallowed. Remote/routed subnets can't be reached
this way (these are link-local protocols) — that needs an interface on the subnet.
"""
import re
import socket
import struct
import time
import xml.etree.ElementTree as ET

import requests


# ---- ARP table ---------------------------------------------------------
def arp_table():
    """{ip: mac} from the kernel ARP cache (everything the Pi has talked to)."""
    out = {}
    try:
        with open("/proc/net/arp") as f:
            next(f, None)
            for line in f:
                parts = line.split()
                if len(parts) >= 4 and parts[3] != "00:00:00:00:00:00":
                    out[parts[0]] = parts[3].lower()
    except OSError:
        pass
    return out


# ---- SSDP / UPnP -------------------------------------------------------
def _http_headers(text):
    h = {}
    for line in text.split("\r\n")[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            h[k.strip().lower()] = v.strip()
    return h


def _fetch_upnp_desc(location, timeout=4):
    try:
        r = requests.get(location, timeout=timeout)
        root = ET.fromstring(r.text)
    except (requests.RequestException, ET.ParseError):
        return {}
    info = {}
    wanted = {"friendlyName": "hostname", "manufacturer": "manufacturer",
              "modelName": "model", "serialNumber": "serial"}
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag in wanted and el.text and el.text.strip():
            info.setdefault(wanted[tag], el.text.strip())
    return info


def ssdp(timeout=3):
    msg = ("M-SEARCH * HTTP/1.1\r\n"
           "HOST: 239.255.255.250:1900\r\n"
           'MAN: "ssdp:discover"\r\n'
           "MX: 2\r\nST: ssdp:all\r\n\r\n").encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    s.settimeout(timeout)
    found = {}
    try:
        s.sendto(msg, ("239.255.255.250", 1900))
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                break
            except OSError:
                break
            h = _http_headers(data.decode("utf-8", "ignore"))
            d = found.setdefault(addr[0], {"source": "ssdp"})
            if h.get("server"):
                d["server"] = h["server"]
            if h.get("location") and "location" not in d:
                d["location"] = h["location"]
    finally:
        s.close()
    for ip, d in found.items():
        if d.get("location"):
            d.update(_fetch_upnp_desc(d.pop("location")))
    return found


# ---- NetBIOS node status (UDP 137) ------------------------------------
_NB_QUERY = (b"\xa2\x48\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00"
             b"\x20CKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\x00"
             b"\x00\x21\x00\x01")


def netbios_name(ip, timeout=1.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(_NB_QUERY, (ip, 137))
        data, _ = s.recvfrom(2048)
    except (socket.timeout, OSError):
        return ""
    finally:
        s.close()
    try:
        count = data[56]
        off = 57
        for _ in range(count):
            name = data[off:off + 15].decode("ascii", "ignore").strip()
            suffix = data[off + 15]
            flags = struct.unpack(">H", data[off + 16:off + 18])[0]
            off += 18
            group = flags & 0x8000
            if suffix == 0x00 and not group and name:
                return name
    except (IndexError, struct.error):
        pass
    return ""


# ---- mDNS / Bonjour (zeroconf) ----------------------------------------
_MDNS_TYPES = ["_http._tcp.local.", "_https._tcp.local.", "_workstation._tcp.local.",
               "_ipp._tcp.local.", "_printer._tcp.local.", "_pdl-datastream._tcp.local.",
               "_rtsp._tcp.local.", "_googlecast._tcp.local.", "_airplay._tcp.local.",
               "_raop._tcp.local.", "_hap._tcp.local.", "_smb._tcp.local.",
               "_axis-video._tcp.local.", "_device-info._tcp.local."]

# mDNS service-type -> a category hint
_MDNS_CAT = {"_printer": "printer", "_pdl-datastream": "printer", "_ipp": "printer",
             "_rtsp": "camera", "_axis-video": "camera", "_googlecast": "media",
             "_airplay": "media", "_raop": "media", "_workstation": "pc", "_smb": "nas"}


def _enc_name(name):
    out = b""
    for part in name.split("."):
        if part:
            out += bytes([len(part)]) + part.encode("ascii", "ignore")
    return out + b"\x00"


def _read_name(data, off):
    """Decode a (possibly compressed) DNS name; return (name, next_offset)."""
    labels, jumped, nxt = [], False, off
    for _ in range(128):                       # guard against loops
        if off >= len(data):
            break
        length = data[off]
        if length & 0xC0 == 0xC0:              # compression pointer
            if not jumped:
                nxt = off + 2
            off = struct.unpack(">H", data[off:off + 2])[0] & 0x3FFF
            jumped = True
            continue
        if length == 0:
            off += 1
            if not jumped:
                nxt = off
            break
        labels.append(data[off + 1:off + 1 + length].decode("utf-8", "ignore"))
        off += 1 + length
    return ".".join(labels), nxt


def mdns(timeout=3):
    """One-shot stdlib mDNS: query common services, request unicast replies, and
    read back A/PTR records to map device IP -> hostname + service category."""
    qtypes = _MDNS_TYPES + ["_services._dns-sd._udp.local."]
    header = struct.pack(">HHHHHH", 0, 0, len(qtypes), 0, 0, 0)
    body = b"".join(_enc_name(t.rstrip(".")) + struct.pack(">HH", 12, 0x8001)  # PTR, IN|unicast
                    for t in qtypes)
    pkt = header + body
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    s.settimeout(timeout)
    results = {}
    try:
        s.sendto(pkt, ("224.0.0.251", 5353))
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, addr = s.recvfrom(8192)
            except (socket.timeout, OSError):
                break
            try:
                _parse_mdns(data, addr[0], results)
            except (struct.error, IndexError):
                continue
    finally:
        s.close()
    return results


def _parse_mdns(data, ip, results):
    qd, an, ns, ar = struct.unpack(">HHHH", data[4:12])
    off = 12
    for _ in range(qd):
        _, off = _read_name(data, off)
        off += 4
    d = results.setdefault(ip, {"source": "mdns"})
    for _ in range(an + ns + ar):
        _name, off = _read_name(data, off)
        if off + 10 > len(data):
            break
        rtype, _rclass, _ttl, rdlen = struct.unpack(">HHIH", data[off:off + 10])
        off += 10
        rdata_off = off
        off += rdlen
        if rtype == 1 and rdlen == 4:                       # A record
            host = _name.rstrip(".")
            if host.endswith(".local"):
                host = host[:-6]
            if host and "hostname" not in d:
                d["hostname"] = host
        elif rtype == 12:                                   # PTR -> service type
            svc, _ = _read_name(data, rdata_off)
            short = svc.split(".")[0]
            if short in _MDNS_CAT:
                d["mdns_category"] = _MDNS_CAT[short]
                d.setdefault("mdns_type", short)


# ---- orchestration -----------------------------------------------------
def gather(live_ips=None, timeout=3, want_netbios=True):
    """Run all local discovery methods and merge into {ip: {hostname, model,
    serial, manufacturer, mac, mdns_category, source}}."""
    import threading
    out = {}

    def merge(src):
        for ip, d in (src or {}).items():
            tgt = out.setdefault(ip, {})
            for k, v in d.items():
                if v and not tgt.get(k):
                    tgt[k] = v

    ssdp_res, mdns_res = {}, {}

    def run_ssdp():
        ssdp_res.update(ssdp(timeout))

    def run_mdns():
        mdns_res.update(mdns(timeout))

    t1 = threading.Thread(target=run_ssdp, daemon=True)
    t2 = threading.Thread(target=run_mdns, daemon=True)
    t1.start(); t2.start(); t1.join(timeout + 3); t2.join(timeout + 3)
    merge(ssdp_res)
    merge(mdns_res)

    for ip, mac in arp_table().items():
        out.setdefault(ip, {}).setdefault("mac", mac)
        out[ip].setdefault("source", "arp")

    if want_netbios:
        import concurrent.futures
        targets = [ip for ip in (set(live_ips or []) | set(out.keys()))
                   if not out.get(ip, {}).get("hostname")]
        if targets:
            with concurrent.futures.ThreadPoolExecutor(max_workers=24) as ex:
                for ip, nm in zip(targets, ex.map(lambda i: netbios_name(i, 0.8), targets)):
                    if nm:
                        out.setdefault(ip, {})["hostname"] = nm
    return out
