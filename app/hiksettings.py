"""Camera settings (Phase 3): picture, text on video, time and video streams.

Read first, change only what was asked, write the WHOLE document back (Hikvision
replaces the whole document on every PUT, so a partial one resets the rest), then
read it back to prove the camera kept it. Every change is logged with its before
values, which is what Undo puts back — after checking nobody changed them since.

The camera decides the form: a setting is changeable only when the camera's own
capability document gives its allowed values (opt="…" or min/max). A setting the
camera does not describe is shown read-only, never guessed at. A few text/number
settings with no capability entry get a fixed, conservative rule (FALLBACK).

Only cameras reached with their OWN login (the "direct" route) can be changed in
this phase; a camera seen only through its recorder says so. Every request goes
through hikisapi.Client — the one-login rule cannot be bypassed from here.
"""
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import campics
import hikisapi

DATA_DIR = os.environ.get("NETWATCH_DATA", "/data")
LOG_PATH = os.path.join(DATA_DIR, "camera_changes.json")
LOG_KEEP = 1000
MAX_DOC = 512 << 10

# Hikvision writes time zones the POSIX way round: CST-2:00:00 is UTC+2.
SA_TZ = "CST-2:00:00"
TZ_CHOICES = ["CST-2:00:00", "CST+0:00:00", "CST-1:00:00", "CST-3:00:00", "CST-4:00:00", "CST-8:00:00"]
NTP_HOST = "pool.ntp.org"
_HOST_RE = re.compile(r"^(?=.{1,63}$)(?=.*[A-Za-z])[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")

ON_OFF = {"true": "On", "false": "Off"}
LABELS = {
    "img.IrcutFilter/IrcutFilterType": {"auto": "Auto", "day": "Day (colour)", "night": "Night (black and white)", "schedule": "On a schedule",
                                        "videoAuto": "Auto (from the picture)", "alarmTrigger": "On an alarm input"},
    "img.WDR/mode": {"close": "Off", "open": "On", "auto": "Auto"},
    "img.NoiseReduce/mode": {"close": "Off", "general": "Normal", "advanced": "Expert"},
    "img.Dehaze/DehazeMode": {"close": "Off", "open": "On", "auto": "Auto"},
    "img.SupplementLight/supplementLightMode": {"irLight": "Infrared", "close": "Off", "colorVuWhiteLight": "White light",
                                                "eventIntelligence": "Smart (white light on an event)", "mixed": "Smart hybrid",
                                                "smart": "Smart", "eventIntelligence/whiteLight": "White light on an event"},
    "img.SupplementLight/mixedLightBrightnessRegulatMode": {"auto": "Auto", "manual": "Manual"},
    "img.powerLineFrequency/powerLineFrequencyMode": {"50hz": "50 Hz (South Africa)", "60hz": "60 Hz"},
    "img.WhiteBalance/WhiteBalanceStyle": {"auto1": "Auto 1", "auto2": "Auto 2", "manual": "Manual", "locked": "Locked",
                                           "daylightLamp": "Daylight lamp", "incandescentlight": "Incandescent",
                                           "warmlight": "Warm light", "naturallight": "Natural light"},
    "time.timeMode": {"NTP": "From a time server", "manual": "Set by hand"},
    "s.Video/videoQualityControlType": {"CBR": "Constant", "VBR": "Variable"},
    "s.Video/fixedQuality": {"1": "Lowest", "20": "Lower", "40": "Low", "60": "Medium", "80": "Higher", "100": "Highest"},
}

# (field id, label, help, fallback when the camera's capabilities don't describe it)
# Field ids are "<doc>.<path>"; the path walks child tags from the document's root.
PICTURE = [
    ("img.Color/brightnessLevel", "Brightness", "", None),
    ("img.Color/contrastLevel", "Contrast", "", None),
    ("img.Color/saturationLevel", "Saturation", "", None),
    ("img.Sharpness/SharpnessLevel", "Sharpness", "", None),
    ("img.IrcutFilter/IrcutFilterType", "Day / night", "Auto switches to black and white in the dark.", None),
    ("img.IrcutFilter/nightToDayFilterLevel", "Day/night sensitivity", "Higher switches to night sooner.", None),
    ("img.IrcutFilter/nightToDayFilterTime", "Day/night delay", "Seconds of light change before it switches.", None),
    ("img.SupplementLight/supplementLightMode", "Light", "The camera's own infrared or white light.", None),
    ("img.SupplementLight/mixedLightBrightnessRegulatMode", "Light brightness", "", None),
    ("img.SupplementLight/irLightBrightness", "Infrared brightness", "", None),
    ("img.SupplementLight/whiteLightBrightness", "White light brightness", "", None),
    ("img.WDR/mode", "WDR (bright and dark at once)", "", None),
    ("img.WDR/WDRLevel", "WDR level", "", None),
    ("img.BLC/enabled", "Backlight compensation", "", None),
    ("img.HLC/enabled", "Headlight compensation", "Dims bright lights such as car headlights.", None),
    ("img.HLC/HLCLevel", "Headlight compensation level", "", None),
    ("img.WhiteBalance/WhiteBalanceStyle", "White balance", "", None),
    ("img.NoiseReduce/mode", "Noise reduction", "", None),
    ("img.NoiseReduce/GeneralMode/generalLevel", "Noise reduction level", "", None),
    ("img.Dehaze/DehazeMode", "Defog", "", None),
    ("img.Shutter/ShutterLevel", "Shutter", "", None),
    ("img.ImageFlip/enabled", "Mirror / flip", "", None),
    ("img.powerLineFrequency/powerLineFrequencyMode", "Anti-flicker", "", None),
]
OSD = [
    ("name.name", "Camera name", "Shown on the picture and in the recorder.", {"kind": "text", "maxlen": 32}),
    ("ovl.channelNameOverlay/enabled", "Show the name", "", {"kind": "bool"}),
    ("ovl.DateTimeOverlay/enabled", "Show date and time", "", {"kind": "bool"}),
    ("ovl.DateTimeOverlay/dateStyle", "Date style", "", None),
    ("ovl.DateTimeOverlay/timeStyle", "Time style", "", None),
    ("ovl.DateTimeOverlay/displayWeek", "Show the weekday", "", None),
    ("ovl.fontSize", "Font size", "", None),
]
TIME = [
    ("time.timeMode", "Clock set", "", None),
    ("time.timeZone", "Time zone", "South Africa is UTC+2 (no summer time).", {"kind": "tz"}),
    ("ntp.NTPServer/hostName", "Time server", "A name such as pool.ntp.org.", {"kind": "host"}),
    ("ntp.NTPServer/synchronizeInterval", "Check the time every", "Minutes.", {"kind": "int", "min": 1, "max": 10080}),
]
STREAM = [
    ("s.Video/videoCodecType", "Codec", "H.265 needs about half the bandwidth of H.264.", None),
    ("s.resolution", "Resolution", "", None),
    ("s.Video/maxFrameRate", "Frames per second", "", None),
    ("s.Video/videoQualityControlType", "Bitrate", "", None),
    ("s.Video/constantBitRate", "Bitrate (kbps)", "", None),
    ("s.Video/vbrUpperCap", "Highest bitrate (kbps)", "", None),
    ("s.Video/fixedQuality", "Quality", "", None),
    ("s.Video/GovLength", "I-frame interval", "Frames between full pictures.", None),
    ("s.Video/SmartCodec/enabled", "H.265+ / H.264+", "Saves bandwidth on a quiet scene.", None),
]
SHOW_IF = {
    "s.Video/constantBitRate": ("s.Video/videoQualityControlType", ["CBR"]),
    "s.Video/vbrUpperCap": ("s.Video/videoQualityControlType", ["VBR"]),
    "s.Video/fixedQuality": ("s.Video/videoQualityControlType", ["VBR"]),
    "ntp.NTPServer/hostName": ("time.timeMode", ["NTP"]),
    "ntp.NTPServer/synchronizeInterval": ("time.timeMode", ["NTP"]),
    "img.WDR/WDRLevel": ("img.WDR/mode", ["open"]),
    "img.HLC/HLCLevel": ("img.HLC/enabled", ["true"]),
    "img.NoiseReduce/GeneralMode/generalLevel": ("img.NoiseReduce/mode", ["general"]),
    "img.IrcutFilter/nightToDayFilterLevel": ("img.IrcutFilter/IrcutFilterType", ["auto"]),
    "img.IrcutFilter/nightToDayFilterTime": ("img.IrcutFilter/IrcutFilterType", ["auto"]),
    "img.SupplementLight/irLightBrightness": ("img.SupplementLight/mixedLightBrightnessRegulatMode", ["manual"]),
    "img.SupplementLight/whiteLightBrightness": ("img.SupplementLight/mixedLightBrightnessRegulatMode", ["manual"]),
}
SCALE = {"s.Video/maxFrameRate": 100}      # frames per second are sent in hundredths


def sections(lens):
    n = int(lens)
    return [
        {"id": "picture", "label": "Picture", "risk": "safe", "fields": PICTURE,
         "docs": {"img": f"/ISAPI/Image/channels/{n}"}},
        {"id": "osd", "label": "Text on video", "risk": "safe", "fields": OSD,
         "docs": {"name": f"/ISAPI/System/Video/inputs/channels/{n}", "ovl": f"/ISAPI/System/Video/inputs/channels/{n}/overlays"}},
        {"id": "time", "label": "Time", "risk": "careful", "fields": TIME, "device": True,
         "docs": {"time": "/ISAPI/System/time", "ntp": "/ISAPI/System/time/ntpServers"},
         # ntp first: switching to time-server mode should find the right server already set
         "order": ["ntp", "time"],
         "presets": [{"id": "sa_internet", "label": "South Africa time from the internet",
                      "changes": {"time.timeMode": "NTP", "time.timeZone": SA_TZ,
                                  "ntp.NTPServer/hostName": NTP_HOST, "ntp.NTPServer/synchronizeInterval": "60"}}]},
        {"id": "main", "label": "Main stream", "risk": "careful", "fields": STREAM,
         "docs": {"s": f"/ISAPI/Streaming/channels/{n}01"},
         "note": "This is what the recorder records. A recorder can set it back to its own settings."},
        {"id": "sub", "label": "Sub stream", "risk": "careful", "fields": STREAM,
         "docs": {"s": f"/ISAPI/Streaming/channels/{n}02"},
         "note": "The small stream used for phone viewing and multi-camera screens."},
    ]


class SettingsError(Exception):
    def __init__(self, msg, status=400, extra=None):
        super().__init__(msg)
        self.status, self.extra = status, extra or {}


# ---- XML helpers ---------------------------------------------------------------------
def _walk(root, path):
    el = root
    for part in path.split("/"):
        if el is None:
            return None
        el = next((c for c in el if hikisapi.tag(c) == part), None)
    return el


def _val(el):
    return (el.text or "").strip() if el is not None else None


def _ns(root):
    return root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else ""


def _esc(t):
    return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def serialize(root):
    """The document as Hikvision expects it: its one namespace declared on the root,
    plain tag names, every attribute and value kept. A document that mixes namespaces
    (or has namespaced attributes) is refused rather than written in a form the camera
    might misread."""
    ns = _ns(root)
    out = []

    def emit(el, top):
        t = el.tag
        if t.startswith("{"):
            uri, name = t[1:].split("}", 1)
            if uri != ns:
                raise SettingsError("this document mixes namespaces and can't be written back safely", 409)
        elif ns:
            raise SettingsError("this document mixes namespaces and can't be written back safely", 409)
        else:
            name = t
        attrs = []
        if top and ns:
            attrs.append(f' xmlns="{_esc(ns)}"')
        for k, v in el.attrib.items():
            if k.startswith("{"):
                raise SettingsError("this document has attributes this writer won't touch", 409)
            attrs.append(f' {k}="{_esc(v).replace(chr(34), "&quot;")}"')
        out.append(f"<{name}{''.join(attrs)}>")
        if el.text:
            out.append(_esc(el.text))
        for c in el:
            emit(c, False)
            if c.tail:
                out.append(_esc(c.tail))
        out.append(f"</{name}>")

    emit(root, True)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n' + "".join(out)).encode()


def _status(r):
    """(ok, reboot, message) from a PUT's ResponseStatus."""
    root = hikisapi.parse(r.content) if r.content else None
    code = hikisapi.text(root, "statusCode") if root is not None else ""
    sub = hikisapi.text(root, "subStatusCode") if root is not None else ""
    msg = hikisapi.text(root, "statusString") if root is not None else ""
    if r.status == 200 and code in ("", "1"):
        return True, False, ""
    if r.status == 200 and code == "7":
        return True, True, "needs a restart"
    return False, False, f"HTTP {r.status}" + (f" {sub or msg}" if (sub or msg) else "")


def tz_offset(tz):
    """'CST-2:00:00' -> timedelta(hours=2). Anything after DST is ignored."""
    m = re.match(r"^[A-Za-z]{3,}([+-])(\d{1,2})(?::(\d{2}))?", tz or "")
    if not m:
        return None
    sign = -1 if m.group(1) == "+" else 1
    return timedelta(hours=sign * int(m.group(2)), minutes=sign * int(m.group(3) or 0))


def tz_label(tz):
    off = tz_offset(tz)
    if off is None:
        return tz
    mins = int(off.total_seconds() // 60)
    s = f"UTC{'+' if mins >= 0 else '-'}{abs(mins) // 60}" + (f":{abs(mins) % 60:02d}" if mins % 60 else "")
    if "DST" in (tz or ""):
        s += " with summer time"
    return s + (" — South Africa" if tz == SA_TZ else " — China" if tz == "CST-8:00:00" else "")


def local_time_now(tz):
    off = tz_offset(tz) or timedelta(0)
    t = datetime.now(timezone.utc).astimezone(timezone(off))
    return t.strftime("%Y-%m-%dT%H:%M:%S") + t.strftime("%z")[:3] + ":" + t.strftime("%z")[3:]


# ---- the form --------------------------------------------------------------------------
def _field(fid, label, help_, fb, docs, caps):
    dk, path = fid.split(".", 1)
    doc, cap = docs.get(dk), caps.get(dk)
    if doc is None:
        return None
    out = {"id": fid, "label": label, "help": help_, "editable": False}
    if path == "resolution":
        w, h = _walk(doc, "Video/videoResolutionWidth"), _walk(doc, "Video/videoResolutionHeight")
        if w is None or h is None:
            return None
        out.update(kind="enum", value=f"{_val(w)}x{_val(h)}")
        cw, ch = _walk(cap, "Video/videoResolutionWidth"), _walk(cap, "Video/videoResolutionHeight")
        ws = [x for x in (cw.get("opt") if cw is not None else "").split(",") if x]
        hs = [x for x in (ch.get("opt") if ch is not None else "").split(",") if x]
        if ws and len(ws) == len(hs):
            pairs = sorted({(int(a), int(b)) for a, b in zip(ws, hs) if a.isdigit() and b.isdigit()}, key=lambda p: p[0] * p[1])
            out["opts"] = [{"v": f"{a}x{b}", "label": f"{a} × {b}"} for a, b in pairs]
            out["editable"] = True
        out["shown"] = out["value"].replace("x", " × ")
        return out
    el = _walk(doc, path)
    if el is None:
        return None
    v = _val(el)
    out["value"] = v
    c = _walk(cap, path) if cap is not None else None
    scale = SCALE.get(fid)
    names = LABELS.get(fid, {})
    opt = c.get("opt") if c is not None else None
    if opt:
        vals = [x.strip() for x in opt.split(",") if x.strip()]
        vals = ["true" if x == "ture" else x for x in vals]
        if set(vals) <= {"true", "false"} and v in ("true", "false"):
            out.update(kind="bool", editable=len(set(vals)) == 2)
        else:
            out.update(kind="enum", editable=True,
                       opts=[{"v": x, "label": names.get(x) or (f"{int(x) / scale:g}" if scale and x.isdigit() else x)} for x in dict.fromkeys(vals)])
    elif c is not None and c.get("min") is not None and c.get("max") is not None:
        try:
            out.update(kind="int", min=int(c.get("min")), max=int(c.get("max")), editable=True)
        except ValueError:
            out["kind"] = "text"
    elif fb:
        out["kind"] = fb["kind"]
        if fb["kind"] == "bool":
            out["editable"] = v in ("true", "false")
        elif fb["kind"] == "text":
            out.update(maxlen=fb["maxlen"], editable=True)
        elif fb["kind"] == "int":
            out.update(min=fb["min"], max=fb["max"], editable=True)
        elif fb["kind"] == "host":
            out.update(kind="text", maxlen=63, host=True, editable=True)
        elif fb["kind"] == "tz":
            choices = list(dict.fromkeys(TZ_CHOICES + ([v] if v else [])))
            out.update(kind="enum", editable=True, tz=True, opts=[{"v": x, "label": tz_label(x)} for x in choices])
    else:
        out["kind"] = "bool" if v in ("true", "false") else "text"
    if out["kind"] == "bool":
        out["shown"] = ON_OFF.get(v, v)
    elif out.get("opts"):
        out["shown"] = next((o["label"] for o in out["opts"] if o["v"] == v), v)
    else:
        out["shown"] = (f"{int(v) / scale:g}" if scale and (v or "").isdigit() else v)
    if fid in SHOW_IF:
        out["show_if"] = {"field": SHOW_IF[fid][0], "values": SHOW_IF[fid][1]}
    return out


def _check(f, value):
    """The value as the camera wants it, or SettingsError."""
    if not f.get("editable"):
        raise SettingsError(f"{f['label']} can't be changed on this camera")
    v = "" if value is None else str(value).strip()
    k = f["kind"]
    if k == "bool":
        if v.lower() not in ("true", "false"):
            raise SettingsError(f"{f['label']}: on or off")
        return v.lower()
    if k == "enum" and f.get("tz") and v not in {o["v"] for o in f.get("opts") or []}:
        # Undo may put back a zone that is not on the short list (a DST rule, say).
        if tz_offset(v) is None or len(v) > 80 or any(ch in v for ch in '<>&"'):
            raise SettingsError(f"{f['label']}: not a time zone")
        return v
    if k == "enum":
        if v not in {o["v"] for o in f.get("opts") or []}:
            raise SettingsError(f"{f['label']}: '{v}' is not one of the camera's choices")
        return v
    if k == "int":
        if not re.fullmatch(r"-?\d+", v) or not f["min"] <= int(v) <= f["max"]:
            raise SettingsError(f"{f['label']}: a whole number from {f['min']} to {f['max']}")
        return str(int(v))
    if k == "text":
        if f.get("host"):
            if not _HOST_RE.match(v) or ".." in v:
                raise SettingsError(f"{f['label']}: a server name such as {NTP_HOST}")
            return v
        if not v or len(v) > f.get("maxlen", 32) or any(ord(ch) < 32 or ch in '<>&"' for ch in v):
            raise SettingsError(f"{f['label']}: 1–{f.get('maxlen', 32)} characters, without < > & \"")
        return v
    raise SettingsError(f"{f['label']} can't be changed")


# ---- the change log --------------------------------------------------------------------
class Log:
    def __init__(self, path=None):
        self.path = path or LOG_PATH
        self._lock = threading.Lock()

    def _load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            return d if isinstance(d, list) else []
        except (OSError, ValueError):
            return []

    def add(self, entry):
        with self._lock:
            rows = self._load()
            rows.append(entry)
            rows = rows[-LOG_KEEP:]
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(rows, f)
            os.replace(tmp, self.path)

    def mark(self, cid, **kv):
        with self._lock:
            rows = self._load()
            for r in rows:
                if r.get("id") == cid:
                    r.update(kv)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(rows, f)
            os.replace(tmp, self.path)

    def get(self, cid):
        return next((r for r in self._load() if r.get("id") == cid), None)

    def for_key(self, key, limit=50):
        return [r for r in reversed(self._load()) if r.get("key") == key][:limit]


# ---- the engine ------------------------------------------------------------------------
class Settings:
    def __init__(self):
        self.engine = None
        self.cfg = lambda: {}
        self.log = Log()
        self.on_change = None           # (camera entry, log entry) -> None: device history
        self._locks = {}
        self._lk = threading.Lock()

    def start(self, engine, cfg, on_change=None, log_path=None):
        self.engine, self.cfg, self.on_change = engine, cfg, on_change
        if log_path:
            self.log = Log(log_path)
        return self

    def enabled(self):
        return bool((self.cfg().get("features") or {}).get("camera_manage"))

    def _lock_for(self, key):
        with self._lk:
            return self._locks.setdefault(key, threading.Lock())

    def _camera(self, key):
        inv = self.engine.inventory()
        e = next((c for c in inv["cameras"] if c["key"] == key), None)
        if e is None:
            raise SettingsError("unknown camera", 404)
        if e.get("_d") and not campics._hikish(e["_d"]):
            raise SettingsError("Changing settings from Netwatch works on Hikvision cameras only; "
                                "use this camera's own web page.", 409, {"reason": "not_hikvision"})
        if not e.get("_own_ok") or not e.get("_d"):
            why = ("This camera is only reached through its recorder. Changing its settings needs the "
                   "camera's own login (saved on its device page); changing them through the recorder comes later."
                   if e.get("_via_ok") else "Changing settings needs the camera's own saved login.")
            raise SettingsError(why, 409, {"reason": "no_own_login"})
        if not e.get("online"):
            raise SettingsError("the camera is offline", 409, {"reason": "offline"})
        return e

    def _lenses(self, e):
        return list(range(1, len(e.get("views") or []) + 1)) or [1]

    def _read(self, cl, path, need=True):
        r = cl.get(path, max_bytes=MAX_DOC)
        if r.status != 200:
            if not need:
                return None
            raise SettingsError(f"the camera answered HTTP {r.status} for {path}"
                                + (" — the login may lack the rights" if r.status in (401, 403) else ""), 502)
        root = hikisapi.parse(r.content)
        if root is None:
            raise SettingsError(f"the camera's answer for {path} could not be read", 502)
        return root

    def _section(self, cl, sec, with_caps=True):
        docs, caps, errs = {}, {}, []
        for dk, path in sec["docs"].items():
            try:
                docs[dk] = self._read(cl, path)
            except SettingsError as e:
                errs.append(str(e))
                continue
            if with_caps:
                try:
                    caps[dk] = self._read(cl, path + "/capabilities", need=False)
                except SettingsError:
                    caps[dk] = None
        fields = []
        for fid, label, help_, fb in sec["fields"]:
            f = _field(fid, label, help_, fb, docs, caps)
            if f:
                fields.append(f)
        return docs, fields, errs

    def read(self, key, lens=1):
        e = self._camera(key)
        lenses = self._lenses(e)
        if int(lens) not in lenses:
            raise SettingsError("no such lens", 404)
        cl = self.engine._client(e["_d"])
        out = []
        with self.engine._sem:
            for sec in sections(lens):
                if sec.get("device") and int(lens) != 1:
                    continue
                try:
                    _docs, fields, errs = self._section(cl, sec)
                except (hikisapi.LoginRefused,) as ex:
                    raise SettingsError(str(ex) or "the camera refused the saved login", 403) from None
                except (hikisapi.Unreachable, hikisapi.TooBig) as ex:
                    errs, fields = [f"could not read: {str(ex) or ex.__class__.__name__}"], []
                out.append({"id": sec["id"], "label": sec["label"], "risk": sec["risk"], "note": sec.get("note", ""),
                            "presets": sec.get("presets", []), "fields": fields,
                            "error": "; ".join(errs) if not fields else ""})
        d = e["_d"]
        return {"ok": True, "key": key, "name": e["name"], "lens": int(lens), "lenses": lenses,
                "netwatch_name": (d.get("name") or "")[:32], "can_change": self.enabled(),
                "why_not": "" if self.enabled() else "Camera settings are turned off for this site (Settings → Cameras).",
                "sections": out, "ts": int(time.time())}

    def apply(self, key, lens, section, changes, who="", expect=None, undo_of=None, confirm=False):
        """Change some settings of one section. `expect` = the values the person saw
        ({field: value}); a camera that shows something else now is a conflict (409)."""
        if not self.enabled():
            raise SettingsError("Camera settings are turned off for this site (Settings → Cameras).", 403)
        sec = next((s for s in sections(lens) if s["id"] == section), None)
        if sec is None:
            raise SettingsError("unknown settings section")
        if not isinstance(changes, dict) or not changes:
            raise SettingsError("nothing to change")
        if sec["risk"] == "careful" and not confirm:
            raise SettingsError("confirm this change first", 400, {"needs_confirm": True})
        e = self._camera(key)
        if int(lens) not in self._lenses(e):
            raise SettingsError("no such lens", 404)
        lock = self._lock_for(key)
        if not lock.acquire(blocking=False):
            raise SettingsError("another change on this camera is still running", 409)
        try:
            cl = self.engine._client(e["_d"])
            with self.engine._sem:
                return self._apply(e, cl, sec, int(lens), changes, who, expect or {}, undo_of)
        except hikisapi.LoginRefused as ex:
            raise SettingsError(str(ex) or "the camera refused the saved login", 403) from None
        except (hikisapi.Unreachable, hikisapi.TooBig) as ex:
            raise SettingsError(f"the camera did not answer ({str(ex) or ex.__class__.__name__})", 502) from None
        finally:
            lock.release()

    def _apply(self, e, cl, sec, lens, changes, who, expect, undo_of):
        docs, fields, errs = self._section(cl, sec)
        by = {f["id"]: f for f in fields}
        want = {}
        for fid, v in changes.items():
            f = by.get(fid)
            if f is None:
                raise SettingsError(f"'{fid}' is not a setting this camera has")
            nv = _check(f, v)
            if nv != f["value"]:
                want[fid] = nv
        moved = [by[fid]["label"] for fid, v in expect.items() if fid in by and str(v) != str(by[fid]["value"])]
        if moved:
            raise SettingsError("changed on the camera since you looked: " + ", ".join(moved), 409,
                                {"conflict": moved})
        if not want:
            return {"ok": True, "applied": [], "unchanged": True, "message": "Nothing to change — the camera already has these values."}
        before = {fid: by[fid]["value"] for fid in want}
        shown_before = {fid: by[fid]["shown"] for fid in want}
        touched = []
        for fid, nv in want.items():
            dk, path = fid.split(".", 1)
            if path == "resolution":
                w, h = nv.split("x")
                _walk(docs[dk], "Video/videoResolutionWidth").text = w
                _walk(docs[dk], "Video/videoResolutionHeight").text = h
            else:
                _walk(docs[dk], path).text = nv
            if dk == "ntp" and path == "NTPServer/hostName":
                fmt = _walk(docs[dk], "NTPServer/addressingFormatType")
                if fmt is not None:
                    fmt.text = "hostname"
            touched.append(dk)
        if "time" in touched:
            # Every time write also sets the clock from the Pi (kept right by the internet):
            # a camera that showed the right local time under the wrong zone would otherwise
            # jump by the zone difference.
            lt = _walk(docs["time"], "localTime")
            if lt is not None:
                lt.text = local_time_now(_val(_walk(docs["time"], "timeZone")))
        reboot, written, failed = False, [], ""
        for dk in [d for d in sec.get("order", list(sec["docs"])) if d in touched]:
            r = cl.request("PUT", sec["docs"][dk], data=serialize(docs[dk]),
                           headers={"Content-Type": "application/xml"}, max_bytes=64 << 10)
            ok, rb, msg = _status(r)
            if not ok:
                failed = f"the camera refused the change: {msg}"
                break
            reboot = reboot or rb
            written.append(dk)
        # Read back what was written: the proof that it took.
        _d2, fields2, _e2 = self._section(cl, {**sec, "docs": {k: v for k, v in sec["docs"].items() if k in written}})
        now = {f["id"]: f for f in fields2}
        rows, kept_all = [], True
        for fid in want:
            dk = fid.split(".", 1)[0]
            got = now.get(fid)
            kept = bool(got and got["value"] == want[fid]) if dk in written else False
            kept_all = kept_all and kept
            rows.append({"id": fid, "label": by[fid]["label"], "before": before[fid], "after": want[fid],
                         "shown_before": shown_before[fid],
                         "shown_after": next((o["label"] for o in by[fid].get("opts") or [] if o["v"] == want[fid]),
                                             ON_OFF.get(want[fid], want[fid]) if by[fid]["kind"] == "bool" else want[fid]),
                         "now": got["value"] if got else None, "written": dk in written, "kept": kept})
        d = e["_d"]
        entry = {"id": uuid.uuid4().hex[:12], "ts": int(time.time()), "who": who or "", "key": e["key"],
                 "ip": e.get("ip"), "mac": e.get("mac"), "name": e.get("name"), "lens": lens,
                 "section": sec["id"], "section_label": sec["label"], "risk": sec["risk"], "fields": rows,
                 "result": "ok" if kept_all and not failed else ("partial" if written else "failed"),
                 "error": failed, "reboot": reboot, "undo_of": undo_of, "undone_by": None}
        if written:
            self.log.add(entry)
            if undo_of:
                self.log.mark(undo_of, undone_by=entry["id"])
            if self.on_change:
                try:
                    self.on_change(d, entry)
                except Exception:  # noqa: BLE001 - history must never fail a change
                    pass
        if failed and not written:
            raise SettingsError(failed, 502)
        msg = ("Done — the camera kept every change." if kept_all and not failed
               else failed or "The camera did not keep every change — see the list.")
        if reboot:
            msg += " The camera says it needs a restart for this to take effect."
        return {"ok": not failed and kept_all, "change": entry, "message": msg, "reboot": reboot}

    def changes(self, key, limit=50):
        return self.log.for_key(key, limit)

    def undo(self, key, cid, who=""):
        c = self.log.get(cid)
        if not c or c.get("key") != key:
            raise SettingsError("unknown change", 404)
        if c.get("undone_by"):
            raise SettingsError("this change was already undone", 409)
        rows = [r for r in c.get("fields") or [] if r.get("written")]
        if not rows:
            raise SettingsError("nothing of this change reached the camera", 409)
        return self.apply(key, c["lens"], c["section"], {r["id"]: r["before"] for r in rows}, who=who,
                          expect={r["id"]: r["after"] for r in rows}, undo_of=cid, confirm=True)


settings = Settings()
