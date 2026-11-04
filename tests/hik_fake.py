"""A fake Hikvision camera / NVR for the camera tests (test_hikisapi, test_campics).

HTTP Digest exactly as Hikvision asks for it (RFC 2617, MD5, qop=auth — what
`requests` answers), or Basic; a few ISAPI documents; and "pictures" that are
the smallest JPEG a parser accepts (SOI + a frame header with the size + EOI).
Every request is counted, so a test can prove a refused login is never tried again.

Behaviour switches (attributes on the Device): `sizes` = the picture sizes it
honours; size_mode "list" (honour those, ignore others: the full picture comes
back), "503" (V5.4 firmware: any size parameter -> 503), "403" (V5.7.13: any
size -> 403) or "ignore"; sub_picture=True makes channel 102's picture the
sub-stream size (V5.7.13) instead of the full one (V5.5); `channels` makes it an NVR.
"""
import base64
import hashlib
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

REALM = "IP Camera(Test)"


def jpeg(w, h, pad=0):
    """A minimal JPEG of size w x h, padded with comment segments to `pad` bytes."""
    sof = bytes([0xFF, 0xC0, 0x00, 0x11, 0x08]) + h.to_bytes(2, "big") + w.to_bytes(2, "big") \
        + bytes([3, 1, 0x22, 0, 2, 0x11, 1, 3, 0x11, 1])
    com, left = [], pad
    while left > 0:
        n = min(left, 65000)
        com.append(bytes([0xFF, 0xFE]) + (n + 2).to_bytes(2, "big") + b"\0" * n)
        left -= n
    return b"\xff\xd8" + b"".join(com) + sof + b"\xff\xd9"


def _md5(s):
    return hashlib.md5(s.encode()).hexdigest()


def _digest_params(h):
    out = {}
    for part in h[len("Digest "):].split(","):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            out[k.strip()] = v.strip().strip('"')
    return out


class Device:
    def __init__(self, user="admin", password="pw", auth="digest", main=(2688, 1520), sub=(640, 360),
                 sizes=((640, 360),), size_mode="list", channels=None, sub_picture=False, inputs=1):
        self.user, self.password, self.auth = user, password, auth
        self.main, self.sub, self.sizes, self.size_mode = main, sub, set(sizes), size_mode
        self.sub_picture = sub_picture
        self.inputs = inputs                 # video inputs (lenses); lens 2+ sends a 384x288 (thermal-sized) picture
        # health (hikhealth): disks [(status, capacity MB)], clock, password flags,
        # oldest recording per channel (epoch; 0 = none), channel numbers + offset (old NVRs: 33..)
        self.disks = [("ok", 5723166)]
        self.tz, self.clock_offset, self.local_offset = "CST-2:00:00", 0, "+02:00"
        self.pw_default = self.pw_risky = False
        self.oldest = {}
        self.search_delay = 0                # seconds a footage search takes (old NVRs: long)
        self.searches = 0
        self.chan_base = 0
        self.channels = channels             # [{id, name, ip, online}] -> an NVR
        self.picture_body = None             # bytes to send instead of a JPEG (tests a bad answer)
        self.hits = []                       # (method, path, sent a login?)
        self.nonce = secrets.token_hex(8)
        self.srv = None

    # ---- server ----
    def start(self):
        dev = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                dev._handle(self)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self.body = self.rfile.read(n) if n else b""
                dev._handle(self)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self

    def stop(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()

    @property
    def addr(self):
        return f"127.0.0.1:{self.srv.server_port}"

    def logins(self):
        """How many requests carried a login."""
        return sum(1 for h in self.hits if h[2])

    # ---- one request ----
    def _authorised(self, req):
        h = req.headers.get("Authorization", "")
        if self.auth == "basic":
            if not h.startswith("Basic "):
                return False
            try:
                u, p = base64.b64decode(h[6:]).decode().split(":", 1)
            except ValueError:
                return False
            return u == self.user and p == self.password
        if not h.startswith("Digest "):
            return False
        p = _digest_params(h)
        if p.get("username") != self.user or p.get("nonce") != self.nonce:
            return False
        ha1 = _md5(f"{self.user}:{REALM}:{self.password}")
        ha2 = _md5(f"{req.command}:{p.get('uri', '')}")
        want = _md5(f"{ha1}:{self.nonce}:{p.get('nc', '')}:{p.get('cnonce', '')}:{p.get('qop', '')}:{ha2}")
        return p.get("response") == want

    def _send(self, req, code, body, ctype="application/xml", extra=None):
        data = body if isinstance(body, bytes) else body.encode()
        req.send_response(code)
        req.send_header("Content-Type", ctype)
        req.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            req.send_header(k, v)
        req.end_headers()
        req.wfile.write(data)

    def _health(self, req, path, q):
        import json as _json
        import re as _re
        import time as _time
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        if path == "/ISAPI/System/time":
            off = int(self.local_offset[:3]) * 3600
            local = _dt.now(_tz.utc) + _td(seconds=off + self.clock_offset)
            return self._send(req, 200, f"<Time><timeMode>NTP</timeMode><localTime>{local.strftime('%Y-%m-%dT%H:%M:%S')}{self.local_offset}</localTime><timeZone>{self.tz}</timeZone></Time>")
        if path == "/ISAPI/Security/userCheck":
            return self._send(req, 200, f"<userCheck><statusValue>200</statusValue><isDefaultPassword>{str(self.pw_default).lower()}</isDefaultPassword><isRiskPassword>{str(self.pw_risky).lower()}</isRiskPassword></userCheck>")
        if path == "/ISAPI/ContentMgmt/Storage":
            items = "".join(f"<hdd><id>{i}</id><hddName>hdd{i}</hddName><status>{st}</status><capacity>{cap}</capacity><freeSpace>0</freeSpace></hdd>"
                            for i, (st, cap) in enumerate(self.disks, 1))
            return self._send(req, 200, f'<storage xmlns="http://www.hikvision.com/ver20/XMLSchema"><hddList>{items}</hddList></storage>')
        if path == "/ISAPI/System/workingstatus" and self.channels is not None:
            rows = [{"chanNo": int(c["id"]) + self.chan_base, "online": 1 if c.get("online", True) else 0,
                     "record": 1 if c.get("record", True) and c.get("online", True) else 0, "bitRate": 4194304} for c in self.channels]
            return self._send(req, 200, _json.dumps({"WorkingStatus": {"devStatus": 0, "ChanStatus": rows}}), "application/json")
        if path == "/ISAPI/ContentMgmt/search" and req.command == "POST":
            body = getattr(req, "body", b"").decode()
            if "isapi.org" not in body or not _re.search(r"<searchID>[0-9A-F-]{36}</searchID>", body):
                return self._send(req, 400, "<ResponseStatus><subStatusCode>badXmlContent</subStatusCode></ResponseStatus>")
            ch = _re.search(r"<trackID>(\d+)01</trackID>", body).group(1)
            self.searches += 1
            if self.search_delay:
                _time.sleep(self.search_delay)
            t = self.oldest.get(ch, 0)
            if not t:
                return self._send(req, 200, "<CMSearchResult><responseStatusStrg>NO MATCHES</responseStatusStrg><numOfMatches>0</numOfMatches></CMSearchResult>")
            st = _dt.fromtimestamp(t, _tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return self._send(req, 200, f"<CMSearchResult><responseStatusStrg>MORE</responseStatusStrg><numOfMatches>1</numOfMatches><matchList><searchMatchItem><timeSpan><startTime>{st}</startTime><endTime>{st}</endTime></timeSpan></searchMatchItem></matchList></CMSearchResult>")
        return None

    def _handle(self, req):
        u = urlparse(req.path)
        self.hits.append((req.command, req.path, bool(req.headers.get("Authorization"))))
        if not self._authorised(req):
            chal = (f'Basic realm="{REALM}"' if self.auth == "basic"
                    else f'Digest realm="{REALM}", qop="auth", nonce="{self.nonce}", opaque="0"')
            return self._send(req, 401, "<userCheck><statusValue>401</statusValue></userCheck>",
                              extra={"WWW-Authenticate": chal})
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = u.path
        hit = self._health(req, path, q)
        if hit is not None:
            return hit
        if path in ("/ISAPI/Streaming/channels/101", "/ISAPI/Streaming/channels/102") and not self.channels:
            w, h = self.main if path.endswith("101") else self.sub
            return self._send(req, 200, f'<StreamingChannel xmlns="http://www.hikvision.com/ver20/XMLSchema"><Video>'
                                        f'<videoResolutionWidth>{w}</videoResolutionWidth>'
                                        f'<videoResolutionHeight>{h}</videoResolutionHeight></Video></StreamingChannel>')
        if path == "/ISAPI/System/Video/inputs/channels" and not self.channels:
            items = "".join(f"<VideoInputChannel><id>{i}</id></VideoInputChannel>" for i in range(1, self.inputs + 1))
            return self._send(req, 200, f'<VideoInputChannelList xmlns="http://www.hikvision.com/ver20/XMLSchema">{items}</VideoInputChannelList>')
        for n in range(2, self.inputs + 1):
            if path in (f"/ISAPI/Streaming/channels/{n}02/picture", f"/ISAPI/Streaming/channels/{n}01/picture") and not self.channels:
                return self._send(req, 200, jpeg(384, 288, pad=15000), "image/jpeg")
        if path == "/ISAPI/Streaming/channels/101/picture" and not self.channels:
            if self.picture_body is not None:
                return self._send(req, 200, self.picture_body, "text/html")
            if "videoResolutionWidth" in q:
                wh = (int(q["videoResolutionWidth"]), int(q.get("videoResolutionHeight", 0)))
                if self.size_mode in ("503", "403"):
                    return self._send(req, int(self.size_mode), "<ResponseStatus><statusCode>3</statusCode></ResponseStatus>")
                if self.size_mode == "list" and wh in self.sizes:
                    return self._send(req, 200, jpeg(*wh, pad=20000), "image/jpeg")
            return self._send(req, 200, jpeg(*self.main, pad=900000), "image/jpeg")
        if path == "/ISAPI/Streaming/channels/102/picture" and not self.channels:
            if self.picture_body is not None:
                return self._send(req, 200, self.picture_body, "text/html")
            if self.sub_picture:
                return self._send(req, 200, jpeg(*self.sub, pad=25000), "image/jpeg")
            return self._send(req, 200, jpeg(*self.main, pad=900000), "image/jpeg")
        if self.channels is not None:
            if path == "/ISAPI/ContentMgmt/InputProxy/channels":
                items = "".join(
                    f"<InputProxyChannel><id>{c['id']}</id><name>{c.get('name', '')}</name><sourceInputPortDescriptor>"
                    f"<ipAddress>{c['ip']}</ipAddress><model>{c.get('model', '')}</model></sourceInputPortDescriptor>"
                    f"</InputProxyChannel>" for c in self.channels)
                return self._send(req, 200, f'<InputProxyChannelList xmlns="http://www.hikvision.com/ver20/XMLSchema">{items}</InputProxyChannelList>')
            if path == "/ISAPI/ContentMgmt/InputProxy/channels/status":
                items = "".join(f"<InputProxyChannelStatus><id>{c['id']}</id><online>{str(c.get('online', True)).lower()}</online>"
                                f"</InputProxyChannelStatus>" for c in self.channels)
                return self._send(req, 200, f"<InputProxyChannelStatusList>{items}</InputProxyChannelStatusList>")
            for c in self.channels:
                if path == f"/ISAPI/Streaming/channels/{c['id']}02/picture" and c.get("no_sub"):
                    return self._send(req, 400, "<ResponseStatus><statusCode>4</statusCode></ResponseStatus>")
                if path == f"/ISAPI/Streaming/channels/{c['id']}02/picture":
                    return self._send(req, 200, jpeg(704, 576, pad=40000), "image/jpeg")
                if path == f"/ISAPI/Streaming/channels/{c['id']}01/picture":
                    wh = (1920, 1088) if "videoResolutionWidth" in q else (704, 576)
                    return self._send(req, 200, jpeg(*wh, pad=180000), "image/jpeg")
        return self._send(req, 404, "<ResponseStatus><statusCode>4</statusCode></ResponseStatus>")
