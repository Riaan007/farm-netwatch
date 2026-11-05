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


def recorder_sections():
    return [
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
