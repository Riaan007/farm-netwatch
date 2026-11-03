"""Setup wizard: a fixed IP address for this Pi.

The wizard runs ONE quick ping sweep of the Pi's own LAN to see which addresses are
taken (nothing is saved — the first real scan still waits for the operator), suggests
a free address, re-checks the chosen one, and then either:

* applies it through NetworkManager with netcfg's auto-revert watchdog — on the
  opt-in netcfg image, where nmcli can reach the host — and hands the browser a
  one-time token to confirm from the new address (no confirm = old address back), or
* returns the details for a DHCP reservation on the router (this Pi's MAC + the
  address), which works on every image.
"""
import hmac
import ipaddress
import re
import secrets
import subprocess
import threading
import time
import xml.etree.ElementTree as ET

import netcfg

MAX_HOSTS = 1024          # sweep at most a /22 — larger LANs get no sweep, just checks
REVERT_S = 300            # time to reach the new address and confirm it
_token = {"value": None, "ts": 0}


def _nmap(args, timeout):
    """nmap's XML on stdout (stderr warnings would break the parse)."""
    try:
        p = subprocess.run(["nmap", *args, "-oX", "-"], capture_output=True, text=True,
                           timeout=timeout)
        return p.stdout if p.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _up_hosts(xml):
    out = {}
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return out
    for h in root.iter("host"):
        st = h.find("status")
        if st is None or st.get("state") != "up":
            continue
        ip = mac = vendor = ""
        for a in h.findall("address"):
            if a.get("addrtype") == "ipv4":
                ip = a.get("addr", "")
            elif a.get("addrtype") == "mac":
                mac, vendor = a.get("addr", "").lower(), a.get("vendor", "")
        if ip:
            out[ip] = {"ip": ip, "mac": mac, "vendor": vendor}
    return out


def primary():
    """The default-route interface: {iface, address (ip/prefix), mac, dhcp} or None."""
    dev = netcfg._default_dev()
    if not dev:
        return None
    rc, out = netcfg._run(["ip", "-o", "-4", "addr", "show", "dev", dev])
    line = next((ln for ln in out.splitlines() if " inet " in ln), "") if rc == 0 else ""
    m = re.search(r"inet\s+(\S+)", line)
    if not m:
        return None
    try:
        with open(f"/sys/class/net/{dev}/address") as f:
            mac = f.read().strip().lower()
    except OSError:
        mac = ""
    return {"iface": dev, "address": m.group(1), "mac": mac, "dhcp": " dynamic " in f" {line} "}


def suggest(net, taken):
    """A free address high in the range — clear of the last four (.251-.254 on a /24,
    where routers and APs like to sit) and of everything the sweep saw."""
    hosts = list(net.hosts())
    for h in reversed(hosts[:-4] if len(hosts) > 8 else hosts):
        if str(h) not in taken:
            return str(h)
    return None


def plan(gateway):
    """What the wizard's fixed-IP step shows. `gateway` = scanner.default_gateway()."""
    p = primary()
    if not p:
        return {"ok": False, "error": "This Pi has no network address on its main port."}
    iface = ipaddress.ip_interface(p["address"])
    net = iface.network
    own = str(iface.ip)
    used = {}
    swept = net.num_addresses <= MAX_HOSTS
    if swept:
        used = _up_hosts(_nmap(["-sn", "-n", "-T4", "--max-retries", "1", str(net)], 120))
    used.pop(own, None)
    taken = set(used) | {own} | ({gateway} if gateway else set())
    con = _connection(p["iface"])
    return {
        "ok": True, "iface": p["iface"], "ip": own, "prefix": net.prefixlen,
        "network": str(net), "gateway": gateway or "", "mac": p["mac"], "dhcp": p["dhcp"],
        "swept": swept, "used": sorted(used.values(), key=lambda d: ipaddress.ip_address(d["ip"])),
        "suggested": suggest(net, taken), "can_apply": bool(con), "connection": con or "",
    }


def _connection(dev):
    """The NetworkManager connection on `dev`, when this image can change it."""
    if not netcfg.nm_available():
        return None
    return next((c["name"] for c in netcfg.list_connections() if c["device"] == dev), None)


def check(ip, gateway):
    """Is `ip` a sensible free address for this Pi? (ok, why-not)."""
    p = primary()
    if not p:
        return False, "This Pi has no network address on its main port."
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False, "That is not an IP address."
    iface = ipaddress.ip_interface(p["address"])
    if addr not in iface.network or addr in (iface.network.network_address,
                                             iface.network.broadcast_address):
        return False, f"Pick an address inside {iface.network}."
    if gateway and ip == gateway:
        return False, "That is the router's address."
    if ip == str(iface.ip):
        return True, ""                       # its own current address — free by definition
    if ip in _up_hosts(_nmap(["-sn", "-n", "--max-retries", "2", ip], 30)):
        return False, f"{ip} is in use by another device."
    return True, ""


def apply(ip, gateway):
    """Validate, then (where NetworkManager is reachable) switch to the fixed address
    a moment AFTER the reply leaves — the change cuts the old address the browser is
    using. Returns the wizard's `fixed_ip` reply."""
    ok, why = check(ip, gateway)
    if not ok:
        return {"applied": False, "ip": ip, "error": why}
    p = primary()
    con = _connection(p["iface"])
    if not con:
        return {"applied": False, "ip": ip, "mac": p["mac"], "reason": "reservation"}
    prefix = ipaddress.ip_interface(p["address"]).network.prefixlen
    dns = netcfg.device_runtime(p["iface"]).get("dns") or gateway or ""
    tok = secrets.token_urlsafe(18)
    _token.update(value=tok, ts=time.time())

    def go():
        ok, msg, _ = netcfg.apply_static_with_revert(con, f"{ip}/{prefix}", gateway or "",
                                                     dns, REVERT_S)
        if not ok:
            print(f"[setupip] fixed IP {ip} not applied: {msg}", flush=True)
    threading.Timer(2.0, go).start()
    return {"applied": True, "ip": ip, "old_ip": str(ipaddress.ip_interface(p["address"]).ip),
            "token": tok, "revert_s": REVERT_S}


def confirm(token):
    """The browser reached the new address: keep it (stops the auto-revert)."""
    tok = _token["value"]
    if not tok or not token or time.time() - _token["ts"] > REVERT_S + 60 \
            or not hmac.compare_digest(tok, str(token)):
        return False
    _token.update(value=None, ts=0)
    netcfg.confirm_static()
    return True
