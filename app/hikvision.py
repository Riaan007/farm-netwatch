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
