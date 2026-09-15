"""Device login test (app/credtest.py): method choice and one-attempt HTTP logic.

Run inside the site image:
  docker run --rm -v "$PWD/app:/app" -v "$PWD/tests:/tests" -e NETWATCH_DATA=/tmp/nw \\
    --entrypoint python farm-netwatch:netcfg /tests/test_credtest.py -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import credtest    # noqa: E402


class Resp:
    def __init__(self, status, www=None, text=""):
        self.status_code, self.text = status, text
        self.headers = {"WWW-Authenticate": www} if www else {}

    def json(self):
        return {"name": "Core-RB"}


class PlanTest(unittest.TestCase):
    def test_methods_follow_the_device(self):
        cam = {"vendor": "Hangzhou Hikvision Digital Technology", "category": "camera"}
        self.assertEqual(credtest.plan(cam, [80, 443]), ["hikvision"])
        self.assertEqual(credtest.plan({"vendor": "Ubiquiti Networks"}, [22]), ["ssh"])
        self.assertEqual(credtest.plan({"vendor": "Routerboard.com"}, [22, 80]), ["ssh", "mikrotik", "web"])
        self.assertEqual(credtest.plan({"vendor": "Ruijie Networks"}, [80]), ["web"])
        self.assertEqual(credtest.plan({"vendor": "Ruijie Networks"}, []), [])


class HttpTest(unittest.TestCase):
    def run_http(self, responses):
        calls = []

        def get(url, **kw):
            calls.append(kw.get("auth"))
            return responses[len(calls) - 1]
        with mock.patch.object(credtest.requests, "get", side_effect=get):
            return credtest._http_once("http://x/", "admin", "pw", 3), calls

    def test_one_unauthenticated_probe_then_one_login(self):
        (res, detail, _), calls = self.run_http([Resp(401, 'Digest realm="x", nonce="1"'), Resp(200)])
        self.assertEqual(res, "ok")
        self.assertIsNone(calls[0])
        self.assertIsInstance(calls[1], credtest.HTTPDigestAuth)
        self.assertEqual(len(calls), 2)

    def test_rejected_login_is_one_attempt(self):
        (res, _, _), calls = self.run_http([Resp(401, 'Basic realm="x"'), Resp(401, 'Basic realm="x"')])
        self.assertEqual(res, "auth_failed")
        self.assertEqual(len(calls), 2)

    def test_form_login_pages_are_not_guessed_at(self):
        (res, _, _), calls = self.run_http([Resp(200)])
        self.assertEqual(res, "no_http_auth")
        self.assertEqual(len(calls), 1)

    def test_hikvision_uses_only_one_port(self):
        with mock.patch.object(credtest, "_http_once", return_value=("auth_failed", "rejected", None)) as h:
            r = credtest.test_hikvision("10.0.0.2", "admin", "bad", [80, 443, 8443])
        self.assertEqual(r["result"], "auth_failed")
        self.assertEqual(h.call_count, 1)
        self.assertIn("locks the account", r["detail"])


class SshTest(unittest.TestCase):
    def ssh(self, rc, err=""):
        with mock.patch.object(credtest.subprocess, "run", return_value=mock.Mock(returncode=rc, stderr=err)):
            return credtest.test_ssh("10.0.0.3", "ubnt", "pw")["result"]

    def test_outcomes(self):
        self.assertEqual(self.ssh(0), "ok")
        self.assertEqual(self.ssh(5), "auth_failed")
        self.assertEqual(self.ssh(255, "user@10.0.0.3: Permission denied (password)."), "auth_failed")
        self.assertEqual(self.ssh(255, "ssh: connect to host 10.0.0.3 port 22: Connection refused"), "unreachable")
        self.assertEqual(self.ssh(1, "exec request failed on channel 0"), "ok")     # switch: logged in, no commands


class FlowTest(unittest.TestCase):
    def test_a_definite_answer_stops_the_test(self):
        dev = {"ip": "10.0.0.4", "vendor": "Routerboard.com"}
        with mock.patch.object(credtest, "open_ports", return_value=[22, 80]), \
                mock.patch.object(credtest, "test_ssh", return_value=credtest._r("auth_failed", "SSH", "no")), \
                mock.patch.object(credtest, "test_mikrotik_rest") as rest:
            r = credtest.test(dev, "admin", "bad")
        self.assertEqual(r["result"], "auth_failed")
        rest.assert_not_called()

    def test_unreachable_ssh_moves_on(self):
        dev = {"ip": "10.0.0.4", "vendor": "Routerboard.com"}
        with mock.patch.object(credtest, "open_ports", return_value=[22, 80]), \
                mock.patch.object(credtest, "test_ssh", return_value=credtest._r("unreachable", "SSH", "reset")), \
                mock.patch.object(credtest, "test_mikrotik_rest", return_value=credtest._r("ok", "RouterOS REST", "yes")):
            r = credtest.test(dev, "admin", "pw")
        self.assertEqual((r["result"], r["method"], len(r["tried"])), ("ok", "RouterOS REST", 2))

    def test_offline_device(self):
        with mock.patch.object(credtest, "open_ports", return_value=[]):
            self.assertEqual(credtest.test({"ip": "10.0.0.9"}, "a", "b")["result"], "unreachable")


if __name__ == "__main__":
    unittest.main()
