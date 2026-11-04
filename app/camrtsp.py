"""One still frame from a camera's RTSP stream, via ffmpeg.

Some cameras are ONVIF but serve no working HTTP snapshot — the Dynacolor fisheye
advertises an ONVIF GetSnapshotUri (a CGI) that answers 404. Their only still
path is the H.264 RTSP stream, so this module asks ffmpeg to decode one frame and
encode it as JPEG. campics falls back to this route when the HTTP snapshot fails.

ffmpeg is optional in the image (Dockerfile INSTALL_FFMPEG=1 — the pulled GHCR
images stay lean); available() says whether this route can run at all, and
campics only offers it when it can.

The RTSP login rides inside the URL (user:pass@host) — ffmpeg has no other way to
carry it — so it stays in this process and never reaches a logged command line
(the URL is built here, passwords URL-encoded). A DESCRIBE the camera refuses
(RTSP 401) marks the key + login refused (hikisapi.remember), the one-try rule
camsnap and hikisapi share.
"""
import os
import shutil
import subprocess
from urllib.parse import quote, urlsplit, urlunsplit

import hikisapi

FFMPEG = os.environ.get("NETWATCH_FFMPEG", "ffmpeg")
TIMEOUT = 20
MAX_BYTES = 8 << 20


def available():
    """True when ffmpeg is on PATH — the image was built with INSTALL_FFMPEG=1."""
    return bool(shutil.which(FFMPEG))


def grab(key, rtsp_url, username, password, registry=None, timeout=TIMEOUT, max_bytes=MAX_BYTES):
    """One frame of the RTSP stream as a hikisapi.Reply (JPEG). Raises
    hikisapi.LoginRefused / Unreachable / TooBig, like camsnap.get."""
    username, password = username or "", password or ""
    if hikisapi.refused(key, username, password, registry):
        raise hikisapi.LoginRefused("this device refused the saved login before; fix the login first")
    if not available():
        raise hikisapi.Unreachable("ffmpeg is not installed in this image (build with INSTALL_FFMPEG=1)")
    # No ffmpeg-level socket timeout option (their names vary by version — rtsp uses
    # `timeout`/`stimeout`, and a wrong one is fatal "Option not found"); the
    # subprocess timeout below is the hard bound instead.
    cmd = [FFMPEG, "-nostdin", "-loglevel", "error", "-rtsp_transport", "tcp",
           "-i", _with_creds(rtsp_url, username, password),
           "-an", "-frames:v", "1", "-q:v", "3", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
    except subprocess.TimeoutExpired:
        raise hikisapi.Unreachable("ffmpeg timed out reading the RTSP stream") from None
    except OSError as e:
        raise hikisapi.Unreachable("ffmpeg could not run (%s)" % e.__class__.__name__) from None
    out = p.stdout or b""
    err = (p.stderr or b"").decode("utf-8", "replace")
    if p.returncode == 0 and out[:2] == b"\xff\xd8":
        if len(out) > max_bytes:
            raise hikisapi.TooBig("frame larger than %d KB" % (max_bytes // 1024))
        hikisapi.remember(key, username, password, True)
        return hikisapi.Reply(200, out, {})
    if _auth_failed(err):
        hikisapi.remember(key, username, password, False)
        raise hikisapi.LoginRefused("the camera refused the RTSP login")
    tail = err.strip().splitlines()[-1] if err.strip() else ("exit %d" % p.returncode)
    raise hikisapi.Unreachable("ffmpeg returned no frame (%s)" % tail[:160])


def _with_creds(url, username, password):
    """rtsp://host/path -> rtsp://user:pass@host/path (login URL-encoded). A URL
    that already carries credentials is left untouched."""
    if not username:
        return url
    parts = urlsplit(url)
    if "@" in parts.netloc:
        return url
    netloc = "%s:%s@%s" % (quote(username, safe=""), quote(password, safe=""), parts.netloc)
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _auth_failed(stderr):
    e = stderr.lower()
    return "401" in e or "unauthorized" in e or "authentication" in e
