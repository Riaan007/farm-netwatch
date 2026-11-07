"""Telegram phase 2 on the site: the bot ANSWERS in its own group (read-only).

The site Pi long-polls its own bot (getUpdates, 50 s) straight over the farm's
internet, so the group can ask "/internet" while the VPN to the hub is down.

Who may ask: only people the hub has listed for this site (Telegram user ids,
role viewer / operator / admin), and only in the ONE paired group. Anyone
else who tries is told once a day to ask FarmCamSA, and lands on the hub's
Settings → Telegram as an access request (name + id), so nobody has to dig
up user ids. Every command is read-only in phase 2; the roles start to
matter in phase 4/5 (diagnostics, fixes).

Safety rules (plan section 9.1):
  * a command older than STALE_S (Telegram keeps undelivered messages for a
    day) is NOT run — "send it again";
  * per person at most RATE_N commands per RATE_WINDOW_S;
  * the offset is saved only after a batch with something in it (SD card);
  * a 409 (someone else polls this bot — the hub while pairing) backs off;
  * `pause_until` from the hub stops polling while the hub pairs a group.

Data comes from this site's OWN HTTP API through Flask's test client, with
siteauth's in-process secret: the bot shows exactly what the web page shows
and never needs to know how each module keeps its state.
"""
import os
import time

import tgapi
import tgbotcore
from tgbotcore import ago as _ago, dhm as _dhm, hm as _hm  # noqa: F401

DATA_DIR = os.environ.get("NETWATCH_DATA", "/data")
OFFSET_PATH = os.path.join(DATA_DIR, "tg_offset.json")
ROLES = tgbotcore.ROLES
TELLME_MAX_S = 24 * 3600

MENU = [[("📋 Status", "status"), ("🌐 Internet", "internet"), ("🔗 Hub link", "hub")],
        [("🖥 Pi", "pi"), ("🔴 What's down", "down"), ("📉 Outages", "outages")],
        [("📷 Cameras", "cameras"), ("💽 Recorders", "nvr"), ("📡 Radio links", "links")]]

HELP = ("<b>What I can tell you</b>\n"
        "/status — everything on one screen\n"
        "/internet — router, DNS, internet, public IP, last outages\n"
        "/hub — the VPN link to the FarmCamSA hub\n"
        "/pi — this Pi's health\n"
        "/down — monitored devices that are down\n"
        "/outages — internet and hub-link outages (3 days)\n"
        "/cameras · /nvr — cameras and recorders\n"
        "/links — radio links\n"
        "/history &lt;device&gt; — a device's last events\n"
        "/tellme &lt;device&gt; — tell me when it comes back (or goes down)\n"
        "/whoami — your Telegram id and role\n"
        "/menu — buttons")


def _ok(v):
    return "✅" if v else ("❌" if v is not None else "❔")


def _dev_name(d):
    return d.get("name") or d.get("device_name") or d.get("hostname") or d.get("vendor") \
        or d.get("type") or d.get("ip") or d.get("key")


class SiteBot(tgbotcore.Bot):
    def __init__(self, settings, api_get, clock=time.time, offset_path=None, api_raw=None):
        """settings() → telegram.json dict; api_get(path) → this site's API JSON or None;
        api_raw(path) → raw bytes (camera pictures)."""
        super().__init__(settings, COMMANDS, offset_path or OFFSET_PATH, clock=clock, name="tgbot")
        self.api_get = api_get
        self.api_raw = api_raw
        self.api_post = lambda path, body: None   # set by tgsite (own API, POST)
        self.tellme = []           # {"key","name","want_online","by","until"}

    def tick(self):
        self.check_tellme()

    def set_maint(self, until, who):
        """Overridden by tgsite (it owns telegram.json)."""
        self.maint = (until, who)

    # ---- device lookup ----------------------------------------------------------
    def devices(self):
        d = self.api_get("/api/devices")
        return (d.get("devices") or []) if isinstance(d, dict) else []

    def find(self, query):
        q = " ".join(query).strip().lower()
        if not q:
            return []
        devs = self.devices()
        exact = [d for d in devs if q in (str(d.get("ip") or "").lower(), str(d.get("mac") or "").lower(),
                                           str(d.get("key") or "").lower(),
                                           str(_dev_name(d) or "").lower())]
        if exact:
            return exact
        hits = [d for d in devs if any(q in str(d.get(f) or "").lower()
                                       for f in ("name", "device_name", "hostname", "ip", "model"))]
        hits.sort(key=lambda d: (not d.get("watch"), not d.get("name")))
        return hits

    def pick(self, cmd, query):
        """One device, or (text, buttons) asking which."""
        hits = self.find(query)
        if len(hits) == 1:
            return hits[0], None
        if not hits:
            return None, (f"No device matches “{tgapi.esc(' '.join(query))}”.", None)
        rows = [[(f"{_dev_name(d)} ({d.get('ip') or '-'})"[:40], f"{cmd}:{d.get('key')}")]
                for d in hits[:8] if len(f"c:{cmd}:{d.get('key')}") <= 64]
        return None, (f"Which one? {len(hits)} devices match.", rows)

    # ---- tellme -----------------------------------------------------------------
    def check_tellme(self):
        if not self.tellme:
            return
        now = self.clock()
        devs = {d.get("key"): d for d in self.devices()}
        keep = []
        for w in self.tellme:
            d = devs.get(w["key"])
            if now > w["until"]:
                self.send(f"⌛ Stopped watching {tgapi.esc(w['name'])} for {tgapi.esc(w['by'])} — "
                          "no change in 24 hours.")
                continue
            if d is not None and bool(d.get("online")) == w["want_online"]:
                self.send(f"{'✅' if w['want_online'] else '🔴'} <b>{tgapi.esc(w['name'])}</b> is "
                          f"{'back ONLINE' if w['want_online'] else 'now OFFLINE'} "
                          f"(you asked, {tgapi.esc(w['by'])}).")
                continue
            keep.append(w)
        self.tellme = keep


def _menu():
    return [list(row) for row in MENU]


# ---- commands: each returns (html text, buttons or None) --------------------------
def c_help(bot, args, who):
    return HELP, _menu()


def _internet_lines(bot):
    i = bot.api_get("/api/internet") or {}
    pub = i.get("public_ip") or {}
    lines = [f"Router {_ok(i.get('gateway')) if i.get('has_gateway') else '❔'} · "
             f"DNS {_ok(i.get('dns'))} · Internet {_ok(i.get('external'))}"]
    if pub.get("ip"):
        since = f", since {_dhm(pub['since'])}" if pub.get("since") else ""
        lines.append(f"Public IP <code>{tgapi.esc(pub['ip'])}</code>{since}")
    return i, lines


def _hub_line(h):
    if not h.get("configured"):
        return "Hub link: not used on this Pi"
    if h.get("connected") and h.get("hub_reachable"):
        return f"Hub link ✅ (handshake {_ago(h.get('last_handshake_s'))} ago)"
    hs = h.get("last_handshake_s")
    return f"Hub link ❌ — last handshake {_ago(hs)} ago" if hs is not None else "Hub link ❌ — no handshake yet"


def _pi_lines(s):
    if not s:
        return ["Pi: no reading yet"]
    th = s.get("throttled") or {}
    disk = (s.get("disks") or [{}])[0]
    mem = s.get("mem") or {}
    warn = []
    if th.get("undervoltage_now"):
        warn.append("⚡ UNDERVOLTAGE now")
    elif th.get("undervoltage_ever"):
        warn.append("⚡ undervoltage since boot")
    if th.get("throttled_now"):
        warn.append("🔥 throttling now")
    line = (f"Pi {s.get('temp_c', '?')} °C · disk {disk.get('used_pct', '?')} % · "
            f"RAM {mem.get('used_pct', '?')} % · up {_ago(s.get('uptime_s'))}")
    return [line] + warn


def _monitored(bot):
    devs = bot.devices()
    mon = [d for d in devs if d.get("watch")]
    return devs, mon, [d for d in mon if not d.get("online")]


def c_status(bot, args, who):
    st = bot.api_get("/api/status") or {}
    name = (st.get("site") or {}).get("name") or "This site"
    _, inet = _internet_lines(bot)
    h = bot.api_get("/api/hub/status") or {}
    devs, mon, down = _monitored(bot)
    lines = [f"📋 <b>{tgapi.esc(name)}</b> · {time.strftime('%H:%M')}"] + inet + [_hub_line(h)]
    lines += _pi_lines(bot.api_get("/api/sysinfo"))
    online = sum(1 for d in devs if d.get("online"))
    lines.append(f"Monitored: {len(mon) - len(down)}/{len(mon)} online · all devices {online}/{len(devs)}")
    if down:
        lines.append("🔴 Down: " + ", ".join(tgapi.esc(_dev_name(d)) for d in down[:8]) +
                     (f" … +{len(down) - 8}" if len(down) > 8 else ""))
    if st.get("last_scan"):
        lines.append(f"Last scan {tgapi.esc(st['last_scan'])}")
    return "\n".join(lines), _menu()


def c_internet(bot, args, who):
    i, lines = _internet_lines(bot)
    lines.insert(0, "🌐 <b>Internet</b>")
    if i.get("checked_ts"):
        lines.append(f"Checked {_ago(bot.clock() - i['checked_ts'])} ago")
    outs = outages(["__inet__8.8.8.8", "__inet__1.1.1.1"], 86400, now=bot.clock())
    lines.append(_outage_text("Internet outages, last 24 h", outs))
    return "\n".join(lines), None


def c_hub(bot, args, who):
    h = bot.api_get("/api/hub/status") or {}
    lines = ["🔗 <b>Link to the FarmCamSA hub</b>", _hub_line(h)]
    if h.get("configured"):
        if h.get("endpoint"):
            lines.append(f"Hub address in use: <code>{tgapi.esc(h['endpoint'])}</code>")
        if h.get("address"):
            lines.append(f"This site on the VPN: <code>{tgapi.esc(h['address'])}</code>")
        heal = h.get("self_heal") or {}
        if heal.get("down_since"):
            lines.append(f"Down since {_dhm(heal['down_since'])}")
        if heal.get("last_action"):
            lines.append(f"Last repair: {tgapi.esc(str(heal['last_action'])[:120])}")
        outs = outages(["__hub__"], 3 * 86400, now=bot.clock())
        lines.append(_outage_text("Hub-link outages, 3 days", outs))
    return "\n".join(lines), None


def c_pi(bot, args, who):
    s = bot.api_get("/api/sysinfo") or {}
    lines = ["🖥 <b>This Pi</b>"] + _pi_lines(s)
    if s.get("model"):
        lines.append(tgapi.esc(s["model"]))
    for d in (s.get("disks") or [])[:3]:
        tr = d.get("trend") or {}
        full = f", full in ~{round(tr['days_to_full'])} days" if tr.get("days_to_full") else ""
        lines.append(f"Disk {tgapi.esc(d.get('mount'))}: {d.get('free_gb', '?')} GB free{full}")
    ld = s.get("load") or []
    if ld:
        lines.append("Load " + " / ".join(str(x) for x in ld[:3]))
    return "\n".join(lines), None


def c_down(bot, args, who):
    devs, mon, down = _monitored(bot)
    if not mon:
        return "Nothing is on this site's Monitored list yet.", None
    if not down:
        return f"✅ All {len(mon)} monitored devices are online.", None
    down_keys = {d.get("key") for d in down}
    behind, roots = {}, []
    for d in down:
        parent = d.get("link")
        if parent in down_keys and parent != d.get("key"):
            behind.setdefault(parent, []).append(d)
        else:
            roots.append(d)
    now = bot.clock()
    lines = [f"🔴 <b>{len(down)} of {len(mon)} monitored devices down</b>"]
    rows = [[(f"📶 Ping {_dev_name(d)}"[:30], f"ping:{d.get('key')}"), ("🧪 Test", f"test:{d.get('key')}")]
            for d in roots[:5] if len(f"c:ping:{d.get('key')}") <= 64]
    for d in roots[:25]:
        seen = d.get("last_seen")
        lines.append(f"• {tgapi.esc(_dev_name(d))} ({tgapi.esc(d.get('ip') or 'no IP')}) — "
                     f"last seen {_ago(now - seen) + ' ago' if seen else 'never'}")
        kids = behind.get(d.get("key")) or []
        if kids:
            lines.append(f"   ↳ and {len(kids)} behind it: " +
                         ", ".join(tgapi.esc(_dev_name(k)) for k in kids[:6]))
    return "\n".join(lines), rows or None


def c_outages(bot, args, who):
    now = bot.clock()
    return "\n".join(["📉 <b>Outages (last 3 days)</b>",
                      _outage_text("Internet", outages(["__inet__8.8.8.8", "__inet__1.1.1.1"], 3 * 86400, now=now)),
                      _outage_text("Hub link", outages(["__hub__"], 3 * 86400, now=now)),
                      _outage_text("Router", outages(["__inet__gateway"], 3 * 86400, now=now))]), None


def c_cameras(bot, args, who):
    c = bot.api_get("/api/cameras") or {}
    cams = c.get("cameras") or []
    if not c.get("enabled", True):
        return "Camera pictures are switched off on this site.", None
    if not cams:
        return "No cameras found on this site.", None
    off = [x for x in cams if not x.get("online")]
    norec = [x for x in cams if x.get("recording") is False]
    lines = [f"📷 <b>Cameras</b>: {len(cams) - len(off)}/{len(cams)} online"]
    if off:
        lines.append("Offline: " + ", ".join(tgapi.esc(x.get("name") or x.get("ip")) for x in off[:12]))
    if norec:
        lines.append("Not recording: " + ", ".join(tgapi.esc(x.get("name") or x.get("ip")) for x in norec[:12]))
    issues = [x for x in cams if x.get("health")]
    for x in issues[:6]:
        h = x["health"][0] if isinstance(x["health"], list) else x["health"]
        txt = h.get("text") if isinstance(h, dict) else str(h)
        lines.append(f"⚠ {tgapi.esc(x.get('name') or x.get('ip'))}: {tgapi.esc(str(txt)[:100])}")
    return "\n".join(lines), None


def c_nvr(bot, args, who):
    c = bot.api_get("/api/cameras") or {}
    recs = c.get("recorders") or []
    if not recs:
        return "No recorders found on this site.", None
    lines = ["💽 <b>Recorders</b>"]
    for r in recs[:8]:
        bits = [_ok(r.get("online"))]
        if r.get("footage_min_days") is not None:
            bits.append(f"{r['footage_min_days']} days of footage")
        if r.get("channels_online") is not None:
            bits.append(f"{r.get('channels_online')}/{(r.get('channels_online') or 0) + (r.get('channels_offline') or 0)} channels")
        if r.get("recording_off"):
            bits.append(f"⚠ {r['recording_off']} not recording" if isinstance(r['recording_off'], int)
                        else "⚠ some channels not recording")
        disks = r.get("disks") or []
        bad = [d for d in disks if isinstance(d, dict) and str(d.get("status") or "ok").lower() not in ("ok", "normal")]
        if disks:
            bits.append(f"{len(disks) - len(bad)}/{len(disks)} disks OK")
        lines.append(f"• <b>{tgapi.esc(r.get('name') or r.get('ip'))}</b> — " + " · ".join(bits))
        for h in (r.get("health") or [])[:3]:
            txt = h.get("text") if isinstance(h, dict) else str(h)
            lines.append(f"   ⚠ {tgapi.esc(str(txt)[:110])}")
    return "\n".join(lines), None


def c_links(bot, args, who):
    o = bot.api_get("/api/radio/overview") or {}
    radios = o.get("radios") or {}
    probs = o.get("problems") or []
    n = len(radios) if hasattr(radios, "__len__") else 0
    if not n and not probs:
        return "No radio links are watched on this site.", None
    lines = [f"📡 <b>Radio links</b>: {n} radios watched"]
    if not probs:
        lines.append("✅ No problems.")
    for p in sorted(probs, key=lambda p: p.get("level") != "crit")[:12]:
        icon = "🔴" if p.get("level") == "crit" else "🟠"
        lines.append(f"{icon} {tgapi.esc(p.get('device') or '')}: {tgapi.esc(str(p.get('what') or '')[:120])}")
    return "\n".join(lines), None


def c_history(bot, args, who):
    if not args:
        return "Which device? e.g. <code>/history gate</code>", None
    d, ask = bot.pick("history", args)
    if ask:
        return ask
    ev = bot.api_get(f"/api/events?key={d.get('key')}&limit=10") or {}
    rows = ev.get("events") or []
    lines = [f"🕘 <b>{tgapi.esc(_dev_name(d))}</b> ({tgapi.esc(d.get('ip') or '-')}) — "
             f"{'online' if d.get('online') else 'OFFLINE'}"]
    if not rows:
        lines.append("No events recorded.")
    for r in rows:
        lines.append(f"{_dhm(r.get('ts'))} · {tgapi.esc(r.get('type'))}")
    return "\n".join(lines), None


def c_tellme(bot, args, who):
    if not args:
        return "Which device? e.g. <code>/tellme gate camera</code>", None
    d, ask = bot.pick("tellme", args)
    if ask:
        return ask
    want = not bool(d.get("online"))
    if len(bot.tellme) >= 30:
        return "I am already watching 30 devices; try later.", None
    bot.tellme.append({"key": d.get("key"), "name": _dev_name(d), "want_online": want, "by": who,
                       "until": bot.clock() + TELLME_MAX_S})
    return (f"👀 I'll tell you when <b>{tgapi.esc(_dev_name(d))}</b> "
            f"{'comes back online' if want else 'goes offline'} (for up to 24 hours)."), None


COMMANDS = {name: (fn, "viewer") for name, fn in {
    "help": c_help, "start": c_help, "menu": c_help, "status": c_status,
    "internet": c_internet, "hub": c_hub, "pi": c_pi, "down": c_down,
    "outages": c_outages, "cameras": c_cameras, "nvr": c_nvr, "links": c_links,
    "history": c_history, "tellme": c_tellme}.items()}

BOT_COMMANDS = [("status", "Everything on one screen"), ("internet", "Router, DNS, internet, public IP"),
                ("hub", "Link to the FarmCamSA hub"), ("pi", "This Pi's health"),
                ("down", "Monitored devices that are down"), ("outages", "Outages, last 3 days"),
                ("cameras", "Cameras"), ("nvr", "Recorders"), ("links", "Radio links"),
                ("history", "A device's last events"), ("tellme", "Tell me when a device changes"),
                ("whoami", "Your id and role"), ("menu", "Buttons")]


# ---- outages from the heartbeat table ------------------------------------------------
def outages(keys, window_s, now=None, gap_s=200):
    """Spans where EVERY key in `keys` was down at the same sample, plus gaps with
    no samples at all (the Pi was off or not sampling). Newest first:
    [{"start","end" (None = still down), "kind": "down"|"no_samples"}]."""
    import history
    now = now or time.time()
    since = int(now - window_s)
    ph = ",".join("?" for _ in keys)
    rows = history._conn().execute(
        f"SELECT ts, MAX(online) AS up FROM heartbeats WHERE key IN ({ph}) AND ts >= ? "
        f"GROUP BY ts ORDER BY ts", (*keys, since)).fetchall()
    out, start, prev = [], None, None
    for r in rows:
        ts, up = r["ts"], r["up"]
        if prev is not None and ts - prev > gap_s:
            if start is not None:
                out.append({"start": start, "end": prev, "kind": "down"})
                start = None
            out.append({"start": prev, "end": ts, "kind": "no_samples"})
        if not up and start is None:
            start = ts
        elif up and start is not None:
            out.append({"start": start, "end": ts, "kind": "down"})
            start = None
        prev = ts
    if start is not None:
        out.append({"start": start, "end": None, "kind": "down"})
    out = [o for o in out if o["kind"] == "no_samples" or (o["end"] or now) - o["start"] >= 60]
    return list(reversed(out))


def _outage_text(label, outs):
    if not outs:
        return f"{label}: none ✅"
    lines = [f"{label}: {sum(1 for o in outs if o['kind'] == 'down')} outage(s)"]
    for o in outs[:6]:
        dur = _ago((o["end"] or time.time()) - o["start"])
        if o["kind"] == "no_samples":
            lines.append(f"  · {_dhm(o['start'])} → {_hm(o['end'])} no readings ({dur}; Pi off or restarting?)")
        else:
            lines.append(f"  · {_dhm(o['start'])} → {_hm(o['end']) if o['end'] else 'NOW'} ({dur})")
    return "\n".join(lines)


# ---- phase 4: diagnostics (operator) -------------------------------------------------
# Every probe runs Netwatch's own tools (commands.py: nmap ping / port / traceroute,
# the 20-ping quality test) and every address must sit inside this site's own
# networks (commands.ip_allowed) — the bot can never be used to probe the internet.
import ipaddress as _ip  # noqa: E402
import random as _random  # noqa: E402
import socket as _socket  # noqa: E402
import struct as _struct  # noqa: E402

SPEED_URL = "https://speed.cloudflare.com/__down?bytes=5000000"


def _target(bot, cmd, args):
    """(ip, label) for a device name / key / IP, or (None, (text, buttons))."""
    if not args:
        return None, (f"Which device? e.g. <code>/{cmd} gate</code> or <code>/{cmd} 192.168.0.20</code>", None)
    q = args[0] if len(args) == 1 else " ".join(args)
    try:
        _ip.ip_address(q)
        devs = [d for d in bot.devices() if d.get("ip") == q]
        return (q, _dev_name(devs[0]) if devs else q), None
    except ValueError:
        pass
    d, ask = bot.pick(cmd, args)
    if ask:
        return None, ask
    if not d.get("ip"):
        return None, (f"{tgapi.esc(_dev_name(d))} has no IP address right now.", None)
    return (d["ip"], _dev_name(d)), None


def _allowed(ip):
    import commands
    return commands.ip_allowed(ip)


def _probe(cmd, run):
    def fn(bot, args, who):
        extra = []
        if cmd == "port" and len(args) >= 2 and args[-1].isdigit():
            args, extra = args[:-1], [args[-1]]
        t, ask = _target(bot, cmd, args)
        if ask:
            return ask
        ip, label = t
        if not _allowed(ip):
            return f"Refused: {tgapi.esc(ip)} is not on this site's networks.", None
        return tgbotcore.Job(f"⏳ /{cmd} {tgapi.esc(label)} ({tgapi.esc(ip)})…",
                             lambda: (f"<b>{tgapi.esc(label)}</b>\n<pre>{tgapi.esc(run(ip, *extra))}</pre>", None))
    return fn


def _run_ping(ip):
    import commands
    return commands.ping(ip)


def _run_port(ip, port=None):
    import commands
    if port is None:
        return "Which port? e.g. /port gate 554"
    return commands.port_check(ip, port)


def _run_trace(ip):
    import commands
    return commands.traceroute(ip)


def _run_test(ip):
    import commands
    return commands.quality_text(ip)


def _dns_query(name, server, timeout=3):
    """Minimal A-record lookup straight at `server` (stdlib only)."""
    qid = _random.randint(0, 65535)
    q = _struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    for part in name.rstrip(".").split("."):
        q += bytes([len(part)]) + part.encode()
    q += b"\x00" + _struct.pack(">HH", 1, 1)
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(q, (server, 53))
        data = s.recv(1500)
    finally:
        s.close()
    if len(data) < 12 or _struct.unpack(">H", data[:2])[0] != qid:
        raise ValueError("bad reply")
    an = _struct.unpack(">H", data[6:8])[0]
    pos = len(q)
    out = []
    for _ in range(an):
        if data[pos] & 0xC0 == 0xC0:
            pos += 2
        else:
            while data[pos]:
                pos += data[pos] + 1
            pos += 1
        rtype, _, _, rlen = _struct.unpack(">HHIH", data[pos:pos + 10])
        pos += 10
        if rtype == 1 and rlen == 4:
            out.append(_socket.inet_ntoa(data[pos:pos + 4]))
        pos += rlen
    return out


def c_dns(bot, args, who):
    name = (args[0] if args else "google.com").strip().lower()
    if not all(p.isalnum() or "-" in p for p in name.split(".")) or len(name) > 200:
        return "That is not a host name.", None

    def work():
        lines = [f"🔎 <b>{tgapi.esc(name)}</b>"]
        try:
            router = sorted({a[4][0] for a in _socket.getaddrinfo(name, None, _socket.AF_INET)})
            lines.append(f"Via the router's DNS: {tgapi.esc(', '.join(router) or 'no answer')}")
        except OSError as e:
            lines.append(f"Via the router's DNS: ❌ {tgapi.esc(str(e)[:80])}")
        for srv in ("1.1.1.1", "8.8.8.8"):
            try:
                ips = _dns_query(name, srv)
                lines.append(f"Via {srv}: {tgapi.esc(', '.join(ips) or 'no A record')}")
            except (OSError, ValueError, _struct.error, IndexError) as e:
                lines.append(f"Via {srv}: ❌ {tgapi.esc(e.__class__.__name__)}")
        return "\n".join(lines), None
    return tgbotcore.Job(f"⏳ Looking up {tgapi.esc(name)}…", work)


def c_find(bot, args, who):
    if not args:
        return "What should I find? A name, IP or MAC.", None
    hits = bot.find(args)
    if not hits:
        return f"Nothing matches “{tgapi.esc(' '.join(args))}”.", None
    now = bot.clock()
    lines = [f"🔍 {len(hits)} match(es)"]
    for d in hits[:8]:
        port = d.get("switch_port")
        port_txt = ""
        if isinstance(port, dict):
            port_txt = f" · switch {tgapi.esc(port.get('switch') or port.get('name') or '')} port {tgapi.esc(port.get('port') or '')}"
        seen = d.get("last_seen")
        lines.append(f"{'✅' if d.get('online') else '❌'} <b>{tgapi.esc(_dev_name(d))}</b> "
                     f"{tgapi.esc(d.get('ip') or '-')} · {tgapi.esc(d.get('mac') or '')}{port_txt}"
                     + ("" if d.get("online") or not seen else f" · last seen {_ago(now - seen)} ago"))
    return "\n".join(lines), None


def c_scan(bot, args, who):
    import commands
    return f"🔄 {tgapi.esc(commands.dispatch('quickscan'))} — results show in /down and /status in a few minutes.", None


def c_dhcp(bot, args, who):
    g = (bot.api_get("/api/status") or {}).get("dhcp_guard") or {}
    if not g:
        return "The DHCP guard is not running on this site.", None
    lines = [f"🛡 <b>DHCP</b>: {g.get('servers', 0)} server(s) answering"
             + (f", checked {_ago(bot.clock() - g['checked_ts'])} ago" if g.get("checked_ts") else "")]
    rog = g.get("rogues") or []
    lines.append("✅ Only the farm's own router hands out addresses." if not rog else
                 f"🔴 {len(rog)} ROGUE DHCP server(s):")
    for r in rog[:6]:
        lines.append(f"• {tgapi.esc(r.get('ip'))} {tgapi.esc(r.get('mac'))} {tgapi.esc(r.get('vendor') or '')} — "
                     f"{tgapi.esc(str(r.get('detail') or '')[:100])}")
    return "\n".join(lines), None


def c_speed(bot, args, who):
    if bot.settings().get("capped"):
        return "Speed tests are off on this site (its internet has a data cap).", None
    import requests as _rq

    def work():
        t0 = time.time()
        n = 0
        try:
            with _rq.get(SPEED_URL, stream=True, timeout=(8, 30)) as r:
                for chunk in r.iter_content(65536):
                    n += len(chunk)
                    if time.time() - t0 > 40:
                        break
        except _rq.RequestException as e:
            return f"❌ Speed test failed: {tgapi.esc(e.__class__.__name__)}", None
        dt = max(0.001, time.time() - t0)
        return (f"🚀 <b>Download</b> {n * 8 / dt / 1e6:.1f} Mbit/s "
                f"({n / 1e6:.1f} MB in {dt:.1f} s, from Cloudflare)"), None
    return tgbotcore.Job("⏳ Downloading 5 MB to measure the line…", work)


def c_pic(bot, args, who):
    if not bot.settings().get("pictures"):
        return "Camera pictures in Telegram are switched off for this site (FarmCamSA can switch them on).", None
    c = bot.api_get("/api/cameras") or {}
    if not c.get("enabled", True):
        return "Camera pictures are switched off on this site.", None
    cams = c.get("cameras") or []
    if not args:
        rows = [[((x.get("name") or x.get("ip") or "?")[:30], f"pic:{x.get('key')}")]
                for x in cams[:10] if len(f"c:pic:{x.get('key')}") <= 64]
        return "Which camera?", rows
    q = " ".join(args).lower()
    hit = [x for x in cams if q in (str(x.get("key") or "").lower(), str(x.get("name") or "").lower(),
                                    str(x.get("ip") or ""))] or \
          [x for x in cams if q in str(x.get("name") or "").lower()]
    if len(hit) != 1:
        rows = [[((x.get("name") or x.get("ip") or "?")[:30], f"pic:{x.get('key')}")]
                for x in (hit or cams)[:10] if len(f"c:pic:{x.get('key')}") <= 64]
        return (f"{len(hit)} cameras match — which one?" if hit else "No camera matches."), rows
    cam = hit[0]
    from urllib.parse import quote

    def work():
        raw = bot.api_raw(f"/api/cameras/{quote(str(cam.get('key')), safe='')}/picture?size=full&fresh=1") \
            if getattr(bot, "api_raw", None) else None
        if not raw:
            return f"No picture from {tgapi.esc(cam.get('name') or cam.get('ip'))} right now.", None
        return {"photo": raw, "caption": f"📷 <b>{tgapi.esc(cam.get('name') or cam.get('ip'))}</b> · "
                                         f"{time.strftime('%H:%M')} (asked by {tgapi.esc(who)})"}
    return tgbotcore.Job(f"⏳ Getting a picture from {tgapi.esc(cam.get('name') or cam.get('ip'))}…", work)


COMMANDS.update({
    "ping": (_probe("ping", _run_ping), "operator"),
    "port": (_probe("port", _run_port), "operator"),
    "trace": (_probe("trace", _run_trace), "operator"),
    "test": (_probe("test", _run_test), "operator"),
    "dns": (c_dns, "operator"),
    "find": (c_find, "operator"),
    "scan": (c_scan, "operator", 600),
    "dhcp": (c_dhcp, "operator"),
    "speed": (c_speed, "operator", 1800),
    "pic": (c_pic, "viewer"),
})
BOT_COMMANDS += [("ping", "Ping a device"), ("test", "Connection quality to a device"),
                 ("trace", "Route to a device"), ("port", "Is a port open?"), ("dns", "Look up a name"),
                 ("find", "Find a device"), ("scan", "Quick network scan"), ("dhcp", "Rogue DHCP check"),
                 ("speed", "Internet speed test"), ("pic", "A camera picture")]
HELP += ("\n\n<b>Tests (operators)</b>\n"
         "/ping · /test · /trace &lt;device&gt; — reachability, quality, route\n"
         "/port &lt;device&gt; &lt;port&gt; · /dns &lt;name&gt; · /find &lt;name|IP|MAC&gt;\n"
         "/scan — quick scan · /dhcp — rogue DHCP · /speed — line speed\n"
         "/pic &lt;camera&gt; — a picture (when switched on for this site)")


# ---- phase 5: fixes (operator, ask first, per-site switch) ----------------------------
# Every fix uses the SAME engine and the SAME safety switches as the web page:
#   /restart → /api/devices/<key>/restart (restart.py: tests the login, restarts, and
#              only says "restarted" when the network saw it drop and come back);
#   /poe     → /api/devices/<switch>/switch/action poe-cycle (refuses the Pi's and the
#              router's own ports on a FRESH read of the switch).
# On top: the site's "Fixes from Telegram" switch (`actions`, off by default) and a
# Yes/Cancel question only the asker (or an admin) can answer.
from urllib.parse import quote as _quote  # noqa: E402

RESTART_WATCH_S = 540
RESTART_POLL_S = 5


def _fixes_on(bot):
    return bool(bot.settings().get("actions"))


FIXES_OFF = ("Fixes from Telegram are switched off for this site. FarmCamSA can switch them on "
             "(hub → Settings → Telegram).")


def c_restart(bot, args, who):
    if not _fixes_on(bot):
        return FIXES_OFF, None
    if not args:
        return "Which device? e.g. <code>/restart tower radio</code>", None
    d, ask = bot.pick("restart", args)
    if ask:
        return ask
    key = _quote(str(d.get("key")), safe="")
    info = bot.api_get(f"/api/devices/{key}/restart") or {}
    name = _dev_name(d)
    if not info.get("can_restart"):
        return f"Can't restart <b>{tgapi.esc(name)}</b>: {tgapi.esc(info.get('detail') or 'not supported')}.", None
    if (info.get("job") or {}).get("done") is False:
        return f"A restart of {tgapi.esc(name)} is already running.", None
    behind = [x for x in bot.devices() if x.get("link") == d.get("key")]
    q = (f"⚠ <b>Restart {tgapi.esc(name)}</b> ({tgapi.esc(d.get('ip'))}) via "
         f"{tgapi.esc(info.get('method_label') or 'its own login')}?")
    if behind:
        q += f"\n{len(behind)} device(s) behind it drop too: " + ", ".join(tgapi.esc(_dev_name(x)) for x in behind[:6])
    if info.get("warning"):
        q += f"\n{tgapi.esc(info['warning'])}"

    def work():
        res = bot.api_post(f"/api/devices/{key}/restart", {"confirm": True}) or {}
        if not res.get("ok"):
            return f"❌ Restart of {tgapi.esc(name)} not started: {tgapi.esc(res.get('error') or 'refused')}", None
        t0 = time.time()
        job = res.get("job") or {}
        while not job.get("done") and time.time() - t0 < RESTART_WATCH_S:
            time.sleep(RESTART_POLL_S)
            job = (bot.api_get(f"/api/devices/{key}/restart") or {}).get("job") or job
        if not job.get("done"):
            return f"⌛ {tgapi.esc(name)}: no verdict after 9 min — check /history {tgapi.esc(name)}.", None
        icon = "✅" if job.get("ok") else "❌"
        down = f" (off the network {job['seconds_down']} s)" if job.get("seconds_down") else ""
        return (f"{icon} <b>{tgapi.esc(name)}</b>: {tgapi.esc(job.get('msg') or job.get('verdict') or '')}{down}",
                [[("🕘 History", f"history:{d.get('key')}")]] if len(f"c:history:{d.get('key')}") <= 64 else None)
    return tgbotcore.Confirm(q, tgbotcore.Job(f"🔄 Restarting {tgapi.esc(name)}…", work))


def c_poe(bot, args, who):
    if not _fixes_on(bot):
        return FIXES_OFF, None
    if not args:
        return "Which device? e.g. <code>/poe gate camera</code> — I power-cycle its switch port.", None
    d, ask = bot.pick("poe", args)
    if ask:
        return ask
    name = _dev_name(d)
    sp = d.get("switch_port") or {}
    if not sp.get("switch_key") or not sp.get("port"):
        return f"I don't know which switch port {tgapi.esc(name)} is on.", None
    if sp.get("uplink"):
        return (f"{tgapi.esc(name)} is only seen through an uplink port ({tgapi.esc(sp['port'])}) — "
                "cycling it would cut a whole switch. Not doing that."), None
    sw = next((x for x in bot.devices() if x.get("key") == sp["switch_key"]), {})
    swname = _dev_name(sw) if sw else sp["switch_key"]
    q = (f"⚠ <b>Power-cycle PoE</b> on port {tgapi.esc(sp['port'])} of {tgapi.esc(swname)}?\n"
         f"{tgapi.esc(name)} loses power for about 8 seconds and restarts.")
    skey = _quote(str(sp["switch_key"]), safe="")

    def work():
        res = bot.api_post(f"/api/devices/{skey}/switch/action",
                           {"action": "poe-cycle", "port": sp["port"], "confirm": True}) or {}
        if not res.get("ok"):
            return f"❌ PoE cycle not done: {tgapi.esc(res.get('error') or res.get('warning') or 'refused')}", None
        if d.get("key"):
            bot.tellme.append({"key": d.get("key"), "name": name, "want_online": True, "by": who,
                               "until": bot.clock() + 1800})
        return (f"✅ Port {tgapi.esc(sp['port'])} power-cycled. {tgapi.esc(res.get('msg') or '')}\n"
                f"I'll tell you when {tgapi.esc(name)} answers again."), None
    return tgbotcore.Confirm(q, tgbotcore.Job(f"🔌 Cycling port {tgapi.esc(sp['port'])}…", work))


def c_restart_netwatch(bot, args, who):
    if not _fixes_on(bot):
        return FIXES_OFF, None

    def work():
        import threading as _th
        _th.Timer(3, lambda: os._exit(3)).start()       # Docker's restart policy brings it back
        return "♻ Netwatch restarts now — back in about a minute (I'll be quiet until then).", None
    return tgbotcore.Confirm("⚠ <b>Restart Netwatch</b> on this Pi? Scans, the bot and the web page stop "
                             "for about a minute. The Pi itself keeps running.",
                             tgbotcore.Job("♻ Restarting Netwatch…", work))


def c_maint(bot, args, who):
    st = bot.settings()
    now = bot.clock()
    until = float(st.get("maint_until") or 0)
    if not args:
        if until > now:
            return (f"🛠 Maintenance until {_hm(until)} ({_ago(until - now)} left) — alerts are held. "
                    "<code>/maint off</code> ends it."), None
        return "No maintenance. <code>/maint 2</code> holds this group's alerts for 2 hours.", None
    if args[0].lower() in ("off", "end", "stop", "0"):
        bot.set_maint(0, who)
        return "✅ Maintenance ended.", None
    try:
        hours = float(args[0].rstrip("h"))
        assert 0 < hours <= 24
    except (ValueError, AssertionError):
        return "Give hours, 0.5–24, e.g. <code>/maint 2</code>.", None
    bot.set_maint(now + hours * 3600, who)
    return (f"🛠 Maintenance for {hours:g} h (until {_hm(now + hours * 3600)}), started by {tgapi.esc(who)}. "
            "Alerts are held for this group and summarised at the end."), None


def c_ack(bot, args, who):
    what = " ".join(args)[:80]
    return f"🙋 <b>{tgapi.esc(who)}</b> is on it{(': ' + tgapi.esc(what)) if what else ''}.", None


DIGEST_AT = "07:00"
# Never run through the hub's /run: they change something or show pictures. They
# belong in the site's own group, in front of that site's people.
SITE_GROUP_ONLY = {"restart", "poe", "restart_netwatch", "maint", "ack", "pic", "tellme"}


def digest_due(last_date, now_ts, at=DIGEST_AT):
    """True once a day, at/after `at` local time, if not sent yet today."""
    lt = time.localtime(now_ts)
    today = time.strftime("%Y-%m-%d", lt)
    hh, mm = (int(x) for x in at.split(":"))
    return last_date != today and (lt.tm_hour, lt.tm_min) >= (hh, mm), today


def daily_report(bot):
    """The morning message: the /status screen plus yesterday's outages."""
    text, _ = c_status(bot, [], "daily report")
    now = bot.clock()
    lines = [f"☀ <b>Daily report</b> · {time.strftime('%a %d %b', time.localtime(now))}",
             text.split("\n", 1)[1] if "\n" in text else text,
             _outage_text("Internet, last 24 h", outages(["__inet__8.8.8.8", "__inet__1.1.1.1"], 86400, now=now))]
    h = bot.api_get("/api/hub/status") or {}
    if h.get("configured"):
        lines.append(_outage_text("Hub link, last 24 h", outages(["__hub__"], 86400, now=now)))
    return "\n".join(lines)


COMMANDS.update({
    "restart": (c_restart, "operator"),
    "poe": (c_poe, "operator"),
    "restart_netwatch": (c_restart_netwatch, "admin"),
    "maint": (c_maint, "operator"),
    "ack": (c_ack, "viewer"),
})
BOT_COMMANDS += [("restart", "Restart a device (asks first)"), ("poe", "Power-cycle a device's PoE port"),
                 ("maint", "Maintenance: hold alerts"), ("restart_netwatch", "Restart Netwatch (admin)")]
HELP += ("\n\n<b>Fixes (operators, when switched on; every fix asks first)</b>\n"
         "/restart &lt;device&gt; — restart it and watch it come back\n"
         "/poe &lt;device&gt; — cut and restore power on its switch port\n"
         "/maint 2 · /maint off — hold this group's alerts during work\n"
         "/restart_netwatch — restart the Netwatch app (admin)")
