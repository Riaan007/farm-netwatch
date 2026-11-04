"""The RTSP one-frame grab (app/camrtsp.py): ffmpeg decode of one frame to JPEG,
the login URL-encoded into the stream URL, feature-detection, and the
one-login-try rule. ffmpeg is stubbed — the tests never spawn it — so they run
anywhere with --network none.

Run inside the site image (nothing leaves the box):
  docker run --rm --network none -v "$PWD/app:/app:ro" -v "$PWD/tests:/tests:ro" -e NETWATCH_DATA=/tmp/nw \\
    -e PYTHONDONTWRITEBYTECODE=1 --entrypoint python farm-netwatch:netcfg /tests/test_camrtsp.py -v
"""
import os
import sys
import tempfile
import unittest

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import camrtsp     # noqa: E402
import hikisapi    # noqa: E402
from hik_fake import jpeg   # noqa: E402


class _Done:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class Grab(unittest.TestCase):
    def setUp(self):
        hikisapi._seen.clear()
        self._run = camrtsp.subprocess.run
        self._which = camrtsp.shutil.which
        camrtsp.shutil.which = lambda name: "/usr/bin/" + name      # pretend ffmpeg is installed
        self.calls = []

    def tearDown(self):
        camrtsp.subprocess.run = self._run
        camrtsp.shutil.which = self._which

    def stub(self, **kw):
        def run(cmd, *a, **k):
            self.calls.append(cmd)
            return _Done(**kw)
        camrtsp.subprocess.run = run

    def test_a_frame_comes_back_as_jpeg(self):
        self.stub(returncode=0, stdout=jpeg(2048, 2048, pad=4000))
        r = camrtsp.grab("c1", "rtsp://1.2.3.4/h264", "Admin", "pw")
        self.assertEqual(r.status, 200)
        self.assertTrue(r.content.startswith(b"\xff\xd8"))
        self.assertIn("-rtsp_transport", self.calls[0])
        self.assertIn("tcp", self.calls[0])

    def test_the_login_is_url_encoded_into_the_rtsp_url(self):
        self.stub(returncode=0, stdout=jpeg(100, 100, pad=10))
        camrtsp.grab("c1", "rtsp://host/h264", "Admin", "p@ss word")
        cmd = self.calls[0]
        self.assertEqual(cmd[cmd.index("-i") + 1], "rtsp://Admin:p%40ss%20word@host/h264")

    def test_a_url_that_already_has_a_login_is_left_alone(self):
        self.assertEqual(camrtsp._with_creds("rtsp://u:p@host/h264", "Admin", "pw"),
                         "rtsp://u:p@host/h264")

    def test_an_rtsp_401_marks_the_login_refused_and_is_not_retried(self):
        self.stub(returncode=1, stderr=b"[rtsp] method DESCRIBE failed: 401 Unauthorized")
        with self.assertRaises(hikisapi.LoginRefused):
            camrtsp.grab("c1", "rtsp://h/h264", "Admin", "wrong")
        self.assertTrue(hikisapi.refused("c1", "Admin", "wrong"))
        n = len(self.calls)
        for _ in range(3):
            with self.assertRaises(hikisapi.LoginRefused):
                camrtsp.grab("c1", "rtsp://h/h264", "Admin", "wrong")
        self.assertEqual(len(self.calls), n, "ffmpeg ran again for a refused login")

    def test_no_frame_is_unreachable(self):
        self.stub(returncode=1, stderr=b"Connection refused")
        with self.assertRaises(hikisapi.Unreachable):
            camrtsp.grab("c1", "rtsp://h/h264", "Admin", "pw")

    def test_without_ffmpeg_the_route_is_unavailable(self):
        camrtsp.shutil.which = lambda name: None
        self.assertFalse(camrtsp.available())
        with self.assertRaises(hikisapi.Unreachable):
            camrtsp.grab("c1", "rtsp://h/h264", "Admin", "pw")


if __name__ == "__main__":
    unittest.main()
