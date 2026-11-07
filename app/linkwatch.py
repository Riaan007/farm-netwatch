"""Telegram phase 3 on the site: "is the internet on while the hub link is down?"

Every minute, ONLY while this Pi's tunnel to the hub is down (a Pi with no
tunnel does nothing), it walks the ladder from plan section 6.1 and names the
first rung that fails:

  router → DNS → internet → Telegram → the hub's NAME (does it still resolve
  to the address the tunnel uses?) → the hub's HOUSE (does its public web
  port answer?) → the tunnel itself.

  * After DIAG_AFTER_S down with the internet working, it posts ONE diagnosis
    in the site's own group (over the farm's internet — the VPN is the thing
    that is broken).
  * While down it sends a small SIGNED heartbeat to its private ntfy topic
    (at once, then every HB_EVERY_S, plus once a day when all is well), so the
    hub can tell "alive, tunnel broken" from "dark" (hub/app/heartbeats.py).
    Flags only — no names, no addresses. HMAC-SHA256 with a per-site key the
    hub gave us over the VPN (`hb_key`).
  * When the link comes back after REPORT_MIN_S it reports the outage: how
    long, whether the internet was down too, and whether the Pi itself
    restarted in the middle (its uptime is shorter than the outage → power).

Repairing the tunnel is NOT done here: vpnheal (re-resolve the endpoint, new
listen port) owns that; this only reads its state from /api/hub/status.
The outage start is kept in /data/linkwatch.json (written on change only),
so a Pi that lost power still knows when the outage began.
"""
import hashlib
import hmac
import json
import os
import re
import socket
import threading
import time

import requests

DATA_DIR = os.environ.get("NETWATCH_DATA", "/data")
STATE_PATH = os.path.join(DATA_DIR, "linkwatch.json")
TICK_S = 60
DIAG_AFTER_S = 180
HB_EVERY_S = 300
HB_DAILY_S = 86400
REPORT_MIN_S = 180
WEB_PORT = 443


def sign(payload, key):
    body = {k: v for k, v in payload.items() if k != "sig"}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(key.encode(), raw, hashlib.sha256).hexdigest()


def _dur(s):
    s = int(max(0, s))
    if s < 3600:
        return f"{max(1, s // 60)} min"
    h, m = divmod(s // 60, 60)
    return f"{h} h {m} min" if h < 48 else f"{h // 24} days"


def _hm(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def endpoint_host(conf_paths):
    """(host, port) from the first wg0.conf that has an Endpoint line."""
    for p in conf_paths:
        try:
            with open(p) as f:
                m = re.search(r"^\s*Endpoint\s*=\s*\[?([^\]\s]+?)\]?:(\d+)\s*$", f.read(), re.M)
        except OSError:
            continue
        if m:
            return m.group(1), int(m.group(2))
    return None, None


class Probes:
    """The network checks, separate so tests can replace them."""
    def resolve(self, host):
        try:
            return sorted({a[4][0] for a in socket.getaddrinfo(host, None, socket.AF_INET)})
        except OSError:
            return []

    def tcp(self, ip, port, timeout=5):
        try:
            socket.create_connection((ip, port), timeout=timeout).close()
            return True
        except OSError:
            return False

    def https(self, url, timeout=6):
        try:
            requests.head(url, timeout=timeout)
            return True
        except requests.RequestException:
            return False


class LinkWatch:
    def __init__(self, settings, api_get, alert, *, probes=None, clock=time.time,
                 conf_paths=None, state_path=None, telegram_ok=None):
        """settings() → telegram.json (hb_topic/hb_key/hb_server); api_get(path) → own API;
        alert(level, title, body, buttons) → the site's group; telegram_ok() → True when
        the bot polled Telegram in the last 2 minutes (saves a request)."""
        self.settings = settings
        self.api_get = api_get
        self.alert = alert
        self.probes = probes or Probes()
        self.clock = clock
        self.conf_paths = conf_paths or ["/data/wg/wg0.conf", "/data/wg-client/wg_confs/wg0.conf"]
        self.state_path = state_path or STATE_PATH
        self.telegram_ok = telegram_ok or (lambda: None)
        self.st = self._load()
        self.seq = int(self.st.get("seq") or 0)
        self.last = {}            # last ladder, for /hub and tests

    # ---- persistence (on change only) ------------------------------------------
    def _load(self):
        try:
            with open(self.state_path) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self):
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self.st, f)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    # ---- the ladder --------------------------------------------------------------
    def ladder(self, h, i):
        L = {"gateway": i.get("gateway") if i.get("has_gateway") else None,
             "dns": i.get("dns"), "internet": i.get("external")}
        tok = self.telegram_ok()
        L["telegram"] = tok if tok is not None else (
            self.probes.https("https://api.telegram.org") if L["internet"] else False)
        host, port = endpoint_host(self.conf_paths)
        tunnel_ip = (h.get("endpoint") or "").rsplit(":", 1)[0].strip("[]") or None
        L.update(host=host, port=port, tunnel_ip=tunnel_ip, name_ips=[], name_ok=None, hub_web=None)
        if host and L["internet"]:
            ips = [host] if re.fullmatch(r"[\d.]+", host) else self.probes.resolve(host)
            L["name_ips"] = ips
            L["name_ok"] = bool(ips) and (tunnel_ip in ips if tunnel_ip else True)
            target = ips[0] if ips else tunnel_ip
            if target:
                L["hub_web"] = self.probes.tcp(target, WEB_PORT)
        L["handshake_s"] = h.get("last_handshake_s")
        L["self_heal"] = (h.get("self_heal") or {}).get("last_action")
        return L

    @staticmethod
    def state_of(L):
        if L.get("gateway") is False:
            return "router_down"
        if L.get("internet") is False:
            return "internet_down"
        if L.get("dns") is False:
            return "dns_broken"
        if L.get("telegram") is False:
            return "filtered"
        if L.get("name_ok") is False:
            return "hub_address_stale"
        if L.get("hub_web") is False:
            return "hub_side_down"
        return "tunnel_down_internet_up"

    @staticmethod
    def explain(L, state):
        """Plain lines for the group."""
        ok = lambda v: "✅" if v else ("❌" if v is False else "❔")  # noqa: E731
        lines = [f"Router {ok(L.get('gateway'))} · DNS {ok(L.get('dns'))} · Internet {ok(L.get('internet'))} · "
                 f"Telegram {ok(L.get('telegram'))}"]
        if L.get("host"):
            ips = ", ".join(L.get("name_ips") or []) or "does not resolve"
            lines.append(f"Hub name {L['host']} → {ips}; tunnel uses {L.get('tunnel_ip') or 'nothing yet'}")
        if L.get("hub_web") is not None:
            lines.append(f"Hub's house (web port): {'answers' if L['hub_web'] else 'NO answer'}")
        hs = L.get("handshake_s")
        lines.append(f"Last tunnel handshake: {_dur(hs) + ' ago' if hs is not None else 'none'}")
        why = {
            "dns_broken": "The router's DNS is broken; the internet itself works.",
            "filtered": "The internet answers but Telegram/web traffic does not — something filters it.",
            "hub_address_stale": "The hub's name points somewhere new and the tunnel still uses the old "
                                 "address (home IP changed). The self-repair re-points it.",
            "hub_side_down": "The hub's house does not answer either — most likely the hub's internet or "
                             "power, not this farm. Nothing to do here.",
            "tunnel_down_internet_up": "This farm's internet and the hub's house are both fine; only the "
                                       "tunnel is stuck (UDP blocked or a stale NAT entry on the farm router). "
                                       "The self-repair tries a new port.",
        }.get(state)
        if why:
            lines.append("Likely: " + why)
        if L.get("self_heal"):
            lines.append(f"Self-repair: {str(L['self_heal'])[:140]}")
        return lines

    # ---- heartbeats ----------------------------------------------------------------
    def heartbeat(self, state, L, now, uptime):
        st = self.settings()
        topic, key = st.get("hb_topic"), st.get("hb_key")
        if not topic or not key:
            return False
        self.seq += 1
        b = lambda v: None if v is None else int(bool(v))  # noqa: E731
        payload = {"v": 1, "t": int(now), "seq": self.seq, "st": state,
                   "hs": L.get("handshake_s"), "gw": b(L.get("gateway")), "inet": b(L.get("internet")),
                   "dns": b(L.get("dns")), "tg": b(L.get("telegram")), "web": b(L.get("hub_web")),
                   "name": b(L.get("name_ok")), "up": uptime}
        payload["sig"] = sign(payload, key)
        server = (st.get("hb_server") or "https://ntfy.sh").rstrip("/")
        try:
            r = requests.post(f"{server}/{topic}", data=json.dumps(payload).encode(),
                              headers={"Priority": "min", "Title": "hb"}, timeout=8)
            ok = r.status_code < 300
        except requests.RequestException:
            ok = False
        if ok:
            self.st["last_hb"] = int(now)
            self.st["seq"] = self.seq
        return ok

    # ---- one minute ------------------------------------------------------------------
    def tick(self):
        h = self.api_get("/api/hub/status") or {}
        if not h.get("configured"):
            return None
        now = self.clock()
        i = self.api_get("/api/internet") or {}
        si = self.api_get("/api/sysinfo") or {}
        uptime = si.get("uptime_s")
        up = bool(h.get("connected") and h.get("hub_reachable"))
        if up:
            if self.st.get("down_since"):
                self._ended(now, uptime)
            elif now - float(self.st.get("last_hb") or 0) > HB_DAILY_S:
                self.heartbeat("ok", {"gateway": True, "internet": True, "dns": True,
                                      "handshake_s": h.get("last_handshake_s")}, now, uptime)
                self._save()
            return "ok"
        L = self.ladder(h, i)
        self.last = L
        state = self.state_of(L)
        changed = False
        if not self.st.get("down_since"):
            self.st.update(down_since=int(now), inet_down=False, diag_sent=False, states=[])
            changed = True
            self.heartbeat(state, L, now, uptime)
        if L.get("internet") is False and not self.st.get("inet_down"):
            self.st["inet_down"] = True
            changed = True
        if state not in self.st.get("states", []):
            self.st["states"] = (self.st.get("states") or []) + [state]
            changed = True
        down_for = now - self.st["down_since"]
        if not self.st.get("diag_sent") and down_for >= DIAG_AFTER_S and L.get("internet"):
            self.alert("warning", "Hub link down — internet is UP",
                       f"Since {_hm(self.st['down_since'])} ({_dur(down_for)}).\n" +
                       "\n".join(self.explain(L, state)),
                       [[("🔗 Hub link", "hub"), ("🌐 Internet", "internet")]])
            self.st["diag_sent"] = True
            changed = True
        if now - float(self.st.get("last_hb") or 0) >= HB_EVERY_S:
            changed |= self.heartbeat(state, L, now, uptime)
        if changed:
            self._save()
        return state

    def _ended(self, now, uptime):
        start = float(self.st["down_since"])
        dur = now - start
        if dur >= REPORT_MIN_S:
            lines = [f"Down {_hm(start)} → {_hm(now)} ({_dur(dur)})."]
            lines.append("The internet here was down for part of it." if self.st.get("inet_down")
                         else "The internet here stayed up (as far as the Pi could see).")
            if uptime is not None and uptime < dur:
                lines.append(f"⚡ The Pi itself restarted during the outage (up {_dur(uptime)}) — "
                             "most likely a power cut at the farm.")
            self.alert("info", "Hub link is back", "\n".join(lines), [[("🔗 Hub link", "hub"), ("📉 Outages", "outages")]])
        for k in ("down_since", "inet_down", "diag_sent", "states"):
            self.st.pop(k, None)
        self._save()

    # ---- thread ----------------------------------------------------------------------
    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="linkwatch").start()

    def _loop(self):
        time.sleep(30)                       # let the first heartbeat sample land
        while True:
            try:
                self.tick()
            except Exception as e:           # never let the thread die
                print(f"[linkwatch] {e.__class__.__name__}: {e}", flush=True)
            time.sleep(TICK_S)
