"""The generic snapshot fetch (app/camsnap.py): a plain JPEG from a non-Hikvision
camera's own snapshot URL, answering HTTP Basic or hand-rolled Digest (the old
Dynacolor Appweb that urllib/requests can't digest), the one-login-try rule it
shares with hikisapi, and ONVIF GetSnapshotUri discovery.

Run inside the site image (nothing leaves the box):
  docker run --rm --network none -v "$PWD/app:/app:ro" -v "$PWD/tests:/tests:ro" -e NETWATCH_DATA=/tmp/nw \\
    -e PYTHONDONTWRITEBYTECODE=1 --entrypoint python farm-netwatch:netcfg /tests/test_camsnap.py -v
"""
import os
import sys
import tempfile
import unittest

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import camsnap    # noqa: E402
import hikisapi   # noqa: E402
from hik_fake import GenericCam   # noqa: E402


class Fetch(unittest.TestCase):
    def setUp(self):
        hikisapi._seen.clear()
        self.cam = GenericCam().start()      # Admin / pw, Digest, realm MegapixelIPCamera

    def tearDown(self):
        self.cam.stop()

    def test_digest_returns_the_jpeg(self):
        r = camsnap.get("c1", self.cam.snapshot_url(), "Admin", "pw")
        self.assertEqual(r.status, 200)
        self.assertTrue(r.content.startswith(b"\xff\xd8"))
        self.assertEqual(self.cam.logins(), 1)

    def test_basic_auth_too(self):
        self.cam.auth = "basic"
        r = camsnap.get("c1", self.cam.snapshot_url(), "Admin", "pw")
        self.assertEqual(r.status, 200)
        self.assertTrue(r.content.startswith(b"\xff\xd8"))

    def test_digest_without_qop_rfc2069(self):
        self.cam.qop = False
        r = camsnap.get("c1", self.cam.snapshot_url(), "Admin", "pw")
        self.assertEqual(r.status, 200)

    def test_a_wrong_password_is_sent_once_and_never_again(self):
        with self.assertRaises(hikisapi.LoginRefused):
            camsnap.get("c1", self.cam.snapshot_url(), "Admin", "nope")
        self.assertEqual(self.cam.logins(), 1)
        seen = len(self.cam.hits)
        for _ in range(3):
            with self.assertRaises(hikisapi.LoginRefused):
                camsnap.get("c1", self.cam.snapshot_url(), "Admin", "nope")
        self.assertEqual(len(self.cam.hits), seen, "a refused login reached the camera again")
        self.assertTrue(hikisapi.refused("c1", "Admin", "nope"))

    def test_capital_a_matters(self):
        # The Dynacolor rejects lowercase 'admin'; a different login gets its own try.
        with self.assertRaises(hikisapi.LoginRefused):
            camsnap.get("c1", self.cam.snapshot_url(), "admin", "pw")
        self.assertFalse(hikisapi.refused("c1", "Admin", "pw"))
        self.assertEqual(camsnap.get("c1", self.cam.snapshot_url(), "Admin", "pw").status, 200)

    def test_an_answer_over_the_cap_is_refused(self):
        with self.assertRaises(hikisapi.TooBig):
            camsnap.get("c1", self.cam.snapshot_url(), "Admin", "pw", max_bytes=200)

    def test_no_camera_is_unreachable(self):
        port = self.cam.srv.server_port
        self.cam.stop()
        with self.assertRaises(hikisapi.Unreachable):
            camsnap.get("c1", f"http://127.0.0.1:{port}/cgi-bin/jpg/image.cgi", "Admin", "pw", timeout=(1, 1))


class Onvif(unittest.TestCase):
    def setUp(self):
        hikisapi._seen.clear()
        self.cam = GenericCam().start()

    def tearDown(self):
        self.cam.stop()

    def test_discovers_the_snapshot_uri(self):
        uri = camsnap.snapshot_uri(self.cam.addr, "Admin", "pw", key="c1")
        self.assertEqual(uri, self.cam.snapshot_url())

    def test_the_discovered_url_then_serves_the_picture(self):
        uri = camsnap.snapshot_uri(self.cam.addr, "Admin", "pw", key="c1")
        self.assertEqual(camsnap.get("c1", uri, "Admin", "pw").status, 200)

    def test_discovers_the_rtsp_stream_uri(self):
        uri = camsnap.stream_uri(self.cam.addr, "Admin", "pw", key="c1")
        self.assertEqual(uri, f"rtsp://{self.cam.addr}/h264")

    def test_a_camera_without_onvif_gives_none(self):
        self.cam.onvif = False
        self.assertIsNone(camsnap.snapshot_uri(self.cam.addr, "Admin", "pw", key="c1"))
        self.assertIsNone(camsnap.stream_uri(self.cam.addr, "Admin", "pw", key="c1"))

    def test_a_wrong_login_during_discovery_is_remembered(self):
        self.assertIsNone(camsnap.snapshot_uri(self.cam.addr, "Admin", "nope", key="c1"))
        self.assertTrue(hikisapi.refused("c1", "Admin", "nope"))


if __name__ == "__main__":
    unittest.main()
