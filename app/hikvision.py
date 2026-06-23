"""Authenticated Hikvision ISAPI device-info fetch.

Hikvision cameras/NVRs expose /ISAPI/System/deviceInfo which returns model,
serial number, firmware, device type, etc. — but only to an authenticated
request (HTTP Digest, sometimes Basic). Uses the per-device saved credentials.
"""
import xml.etree.ElementTree as ET

import requests
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

# Tags we lift out of the (namespaced) ISAPI XML.
FIELDS = {"deviceName", "deviceID", "model", "serialNumber", "macAddress",
          "firmwareVersion", "firmwareReleasedDate", "deviceType",
          "hardwareVersion", "encoderVersion"}


def _parse(text):
    out = {}
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return out
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]      # strip XML namespace
        if tag in FIELDS and el.text and el.text.strip():
            out[tag] = el.text.strip()
    return out


def fetch(ip, username, password, timeout=6):
    """Return {'ok': True, 'info': {...}} or {'ok': False, 'error': str}."""
    if not ip:
        return {"ok": False, "error": "no IP"}
    last = "no response"
    for scheme in ("http", "https"):
        url = f"{scheme}://{ip}/ISAPI/System/deviceInfo"
        for auth in (HTTPDigestAuth(username, password), HTTPBasicAuth(username, password)):
            try:
                r = requests.get(url, auth=auth, timeout=timeout, verify=False)
            except requests.RequestException as e:
                last = f"connection failed ({e.__class__.__name__})"
                continue
            if r.status_code == 200:
                info = _parse(r.text)
                if info:
                    return {"ok": True, "info": info}
                last = "200 OK but no ISAPI device-info (not a Hikvision device?)"
            elif r.status_code in (401, 403):
                last = "authentication failed — check the saved username/password"
            else:
                last = f"HTTP {r.status_code}"
    return {"ok": False, "error": last}


# ---- network config (read + change the camera's IP) ----------------------
NET_PATH = "/ISAPI/System/Network/interfaces/1/ipAddress"


def _ns(root):
    """Default XML namespace of a parsed ISAPI doc (or '' if none)."""
    return root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else ""


def _q(ns, tag):
    return f"{{{ns}}}{tag}" if ns else tag


def _net_get_raw(ip, username, password, timeout):
    """GET the interface ipAddress doc. Returns (scheme, auth, text), ('AUTH',..)
    on auth failure, or None if unreachable."""
    for scheme in ("http", "https"):
        url = f"{scheme}://{ip}{NET_PATH}"
        for auth in (HTTPDigestAuth(username, password), HTTPBasicAuth(username, password)):
            try:
                r = requests.get(url, auth=auth, timeout=timeout, verify=False)
            except requests.RequestException:
                continue
            if r.status_code == 200 and r.text.strip():
                return scheme, auth, r.text
            if r.status_code in (401, 403):
                return "AUTH", None, None
    return None


def get_network(ip, username, password, timeout=6):
    """Read the camera's current IPv4 settings (to pre-fill the change form)."""
    if not ip:
        return {"ok": False, "error": "no IP"}
    got = _net_get_raw(ip, username, password, timeout)
    if got is None:
        return {"ok": False, "error": "could not reach the camera's ISAPI network API"}
    if got[0] == "AUTH":
        return {"ok": False, "error": "authentication failed — check the saved username/password"}
    _scheme, _auth, text = got
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return {"ok": False, "error": "unexpected response (not a Hikvision device?)"}
    ns = _ns(root)

    def _txt(tag):
        el = root.find(_q(ns, tag))
        return el.text.strip() if (el is not None and el.text) else ""

    gw, gw_ip = root.find(_q(ns, "DefaultGateway")), ""
    if gw is not None:
        gwip = gw.find(_q(ns, "ipAddress"))
        gw_ip = gwip.text.strip() if (gwip is not None and gwip.text) else ""
    return {"ok": True, "addressingType": _txt("addressingType"),
            "ipAddress": _txt("ipAddress"), "subnetMask": _txt("subnetMask"),
            "gateway": gw_ip}


def _edit_ip_xml(text, new_ip, mask, gateway):
    """Read-modify-write the ISAPI ipAddress XML: force static + set the IPv4
    fields, preserving every other element and the default namespace."""
    root = ET.fromstring(text)
    ns = _ns(root)
    if ns:
        ET.register_namespace("", ns)        # keep the default ns on re-serialise

    def _set(tag, value):
        el = root.find(_q(ns, tag))
        if el is not None and value:
            el.text = value

    _set("addressingType", "static")
    _set("ipAddress", new_ip)
    _set("subnetMask", mask)
    if gateway:
        gw = root.find(_q(ns, "DefaultGateway"))
        if gw is not None:
            gwip = gw.find(_q(ns, "ipAddress"))
            if gwip is not None:
                gwip.text = gateway
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


def reboot(ip, scheme, auth, timeout=8):
    """Reboot the camera (PUT /ISAPI/System/reboot). The connection drops as it
    goes down — that's expected, so a RequestException counts as 'reboot sent'."""
    url = f"{scheme}://{ip}/ISAPI/System/reboot"
    try:
        r = requests.put(url, auth=auth, timeout=timeout, verify=False)
        return r.status_code in (200, 0) or r.status_code < 400
    except requests.RequestException:
        return True            # it went down — that's the reboot


def set_ip(ip, username, password, new_ip, mask="", gateway="", timeout=8):
    """Change the camera's IPv4 address via ISAPI (read-modify-write PUT).

    Hikvision writes the new IP but, on many firmwares, only applies it after a
    REBOOT (the PUT returns statusCode 7 / 'Reboot Required'). When it does, we
    reboot the camera so the change actually takes effect; it comes back on the
    new address in ~1 minute. Returns {'ok', 'new_ip', 'rebooted', 'msg'} or
    {'ok': False, 'error'}."""
    if not (ip and new_ip):
        return {"ok": False, "error": "missing current or new IP"}
    got = _net_get_raw(ip, username, password, timeout)
    if got is None:
        return {"ok": False, "error": "could not reach the camera's ISAPI network API"}
    if got[0] == "AUTH":
        return {"ok": False, "error": "authentication failed — check the saved username/password"}
    scheme, auth, text = got
    try:
        payload = _edit_ip_xml(text, new_ip, mask, gateway)
    except ET.ParseError:
        return {"ok": False, "error": "could not parse the camera's network config"}
    url = f"{scheme}://{ip}{NET_PATH}"
    try:
        r = requests.put(url, data=payload.encode("utf-8"), auth=auth, timeout=timeout,
                         verify=False, headers={"Content-Type": "application/xml"})
    except requests.RequestException as e:
        return {"ok": False, "error": f"no confirmation ({e.__class__.__name__}) — the "
                f"camera may already have moved to {new_ip}; verify with a scan"}
    if r.status_code in (401, 403):
        return {"ok": False, "error": "authentication failed"}
    if r.status_code != 200:
        detail = ""
        try:
            rr = ET.fromstring(r.text)
            sub = rr.find(_q(_ns(rr), "subStatusCode"))
            if sub is not None and sub.text:
                detail = f" ({sub.text})"
        except ET.ParseError:
            pass
        return {"ok": False, "error": f"camera rejected the change: HTTP {r.status_code}{detail}"}

    # 200 OK — did it apply live, or is a reboot needed to take effect?
    needs_reboot = "reboot" in (r.text or "").lower()
    if needs_reboot:
        # Reboot via the OLD address (still live right now) so the new IP applies.
        reboot(ip, scheme, auth, timeout)
        return {"ok": True, "new_ip": new_ip, "rebooted": True,
                "msg": f"Camera is rebooting to apply {new_ip} (back in ~1 min)."}
    return {"ok": True, "new_ip": new_ip, "rebooted": False,
            "msg": f"Camera moved to {new_ip}."}
