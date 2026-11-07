"""Recorder settings (Phase 4c) — the section specs only.

Two places use them:
  * a camera's "Recording" tab — the recorder's own record of that camera (its channel's
    recording track): whether and when it records, how much before and after an event
    it keeps, which stream it records, how many days it keeps, sound;
  * the recorder's own "Recorder settings" — what it does when its disks are full.

hiksettings.py does all the reading, checking, writing, read-back, logging and Undo
(and the week-schedule kind). This file imports nothing from hiksettings.

Every section is "careful": a wrong schedule or "stop when full" loses footage, so the
change list is shown and confirmed first. Channel editing (a camera's address and login
on the recorder), disk quotas, disk groups and formatting are NOT here: they can lose
footage or cameras in one step (Phase 4d / 6).
"""

# The recorder's names for its recording types -> (one-letter code in the week value, label).
# The week value is 7 days (Monday first) of 24 hourly codes joined by "|"; "-" = not recorded.
MODES = [("CMR", "C", "Continuous"), ("MOTION", "M", "Motion"), ("ALARM", "A", "Alarm input"),
         ("ALARMANDMOTION", "B", "Motion and alarm"), ("MOTIONORALARM", "O", "Motion or alarm"),
         ("AllEvent", "E", "Any event"), ("EDR", "D", "Event-dependent"), ("POS", "P", "Till (POS)")]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MAX_PER_DAY = 8           # Hikvision recorders take up to 8 time periods a day

PRE = ["0", "5", "10", "15", "20", "25", "30"]
POST = ["5", "10", "30", "60", "120", "300", "600"]
# which stream a track records, by the stream number's last two digits
STREAM = {"01": "Main stream", "02": "Sub stream", "03": "Third stream", "07": "Dual stream (main and sub)"}

X = "CustomExtensionList/CustomExtension/"


def camera_sections(ch):
    ch = int(ch)
    on = ("t." + X + "enableSchedule", ["true"])
    return [
        {"id": "record", "label": "Recording", "risk": "careful", "optional": "t",
         "docs": {"t": f"/ISAPI/ContentMgmt/record/tracks/{ch}01"},
         "fields": [
             ("t." + X + "enableSchedule", "Record this camera",
              "Off = the recorder does not record this camera at all.", {"kind": "bool"}),
             ("t.TrackSchedule/ScheduleBlockList/ScheduleBlock", "When it records",
              "Pick a kind of recording, then tap or drag over the hours. Motion and alarm recording "
              "need that detection switched on (the Detection panel).", {"kind": "week", "show_if": on}),
             ("t." + X + "PreRecordTimeSeconds", "Keep from before an event (seconds)",
              "For motion and alarm recording: how much from just before it went off is kept.",
              {"kind": "choices", "opts": PRE, "unit": "s", "show_if": on}),
             ("t." + X + "PostRecordTimeSeconds", "Keep recording after an event (seconds)", "",
              {"kind": "choices", "opts": POST, "unit": "s", "show_if": on}),
             ("t.SrcDescriptor/SrcUrl", "Records", "The main stream is the sharp one. The sub stream saves disk "
              "space but is not good enough to recognise a face or a number plate.", {"kind": "srcurl", "show_if": on}),
             ("t.Duration", "Delete footage older than (days)",
              "0 = keep it until the disks are full, then the oldest is overwritten. A number deletes older "
              "footage even when there is space.", {"kind": "isodays"}),
             ("t." + X + "SaveAudio", "Record sound", "Only if the camera has a microphone. Recording "
              "people talking may need their consent.", {"kind": "bool"}),
         ],
         "note": "Set on the recorder, for this camera's channel. The holiday schedule is left as it is."},
    ]


def track_section(ch):
    """Channel `ch`'s recording track as a section of the RECORDER itself (id "track<ch>"):
    the recorder page reads and changes every channel's recording on that recorder. A
    camera's own Recording tab uses camera_sections through its main recorder."""
    sec = dict(camera_sections(ch)[0])
    sec.update(id=f"track{int(ch)}", label=f"Recording — channel {int(ch)}")
    return sec


def week_summary(week):
    """A week value ("7 days x 24 codes") in a few words: {kind, text}."""
    rows = (week or "").split("|")
    if week is None or len(rows) != 7 or any(len(r) != 24 for r in rows):
        return {"kind": "custom", "text": "Set times (shown on the recorder)"}
    cells = "".join(rows)
    names = {k: lbl for _m, k, lbl in MODES}
    used = {c for c in cells if c != "-"}
    if not used:
        return {"kind": "off", "text": "Nothing scheduled"}
    if len(used) == 1 and "-" not in cells:
        k = used.pop()
        return {"kind": {"C": "continuous", "M": "motion"}.get(k, "event"), "text": f"{names.get(k, k)}, all week"}
    share = {k: cells.count(k) * 100 // len(cells) for k in used}
    top = sorted(share.items(), key=lambda x: -x[1])
    return {"kind": "mixed", "text": ", ".join(f"{names.get(k, k).lower()} {v} %" for k, v in top)
            + (f", off {cells.count('-') * 100 // len(cells)} %" if "-" in cells else "")}


def week_all(code):
    """The whole week in one recording type ("C" continuous, "M" motion, "-" nothing)."""
    return "|".join([code * 24] * 7)


def stream_of(src):
    """'main' / 'sub' / 'third' / 'dual' from a track's SrcUrl (…/channels/<n>01)."""
    import re
    m = re.search(r"(\d{3,})\s*$", src or "")
    return {"01": "main", "02": "sub", "03": "third", "07": "dual"}.get(m.group(1)[-2:], "") if m else ""


def stream_url(src, which):
    """The same SrcUrl pointed at another stream of the same channel."""
    import re
    end = {"main": "01", "sub": "02"}[which]
    return re.sub(r"(\d+?)(\d\d)(\s*)$", lambda m: m.group(1) + end + m.group(3), src or "", count=1)


def recorder_sections():
    return [
        {"id": "name", "label": "Name", "risk": "safe", "docs": {"d": "/ISAPI/System/deviceInfo"},
         "fields": [
             ("d.deviceName", "Recorder name", "Shown in Netwatch and on the recorder's own screen — "
              "e.g. 'Office NVR'. Most are still called by their model or 'AcuSense'.", {"kind": "text", "maxlen": 32}),
         ]},
        {"id": "storage", "label": "Disks", "risk": "careful", "optional": "x",
         "docs": {"x": "/ISAPI/ContentMgmt/Storage/extension"},
         "fields": [
             ("x.LoopEnable", "Overwrite the oldest footage when the disks are full",
              "Off = the recorder STOPS recording when the disks are full. Leave it on.", {"kind": "bool"}),
             ("x.enableDormant", "Let idle disks sleep",
              "Saves power and wear on disks that are not being written to.", {"kind": "bool"}),
         ],
         "note": "Disk groups, quotas and formatting are not changed from Netwatch."},
    ]
