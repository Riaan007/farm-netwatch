"""Device record edits: the category set, the checks on POST /api/devices/<key>,
which device writes need the site login, and stored junk never breaking a
restore or reaching the device feed.

Run inside the site image with a throwaway data dir:
  docker run --rm -v "$PWD/app:/app" -v "$PWD/tests:/tests" -e NETWATCH_DATA=/tmp/nw \
    --entrypoint python farm-netwatch:netcfg /tests/test_device_meta.py -v
"""
import inspect
import io
import os
import re
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
APP = os.environ.get("NETWATCH_APP", "/app")
sys.path.insert(0, APP)

import assets      # noqa: E402
import discovery   # noqa: E402
import identify    # noqa: E402
import kuma        # noqa: E402
import monitoring  # noqa: E402
import server      # noqa: E402
import siteauth    # noqa: E402

CAM, NVR, GONE = "aa:00:00:00:00:01", "aa:00:00:00:00:02", "aa:00:00:00:00:09"
KEY = "k" * 43
LAN = {"REMOTE_ADDR": "192.168.0.50"}
HUB = {siteauth.HEADER: KEY}
BAD_CATEGORIES = ["<img src=x onerror=alert(1)>", "Camera", "camera ", "", "switch",
                  "constructor", 5, True, ["camera"], {"camera": 1}]


def page():
    with open(os.path.join(APP, "static", "index.html"), encoding="utf-8") as f:
        return f.read()


class CategorySet(unittest.TestCase):
    """identify.CATEGORIES is the one list: everything the site can produce, and
    exactly what the site page knows how to show."""

    def test_everything_the_scanner_produces_is_known(self):
        produced = set(re.findall(r'return "([a-z-]+)", ', inspect.getsource(identify.classify)))
        self.assertIn("camera", produced)
        self.assertLessEqual(produced, identify.CATEGORIES)
        self.assertLessEqual(set(discovery._MDNS_CAT.values()), identify.CATEGORIES)

    def test_the_page_offers_and_labels_exactly_the_known_set(self):
        html = page()
        labels = re.search(r"const catLabel = \{(.*?)\};", html, re.S).group(1)
        keys = {a or b for a, b in re.findall(r'(?:"([a-z-]+)"|\b([a-z]+))\s*:', labels)}
        self.assertEqual(keys, identify.CATEGORIES)
        select = re.search(r'<select id="edit-category".*?</select>', html, re.S).group(0)
        self.assertEqual(set(re.findall(r'<option value="([^"]+)"', select)), identify.CATEGORIES)

    def test_kuma_tags_and_asset_groups_use_known_categories(self):
        self.assertEqual(set(kuma._TAG), identify.CATEGORIES)
        self.assertLessEqual(set(assets.BY_CATEGORY), identify.CATEGORIES)

    def test_is_category_never_raises(self):
        self.assertTrue(identify.is_category("internet-ap"))
        for v in BAD_CATEGORIES + [None]:
            self.assertFalse(identify.is_category(v), repr(v))


class Base(unittest.TestCase):
    def setUp(self):
        if os.path.exists(siteauth.AUTH_PATH):
            os.remove(siteauth.AUTH_PATH)
        siteauth._fails.clear()
        siteauth.set_hub_key(KEY)
        self.s = server.scanner
        self.s.registry = {CAM: {"name": "Gate"}, NVR: {}}
        now = int(time.time())
        self.s.devices = {CAM: {"key": CAM, "ip": "10.0.0.2", "online": True, "last_seen": now,
                                "category": "camera", "name": "Gate"},
                          NVR: {"key": NVR, "ip": "10.0.0.3", "online": True, "last_seen": now,
                                "category": "nvr", "name": ""}}
        mock.patch.object(self.s, "save_registry").start()
        mock.patch.object(self.s, "trigger").start()
        mock.patch.object(monitoring, "follow_kuma").start()
        self.addCleanup(mock.patch.stopall)
        self.c = server.app.test_client()

    def post(self, path, body=None, hub=True, **kw):
        if body is not None:
            kw["json"] = body
        return self.c.post(path, environ_base=LAN, headers=HUB if hub else {}, **kw)


class DeviceMetaApi(Base):
    def test_anonymous_edits_are_refused_and_change_nothing(self):
        for body in ({"name": "Pwned"}, {"category": "iot"}, {"serial": "x"}, {"model": "x"},
                     {"type": "x"}, {"link": NVR}, {}):
            r = self.post(f"/api/devices/{CAM}", body, hub=False)
            self.assertEqual(r.status_code, 401, body)
            self.assertEqual(r.get_json()["error"], "auth_required")
        self.assertEqual(self.s.registry[CAM], {"name": "Gate"})
        self.assertEqual(self.s.devices[CAM]["category"], "camera")

    def test_hub_key_saves_every_field(self):
        r = self.post(f"/api/devices/{CAM}", {"name": "  Front gate ", "category": "nvr", "serial": "SN1",
                                             "model": "DS-2CD", "type": "Bullet", "link": NVR})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        reg = self.s.registry[CAM]
        self.assertEqual((reg["name"], reg["category"], reg["serial"], reg["model"], reg["type"], reg["link"]),
                         ("Front gate", "nvr", "SN1", "DS-2CD", "Bullet", NVR))
        self.assertEqual(self.s.devices[CAM]["category"], "nvr")
        # "" clears the link; a field left out is not touched
        self.assertEqual(self.post(f"/api/devices/{CAM}", {"link": ""}).status_code, 200)
        self.assertEqual((reg["link"], reg["name"]), ("", "Front gate"))

    def test_a_logged_in_person_can_save(self):
        siteauth.set_password("farm-pass-1")
        self.assertEqual(self.c.post("/api/auth/login", json={"password": "farm-pass-1"},
                                     environ_base=LAN).status_code, 200)
        r = self.post(f"/api/devices/{CAM}", {"name": "Back gate", "category": "camera"}, hub=False)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.s.registry[CAM]["name"], "Back gate")

    def test_unknown_categories_are_refused(self):
        for cat in BAD_CATEGORIES:
            r = self.post(f"/api/devices/{CAM}", {"name": "Changed", "category": cat})
            self.assertEqual(r.status_code, 400, repr(cat))
            self.assertIn("unknown category", r.get_json()["error"])
        self.assertEqual(self.s.registry[CAM], {"name": "Gate"})     # nothing half-saved
        for cat in sorted(identify.CATEGORIES):
            self.assertEqual(self.post(f"/api/devices/{CAM}", {"category": cat}).status_code, 200, cat)

    def test_text_fields_must_be_short_text(self):
        for body in ({"name": 5}, {"name": ["a"]}, {"serial": {"a": 1}}, {"model": True},
                     {"name": "x" * 101}, {"type": "x" * 121}):
            self.assertEqual(self.post(f"/api/devices/{CAM}", body).status_code, 400, str(body)[:40])
        self.assertEqual(self.s.registry[CAM], {"name": "Gate"})
        self.assertEqual(self.post(f"/api/devices/{CAM}", {"name": "x" * 100}).status_code, 200)
        self.assertEqual(self.post(f"/api/devices/{CAM}", {"name": None}).status_code, 200)   # null = unchanged
        self.assertEqual(self.s.registry[CAM]["name"], "x" * 100)

    def test_link_must_be_another_known_device(self):
        for link in (CAM, GONE, 7, ["x"], "x" * 30):
            self.assertEqual(self.post(f"/api/devices/{CAM}", {"link": link}).status_code, 400, repr(link))
        self.assertNotIn("link", self.s.registry[CAM])

    def test_bad_bodies_and_unknown_devices(self):
        for data in ("[1, 2]", '"name"', "{not json", "null"):
            r = self.post(f"/api/devices/{CAM}", data=data, content_type="application/json")
            self.assertEqual(r.status_code, 400, data)
        self.assertEqual(self.post(f"/api/devices/{GONE}", {"name": "Ghost"}).status_code, 404)
        self.assertNotIn(GONE, self.s.registry)
        self.assertEqual(self.post(f"/api/devices/{CAM}", {"watch": "yes"}).status_code, 400)
        self.assertEqual(self.post(f"/api/devices/{CAM}", {"watch": 1}).status_code, 400)

    def test_an_offline_device_known_only_to_the_registry_can_be_edited(self):
        del self.s.devices[NVR]
        self.assertEqual(self.post(f"/api/devices/{NVR}", {"name": "Recorder"}).status_code, 200)
        self.assertEqual(self.s.registry[NVR]["name"], "Recorder")

    def test_old_hub_watch_call_still_works(self):
        r = self.post(f"/api/devices/{CAM}", {"watch": True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.s.registry[CAM]["watch"])


class OtherDeviceWrites(Base):
    """GPS, asset register, photo and wireless-bridge MACs need the login;
    reading them, and the problem acks, stay open."""
    PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32

    def upload(self, hub):
        return self.post(f"/api/devices/{CAM}/photo", hub=hub, content_type="multipart/form-data",
                         data={"photo": (io.BytesIO(self.PNG), "cam.png")})

    def test_anonymous_writes_are_refused(self):
        self.assertEqual(self.post(f"/api/devices/{CAM}/location", {"lat": -33.9, "lon": 18.4}, hub=False).status_code, 401)
        self.assertEqual(self.post(f"/api/devices/{CAM}/asset", {"asset": {"asset_tag": "A1"}}, hub=False).status_code, 401)
        self.assertEqual(self.upload(hub=False).status_code, 401)
        self.assertEqual(self.c.delete(f"/api/devices/{CAM}/photo", environ_base=LAN).status_code, 401)
        with mock.patch.object(self.s, "apply_bridge_macs") as apply:
            r = self.post("/api/bridge-macs", {"mac": "788a203a529b"}, hub=False)
        self.assertEqual(r.status_code, 401)
        apply.assert_not_called()
        self.assertEqual(self.s.registry[CAM], {"name": "Gate"})
        self.assertEqual(server.config.load()["scan"].get("bridge_macs") or [], [])

    def test_reads_stay_open(self):
        self.assertEqual(self.c.get(f"/api/devices/{CAM}/asset", environ_base=LAN).status_code, 200)
        self.assertEqual(self.c.get("/api/bridge-macs", environ_base=LAN).status_code, 200)
        self.assertIn(self.c.get(f"/api/devices/{NVR}/photo", environ_base=LAN).status_code, (200, 404))

    def test_problem_acks_stay_open(self):
        with mock.patch.object(self.s, "wake"):
            self.assertEqual(self.post("/api/conflicts/clear", {"ip": "10.0.0.2"}, hub=False).status_code, 200)
            self.assertEqual(self.post(f"/api/devices/{CAM}/ack-ip", {}, hub=False).status_code, 200)

    def test_with_the_hub_key(self):
        r = self.post(f"/api/devices/{CAM}/location", {"lat": -33.9, "lon": 18.4})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.s.registry[CAM]["geo"]["lat"], -33.9)
        r = self.post(f"/api/devices/{CAM}/asset", {"asset": {"asset_tag": " A1 ", "nonsense": "x"}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.s.registry[CAM]["asset"], {"asset_tag": "A1"})
        self.assertEqual(self.upload(hub=True).status_code, 200)
        with self.c.get(f"/api/devices/{CAM}/photo", environ_base=LAN) as got:
            self.assertEqual(got.status_code, 200)
        self.assertEqual(self.c.delete(f"/api/devices/{CAM}/photo", environ_base=LAN, headers=HUB).status_code, 200)
        with mock.patch.object(self.s, "apply_bridge_macs"), mock.patch.object(self.s, "wake"):
            r = self.post("/api/bridge-macs", {"mac": "78:8a:20:3a:52:9b"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()["bridge_macs"], ["788a203a529b"])
            self.post("/api/bridge-macs", {"mac": "788a203a529b", "enable": False})
        self.assertEqual(server.config.load()["scan"]["bridge_macs"], [])

    def test_unknown_devices_and_bad_bodies(self):
        self.assertEqual(self.post(f"/api/devices/{GONE}/location", {"lat": 1, "lon": 1}).status_code, 404)
        self.assertEqual(self.post(f"/api/devices/{GONE}/asset", {"asset": {"asset_tag": "A1"}}).status_code, 404)
        self.assertEqual(self.post(f"/api/devices/{CAM}/asset", data="[1]", content_type="application/json").status_code, 400)
        self.assertEqual(self.post(f"/api/devices/{CAM}/asset", {"asset": ["x"]}).status_code, 400)
        r = self.post(f"/api/devices/{GONE}/photo", content_type="multipart/form-data",
                      data={"photo": (io.BytesIO(self.PNG), "x.png")})
        self.assertEqual(r.status_code, 404)
        self.assertNotIn(GONE, self.s.registry)
        self.assertFalse(os.path.exists(server._photo_path(GONE)))


class StoredJunk(Base):
    """A registry from before the check (or a restored bundle) may hold any
    category: a restore still works, and nothing unknown reaches the feed."""

    def test_restore_keeps_a_registry_with_odd_categories(self):
        bundle = {"kind": "netwatch-backup", "config": {k: v for k, v in server.config.load().items()},
                  "devices": {CAM: {"name": "Gate", "category": "<b>x</b>"},
                              NVR: {"category": {"x": 1}}, GONE: {"category": 7, "name": "Old"}}}
        r = self.post("/api/config/import", bundle)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("device registry (3)", r.get_json()["applied"])
        self.assertEqual(self.s.registry[CAM]["category"], "<b>x</b>")      # kept as it was
        self.assertEqual(self.s.registry[GONE]["name"], "Old")

    def test_feed_shows_unknown_for_a_category_it_does_not_know(self):
        self.s.devices[CAM]["category"] = "<img src=x onerror=alert(1)>"
        self.s.devices[NVR]["category"] = {"x": 1}
        now = int(time.time())
        self.s.devices["aa:00:00:00:00:03"] = {"key": "aa:00:00:00:00:03", "ip": "10.0.0.4", "online": True,
                                               "last_seen": now, "category": "voip"}
        self.s.devices["aa:00:00:00:00:04"] = {"key": "aa:00:00:00:00:04", "ip": "10.0.0.5", "online": True,
                                               "last_seen": now, "category": ""}
        devs = {d["key"]: d for d in self.c.get("/api/devices", environ_base=LAN).get_json()["devices"]}
        self.assertEqual(devs[CAM]["category"], "unknown")
        self.assertEqual(devs[NVR]["category"], "unknown")
        self.assertEqual(devs["aa:00:00:00:00:03"]["category"], "voip")
        self.assertEqual(devs["aa:00:00:00:00:04"]["category"], "")

    def test_a_scan_ignores_a_stored_category_it_does_not_know(self):
        h = {"ip": "10.0.0.2", "mac": CAM, "nmap_vendor": "Hikvision", "hostname": "x",
             "ports": [80, 554], "services": {}, "os": "", "rtt": 1.0}
        with mock.patch.object(identify, "reverse_dns", return_value=""):
            for stored, shown in (("<b>x</b>", "camera"), ({"x": 1}, "camera"), (7, "camera"),
                                  ("", "camera"), ("nvr", "nvr"), ("unknown", "unknown")):
                self.s.registry[CAM] = {"category": stored}
                rec = self.s._build_record(h, "10.0.0.0/24", True, False, False, CAM)
                self.assertEqual(rec["category"], shown, repr(stored))


if __name__ == "__main__":
    unittest.main()
