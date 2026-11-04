"""Fake Hikvision devices that answer SADP (UDP 37020) for tests/test_sadp.py.

Each fake unit answers an inquiry the way the real ones do: a <ProbeMatch> sent
to the multicast group 239.255.255.250 on port 37020, from its own address. They
live on the loopback (127.0.0.N): multicast works there in a `--network none`
container with no setup, so nothing leaves the box. Every model, serial and MAC
here is made up.

Switches per unit: `src` = the address it really sends from (a unit that claims
one address and speaks from another), `raw` = the exact bytes it sends instead of
its answer (hostile answers), `repeat` = answers per inquiry, `from_inquiry` = the
first inquiry it answers (a unit that missed the first one). The responder keeps
every datagram it received in `heard`, so a test can prove what Netwatch sent.
"""
import socket
import struct
import threading
from xml.sax.saxutils import escape

GROUP, PORT, IFACE = "239.255.255.250", 37020, "127.0.0.1"


def probe_match(ip, mac, model, serial, firmware, uuid="", dhcp="false", gateway="127.0.0.254",
                http="80", command="8000", activated="true", channels="1"):
    """An answer laid out like a real one (field order and the extra fields too)."""
    fields = [("Uuid", uuid), ("Types", "inquiry"), ("DeviceType", "139784"),
              ("DeviceDescription", model), ("DeviceSN", serial), ("CommandPort", command),
              ("HttpPort", http), ("MAC", mac), ("IPv4Address", ip),
              ("IPv4SubnetMask", "255.255.255.0"), ("IPv4Gateway", gateway), ("IPv6Address", "::"),
              ("IPv6Gateway", "::"), ("IPv6MaskLen", "64"), ("DHCP", dhcp), ("AnalogChannelNum", "0"),
              ("DigitalChannelNum", channels), ("SoftwareVersion", firmware),
              ("DSPVersion", "V7.3 build 250101"), ("BootTime", "2026-01-01 00:00:00"),
              ("Encrypt", "true"), ("ResetAbility", "false"), ("DiskNumber", "0"),
              ("Activated", activated), ("PasswordResetAbility", "true"), ("SupportHCPlatform", "true"),
              ("HCPlatformEnable", "flase"), ("OEMInfo", "N/A")]
    body = "".join(f"<{k}>{escape(v)}</{k}>" for k, v in fields)
    return ('<?xml version="1.0" encoding="UTF-8"?><ProbeMatch>' + body + "</ProbeMatch>").encode()


class Unit:
    def __init__(self, ip, mac, model, serial="", firmware="V5.7.20build 250101", src=None, raw=None,
                 repeat=1, from_inquiry=1, **fields):
        self.ip, self.mac, self.model, self.serial, self.firmware = ip, mac, model, serial, firmware
        self.src, self.raw, self.repeat, self.from_inquiry, self.fields = src or ip, raw, repeat, from_inquiry, fields

    def answer(self, uuid):
        if self.raw is not None:
            return self.raw
        return probe_match(self.ip, self.mac, self.model, self.serial, self.firmware, uuid=uuid, **self.fields)


class Responder:
    """Listens on the group like a Hikvision device and answers each inquiry for
    every unit it holds."""

    def __init__(self, *units, port=PORT, group=GROUP, iface=IFACE):
        self.units, self.port, self.group, self.iface = list(units), port, group, iface
        self.heard = []           # every datagram that reached the listener
        self.inquiries = 0
        self._stop = threading.Event()
        self._sock = self._thread = None

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", self.port))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                     struct.pack("4s4s", socket.inet_aton(self.group), socket.inet_aton(self.iface)))
        s.settimeout(0.1)
        self._sock = s
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        """Stops answering and frees the port before it returns (the next test binds it)."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self):
        try:
            while not self._stop.is_set():
                try:
                    data, _src = self._sock.recvfrom(65535)
                except socket.timeout:
                    continue
                self.heard.append(data)
                if b"<Probe>" not in data or b"<Types>inquiry</Types>" not in data:
                    continue              # an answer (ours or another unit's), not an inquiry
                self.inquiries += 1
                uuid = data.split(b"<Uuid>", 1)[-1].split(b"</Uuid>", 1)[0].decode("ascii", "replace")
                for u in self.units:
                    for _ in range(u.repeat if self.inquiries >= u.from_inquiry else 0):
                        if self._stop.is_set():
                            return
                        self._send(u.src, u.answer(uuid))
        finally:
            self._sock.close()

    def _send(self, src, payload):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind((src, 0))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.iface))
            s.sendto(payload, (self.group, self.port))
        finally:
            s.close()
