"""MikroTik SwOS switch support (app/swos.py): the page format, the snapshot shape
the monitor and UIs share with the EdgeSwitch, and writes against a fake switch
that speaks digest auth like the real one. The fixtures are the real answers of
the home CSS106-1G-4P-1S (SwOS 2.11), 2026-09-19.

Run inside the site image:
  docker run --rm -v "$PWD/app:/app" -v "$PWD/tests:/tests" -e NETWATCH_DATA=/tmp/nw \
    --entrypoint python farm-netwatch:netcfg /tests/test_swos.py -v
"""
import hashlib
import os
import re
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import credtest   # noqa: E402
import identify   # noqa: E402
import swos       # noqa: E402
import switchmon  # noqa: E402

SYS = ("{upt:0x003206b1,ip:0x2358a8c0,mac:'48a98af63f71',sid:'48454e30385a364b435358',id:'4d696b726f54696b',"
       "ver:'322e3131',brd:'4353533130362d31472d34502d3153',bld:0x5e4e7ab2,wdt:0x01,dsc:0x01,ivl:0x00,"
       "alla:0x00000000,allm:0x00,allp:0x3f,avln:0x0000,prio:0x8000,cost:0x00,rpr:0x8000,rmac:'48a98af63f71',"
       "igmp:0x00,sip:0x0158a8c0,iptp:0x00,volt:0x0072,temp:0x00000014,lcbl:0x00,upgr:0x00}")
LINK = ("{en:0x3f,lnk:0x01,spd:[0x02,0x03,0x03,0x03,0x03,0x03],dpx:0x21,an:0x3f,spdc:[0x00,0x00,0x00,0x00,0x00,0x00],"
        "dpxc:0x3f,fct:0x3f,poe:[0x01,0x01,0x01,0x01,0x01,0x01],prio:[0xff,0x00,0x01,0x02,0x03,0x04],"
        "poes:[0x00,0x02,0x02,0x03,0x05,0x00],curr:[0x0000,0x0000,0x0000,0x00c8,0x0000,0x0000],"
        "pwr:[0x0000,0x0000,0x0000,0x0036,0x0000,0x0000],"
        "nm:['506f727431','506f727432','506f727433','506f727434','506f727435','534650']}")
_Z = ",0x00" * 4
STATS = (f"{{rb:[0x071ad0c7,0x00033616{_Z}],rbh:[0x00000001,0x00{_Z}],tb:[0x00001000,0x00{_Z}],"
         f"tbh:[0x00,0x00{_Z}],rte:[0x00000002,0x00{_Z}],tte:[0x00000001,0x00{_Z}]}}")
HOSTS = ("[{adr:'00d089164163',vid:0x0000,prt:0x00,drp:0x00,mir:0x00},{adr:'2ccf67e2e93d',vid:0x0000,prt:0x00,"
         "drp:0x00,mir:0x00},{adr:'2ccf67e2e93d',vid:0x0000,prt:0x00,drp:0x00,mir:0x00},"
         "{adr:'c0b8e67b4baf',vid:0x0000,prt:0x03,drp:0x00,mir:0x00}]")
FWD = "{vlan:[0x01,0x01,0x01,0x01,0x01,0x01],dvid:[0x0001,0x0001,0x0001,0x0001,0x0001,0x0001]}"
SNMP = "{en:0x01,com:'7075626c6963',ci:'',loc:''}"


# ---- a fake SwOS switch (digest auth, the real files, POST /link.b applies) ----------
class FakeSwos:
    def __init__(self, password=""):
        self.password = password
        self.files = {"/sys.b": SYS, "/link.b": LINK, "/!stats.b": STATS, "/!dhost.b": HOSTS,
                      "/fwd.b": FWD, "/vlan.b": "[]", "/snmp.b": SNMP}
        self.posts = []
        self.rebooted = False
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _authed(self, method):
                h = self.headers.get("Authorization") or ""
                if not h.startswith("Digest "):
                    return False
                f = dict(re.findall(r'(\w+)="?([^",]*)"?', h[7:]))
                ha1 = hashlib.md5(f"{f.get('username')}:{f.get('realm')}:{fake.password}".encode()).hexdigest()
                ha2 = hashlib.md5(f"{method}:{f.get('uri')}".encode()).hexdigest()
                want = hashlib.md5(f"{ha1}:{f.get('nonce')}:{f.get('nc')}:{f.get('cnonce')}:{f.get('qop')}:{ha2}"
                                   .encode()).hexdigest()
                return f.get("username") == "admin" and f.get("response") == want

            def _deny(self):
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Digest realm="CSS106", qop="auth", nonce="abc123"')
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _send(self, body, code=200):
                data = body.encode() if isinstance(body, str) else body
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if not self._authed("GET"):
                    return self._deny()
                if self.path == "/backup.swb":
                    return self._send(b"SWB\x00config")
                if self.path in fake.files:
                    return self._send(fake.files[self.path])
                self._send("", 404)

            def do_POST(self):
                if not self._authed("POST"):
                    return self._deny()
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                fake.posts.append((self.path, body))
                if self.path == "/reboot":
                    fake.rebooted = True
                    return self._send("")
                if self.path == "/link.b":
                    cur = swos.parse(fake.files["/link.b"])
                    cur.update(swos.parse(body))
                    fake.files["/link.b"] = swos.dump(cur)
                    return self._send("")
                self._send("", 404)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def _patch_base(fake):
    """swos.Session talks to http://<ip>; point it at the fake's port."""
    orig = swos.Session.__init__

    def init(self, ip, username, password, timeout=swos.TIMEOUT):
        orig(self, ip, username, password, timeout)
        self.base = f"http://127.0.0.1:{fake.port}"
    swos.Session.__init__ = init
    return lambda: setattr(swos.Session, "__init__", orig)


class FormatTest(unittest.TestCase):
    def test_parse_real_answers(self):
        s = swos.parse(SYS)
        self.assertEqual(s["upt"], 0x003206b1)
        self.assertEqual(swos.hexstr(s["brd"]), "CSS106-1G-4P-1S")
        self.assertEqual(swos._ip(s["ip"]), "192.168.88.35")
        self.assertEqual(len(swos.parse(HOSTS)), 4)
        self.assertEqual(swos.parse("[]"), [])

    def test_dump_round_trips_and_pads_hex(self):
        link = swos.parse(LINK)
        self.assertEqual(swos.parse(swos.dump(link)), link)
        self.assertEqual(swos.dump({"a": 0x3, "b": 0x123}), "{a:0x03,b:0x0123}")

    def test_bad_answer_raises(self):
        with self.assertRaises((ValueError, IndexError)):
            swos.parse("<html>")


class NormalizeTest(unittest.TestCase):
    def setUp(self):
        st2 = swos.parse(STATS)
        st2["rb"] = list(st2["rb"])
        st2["rb"][0] += 1000                      # 1000 bytes in 2 s on port 1
        self.snap = swos.normalize(swos.parse(SYS), swos.parse(LINK), swos.parse(STATS),
                                   swos.parse(HOSTS), swos.parse(FWD), [], swos.parse(SNMP), st2, 2.0)
        self.p = {p["id"]: p for p in self.snap["ports"]}

    def test_device(self):
        d = self.snap["device"]
        self.assertEqual((d["model"], d["firmware"], d["serial"]), ("CSS106-1G-4P-1S", "2.11", "HEN08Z6KCSX"))
        self.assertEqual(d["name"], "")           # "MikroTik" is the factory identity
        self.assertEqual(d["uptime"], 0x003206b1 // 100)
        self.assertEqual(self.snap["health"]["temp"], 20.0)
        self.assertEqual(self.snap["health"]["psu"][0]["voltage"], 11.4)

    def test_ports(self):
        self.assertEqual(list(self.p), ["0/1", "0/2", "0/3", "0/4", "0/5", "0/6"])
        p1 = self.p["0/1"]
        self.assertTrue(p1["up"])
        self.assertEqual((p1["speed"], p1["duplex"]), (1000, "full"))
        self.assertFalse(p1["poe_supported"])     # PoE-in port: no PoE out
        self.assertEqual(p1["errors"], 3)
        self.assertEqual(p1["rx_bytes"], (1 << 32) + 0x071ad0c7 + 1000)
        self.assertAlmostEqual(p1["rx_bps"], 4000.0)
        self.assertEqual(self.p["0/6"]["type"], "sfp")
        self.assertIsNone(self.p["0/2"]["speed"])  # no link, speed index 3 = none

    def test_poe(self):
        self.assertEqual((self.p["0/2"]["poe_mode"], self.p["0/2"]["poe_status"]), ("auto", "waiting for load"))
        self.assertEqual(self.p["0/4"]["poe_w"], 5.4)
        self.assertEqual(self.p["0/4"]["poe_ma"], 200)
        self.assertTrue(self.p["0/5"]["poe_fault"])           # short circuit
        self.assertFalse(self.p["0/4"]["poe_fault"])
        self.assertEqual(self.snap["poe"]["powered"], 1)
        self.assertEqual(self.snap["poe"]["used_w"], 5.4)

    def test_mac_table_dedup_and_ports(self):
        self.assertEqual([m["mac"] for m in self.p["0/1"]["macs"]], ["00:d0:89:16:41:63", "2c:cf:67:e2:e9:3d"])
        self.assertEqual([m["mac"] for m in self.p["0/4"]["macs"]], ["c0:b8:e6:7b:4b:af"])

    def test_protected_port_uses_the_mac_table(self):
        prot = switchmon.protected_ports(self.snap, own_macs={"2c:cf:67:e2:e9:3d"}, gw_mac="00:d0:89:16:41:63")
        self.assertEqual(list(prot), ["0/1"])

    def test_services_flag_default_snmp(self):
        self.assertEqual(self.snap["services"]["snmp_community"], "public")
        self.assertFalse(self.snap["caps"]["locate"])
        self.assertFalse(self.snap["caps"]["cable_test"])


class DetectTest(unittest.TestCase):
    DEV = {"banner": {"title": "MikroTik SwOS"}, "vendor": "Routerboard.com",
           "services": {"80": "http MikroTik RouterBoard 250GS httpd"}, "ports": [80]}

    def test_swos_is_a_switch_not_edgeswitch(self):
        self.assertIs(switchmon.driver(self.DEV), swos)
        self.assertTrue(swos.is_swos({"services": {"80": "http MikroTik RouterBoard 250GS httpd"}}))
        self.assertFalse(swos.is_swos({"banner": {"title": "RouterOS router configuration page"},
                                       "vendor": "Routerboard.com"}))
        self.assertIsNone(switchmon.driver({"vendor": "Routerboard.com", "banner": {"title": "RouterOS"}}))

    def test_classify(self):
        cat, label, _ = identify.classify("Routerboard.com", [80], {"title": "MikroTik SwOS"}, "")
        self.assertEqual((cat, label), ("network", "Switch (MikroTik SwOS)"))

    def test_credtest_plan(self):
        self.assertEqual(credtest.plan(self.DEV, [80]), ["swos"])


class LiveFakeTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSwos(password="")
        self.unpatch = _patch_base(self.fake)

    def tearDown(self):
        self.unpatch()
        self.fake.close()

    def test_read(self):
        s = swos.read("fake", "admin", "", rate_s=0)
        self.assertTrue(s["ok"], s)
        self.assertEqual(s["summary"]["ports"], 6)

    def test_wrong_password_is_auth_failed(self):
        s = swos.read("fake", "admin", "nope", rate_s=0)
        self.assertEqual((s["ok"], s["kind"]), (False, "auth_failed"))
        self.assertEqual(credtest.test_swos("fake", "admin", "nope")["result"], "auth_failed")
        self.assertEqual(credtest.test_swos("fake", "admin", "")["result"], "ok")

    def test_port_off_sends_only_writable_fields_and_one_bit(self):
        r = swos.set_port("fake", "admin", "", "0/3", enabled=False)
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["before"]["enabled"], r["after"]["enabled"]), (True, False))
        path, body = self.fake.posts[-1]
        sent = swos.parse(body)
        self.assertEqual(path, "/link.b")
        self.assertEqual(set(sent), set(swos.LINK_WRITABLE))
        self.assertEqual(sent["en"], 0x3f & ~(1 << 2))

    def test_poe_mode_and_name(self):
        self.assertTrue(swos.set_port("fake", "admin", "", "0/2", poe="off")["ok"])
        self.assertEqual(swos.parse(self.fake.files["/link.b"])["poe"][1], 0)
        r = swos.set_port("fake", "admin", "", "0/2", name="Gate cam")
        self.assertEqual(r["after"]["name"], "Gate cam")
        self.assertFalse(swos.set_port("fake", "admin", "", "0/2", poe="48v")["ok"])
        self.assertFalse(swos.set_port("fake", "admin", "", "0/9", enabled=False)["ok"])
        self.assertFalse(swos.set_port("fake", "admin", "", "0/1", poe="off")["ok"])  # PoE-in port

    def test_poe_cycle_restores_mode(self):
        r = swos.poe_cycle("fake", "admin", "", "0/4", off_s=0)
        self.assertTrue(r["ok"], r)
        modes = [swos.parse(b)["poe"][3] for p, b in self.fake.posts if p == "/link.b"]
        self.assertEqual(modes, [0, 1])
        self.assertFalse(swos.poe_cycle("fake", "admin", "", "0/6", off_s=0)["ok"])  # SFP: no PoE

    def test_reboot_and_backup(self):
        self.assertTrue(swos.reboot("fake", "admin", "")["ok"])
        self.assertTrue(self.fake.rebooted)
        b = swos.backup("fake", "admin", "")
        self.assertEqual((b["ok"], b["ext"]), (True, ".swb"))
        self.assertFalse(swos.locate("fake", "admin", "")["ok"])


if __name__ == "__main__":
    unittest.main()
