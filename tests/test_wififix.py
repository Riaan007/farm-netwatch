"""Safe Wi-Fi fixes (app/wififix.py): planning rules, and the script that runs ON
the radio executed for real under /bin/sh with airOS stand-ins (wstalist,
rc.softrestart, cfgmtd), end to end through the job runner.

Run inside the site image (it writes /tmp/system.cfg and /usr/etc/rc.d — a
throwaway container only):
  docker run --rm -v "$PWD/app:/app" -v "$PWD/tests:/tests" -e NETWATCH_DATA=/tmp/nw \
    --entrypoint python farm-netwatch:netcfg /tests/test_wififix.py -v
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("NETWATCH_APP", "/app"))

import airos      # noqa: E402
import wififix    # noqa: E402

LOGIN = {"username": "ubnt", "password": "pw"}
STUBS = tempfile.mkdtemp()
PEERS_FILE = os.path.join(STUBS, "peers")      # how many peers wstalist reports
FLASH = os.path.join(STUBS, "flash")           # what cfgmtd "wrote"
ASSOC_FILE = os.path.join(STUBS, "assoc")      # peers' connection time (s) wstalist reports
LIVE_FILE = os.path.join(STUBS, "live")        # the frequency (MHz) the radio runs
FMT_FILE = os.path.join(STUBS, "fmt")          # printf format mca-status uses for it (default "freq=%s")
IW_FILE = os.path.join(STUBS, "iw")            # present = iwconfig reports it too ("Frequency:5.745 GHz")
STUCK_FILE = os.path.join(STUBS, "stuck")      # present = the restart drops the link but keeps the old channel
NOOP_FILE = os.path.join(STUBS, "noop")        # present = soft restart applies nothing
DROP_FILE = os.path.join(STUBS, "drop")        # wstalist answers "no peers" this many more times
TX_FILE = os.path.join(STUBS, "tx")            # present = the radio reports its live transmit power (dBm)
TXCAP_FILE = os.path.join(STUBS, "txcap")      # the most the country limit lets it transmit
NODROP_FILE = os.path.join(STUBS, "nodrop")    # present = the restart applies without dropping the link
                                               # (a power change on an airOS 6 LiteBeam M5, Tri-Lectro)


def cfg_text(over=None):
    base = {"netconf.1.ip": "192.168.1.20", "wireless.1.mode": "ap-ptmp", "wireless.1.ssid": "Tower",
            "radio.1.freq": "5480", "radio.1.center.1.freq": "5490", "radio.1.chanbw": "40", "radio.1.txpower": "18", "radio.1.obey": "enabled",
            "radio.1.polling_11ac_11n_compat": "1"}
    base.update(over or {})
    return "\n".join(f"{k}={v}" for k, v in base.items() if v is not None) + "\n"


def wsta(n, model="PowerBeam 5AC"):
    return json.dumps([{"mac": f"0c:ea:14:00:00:0{i}", "lastip": f"192.168.1.3{i}",
                        "remote": {"hostname": f"Sta{i}", "platform": model}} for i in range(n)])


def ssh_out(cfg, peers, mode="ap-ptmp-ac", freq="5480", tx="18", center="5490", probe="1\n/sbin/cfgmtd\nXW.v6.1.7",
            livefreq=None):
    status = f"wlanOpmode={mode}\nfreq={freq}\nwlanTxPower={tx}\n" + (f"centerFreq={center}\n" if center else "")
    lf = f"mca={freq}\n" if livefreq is None else livefreq       # what the probe's livefreq printed
    return (f"{status}{airos._CFG_MARK}\n{cfg}{airos._WSTA_MARK}\n{peers}\n{wififix._PROBE_MARK}\n{probe}\n"
            f"{wififix._FREQ_MARK}\n{lf}")


def m5_cfg(over=None):
    """An airOS 6 NanoBeam M5 AP like Tri-Lectro's office link: 40 MHz as control + extension."""
    base = {"netconf.1.ip": "192.168.1.11", "wireless.1.mode": "master", "wireless.1.ssid": "LAN-OFFICE-PTP",
            "radio.1.freq": "5180", "radio.1.ieee_mode": "11naht40plus", "radio.1.chanbw": "40",
            "radio.1.txpower": "20", "radio.1.obey": "enabled", "radio.countrycode": "826",
            "radio.1.countrycode": "826", "system.timezone": "GMT", "ntpclient.status": "disabled"}
    base.update(over or {})
    return "\n".join(f"{k}={v}" for k, v in base.items() if v is not None) + "\n"


class FakeRadios:
    """ip -> (cfg text, wstalist json, live status kwargs). Runs the uploaded
    fix script for real when the job starts it with bgnd."""

    def __init__(self, radios):
        self.radios, self.commands = radios, []

    def __call__(self, ip, user, password, command, timeout=20, stdin_data=None):
        self.commands.append(command)
        if ip not in self.radios:
            return 255, "", "ssh: connect to host port 22: No route to host"
        cfg, peers, live = self.radios[ip]
        if command.startswith("mca-status"):
            return 0, ssh_out(cfg, peers, **live), ""
        if command.startswith("cat > /tmp/nwfix.sh"):
            with open("/tmp/system.cfg", "w") as f:
                f.write(cfg)
            with open("/tmp/nwfix.sh", "w") as f:
                f.write(stdin_data)
            subprocess.run(["/bin/sh", "/tmp/nwfix.sh"], env={**os.environ, "PATH": f"{STUBS}:/usr/bin:/bin"},
                           timeout=60, check=False)
            with open("/tmp/system.cfg") as f:
                cfg_after = f.read()
            if os.path.exists(LIVE_FILE):                # what the radio RUNS after the script
                live = {**live, "freq": open(LIVE_FILE).read().strip()}
            self.radios[ip] = (cfg_after, peers, live)
            return 0, "STARTED\n", ""
        if command.startswith("cat /tmp/nwfix.state"):
            with open("/tmp/nwfix.state") as f:
                return 0, f.read(), ""
        return 0, "", ""


def stubs():
    os.makedirs("/usr/etc/rc.d", exist_ok=True)
    w = lambda p, body: (open(p, "w").write("#!/bin/sh\n" + body + "\n"), os.chmod(p, 0o755))  # noqa: E731
    w(os.path.join(STUBS, "wstalist"), f'd=$(cat {DROP_FILE} 2>/dev/null || echo 0); '
                                       f'if [ "$d" -gt 0 ]; then echo $((d-1)) > {DROP_FILE}; printf "[]"; exit 0; fi; '
                                       f'n=$(cat {PEERS_FILE}); u=$(cat {ASSOC_FILE} 2>/dev/null || echo 5000); i=0; printf "["; '
                                       f'while [ $i -lt $n ]; do printf \'{{"uptime": %s, "remote":{{}}}},\' $u; i=$((i+1)); done; printf "]"')
    # mca-status reports the frequency the radio RUNS: what the last soft restart applied,
    # in the format FMT_FILE gives (airOS builds differ); iwconfig reports it in GHz
    w(os.path.join(STUBS, "mca-status"), f'[ -f {TX_FILE} ] && echo "wlanTxPower=$(cat {TX_FILE})"; '
                                         f'[ -f {LIVE_FILE} ] || exit 0; f=$(cat {LIVE_FILE}); '
                                         f'if [ -f {FMT_FILE} ]; then printf "$(cat {FMT_FILE})" "$f"; else echo "freq=$f"; fi')
    w(os.path.join(STUBS, "iwconfig"), f'[ -f {IW_FILE} ] || exit 0; '
                                       f'echo "ath0  IEEE 802.11na  ESSID:\\"LAN\\""; '
                                       f'if [ -f {LIVE_FILE} ]; then f=$(cat {LIVE_FILE}); '
                                       f'echo "      Mode:Master  Frequency:${{f%???}}.${{f#?}} GHz  Access Point: 18:E8:29:30:39:00"; fi; '
                                       f'[ -f {TX_FILE} ] && echo "      Bit Rate:144.4 Mb/s   Tx-Power=$(cat {TX_FILE}) dBm"; exit 0')
    w(os.path.join(STUBS, "sleep"), "exit 0")
    w(os.path.join(STUBS, "cfgmtd"), f'cp /tmp/running.cfg {FLASH}')
    # a real test-mode restart re-associates the link (connection time resets) and makes the
    # config live; NOOP_FILE makes it a radio that only "accepts" the command
    # airOS 6 (like the real Tri-Lectro radios): the link drops for a while and comes back,
    # and wstalist "uptime" does NOT reset (it is the peer's own uptime)
    w("/usr/etc/rc.d/rc.softrestart", f'[ -f {NOOP_FILE} ] && exit 0; [ -f {NODROP_FILE} ] || echo 1 > {DROP_FILE}; '
                                       f'[ -f {STUCK_FILE} ] || sed -n "s/^radio.1.freq=//p" /tmp/system.cfg > {LIVE_FILE}; '
                                       f'if [ -f {TX_FILE} ]; then p=$(sed -n "s/^radio.1.txpower=//p" /tmp/system.cfg); '
                                       f'c=$(cat {TXCAP_FILE} 2>/dev/null || echo 99); [ "$p" -gt "$c" ] && p=$c; echo $p > {TX_FILE}; fi; exit 0')


def set_peers(n):
    with open(PEERS_FILE, "w") as f:
        f.write(str(n))


class Planning(unittest.TestCase):
    def setUp(self):
        self._ssh = airos._ssh

    def tearDown(self):
        airos._ssh = self._ssh

    def tgt(self, ip="10.0.0.1"):
        return {"key": "ap", "ip": ip, "name": "Tower AP"}

    def test_ac_mode_plan(self):
        airos._ssh = FakeRadios({"10.0.0.1": (cfg_text(), wsta(2), {})})
        p = wififix.plan("ac_mode", self.tgt(), lambda k: LOGIN)
        self.assertEqual(p["edits"], [{"key": "radio.1.polling_11ac_11n_compat", "old": "1", "new": "0"}])
        self.assertEqual((p["min_peers"], p["wait_s"]), (2, 120))

    def test_ac_mode_refused_for_old_station_or_already_ac(self):
        airos._ssh = FakeRadios({"10.0.0.1": (cfg_text(), wsta(1, "NanoStation M5"), {})})
        with self.assertRaisesRegex(wififix.FixError, "not AC radios"):
            wififix.plan("ac_mode", self.tgt(), lambda k: LOGIN)
        airos._ssh = FakeRadios({"10.0.0.1": (cfg_text({"radio.1.polling_11ac_11n_compat": "0"}), wsta(1), {})})
        with self.assertRaisesRegex(wififix.FixError, "already in airMAX AC"):
            wififix.plan("ac_mode", self.tgt(), lambda k: LOGIN)

    def test_no_peers_means_no_change(self):
        airos._ssh = FakeRadios({"10.0.0.1": (cfg_text(), "[]", {})})
        with self.assertRaisesRegex(wififix.FixError, "no wireless peer"):
            wififix.plan("ac_mode", self.tgt(), lambda k: LOGIN)

    def test_power_matches_the_other_end(self):
        airos._ssh = FakeRadios({"10.0.0.1": (cfg_text({"radio.1.txpower": "14"}), wsta(1), {"tx": "14"}),
                                 "10.0.0.2": (cfg_text({"radio.1.txpower": "22"}), wsta(1), {})})
        p = wififix.plan("power", self.tgt(), lambda k: LOGIN,
                         params={"match": {"key": "sta", "ip": "10.0.0.2", "name": "Gate"}})
        self.assertEqual(p["edits"], [{"key": "radio.1.txpower", "old": "14", "new": "22"}])
        self.assertTrue(any("interference" in n for n in p["notes"]))

    def test_missing_key_is_refused_not_guessed(self):
        airos._ssh = FakeRadios({"10.0.0.1": (cfg_text({"radio.1.freq": None}), wsta(1), {})})
        with self.assertRaisesRegex(wififix.FixError, "current channel"):
            wififix.plan("channel", self.tgt(), lambda k: LOGIN, params={"freq": 5500})

    def test_channel_blocked_by_a_station_scan_list(self):
        sta_cfg = cfg_text({"wireless.1.mode": "sta", "radio.1.scan_list.status": "enabled", "radio.1.scan_list.channels": "5480,5500"})
        airos._ssh = FakeRadios({"10.0.0.1": (cfg_text(), wsta(1), {}), "192.168.1.30": (sta_cfg, wsta(1), {})})
        find = lambda mac=None, ip=None: {"key": "s0", "ip": ip, "name": "Sta0"}   # noqa: E731
        with self.assertRaisesRegex(wififix.FixError, "would not follow"):
            wififix.plan("channel", self.tgt(), lambda k: LOGIN, find, {"freq": 5660})
        p = wififix.plan("channel", self.tgt(), lambda k: LOGIN, find, {"freq": 5500})
        self.assertEqual(p["edits"], [{"key": "radio.1.center.1.freq", "old": "5490", "new": "5500"},
                                      {"key": "radio.1.freq", "old": "5480", "new": "5490"}])
        self.assertEqual(p["wait_s"], 300)                       # 5500 is DFS

    def test_channel_needs_consent_for_unreadable_stations(self):
        airos._ssh = FakeRadios({"10.0.0.1": (cfg_text(), wsta(1), {})})
        with self.assertRaisesRegex(wififix.FixError, "cannot read these stations"):
            wififix.plan("channel", self.tgt(), lambda k: LOGIN, lambda **k: None, {"freq": 5180})
        p = wififix.plan("channel", self.tgt(), lambda k: LOGIN, lambda **k: None,
                         {"freq": 5180, "accept_unverified": True})
        self.assertEqual(p["wait_s"], 120)
        self.assertTrue(p["notes"])

    def test_channel_moves_centre_and_control_together(self):
        """Tweespruit 2026-10-26: only radio.1.freq was changed, the centre stayed, every move was undone."""
        airos._ssh = FakeRadios({"10.0.0.1": (cfg_text({"radio.1.freq": "5490", "radio.1.center.1.freq": "5500"}),
                                              wsta(1), {"freq": "5490", "center": "5500"})})
        p = wififix.plan("channel", self.tgt(), lambda k: LOGIN, lambda **k: None,
                         {"freq": 5755, "accept_unverified": True})
        self.assertEqual(p["edits"], [{"key": "radio.1.center.1.freq", "old": "5500", "new": "5755"},
                                      {"key": "radio.1.freq", "old": "5490", "new": "5745"}])
        self.assertIn("Centre frequency: 5500 → 5755 MHz", p["lines"][0])
        self.assertEqual(p["wait_s"], 120)                       # 5735-5775 is not DFS
        body = wififix.script(p["edits"], 1, 10)                 # both keys pass the script's whitelist
        self.assertIn("radio.1.center.1.freq=5755", body)

    def test_channel_width_change_puts_control_inside_the_channel(self):
        airos._ssh = FakeRadios({"10.0.0.1": (cfg_text(), wsta(1), {})})
        p = wififix.plan("channel", self.tgt(), lambda k: LOGIN, lambda **k: None,
                         {"freq": 5200, "width": 20, "accept_unverified": True})
        self.assertEqual([(e["key"], e["new"]) for e in p["edits"]],
                         [("radio.1.center.1.freq", "5200"), ("radio.1.freq", "5200"), ("radio.1.chanbw", "20")])

    def test_channel_without_centre_key(self):
        # 20 MHz, firmware with no separate centre: control IS the centre, one key changes
        airos._ssh = FakeRadios({"10.0.0.1": (cfg_text({"radio.1.center.1.freq": None, "radio.1.chanbw": "20"}),
                                              wsta(1), {"center": "5480"})})
        p = wififix.plan("channel", self.tgt(), lambda k: LOGIN, lambda **k: None,
                         {"freq": 5200, "accept_unverified": True})
        self.assertEqual(p["edits"], [{"key": "radio.1.freq", "old": "5480", "new": "5200"}])
        # 40 MHz and the radio reports a centre its config does not hold: refuse, never guess
        airos._ssh = FakeRadios({"10.0.0.1": (cfg_text({"radio.1.center.1.freq": None}), wsta(1), {})})
        with self.assertRaisesRegex(wififix.FixError, "centre frequency its config does not hold"):
            wififix.plan("channel", self.tgt(), lambda k: LOGIN, lambda **k: None,
                         {"freq": 5200, "accept_unverified": True})

    def test_channel_outside_the_band_is_refused(self):
        airos._ssh = FakeRadios({"10.0.0.1": (cfg_text(), wsta(1), {})})
        with self.assertRaisesRegex(wififix.FixError, "allowed 5 GHz ranges"):
            wififix.plan("channel", self.tgt(), lambda k: LOGIN, lambda **k: None,
                         {"freq": 5400, "accept_unverified": True})

    def test_country_plan_sets_both_keys_and_power(self):
        airos._ssh = FakeRadios({"10.0.0.1": (m5_cfg(), wsta(1, "NanoBeam M5"), {"center": None, "freq": "5180"})})
        p = wififix.plan("country", self.tgt(), lambda k: LOGIN, params={"country": 710, "txpower": 10})
        self.assertEqual({e["key"]: e["new"] for e in p["edits"]},
                         {"radio.countrycode": "710", "radio.1.countrycode": "710", "radio.1.txpower": "10"})
        self.assertTrue(p["verify_restart"])
        self.assertIn("United Kingdom → South Africa", p["lines"][0])
        with self.assertRaisesRegex(wififix.FixError, "already set"):
            airos._ssh = FakeRadios({"10.0.0.1": (m5_cfg({"radio.countrycode": "710", "radio.1.countrycode": "710"}),
                                                  wsta(1), {"center": None})})
            wififix.plan("country", self.tgt(), lambda k: LOGIN, params={"country": 710})

    def test_power_direct_value(self):
        # Tri-Lectro gate link 2026-11-07: one end 7 dBm, the other 21 — set both to a chosen value
        airos._ssh = FakeRadios({"10.0.0.1": (m5_cfg({"radio.1.txpower": "7", "radio.1.antenna.gain": "23"}),
                                              wsta(1, "LiteBeam M5"), {"center": None, "tx": "7"})})
        p = wififix.plan("power", self.tgt(), lambda k: LOGIN, params={"dbm": 14})
        self.assertEqual(p["edits"], [{"key": "radio.1.txpower", "old": "7", "new": "14"}])
        self.assertEqual(p["verify_power"], 14)
        self.assertFalse(p.get("verify_restart"))
        self.assertEqual(p["label"], "Set transmit power")
        self.assertTrue(any("37 dBm EIRP" in n for n in p["notes"]), p["notes"])
        self.assertIn("radio.1.txpower", p["radio_cfg"])
        for bad in (0, 28, "x"):
            with self.assertRaisesRegex(wififix.FixError, "1–27"):
                wififix.plan("power", self.tgt(), lambda k: LOGIN, params={"dbm": bad})
        airos._ssh = FakeRadios({"10.0.0.1": (m5_cfg({"radio.1.txpower": "14"}), wsta(1), {"center": None})})
        with self.assertRaisesRegex(wififix.FixError, "already set to 14"):
            wififix.plan("power", self.tgt(), lambda k: LOGIN, params={"dbm": 14})

    def test_power_direct_turns_country_limits_back_on_only_when_asked(self):
        over = {"radio.1.txpower": "21", "radio.1.obey": "disabled", "radio.1.reg_obey": "disabled"}
        airos._ssh = FakeRadios({"10.0.0.1": (m5_cfg(over), wsta(1), {"center": None})})
        with self.assertRaisesRegex(wififix.FixError, "does not obey"):
            wififix.plan("power", self.tgt(), lambda k: LOGIN, params={"dbm": 14})
        p = wififix.plan("power", self.tgt(), lambda k: LOGIN, params={"dbm": 14, "obey": True})
        self.assertEqual({e["key"]: e["new"] for e in p["edits"]},
                         {"radio.1.txpower": "14", "radio.1.obey": "enabled", "radio.1.reg_obey": "enabled"})
        # same power, limits off: switching them on alone is still a change
        airos._ssh = FakeRadios({"10.0.0.1": (m5_cfg(dict(over, **{"radio.1.txpower": "14"})), wsta(1), {"center": None})})
        p = wififix.plan("power", self.tgt(), lambda k: LOGIN, params={"dbm": 14, "obey": True})
        self.assertEqual([e["key"] for e in p["edits"]], ["radio.1.obey", "radio.1.reg_obey"])

    def test_power_direct_needs_test_mode(self):
        airos._ssh = FakeRadios({"10.0.0.1": (m5_cfg(), wsta(1), {"center": None, "probe": "0\n/sbin/cfgmtd"})})
        with self.assertRaisesRegex(wififix.FixError, "test mode"):
            wififix.plan("power", self.tgt(), lambda k: LOGIN, params={"dbm": 10})

    def test_airos6_fixes_need_test_mode_on_the_radio(self):
        for probe in ("0\n/sbin/cfgmtd", "1\n", ""):
            airos._ssh = FakeRadios({"10.0.0.1": (m5_cfg(), wsta(1), {"center": None, "probe": probe})})
            with self.assertRaisesRegex(wififix.FixError, "test mode"):
                wififix.plan("country", self.tgt(), lambda k: LOGIN, params={"country": 710})

    def test_ntp_plan_adds_missing_keys_only_for_ntp(self):
        airos._ssh = FakeRadios({"10.0.0.1": (m5_cfg(), wsta(1), {"center": None})})
        p = wififix.plan("ntp", self.tgt(), lambda k: LOGIN, params={})
        got = {e["key"]: (e["new"], e.get("add", False)) for e in p["edits"]}
        self.assertEqual(got, {"ntpclient.status": ("enabled", False), "ntpclient.1.status": ("enabled", True),
                               "ntpclient.1.server": ("pool.ntp.org", True), "system.timezone": ("GMT-2", False)})
        with self.assertRaises(wififix.FixError):
            wififix.plan("ntp", self.tgt(), lambda k: LOGIN, params={"server": "pool.ntp.org; reboot"})

    def test_airos6_channel_moves_to_20_mhz_only(self):
        airos._ssh = FakeRadios({"10.0.0.1": (m5_cfg(), wsta(1, "NanoBeam M5"),
                                              {"center": "5190", "freq": "5180", "mode": "ap"})})
        p = wififix.plan("channel", self.tgt(), lambda k: LOGIN, lambda **k: None,
                         {"freq": 5745, "width": 20, "accept_unverified": True})
        self.assertEqual({e["key"]: e["new"] for e in p["edits"]},
                         {"radio.1.freq": "5745", "radio.1.ieee_mode": "11naht20", "radio.1.chanbw": "20"})
        self.assertEqual(p["verify_freq"], 5745)
        self.assertTrue(any("Live-channel check tried on the radio first: mca-status 5180 MHz" in n for n in p["notes"]), p["notes"])
        self.assertEqual(p["radio_cfg"]["radio.1.ieee_mode"], "11naht40plus")
        self.assertFalse(any(not k.startswith("radio.") for k in p["radio_cfg"]))
        with self.assertRaisesRegex(wififix.FixError, "20 MHz"):
            wififix.plan("channel", self.tgt(), lambda k: LOGIN, lambda **k: None,
                         {"freq": 5755, "width": 40, "accept_unverified": True})

    def test_airos6_channel_refused_when_the_live_reading_fails(self):
        """The check that proves a move must first read the channel the radio runs NOW."""
        for lf in ("", "mca=5745\n", "iw=5185\n"):
            airos._ssh = FakeRadios({"10.0.0.1": (m5_cfg(), wsta(1, "NanoBeam M5"),
                                                  {"center": "5190", "freq": "5180", "mode": "ap", "livefreq": lf})})
            with self.assertRaisesRegex(wififix.FixError, "cannot read NanoBeam's live channel"):
                wififix.plan("channel", {"key": "m5", "ip": "10.0.0.1", "name": "NanoBeam"}, lambda k: LOGIN,
                             lambda **k: None, {"freq": 5745, "width": 20, "accept_unverified": True})
        airos._ssh = FakeRadios({"10.0.0.1": (m5_cfg(), wsta(1, "NanoBeam M5"),
                                              {"center": "5190", "freq": "5180", "mode": "ap", "livefreq": "iw=5180\n"})})
        p = wififix.plan("channel", {"key": "m5", "ip": "10.0.0.1", "name": "NanoBeam"}, lambda k: LOGIN,
                         lambda **k: None, {"freq": 5745, "width": 20, "accept_unverified": True})
        self.assertTrue(any("iwconfig 5180 MHz" in n for n in p["notes"]))

    def test_freqs_parser(self):
        self.assertEqual(wififix._freqs("iw=5745 mca=5745 "), {"iw": 5745, "mca": 5745})
        self.assertEqual(wififix._freqs("iw= mca=abc x=5745 mca=99"), {})

    def test_script_rejects_unsafe_edits(self):
        with self.assertRaises(wififix.FixError):
            wififix.script([{"key": "netconf.1.ip", "old": "1", "new": "2"}], 1, 10)
        with self.assertRaises(wififix.FixError):
            wififix.script([{"key": "radio.1.freq", "old": "1", "new": "5480; reboot"}], 1, 10)
        with self.assertRaises(wififix.FixError):
            wififix.script([{"key": "system.timezone", "old": "GMT", "new": "GMT-2'; reboot; '"}], 1, 10)
        with self.assertRaises(wififix.FixError):     # only NTP keys may be added
            wififix.script([{"key": "radio.countrycode", "old": None, "new": "710", "add": True}], 1, 10)


class OnTheRadio(unittest.TestCase):
    """The generated script, run for real, through the job runner."""

    @classmethod
    def setUpClass(cls):
        stubs()

    def setUp(self):
        self._ssh, self._poll = airos._ssh, wififix.POLL_S
        wififix.POLL_S = 0.05
        for p in (FLASH, "/tmp/nwfix.state", ASSOC_FILE, LIVE_FILE, NOOP_FILE, DROP_FILE, FMT_FILE, IW_FILE, STUCK_FILE,
                  TX_FILE, TXCAP_FILE, NODROP_FILE):
            if os.path.exists(p):
                os.remove(p)

    def tearDown(self):
        airos._ssh, wififix.POLL_S = self._ssh, self._poll

    def run_job(self, peers_after):
        radios = FakeRadios({"10.0.0.1": (cfg_text(), wsta(2), {})})
        airos._ssh = radios
        p = wififix.plan("ac_mode", {"key": "ap", "ip": "10.0.0.1", "name": "AP"}, lambda k: LOGIN)
        p["wait_s"] = 10
        set_peers(peers_after)
        done = []
        wififix.start(p, LOGIN, on_done=lambda plan, j: done.append(j))
        for _ in range(400):
            if done:
                break
            time.sleep(0.05)
        self.assertTrue(done, "job never finished")
        return done[0], radios

    def test_peers_back_means_saved(self):
        job, radios = self.run_job(peers_after=2)
        self.assertEqual(job["verdict"], "applied", job)
        self.assertTrue(job["ok"])
        self.assertIn("radio.1.polling_11ac_11n_compat=0", open(FLASH).read())
        self.assertIn("radio.1.polling_11ac_11n_compat=0", radios.radios["10.0.0.1"][0])
        self.assertTrue(any("bgnd -r nwfix" in c for c in radios.commands))

    def test_peers_missing_means_undone_and_nothing_saved(self):
        job, radios = self.run_job(peers_after=1)
        self.assertEqual(job["verdict"], "reverted", job)
        self.assertFalse(job["ok"])
        self.assertFalse(os.path.exists(FLASH))                              # flash never written
        self.assertIn("radio.1.polling_11ac_11n_compat=1", radios.radios["10.0.0.1"][0])   # config restored

    def test_changed_since_preview_stops_before_touching(self):
        radios = FakeRadios({"10.0.0.1": (cfg_text(), wsta(2), {})})
        airos._ssh = radios
        p = wififix.plan("ac_mode", {"key": "ap2", "ip": "10.0.0.1", "name": "AP"}, lambda k: LOGIN)
        radios.radios["10.0.0.1"] = (cfg_text({"radio.1.polling_11ac_11n_compat": "0"}), wsta(2), {})
        done = []
        wififix.start(p, LOGIN, on_done=lambda plan, j: done.append(j))
        for _ in range(100):
            if done:
                break
            time.sleep(0.05)
        self.assertEqual(done[0]["verdict"], "refused")
        self.assertFalse(any(c.startswith("cat > /tmp/nwfix.sh") for c in radios.commands))

    def run_m5(self, kind, params, noop=False, over=None):
        radios = FakeRadios({"10.0.0.1": (m5_cfg(over), wsta(1, "NanoBeam M5"),
                                          {"center": "5190", "freq": "5180", "mode": "ap"})})
        airos._ssh = radios
        p = wififix.plan(kind, {"key": "m5", "ip": "10.0.0.1", "name": "NanoBeam"}, lambda k: LOGIN,
                         lambda **k: None, params)
        p["wait_s"] = 10
        set_peers(1)
        if noop:
            open(NOOP_FILE, "w").close()
        done = []
        wififix.start(p, LOGIN, on_done=lambda plan, j: done.append(j))
        for _ in range(400):
            if done:
                break
            time.sleep(0.05)
        self.assertTrue(done, "job never finished")
        return done[0], radios

    def test_country_saved_only_after_the_link_really_restarted(self):
        job, radios = self.run_m5("country", {"country": 710})
        self.assertEqual(job["verdict"], "applied", job)
        self.assertIn("radio.countrycode=710", open(FLASH).read())

    def test_radio_without_a_real_test_restart_is_put_back(self):
        # the command "succeeds" but nothing re-associates: never save untested settings
        job, radios = self.run_m5("country", {"country": 710}, noop=True)
        self.assertEqual(job["verdict"], "reverted", job)
        self.assertIn("did not drop and re-associate", job["msg"])
        self.assertFalse(os.path.exists(FLASH))
        self.assertIn("radio.countrycode=826", radios.radios["10.0.0.1"][0])

    def test_airos6_channel_saved_when_live(self):
        job, radios = self.run_m5("channel", {"freq": 5745, "width": 20, "accept_unverified": True})
        self.assertEqual(job["verdict"], "applied", job)
        flash = open(FLASH).read()
        self.assertIn("radio.1.freq=5745", flash)
        self.assertIn("radio.1.ieee_mode=11naht20", flash)

    def test_airos6_channel_live_check_reads_other_formats(self):
        """Comma-separated, CR-terminated or GHz output, or iwconfig only: all prove the move."""
        for fmt, iw in (("deviceName=NB,deviceId=x,freq=%s,platform=NanoBeam M5\r\n", False),
                        ("wlanFreq=%s MHz\r\n", False), ("signal=-60 freq_unknown=%s\n", True)):
            for p in (FLASH, LIVE_FILE, FMT_FILE, IW_FILE):
                if os.path.exists(p):
                    os.remove(p)
            open(FMT_FILE, "w").write(fmt)
            if iw:
                open(IW_FILE, "w").close()
            job, radios = self.run_m5("channel", {"freq": 5745, "width": 20, "accept_unverified": True})
            self.assertEqual(job["verdict"], "applied", (fmt, job))
            self.assertTrue(any(" FREQ " in f" {ln} " and "=5745" in ln for ln in job["log"]), job["log"])
            self.assertTrue(any("FREQ0" in ln for ln in job["log"]), job["log"])
            self.assertIn("radio.1.freq=5745", open(FLASH).read())

    def test_airos6_channel_that_did_not_move_is_put_back_and_says_what_it_ran(self):
        open(STUCK_FILE, "w").close()
        open(LIVE_FILE, "w").write("5180")
        job, radios = self.run_m5("channel", {"freq": 5745, "width": 20, "accept_unverified": True})
        self.assertEqual(job["verdict"], "reverted", job)
        self.assertIn("frequency is not 5745", job["msg"])
        self.assertIn("mca=5180", job["msg"])
        self.assertFalse(os.path.exists(FLASH))
        self.assertIn("radio.1.freq=5180", radios.radios["10.0.0.1"][0])

    def test_power_direct_saved_with_limits_on(self):
        job, radios = self.run_m5("power", {"dbm": 14, "obey": True},
                                  over={"radio.1.txpower": "21", "radio.1.obey": "disabled"})
        self.assertEqual(job["verdict"], "applied", job)
        flash = open(FLASH).read()
        self.assertIn("radio.1.txpower=14", flash)
        self.assertIn("radio.1.obey=enabled", flash)

    def put(self, path, value=""):
        with open(path, "w") as f:
            f.write(str(value))

    def test_power_direct_saved_when_the_radio_transmits_it_without_a_drop(self):
        # the real Tri-Lectro LiteBeam M5: the power changes, the link never drops
        self.put(TX_FILE, 20)
        self.put(NODROP_FILE)
        self.put(IW_FILE)
        job, radios = self.run_m5("power", {"dbm": 14})
        self.assertEqual(job["verdict"], "applied", job)
        self.assertIn("radio.1.txpower=14", open(FLASH).read())
        self.assertIn("TXP iw=14 mca=14", "\n".join(job.get("log") or []))

    def test_power_direct_capped_but_moved_is_saved(self):
        self.put(TX_FILE, 20)
        self.put(TXCAP_FILE, 22)
        self.put(NODROP_FILE)
        job, radios = self.run_m5("power", {"dbm": 25})
        self.assertEqual(job["verdict"], "applied", job)
        self.assertIn("TXP mca=22", "\n".join(job.get("log") or []))

    def test_power_direct_unchanged_on_air_is_put_back(self):
        # obey on and already at the country limit: the setting moves, the radio does not
        self.put(TX_FILE, 20)
        self.put(TXCAP_FILE, 20)
        self.put(NODROP_FILE)
        job, radios = self.run_m5("power", {"dbm": 25})
        self.assertEqual(job["verdict"], "reverted", job)
        self.assertIn("still transmits the same power", job["msg"])
        self.assertFalse(os.path.exists(FLASH))
        self.assertIn("radio.1.txpower=20", radios.radios["10.0.0.1"][0])

    def test_power_direct_without_a_reading_needs_the_link_to_restart(self):
        self.put(NODROP_FILE)
        job, radios = self.run_m5("power", {"dbm": 14})
        self.assertEqual(job["verdict"], "reverted", job)
        self.assertIn("reports no transmit power", job["msg"])
        self.assertFalse(os.path.exists(FLASH))

    def test_power_direct_put_back_when_not_live(self):
        job, radios = self.run_m5("power", {"dbm": 14}, noop=True)
        self.assertEqual(job["verdict"], "reverted", job)
        self.assertFalse(os.path.exists(FLASH))
        self.assertIn("radio.1.txpower=20", radios.radios["10.0.0.1"][0])

    def test_ntp_adds_lines_and_saves(self):
        job, radios = self.run_m5("ntp", {})
        self.assertEqual(job["verdict"], "applied", job)
        flash = open(FLASH).read()
        for line in ("ntpclient.status=enabled", "ntpclient.1.server=pool.ntp.org", "system.timezone=GMT-2"):
            self.assertIn(line, flash)

    def test_tokens_expire_and_are_single_use(self):
        tok = wififix.remember({"x": 1})
        self.assertEqual(wififix.take(tok), {"x": 1})
        with self.assertRaises(wififix.FixError):
            wififix.take(tok)


class ThroughTheApi(unittest.TestCase):
    """The site route passes every option plan() reads (a dropped one silently
    turned "set 14 dBm" into "which radio should be matched?" at Tri-Lectro)."""

    def test_route_passes_every_planned_option(self):
        from unittest import mock
        import config
        import server
        import siteauth
        key = "k" * 43
        siteauth.set_hub_key(key)
        cfg = config.load()
        cfg.setdefault("features", {})["wifi_fix"] = True
        seen = {}

        def fake_plan(kind, target, login_for, find_device=None, params=None):
            seen.update(params or {})
            return {"kind": kind, "edits": []}
        with mock.patch.object(config, "load", return_value=cfg), \
                mock.patch.object(server, "_radio_target", return_value={"key": "r", "ip": "10.0.0.1", "name": "R"}), \
                mock.patch.object(wififix, "plan", side_effect=fake_plan):
            r = server.app.test_client().post(
                "/api/wifi/fix/preview", environ_base={"REMOTE_ADDR": "192.168.0.50"},
                headers={siteauth.HEADER: key},
                json={"kind": "power", "radio": "r", "params": {"dbm": 14, "obey": True, "rm -rf": 1}})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(seen, {"dbm": 14, "obey": True})
        # every option a plan branch reads with params.get("...") is on the API's list
        import inspect
        import re as _re
        read = set(_re.findall(r'params\.get\("([a-z_]+)"', inspect.getsource(wififix)))
        self.assertLessEqual(read - {"match"}, set(wififix.PARAMS))


if __name__ == "__main__":
    unittest.main()
