"""Camera pictures — the engine behind the Cameras tab (phase 1 of the Hikvision
plan, ~/reports/netwatch/Hikvision_camera_management_plan_2026-11-03.md).

Which cameras: every device the scan calls a camera or a recorder, plus every
channel an NVR reports that the scan can't see itself (a camera on the NVR's own
PoE ports, or one hidden behind a wireless bridge).

Two ways to a picture, chosen per camera:
  direct — the camera's own saved login, asking for a small size it supports.
           Firmware differs (home cameras, 2026-11-03): V5.5 honours a size from
           its own list (a 5120x1440 panorama sends 640x360, 19 KB, but ignores
           an exact 640x180 and sends the full 1.2 MB); V5.7.13 answers 403 to
           every size but sends its sub-stream size (640x360) from channel 102;
           V5.4 answers 503 to any size. What works is found once per camera
           and remembered (_negotiate).
  nvr    — the recorder's login and the camera's channel N:
           /ISAPI/Streaming/channels/N02/picture is always a small 704x576
           (~40-60 KB). With any size parameter an NVR sends the main stream
           instead (1920x1088, ~190 KB) — that is the "full" picture.
A camera with its own login uses it and falls back to its NVR; a camera whose
firmware can't send small pictures goes to its NVR first. The aspect of the
picture can differ from the camera's (an NVR squeezes everything into 704x576),
so the listing carries the camera's own aspect for the page to draw it with.

Privacy (POPIA): pictures live in memory. The ONE copy kept on disk per camera is
its last good small picture (/data/camera_pics), saved at most every
camera.save_hours — Riaan asked (2026-11-03) that a camera that is off still
shows what it looks at. Switching "Camera pictures" off in Settings deletes them.
The routes that return pictures need the site login or the hub key.

Every login goes through hikisapi: one try per saved login, because Hikvision
locks accounts after a few wrong passwords.
"""
import collections
import hashlib
import os
import threading
import time

import camrtsp
import camsnap
import config
import creds
import hikisapi
import identify

DATA_DIR = os.environ.get("NETWATCH_DATA", "/data")
PIC_DIR = os.path.join(DATA_DIR, "camera_pics")

PIC_PATH = "/ISAPI/Streaming/channels/101/picture"
SUB_PATH = "/ISAPI/Streaming/channels/102/picture"   # small on V5.7 firmware, full size on V5.5
THUMB_MAX = 400 << 10          # a "small" picture bigger than this means the size was ignored
FULL_MAX = 8 << 20
SAVE_MAX = 1536 << 10          # never keep more than this per camera on the SD card
MEM_THUMBS = 16 << 20          # Tweespruit's Pi 3 has 905 MB of RAM — 16 MB of pictures is plenty
MEM_FULLS = 8 << 20
FULL_TTL = 120
FRESH_MIN_S = 2                # a picture this young is served even to "fresh=1"
ON_DEMAND_MAX_AGE = 300        # with no background refresh, a view older than this is read again
CAPS_TTL = 24 * 3600
NVR_READ_S = 15 * 60
LIST_TTL = 10
START_DELAY_S = 60
GAP_S = 0.3                    # pause between two cameras in a background round
SAVED_UNKNOWN_KEEP_S = 30 * 86400
PARALLEL = 3                   # device requests at once on this Pi
MAX_VIEWS = 4                  # lenses / video inputs shown per camera
PER_NVR = 2

WHY = {
    "off": "Camera pictures are turned off in this site's Settings",
    "offline": "The camera is offline",
    "no_login": "No login saved for this camera or for a recorder it is on",
    "login_refused": "The camera refused its saved login — test or fix the login",
    "nvr_login_refused": "Its recorder refused the saved login — test or fix the recorder's login",
    "nvr_offline": "Its recorder is offline",
    "chan_offline": "Its recorder says this camera is not connected",
    "not_supported": "Netwatch found no Hikvision ISAPI or ONVIF snapshot for this camera",
    "failed": "The last try to read a picture failed",
    "unknown": "Unknown camera",
}
_SOURCE_FIELDS = {"model": "model", "serial": "serialNumber", "firmware": "firmwareVersion"}
_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


class BadPicture(Exception):
    pass


def jpeg_size(b):
    """(width, height) from a JPEG's frame header, or None when b isn't a JPEG."""
    if len(b) < 4 or b[0] != 0xFF or b[1] != 0xD8:
        return None
    i, n = 2, len(b)
    while i + 4 <= n:
        if b[i] != 0xFF:
            return None
        m = b[i + 1]
        if m == 0xFF:                       # fill byte
            i += 1
            continue
        if m == 0x01 or 0xD0 <= m <= 0xD8:  # markers without a length
            i += 2
            continue
        if m == 0xDA:                       # image data starts: no frame header seen
            return None
        seg = (b[i + 2] << 8) | b[i + 3]
        if m in _SOF:
            if i + 9 > n:
                return None
            h, w = (b[i + 5] << 8) | b[i + 6], (b[i + 7] << 8) | b[i + 8]
            return (w, h) if w and h else None
        i += 2 + seg
    return None


class Pic:
    __slots__ = ("data", "ts", "route", "w", "h")

    def __init__(self, data, ts, route, w, h):
        self.data, self.ts, self.route, self.w, self.h = data, ts, route, w, h


def _hikish(d):
    """Does this device speak ISAPI? Vendor, model, or Hikvision's SDK port 8000 —
    a camera on a routed subnet has no MAC vendor, and few other cameras open 8000."""
    v, m = (d.get("vendor") or "").lower(), (d.get("model") or "").upper()
    # "Hangzhou Ezviz Software" is Hikvision's own consumer brand.
    return ("hikvision" in v or "hangzhou" in v or m.startswith(("DS-", "IDS-", "HIKVISION"))
            or 8000 in (d.get("ports") or ()))


def _model_kind(d):
    """What the device's Hikvision model says it is ("camera", "nvr", "access",
    "intercom", "network"), or None — identify.hik_kind_of: the model discovery
    heard it give, else the one on its record. It overrides the category both
    ways: the category may be the scan's guess from open ports (a PoE switch
    with only port 8000 open "is" a recorder), or an old choice saved with it."""
    kind = identify.hik_kind_of(d)
    return kind[0] if kind else None


def _is_recorder(d, lists):
    """A recorder by its model; with no model that says, by its category or an
    NVR channel list read from it."""
    kind = _model_kind(d)
    if kind:
        return kind == "nvr"
    return d.get("category") == "nvr" or d.get("key") in lists


def _ipnum(ip):
    try:
        return tuple(int(p) for p in str(ip).split("."))
    except ValueError:
        return (999,)


def _size_params(wh):
    return {"videoResolutionWidth": wh[0], "videoResolutionHeight": wh[1]}


def _candidates(caps):
    """Small sizes worth asking a camera for, best first: its own sub-stream size
    (always one it supports, in its own shape) when that is small, then the usual
    sub-stream sizes in the camera's shape, then a large sub-stream as a last try."""
    main, sub = caps.get("main"), caps.get("sub")
    wide = (main[0] / main[1]) >= 1.5 if main else True
    out = []
    if sub and sub[0] <= 800:
        out.append(sub)
    out += [(640, 360), (640, 480), (704, 576)] if wide else [(640, 480), (704, 576), (640, 360)]
    if sub and sub[0] > 800:
        out.append(sub)
    seen, uniq = set(), []
    for wh in out:
        if wh not in seen:
            seen.add(wh)
            uniq.append(wh)
    return uniq


def read_nvr_channels(client):
    """An NVR's channels via hikisapi: [{id, name, ip, online, model, serial,
    firmware}] — the same shape as hikvision.nvr_channels, or None."""
    r = client.get("/ISAPI/ContentMgmt/InputProxy/channels", max_bytes=2 << 20)
    root = hikisapi.parse(r.content) if r.ok else None
    if root is None:
        return None
    out = []
    for ch in root.iter():
        if hikisapi.tag(ch) != "InputProxyChannel":
            continue
        ip = next((e.text.strip() for e in ch.iter() if hikisapi.tag(e) == "ipAddress" and e.text), "")
        src = next((e for e in ch if hikisapi.tag(e) == "sourceInputPortDescriptor"), None)
        out.append({"id": hikisapi.child_text(ch, "id"), "name": hikisapi.child_text(ch, "name"), "ip": ip,
                    **{k: hikisapi.child_text(src, t) for k, t in _SOURCE_FIELDS.items()}})
    status = {}
    try:
        r2 = client.get("/ISAPI/ContentMgmt/InputProxy/channels/status", max_bytes=2 << 20)
        root2 = hikisapi.parse(r2.content) if r2.ok else None
        for st in (root2.iter() if root2 is not None else ()):
            if hikisapi.tag(st) == "InputProxyChannelStatus":
                cid, on = hikisapi.child_text(st, "id"), hikisapi.child_text(st, "online").lower()
                if cid and on in ("true", "false"):
                    status[cid] = on == "true"
    except (hikisapi.Unreachable, hikisapi.TooBig):
        pass
    for ch in out:
        ch["online"] = status.get(ch["id"])
    return out


class Engine:
    def __init__(self):
        self._lock = threading.Lock()
        self._thumbs = collections.OrderedDict()     # key -> Pic, least recently used first
        self._fulls = collections.OrderedDict()
        self._caps = {}           # camera key -> {"ts", "main": (w, h), "sub": (w, h), "thumb": (w, h)|"none"|None}
        self._fail = {}           # camera key -> (ts, message)
        self._saved_ts = {}       # camera key -> mtime of its saved picture (None = none)
        self._own_lists = {}      # nvr key -> (read at, nvr ip, channels) read by this engine
        self._nvr_read_at = {}
        self._dev_locks = collections.defaultdict(threading.Lock)
        self._nvr_sems = collections.defaultdict(lambda: threading.BoundedSemaphore(PER_NVR))
        self._sem = threading.BoundedSemaphore(PARALLEL)
        self._list = (0, None)
        self._wake = threading.Event()
        self._forced = False
        self._purged = False
        self._started = False
        self._stop = False
        self.health = None        # hikhealth.Health, set by its start()
        self.devices = lambda: []
        self.registry = lambda: {}
        self.nvr_lists = lambda: {}
        self.multi_macs = lambda: set()
        self.cfg = config.load

    # ---- wiring -------------------------------------------------------------------
    def start(self, devices, registry, nvr_lists, multi_macs=None, cfg=None, thread=True):
        self.devices, self.registry, self.nvr_lists = devices, registry, nvr_lists
        self.multi_macs = multi_macs or (lambda: set())
        self.cfg = cfg or config.load
        if thread and not self._started:
            self._started = True
            threading.Thread(target=self._loop, daemon=True, name="campics").start()

    def _settings(self):
        c = self.cfg() or {}
        cam = c.get("camera") or {}
        return {"enabled": bool((c.get("features") or {}).get("camera_pictures", True)),
                "refresh_min": max(0, int(cam.get("refresh_min", 30) or 0)),
                "save_hours": max(1, int(cam.get("save_hours", 6) or 6))}

    def _max_age(self, st):
        return st["refresh_min"] * 60 if st["refresh_min"] else ON_DEMAND_MAX_AGE

    # ---- which cameras, and the way to each ------------------------------------------
    def _lists(self):
        """Every NVR's newest channel list: the scanner's names pass and this engine
        both read them; the newer one wins."""
        out = dict(self.nvr_lists() or {})
        with self._lock:
            own = dict(self._own_lists)
        for k, v in own.items():
            if k not in out or v[0] >= out[k][0]:
                out[k] = v
        return out

    def inventory(self, force=False):
        now = time.time()
        with self._lock:
            ts, cached = self._list
        if cached is not None and not force and now - ts < LIST_TTL:
            return cached
        inv = self._build()
        with self._lock:
            self._list = (time.time(), inv)
        return inv

    def _build(self):
        devices = [d for d in (self.devices() or []) if d.get("key")]
        reg = self.registry() or {}
        have = creds.keys_with_creds()
        lists = self._lists()
        multi = {str(m).lower() for m in (self.multi_macs() or ())}
        by_key = {d["key"]: d for d in devices}
        try:
            import radiomon
            radio = radiomon.is_radio
        except Exception:  # noqa: BLE001 - only used to leave radios out
            radio = lambda d: False     # noqa: E731

        def login(key):
            if key not in have:
                return None
            c = creds.get(key)
            return {"refused": hikisapi.refused(key, c["username"], c["password"], reg)}

        recorders = {}
        for d in devices:
            if _is_recorder(d, lists):
                recorders[d["key"]] = d
        rec_login = {k: login(k) for k in recorders}

        # camera IP -> the best channel for it over every NVR's list. A camera with two
        # views on one address (a thermal + normal camera, a dual-lens one) has two
        # channels on the same NVR: they become the VIEWS of one camera (one tile,
        # both pictures), in channel order. The same camera on a second NVR is not repeated.
        chans, seen = {}, {}
        for nk, (read_at, nvr_ip, cl) in lists.items():
            nd = by_key.get(nk) or {"key": nk, "ip": nvr_ip, "online": True}
            lg = rec_login.get(nk) if nk in recorders else login(nk)
            for c in cl or []:
                if not c.get("ip"):
                    continue
                seen.setdefault((nk, c["ip"]), []).append((nd, c, lg, read_at))
                rank = ({True: 0, None: 1}.get(c.get("online"), 2), 0 if (lg and not lg["refused"]) else 1,
                        _ipnum(nvr_ip), _ipnum(c.get("id")))
                if c["ip"] not in chans or rank < chans[c["ip"]][0]:
                    chans[c["ip"]] = (rank, nd, c, lg, read_at)
        sibs = {}                 # camera IP -> its channels on the chosen NVR, first channel first
        for ip, (rank, nd, best, lg, read_at) in list(chans.items()):
            mine = sorted((c for (_n, c, _l, _t) in seen.get((nd["key"], ip), [])), key=lambda c: _ipnum(c.get("id")))
            sibs[ip] = mine
            if mine and mine[0] is not best:      # view 1 is the camera's first channel
                chans[ip] = (rank, nd, mine[0], lg, read_at)

        cams, covered = [], set()
        for d in devices:
            if d["key"] in recorders or radio(d):
                continue
            ip = d.get("ip")
            in_multi = (d.get("mac") or "").lower() in multi
            on_nvr = bool(ip and ip in chans and not in_multi)
            kind = _model_kind(d)
            if kind == "network":
                continue           # a switch, whatever an old guess or an NVR channel says
            if kind in ("access", "intercom"):
                if not on_nvr:
                    continue       # a terminal / door station only counts once an NVR records it
            elif not (kind == "camera" or d.get("category") == "camera" or on_nvr
                      or (_hikish(d) and 554 in (d.get("ports") or []))):
                continue
            if in_multi and ip in chans:
                continue           # a bridge's merged record: its channel stands for it below
            hit = chans.get(ip) if ip and not in_multi else None
            if hit:
                covered.add(ip)
            cams.append(self._entry(d, hit, login(d["key"]), reg, sibs.get(ip) if hit else None))
        for ip, hit in chans.items():
            if ip not in covered:
                cams.append(self._entry(None, hit, None, reg, sibs.get(ip)))
        cams.sort(key=lambda e: (str(e["nvr"]["name"] or "") if e["nvr"] else "~",
                                 _ipnum(e["nvr"]["channel"]) if e["nvr"] else (0,), _ipnum(e["ip"])))

        recs = []
        for k, d in recorders.items():
            lg = rec_login[k]
            cl = (lists.get(k) or (0, "", []))[2] or []
            recs.append({"key": k, "name": d.get("name") or d.get("device_name") or d.get("model") or d.get("ip") or k,
                         "ip": d.get("ip") or "", "model": d.get("model") or "", "firmware": d.get("firmware") or "",
                         "online": bool(d.get("online")), "has_login": lg is not None,
                         "login_refused": bool(lg and lg["refused"]),
                         "channels": len(cl), "channels_online": sum(1 for c in cl if c.get("online") is True),
                         "channels_offline": sum(1 for c in cl if c.get("online") is False),
                         "read_at": int((lists.get(k) or (0,))[0]) or None})
        recs.sort(key=lambda r: _ipnum(r["ip"]))
        return {"cameras": cams, "recorders": recs}

    def _entry(self, d, hit, own, reg, sibs=None):
        """One camera: what it is, which way Netwatch reaches its picture, and why
        not when it can't. `hit` = (rank, nvr device, channel, nvr login, read at);
        `sibs` = all its channels on that NVR (a 2-lens camera has two)."""
        nvr, chan, nlg = (hit[1], hit[2], hit[3]) if hit else (None, None, None)
        if d:
            key, ip = d["key"], d.get("ip") or ""
            name = d.get("name") or d.get("device_name") or (chan or {}).get("name") or d.get("model") or ip
            online = bool(d.get("online"))
            hik = _hikish(d)           # its own login is only any use over ISAPI
            model, firmware = d.get("model") or (chan or {}).get("model") or "", d.get("firmware") or ""
        else:
            key, ip = f"ch:{nvr['key']}:{chan.get('id')}", chan.get("ip") or ""
            name = chan.get("name") or f"Channel {chan.get('id')}"
            online = chan.get("online") is not False and bool(nvr.get("online"))
            hik, model, firmware = True, chan.get("model") or "", chan.get("firmware") or ""
        with self._lock:
            caps = dict(self._caps.get(key) or {})
            fail = self._fail.get(key)
        snap = caps.get("snap")
        gen_login = bool(own and not own["refused"] and d and not hik)
        http_override = bool(gen_login and self._snap_override(d, reg))
        rtsp_override = bool(gen_login and self._rtsp_override(d, reg))
        own_ok = bool(own and not own["refused"] and hik and d)
        # A non-Hikvision camera with a login reaches its picture generically — an
        # ONVIF snapshot URL (or an override), see _snap_url. snap=="none" means we
        # asked and the camera offered none; http=="bad" means the URL it gave does
        # not actually return a picture (the Dynacolor's 404s), so fall to RTSP.
        gen_ok = bool(gen_login and caps.get("http") != "bad" and (http_override or snap != "none"))
        # Last resort for such a camera: one frame off its RTSP stream (needs ffmpeg).
        rtsp_ok = bool(gen_login and camrtsp.available() and (rtsp_override or caps.get("rtsp") != "none"))
        via_ok = bool(nvr and nlg and not nlg["refused"] and nvr.get("online") and chan.get("online") is not False)
        if own_ok and caps.get("thumb") == "none" and via_ok:
            route = "nvr"                     # this firmware can't send small pictures
        elif own_ok:
            route = "direct"
        elif gen_ok:
            route = "own-generic"             # not Hikvision: fetch its own snapshot URL
        elif rtsp_ok:
            route = "own-rtsp"                # no working HTTP snapshot: one RTSP frame
        elif via_ok:
            route = "nvr"
        else:
            route = None
        if not online:
            why = "offline"
        elif route:
            why = "failed" if fail and time.time() - fail[0] < 600 else ""
        elif own and own["refused"]:
            why = "login_refused"
        elif nvr and nlg and nlg["refused"]:
            why = "nvr_login_refused"
        elif nvr and nlg and not nvr.get("online"):
            why = "nvr_offline"
        elif nvr and nlg and chan.get("online") is False:
            why = "chan_offline"
        elif own and not hik:
            why = "not_supported"
        else:
            why = "no_login"
        main = caps.get("main")
        # Views: the NVR's channels at this address, or the camera's own video inputs
        # (read with its own login) — whichever says more. View 1 is the camera itself.
        sibs = list(sibs or ([chan] if chan else []))
        n_views = min(MAX_VIEWS, max(len(sibs), int(caps.get("inputs") or 1), 1))
        views = [{"n": i, "name": (sibs[i - 1].get("name") if i <= len(sibs) else "") or f"Lens {i}",
                  "picture": self._pic_meta(self.view_key(key, i))} for i in range(1, n_views + 1)]
        e = {"key": key, "kind": "channel" if d is None else "camera", "ip": ip,
             "mac": (d or {}).get("mac") or "", "name": str(name or key)[:80], "model": model, "firmware": firmware,
             "online": online, "last_seen": (d or {}).get("last_seen"), "has_login": own is not None,
             "route": route, "why": why, "why_text": WHY.get(why, "") if why else "",
             "fail": fail[1] if fail and why == "failed" else "",
             "aspect": round(main[0] / main[1], 4) if main else None,
             "nvr": {"key": nvr["key"], "name": nvr.get("name") or nvr.get("device_name") or nvr.get("model") or nvr.get("ip"),
                     "ip": nvr.get("ip"), "channel": str(chan.get("id")), "chan_online": chan.get("online")} if nvr else None,
             "picture": self._pic_meta(key), "views": views if n_views > 1 else []}
        e["_d"], e["_nvr"], e["_chan"] = d, nvr, chan
        e["_vchans"] = {i: sibs[i - 1] for i in range(1, len(sibs) + 1)}
        e["_own_ok"], e["_via_ok"], e["_rtsp_ok"] = own_ok, via_ok, rtsp_ok
        return e

    @staticmethod
    def view_key(key, view):
        """Cache / saved-picture key of one view: view 1 is the camera's own key."""
        return key if int(view or 1) <= 1 else f"{key}#v{int(view)}"

    def _pic_meta(self, key):
        with self._lock:
            p = self._thumbs.get(key)
        if p:
            return {"ts": int(p.ts), "w": p.w, "h": p.h, "route": p.route}
        ts = self._saved_mtime(key)
        return {"ts": int(ts), "route": "saved"} if ts else None

    # ---- the public listing -------------------------------------------------------------
    def listing(self):
        st = self._settings()
        inv = self.inventory() if st["enabled"] else {"cameras": [], "recorders": []}
        cams = [{k: v for k, v in e.items() if not k.startswith("_")} for e in inv["cameras"]]
        recs = [dict(r) for r in inv["recorders"]]
        if self.health is not None and st["enabled"]:
            try:
                self.health.annotate(cams, recs)         # phase 2: hikhealth.py
            except Exception as ex:  # noqa: BLE001 - pictures must not depend on health
                print(f"[cameras] health annotate failed: {ex}", flush=True)
        return {"ok": True, "enabled": st["enabled"], "refresh_min": st["refresh_min"],
                "save_hours": st["save_hours"], "ts": int(time.time()),
                "cameras": cams, "recorders": recs,
                "counts": {"cameras": len(cams), "reachable": sum(1 for e in cams if e["route"]),
                           "with_picture": sum(1 for e in cams if e["picture"])}}

    def _find(self, key):
        for e in self.inventory()["cameras"]:
            if e["key"] == key:
                return e
        for e in self.inventory(force=True)["cameras"]:      # a camera seen for the first time
            if e["key"] == key:
                return e
        return None

    # ---- pictures -----------------------------------------------------------------------
    def picture(self, key, size="thumb", fresh=False, view=1):
        """(Pic, flags, None, 200) or (None, None, {error, why}, status). `view` picks
        the lens of a camera with several (1 = the camera's main view)."""
        st = self._settings()
        if not st["enabled"]:
            return None, None, {"error": WHY["off"], "why": "off"}, 403
        e = self._find(key)
        if e is None:
            return None, None, {"error": WHY["unknown"], "why": "unknown"}, 404
        view = int(view or 1)
        if view != 1 and not 1 <= view <= len(e["views"]):
            return None, None, {"error": "this camera has no such view", "why": "unknown"}, 404
        if view != 1:
            e = dict(e, _view=view)
        return self._picture(e, self.view_key(key, view), size, fresh, st)

    def _picture(self, e, key, size, fresh, st):
        full = size == "full"
        store, cap = (self._fulls, MEM_FULLS) if full else (self._thumbs, MEM_THUMBS)
        live = bool(e["route"]) and e["online"]
        mem = self._mem_get(store, key, FULL_TTL if full else None)
        now = time.time()
        if mem and (now - mem.ts < FRESH_MIN_S or (not fresh and now - mem.ts < self._max_age(st))):
            return mem, {"stale": not live, "why": e["why"]}, None, 200
        if not live:
            best = mem or self._mem_get(self._thumbs, key) or self._saved(key)
            if best:
                return best, {"stale": True, "why": e["why"]}, None, 200
            return None, None, {"error": e["why_text"] or WHY["failed"], "why": e["why"] or "failed"}, 404
        with self._dev_locks[key]:
            mem = self._mem_get(store, key, FULL_TTL if full else None)
            if mem and time.time() - mem.ts < FRESH_MIN_S:
                return mem, {"stale": False, "why": ""}, None, 200
            try:
                pic = self._fetch(e, full)
            except (hikisapi.LoginRefused, hikisapi.Unreachable, hikisapi.TooBig, BadPicture) as ex:
                msg = str(ex) or ex.__class__.__name__
                with self._lock:
                    self._fail[key] = (time.time(), msg[:160])
                    self._list = (0, None)                 # the listing should say so at once
                best = mem or self._mem_get(self._thumbs, key) or self._saved(key)
                if best:
                    return best, {"stale": True, "why": "failed", "error": msg}, None, 200
                return None, None, {"error": msg, "why": "failed"}, 502
        with self._lock:
            self._fail.pop(key, None)
        self._mem_put(store, key, pic, cap)
        if not full:
            self._maybe_save(key, pic, st["save_hours"])
        return pic, {"stale": False, "why": ""}, None, 200

    def _fetch(self, e, full):
        view = e.get("_view", 1)
        nvr_ok = e["_via_ok"] and view in e["_vchans"]       # the NVR has a channel for this view
        order = [e["route"]]
        if e["route"] == "direct" and nvr_ok:
            order.append("nvr")
        elif e["route"] == "nvr" and e["_own_ok"]:
            order.append("direct")
        elif e["route"] == "own-generic":
            if e["_rtsp_ok"]:
                order.append("own-rtsp")      # the HTTP snapshot failed: try one RTSP frame
            if nvr_ok:
                order.append("nvr")           # an NVR usually 403s a non-Hik channel, but try
        elif e["route"] == "own-rtsp" and nvr_ok:
            order.append("nvr")
        if view > 1 and e["route"] in ("nvr", "own-generic", "own-rtsp") and not nvr_ok:
            order = ["direct"] if e["_own_ok"] else []
        if not order:
            raise BadPicture("no way to this view's picture")
        last = None
        for route in order:
            try:
                with self._sem:
                    if route == "direct":
                        return self._fetch_direct(e, full)
                    if route == "own-generic":
                        return self._fetch_generic(e, full)
                    if route == "own-rtsp":
                        return self._fetch_rtsp(e, full)
                    return self._fetch_nvr(e, full)
            except (hikisapi.LoginRefused, hikisapi.Unreachable, hikisapi.TooBig, BadPicture) as ex:
                last = ex
        raise last

    def _client(self, d):
        c = creds.get(d["key"])
        return hikisapi.Client(d["key"], d.get("ip"), c["username"], c["password"], d.get("ports"), self.registry())

    def _snap_override(self, d, reg):
        """An operator-set snapshot URL for a non-Hikvision camera: a registry
        `snapshot_url` on the device, or config camera.snapshot_urls keyed by the
        device key or its IP. A path ("/cgi-bin/jpg/image.cgi") or a full URL."""
        key, ip = d["key"], d.get("ip") or ""
        r = (reg.get(key) or {}).get("snapshot_url") if reg else None
        if r:
            return r
        urls = ((self.cfg() or {}).get("camera") or {}).get("snapshot_urls") or {}
        return urls.get(key) or (urls.get(ip) if ip else None)

    def _snap_url(self, d):
        """This camera's snapshot URL: an override, else an ONVIF GetSnapshotUri
        found once and remembered (caps["snap"] = the URL, or "none")."""
        key, ip = d["key"], d.get("ip") or ""
        override = self._snap_override(d, self.registry() or {})
        if override:
            return override if "://" in override else f"http://{ip}/{override.lstrip('/')}"
        with self._lock:
            snap = (self._caps.get(key) or {}).get("snap")
        if snap and snap != "none":
            return snap
        if snap == "none":
            raise BadPicture("no snapshot URL for this camera")
        c = creds.get(key)
        uri = camsnap.snapshot_uri(ip, c["username"], c["password"], d.get("ports"), self.registry(), key)
        if uri:
            self._set_caps(key, snap=uri)
            return uri
        self._set_caps(key, snap="none")
        raise BadPicture("ONVIF offered no snapshot URL for this camera")

    @staticmethod
    def _pic(r, route):
        if r.status != 200:
            raise BadPicture(f"the device answered HTTP {r.status}")
        wh = jpeg_size(r.content)
        if not wh:
            raise BadPicture("the device's answer was not a picture")
        return Pic(r.content, time.time(), route, wh[0], wh[1])

    def _fetch_direct(self, e, full):
        d = e["_d"]
        cl = self._client(d)
        caps = self._caps_for(cl, d["key"])
        view = e.get("_view", 1)
        if view > 1:
            # Another lens (video input n): its sub-stream picture when that is small,
            # else its main one. A thermal lens is small to begin with.
            if not full:
                r = cl.get(f"/ISAPI/Streaming/channels/{view}02/picture", max_bytes=FULL_MAX)
                if r.status == 200 and jpeg_size(r.content) and len(r.content) <= THUMB_MAX:
                    return self._pic(r, "direct")
            return self._pic(cl.get(f"/ISAPI/Streaming/channels/{view}01/picture", max_bytes=FULL_MAX), "direct")
        if full:
            return self._pic(cl.get(PIC_PATH, max_bytes=FULL_MAX), "direct")
        want = caps.get("thumb")
        if want == "none":
            return self._pic(cl.get(PIC_PATH, max_bytes=FULL_MAX), "direct")
        if want == "sub":
            r = cl.get(SUB_PATH, max_bytes=FULL_MAX)
            if self._small(r, caps):
                return self._pic(r, "direct")
        elif want:
            r = cl.get(PIC_PATH, params=_size_params(want), max_bytes=FULL_MAX)
            if r.status == 200 and jpeg_size(r.content) == tuple(want) and len(r.content) <= THUMB_MAX:
                return self._pic(r, "direct")
        return self._negotiate(cl, d["key"], caps)

    def _fetch_generic(self, e, full):
        """A non-Hikvision camera's own snapshot URL (camsnap). One size — the
        camera's own frame — so thumb and full are the same picture; its shape is
        remembered so the tile is drawn right (an NVR can't give this picture)."""
        d = e["_d"]
        url = self._snap_url(d)
        c = creds.get(d["key"])
        r = camsnap.get(d["key"], url, c["username"], c["password"], registry=self.registry())
        if r.status != 200 or not jpeg_size(r.content):
            # The snapshot URL does not actually return a picture (the Dynacolor
            # advertises one over ONVIF that 404s). Remember, so the camera routes
            # to its RTSP stream next instead of asking the dud URL again.
            self._set_caps(d["key"], http="bad")
            raise BadPicture("the snapshot URL returned HTTP %s, not a picture" % r.status)
        pic = self._pic(r, "own-generic")
        self._set_caps(d["key"], main=(pic.w, pic.h))
        return pic

    def _fetch_rtsp(self, e, full):
        """One frame off the camera's RTSP stream (camrtsp / ffmpeg), for a
        non-Hikvision camera with no working HTTP snapshot. Its shape is remembered
        like _fetch_generic so the tile is drawn right."""
        d = e["_d"]
        url = self._rtsp_url(d)
        c = creds.get(d["key"])
        r = camrtsp.grab(d["key"], url, c["username"], c["password"], registry=self.registry())
        pic = self._pic(r, "own-rtsp")
        self._set_caps(d["key"], main=(pic.w, pic.h))
        return pic

    def _rtsp_override(self, d, reg):
        """An operator-set RTSP URL for a camera: registry `rtsp_url`, or config
        camera.rtsp_urls by device key or IP."""
        key, ip = d["key"], d.get("ip") or ""
        r = (reg.get(key) or {}).get("rtsp_url") if reg else None
        if r:
            return r
        urls = ((self.cfg() or {}).get("camera") or {}).get("rtsp_urls") or {}
        return urls.get(key) or (urls.get(ip) if ip else None)

    def _rtsp_url(self, d):
        """This camera's RTSP URL: an override, else an ONVIF GetStreamUri found
        once and remembered (caps["rtsp"] = the URL, or "none")."""
        key, ip = d["key"], d.get("ip") or ""
        override = self._rtsp_override(d, self.registry() or {})
        if override:
            return override
        with self._lock:
            rtsp = (self._caps.get(key) or {}).get("rtsp")
        if rtsp and rtsp != "none":
            return rtsp
        if rtsp == "none":
            raise BadPicture("no RTSP URL for this camera")
        c = creds.get(key)
        uri = camsnap.stream_uri(ip, c["username"], c["password"], d.get("ports"), self.registry(), key)
        if uri:
            self._set_caps(key, rtsp=uri)
            return uri
        self._set_caps(key, rtsp="none")
        raise BadPicture("ONVIF offered no RTSP URL for this camera")

    @staticmethod
    def _small(r, caps):
        """A picture from channel 102 that really is smaller than the main stream."""
        got = jpeg_size(r.content) if r.status == 200 else None
        main = caps.get("main")
        return bool(got) and len(r.content) <= THUMB_MAX and (not main or got[0] < main[0])

    def _negotiate(self, cl, key, caps):
        """Find a way to a small picture on this camera (see the module docstring):
        a size it honours, else its sub-stream's own picture, else none."""
        full = None
        for wh in _candidates(caps):
            r = cl.get(PIC_PATH, params=_size_params(wh), max_bytes=FULL_MAX)
            got = jpeg_size(r.content) if r.status == 200 else None
            if got == wh and len(r.content) <= THUMB_MAX:
                self._set_caps(key, thumb=wh)
                return self._pic(r, "direct")
            if got:
                full = r                  # the size was ignored: this is the full picture
            elif r.status in (400, 501, 503):
                break                     # V5.4: refuses sizes outright — don't ask for more
        r = cl.get(SUB_PATH, max_bytes=FULL_MAX)
        if self._small(r, caps):
            self._set_caps(key, thumb="sub")
            return self._pic(r, "direct")
        if not full and r.status == 200 and jpeg_size(r.content):
            full = r                      # channel 102 sent the full picture too
        self._set_caps(key, thumb="none")
        return self._pic(full or cl.get(PIC_PATH, max_bytes=FULL_MAX), "direct")

    def _caps_for(self, cl, key):
        with self._lock:
            c = self._caps.get(key)
        if c and time.time() - c["ts"] < CAPS_TTL:
            return dict(c)
        new = {"ts": time.time(), "main": None, "sub": None, "thumb": (c or {}).get("thumb"),
               "inputs": (c or {}).get("inputs")}
        try:                       # how many video inputs (lenses) the camera has
            r = cl.get("/ISAPI/System/Video/inputs/channels", max_bytes=256 << 10)
            root = hikisapi.parse(r.content) if r.ok else None
            if root is not None:
                new["inputs"] = sum(1 for el in root.iter() if hikisapi.tag(el) == "VideoInputChannel") or 1
        except (hikisapi.Unreachable, hikisapi.TooBig):
            pass
        for sid, name in (("101", "main"), ("102", "sub")):
            try:
                r = cl.get(f"/ISAPI/Streaming/channels/{sid}", max_bytes=128 << 10)
            except (hikisapi.Unreachable, hikisapi.TooBig):
                continue
            root = hikisapi.parse(r.content) if r.ok else None
            w, h = hikisapi.text(root, "videoResolutionWidth"), hikisapi.text(root, "videoResolutionHeight")
            if w.isdigit() and h.isdigit() and int(w) and int(h):
                new[name] = (int(w), int(h))
        with self._lock:
            if (c or {}).get("main") != new["main"] or (c or {}).get("inputs") != new["inputs"]:
                self._list = (0, None)           # the listing carries the camera's shape and views
            self._caps[key] = new
        return dict(new)

    def _set_caps(self, key, **kv):
        with self._lock:
            c = self._caps.setdefault(key, {"ts": time.time(), "main": None, "sub": None, "thumb": None})
            if any(c.get(k) != v for k, v in kv.items()):
                self._list = (0, None)           # e.g. "can't do small pictures" changes its route
            c.update(kv)

    def _fetch_nvr(self, e, full):
        view = e.get("_view", 1)
        chan = e["_vchans"].get(view) or e["_chan"]
        nvr, ch = e["_nvr"], str(chan.get("id") or "")
        if not ch.isdigit():
            raise BadPicture("the recorder gave this camera no channel number")
        ck = self.view_key(e["key"], view)
        cl = self._client(nvr)
        main = f"/ISAPI/Streaming/channels/{int(ch)}01/picture"
        with self._lock:
            # An old NVR (V4.1.18, Tri-Lectro) answers 400 to the sub-stream picture of
            # some channels but sends the main one; such a channel is remembered.
            use_main = self._caps.get(ck, {}).get("nvr_pic") == "main"
        with self._nvr_sems[nvr["key"]]:
            if full:     # any size parameter makes an NVR send the main stream
                r = cl.get(main, params=_size_params((1920, 1080)), max_bytes=FULL_MAX)
            elif use_main:
                r = cl.get(main, max_bytes=FULL_MAX)
            else:
                r = cl.get(f"/ISAPI/Streaming/channels/{int(ch)}02/picture", max_bytes=FULL_MAX)
                if r.status in (400, 403, 404) or (r.status == 200 and not jpeg_size(r.content)):
                    r2 = cl.get(main, max_bytes=FULL_MAX)
                    if r2.status == 200 and jpeg_size(r2.content):
                        self._set_caps(ck, nvr_pic="main")
                        r = r2
        return self._pic(r, "nvr")

    # ---- memory -----------------------------------------------------------------------
    def _mem_get(self, store, key, ttl=None):
        with self._lock:
            p = store.get(key)
            if p is None:
                return None
            if ttl and time.time() - p.ts > ttl:
                del store[key]
                return None
            store.move_to_end(key)
            return p

    def _mem_put(self, store, key, pic, cap):
        with self._lock:
            store[key] = pic
            store.move_to_end(key)
            total = sum(len(p.data) for p in store.values())
            while total > cap and len(store) > 1:
                _k, old = store.popitem(last=False)
                total -= len(old.data)

    # ---- the one saved picture per camera ------------------------------------------------
    @staticmethod
    def _path(key):
        return os.path.join(PIC_DIR, hashlib.sha1(key.encode("utf-8")).hexdigest()[:24] + ".jpg")

    def _saved_mtime(self, key):
        with self._lock:
            if key in self._saved_ts:
                return self._saved_ts[key]
        try:
            ts = os.path.getmtime(self._path(key))
        except OSError:
            ts = None
        with self._lock:
            self._saved_ts[key] = ts
        return ts

    def _saved(self, key):
        try:
            with open(self._path(key), "rb") as f:
                data = f.read(SAVE_MAX + 1)
            ts = os.path.getmtime(self._path(key))
        except OSError:
            return None
        wh = jpeg_size(data)
        return Pic(data, ts, "saved", wh[0], wh[1]) if wh and len(data) <= SAVE_MAX else None

    def _maybe_save(self, key, pic, save_hours):
        """Keep the last good small picture — at most every save_hours, so the SD
        card sees a few writes a day per camera, not one per refresh."""
        if len(pic.data) > SAVE_MAX:
            return
        last = self._saved_mtime(key)
        if last and time.time() - last < save_hours * 3600:
            return
        path = self._path(key)
        try:
            os.makedirs(PIC_DIR, exist_ok=True)
            with open(path + ".tmp", "wb") as f:
                f.write(pic.data)
            os.replace(path + ".tmp", path)
        except OSError as ex:
            print(f"[cameras] could not save a picture: {ex}", flush=True)
            return
        with self._lock:
            self._saved_ts[key] = time.time()

    def purge(self):
        """Pictures switched off: forget every picture and delete the saved ones."""
        with self._lock:
            self._thumbs.clear()
            self._fulls.clear()
            self._saved_ts.clear()
        n = 0
        try:
            for f in os.listdir(PIC_DIR):
                if f.endswith((".jpg", ".tmp")):
                    os.remove(os.path.join(PIC_DIR, f))
                    n += 1
        except OSError:
            pass
        if n:
            print(f"[cameras] pictures switched off: deleted {n} saved picture(s)", flush=True)

    def _tidy_saved(self, keys):
        """Saved pictures of cameras that no longer exist go after 30 days."""
        keep = {os.path.basename(self._path(k)) for k in keys}
        try:
            names = os.listdir(PIC_DIR)
        except OSError:
            return
        for f in names:
            p = os.path.join(PIC_DIR, f)
            try:
                if f not in keep and time.time() - os.path.getmtime(p) > SAVED_UNKNOWN_KEEP_S:
                    os.remove(p)
            except OSError:
                pass

    # ---- background --------------------------------------------------------------------
    def refresh_now(self):
        self._forced = True
        self._wake.set()

    def _read_nvrs(self):
        """Each recorder's channel list, every NVR_READ_S, through hikisapi. The
        scanner reads them every 6 h for names; the Cameras tab wants channel
        status fresher than that."""
        have = creds.keys_with_creds()
        for d in self.devices() or []:
            k = d.get("key")
            if not (k in have and d.get("online") and d.get("ip")
                    and _is_recorder(d, self.nvr_lists() or {})):
                continue
            if time.time() - self._nvr_read_at.get(k, 0) < NVR_READ_S:
                continue
            self._nvr_read_at[k] = time.time()
            try:
                with self._sem:
                    chans = read_nvr_channels(self._client(d))
            except (hikisapi.LoginRefused, hikisapi.Unreachable, hikisapi.TooBig):
                continue
            if chans is not None:
                with self._lock:
                    self._own_lists[k] = (time.time(), d.get("ip"), chans)

    def _round(self):
        st = self._settings()
        if not st["enabled"]:
            if not self._purged:
                self.purge()
                self._purged = True
            return
        self._purged = False
        self._read_nvrs()
        forced, self._forced = self._forced, False
        inv = self.inventory(force=True)
        if st["refresh_min"] or forced:
            due = (st["refresh_min"] * 60 * 0.8) if (st["refresh_min"] and not forced) else 60
            for e in inv["cameras"]:
                if self._stop or not self._settings()["enabled"]:
                    break
                if not (e["route"] and e["online"]):
                    continue
                p = self._mem_get(self._thumbs, e["key"])
                if p and time.time() - p.ts < due:
                    continue
                for v in range(1, max(1, len(e["views"])) + 1):     # every lens of a 2-lens camera
                    self.picture(e["key"], "thumb", fresh=True, view=v)
                    time.sleep(GAP_S)
        self._tidy_saved([self.view_key(e["key"], v) for e in inv["cameras"] for v in range(1, max(1, len(e["views"])) + 1)])

    def _loop(self):
        self._wake.wait(START_DELAY_S)
        while not self._stop:
            self._wake.clear()
            try:
                self._round()
            except Exception as ex:  # noqa: BLE001 - pictures must never take the site down
                print(f"[cameras] background round failed: {ex}", flush=True)
            mins = self._settings()["refresh_min"]
            self._wake.wait(min(mins * 60, NVR_READ_S) if mins else NVR_READ_S)


engine = Engine()
