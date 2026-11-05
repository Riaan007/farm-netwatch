"""MikroTik helpers (app/mikrotik.py) — the network-free logic: MNDP parsing,
RouterOS API length framing, the console danger guard, the MAC-Telnet `:put`
status parser, the console echo strip (RouterOS 6.49 redraws the input line per
keystroke) and the "API turned off" advice. No real router is ever contacted: the
MAC-Telnet tests drive mt_run under a real pty against a stand-in console.

Run inside the site image:
  docker run --rm -v "$PWD/app:/app" -v "$PWD/tests:/tests" -e NETWATCH_DATA=/tmp/nw \
    --entrypoint python farm-netwatch:netcfg /tests/test_mikrotik.py -v
"""
import os
import struct
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import mikrotik    # noqa: E402


def _tlv(t, v):
    return struct.pack(">HH", t, len(v)) + v


def _mndp_packet(mac, identity, version, board, ip, uptime, iface):
    body = b"\x00\x00\x00\x00"                       # 4-byte header
    body += _tlv(1, bytes.fromhex(mac.replace(":", "")))
    body += _tlv(5, identity.encode())
    body += _tlv(7, version.encode())
    body += _tlv(8, b"MikroTik")
    body += _tlv(10, struct.pack("<I", uptime))
    body += _tlv(12, board.encode())
    body += _tlv(16, iface.encode())
    body += _tlv(17, bytes(int(x) for x in ip.split(".")))
    return body


class TestMndp(unittest.TestCase):
    def test_parse_real_shape(self):
        pkt = _mndp_packet("08:55:31:60:b6:76", "MikroTik", "6.45.9 (long-term)",
                           "RB750UPr2", "192.168.88.46", 1472, "bridgeLocal/ether1")
        d = mikrotik._parse_mndp(pkt)
        self.assertEqual(d["mac"], "08:55:31:60:b6:76")
        self.assertEqual(d["identity"], "MikroTik")
        self.assertEqual(d["version"], "6.45.9 (long-term)")
        self.assertEqual(d["board"], "RB750UPr2")
        self.assertEqual(d["ipv4"], "192.168.88.46")
        self.assertEqual(d["uptime"], 1472)
        self.assertEqual(d["interface"], "bridgeLocal/ether1")

    def test_truncated_packet_is_safe(self):
        # a body that claims a longer TLV than it carries must not raise
        bad = b"\x00\x00\x00\x00" + struct.pack(">HH", 5, 99) + b"short"
        self.assertIsInstance(mikrotik._parse_mndp(bad), dict)

    def test_normalize_mac(self):
        self.assertEqual(mikrotik.normalize_mac("0855.3160.B676"), "08:55:31:60:b6:76")
        self.assertEqual(mikrotik.normalize_mac("08-55-31-60-b6-76"), "08:55:31:60:b6:76")
        self.assertEqual(mikrotik.normalize_mac("nope"), "")


class TestApiFraming(unittest.TestCase):
    def test_enc_len_boundaries(self):
        self.assertEqual(mikrotik.RosApi._enc_len(0x05), b"\x05")
        self.assertEqual(mikrotik.RosApi._enc_len(0x7f), b"\x7f")
        self.assertEqual(mikrotik.RosApi._enc_len(0x80), b"\x80\x80")
        self.assertEqual(mikrotik.RosApi._enc_len(0x4000), b"\xc0\x40\x00")

    def test_enc_len_round_trip(self):
        # decode with the same rules the client reads with, over a fake socket
        class FakeSock:
            def __init__(self, data): self.data = data
            def recv(self, n):
                out, self.data = self.data[:n], self.data[n:]
                return out
        for n in (0, 1, 0x7f, 0x80, 0x1234, 0x4000, 0x1FFFFF, 0x200000):
            api = mikrotik.RosApi("0.0.0.0")
            api.sock = FakeSock(mikrotik.RosApi._enc_len(n))
            self.assertEqual(api._read_len(), n, "len %d did not round-trip" % n)


class TestDangerGuard(unittest.TestCase):
    def test_flags_bricking_commands(self):
        for c in ["/system reset-configuration", "/system reset",
                  "/interface disable ether1", "/interface remove bridge",
                  "/ip address remove 0", "/user set admin password=x",
                  "/user remove admin", "/system shutdown",
                  "/system routerboard upgrade", "  /SYSTEM   RESET  "]:
            self.assertTrue(mikrotik.dangerous_command(c), "should flag: %r" % c)

    def test_flags_slash_delimited_form(self):
        # the RouterOS one-liner (slash) form must NOT bypass the guard
        for c in ["/system/reboot", "/system/shutdown", "/system/reset",
                  "/ip/address/remove numbers=0", "/ip/firewall/filter/remove numbers=0",
                  "/system/routerboard/upgrade", "/interface/disable ether2"]:
            self.assertTrue(mikrotik.dangerous_command(c), "slash form should flag: %r" % c)

    def test_passes_read_commands(self):
        for c in ["/system identity print", "/system resource print",
                  "/ip address print", "/interface print", "/log print",
                  "/system routerboard print"]:
            self.assertEqual(mikrotik.dangerous_command(c), "", "should pass: %r" % c)


class TestMtStatusParse(unittest.TestCase):
    CANNED = "\n".join([
        "__NW__|identity|MikroTik",
        "__NW__|version|6.45.9 (long-term)",
        "__NW__|board|hEX PoE lite",
        "__NW__|uptime|01:23:15",
        "__NW__|cpu|3",
        "__NW__|free_memory|44298240",
        "__NW__|total_memory|67108864",
        "__NW__|arch|mipsbe",
        "__NW__|model|RB750UPr2",
        "__NW__|serial|D1630D332D79",
        "__NW__|firmware|6.45.9",
        "__NW__|iface|ether1~ether~true~false~22657774~9040211",
        "__NW__|iface|ether2~ether~false~false~0~0",
        "__NW__|addr|192.168.88.46/24~bridgeLocal",
    ])

    def test_parse(self):
        orig = mikrotik.mt_run
        mikrotik.mt_run = lambda *a, **k: {"ok": True, "output": self.CANNED, "per": []}
        try:
            s = mikrotik.mt_status("08:55:31:60:b6:76", "admin", "")
        finally:
            mikrotik.mt_run = orig
        self.assertTrue(s["ok"])
        self.assertEqual(s["source"], "mactelnet")
        self.assertEqual(s["identity"], "MikroTik")
        self.assertEqual(s["model"], "RB750UPr2")
        self.assertEqual(s["board"], "hEX PoE lite")
        self.assertEqual(s["serial"], "D1630D332D79")
        self.assertEqual(s["arch"], "mipsbe")
        self.assertEqual(len(s["interfaces"]), 2)
        self.assertEqual(s["interfaces"][0]["name"], "ether1")
        self.assertTrue(s["interfaces"][0]["running"])
        self.assertFalse(s["interfaces"][1]["running"])
        self.assertEqual(s["addresses"], [{"address": "192.168.88.46/24",
                                           "interface": "bridgeLocal", "disabled": False}])


class TestClean(unittest.TestCase):
    def test_collapses_blank_lines_and_ansi(self):
        raw = "a\r\n\r\n\r\n\r\nb\x1b[32mc\x1b[0m"
        out = mikrotik._clean(raw)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\n\n\n", out)
        self.assertIn("bc", out)


# --- the console echo ----------------------------------------------------------
# A made-up user, identity, MAC and documentation-range addresses — never a client's.
P = "[ops@hex-lite] > "
WIDTH = 200                      # mt_run's pty is 200 columns; RouterOS pads to 199
MAC = "02:00:00:00:00:20"
BANNER = ("Connecting to %s...done\n\n\n  MikroTik RouterOS 6.49.8 (c) 1999-2023"
          "       https://www.mikrotik.com/\n\n" % MAC)
CMD = "/ip service print"
ANSWER = "\n".join([
    "Flags: X - disabled, I - invalid",
    "",
    " #   NAME     PORT  ADDRESS                                   CERTIFICATE",
    "",
    " 0   telnet     23",
    " 1 X api      8728",
    " 2   winbox   8291",
])


def echo_649(cmd, answer, prompt=P):
    """What comes back for one typed command on RouterOS 6.49.8 under the +cte
    login, after _clean() — the shape a hEX lite (RB750r2) sent on 2026-11-05:
    the first character bare on its own line; then per further keystroke the old
    line padded to the window width and the new one; on Enter the whole line three
    more times; then the answer and the next prompt (redrawn padded, then bare)."""
    out = [cmd[0], ""]
    for k in range(1, len(cmd)):
        out += [(prompt + cmd[:k]).ljust(WIDTH - 1), prompt + cmd[:k + 1], ""]
    out += [(prompt + cmd).ljust(WIDTH - 1), prompt + cmd, prompt + cmd, ""]
    return "\n".join(out + answer.split("\n") + ["", prompt.ljust(WIDTH - 1), prompt])


def echo_645(cmd, answer, prompt=P):
    """RouterOS 6.45.9 (the home RB750UPr2): the command echoed once — the prompt
    was already on screen when it was typed — then the answer and the next prompt."""
    return "\n".join([cmd] + answer.split("\n") + ["", prompt])


def old_trim(text, cmd):
    """mt_run's per-command trim before the echo fix, kept to prove a console
    that doesn't redraw still gives exactly the answer it always did."""
    lines, cc = text.split("\n"), cmd.strip()
    while lines and (not lines[0].strip() or (cc and cc in lines[0]) or "] >" in lines[0]):
        lines.pop(0)
    while lines and (not lines[-1].strip() or "] >" in lines[-1]):
        lines.pop()
    return "\n".join(lines)


CLEAN_CASES = [
    (CMD, ANSWER),
    ("/system resource print", "             uptime: 3d4h5m6s\n\n            version: 6.45.9 (long-term)"),
    (":put [/system identity get name]", "hex-lite"),
    ('/log print where message~"login failure"', "10:02:11 system,error,critical login failure for user x"),
    (":delay 1", ""),
]


class TestConsoleEcho(unittest.TestCase):
    def test_sample_has_the_real_shape(self):
        # guard the fixture: the bare first character, lines padded to the window,
        # a window's width of echo per typed character — what 6.49.8 sent
        text = echo_649(CMD, ANSWER)
        lines = text.split("\n")
        self.assertEqual(lines[0], "/")
        self.assertEqual(lines[2], (P + "/").ljust(WIDTH - 1))
        self.assertEqual(lines[3], P + "/i")
        self.assertGreater(len(text), WIDTH * len(CMD))
        # ... which the old trim left in: it stopped at the bare "/"
        self.assertIn(P + "/ip serv", old_trim(text, CMD))

    def test_redraw_echo_is_dropped(self):
        for cmd, answer in CLEAN_CASES:
            self.assertEqual(mikrotik._command_answer(echo_649(cmd, answer), cmd), answer, cmd)

    def test_single_echo_trims_exactly_as_before(self):
        for cmd, answer in CLEAN_CASES:
            text = echo_645(cmd, answer)
            self.assertEqual(mikrotik._command_answer(text, cmd), answer, cmd)
            self.assertEqual(mikrotik._command_answer(text, cmd), old_trim(text, cmd), cmd)

    def test_inside_a_menu(self):
        # after `/ip service` the prompt is "[ops@hex-lite] /ip service> "
        sub = "[ops@hex-lite] /ip service> "
        text = echo_649("print", ANSWER, prompt=sub)
        self.assertEqual(mikrotik._command_answer(text, "print"), ANSWER)

    def test_prompt_only_lines_are_dropped(self):
        text = echo_645(CMD, "line one\n" + P + "\n" + P.rstrip().ljust(WIDTH - 1) + "\nline two")
        self.assertEqual(mikrotik._command_answer(text, CMD), "line one\nline two")

    def test_a_write_with_a_stray_keystroke(self):
        # a 6.49.8 write came back as a lone "/" (a piece of the typed command)
        cmd = "/ip dhcp-server lease make-static [find address=192.0.2.10]"
        text = echo_649(cmd, "").replace("\n\n" + P.ljust(WIDTH - 1), "\n\n/\n\n" + P.ljust(WIDTH - 1))
        self.assertIn("\n/\n", text)
        self.assertEqual(mikrotik._command_answer(text, cmd), "")
        # cut short while the echo was still coming in: no echo wall either
        part = "\n".join(echo_649(cmd, "").split("\n")[:12] + [P.rstrip()])
        self.assertEqual(mikrotik._command_answer(part, cmd), "")

    def test_whole_session_text(self):
        # login banner, the login prompt with the first character typed after it,
        # the redraws, the answer
        session = BANNER + P + echo_649(CMD, ANSWER)
        self.assertEqual(mikrotik._command_answer(session, CMD), ANSWER)


class TestRunConsoleEcho(unittest.TestCase):
    def _run(self, res):
        with mock.patch.object(mikrotik, "mactelnet_available", return_value=True), \
             mock.patch.object(mikrotik, "mt_run", return_value=res):
            return mikrotik.run_console(mac=MAC, user="ops", command=CMD)

    def test_the_per_command_answer_is_used(self):
        r = self._run({"ok": True, "output": BANNER + P + echo_649(CMD, ANSWER),
                       "per": [{"cmd": CMD, "out": ANSWER}]})
        self.assertEqual((r["ok"], r["source"], r["output"]), (True, "mactelnet", ANSWER))

    def test_a_cut_off_session_never_hands_back_the_echo(self):
        # no per-command answer (cut off at the deadline): the whole session text
        r = self._run({"ok": True, "output": BANNER + P + echo_649(CMD, ANSWER), "per": []})
        self.assertEqual(r["output"], ANSWER)


STATUS_ANSWERS = {
    "identity": "__NW__|identity|hex-lite",
    "version": "__NW__|version|6.49.8 (long-term)",
    "board": "__NW__|board|hEX lite",
    "uptime": "__NW__|uptime|3d04:05:06",
    "cpu": "__NW__|cpu|4",
    "free_memory": "__NW__|free_memory|39321600",
    "total_memory": "__NW__|total_memory|67108864",
    "arch": "__NW__|arch|mipsbe",
    "model": "__NW__|model|RB750r2",
    "serial": "__NW__|serial|AB12CD34EF56",
    "firmware": "__NW__|firmware|6.49.8",
    "iface": "__NW__|iface|ether1~ether~true~false~1000~2000\n\n__NW__|iface|ether2~ether~false~false~0~0",
    "addr": "__NW__|addr|192.0.2.1/24~bridge",
}


def _status_answer(line):
    """The canned answer to one of _mt_script_lines() (keyed by its marker)."""
    return next(v for k, v in STATUS_ANSWERS.items() if "|%s|" % k in line)


class TestMtStatusThroughEcho(unittest.TestCase):
    def test_markers_parse_through_the_redraws(self):
        # every echo line carries "__NW__|key|" too, but after the prompt — only
        # the router's own output lines start with the marker
        session = BANNER + P + "".join(echo_649(c, _status_answer(c)) for c in mikrotik._mt_script_lines())
        with mock.patch.object(mikrotik, "mt_run", return_value={"ok": True, "output": session, "per": []}):
            s = mikrotik.mt_status(MAC, "ops", "")
        self.assertTrue(s["ok"], s)
        self.assertEqual((s["identity"], s["version"], s["model"], s["serial"], s["arch"]),
                         ("hex-lite", "6.49.8 (long-term)", "RB750r2", "AB12CD34EF56", "mipsbe"))
        self.assertEqual([(i["name"], i["running"]) for i in s["interfaces"]],
                         [("ether1", True), ("ether2", False)])
        self.assertEqual(s["addresses"], [{"address": "192.0.2.1/24", "interface": "bridge",
                                           "disabled": False}])


# A stand-in for the mac-telnet binary + a RouterOS console, for mt_run under a
# real pty. "6.49" redraws the whole input line for every typed character the way
# a hEX lite on 6.49.8 does under the +cte login; "6.45" echoes each one once.
FAKE_CONSOLE = r'''
import os, sys, tty
P = "[ops@hex-lite] > "
fd = sys.stdin.fileno()
tty.setraw(fd)


def w(s):
    os.write(1, s.encode())


w("Connecting to %s...done\r\n\r\n\r\n  MikroTik RouterOS 6.49.8 (c) 1999-2023"
  "       https://www.mikrotik.com/\r\n\r\n\r\n" % sys.argv[1])
w(P)
line = ""
while True:
    try:
        b = os.read(fd, 1)
    except OSError:
        break
    if not b:
        break
    ch = b.decode("latin-1")
    if ch != "\r":
        prev, line = line, line + ch
        if MODE == "6.45" or not prev:
            w(ch)
        else:
            w("\r\n\r" + (P + prev).ljust(199) + "\r" + P + line)
        continue
    if MODE == "6.49":
        w("\r\n\r" + (P + line).ljust(199) + "\r" + P + line + "\r" + P + line + "\r\n\r\n")
    else:
        w("\r\n")
    if line == "/quit":
        w("interrupted\r\n")
        break
    ans = ANSWERS.get(line, "")
    w((ans.replace("\n", "\r\n") + "\r\n" if ans else "") + "\r\n" + P)
    line = ""
'''


@unittest.skipUnless(sys.platform.startswith("linux"), "needs a Linux pty")
class TestMacTelnetPty(unittest.TestCase):
    """mt_run itself — pty, prompt detection, per-command capture — against the
    stand-in console: the whole path the Terminal tab uses, no router involved."""

    def _console(self, mode, answers):
        path = os.path.join(tempfile.mkdtemp(), "mactelnet")
        with open(path, "w") as f:
            f.write("#!%s\nMODE = %r\nANSWERS = %r\n%s" % (sys.executable, mode, answers, FAKE_CONSOLE))
        os.chmod(path, 0o755)
        p = mock.patch.object(mikrotik, "MACTELNET_BIN", path)
        p.start()
        self.addCleanup(p.stop)

    def test_terminal_on_6_49_shows_only_the_answer(self):
        self._console("6.49", {CMD: ANSWER})
        r = mikrotik.run_console(mac=MAC, user="ops", password="", command=CMD)
        self.assertEqual((r.get("ok"), r.get("source")), (True, "mactelnet"), r)
        self.assertEqual(r["output"], ANSWER)

    def test_terminal_on_6_45_is_unchanged(self):
        self._console("6.45", {CMD: ANSWER})
        r = mikrotik.run_console(mac=MAC, user="ops", password="", command=CMD)
        self.assertEqual(r.get("output"), ANSWER, r)

    def test_status_by_mac_on_6_49(self):
        lines = [mikrotik._mt_script_lines()[0], mikrotik._mt_script_lines()[-2]]   # identity + interfaces
        self._console("6.49", {c: _status_answer(c) for c in lines})
        with mock.patch.object(mikrotik, "_mt_script_lines", return_value=lines):
            s = mikrotik.mt_status(MAC, "ops", "")
        self.assertTrue(s.get("ok"), s)
        self.assertEqual(s["identity"], "hex-lite")
        self.assertEqual([i["name"] for i in s["interfaces"]], ["ether1", "ether2"])


# --- "API turned off": open it to this Pi only -----------------------------------
PI = "192.0.2.10"


class TestApiOffAdvice(unittest.TestCase):
    def test_commands_open_the_api_to_this_pi_only(self):
        self.assertEqual(mikrotik.api_allow_commands(PI),
                         ["/ip service set api disabled=no address=192.0.2.10/32",
                          "/ip dhcp-server lease make-static [find address=192.0.2.10]"])

    def test_no_usable_pi_ip_gets_a_placeholder(self):
        for bad in ("", None, "2001:db8::1", "192.0.2.300", "x; /system reset"):
            allow, lease = mikrotik.api_allow_commands(bad)
            self.assertEqual(allow, "/ip service set api disabled=no address=<this Pi's IP>/32", bad)

    def test_hint_never_recommends_opening_it_to_everyone(self):
        for has_login in (True, False):
            h = mikrotik.api_off_hint(PI, has_login)
            self.assertIn("run  /ip service set api disabled=no address=192.0.2.10/32  ", h)
            self.assertIn("/ip dhcp-server lease make-static [find address=192.0.2.10]", h)
            self.assertEqual(h.count("enable api"), 1)       # only in the warning
            self.assertIn("Don't use  /ip service enable api  — that opens port 8728 to every address", h)
        self.assertIn("Save the router's login first", mikrotik.api_off_hint(PI, False))
        self.assertNotIn("Save the router's login first", mikrotik.api_off_hint(PI, True))
        self.assertIn("address=<this Pi's IP>/32", mikrotik.api_off_hint(""))

    def test_own_ip_towards(self):
        for ip in ("not-an-ip", "", None, "2001:db8::1", "127.0.0.1"):
            self.assertEqual(mikrotik.own_ip_towards(ip), "", ip)   # never loopback
        got = mikrotik.own_ip_towards("192.0.2.1")                  # "" when there's no route
        self.assertTrue(got == "" or (mikrotik._is_ipv4(got) and not got.startswith("127.")), got)

    def test_the_router_card_text_matches(self):
        # routerview.js (site page AND hub — the hub image copies the same file)
        path = os.path.join(os.environ.get("NETWATCH_APP", "/app"), "static", "routerview.js")
        with open(path, encoding="utf-8") as f:
            line = next(x for x in f if x.strip().startswith("api_off:"))
        self.assertIn("/ip service set api disabled=no address={pi}/32", line)
        self.assertEqual(line.count("enable api"), 1)
        self.assertIn("Not  /ip service enable api  — that opens 8728 to every address", line)


class TestReportRouteAdvice(unittest.TestCase):
    """The site routes that tell the operator about a router whose API refuses."""
    KEY = MAC

    def setUp(self):
        import server      # the site app: only these tests need it
        import siteauth
        self.server = server
        hub = "k" * 43
        siteauth.set_hub_key(hub)
        self.hdr = {siteauth.HEADER: hub}
        self.dev = {"key": self.KEY, "mac": self.KEY, "ip": "192.0.2.1", "online": True, "name": "Gateway"}
        for p in (mock.patch.object(server, "_mtk_target",
                                    return_value=(self.dev, self.KEY, "192.0.2.1", "ops", "pw")),
                  mock.patch.object(server.creds, "get",
                                    return_value={"username": "ops", "password": "pw", "notes": ""}),
                  mock.patch.object(server.mikrotik, "api_report",
                                    return_value={"ok": False, "error": "[Errno 111] Connection refused"}),
                  mock.patch.object(server.mikrotik, "own_ip_towards", return_value=PI),
                  mock.patch.object(server, "_mndp_map", return_value={})):
            p.start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(server._ROUTER_CACHE.pop, self.KEY, None)

    def test_report_names_this_pi(self):
        r = self.server.app.test_client().get("/api/devices/%s/mikrotik/report" % self.KEY,
                                              headers=self.hdr, environ_base={"REMOTE_ADDR": "192.168.0.50"})
        j = r.get_json()
        self.assertEqual((r.status_code, j["ok"], j["kind"], j["pi_ip"]), (200, False, "api_off", PI))
        self.assertEqual(j["fix"], mikrotik.api_allow_commands(PI))
        self.assertEqual(j["hint"], mikrotik.api_off_hint(PI, True))

    def test_router_card_carries_the_pi_ip(self):
        v = self.server._router_view(self.dev, force=True)
        self.assertEqual((v["ok"], v["kind"], v["pi_ip"]), (False, "api_off", PI))


if __name__ == "__main__":
    unittest.main(verbosity=2)
