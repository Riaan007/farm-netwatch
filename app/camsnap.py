"""Generic snapshot fetch for cameras that are not Hikvision ISAPI.

campics.py reaches Hikvision cameras and recorders through ISAPI (hikisapi.py).
A camera that isn't Hikvision — the Dynacolor fisheye on the home LAN, an old
Mbedthis-Appweb box — has no ISAPI, but it still serves a plain JPEG at a
snapshot URL: an ONVIF GetSnapshotUri, or a vendor CGI such as
http://<ip>/cgi-bin/jpg/image.cgi. This module GETs that JPEG with the camera's
own saved login, answering HTTP Basic or Digest from the camera's own
WWW-Authenticate challenge, and can ask a camera over ONVIF where its snapshot is.

Why the digest is hand-rolled: Python's urllib digest handler (and requests'
HTTPDigestAuth) trip over this vintage of Appweb (2.4.2, realm
"MegapixelIPCamera"). So the Authorization header is built here, RFC 2617 by the
book:
    HA1 = md5(user:realm:pass)              (algorithm MD5-sess: md5(HA1:nonce:cnonce))
    HA2 = md5(method:uri)
    response = md5(HA1:nonce:nc:cnonce:qop:HA2)   when the server offers qop=auth
             = md5(HA1:nonce:HA2)                 when it does not (RFC 2069)
an `opaque` value is echoed back when the challenge carries one. The transport is
`requests`, but with the header we built — never its own digest auth.

One login try, the hikisapi rule: a snapshot that comes back 401 with our
credentials marks the key + login refused (hikisapi.remember), so a wrong
password is never sent to the device twice.

ONVIF responses are read with hikisapi.parse, which refuses DOCTYPE/ENTITY — a
camera on a client LAN is not trusted, and this endpoint answers unauthenticated.
"""
import base64
import hashlib
import secrets
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

import creds  # noqa: F401 - kept for symmetry with hikisapi; fingerprinting lives there
import hikisapi

TIMEOUT = (4, 12)
MAX_BYTES = 8 << 20

ONVIF_DEVICE_PATH = "/onvif/device_service"

_WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
_WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
_PWDIGEST = ("http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-"
             "profile-1.0#PasswordDigest")
_B64ENC = ("http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-"
           "security-1.0#Base64Binary")


# ---- the snapshot GET ---------------------------------------------------------
def get(key, url, username, password, registry=None, timeout=TIMEOUT, max_bytes=MAX_BYTES):
    """Fetch the JPEG at `url` with the saved login, as a hikisapi.Reply. Raises
    hikisapi.LoginRefused / Unreachable / TooBig exactly as Client does."""
    username, password = username or "", password or ""
    if hikisapi.refused(key, username, password, registry):
        raise hikisapi.LoginRefused("this device refused the saved login before; fix the login first")
    session = _session()
    try:
        status, content, headers, sent_auth = _request(
            session, "GET", url, username, password, max_bytes=max_bytes, timeout=timeout)
    except requests.RequestException as e:
        raise hikisapi.Unreachable(e.__class__.__name__) from None
    finally:
        session.close()
    if status == 401:
        if sent_auth:
            hikisapi.remember(key, username, password, False)
            raise hikisapi.LoginRefused("the device refused the saved login")
        raise hikisapi.LoginRefused("the device asks for a login this client can't give")
    if sent_auth:
        hikisapi.remember(key, username, password, True)
    return hikisapi.Reply(status, content, headers)


# ---- ONVIF snapshot discovery -------------------------------------------------
def snapshot_uri(ip, username, password, ports=None, registry=None, key=None):
    """The absolute snapshot URL an ONVIF camera reports (GetProfiles ->
    GetSnapshotUri), or None. Best-effort: any failure means 'use an override or
    give up'. A 401 after we sent the login marks the login refused, so discovery
    never hammers a wrong password (hikisapi's one-try rule)."""
    username, password = username or "", password or ""
    if key and hikisapi.refused(key, username, password, registry):
        return None
    base = _base(ip, ports)
    session = _session()
    refused = False

    def soap(url, inner):
        nonlocal refused
        env = _envelope(inner, username, password).encode("utf-8")
        status, content, _h, sent = _request(
            session, "POST", url, username, password, data=env,
            headers={"Content-Type": "application/soap+xml; charset=utf-8"}, max_bytes=1 << 20)
        if status == 401 and sent:
            refused = True
        return hikisapi.parse(content) if status == 200 else None

    try:
        dev = base + ONVIF_DEVICE_PATH
        caps = soap(dev, "<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>")
        media = hikisapi.find(caps, "Media")
        xaddr = hikisapi.text(media, "XAddr") if media is not None else ""
        media_url = xaddr if xaddr.startswith(("http://", "https://")) else dev
        profiles = soap(media_url, "<trt:GetProfiles/>")
        token = _profile_token(profiles)
        if not token:
            return None
        got = soap(media_url, "<trt:GetSnapshotUri><trt:ProfileToken>"
                   f"{_xml_escape(token)}</trt:ProfileToken></trt:GetSnapshotUri>")
        uri = hikisapi.text(got, "Uri")
        return uri or None
    except requests.RequestException:
        return None
    finally:
        session.close()
        if refused and key:
            hikisapi.remember(key, username, password, False)


def _profile_token(profiles):
    """The token of the first media profile — an attribute on <trt:Profiles>."""
    if profiles is None:
        return ""
    for el in profiles.iter():
        if hikisapi.tag(el) == "Profiles" and el.get("token"):
            return el.get("token")
    return ""


# ---- HTTP with hand-rolled Basic / Digest -------------------------------------
def _session():
    s = requests.Session()
    s.verify = False
    return s


def _request(session, method, url, username, password, data=None, headers=None,
             max_bytes=MAX_BYTES, timeout=TIMEOUT):
    """One request, answering a 401 with Basic or Digest built from the challenge.
    Returns (status, body, headers, sent_auth) — sent_auth True when the login was
    actually sent (a 401 then means the device refused it)."""
    r = session.request(method, url, data=data, headers=headers, timeout=timeout,
                        stream=True, verify=False)
    if r.status_code != 401:
        return r.status_code, _read(r, max_bytes), r.headers, False
    chal = _parse_challenge(r.headers.get("WWW-Authenticate", ""))
    r.close()
    if not chal:
        return 401, b"", {}, False
    h = dict(headers or {})
    h["Authorization"] = _auth_header(method, url, username, password, chal)
    r2 = session.request(method, url, data=data, headers=h, timeout=timeout,
                         stream=True, verify=False)
    return r2.status_code, _read(r2, max_bytes), r2.headers, True


def _read(r, max_bytes):
    chunks, total = [], 0
    try:
        for c in r.iter_content(65536):
            total += len(c)
            if total > max_bytes:
                raise hikisapi.TooBig(f"answer larger than {max_bytes // 1024} KB")
            chunks.append(c)
    finally:
        r.close()
    return b"".join(chunks)


def _parse_challenge(header):
    """The WWW-Authenticate challenge as {_scheme, realm, nonce, qop, ...}, Digest
    preferred over Basic, or None when there is no scheme we can answer."""
    h = (header or "").strip()
    low = h.lower()
    if "digest" in low:
        params = requests.utils.parse_dict_header(h[low.index("digest") + 6:].strip())
        params["_scheme"] = "digest"
        return params
    if low.startswith("basic"):
        return {"_scheme": "basic"}
    return None


def _auth_header(method, url, username, password, chal):
    if chal["_scheme"] == "basic":
        return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
    return _digest_header(method, url, username, password, chal)


def _digest_header(method, url, username, password, chal):
    u = urlsplit(url)
    uri = (u.path or "/") + (("?" + u.query) if u.query else "")
    realm = chal.get("realm", "")
    nonce = chal.get("nonce", "")
    opaque = chal.get("opaque")
    algorithm = chal.get("algorithm") or "MD5"
    qops = [q.strip() for q in (chal.get("qop") or "").split(",") if q.strip()]

    def md5(s):
        return hashlib.md5(s.encode("utf-8")).hexdigest()

    cnonce = secrets.token_hex(8)
    nc = "00000001"
    ha1 = md5(f"{username}:{realm}:{password}")
    if algorithm.lower() == "md5-sess":
        ha1 = md5(f"{ha1}:{nonce}:{cnonce}")
    ha2 = md5(f"{method}:{uri}")
    use_qop = "auth" in qops
    if use_qop:
        response = md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
    else:
        response = md5(f"{ha1}:{nonce}:{ha2}")              # RFC 2069
    parts = [f'username="{username}"', f'realm="{realm}"', f'nonce="{nonce}"',
             f'uri="{uri}"', f'response="{response}"', f"algorithm={algorithm}"]
    if use_qop:
        parts += ["qop=auth", f"nc={nc}", f'cnonce="{cnonce}"']
    if opaque is not None:
        parts.append(f'opaque="{opaque}"')
    return "Digest " + ", ".join(parts)


# ---- ONVIF SOAP helpers -------------------------------------------------------
def _base(ip, ports):
    ports = set(ports or ())
    scheme = "https" if (443 in ports and 80 not in ports) else "http"
    return f"{scheme}://{ip}"


def _envelope(inner, username, password):
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
            ' xmlns:tds="http://www.onvif.org/ver10/device/wsdl"'
            ' xmlns:trt="http://www.onvif.org/ver10/media/wsdl"'
            ' xmlns:tt="http://www.onvif.org/ver10/schema">'
            + _security_header(username, password)
            + f"<s:Body>{inner}</s:Body></s:Envelope>")


def _security_header(username, password):
    """WS-Security UsernameToken (PasswordDigest) — the ONVIF way to prove a login
    in the SOAP body. Cameras that want HTTP Digest instead answer 401, and
    _request then retries with the Digest header; carrying both costs nothing."""
    if not username:
        return ""
    nonce = secrets.token_bytes(16)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pw = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
    return (f'<s:Header><wsse:Security xmlns:wsse="{_WSSE}" xmlns:wsu="{_WSU}">'
            '<wsse:UsernameToken>'
            f'<wsse:Username>{_xml_escape(username)}</wsse:Username>'
            f'<wsse:Password Type="{_PWDIGEST}">{pw}</wsse:Password>'
            f'<wsse:Nonce EncodingType="{_B64ENC}">{base64.b64encode(nonce).decode()}</wsse:Nonce>'
            f'<wsu:Created>{created}</wsu:Created>'
            '</wsse:UsernameToken></wsse:Security></s:Header>')


def _xml_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))
