"""Hub backup encryption (hub/app/backups.py).

Run inside the hub image with a throwaway data dir:
  docker run --rm -v "$PWD/hub/app:/app:ro" -v "$PWD/tests:/tests:ro" -e HUB_DATA=/tmp/hub \
    --entrypoint python farm-netwatch-hub:local /tests/test_backups.py -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("HUB_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("HUB_APP", "/app"))

import backups  # noqa: E402

BUNDLE = {"kind": "netwatch-backup", "version": 1, "config": {"site": {"name": "Test"}},
          "credentials": {"aa:bb": "c2VjcmV0"}, "secret_key": "S0VZ",
          "hubvpn_conf": "[Interface]\nPrivateKey = abc\n"}
SITE = {"id": "farm-a", "vpn_ip": "10.8.0.9"}


class FakeResp:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return BUNDLE


class Backups(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(backups.DATA_DIR, ignore_errors=True)
        os.makedirs(backups.DATA_DIR)

    def _store(self):
        with mock.patch.object(backups.requests, "get", return_value=FakeResp()), \
                mock.patch.object(backups.siteapi, "headers", return_value={}):
            return backups.store(SITE, (1, 1))

    def test_stored_file_has_no_secrets_and_restores(self):
        entry = self._store()
        raw, _ = backups.read_raw("farm-a", entry["name"])
        for secret in (b"c2VjcmV0", b"S0VZ", b"PrivateKey", b"Test"):
            self.assertNotIn(secret, raw)
        self.assertEqual(json.loads(raw)["kind"], backups.KIND)
        plain, _ = backups.read("farm-a", None)
        self.assertEqual(json.loads(plain), BUNDLE)
        self.assertTrue(backups.list_("farm-a")[0]["encrypted"])
        self.assertEqual(oct(os.stat(backups.KEY_PATH).st_mode & 0o777), "0o600")

    def test_tamper_and_wrong_key_are_refused(self):
        entry = self._store()
        path = os.path.join(backups._dir("farm-a"), entry["name"])
        env = json.loads(open(path, "rb").read())
        data = bytearray(backups.base64.b64decode(env["data"]))
        data[5] ^= 1
        env["data"] = backups.base64.b64encode(bytes(data)).decode()
        with self.assertRaises(backups.BackupError):
            backups.decrypt(json.dumps(env).encode())
        with self.assertRaises(backups.BackupError) as cm:
            backups.decrypt(open(path, "rb").read(), os.urandom(32))
        self.assertIn("different backup key", str(cm.exception))

    def test_existing_plaintext_is_encrypted_in_place(self):
        d = backups._dir("farm-b")
        os.makedirs(d)
        p = os.path.join(d, "20260101-000000.json")
        with open(p, "w") as f:
            json.dump(BUNDLE, f)
        os.utime(p, (1767225600, 1767225600))
        with open(os.path.join(d, "notes.txt"), "w") as f:
            f.write("leave me")
        self.assertEqual(backups.encrypt_existing(), 1)
        self.assertEqual(backups.encrypt_existing(), 0)          # idempotent
        self.assertTrue(backups.is_encrypted(open(p, "rb").read()))
        self.assertEqual(int(os.path.getmtime(p)), 1767225600)   # age preserved
        self.assertEqual(json.loads(backups.read("farm-b", None)[0]), BUNDLE)
        self.assertEqual(open(os.path.join(d, "notes.txt")).read(), "leave me")

    def test_key_is_stable(self):
        self.assertEqual(backups.key(), backups.key())


if __name__ == "__main__":
    unittest.main()
