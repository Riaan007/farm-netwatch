"""A stand-in for go2rtc in tests (app/camlive.py): `-config <file or json>` with
api.unix_listen (a Unix socket, as Netwatch runs it) or api.listen;
PUT/DELETE /api/streams, GET /api/streams (lists sources WITH their logins, like the
real one), GET /api/stream.mp4?src= sends MP4-ish bytes until the reader goes. Like
go2rtc 1.9.4, a PUT adds the stream and then tries to save it to the config file:
when that fails it answers 400 although the stream is there."""
import json
import os
import socketserver
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

arg = sys.argv[sys.argv.index("-config") + 1]
CFG_PATH = arg if os.path.isfile(arg) else ""
cfg = json.load(open(arg)) if CFG_PATH else json.loads(arg)
SOCK = cfg["api"].get("unix_listen") or ""
AUTH = None
streams = {}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ok(self):
        if AUTH and self.headers.get("Authorization") != AUTH:
            self._send(401, b"Unauthorized", "text/plain")
            return False
        return True

    def do_GET(self):
        if not self._ok():
            return None
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/api":
            return self._send(200, b"{}")
        if u.path == "/api/streams":
            return self._send(200, json.dumps(streams).encode())
        if u.path == "/api/stream.mp4":
            if q.get("src") not in streams:
                return self._send(404, b"stream not found", "text/plain")
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.end_headers()
            try:
                self.wfile.write(b"\x00\x00\x00\x18ftypiso5" + b"\x00" * 12)
                while q["src"] in streams:
                    self.wfile.write(b"\x00\x00\x10\x00moof" + b"\x01" * 4088)
                    self.wfile.flush()
                    time.sleep(0.02)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return None
        return self._send(404)

    def do_PUT(self):
        if not self._ok():
            return None
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/api/streams" and q.get("name") and q.get("src"):
            streams[q["name"]] = q["src"]
            if not CFG_PATH:
                return self._send(400, b"config file disabled", "text/plain")
            try:
                with open(CFG_PATH, "a") as f:          # the real one saves the stream into its config
                    f.write(json.dumps(streams))
            except OSError:
                return self._send(400, b"open config: permission denied", "text/plain")
            return self._send(200)
        return self._send(400)

    def do_DELETE(self):
        if not self._ok():
            return None
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        streams.pop(q.get("src"), None)
        return self._send(200)


class UnixHTTP(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


if SOCK:
    H.address_string = lambda self: "unix"
    UnixHTTP(SOCK, H).serve_forever()
else:
    host, port = cfg["api"]["listen"].rsplit(":", 1)
    ThreadingHTTPServer((host, int(port)), H).serve_forever()
