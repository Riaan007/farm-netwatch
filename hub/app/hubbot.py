"""The Ops bot's commands (Telegram phase 2): the FarmCamSA Ops group asks the
hub about the whole fleet. Read-only. The engine (people, roles, stale drop,
rate limit, buttons) is tgbotcore; these functions only read what the hub
already holds: poller snapshots, the site cards the Control Center shows,
public IPs and backups. Nothing here calls a site.
"""
import time

import backups
import heartbeats
import hubconfig
import publicip
import tgapi
import tgbotcore
from tgbotcore import ago, dhm

MENU = [[("🗺 All sites", "sites"), ("🔗 VPN", "vpn")],
        [("💾 Backups", "backups"), ("🖥 Hub health", "hubhealth")]]
HELP = ("<b>FarmCamSA Ops</b>\n"
        "/sites — every site on one screen\n"
        "/site &lt;name&gt; — one site in detail\n"
        "/vpn — tunnels and public addresses\n"
        "/backups — age of each site's last config backup\n"
        "/hubhealth — ServerPI itself\n"
        "/visit &lt;site&gt; — what's wrong, where, what to bring\n"
        "/run &lt;site&gt; &lt;command&gt; — e.g. /run bennie internet, /run tankwa ping gate\n"
        "/whoami — your id and role · /menu — buttons")
BOT_COMMANDS = [("sites", "Every site on one screen"), ("site", "One site in detail"),
                ("vpn", "Tunnels and public addresses"), ("backups", "Config backup ages"),
                ("hubhealth", "ServerPI itself"), ("visit", "Visit brief: what to bring"),
                ("run", "Run a site command: /run bennie status"),
                ("whoami", "Your id and role"), ("menu", "Buttons")]

_site_card = None          # server._site_card, set by start()
_snapshot = None           # poller.snapshot


def _sites():
    return [s for s in hubconfig.load().get("sites", []) if s.get("enabled", True)]


def _icon(card):
    if not card.get("reachable"):
        return "🔴"
    if card.get("watched_down") or card.get("conflicts"):
        return "🟠"
    return "✅"


def c_help(bot, args, who):
    return HELP, [list(r) for r in MENU]


def c_sites(bot, args, who):
    lines, rows = [f"🗺 <b>Sites</b> · {time.strftime('%H:%M')}"], []
    for s in _sites():
        c = _site_card(s)
        name = c.get("name") or s["id"]
        if c.get("reachable"):
            mon = c.get("monitored")
            down = c.get("watched_down") or 0
            bits = [f"{(mon or 0) - down}/{mon} monitored up" if mon else "nothing monitored"]
            if c.get("conflicts"):
                bits.append(f"{c['conflicts']} IP conflict(s)")
            if c.get("latency_ms") is not None:
                bits.append(f"{round(c['latency_ms'])} ms")
            lines.append(f"{_icon(c)} <b>{tgapi.esc(name)}</b> — " + " · ".join(bits))
        else:
            lines.append(f"🔴 <b>{tgapi.esc(name)}</b> — not reachable over the VPN"
                         + (f" ({tgapi.esc(c.get('error'))})" if c.get("error") else ""))
        rows.append((name[:20], f"site:{s['id']}"))
    return "\n".join(lines), [rows[i:i + 3] for i in range(0, len(rows), 3)]


def _find_site(args):
    q = " ".join(args).strip().lower()
    sites = _sites()
    if not q:
        return None, sites
    exact = [s for s in sites if q in (s["id"].lower(), (s.get("name") or "").lower())]
    if exact:
        return exact[0], None
    hits = [s for s in sites if q in s["id"].lower() or q in (s.get("name") or "").lower()]
    return (hits[0], None) if len(hits) == 1 else (None, hits or sites)


def c_site(bot, args, who):
    s, choices = _find_site(args)
    if not s:
        return "Which site?", [[((x.get("name") or x["id"])[:20], f"site:{x['id']}")
                                for x in choices[i:i + 3]] for i in range(0, len(choices), 3)]
    c = _site_card(s)
    snap = _snapshot(s["id"]) or {}
    name = c.get("name") or s["id"]
    lines = [f"{_icon(c)} <b>{tgapi.esc(name)}</b>"]
    if c.get("reachable"):
        lines.append(f"VPN ✅ {round(c['latency_ms']) if c.get('latency_ms') is not None else '?'} ms")
    else:
        lines.append(f"VPN ❌ hub can't reach it ({tgapi.esc(c.get('error') or 'no answer')})")
        if snap.get("status_fetched"):
            lines.append(f"Last good answer {dhm(snap.get('status_fetched'))} (data below may be old)")
        hb = heartbeats.latest(s["id"])
        if hb:
            lines.append(f"Heartbeat over its own internet {ago(hb['age'])} ago: "
                         f"{tgapi.esc(heartbeats.STATE_TEXT.get(hb.get('st'), hb.get('st')))}")
        elif heartbeats.is_vpn_site(s):
            lines.append("No heartbeat over its own internet (dark, or an older Netwatch).")
    pub = publicip.card(s["id"])
    if pub and pub.get("ip"):
        lines.append(f"Public IP <code>{tgapi.esc(pub['ip'])}</code>"
                     + (f", tunnel seen {ago(time.time() - pub['seen'])} ago" if pub.get("seen") else ""))
    if c.get("devices_total") is not None:
        lines.append(f"Devices {c.get('devices_online')}/{c.get('devices_total')} online · "
                     f"monitored {(c.get('monitored') or 0) - (c.get('watched_down') or 0)}/{c.get('monitored') or 0}")
    devs = (snap.get("devices") or {}).get("devices") or []
    down = [d for d in devs if d.get("watch") and not d.get("online")]
    if down:
        lines.append("🔴 Down: " + ", ".join(tgapi.esc(d.get("name") or d.get("ip") or "?") for d in down[:10])
                     + (f" … +{len(down) - 10}" if len(down) > 10 else ""))
    si = snap.get("sysinfo") or {}
    if si:
        disk = (si.get("disks") or [{}])[0]
        th = si.get("throttled") or {}
        lines.append(f"Pi {si.get('temp_c', '?')} °C · disk {disk.get('used_pct', '?')} % · "
                     f"up {ago(si.get('uptime_s'))}" + (" · ⚡ UNDERVOLTAGE" if th.get("undervoltage_now") else ""))
    if c.get("last_scan_ts"):
        lines.append(f"Last scan {ago(time.time() - c['last_scan_ts'])} ago")
    if c.get("conflicts"):
        lines.append(f"⚠ {c['conflicts']} live IP conflict(s)")
    return "\n".join(lines), None


def c_vpn(bot, args, who):
    hub = publicip.hub_card() or {}
    lines = ["🔗 <b>VPN</b>"]
    if hub.get("ip"):
        lines.append(f"Hub public IP <code>{tgapi.esc(hub['ip'])}</code>")
    dd = heartbeats.status().get("ddns") or {}
    if dd.get("name"):
        lines.append(f"DDNS {tgapi.esc(dd['name'])} → {tgapi.esc(', '.join(dd.get('ips') or []) or '?')} "
                     + ("✅" if dd.get("ok") else "❌ NOT the hub"))
    for s in _sites():
        c = _site_card(s)
        pub = publicip.card(s["id"]) or {}
        state = "✅" if c.get("reachable") else "❌"
        extra = []
        if c.get("latency_ms") is not None and c.get("reachable"):
            extra.append(f"{round(c['latency_ms'])} ms")
        if pub.get("ip"):
            extra.append(f"from {tgapi.esc(pub['ip'])}" + ("" if pub.get("live") else " (not live)"))
        lines.append(f"{state} <b>{tgapi.esc(c.get('name') or s['id'])}</b> {tgapi.esc(s.get('vpn_ip') or '')}"
                     + (" · " + " · ".join(extra) if extra else ""))
    return "\n".join(lines), None


def c_backups(bot, args, who):
    now = time.time()
    lines = ["💾 <b>Config backups</b>"]
    for s in _sites():
        lst = backups.list_(s["id"])
        if not lst:
            lines.append(f"❔ {tgapi.esc(s.get('name') or s['id'])}: none")
            continue
        age = now - lst[0]["ts"]
        icon = "✅" if age < 26 * 3600 else "🟠"
        lines.append(f"{icon} {tgapi.esc(s.get('name') or s['id'])}: {ago(age)} ago ({len(lst)} kept)")
    return "\n".join(lines), None


def c_hubhealth(bot, args, who):
    home = next((s for s in hubconfig.load().get("sites", [])
                 if str(s.get("vpn_ip", "")).startswith("192.168.")), None)
    si = (_snapshot(home["id"]) or {}).get("sysinfo") if home else None
    if not si:
        return "No reading of ServerPI yet (it comes from the home site's Pi health).", None
    disk = (si.get("disks") or [{}])[0]
    mem = si.get("mem") or {}
    th = si.get("throttled") or {}
    lines = ["🖥 <b>ServerPI (the hub)</b>",
             f"{si.get('temp_c', '?')} °C · load {' / '.join(str(x) for x in (si.get('load') or [])[:3])}",
             f"RAM {mem.get('used_pct', '?')} % · swap {mem.get('swap_pct', '?')} %",
             f"Disk {disk.get('used_pct', '?')} % used, {disk.get('free_gb', '?')} GB free",
             f"Up {ago(si.get('uptime_s'))}"]
    if th.get("undervoltage_now") or th.get("throttled_now"):
        lines.append("⚡ undervoltage / throttling NOW")
    return "\n".join(lines), None


def c_visit(bot, args, who):
    import visit
    s, choices = _find_site(args)
    if not s:
        return "Visit brief for which site?", [[((x.get("name") or x["id"])[:20], f"visit:{x['id']}")
                                                for x in choices[i:i + 3]] for i in range(0, len(choices), 3)]
    return visit.brief(s, _snapshot(s["id"]) or {}), None


def c_run(bot, args, who):
    """/run <site> <command> [args] — a site's look/test command, over the VPN."""
    import requests
    import siteapi
    if len(args) < 2:
        return ("e.g. <code>/run bennie status</code> · <code>/run tankwa ping gate</code> · "
                "<code>/run tweespruit internet</code>"), None
    s, _ = _find_site(args[:1])
    if not s:
        return f"No site matches “{tgapi.esc(args[0])}”.", None
    cmd, rest = args[1].lstrip("/").lower(), args[2:]
    name = s.get("name") or s["id"]

    def work():
        try:
            r = requests.post(siteapi.base_url(s) + "/api/telegram/run",
                              json={"cmd": cmd, "args": rest, "who": who},
                              headers=siteapi.headers(s), timeout=(5, 90))
            j = r.json()
        except (requests.RequestException, ValueError) as e:
            return (f"❌ {tgapi.esc(name)}: not reachable over the VPN ({tgapi.esc(e.__class__.__name__)}). "
                    "Its own group may still answer over the farm's internet."), None
        if r.status_code == 404:
            return f"❌ {tgapi.esc(name)} runs an older Netwatch without /run.", None
        head = f"🏷 <b>{tgapi.esc(name)}</b> · /{tgapi.esc(cmd)}\n"
        return (head + (j.get("text") or "") if j.get("ok") else f"❌ {tgapi.esc(name)}: {j.get('text')}"), None
    return tgbotcore.Job(f"⏳ /{tgapi.esc(cmd)} at {tgapi.esc(name)}…", work)


def c_ack(bot, args, who):
    what = " ".join(args)[:80]
    return f"🙋 <b>{tgapi.esc(who)}</b> is on it{(': ' + tgapi.esc(what)) if what else ''}.", None


COMMANDS = {name: (fn, "viewer") for name, fn in {
    "ack": c_ack,
    "visit": c_visit,
    "run": c_run,
    "help": c_help, "start": c_help, "menu": c_help, "sites": c_sites, "site": c_site,
    "vpn": c_vpn, "backups": c_backups, "hubhealth": c_hubhealth}.items()}
COMMANDS["run"] = (c_run, "operator")      # runs commands ON a site: operators only


class OpsBot(tgbotcore.Bot):
    def __init__(self, settings, offset_path):
        super().__init__(settings, COMMANDS, offset_path, name="opsbot")
        self._cmds_for = None

    def tick(self):
        st = self.settings()
        self.maybe_report(st)
        key = (tgapi.fingerprint(st.get("token")), st.get("chat_id"))
        if st.get("token") and st.get("chat_id") and key != self._cmds_for:
            res = tgapi.call(st["token"], "setMyCommands", {
                "commands": [{"command": c, "description": d} for c, d in BOT_COMMANDS],
                "scope": {"type": "chat", "chat_id": st["chat_id"]}})
            if res.ok:
                self._cmds_for = key


    report_state = None      # (get, set) of the last report date, set by telegram.start

    def maybe_report(self, st):
        """07:00 Ops daily report: one line per site + backups that are old."""
        if not st.get("chat_id") or not self.report_state:
            return
        get, put = self.report_state
        lt = time.localtime()
        today = time.strftime("%Y-%m-%d", lt)
        if not get():                    # first start: begin tomorrow, not right now
            put(today)
            return
        if get() == today or (lt.tm_hour, lt.tm_min) < (7, 0):
            return
        put(today)
        text, rows = c_sites(self, [], "daily report")
        old = [s for s in _sites() if (lambda b: not b or time.time() - b[0]["ts"] > 26 * 3600)(backups.list_(s["id"]))]
        if old:
            text += "\n🟠 Config backup older than a day: " + ", ".join(tgapi.esc(s.get("name") or s["id"]) for s in old)
        self.send(f"☀ <b>Daily report</b> · {time.strftime('%a %d %b')}\n" + text.split("\n", 1)[-1], buttons=rows)


def configure(site_card, snapshot):
    global _site_card, _snapshot
    _site_card, _snapshot = site_card, snapshot


# ---- phase 6: the hub answers in a DARK site's group ------------------------------------
class StandInBot(tgbotcore.Bot):
    """While a site is dark (no VPN, no heartbeat), its own bot is silent — so the
    hub polls THAT site's bot and answers /status and /visit in the site's group,
    clearly marked. It lets go at once: when the hub reaches the site again, when a
    fresh heartbeat arrives (telegram.on_poll stops it), or on the first 409 (the
    site's own bot is polling again — the site always wins)."""
    def __init__(self, site, settings, offset_path):
        sid = site["id"]

        def status(bot, args, who):
            text, _ = c_site(bot, [sid], who)
            return text, [[("🧰 Visit brief", "visit"), ("📋 Status", "status")]]

        def visit_(bot, args, who):
            import visit
            return visit.brief(site, _snapshot(sid) or {}), None

        def help_(bot, args, who):
            return ("The farm's own Pi is not reachable, so the hub answers for it:\n"
                    "/status — what the hub knows\n/visit — what's wrong and what to bring"), \
                [[("📋 Status", "status"), ("🧰 Visit brief", "visit")]]
        cmds = {"status": (status, "viewer"), "visit": (visit_, "viewer"), "help": (help_, "viewer"),
                "menu": (help_, "viewer"), "start": (help_, "viewer"), "down": (status, "viewer"),
                "internet": (status, "viewer"), "hub": (status, "viewer")}
        super().__init__(settings, cmds, offset_path, name=f"standin-{sid}")
        self.site = site
        self.active = True

    def send(self, text, reply_to=None, buttons=None):
        name = (self.site.get("name") or self.site["id"])
        return super().send(f"🛰 <i>Answered by the FarmCamSA hub — {tgapi.esc(name)} itself is not "
                            f"reachable.</i>\n{text}", reply_to=reply_to, buttons=buttons)

    def loop(self):
        while self.active:
            try:
                wait = self.poll_once(timeout=25)
            except Exception as e:
                print(f"[{self.name}] {e.__class__.__name__}: {e}", flush=True)
                wait = 30
            if self.health["conflict_until"] > self.clock():
                print(f"[{self.name}] the site's own bot is back (409): standing down", flush=True)
                self.active = False
                break
            if wait and self.active:
                time.sleep(min(wait, 10))
