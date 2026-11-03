"""The ISAPI client's promise (app/hikisapi.py): a login a device refused is never
sent to that device again. Hikvision locks the account after a few wrong
passwords (7 on the home cameras), and Netwatch talks to cameras on its own.

Run inside the site image (nothing leaves the box):
  docker run --rm --network none -v "$PWD/app:/app:ro" -v "$PWD/tests:/tests:ro" -e NETWATCH_DATA=/tmp/nw \\
    -e PYTHONDONTWRITEBYTECODE=1 --entrypoint python farm-netwatch:netcfg /tests/test_hikisapi.py -v
"""
import os
import sys
import tempfile
import time
import unittest

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import creds      # noqa: E402
import hikisapi   # noqa: E402
from hik_fake import Device   # noqa: E402


class Client(unittest.TestCase):
    def setUp(self):
        self.dev = Device().start()
        hikisapi._seen.clear()

    def tearDown(self):
        self.dev.stop()

    def client(self, password="pw", key="cam1", registry=None, ports=None):
        return hikisapi.Client(key, self.dev.addr, "admin", password, ports=ports, registry=registry)

    def test_a_good_login_answers(self):
        r = self.client().get("/ISAPI/Streaming/channels/101")
        self.assertEqual(r.status, 200)
        self.assertEqual(hikisapi.text(hikisapi.parse(r.content), "videoResolutionWidth"), "2688")

    def test_a_wrong_password_is_sent_once_and_never_again(self):
        with self.assertRaises(hikisapi.LoginRefused):
            self.client("wrong").get("/ISAPI/Streaming/channels/101")
        self.assertEqual(self.dev.logins(), 1)
        seen = len(self.dev.hits)
        for _ in range(3):                          # later polls, new clients, other paths
            with self.assertRaises(hikisapi.LoginRefused):
                self.client("wrong").get("/ISAPI/Streaming/channels/101/picture")
        self.assertEqual(len(self.dev.hits), seen, "a refused login reached the device again")
        self.assertTrue(hikisapi.refused("cam1", "admin", "wrong"))

    def test_saving_a_different_login_allows_one_new_try(self):
        with self.assertRaises(hikisapi.LoginRefused):
            self.client("wrong").get("/ISAPI/Streaming/channels/101")
        self.assertEqual(self.client("pw").get("/ISAPI/Streaming/channels/101").status, 200)
        self.assertFalse(hikisapi.refused("cam1", "admin", "pw"))

    def test_a_failed_login_test_counts_as_refused(self):
        reg = {"cam1": {"cred_test": {"result": "auth_failed", "fp": creds.fingerprint("admin", "pw"), "ts": time.time()}}}
        with self.assertRaises(hikisapi.LoginRefused):
            self.client("pw", registry=reg).get("/ISAPI/Streaming/channels/101")
        self.assertEqual(self.dev.hits, [], "the login was tried although the login test had failed")

    def test_a_newer_passing_login_test_lifts_the_refusal(self):
        with self.assertRaises(hikisapi.LoginRefused):
            self.client("wrong").get("/ISAPI/Streaming/channels/101")
        reg = {"cam1": {"cred_test": {"result": "ok", "fp": creds.fingerprint("admin", "wrong"), "ts": time.time() + 1}}}
        self.assertFalse(hikisapi.refused("cam1", "admin", "wrong", reg))

    def test_a_login_test_for_another_password_does_not_count(self):
        reg = {"cam1": {"cred_test": {"result": "auth_failed", "fp": creds.fingerprint("admin", "old"), "ts": time.time()}}}
        self.assertEqual(self.client("pw", registry=reg).get("/ISAPI/Streaming/channels/101").status, 200)

    def test_basic_only_device_gets_one_try_too(self):
        self.dev.auth = "basic"
        self.assertEqual(self.client("pw").get("/ISAPI/Streaming/channels/101").status, 200)
        hikisapi._seen.clear()
        self.dev.hits.clear()
        with self.assertRaises(hikisapi.LoginRefused):
            self.client("wrong", key="cam2").get("/ISAPI/Streaming/channels/101")
        self.assertEqual(self.dev.logins(), 1)
        with self.assertRaises(hikisapi.LoginRefused):
            self.client("wrong", key="cam2").get("/ISAPI/Streaming/channels/101")
        self.assertEqual(self.dev.logins(), 1)

    def test_an_answer_over_the_cap_is_refused(self):
        with self.assertRaises(hikisapi.TooBig):
            self.client().get("/ISAPI/Streaming/channels/101/picture", max_bytes=100 << 10)

    def test_no_device_is_unreachable(self):
        port = self.dev.srv.server_port
        self.dev.stop()
        with self.assertRaises(hikisapi.Unreachable):
            hikisapi.Client("cam1", f"127.0.0.1:{port}", "admin", "pw", timeout=(1, 1)).get("/ISAPI/System/deviceInfo")


class Xml(unittest.TestCase):
    def test_doctype_and_entities_are_refused(self):
        self.assertIsNone(hikisapi.parse('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>'))
        self.assertIsNone(hikisapi.parse('<x><!ENTITY a SYSTEM "file:///etc/passwd"></x>'))

    def test_namespaced_lookups(self):
        root = hikisapi.parse(b'<A xmlns="urn:x"><B><c> 7 </c></B><c>8</c></A>')
        self.assertEqual(hikisapi.text(root, "c"), "7")
        self.assertEqual(hikisapi.child_text(root, "c"), "8")
        self.assertIsNone(hikisapi.parse(b"not xml"))


if __name__ == "__main__":
    unittest.main()
