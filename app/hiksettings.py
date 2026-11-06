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
import copy
import json
import os
import re
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import campics
import creds
import hikchannels
import hikdetect
import hikexplorer
import hikrecord
import hikisapi
import hiksecurity

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


GROUPS = ("basic", "detect", "record", "recorder", "security")
CAMERA_GROUPS = ("basic", "detect", "record", "security")
RECORDER_GROUPS = ("recorder", "security")
RECORDER_SECTIONS = ("record", "storage")


def sections(lens, group="basic", via=None):
    """The sections of a group. `via` = the recorder channel when the camera is changed
    through its recorder: the recorder's proxy documents, which carry the camera's own
    settings (same shape), and no clock (a recorder sets its cameras' clocks itself).
    "record" = the recorder's recording of the camera (only through a recorder);
    "recorder" = the recorder's own settings (its key, not a camera's)."""
    if group == "record":
        return hikrecord.camera_sections(via) if via is not None else []
    if group == "recorder":
        return hikrecord.recorder_sections()
    if group == "security":
        return hiksecurity.sections() if via is None else []     # the device's own login only
    if via is not None:
        return nvr_sections(int(via), group)
    if group == "detect":
        return hikdetect.sections(lens)
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


VIA_NOTE = "Changed through the recorder, which passes it on to the camera."


def nvr_sections(ch, group="basic"):
    if group == "detect":
        return hikdetect.nvr_sections(ch)
    return [
        {"id": "picture", "label": "Picture", "risk": "safe", "fields": PICTURE, "optional": "img", "note": VIA_NOTE,
         "docs": {"img": f"/ISAPI/ContentMgmt/ImageProxy/channels/{ch}"}},
        # the channel NAME lives in the recorder's channel document next to the camera's
        # address and login, so it is not written from here (that is channel editing)
        {"id": "osd", "label": "Text on video", "risk": "safe", "fields": OSD, "optional": "ovl", "note": VIA_NOTE,
         "docs": {"ovl": f"/ISAPI/ContentMgmt/InputProxy/channels/{ch}/overlays"}},
        {"id": "main", "label": "Main stream", "risk": "careful", "fields": STREAM, "optional": "s",
         "docs": {"s": f"/ISAPI/ContentMgmt/StreamingProxy/channels/{ch}01"},
         "note": "This is what the recorder records. " + VIA_NOTE},
        {"id": "sub", "label": "Sub stream", "risk": "careful", "fields": STREAM, "optional": "s",
         "docs": {"s": f"/ISAPI/ContentMgmt/StreamingProxy/channels/{ch}02"},
         "note": "The small stream used for phone viewing and multi-camera screens. " + VIA_NOTE},
    ]


class SettingsError(Exception):
    def __init__(self, msg, status=400, extra=None):
        super().__init__(msg)
        self.status, self.extra = status, extra or {}


# ---- XML helpers ---------------------------------------------------------------------
_PART = re.compile(r"^([A-Za-z0-9_.-]+)\[id=(\d+)\]$")


def _child(el, name):
    return next((c for c in el if hikisapi.tag(c) == name), None) if el is not None else None


def _walk(root, path, loose=False):
    """Walk child tags from root. "Tag[id=N]" picks the Tag whose <id> is N; with
    loose=True (capability documents, which may describe one item for all) it falls
    back to the first Tag."""
    el = root
    for part in path.split("/"):
        if el is None:
            return None
        m = _PART.match(part)
        if m:
            name, want = m.group(1), m.group(2)
            hits = [c for c in el if hikisapi.tag(c) == name]
            el = next((c for c in hits if _val(_child(c, "id")) == want), None)
            if el is None and loose and hits:
                el = hits[0]
        else:
            el = _child(el, part)
    return el


def _qual(parent, name):
    """A tag name in the parent's namespace."""
    return parent.tag[:parent.tag.index("}") + 1] + name if parent.tag.startswith("{") else name


def _cap_path(path):
    if "?" in path:
        base, q = path.split("?", 1)
        return f"{base}/capabilities?{q}"
    return path + "/capabilities"


def _is_json(path):
    return "format=json" in path


# JSON documents (the strobe and siren) are turned into the same element tree, so the
# form code is shared: "@min"/"@opt" become attributes, and "_t" remembers the JSON type
# so the document goes back exactly as it came (numbers stay numbers).
def _j2e(name, v, depth=0):
    if depth > 24:
        raise SettingsError("the camera's answer is nested too deep", 502)
    el = ET.Element(name)
    if isinstance(v, dict):
        el.set("_t", "o")
        for k, x in v.items():
            k = str(k)[:64]
            if k.startswith("@"):
                el.set(k[1:], str(x))
            else:
                el.append(_j2e(k, x, depth + 1))
    elif isinstance(v, list):
        el.set("_t", "l")
        for x in v[:500]:
            el.append(_j2e("_i", x, depth + 1))
    elif isinstance(v, bool):
        el.set("_t", "b")
        el.text = "true" if v else "false"
    elif isinstance(v, (int, float)):
        el.set("_t", "n")
        el.text = str(v)
    elif v is None:
        el.set("_t", "z")
    else:
        el.set("_t", "s")
        el.text = str(v)
    return el


def _e2j(el):
    t = el.get("_t")
    if t == "o":
        return {c.tag: _e2j(c) for c in el}
    if t == "l":
        return [_e2j(c) for c in el]
    if t == "b":
        return (el.text or "") == "true"
    if t == "n":
        x = (el.text or "0").strip()
        return int(x) if re.fullmatch(r"-?\d+", x) else float(x)
    if t == "z":
        return None
    return el.text or ""


def _parse_json(content):
    try:
        d = json.loads(content.decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(d, dict) or len(d) != 1:
        return None
    k, v = next(iter(d.items()))
    return _j2e(str(k)[:64], v)


def serialize_json(root):
    return json.dumps({root.tag: _e2j(root)}).encode()


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
    """(ok, reboot, message) from a PUT's ResponseStatus (XML, or JSON for JSON documents)."""
    code = sub = msg = ""
    body = (r.content or b"").lstrip()
    if body[:1] == b"{":
        try:
            j = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            j = {}
        if isinstance(j, dict):
            code, sub, msg = (str(j.get(k) if j.get(k) is not None else "")[:120]
                              for k in ("statusCode", "subStatusCode", "statusString"))
    else:
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
    if fb and fb.get("kind") in SPECIAL:
        out = _special(fid, label, help_, fb, docs, caps)
        if out is not None and fb.get("show_if"):
            out["show_if"] = {"field": fb["show_if"][0], "values": list(fb["show_if"][1])}
        return out
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
    c = _walk(cap, path, loose=True) if cap is not None else None
    scale = SCALE.get(fid)
    names = LABELS.get(fid) or (fb or {}).get("labels") or {}
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
            st = c.get("step") or ""
            if st.isdigit() and 1 < int(st) <= out["max"] - out["min"]:
                out["step"] = int(st)
        except ValueError:
            out["kind"] = "text"
    elif fb and fb.get("kind"):
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
    elif fb and fb.get("show_if"):
        out["show_if"] = {"field": fb["show_if"][0], "values": list(fb["show_if"][1])}
    return out


# ---- drawn areas, lines, masks, the motion grid, multi-choice, alarm actions ------------
# Hikvision gives region coordinates in its own "normalized screen" (often 1000 x 1000,
# privacy masks 704 x 576) with the origin at the BOTTOM-left. The page always works in
# 0..1000 with the origin top-left; these convert both ways.
SPECIAL = {"area", "line", "rects", "grid", "set", "links", "jopts", "week", "isodays", "choices", "srcurl"}
Y_UP = True
LINK_ORDER = ["center", "record", "beep", "whiteLight", "FTP"]
LINK_LABEL = {"center": "Tell the recorder / Netwatch", "record": "Record", "beep": "Siren / voice warning",
              "whiteLight": "Strobe light", "FTP": "Send a picture to FTP", "email": "E-mail", "IO": "Switch an alarm output"}
LINK_CAP = {"center": "Center", "record": "Record", "beep": "Beep", "whiteLight": "WhiteLight", "FTP": "FTP"}


def _screen(doc):
    w = _val(_walk(doc, "normalizedScreenSize/normalizedScreenWidth")) or ""
    h = _val(_walk(doc, "normalizedScreenSize/normalizedScreenHeight")) or ""
    w = int(w) if w.isdigit() and 0 < int(w) <= 100000 else 1000
    h = int(h) if h.isdigit() and 0 < int(h) <= 100000 else 1000
    return w, h


def _to_ui(x, y, w, h):
    u = round(min(max(x, 0), w) * 1000 / w)
    v = round((h - min(max(y, 0), h)) * 1000 / h) if Y_UP else round(min(max(y, 0), h) * 1000 / h)
    return u, v


def _to_dev(u, v, w, h):
    x = round(u * w / 1000)
    y = round(h - v * h / 1000) if Y_UP else round(v * h / 1000)
    return min(max(x, 0), w), min(max(y, 0), h)


def _points(lst, pt_tag):
    out = []
    for c in (lst if lst is not None else []):
        if hikisapi.tag(c) != pt_tag:
            continue
        x, y = _val(_child(c, "positionX")) or "", _val(_child(c, "positionY")) or ""
        if not (re.fullmatch(r"-?\d+", x) and re.fullmatch(r"-?\d+", y)):
            return None
        out.append((int(x), int(y)))
    return out


def _pts_str(pts):
    return " ".join(f"{u},{v}" for u, v in pts)


def _parse_pts(v):
    out = []
    for part in (v or "").split():
        m = re.fullmatch(r"(\d{1,4}),(\d{1,4})", part)
        if not m:
            return None
        u, w = int(m.group(1)), int(m.group(2))
        if u > 1000 or w > 1000:
            return None
        out.append((u, w))
    return out


def _mask_rects(lst):
    out = []
    for r in (lst if lst is not None else []):
        if hikisapi.tag(r) != "PrivacyMaskRegion":
            continue
        if _val(_child(r, "enabled")) == "false":
            continue
        pts = _points(_child(r, "RegionCoordinatesList"), "RegionCoordinates")
        if pts:
            out.append(pts)
    return out


def _rects_str(rects_dev, w, h):
    out = []
    for pts in rects_dev:
        ui = [_to_ui(x, y, w, h) for x, y in pts]
        us, vs = [p[0] for p in ui], [p[1] for p in ui]
        out.append(f"{min(us)},{min(vs)},{max(us)},{max(vs)}")
    return ";".join(out)


def _parse_rects(v):
    out = []
    for part in [x for x in (v or "").split(";") if x.strip()]:
        m = re.fullmatch(r"\s*(\d{1,4}),(\d{1,4}),(\d{1,4}),(\d{1,4})\s*", part)
        if not m:
            return None
        l, t, r, b = (int(g) for g in m.groups())
        if max(l, t, r, b) > 1000 or r - l < 10 or b - t < 10:
            return None
        out.append((l, t, r, b))
    return out


def _grid_shape(doc):
    rows = _val(_walk(doc, "Grid/rowGranularity")) or ""
    cols = _val(_walk(doc, "Grid/columnGranularity")) or ""
    if not (rows.isdigit() and cols.isdigit()) or not (0 < int(rows) <= 64 and 0 < int(cols) <= 64):
        return None
    return int(rows), int(cols)


def _grid_count(hexmap, rows, cols):
    per = (cols + 7) // 8 * 2
    n = 0
    for r in range(rows):
        bits = int(hexmap[r * per:(r + 1) * per] or "0", 16)
        n += bin(bits >> (per * 4 - cols)).count("1")
    return n


def _links_of(doc):
    lst = _child(doc, "EventTriggerNotificationList")
    return [(_val(_child(n, "notificationMethod")) or "") for n in (lst if lst is not None else [])]


def _link_order(ms):
    return sorted(dict.fromkeys(m for m in ms if m), key=lambda m: (LINK_ORDER.index(m) if m in LINK_ORDER else 99, m))


def _special(fid, label, help_, fb, docs, caps):
    dk, path = fid.split(".", 1)
    doc, cap = docs.get(dk), caps.get(dk)
    kind = fb["kind"]
    out = {"id": fid, "label": label, "help": help_, "editable": False, "kind": kind}
    if kind in ("area", "line"):
        rpath = path.rsplit("/", 1)[0]
        reg = _walk(doc, rpath)
        if reg is None:
            return None
        lt, pt = fb["coords"]
        pts = _points(_child(reg, lt), pt)
        if pts is None:
            return None
        w, h = _screen(doc)
        ui = [_to_ui(x, y, w, h) for x, y in pts]
        drawn = bool(pts) and any(p != (0, 0) for p in pts)
        if kind == "line":
            lo = hi = 2
            creg = _walk(cap, rpath, loose=True) if cap is not None else None
        else:
            creg = _walk(cap, rpath, loose=True) if cap is not None else None
            lo = _val(_child(creg, "minRegionCoordinatesNum")) or ""
            hi = _val(_child(creg, "maxRegionCoordinatesNum")) or ""
            lo = int(lo) if lo.isdigit() else 0
            hi = int(hi) if hi.isdigit() else 0
            if not (3 <= lo <= hi <= 20):
                creg = None
        out.update(value=_pts_str(ui) if drawn else "", nw=w, nh=h, min=lo, max=hi,
                   shown=(f"{len(ui)} points drawn" if kind == "area" else "line drawn") if drawn else "not drawn",
                   editable=creg is not None, _region=rpath, _coords=list(fb["coords"]))
        return out
    if kind == "rects":
        lst = _walk(doc, path)
        if lst is None:
            return None
        w, h = _screen(doc)
        size = lst.get("size") or ""
        size = int(size) if size.isdigit() and 0 < int(size) <= 8 else 4
        rects = _mask_rects(lst)
        v = _rects_str(rects, w, h)
        out.update(value=v, nw=w, nh=h, max=size, editable=True,
                   shown=f"{len(rects)} box{'es' if len(rects) != 1 else ''}" if rects else "none")
        return out
    if kind == "grid":
        el = _walk(doc, path)
        shape = _grid_shape(doc)
        if el is None or shape is None:
            return None
        rows, cols = shape
        v = (_val(el) or "").lower()
        per = (cols + 7) // 8 * 2
        ok = len(v) == rows * per and re.fullmatch(r"[0-9a-f]*", v) is not None
        out.update(value=v, rows=rows, cols=cols,
                   editable=ok and (_val(_child(doc, "regionType")) or "grid") == "grid",
                   shown=f"{_grid_count(v, rows, cols)} of {rows * cols} squares" if ok else "—")
        return out
    if kind == "set":
        el = _walk(doc, path)
        if el is None:
            return None
        c = _walk(cap, path, loose=True) if cap is not None else None
        opts = [x.strip() for x in ((c.get("opt") if c is not None else "") or "").split(",") if x.strip()]
        v = _val(el) or ""
        names = fb.get("labels") or {}
        cur = [x for x in v.split(",") if x]
        out.update(value=",".join(cur), opts=[{"v": x, "label": names.get(x, x)} for x in opts], editable=bool(opts),
                   shown=", ".join(names.get(x, x) for x in cur) or "Anything that moves")
        return out
    if kind == "links":
        if hikisapi.tag(doc) != "EventTrigger":
            return None
        have = _link_order(_links_of(doc))
        tc = docs.get("tc")
        block = _child(tc, fb["cap"]) if tc is not None else None
        offer = []
        if block is not None:
            for m in LINK_ORDER:
                if _val(_child(block, "isSupport" + LINK_CAP[m])) == "true":
                    offer.append(m)
        out.update(value=",".join(have), opts=[{"v": m, "label": LINK_LABEL[m]} for m in offer],
                   fixed=[m for m in have if m not in offer], editable=bool(offer),
                   shown=", ".join(LINK_LABEL.get(m, m) for m in have) or "Nothing")
        return out
    if kind == "jopts":
        el = _walk(doc, path)
        if el is None:
            return None
        v = _val(el) or ""
        opts = []
        lst = _child(cap, fb["list"]) if cap is not None else None
        for it in (lst if lst is not None else []):
            ov, ol = _val(_child(it, fb["v"])), _val(_child(it, fb["label"]))
            if ov and re.fullmatch(r"\d{1,4}", ov):
                opts.append({"v": ov, "label": (ol or ov)[:80]})
        out.update(kind="enum", value=v, opts=opts, editable=bool(opts),
                   shown=next((o["label"] for o in opts if o["v"] == v), v))
        return out
    if kind == "choices":
        el = _walk(doc, path)
        if el is None:
            return None
        v = _val(el) or ""
        unit = fb.get("unit", "")
        vals = list(fb["opts"]) + ([v] if v and v not in fb["opts"] else [])
        vals.sort(key=lambda x: int(x) if x.isdigit() else 1 << 30)
        out.update(kind="enum", value=v, editable=v.isdigit(),
                   opts=[{"v": x, "label": f"{x} {unit}".strip()} for x in vals])
        out["shown"] = f"{v} {unit}".strip()
        return out
    if kind == "srcurl":
        el = _walk(doc, path)
        if el is None:
            return None
        v = _val(el) or ""
        c = _walk(cap, path, loose=True) if cap is not None else None
        vals = [x.strip() for x in ((c.get("opt") if c is not None else "") or "").split(",") if x.strip()]
        ok = v in vals and all(_STREAM_URL.match(x) for x in vals)
        out.update(kind="enum", value=v, editable=ok,
                   opts=[{"v": x, "label": _stream_label(x)} for x in (vals if ok else [v])], shown=_stream_label(v))
        return out
    if kind == "isodays":
        el = _walk(doc, path)
        if el is None:
            return None
        m = _ISO_DAYS.match(_val(el) or "")
        if not m:
            return None
        days, hours = int(m.group(1)), int(m.group(2) or 0)
        lim = el if el.get("max") else _walk(cap, path, loose=True) if cap is not None else None
        lo, hi = (lim.get("min"), lim.get("max")) if lim is not None else ("0", "")
        hi = int(hi) if (hi or "").isdigit() else 0
        out.update(kind="int", value=str(days), min=int(lo) if (lo or "").isdigit() else 0, max=hi,
                   editable=hours == 0 and hi > 0, _iso=True, shown=_days_shown(str(days)))
        return out
    if kind == "week":
        blk = _walk(doc, path)
        if blk is None:
            return None
        v = _week_of(blk)
        c = _walk(cap, "DefaultRecordingMode", loose=True) if cap is not None else None
        offer = [x.strip() for x in ((c.get("opt") if c is not None else "") or "").split(",") if x.strip()]
        code = {m: k for m, k, _l in hikrecord.MODES}
        used = set((v or "").replace("|", "")) - {"-"}
        modes = [(m, k, lab) for m, k, lab in hikrecord.MODES if m in offer or k in used]
        if not offer:
            modes = [(m, k, lab) for m, k, lab in hikrecord.MODES if m in ("CMR", "MOTION") or k in used]
        out.update(value=v or "", modes=[{"v": k, "mode": m, "label": lab} for m, k, lab in modes],
                   editable=v is not None and all(k in code.values() for k in used),
                   shown=_week_shown(v, {k: lab for _m, k, lab in hikrecord.MODES}) if v is not None
                   else "set in minutes on the recorder (shown there)")
        return out
    return None


# ---- the recording schedule and other recorder values ---------------------------------
_STREAM_URL = re.compile(r"^rtsp://localhost/(?:PSIA|ISAPI)/Streaming/channels/\d+$")
_ISO_DAYS = re.compile(r"^P(\d{1,4})D(?:T(\d{1,2})H)?$")
_HOUR = re.compile(r"^(\d{2}):00:00$")


def _stream_label(url):
    tail = re.search(r"(\d{2})$", url or "")
    return hikrecord.STREAM.get(tail.group(1), "Stream " + tail.group(1)) if tail else (url or "—")


def _days_shown(v):
    return "No limit (until the disks are full)" if v == "0" else f"{v} day{'s' if v != '1' else ''}"


def _hour_of(t):
    m = _HOUR.match(t or "")
    return int(m.group(1)) if m and int(m.group(1)) <= 24 else None


def _week_of(blk):
    """The schedule as 7 days x 24 hourly codes ("-" = not recorded), Monday first, or
    None when it uses times an hour grid can't show (it is then only shown)."""
    days = hikrecord.DAYS
    code = {m: k for m, k, _l in hikrecord.MODES}
    cells = [["-"] * 24 for _ in days]
    for a in blk:
        if hikisapi.tag(a) != "ScheduleAction":
            continue
        s, e = _child(a, "ScheduleActionStartTime"), _child(a, "ScheduleActionEndTime")
        if s is None or e is None:
            return None
        sd, ed = _val(_child(s, "DayOfWeek")), _val(_child(e, "DayOfWeek"))
        hs, he = _hour_of(_val(_child(s, "TimeOfDay"))), _hour_of(_val(_child(e, "TimeOfDay")))
        if sd not in days or ed not in days or hs is None or he is None or hs > 23:
            return None
        di, dj = days.index(sd), days.index(ed)
        end = he if dj == di else 24 if (dj == (di + 1) % 7 and he == 0) else None
        if end is None or end < hs:
            return None
        acts = _child(a, "Actions")
        if acts is None or (_val(_child(acts, "Record")) or "true") != "true":
            continue
        k = code.get(_val(_child(acts, "ActionRecordingMode")) or "CMR")
        if k is None:
            return None
        for h in range(hs, end):
            cells[di][h] = k
    return "|".join("".join(r) for r in cells)


def _segments(row):
    out, h = [], 0
    while h < 24:
        k, j = row[h], h
        while j < 24 and row[j] == k:
            j += 1
        if k != "-":
            out.append((h, j, k))
        h = j
    return out


def _week_shown(v, names):
    rows = (v or "").split("|")
    if len(rows) != 7:
        return "—"
    if all(r == "-" * 24 for r in rows):
        return "Never"

    def one(r):
        segs = _segments(r)
        if not segs:
            return "not recorded"
        return ", ".join(f"{names.get(k, k)} {a:02d}–{b:02d}" for a, b, k in segs)
    short = [d[:3] for d in hikrecord.DAYS]
    out, i = [], 0
    while i < 7:
        j = i
        while j + 1 < 7 and rows[j + 1] == rows[i]:
            j += 1
        out.append(("Every day" if (i, j) == (0, 6) else short[i] if i == j else f"{short[i]}–{short[j]}") + ": " + one(rows[i]))
        i = j + 1
    return "; ".join(out)


def _set_week(blk, v):
    """Replace the schedule's periods with the week value, keeping everything else the
    recorder had in each period (DST flag, logging, snapshots) from its first one."""
    days, mode = hikrecord.DAYS, {k: m for m, k, _l in hikrecord.MODES}
    old = [a for a in blk if hikisapi.tag(a) == "ScheduleAction"]
    tmpl = old[0] if old else None
    for a in old:
        blk.remove(a)
    n = 0

    def sub(parent, name, text=None):
        el = _child(parent, name)
        if el is None:
            el = ET.SubElement(parent, _qual(blk, name))
        if text is not None:
            el.text = text
        return el
    for di, row in enumerate(v.split("|")):
        for h0, h1, k in _segments(row):
            n += 1
            a = copy.deepcopy(tmpl) if tmpl is not None else ET.Element(_qual(blk, "ScheduleAction"))
            sub(a, "id", str(n))
            s = sub(a, "ScheduleActionStartTime")
            sub(s, "DayOfWeek", days[di])
            sub(s, "TimeOfDay", f"{h0:02d}:00:00")
            e = sub(a, "ScheduleActionEndTime")
            sub(e, "DayOfWeek", days[di] if h1 < 24 else days[(di + 1) % 7])
            sub(e, "TimeOfDay", f"{h1 % 24:02d}:00:00")
            if tmpl is None:
                sub(a, "ScheduleDSTEnable", "false")
                sub(a, "Description", "nothing")
            acts = sub(a, "Actions")
            sub(acts, "Record", "true")
            if tmpl is None:
                sub(acts, "Log", "false")
                sub(acts, "SaveImg", "false")
            sub(acts, "ActionRecordingMode", mode[k])
            blk.append(a)


def _shown(f, v):
    """How a value reads in the change list."""
    k = f["kind"]
    if k == "area":
        return f"{len(_parse_pts(v) or [])} points drawn" if v else "not drawn"
    if k == "line":
        return "line drawn" if v else "not drawn"
    if k == "rects":
        n = len(_parse_rects(v) or [])
        return f"{n} box{'es' if n != 1 else ''}" if n else "none"
    if k == "grid":
        return f"{_grid_count(v, f['rows'], f['cols'])} of {f['rows'] * f['cols']} squares"
    if k == "set":
        names = {o["v"]: o["label"] for o in f.get("opts") or []}
        return ", ".join(names.get(x, x) for x in v.split(",") if x) or "Anything that moves"
    if k == "links":
        return ", ".join(LINK_LABEL.get(m, m) for m in v.split(",") if m) or "Nothing"
    if k == "week":
        return _week_shown(v, {x["v"]: x["label"] for x in f.get("modes") or []})
    if f.get("_iso"):
        return _days_shown(v)
    if k == "bool":
        return ON_OFF.get(v, v)
    return next((o["label"] for o in f.get("opts") or [] if o["v"] == v), v)


def _check(f, value):
    """The value as the camera wants it, or SettingsError."""
    if not f.get("editable"):
        raise SettingsError(f"{f['label']} can't be changed on this camera")
    v = "" if value is None else str(value).strip()
    k = f["kind"]
    if k in ("area", "line"):
        if v == "":
            return ""           # not drawn (Undo of a first drawing): the camera's empty form
        pts = _parse_pts(v)
        if not pts or not f["min"] <= len(pts) <= f["max"]:
            want = "two ends" if k == "line" else (f"{f['min']} points" if f["min"] == f["max"] else f"{f['min']}–{f['max']} points")
            raise SettingsError(f"{f['label']}: draw {want} on the picture")
        if len(set(pts)) != len(pts):
            raise SettingsError(f"{f['label']}: two points are on the same spot")
        w, h = f["nw"], f["nh"]
        return _pts_str([_to_ui(*_to_dev(u, t, w, h), w, h) for u, t in pts])
    if k == "rects":
        rects = _parse_rects(v)
        if rects is None or len(rects) > f["max"]:
            raise SettingsError(f"{f['label']}: up to {f['max']} boxes, each at least a little wide and high")
        w, h = f["nw"], f["nh"]
        canon = []
        for l, t, r, b in rects:
            pts = [_to_dev(l, b, w, h), _to_dev(l, t, w, h), _to_dev(r, t, w, h), _to_dev(r, b, w, h)]
            canon.append(pts)
        return _rects_str(canon, w, h)
    if k == "grid":
        v = v.lower()
        rows, cols = f["rows"], f["cols"]
        per = (cols + 7) // 8 * 2
        if len(v) != rows * per or not re.fullmatch(r"[0-9a-f]*", v):
            raise SettingsError(f"{f['label']}: not a grid of {cols} × {rows} squares")
        pad = per * 4 - cols
        if any(int(v[r * per:(r + 1) * per], 16) & ((1 << pad) - 1) for r in range(rows)):
            raise SettingsError(f"{f['label']}: not a grid of {cols} × {rows} squares")
        return v
    if k == "set":
        allowed = [o["v"] for o in f.get("opts") or []]
        got = [x.strip() for x in v.split(",") if x.strip()]
        if any(x not in allowed for x in got):
            raise SettingsError(f"{f['label']}: not one of the camera's choices")
        return ",".join(x for x in allowed if x in got)
    if k == "links":
        allowed = {o["v"] for o in f.get("opts") or []}
        got = [x.strip() for x in v.split(",") if x.strip()]
        if any(x not in allowed and x not in f.get("fixed", []) for x in got):
            raise SettingsError(f"{f['label']}: not something this camera can do for this alarm")
        if set(f.get("fixed", [])) - set(got):
            raise SettingsError(f"{f['label']}: actions set on the camera's own page stay as they are")
        return ",".join(_link_order(got))
    if k == "week":
        rows = v.split("|")
        allowed = {x["v"] for x in f.get("modes") or []} | {"-"}
        if len(rows) != 7 or any(len(r) != 24 or set(r) - allowed for r in rows):
            raise SettingsError(f"{f['label']}: not a week of hours this recorder can record")
        for d, r in zip(hikrecord.DAYS, rows):
            if len(_segments(r)) > hikrecord.MAX_PER_DAY:
                raise SettingsError(f"{f['label']}: {d} has more than {hikrecord.MAX_PER_DAY} separate periods — "
                                    "the recorder takes up to that many a day")
        return v
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
        if f.get("step") and (int(v) - f["min"]) % f["step"]:
            raise SettingsError(f"{f['label']}: in steps of {f['step']}")
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


# ---- writing a value into its document ------------------------------------------------
def _set_points(parent, list_tag, pt_tag, pts):
    lst = _child(parent, list_tag)
    if lst is None:
        lst = ET.Element(_qual(parent, list_tag))
        # put it where the camera keeps it in a sibling item, else before the target setting
        kids = list(parent)
        at = next((i for i, c in enumerate(kids) if hikisapi.tag(c) in ("detectionTarget", "alarmConfidence")), len(kids))
        parent.insert(at, lst)
    for c in list(lst):
        lst.remove(c)
    for x, y in pts:
        p = ET.SubElement(lst, _qual(parent, pt_tag))
        ET.SubElement(p, _qual(parent, "positionX")).text = str(x)
        ET.SubElement(p, _qual(parent, "positionY")).text = str(y)


def _set_masks(lst, rects, w, h):
    old = [r for r in lst if hikisapi.tag(r) == "PrivacyMaskRegion"]
    for r in old:
        lst.remove(r)
    for i, (l, t, r_, b) in enumerate(rects, 1):
        reg = old[i - 1] if i - 1 < len(old) else None
        if reg is None:
            reg = ET.Element(_qual(lst, "PrivacyMaskRegion"))
            ET.SubElement(reg, _qual(lst, "id"))
            ET.SubElement(reg, _qual(lst, "enabled"))
        _child(reg, "id").text = str(i)
        en = _child(reg, "enabled")
        if en is not None:
            en.text = "true"
        _set_points(reg, "RegionCoordinatesList", "RegionCoordinates",
                    [_to_dev(l, b, w, h), _to_dev(l, t, w, h), _to_dev(r_, t, w, h), _to_dev(r_, b, w, h)])
        lst.append(reg)


LINK_TEMPLATE = {
    "record": [("id", "record-{n}"), ("notificationMethod", "record"), ("notificationRecurrence", "beginning"), ("videoInputID", "{n}")],
    "center": [("id", "center"), ("notificationMethod", "center"), ("notificationRecurrence", "beginning")],
    "beep": [("id", "beep"), ("notificationMethod", "beep"), ("notificationRecurrence", "beginning")],
    "whiteLight": [("id", "whiteLight"), ("notificationMethod", "whiteLight"), ("notificationRecurrence", "beginning"),
                   ("WhiteLightAction/whiteLightDurationTime", "0")],
    "FTP": [("id", "FTP"), ("notificationMethod", "FTP"), ("notificationRecurrence", "beginning")],
}


def _set_links(doc, want, lens):
    lst = _child(doc, "EventTriggerNotificationList")
    if lst is None:
        lst = ET.SubElement(doc, _qual(doc, "EventTriggerNotificationList"))
    keep = set(want)
    for n in list(lst):
        if (_val(_child(n, "notificationMethod")) or "") not in keep:
            lst.remove(n)
    have = set(_links_of(doc))
    for m in want:
        if m in have:
            continue
        n = ET.SubElement(lst, _qual(doc, "EventTriggerNotification"))
        for path, val in LINK_TEMPLATE[m]:
            el = n
            for part in path.split("/"):
                el = _child(el, part) if _child(el, part) is not None else ET.SubElement(el, _qual(doc, part))
            el.text = val.replace("{n}", str(lens))


def _put_value(doc, f, nv, lens):
    dk, path = f["id"].split(".", 1)
    k = f["kind"]
    if k in ("area", "line"):
        reg = _walk(doc, f["_region"])
        w, h = f["nw"], f["nh"]
        pts = [_to_dev(u, t, w, h) for u, t in _parse_pts(nv)] if nv else [(0, 0)] * max(f["min"], 2)
        _set_points(reg, f["_coords"][0], f["_coords"][1], pts)
    elif k == "rects":
        _set_masks(_walk(doc, path), _parse_rects(nv), f["nw"], f["nh"])
    elif k == "links":
        _set_links(doc, [m for m in nv.split(",") if m], lens)
    elif k == "week":
        _set_week(_walk(doc, path), nv)
    elif f.get("_iso"):
        _walk(doc, path).text = f"P{int(nv)}DT0H"
    elif path == "resolution":
        w, h = nv.split("x")
        _walk(doc, "Video/videoResolutionWidth").text = w
        _walk(doc, "Video/videoResolutionHeight").text = h
    else:
        _walk(doc, path).text = nv


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
        self._ptz_logged = {}
        self._risky_lock = threading.Lock()    # one risky job at a time on the whole site

    def start(self, engine, cfg, on_change=None, log_path=None):
        self.engine, self.cfg, self.on_change = engine, cfg, on_change
        if log_path:
            self.log = Log(log_path)
        return self

    def enabled(self):
        return bool((self.cfg().get("features") or {}).get("camera_manage"))

    def risky(self):
        return bool((self.cfg().get("features") or {}).get("camera_risky"))

    def _admin_client(self, d):
        """The client risky work uses: the admin login kept aside (hiksecurity), else the
        saved login. Its own one-try slot (`<key>#admin`), so a refusal of one login is
        never wiped out by the other one working."""
        a = creds.get_admin(d["key"])
        if not a["username"]:
            return self.engine._client(d)
        return hikisapi.Client(d["key"] + "#admin", d.get("ip"), a["username"], a["password"], d.get("ports"))

    def _risky_gate(self, confirm_text, undo=False):
        if not self.risky():
            raise SettingsError("Risky camera work is turned off for this site (Settings → Cameras).", 403)
        if not undo and str(confirm_text or "").strip() != hiksecurity.CONFIRM_WORD:
            raise SettingsError(f"type {hiksecurity.CONFIRM_WORD} to confirm this change", 400,
                                {"needs_word": hiksecurity.CONFIRM_WORD})

    def _lock_for(self, key):
        with self._lk:
            return self._locks.setdefault(key, threading.Lock())

    def _target(self, key, lens=1, route=None):
        """The camera and the way to change it: its own login ("direct"), else its
        recorder ("nvr"), or the route asked for. Returns (entry, route, recorder channel
        or None, client, semaphore, device for History). A recorder's own key is the
        recorder itself, with its saved login."""
        inv = self.engine.inventory()
        e = next((c for c in inv["cameras"] if c["key"] == key), None)
        if e is None:
            rec = next((r for r in inv.get("recorders") or [] if r["key"] == key), None)
            if rec is not None:
                return self._recorder(rec)
            raise SettingsError("unknown camera", 404)
        d = e.get("_d")
        if d and not campics._hikish(d):
            raise SettingsError("Changing settings from Netwatch works on Hikvision cameras only; "
                                "use this camera's own web page.", 409, {"reason": "not_hikvision"})
        if route not in (None, "", "direct", "nvr"):
            raise SettingsError("unknown route")
        own = bool(e.get("_own_ok") and d)
        via = bool(e.get("_via_ok") and e.get("_nvr"))
        use = route or ("direct" if own else "nvr" if via else "")
        if use == "direct" and not own:
            raise SettingsError("This camera has no saved login of its own." + (" It can be changed through its recorder." if via else ""),
                                409, {"reason": "no_own_login"})
        if use == "nvr" and not via:
            raise SettingsError("This camera is not reached through a recorder with a saved login.", 409, {"reason": "no_recorder"})
        if not use:
            raise SettingsError("Changing settings needs the camera's own saved login, or its recorder's.", 409,
                                {"reason": "no_own_login"})
        if not e.get("online"):
            raise SettingsError("the camera is offline", 409, {"reason": "offline"})
        if use == "nvr":
            chan = (e.get("_vchans") or {}).get(int(lens)) or e.get("_chan") or {}
            ch = str(chan.get("id") or "")
            if not ch.isdigit():
                raise SettingsError("the recorder gave this camera no channel number", 409, {"reason": "no_channel"})
            if chan.get("online") is False or (int(lens) == 1 and (e.get("nvr") or {}).get("chan_online") is False):
                raise SettingsError("the recorder has lost this camera (channel not connected)", 409, {"reason": "offline"})
            nvr = e["_nvr"]
            return e, "nvr", int(ch), self.engine._client(nvr), self.engine._nvr_sems[nvr["key"]], nvr
        return e, "direct", None, self.engine._client(d), self.engine._sem, d

    def _recorder(self, rec):
        d = next((x for x in (self.engine.devices() or []) if x.get("key") == rec["key"]), None)
        if d is None:
            raise SettingsError("unknown recorder", 404)
        if not campics._hikish(d):
            raise SettingsError("Changing settings from Netwatch works on Hikvision recorders only.", 409,
                                {"reason": "not_hikvision"})
        if not rec.get("has_login"):
            raise SettingsError("This recorder has no saved login.", 409, {"reason": "no_own_login"})
        if rec.get("login_refused"):
            raise SettingsError("The recorder refused the saved login — fix it under the device's login first.", 403)
        if not rec.get("online"):
            raise SettingsError("the recorder is offline", 409, {"reason": "offline"})
        e = {"key": rec["key"], "name": rec.get("name") or d.get("ip"), "ip": d.get("ip"), "mac": d.get("mac"),
             "online": True, "_d": d, "_own_ok": True, "_recorder": True}
        return e, "direct", None, self.engine._client(d), self.engine._nvr_sems[rec["key"]], d

    def _groups(self, e):
        return RECORDER_GROUPS if e.get("_recorder") else CAMERA_GROUPS

    def _lenses(self, e):
        return list(range(1, len(e.get("views") or []) + 1)) or [1]

    def _read(self, cl, path, need=True, memo=None):
        if memo is not None and path in memo:
            hit = memo[path]
            if isinstance(hit, SettingsError):
                if not need:
                    return None
                raise hit
            return hit
        try:
            root = self._read1(cl, path, need)
        except SettingsError as e:
            if memo is not None:
                memo[path] = e
            raise
        if memo is not None and root is not None:
            memo[path] = root
        return root

    def _read1(self, cl, path, need):
        r = cl.get(path, max_bytes=MAX_DOC)
        if r.status != 200:
            if not need:
                return None
            raise SettingsError(f"the camera answered HTTP {r.status} for {path.split('?')[0]}"
                                + (" — the login may lack the rights" if r.status in (401, 403) else ""), 502,
                                {"http": r.status})
        root = _parse_json(r.content) if _is_json(path) else hikisapi.parse(r.content)
        if root is None:
            raise SettingsError(f"the camera's answer for {path} could not be read", 502)
        return root

    def _section(self, cl, sec, with_caps=True, memo=None, skip_missing=False):
        """Read a section's documents and build its form. A section marked `optional`
        (detection) whose own documents the camera doesn't have raises _Missing — that
        tab is left out. Its helper documents (alarm actions) may be missing quietly."""
        docs, caps, errs = {}, {}, []
        own = set((sec.get("optional") or "").split("|")) - {""}
        missing = []
        for dk, path in sec["docs"].items():
            try:
                docs[dk] = self._read(cl, path, memo=memo)
            except SettingsError as e:
                if own:
                    if dk in own:
                        missing.append(dk)
                        if (e.extra or {}).get("http") not in (403, 404, 400, 500, 501):
                            errs.append(str(e))
                    continue
                errs.append(str(e))
                continue
            if with_caps and dk not in sec.get("nocaps", ()):
                try:
                    caps[dk] = self._read(cl, _cap_path(path), need=False, memo=memo)
                except SettingsError:
                    caps[dk] = None
        if own and skip_missing and set(missing) >= own and not errs:
            raise _Missing()
        specs = list(sec["fields"])
        if sec.get("gen"):
            specs += sec["gen"](_HELP, docs, caps)
        specs += sec.get("tail", [])
        fields = []
        for fid, label, help_, fb in specs:
            f = _field(fid, label, help_, fb, docs, caps)
            if f:
                fields.append(f)
        return docs, fields, errs

    def read(self, key, lens=1, group="basic", route=None):
        if group not in GROUPS:
            raise SettingsError("unknown settings group")
        if group == "record" and not route:
            route = "nvr"            # recording is the recorder's: always through it
        if group == "security":
            route = "direct"         # a device's own users and ports: its own login only
        e, use, ch, cl, sem, _hd = self._target(key, lens, route)
        if group == "security":
            cl = self._admin_client(e["_d"])
        if group not in self._groups(e):
            raise SettingsError("these settings are a camera's" if e.get("_recorder") else "these settings are a recorder's")
        lenses = self._lenses(e)
        if int(lens) not in lenses:
            raise SettingsError("no such lens", 404)
        out, memo, extra = [], {}, {}
        with sem:
            if group == "security":
                try:
                    extra["info"] = hiksecurity.info(cl, key)
                except hikisapi.LoginRefused as ex:
                    raise SettingsError(str(ex) or "the device refused the saved login", 403) from None
                extra.update(risky=self.risky(), confirm_word=hiksecurity.CONFIRM_WORD)
            for sec in sections(lens, group, ch):
                if sec.get("device") and int(lens) != 1:
                    continue
                try:
                    _docs, fields, errs = self._section(cl, sec, memo=memo, skip_missing=True)
                except _Missing:
                    continue
                except (hikisapi.LoginRefused,) as ex:
                    raise SettingsError(str(ex) or "the camera refused the saved login", 403) from None
                except (hikisapi.Unreachable, hikisapi.TooBig) as ex:
                    errs, fields = [f"could not read: {str(ex) or ex.__class__.__name__}"], []
                fields = [{k: v for k, v in f.items() if not k.startswith("_")} for f in fields]
                out.append({"id": sec["id"], "label": sec["label"], "risk": sec["risk"], "note": sec.get("note", ""),
                            "presets": sec.get("presets", []), "fields": fields,
                            "error": "; ".join(errs) if not fields else ""})
        d = e.get("_d") or {}
        nvr = e.get("_nvr") or {}
        return {"ok": True, "key": key, "name": e["name"], "lens": int(lens), "lenses": lenses, "group": group,
                "route": use, "via": {"name": (e.get("nvr") or {}).get("name") or nvr.get("ip"), "ip": nvr.get("ip"), "channel": ch} if ch else None,
                "routes": ["nvr"] if group == "record" else [r for r, ok in (("direct", bool(e.get("_own_ok") and e.get("_d"))),
                                           ("nvr", bool(e.get("_via_ok") and e.get("_nvr")))) if ok],
                "recorder": bool(e.get("_recorder")),
                "netwatch_name": (d.get("name") or e.get("name") or "")[:32], "can_change": self.enabled(),
                "why_not": "" if self.enabled() else "Camera settings are turned off for this site (Settings → Cameras).",
                "sections": out, "ts": int(time.time()), **extra}

    def apply(self, key, lens, section, changes, who="", expect=None, undo_of=None, confirm=False, route=None,
              confirm_text=""):
        """Change some settings of one section. `expect` = the values the person saw
        ({field: value}); a camera that shows something else now is a conflict (409)."""
        if not self.enabled():
            raise SettingsError("Camera settings are turned off for this site (Settings → Cameras).", 403)
        if not isinstance(changes, dict) or not changes:
            raise SettingsError("nothing to change")
        if section == "record" and not route:
            route = "nvr"
        risky = section in hiksecurity.SECTION_IDS
        if risky:
            self._risky_gate(confirm_text, undo=bool(undo_of))
            route = "direct"
        e, use, ch, cl, sem, hist_dev = self._target(key, lens, route)
        if risky:
            cl = self._admin_client(e["_d"])
        sec = next((s for g in self._groups(e) for s in sections(lens, g, ch) if s["id"] == section), None)
        if sec is None:
            raise SettingsError("unknown settings section" + (" (not available through the recorder)" if ch else ""))
        if sec["risk"] == "careful" and not confirm:
            raise SettingsError("confirm this change first", 400, {"needs_confirm": True})
        if int(lens) not in self._lenses(e):
            raise SettingsError("no such lens", 404)
        lock = self._lock_for(key)
        if not lock.acquire(blocking=False):
            raise SettingsError("another change on this camera is still running", 409)
        if risky and not self._risky_lock.acquire(blocking=False):
            lock.release()
            raise SettingsError("another risky job is running on this site; one at a time", 409)
        try:
            with sem:
                return self._apply(e, cl, sec, int(lens), changes, who, expect or {}, undo_of,
                                   route=use, ch=ch, hist_dev=hist_dev)
        except hikisapi.LoginRefused as ex:
            raise SettingsError(str(ex) or "the camera refused the saved login", 403) from None
        except (hikisapi.Unreachable, hikisapi.TooBig) as ex:
            raise SettingsError(f"the camera did not answer ({str(ex) or ex.__class__.__name__})", 502) from None
        finally:
            if risky:
                self._risky_lock.release()
            lock.release()

    def _apply(self, e, cl, sec, lens, changes, who, expect, undo_of, route="direct", ch=None, hist_dev=None):
        dev = "recorder" if (e.get("_recorder") or sec["id"] in RECORDER_SECTIONS) else "camera"
        docs, fields, errs = self._section(cl, sec, memo={})
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
            raise SettingsError(f"changed on the {dev} since you looked: " + ", ".join(moved), 409,
                                {"conflict": moved})
        if not want:
            return {"ok": True, "applied": [], "unchanged": True, "message": f"Nothing to change — the {dev} already has these values."}
        before = {fid: by[fid]["value"] for fid in want}
        shown_before = {fid: by[fid]["shown"] for fid in want}
        touched = []
        for fid, nv in want.items():
            dk, path = fid.split(".", 1)
            if dk in sec.get("readonly", ()):
                raise SettingsError(f"{by[fid]['label']} can't be changed")
            _put_value(docs[dk], by[fid], nv, lens)
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
            js = _is_json(sec["docs"][dk])
            r = cl.request("PUT", sec["docs"][dk], data=serialize_json(docs[dk]) if js else serialize(docs[dk]),
                           headers={"Content-Type": "application/json" if js else "application/xml"}, max_bytes=64 << 10)
            ok, rb, msg = _status(r)
            if not ok:
                failed = f"the {dev} refused the change: {msg}"
                break
            reboot = reboot or rb
            written.append(dk)
        # Read back what was written: the proof that it took.
        # (the helper documents the form needs, such as the alarm-action list, come along unwritten)
        _d2, fields2, _e2 = self._section(cl, {**sec, "optional": "", "docs": {
            k: v for k, v in sec["docs"].items() if k in written or k in sec.get("readonly", ())}})
        now = {f["id"]: f for f in fields2}
        rows, kept_all = [], True
        for fid in want:
            dk = fid.split(".", 1)[0]
            got = now.get(fid)
            kept = bool(got and got["value"] == want[fid]) if dk in written else False
            kept_all = kept_all and kept
            rows.append({"id": fid, "label": by[fid]["label"], "before": before[fid], "after": want[fid],
                         "shown_before": shown_before[fid],
                         "shown_after": _shown(by[fid], want[fid]),
                         "now": got["value"] if got else None, "written": dk in written, "kept": kept})
        d = hist_dev or e.get("_d")
        entry = {"id": uuid.uuid4().hex[:12], "ts": int(time.time()), "who": who or "", "key": e["key"],
                 "ip": e.get("ip"), "mac": e.get("mac"), "name": e.get("name"), "lens": lens,
                 "route": route, "channel": ch,
                 "section": sec["id"], "risk": sec["risk"], "fields": rows,
                 "section_label": sec["label"] + (f" — {e.get('name')}, through the recorder (channel {ch})" if ch else ""),
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
        msg = (f"Done — the {dev} kept every change." if kept_all and not failed
               else failed or f"The {dev} did not keep every change — see the list.")
        if reboot:
            msg += f" The {dev} says it needs a restart for this to take effect."
        return {"ok": not failed and kept_all, "change": entry, "message": msg, "reboot": reboot}

    # ---- PTZ: moves that stop by themselves, and presets -------------------------------
    def ptz(self, key, lens=1, route=None):
        """What PTZ this camera has (from its capabilities; through a recorder that
        doesn't say, a PTZ model number is enough) and its presets."""
        e, use, ch, cl, sem, _hd = self._target(key, lens, route)
        if e.get("_recorder"):
            raise SettingsError("a recorder has no PTZ of its own", 404)
        base = _ptz_base(use, lens, ch)
        try:
            with sem:
                caps = self._read(cl, base + "/capabilities", need=False)
                presets = self._read(cl, base + "/presets", need=False)
        except hikisapi.LoginRefused as ex:
            raise SettingsError(str(ex) or "the camera refused the saved login", 403) from None
        except (hikisapi.Unreachable, hikisapi.TooBig) as ex:
            raise SettingsError(f"the camera did not answer ({str(ex) or ex.__class__.__name__})", 502) from None
        axes = {ax: caps is not None and _walk(caps, path) is not None for ax, path in PTZ_AXES}
        guessed = False
        if not any(axes.values()) and (PTZ_MODEL.match(e.get("model") or "") or "ptz" in (e.get("name") or "").lower()):
            axes, guessed = {"pan": True, "tilt": True, "zoom": True}, True
        plist = []
        for p in (presets if presets is not None else [])[:256]:
            pid = _val(_child(p, "id")) or ""
            if not pid.isdigit() or _val(_child(p, "enabled")) == "false":
                continue
            plist.append({"id": int(pid), "name": (_val(_child(p, "presetName")) or f"Preset {pid}")[:40]})
        has = any(axes.values())
        return {"ok": True, "key": key, "lens": int(lens), "route": use, "has_ptz": has, "axes": axes, "guessed": guessed,
                "presets": plist[:64] if has else [], "can_move": self.enabled() and has,
                "why_not": "" if self.enabled() else "Camera settings are turned off for this site (Settings → Cameras)."}

    def ptz_do(self, key, lens=1, action="", pan=0, tilt=0, zoom=0, ms=500, preset=None, who="", route=None):
        """One move (pan/tilt/zoom -100..100 for at most PTZ_MAX_MS — the camera stops by
        itself, so a lost connection can't leave it turning), or go to a preset."""
        if not self.enabled():
            raise SettingsError("Camera settings are turned off for this site (Settings → Cameras).", 403)
        e, use, ch, cl, sem, hist_dev = self._target(key, lens, route)
        if e.get("_recorder"):
            raise SettingsError("a recorder has no PTZ of its own", 404)
        base = _ptz_base(use, lens, ch)
        if action == "move":
            try:
                p, t, z, ms = int(pan), int(tilt), int(zoom), int(ms)
            except (TypeError, ValueError):
                raise SettingsError("pan, tilt, zoom and ms must be whole numbers") from None
            if not all(-100 <= x <= 100 for x in (p, t, z)) or not (p or t or z):
                raise SettingsError("pan, tilt and zoom go from -100 to 100, and one must move")
            ms = min(max(ms, 100), PTZ_MAX_MS)
            what = "moved by hand"
        elif action == "goto":
            if not re.fullmatch(r"\d{1,3}", str(preset or "")) or not 1 <= int(preset) <= 300:
                raise SettingsError("not a preset number")
            what = f"went to preset {int(preset)}"
        else:
            raise SettingsError("unknown PTZ action")
        lock = self._lock_for(key + "#ptz")
        if not lock.acquire(blocking=False):
            raise SettingsError("the camera is still moving", 409)
        try:
            with sem:
                if action == "goto":
                    ok, _rb, msg = _status(cl.request("PUT", f"{base}/presets/{int(preset)}/goto", data=b"",
                                                      headers={"Content-Type": "application/xml"}, max_bytes=16 << 10))
                else:
                    body = (f'<?xml version="1.0" encoding="UTF-8"?><PTZData xmlns="{HIK_NS}"><pan>{p}</pan><tilt>{t}</tilt>'
                            f'<zoom>{z}</zoom><Momentary><duration>{ms}</duration></Momentary></PTZData>').encode()
                    ok, _rb, msg = _status(cl.request("PUT", base + "/momentary", data=body,
                                                      headers={"Content-Type": "application/xml"}, max_bytes=16 << 10))
                    if not ok:
                        # older firmware: a continuous move, always followed by a stop
                        cont = (f'<?xml version="1.0" encoding="UTF-8"?><PTZData xmlns="{HIK_NS}"><pan>{p}</pan><tilt>{t}</tilt>'
                                f'<zoom>{z}</zoom></PTZData>').encode()
                        stop = (f'<?xml version="1.0" encoding="UTF-8"?><PTZData xmlns="{HIK_NS}"><pan>0</pan><tilt>0</tilt>'
                                f'<zoom>0</zoom></PTZData>').encode()
                        try:
                            ok, _rb, msg = _status(cl.request("PUT", base + "/continuous", data=cont,
                                                              headers={"Content-Type": "application/xml"}, max_bytes=16 << 10))
                            if ok:
                                time.sleep(ms / 1000)
                        finally:
                            cl.request("PUT", base + "/continuous", data=stop,
                                       headers={"Content-Type": "application/xml"}, max_bytes=16 << 10)
        except hikisapi.LoginRefused as ex:
            raise SettingsError(str(ex) or "the camera refused the saved login", 403) from None
        except (hikisapi.Unreachable, hikisapi.TooBig) as ex:
            raise SettingsError(f"the camera did not answer ({str(ex) or ex.__class__.__name__})", 502) from None
        finally:
            lock.release()
        if not ok:
            raise SettingsError(f"the camera refused: {msg}", 502)
        self._ptz_history(e, hist_dev, lens, what, who, use, ch)
        return {"ok": True, "message": "Done."}

    def _ptz_history(self, e, dev, lens, what, who, use, ch):
        """A PTZ move changes what the camera watches, so it goes into History — at most
        once per camera every PTZ_LOG_GAP seconds (a session of nudges is one line)."""
        now = time.time()
        k = e["key"]
        if what.startswith("moved") and now - self._ptz_logged.get(k, 0) < PTZ_LOG_GAP:
            return
        self._ptz_logged[k] = now
        if self.on_change and dev:
            try:
                self.on_change(dev, {"section_label": "PTZ" + (f" — {e.get('name')}, through the recorder (channel {ch})" if ch else ""),
                                     "who": who, "result": "ok", "lens": int(lens), "undo_of": None,
                                     "fields": [{"label": "Camera", "shown_before": "", "shown_after": what, "kept": True}]})
            except Exception:  # noqa: BLE001
                pass

    # ---- the Netwatch account (hiksecurity.py) -----------------------------------------
    def account(self, key, action, who="", confirm_text="", undo_of=None):
        """action "create": make the `netwatch` Operator account and log in with it from
        now on (the admin login is kept aside for risky jobs); "remove": delete it and go
        back to admin. Needs both camera switches and the typed word (not for Undo)."""
        if action not in ("create", "remove"):
            raise SettingsError("unknown account action")
        if not self.enabled():
            raise SettingsError("Camera settings are turned off for this site (Settings → Cameras).", 403)
        self._risky_gate(confirm_text, undo=bool(undo_of))
        e, _use, _ch, _cl, sem, d = self._target(key, 1, "direct")
        lock = self._lock_for(key)
        if not lock.acquire(blocking=False):
            raise SettingsError("another change on this device is still running", 409)
        if not self._risky_lock.acquire(blocking=False):
            lock.release()
            raise SettingsError("another risky job is running on this site; one at a time", 409)
        dev = "recorder" if e.get("_recorder") else "camera"
        try:
            with sem:
                cl = self._admin_client(d)
                if action == "create":
                    ports = d.get("ports")

                    def client_for(u, pw):
                        return hikisapi.Client(d["key"] + "#new", d.get("ip"), u, pw, ports)
                    res = hiksecurity.create(cl, d, _HELP, client_for)
                else:
                    res = hiksecurity.remove(cl, d, _HELP)
        except hiksecurity.AccountError as ex:
            raise SettingsError(str(ex), ex.status) from None
        except hikisapi.LoginRefused as ex:
            raise SettingsError(str(ex) or f"the {dev} refused the admin login", 403) from None
        except (hikisapi.Unreachable, hikisapi.TooBig) as ex:
            raise SettingsError(f"the {dev} did not answer ({str(ex) or ex.__class__.__name__})", 502) from None
        finally:
            self._risky_lock.release()
            lock.release()
        made = action == "create"
        rows = [{"id": "account", "label": "Netwatch account", "before": "" if made else "netwatch",
                 "after": "netwatch" if made else "", "shown_before": "none" if made else "netwatch (Operator)",
                 "shown_after": "netwatch (Operator)" if made else "removed", "written": True, "kept": True},
                {"id": "login", "label": "Netwatch logs in as", "before": res["admin"] if made else "netwatch",
                 "after": "netwatch" if made else res["admin"], "shown_before": res["admin"] if made else "netwatch",
                 "shown_after": "netwatch" if made else res["admin"], "written": True, "kept": True}]
        entry = {"id": uuid.uuid4().hex[:12], "ts": int(time.time()), "who": who or "", "key": key,
                 "ip": e.get("ip"), "mac": e.get("mac"), "name": e.get("name"), "lens": 1, "route": "direct",
                 "channel": None, "section": "account", "risk": "dangerous", "fields": rows,
                 "section_label": "Netwatch account " + ("set up" if made else "removed"),
                 "result": "ok", "error": "", "reboot": False, "undo_of": undo_of, "undone_by": None,
                 "no_undo": not made, "steps": res["steps"]}
        self.log.add(entry)
        if undo_of:
            self.log.mark(undo_of, undone_by=entry["id"])
        if self.on_change:
            try:
                self.on_change(d, entry)
            except Exception:  # noqa: BLE001 - history must never fail a change
                pass
        msg = (f"Done — Netwatch now logs in to this {dev} as 'netwatch' (Operator). The admin login is kept "
               "for risky jobs only." if made else
               f"Done — the netwatch account is gone and Netwatch logs in as {res['admin']} again.")
        return {"ok": True, "change": entry, "message": msg}

    # ---- recorder channels and the password job (hikchannels.py) ------------------------
    def _log_job(self, e, d, section, label, rows, who, undo=None, undo_of=None, steps=None, result="ok", error=""):
        entry = {"id": uuid.uuid4().hex[:12], "ts": int(time.time()), "who": who or "", "key": e["key"],
                 "ip": e.get("ip"), "mac": e.get("mac"), "name": e.get("name"), "lens": 1, "route": "direct",
                 "channel": None, "section": section, "risk": "dangerous", "fields": rows,
                 "section_label": label, "result": result, "error": error, "reboot": False,
                 "undo_of": undo_of, "undone_by": None, "no_undo": undo is None, "undo": undo, "steps": steps or []}
        self.log.add(entry)
        if undo_of:
            self.log.mark(undo_of, undone_by=entry["id"])
        if self.on_change:
            try:
                self.on_change(d, entry)
            except Exception:  # noqa: BLE001 - history must never fail a change
                pass
        return entry

    def _netwatch_cams(self):
        """Netwatch's own devices by IP (the ones a channel can be matched to)."""
        return {d.get("ip"): d for d in (self.engine.devices() or []) if d.get("ip")}

    def channels(self, key):
        """A recorder's channels, each matched to the Netwatch device at its address,
        and the cameras Netwatch could add (it has their admin login)."""
        e, _use, _ch, cl, sem, d = self._target(key, 1, "direct")
        if not e.get("_recorder"):
            raise SettingsError("channels belong to a recorder", 400)
        by_ip = self._netwatch_cams()
        with sem:
            try:
                rows = hikchannels.listing(self._admin_client(d))
                lo, hi = hikchannels.caps(self._admin_client(d))
            except hikisapi.LoginRefused as ex:
                raise SettingsError(str(ex) or "the recorder refused the saved login", 403) from None
        used = {r["ip"] for r in rows}
        for r in rows:
            nd = by_ip.get(r["ip"])
            lg = hikchannels.admin_login(nd["key"]) if nd else None
            r["netwatch"] = {"key": nd["key"], "name": (nd.get("name") or nd.get("model") or nd["ip"])[:48]} if nd else None
            r["login_known"] = bool(lg)
            r["login_user"] = lg[0] if lg else ""
        addable = []
        for c in self.engine.inventory()["cameras"]:
            dd = c.get("_d")
            if not dd or not dd.get("ip") or dd["ip"] in used or not campics._hikish(dd):
                continue
            if hikchannels.admin_login(dd["key"]):
                addable.append({"key": dd["key"], "name": (c.get("name") or dd["ip"])[:48], "ip": dd["ip"]})
        free = [n for n in range(lo, hi + 1) if n not in {r["id"] for r in rows}]
        return {"ok": True, "key": key, "name": e["name"], "channels": rows, "addable": addable,
                "free": len(free), "can_change": self.enabled() and self.risky(),
                "why_not": "" if self.enabled() and self.risky() else
                "Risky camera work is turned off for this site (Settings → Cameras).",
                "confirm_word": hiksecurity.CONFIRM_WORD}

    def channel_do(self, key, action, ch=None, camera=None, ip=None, port=None, who="", confirm_text="", undo_of=None,
                   track=None):
        """fix_login / move (new address, port) / add (a Netwatch camera) / remove one
        channel. The channel's password always comes from Netwatch's saved admin login
        for the camera at that address."""
        if action not in ("fix_login", "move", "add", "remove"):
            raise SettingsError("unknown channel action")
        if not self.enabled():
            raise SettingsError("Camera settings are turned off for this site (Settings → Cameras).", 403)
        self._risky_gate(confirm_text, undo=bool(undo_of))
        e, _use, _c, _cl, sem, d = self._target(key, 1, "direct")
        if not e.get("_recorder"):
            raise SettingsError("channels belong to a recorder", 400)
        by_ip = self._netwatch_cams()
        lock = self._lock_for(key)
        if not lock.acquire(blocking=False):
            raise SettingsError("another change on this recorder is still running", 409)
        if not self._risky_lock.acquire(blocking=False):
            lock.release()
            raise SettingsError("another risky job is running on this site; one at a time", 409)
        try:
            with sem:
                cl = self._admin_client(d)
                rows = hikchannels.listing(cl)
                cur = next((r for r in rows if r["id"] == int(ch or 0)), None) if action != "add" else None
                if action != "add" and cur is None:
                    raise SettingsError(f"the recorder has no channel {ch}", 404)

                def login_for(addr):
                    nd = by_ip.get(addr)
                    lg = hikchannels.admin_login(nd["key"]) if nd else None
                    if not lg:
                        raise SettingsError(f"Netwatch has no admin login for the camera at {addr}, so it can't give the "
                                            "recorder one. Save the camera's login in Netwatch first.", 409)
                    return lg
                H = _HELP
                if action == "fix_login":
                    u, pw = login_for(cur["ip"])
                    hikchannels.put_channel(cl, H, cur["id"], hikchannels._set_login(hikchannels._doc(cl, cur["id"]), u, pw))
                    back = hikchannels.wait_online(cl, cur["id"])
                    rows_l = [{"id": "login", "label": f"Channel {cur['id']} ({cur['name']}) login", "before": cur["user"],
                               "after": u, "shown_before": f"{cur['user']}, {'connected' if cur['online'] else 'not connected'}",
                               "shown_after": f"{u} from Netwatch, {'connected' if back else 'NOT connected yet'}",
                               "written": True, "kept": back}]
                    entry = self._log_job(e, d, "channels", f"Recorder channel {cur['id']}: login fixed", rows_l, who,
                                          undo_of=undo_of, result="ok" if back else "partial")
                    msg = ("Done — the channel is connected." if back else
                           "The recorder took the login but the channel is not connected yet; check the camera.")
                elif action == "move":
                    ip = str(ip or "").strip()
                    if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip) or any(int(x) > 255 for x in ip.split(".")):
                        raise SettingsError("a camera address is four numbers 0–255, like 10.0.0.64")
                    try:
                        port = int(port or cur["port"] or 8000)
                    except (TypeError, ValueError):
                        raise SettingsError("the port is a number") from None
                    if not 1 <= port <= 65535:
                        raise SettingsError("the port is 1–65535")
                    if ip == cur["ip"] and port == cur["port"]:
                        return {"ok": True, "unchanged": True, "message": "Nothing to change."}
                    if any(r["ip"] == ip and r["id"] != cur["id"] for r in rows):
                        raise SettingsError(f"another channel already records {ip}", 409)
                    u, pw = login_for(ip)
                    hikchannels.put_channel(cl, H, cur["id"], hikchannels._set_login(
                        hikchannels._doc(cl, cur["id"]), u, pw, ip=ip, port=port))
                    back = hikchannels.wait_online(cl, cur["id"])
                    rows_l = [{"id": "address", "label": f"Channel {cur['id']} ({cur['name']}) address",
                               "before": f"{cur['ip']}:{cur['port']}", "after": f"{ip}:{port}",
                               "shown_before": f"{cur['ip']}:{cur['port']}", "shown_after": f"{ip}:{port}",
                               "written": True, "kept": back}]
                    entry = self._log_job(e, d, "channels", f"Recorder channel {cur['id']}: camera address changed", rows_l,
                                          who, undo={"action": "move", "ch": cur["id"], "ip": cur["ip"], "port": cur["port"]},
                                          undo_of=undo_of, result="ok" if back else "partial")
                    msg = "Done — the channel is connected." if back else "Changed, but the channel is not connected yet."
                elif action == "add":
                    nd = next((x for x in by_ip.values() if x.get("key") == camera), None) if camera else by_ip.get(str(ip or ""))
                    if nd is None:
                        raise SettingsError("pick a camera Netwatch knows", 400)
                    if any(r["ip"] == nd["ip"] for r in rows):
                        raise SettingsError("this recorder already records that camera", 409)
                    u, pw = login_for(nd["ip"])
                    lo, hi = hikchannels.caps(cl)
                    want = int(ch) if ch else None
                    taken = {r["id"] for r in rows}
                    free = [n for n in range(lo, hi + 1) if n not in taken]
                    if not free:
                        raise SettingsError("the recorder has no free channel", 409)
                    num = want if want in free else free[0]
                    if not rows:
                        raise SettingsError("the recorder has no channel to copy the form from", 409)
                    template = hikchannels._doc(cl, rows[0]["id"])
                    name = (nd.get("name") or nd.get("model") or nd["ip"])
                    hikchannels.add_channel(cl, H, hikchannels.new_channel(template, num, name, nd["ip"], 8000, u, pw))
                    back = hikchannels.wait_online(cl, num)
                    if track and num == int(ch or 0):
                        # Undo of a removal: the channel's recording comes back as it was, not as
                        # the recorder's defaults for a new channel
                        doc = hikisapi.parse(track.encode())
                        if doc is not None:
                            r = cl.request("PUT", f"/ISAPI/ContentMgmt/record/tracks/{num}01", data=serialize(doc),
                                           headers={"Content-Type": "application/xml"}, max_bytes=64 << 10)
                            if not _status(r)[0]:
                                back = False
                    rows_l = [{"id": "channel", "label": f"Channel {num}", "before": "", "after": nd["ip"],
                               "shown_before": "free", "shown_after": f"{name} ({nd['ip']}), "
                               f"{'connected' if back else 'NOT connected yet'}", "written": True, "kept": back}]
                    entry = self._log_job(e, d, "channels", f"Recorder channel {num}: camera added", rows_l, who,
                                          undo={"action": "remove", "ch": num}, undo_of=undo_of,
                                          result="ok" if back else "partial")
                    msg = (f"Done — channel {num} records {name}." if back else
                           f"Added as channel {num}, but it is not connected yet.")
                else:   # remove
                    tr = cl.get(f"/ISAPI/ContentMgmt/record/tracks/{cur['id']}01", max_bytes=MAX_DOC)
                    keep_track = tr.text if tr.status == 200 and hikisapi.parse(tr.content) is not None else None
                    hikchannels.delete_channel(cl, H, cur["id"], (creds.get_admin(key)["password"] or creds.get(key)["password"]))
                    if any(r["id"] == cur["id"] for r in hikchannels.listing(cl)):
                        raise SettingsError("the recorder still lists the channel", 502)
                    rows_l = [{"id": "channel", "label": f"Channel {cur['id']}", "before": cur["ip"], "after": "",
                               "shown_before": f"{cur['name']} ({cur['ip']})", "shown_after": "removed",
                               "written": True, "kept": True}]
                    nd = by_ip.get(cur["ip"])
                    undo = ({"action": "add", "ch": cur["id"], "ip": cur["ip"], "track": keep_track}
                            if nd and hikchannels.admin_login(nd["key"]) else None)
                    entry = self._log_job(e, d, "channels", f"Recorder channel {cur['id']}: camera removed", rows_l, who,
                                          undo=undo, undo_of=undo_of)
                    msg = ("Done — the recorder no longer records this camera. Its old footage stays until it is "
                           "overwritten.")
        except hikchannels.JobError as ex:
            raise SettingsError(str(ex), ex.status) from None
        except hikisapi.LoginRefused as ex:
            raise SettingsError(str(ex) or "the recorder refused the admin login", 403) from None
        except (hikisapi.Unreachable, hikisapi.TooBig) as ex:
            raise SettingsError(f"the recorder did not answer ({str(ex) or ex.__class__.__name__})", 502) from None
        finally:
            self._risky_lock.release()
            lock.release()
        return {"ok": entry["result"] == "ok", "change": entry, "message": msg}

    def _channels_using(self, ip, user):
        """[(recorder entry, device, admin client, [channel ids], [channels with another user])]
        for every recorder with a working login that records `ip`."""
        out = []
        for rec in self.engine.inventory().get("recorders") or []:
            if not rec.get("has_login") or rec.get("login_refused") or not rec.get("online"):
                continue
            try:
                e, _u, _c, _cl, sem, d = self._recorder(rec)
            except SettingsError:
                continue
            cl = self._admin_client(d)
            with sem:
                try:
                    rows = hikchannels.listing(cl)
                except (hikisapi.LoginRefused, hikisapi.Unreachable, hikisapi.TooBig):
                    continue
            mine = [r for r in rows if r["ip"] == ip]
            if mine:
                out.append((e, d, cl, [r["id"] for r in mine if r["user"] == user],
                            [r for r in mine if r["user"] != user]))
        return out

    def password_plan(self, key):
        """What the password job would do on this camera: its admin user and the recorder
        channels that would be given the new password."""
        e, _use, _ch, _cl, _sem, d = self._target(key, 1, "direct")
        if e.get("_recorder"):
            raise SettingsError("the password job is for cameras", 400)
        lg = hikchannels.admin_login(key)
        if not lg:
            raise SettingsError("Netwatch has no admin login for this camera.", 409)
        recs = self._channels_using(d.get("ip"), lg[0])
        return {"ok": True, "user": lg[0], "can_change": self.enabled() and self.risky(),
                "confirm_word": hiksecurity.CONFIRM_WORD, "min": hikchannels.PW_MIN, "max": hikchannels.PW_MAX,
                "recorders": [{"name": r[0]["name"], "channels": r[3],
                               "other_user": [{"channel": x["id"], "user": x["user"]} for x in r[4]]} for r in recs]}

    def password(self, key, new, again, who="", confirm_text=""):
        if not self.enabled():
            raise SettingsError("Camera settings are turned off for this site (Settings → Cameras).", 403)
        self._risky_gate(confirm_text)
        e, _use, _ch, _cl, sem, d = self._target(key, 1, "direct")
        if e.get("_recorder"):
            raise SettingsError("the password job is for cameras", 400)
        lg = hikchannels.admin_login(key)
        if not lg:
            raise SettingsError("Netwatch has no admin login for this camera.", 409)
        try:
            hikchannels.check_password(str(new or ""), str(again or ""), lg[0], lg[1])
        except hikchannels.JobError as ex:
            raise SettingsError(str(ex), ex.status) from None
        lock = self._lock_for(key)
        if not lock.acquire(blocking=False):
            raise SettingsError("another change on this camera is still running", 409)
        if not self._risky_lock.acquire(blocking=False):
            lock.release()
            raise SettingsError("another risky job is running on this site; one at a time", 409)
        try:
            recs = self._channels_using(d.get("ip"), lg[0])
            ports = d.get("ports")

            def client_for(u, pw):
                return hikisapi.Client(key + "#pwjob", d.get("ip"), u, pw, ports)
            with sem:
                res = hikchannels.change_password(self._admin_client(d), d, _HELP, str(new), str(again),
                                                  [(r[0]["name"], r[2], r[3]) for r in recs], client_for)
        except hikchannels.JobError as ex:
            raise SettingsError(str(ex), ex.status) from None
        except hikisapi.LoginRefused as ex:
            raise SettingsError(str(ex) or "the camera refused the admin login", 403) from None
        except (hikisapi.Unreachable, hikisapi.TooBig) as ex:
            raise SettingsError(f"the camera did not answer ({str(ex) or ex.__class__.__name__})", 502) from None
        finally:
            self._risky_lock.release()
            lock.release()
        bad = [c for c in res["channels"] if not c["online"]]
        rows = [{"id": "password", "label": f"{res['user']} password", "before": "", "after": "",
                 "shown_before": "old", "shown_after": "changed (saved in Netwatch)", "written": True, "kept": True}]
        rows += [{"id": f"ch{c['channel']}", "label": f"{c['recorder']} channel {c['channel']}", "before": "", "after": "",
                  "shown_before": "old password", "shown_after": ("new password, connected" if c["online"] else
                  "new password, NOT connected" if c["updated"] else f"NOT updated: {c['error']}"),
                  "written": c["updated"], "kept": c["online"]} for c in res["channels"]]
        lock_off = any("STILL PAUSED" in s for s in res["steps"])
        entry = self._log_job(e, d, "password", "Admin password changed", rows, who, steps=res["steps"],
                              result="ok" if not bad and not lock_off else "partial")
        msg = "Done — the camera has the new password and Netwatch saved it."
        if res["channels"]:
            msg += (" Every recorder channel is connected with it." if not bad else
                    f" {len(bad)} recorder channel(s) did not come back — use “Fix login” on the recorder's camera list.")
        if lock_off:
            msg += " The camera's login lock could not be switched back on — do it in Logins and security."
        return {"ok": not bad and not lock_off, "change": entry, "message": msg}

    # ---- the ISAPI explorer (hikexplorer.py) ---------------------------------------------
    def _explorer_target(self, key):
        if not self.enabled() or not self.risky():
            raise SettingsError("The ISAPI explorer needs Camera settings and Risky camera work (Settings → Cameras).", 403)
        e, _use, _ch, _cl, sem, d = self._target(key, 1, "direct")
        return e, sem, d

    def _explore_get(self, cl, path):
        r = cl.get(path, max_bytes=hikexplorer.MAX_DOC)
        if r.status != 200:
            raise SettingsError(f"the device answered HTTP {r.status} for {path}", 404 if r.status in (403, 404) else 502,
                                {"http": r.status})
        root = hikisapi.parse(r.content)
        if root is None:
            raise SettingsError("the explorer reads XML documents only", 415)
        return root

    def explore_read(self, key, path):
        try:
            path = hikexplorer.check_path(path)
        except hikexplorer.ExplorerError as ex:
            raise SettingsError(str(ex), ex.status) from None
        e, sem, d = self._explorer_target(key)
        with sem:
            try:
                cl = self._admin_client(d)
                root = self._explore_get(cl, path)
                try:
                    cap = self._explore_get(cl, path + "/capabilities")
                except SettingsError:
                    cap = None
            except hikisapi.LoginRefused as ex:
                raise SettingsError(str(ex) or "the device refused the saved login", 403) from None
            except (hikisapi.Unreachable, hikisapi.TooBig) as ex:
                raise SettingsError(f"the device did not answer ({str(ex) or ex.__class__.__name__})", 502) from None
        try:
            hikexplorer.check_path(path, write=True)
            writable = True
        except hikexplorer.ExplorerError:
            writable = False
        return {"ok": True, "key": key, "name": e["name"], "path": path, "writable": writable,
                "doc": _pretty(hikexplorer.redact(root)),
                "caps": serialize(hikexplorer.redact(cap)).decode("utf-8", "replace") if cap is not None else "",
                "secret": hikexplorer.SECRET, "common": hikexplorer.COMMON, "confirm_word": hiksecurity.CONFIRM_WORD}

    def explore_write(self, key, path, body, preview=False, who="", confirm_text="", undo_of=None, expect_now=None):
        """Change values of one document. preview=True only returns the change list."""
        try:
            path = hikexplorer.check_path(path, write=True)
        except hikexplorer.ExplorerError as ex:
            raise SettingsError(str(ex), ex.status) from None
        e, sem, d = self._explorer_target(key)
        if not preview:
            self._risky_gate(confirm_text, undo=bool(undo_of))
        lock = self._lock_for(key)
        if not lock.acquire(blocking=False):
            raise SettingsError("another change on this device is still running", 409)
        if not preview and not self._risky_lock.acquire(blocking=False):
            lock.release()
            raise SettingsError("another risky job is running on this site; one at a time", 409)
        dev = "recorder" if e.get("_recorder") else "camera"
        try:
            with sem:
                cl = self._admin_client(d)
                cur = self._explore_get(cl, path)
                shown_now = serialize(hikexplorer.redact(cur)).decode("utf-8", "replace")
                if expect_now is not None and shown_now != expect_now:
                    raise SettingsError(f"the {dev} shows something else now than after that change — not undone", 409,
                                        {"conflict": [path]})
                doc, changes = hikexplorer.merge(cur, str(body or ""))
                if preview or not changes:
                    return {"ok": True, "preview": True, "changes": changes,
                            "message": "" if changes else f"Nothing to change — the {dev} already has these values."}
                r = cl.request("PUT", path, data=serialize(doc), headers={"Content-Type": "application/xml"},
                               max_bytes=64 << 10)
                ok, reboot, msg = _status(r)
                if not ok:
                    raise SettingsError(f"the {dev} refused the change: {msg}", 502)
                back = hikexplorer.leaves(self._explore_get(cl, path))
        except hikexplorer.ExplorerError as ex:
            raise SettingsError(str(ex), ex.status) from None
        except hikisapi.LoginRefused as ex:
            raise SettingsError(str(ex) or "the device refused the saved login", 403) from None
        except (hikisapi.Unreachable, hikisapi.TooBig) as ex:
            raise SettingsError(f"the {dev} did not answer ({str(ex) or ex.__class__.__name__})", 502) from None
        finally:
            if not preview:
                self._risky_lock.release()
            lock.release()
        rows, kept_all = [], True
        for c in changes:
            got = (back.get(c["path"]).text or "").strip() if back.get(c["path"]) is not None else None
            kept = got == c["after"]
            kept_all = kept_all and kept
            rows.append({"id": c["path"], "label": c["path"].split("/", 2)[-1], "before": c["before"], "after": c["after"],
                         "shown_before": c["before"], "shown_after": c["after"], "now": got, "written": True, "kept": kept})
        after_txt = serialize(hikexplorer.redact(doc)).decode("utf-8", "replace")
        undo = ({"path": path, "doc": shown_now, "after": after_txt}
                if len(shown_now) + len(after_txt) <= 128 << 10 else None)
        entry = self._log_job(e, d, "explorer", f"ISAPI explorer: {path}", rows, who, undo=undo, undo_of=undo_of,
                              result="ok" if kept_all else "partial")
        msg = (f"Done — the {dev} kept every change." if kept_all else f"The {dev} did not keep every change — see the list.")
        if reboot:
            msg += f" The {dev} says it needs a restart for this to take effect."
        return {"ok": kept_all, "change": entry, "message": msg, "reboot": reboot}

    def changes(self, key, limit=50):
        return self.log.for_key(key, limit)

    def undo(self, key, cid, who=""):
        c = self.log.get(cid)
        if not c or c.get("key") != key:
            raise SettingsError("unknown change", 404)
        if c.get("undone_by"):
            raise SettingsError("this change was already undone", 409)
        if c.get("section") == "explorer":
            u = c.get("undo")
            if not u:
                raise SettingsError("this change was too big to keep for Undo", 409)
            return self.explore_write(key, u["path"], u["doc"], who=who, undo_of=cid, expect_now=u["after"])
        if c.get("section") in ("channels", "password"):
            u = c.get("undo")
            if not u:
                raise SettingsError("this change can't be undone", 409)
            return self.channel_do(key, u["action"], ch=u.get("ch"), ip=u.get("ip"), port=u.get("port"),
                                   who=who, undo_of=cid, track=u.get("track"))
        if c.get("section") == "account":
            if c.get("no_undo"):
                raise SettingsError("a removal can't be undone — set the account up again", 409)
            return self.account(key, "remove", who=who, undo_of=cid)
        rows = [r for r in c.get("fields") or [] if r.get("written")]
        if not rows:
            raise SettingsError("nothing of this change reached the camera", 409)
        return self.apply(key, c["lens"], c["section"], {r["id"]: r["before"] for r in rows}, who=who,
                          expect={r["id"]: r["after"] for r in rows}, undo_of=cid, confirm=True,
                          route=c.get("route") or "direct")


HIK_NS = "http://www.hikvision.com/ver20/XMLSchema"
PTZ_MAX_MS = 1500
PTZ_LOG_GAP = 600
PTZ_AXES = (("pan", "ContinuousPanTiltSpace/XRange"), ("tilt", "ContinuousPanTiltSpace/YRange"),
            ("zoom", "ContinuousZoomSpace/ZRange"))
PTZ_MODEL = re.compile(r"^I?DS-2(DE|DF|DY|SE|SF)", re.I)


def _ptz_base(use, lens, ch):
    return f"/ISAPI/ContentMgmt/PTZCtrlProxy/channels/{int(ch)}" if use == "nvr" else f"/ISAPI/PTZCtrl/channels/{int(lens)}"


def _pretty(root):
    """Indented for reading and editing (the write starts from the device's own document,
    and only leaf values are compared, so the indentation never reaches the device)."""
    r = copy.deepcopy(root)
    ET.indent(r, "  ")
    return serialize(r).decode("utf-8", "replace")


class _Missing(Exception):
    """This camera doesn't have the section's documents: leave the tab out."""


class _Helpers:
    walk = staticmethod(_walk)
    val = staticmethod(_val)
    child = staticmethod(_child)
    serialize = staticmethod(serialize)
    status = staticmethod(_status)


_HELP = _Helpers()

settings = Settings()
