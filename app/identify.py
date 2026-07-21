"""Device identification: MAC vendor (offline OUI + online API), HTTP banner,
and a heuristic that turns vendor + open ports + banner into a device type.

The OUI table is a simple TSV (`AABBCC<TAB>Vendor`) baked into the image at
/app/data/oui.tsv by the Dockerfile. Online lookups (api.macvendors.com) are a
fallback for unknown prefixes and are cached to /data so each MAC is fetched once.
"""
import json
import os
import re
import socket
import threading

import requests

DATA_DIR = os.environ.get("NETWATCH_DATA", "/data")
OUI_TSV = os.path.join(os.path.dirname(__file__), "data", "oui.tsv")
OUI_CACHE = os.path.join(DATA_DIR, "oui_cache.json")

_oui_map = None
_oui_lock = threading.Lock()
_cache_lock = threading.Lock()

# Web ports we try to fetch a banner from, in order of preference.
WEB_PORTS = [(80, "http"), (8080, "http"), (443, "https"), (8443, "https"), (8000, "http")]

# Feature badge derivation: port -> single-char badge shown on the grid card.
FEATURE_PORTS = {
    22: "S", 80: "W", 443: "W", 8080: "W", 8443: "W",
    554: "R", 8000: "H", 8291: "M", 1883: "Q",
}


def normalize_mac(mac):
    if not mac:
        return ""
    h = re.sub(r"[^0-9a-fA-F]", "", mac).lower()
    return h if len(h) == 12 else ""


def _load_oui():
    global _oui_map
    if _oui_map is not None:
        return _oui_map
    with _oui_lock:
        if _oui_map is not None:
            return _oui_map
        m = {}
        try:
            with open(OUI_TSV, encoding="utf-8", errors="replace") as f:
                for line in f:
                    parts = line.rstrip("\n").split("\t", 1)
                    if len(parts) == 2 and parts[0]:
                        m[parts[0].upper()] = parts[1]
        except FileNotFoundError:
            pass
        _oui_map = m
        return m


def _load_cache():
    try:
        with open(OUI_CACHE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(cache):
    with _cache_lock:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = OUI_CACHE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, OUI_CACHE)


def vendor_for_mac(mac, online_ok=True):
    """Return a best-effort vendor string for a MAC, or '' if unknown."""
    h = normalize_mac(mac)
    if not h:
        return ""
    prefix = h[:6].upper()
    name = _load_oui().get(prefix)
    if name:
        return name
    cache = _load_cache()
    if prefix in cache:
        return cache[prefix]
    if not online_ok:
        return ""
    try:
        sep = ":".join(h[i:i + 2] for i in range(0, 12, 2))
        r = requests.get(f"https://api.macvendors.com/{sep}", timeout=4)
        name = r.text.strip() if r.status_code == 200 and r.text and "errors" not in r.text else ""
    except requests.RequestException:
        name = ""
    cache[prefix] = name  # cache misses too, so we don't hammer the API
    _save_cache(cache)
    return name


def reverse_dns(ip):
    try:
        return socket.gethostbyaddr(ip)[0]
    except (socket.herror, socket.gaierror, OSError):
        return ""


def http_banner(ip, open_ports, online_ok=True):
    """Fetch '/' on the first reachable web port; return {title, server, url}."""
    if not online_ok:
        return {}
    for port, scheme in WEB_PORTS:
        if port not in open_ports:
            continue
        url = f"{scheme}://{ip}:{port}/"
        try:
            r = requests.get(url, timeout=4, verify=False, allow_redirects=True)
        except requests.RequestException:
            continue
        server = r.headers.get("Server", "")
        title = ""
        m = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.I | re.S)
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip()[:120]
        if server or title:
            return {"title": title, "server": server, "url": url}
    return {}


def features_for_ports(open_ports, deep=False):
    feats = []
    for p in open_ports:
        f = FEATURE_PORTS.get(p)
        if f and f not in feats:
            feats.append(f)
    if deep:
        feats.append("+")
    return feats


def classify(vendor, open_ports, banner, hostname):
    """Return (category, type_label, confidence) from the available signals.

    Ordered so that strong, network-independent evidence (HTTP banner / vendor)
    wins over ambiguous single-port guesses — e.g. an HP LaserJet that happens to
    expose port 8291 is a printer, not a MikroTik. category values map to the
    dashboard colour scheme (see catStyle/catIcon in index.html).
    """
    v = (vendor or "").lower()
    b = " ".join([banner.get("title", ""), banner.get("server", ""), hostname or ""]).lower()
    text = v + " " + b
    ports = set(open_ports)

    def has(*words):
        return any(w in text for w in words)

    # Printers / MFPs — banner/vendor, or printing ports (9100 JetDirect, 515 LPD, 631 IPP)
    if has("laserjet", "officejet", "deskjet", "designjet", "pagewide", "imageclass",
           "workforce", "ecotank", " mfp", "printer", "kyocera", "lexmark", "ricoh",
           "brother", "epson", "canon", "xerox", "develop ineo") or (ports & {9100, 515, 631}):
        return "printer", "Printer / MFP", "high"

    # Cameras / NVRs (Hikvision, Dahua, Axis, Uniview ...)
    cam_vendor = any(x in v for x in ("hikvision", "dahua", "axis", "hangzhou", "uniview",
                                       "reolink", "hanwha", "vivotek", "amcrest"))
    if "nvr" in b or "9664" in b or has("recorder") or (cam_vendor and 8000 in ports and 554 not in ports):
        return "nvr", "NVR / Recorder", "high" if cam_vendor else "med"
    if 554 in ports or cam_vendor or has("ipcamera", "ip camera", "webcam", "netcam"):
        return "camera", "IP Camera", "high" if (cam_vendor and 554 in ports) else "med"

    # NAS / storage
    if has("synology", "qnap", "truenas", "freenas", "diskstation", "rackstation", "openmediavault") \
            or ((ports & {5000, 5001}) and "http" in b):
        return "nas", "NAS / Storage", "med"

    # VoIP phones / PBX
    if has("grandstream", "yealink", "polycom", "snom", "fanvil", "voip", "sip phone", "cisco spa") \
            or 5060 in ports:
        return "voip", "VoIP / Phone", "med"

    # Alarm / security panels (Risco, Ajax, Paradox ...)
    if has("risco", "ajax systems", "paradox security", "alarm panel", "alarm system"):
        return "alarm", "Alarm system", "med"

    # Solar / inverters / energy (common on farms)
    if has("victron", "fronius", "goodwe", "sungrow", "deye", "solaredge", "growatt",
           "huawei sun", "sma ", "inverter", "solar", "shelly em"):
        return "solar", "Solar / Inverter", "med"

    # Media / TV / streaming
    if has("roku", "chromecast", "apple tv", "appletv", "android tv", "smart tv",
           "samsung tv", "webos", "bravia", "kodi", "plex", "shield"):
        return "media", "Media / TV", "med"

    # Networking gear (pro APs / radios / switches)
    if any(x in v for x in ("ubiquiti", "ubnt", "mikrotik", "ruijie", "ruckus", "aruba",
                            "zyxel", "engenius", "cambium")) or "unifi" in b or "routeros" in b \
            or 8291 in ports:
        if 53 in ports and (80 in ports or 443 in ports):
            return "internet-ap", "Router / Gateway", "high"
        return "network", "Access Point / Switch", "high"

    # Consumer routers / internet APs, or a host serving DNS + web (the gateway)
    if any(x in v for x in ("cudy", "tp-link", "tplink", "mercusys", "d-link", "dlink",
                            "netgear", "asus", "huawei", "zte")) or "router" in b \
            or (53 in ports and (80 in ports or 443 in ports)):
        return "internet-ap", "Router / Gateway", "med" if not (53 in ports) else "high"

    # IoT (smart plugs / ESP / smart-home)
    if any(x in v for x in ("tuya", "espressif", "sonoff", "shelly", "tasmota", "itead",
                            "xiaomi", "ewelink", "broadlink", "tplink smart", "sengled")):
        return "iot", "IoT Device", "med"

    # Computers / servers
    if "raspberry" in text:
        return "server", "Raspberry Pi", "high"
    if any(x in v for x in ("intel", "dell", "lenovo", "asustek", "gigabyte", "micro-star",
                            "apple", "hewlett")) and (ports & {22, 445, 3389, 139}):
        return "pc", "Computer / Host", "low"
    if 22 in ports and (80 in ports or 443 in ports):
        return "server", "Server / Host", "low"

    if vendor:
        return "unknown", vendor, "low"
    return "unknown", "Unknown device", "low"
