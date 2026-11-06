"""Live video from a camera (Phase 7, part 1).

go2rtc (a small static program in the image, /usr/local/bin/go2rtc) takes the camera's
SUB-stream over RTSP and hands it out as MP4 that a browser's <video> plays. It only
repackages; it does not decode, so a Pi 3 copes. Netwatch:

  * starts go2rtc only while someone watches and stops it IDLE_STOP_S after the last
    viewer. It opens NO network port: its API is a Unix socket in a folder only root
    can open (go2rtc 1.9.4 lets local callers skip its own API login, and the API lists
    every stream WITH its camera login), and RTSP/WebRTC/SRTP/RTMP are off;
  * gives it each camera's address and login over that socket when a viewer starts,
    under a random name, and removes it when the viewer stops. Its config is given on
    the command line (no secrets in it) and never as a file: as root, go2rtc writes
    every stream it is given — login included — into its config file, even a
    read-only one (seen 2026-11-06), and with no file it can't;
  * sends the MP4 on through the site's own login (or the hub key), so the hub and the
    browser never see a camera's password;
  * stops each view after MAX_S (5 minutes) and runs at most MAX_VIEWS at once — farm
    links are thin.

Route: the camera's own login ("direct", channel <view>02), else its recorder
("nvr", channel <recorder channel>02). Hikvision only.
"""
import http.client
import json
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from urllib.parse import quote, urlencode

import campics
import creds
import hikisapi

BIN = [os.environ.get("NETWATCH_GO2RTC", "/usr/local/bin/go2rtc")]
MAX_VIEWS = 2
MAX_S = 300
IDLE_STOP_S = 60
CHUNK = 64 << 10
RTSP_PORT = 554


class _Unix(http.client.HTTPConnection):
    """HTTP over go2rtc's Unix socket."""

    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.sock_path = path

    def connect(self):
        sk = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sk.settimeout(self.timeout)
        sk.connect(self.sock_path)
        self.sock = sk


class LiveError(Exception):
    def __init__(self, msg, status=400, extra=None):
        super().__init__(msg)
        self.status = status
        self.extra = extra or {}


def available():
    return bool(BIN) and (len(BIN) > 1 or (os.path.isfile(BIN[0]) and os.access(BIN[0], os.X_OK)))


class Live:
    def __init__(self, engine):
        self.engine = engine
        self.proc = None
        self.cwd = None
        self.views = {}                 # name -> {key, view, who, started}
        self._lk = threading.Lock()
        self._stop_timer = None

    # ---- go2rtc ---------------------------------------------------------------------
    def _sock(self):
        return os.path.join(self.cwd, "api.sock") if self.cwd else ""

    def _call(self, method, path, params=None, timeout=5):
        """(status, body) from go2rtc's API, or (0, b"") when it doesn't answer."""
        if not self._sock():
            return 0, b""
        c = _Unix(self._sock(), timeout)
        try:
            c.request(method, path + ("?" + urlencode(params) if params else ""))
            r = c.getresponse()
            return r.status, r.read(1 << 20)
        except (OSError, http.client.HTTPException):
            return 0, b""
        finally:
            c.close()

    def _up(self):
        return self._call("GET", "/api", timeout=1)[0] == 200

    def _ensure(self):
        """go2rtc running and answering (call with the lock held)."""
        if self._stop_timer:
            self._stop_timer.cancel()
            self._stop_timer = None
        if self.proc and self.proc.poll() is None and self._up():
            return
        self._stop()
        if not available():
            raise LiveError("Live video needs a newer Netwatch on this site (go2rtc is missing).", 501)
        cfg = {"api": {"listen": "", "unix_listen": "{SOCK}"}, "rtsp": {"listen": ""}, "webrtc": {"listen": ""},
               "srtp": {"listen": ""}, "rtmp": {"listen": ""}, "log": {"level": "error", "format": "text"}}
        self.cwd = tempfile.mkdtemp(prefix="nw-go2rtc-")          # 0700: only root can reach the socket
        cfg["api"]["unix_listen"] = self._sock()
        self.proc = subprocess.Popen(BIN + ["-config", json.dumps(cfg)], cwd=self.cwd,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(40):
            if self._up():
                return
            if self.proc.poll() is not None:
                break
            time.sleep(0.1)
        self._stop()
        raise LiveError("the live-video relay did not start on the site Pi", 502)

    def _scrub(self):
        """Nothing it writes may keep a login on the card."""
        if self.cwd and os.path.isdir(self.cwd):
            for n in os.listdir(self.cwd):
                if n == "api.sock":
                    continue
                try:
                    os.remove(os.path.join(self.cwd, n))
                except OSError:
                    pass

    def _stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        if self.cwd:
            shutil.rmtree(self.cwd, ignore_errors=True)
            self.cwd = None

    def _idle_later(self):
        if self._stop_timer:
            self._stop_timer.cancel()

        def stop():
            with self._lk:
                if not self.views:
                    self._stop()
        self._stop_timer = threading.Timer(IDLE_STOP_S, stop)
        self._stop_timer.daemon = True
        self._stop_timer.start()

    # ---- which stream ---------------------------------------------------------------
    def _source(self, key, view):
        """(entry, rtsp url with login, client for codec questions, ISAPI stream id)."""
        st = self.engine._settings()
        if not st["enabled"]:
            raise LiveError("Camera pictures are turned off for this site.", 403)
        e = self.engine._find(key)
        if e is None:
            raise LiveError("unknown camera", 404)
        view = int(view or 1)
        if view != 1 and not 1 <= view <= max(1, len(e.get("views") or [])):
            raise LiveError("this camera has no such view", 404)
        if not e.get("online"):
            raise LiveError("the camera is offline", 409, {"reason": "offline"})
        d = e.get("_d")
        if e.get("_own_ok") and d and campics._hikish(d):
            c, host, sid = creds.get(d["key"]), d.get("ip"), f"{view}02"
            cl = self.engine._client(d)
        elif e.get("_via_ok") and e.get("_nvr"):
            chan = (e.get("_vchans") or {}).get(view) or e.get("_chan") or {}
            ch = str(chan.get("id") or "")
            if not ch.isdigit():
                raise LiveError("the recorder gave this camera no channel number", 409)
            if chan.get("online") is False:
                raise LiveError("the recorder has lost this camera", 409, {"reason": "offline"})
            nvr = e["_nvr"]
            c, host, sid = creds.get(nvr["key"]), nvr.get("ip"), f"{int(ch)}02"
            cl = self.engine._client(nvr)
        else:
            raise LiveError("Live video needs the camera's own login or its recorder's (Hikvision).", 409,
                            {"reason": "no_login"})
        if not host:
            raise LiveError("no address for this camera", 409)
        url = (f"rtsp://{quote(c['username'], safe='')}:{quote(c['password'], safe='')}@{host}:{RTSP_PORT}"
               f"/Streaming/Channels/{sid}")
        return e, url, cl, sid

    def info(self, key, view=1):
        """What a viewer needs to know before starting: the codec of the stream it
        would get (a browser may not play H.265), its size, and how busy the site is."""
        e, _url, cl, sid = self._source(key, view)
        out = {"ok": True, "key": key, "name": e.get("name") or "", "max_s": MAX_S, "max_views": MAX_VIEWS,
               "busy": len(self.views), "available": available(), "codec": "", "size": ""}
        try:
            r = cl.get(f"/ISAPI/Streaming/channels/{sid}", max_bytes=128 << 10)
            root = hikisapi.parse(r.content) if r.ok else None
            if root is not None:
                out["codec"] = hikisapi.text(root, "videoCodecType")
                w, h = hikisapi.text(root, "videoResolutionWidth"), hikisapi.text(root, "videoResolutionHeight")
                out["size"] = f"{w}x{h}" if w and h else ""
        except (hikisapi.Unreachable, hikisapi.LoginRefused, hikisapi.TooBig):
            pass
        return out

    # ---- a view ----------------------------------------------------------------------
    def open(self, key, view=1, who=""):
        """Start one view. Returns (name, iterator of MP4 bytes). The iterator ends after
        MAX_S or when the viewer leaves, and always cleans up."""
        _e, url, _cl, _sid = self._source(key, view)
        # a viewer who left before the first bytes never ran the pump's clean-up
        for n in [n for n, v in list(self.views.items()) if time.time() - v["started"] > MAX_S + 30]:
            self.close(n)
        with self._lk:
            if len(self.views) >= MAX_VIEWS:
                raise LiveError(f"{MAX_VIEWS} live views are already running on this site; try again in a few minutes", 429)
            self._ensure()
            name = "v" + secrets.token_hex(8)
            # go2rtc adds the stream, then tries to save it to its config and answers 400
            # ("config file disabled") — so ask whether it is there
            self._call("PUT", "/api/streams", {"name": name, "src": url})
            st, body = self._call("GET", "/api/streams")
            try:
                added = st == 200 and name in (json.loads(body or b"{}") or {})
            except ValueError:
                added = False
            self._scrub()
            if not added:
                self._delete(name)
                if not self.views:
                    self._idle_later()
                raise LiveError("the live-video relay refused the stream", 502)
            self.views[name] = {"key": key, "view": int(view or 1), "who": who or "", "started": time.time()}
        print(f"[camlive] start {key} view {view} by {who or '?'}", flush=True)
        return name, self._pump(name)

    def _pump(self, name):
        t0, sent = time.time(), 0
        c = _Unix(self._sock(), 20)
        try:
            c.request("GET", "/api/stream.mp4?" + urlencode({"src": name}))
            r = c.getresponse()
            if r.status != 200:
                return
            while time.time() - t0 <= MAX_S:
                chunk = r.read1(CHUNK) if hasattr(r, "read1") else r.read(CHUNK)
                if not chunk:
                    break
                sent += len(chunk)
                yield chunk
        except (OSError, http.client.HTTPException):
            return
        finally:
            c.close()
            self.close(name, sent, time.time() - t0)

    def close(self, name, sent=0, secs=0.0):
        with self._lk:
            v = self.views.pop(name, None)
            if v is None:
                return
            self._delete(name)
            self._scrub()
            if not self.views:
                self._idle_later()
        print(f"[camlive] stop {v['key']} after {int(secs)} s, {sent >> 10} KB", flush=True)

    def _delete(self, name):
        self._call("DELETE", "/api/streams", {"src": name}, timeout=3)

    def status(self):
        with self._lk:
            return {"running": bool(self.proc and self.proc.poll() is None),
                    "views": [{"key": v["key"], "view": v["view"], "who": v["who"], "secs": int(time.time() - v["started"])}
                              for v in self.views.values()]}
