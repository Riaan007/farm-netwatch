"""Camera detection and deterrence settings (Phase 4a) — the section specs only.

hiksettings.py does all the reading, checking, writing, read-back, logging and Undo;
this file only says which documents make up each section and which settings in them
are offered. It imports nothing from hiksettings (that module imports this one).

Every section here is "careful": the change list is shown and confirmed first.
Areas and lines are drawn on the camera's picture; the camera's own coordinates
(its normalizedScreenSize, origin bottom-left) are converted in hiksettings.

A camera that does not have a document (403/404) simply doesn't get that tab.
"""

TARGET = {"all": "Anything", "human": "People", "vehicle": "Vehicles"}
DIRECTION = {"any": "Both ways (A ↔ B)", "left-right": "Only A → B", "right-left": "Only B → A"}
FREQ = {"high": "Fast flashing", "medium": "Medium flashing", "low": "Slow flashing", "normallyOn": "Stays on"}

LINK_HELP = ("What the camera does when this goes off. \"Tell the recorder\" lets the NVR and "
             "Netwatch see the alarm; the siren and strobe use the Strobe and siren tab's settings.")


def _ids(H, lst):
    """The ids of the items (regions, lines) in a list element, in order."""
    out = []
    for c in (lst if lst is not None else []):
        i = H.val(H.child(c, "id"))
        if i and i.isdigit():
            out.append(i)
    return out


def _region_fields(H, docs, caps, dk, list_path, item_tag, noun, coords, shape):
    doc = docs.get(dk)
    if doc is None:
        return []
    lst = H.walk(doc, list_path)
    out = []
    for i in _ids(H, lst):
        base = f"{dk}.{list_path}/{item_tag}[id={i}]"
        on = (base + "/enabled", ["true"])
        out += [
            (base + "/enabled", f"{noun} {i}", "", {"kind": "bool"}),
            (base + "/" + shape, f"{noun} {i}: where", "Draw it on the picture.",
             {"kind": shape, "coords": coords, "show_if": on}),
            (base + "/directionSensitivity", f"{noun} {i}: direction", "A is where you started the line.",
             {"labels": DIRECTION, "show_if": on}),
            (base + "/sensitivityLevel", f"{noun} {i}: sensitivity", "", {"show_if": on}),
            (base + "/timeThreshold", f"{noun} {i}: seconds inside before it alarms", "", {"show_if": on}),
            (base + "/detectionTarget", f"{noun} {i}: what to look for", "", {"labels": TARGET, "show_if": on}),
        ]
    return out


def _gen_intrusion(H, docs, caps):
    return _region_fields(H, docs, caps, "fd", "FieldDetectionRegionList", "FieldDetectionRegion", "Area",
                          ("RegionCoordinatesList", "RegionCoordinates"), "area")


def _gen_line(H, docs, caps):
    return _region_fields(H, docs, caps, "ld", "LineItemList", "LineItem", "Line",
                          ("CoordinatesList", "Coordinates"), "line")


def _links(event_cap):
    return ("tr.links", "When it goes off", LINK_HELP, {"kind": "links", "cap": event_cap})


def sections(n):
    n = int(n)
    return [
        {"id": "motion", "label": "Motion", "risk": "careful", "optional": "md",
         "docs": {"md": f"/ISAPI/System/Video/inputs/channels/{n}/motionDetection",
                  "tr": f"/ISAPI/Event/triggers/VMD-{n}", "tc": "/ISAPI/Event/triggersCap"},
         "nocaps": ["tr", "tc"], "readonly": ["tc"],
         "fields": [
             ("md.enabled", "Motion detection", "", None),
             ("md.MotionDetectionLayout/sensitivityLevel", "Sensitivity", "", None),
             ("md.MotionDetectionLayout/targetType", "Only alarm on",
              "Ticking people and/or vehicles ignores animals, moving leaves and shadows. None ticked = anything that moves.",
              {"kind": "set", "labels": TARGET}),
             ("md.MotionDetectionLayout/layout/gridMap", "Where", "Tap or drag over the picture to mark the squares that count.",
              {"kind": "grid"}),
             _links("MotionDetectionTriggerCap"),
         ],
         "note": "Plain motion detection. Intrusion and line crossing (their own tabs) are better at ignoring animals and shadows."},
        {"id": "intrusion", "label": "Intrusion", "risk": "careful", "optional": "fd",
         "docs": {"fd": f"/ISAPI/Smart/FieldDetection/{n}",
                  "tr": f"/ISAPI/Event/triggers/fielddetection-{n}", "tc": "/ISAPI/Event/triggersCap"},
         "nocaps": ["tr", "tc"], "readonly": ["tc"],
         "fields": [("fd.enabled", "Intrusion detection", "Alarms when someone stays inside an area.", None)],
         "gen": _gen_intrusion, "tail": [_links("FiledDetectTriggerCap")]},
        {"id": "line", "label": "Line crossing", "risk": "careful", "optional": "ld",
         "docs": {"ld": f"/ISAPI/Smart/LineDetection/{n}",
                  "tr": f"/ISAPI/Event/triggers/linedetection-{n}", "tc": "/ISAPI/Event/triggersCap"},
         "nocaps": ["tr", "tc"], "readonly": ["tc"],
         "fields": [("ld.enabled", "Line crossing detection", "Alarms when someone crosses a line.", None)],
         "gen": _gen_line, "tail": [_links("LineDetectTriggerCap")]},
        {"id": "tamper", "label": "Tampering", "risk": "careful", "optional": "td|sc|df",
         "docs": {"td": f"/ISAPI/System/Video/inputs/channels/{n}/tamperDetection",
                  "sc": f"/ISAPI/Smart/SceneChangeDetection/{n}",
                  "df": f"/ISAPI/Smart/DefocusDetection/{n}",
                  "tr": f"/ISAPI/Event/triggers/tamper-{n}", "tc": "/ISAPI/Event/triggersCap"},
         "nocaps": ["tr", "tc"], "readonly": ["tc"],
         "fields": [
             ("td.enabled", "Covered lens (video tampering)", "Alarms when the lens is covered or sprayed.", {"kind": "bool"}),
             ("td.TamperDetectionRegionList/TamperDetectionRegion[id=1]/enabled", "Watch the whole picture for it", "",
              {"kind": "bool", "show_if": ("td.enabled", ["true"])}),
             ("td.TamperDetectionRegionList/TamperDetectionRegion[id=1]/sensitivityLevel", "Covered lens: sensitivity", "",
              {"kind": "int", "min": 0, "max": 100, "show_if": ("td.enabled", ["true"])}),
             ("sc.enabled", "Camera moved (scene change)", "Alarms when the camera is turned away.", {"kind": "bool"}),
             ("sc.sensitivityLevel", "Camera moved: sensitivity", "", {"kind": "int", "min": 1, "max": 100,
                                                                         "show_if": ("sc.enabled", ["true"])}),
             ("df.enabled", "Out of focus", "", {"kind": "bool"}),
             ("df.sensitivityLevel", "Out of focus: sensitivity", "", {"kind": "int", "min": 1, "max": 100,
                                                                         "show_if": ("df.enabled", ["true"])}),
             ("tr.links", "Covered lens: when it goes off", LINK_HELP, {"kind": "links", "cap": "TamperDetectionTriggerCap"}),
         ]},
        {"id": "masks", "label": "Privacy masks", "risk": "careful", "optional": "pm",
         "docs": {"pm": f"/ISAPI/System/Video/inputs/channels/{n}/privacyMask"},
         "fields": [
             ("pm.enabled", "Privacy masks", "Black boxes over parts of the picture, such as a neighbour's yard. "
                                             "What is masked is not recorded either.", {"kind": "bool"}),
             ("pm.PrivacyMaskRegionList", "Masked areas", "Drag on the picture to add a box; tap a box to remove it.",
              {"kind": "rects", "show_if": ("pm.enabled", ["true"])}),
         ]},
        {"id": "deter", "label": "Strobe and siren", "risk": "careful", "optional": "wl|au", "device": True,
         "docs": {"wl": f"/ISAPI/Event/triggers/notifications/channels/{n}/whiteLightAlarm?format=json",
                  "au": "/ISAPI/Event/triggers/notifications/AudioAlarm?format=json"},
         "fields": [
             ("wl.frequency", "Strobe", "How the white light flashes when an alarm asks for it.", {"labels": FREQ}),
             ("wl.durationTime", "Strobe for (seconds)", "", None),
             ("au.audioID", "Siren sound", "", {"kind": "jopts", "list": "audioTypeListCap", "v": "audioID",
                                                 "label": "audioDescription"}),
             ("au.audioVolume", "Siren volume", "", None),
             ("au.alarmTimes", "Play it (times)", "", None),
         ],
         "note": "These only sound or flash for an alarm whose \"When it goes off\" includes the siren or strobe. "
                 "The times of day they may run are set on the camera's own page."},
    ]


def nvr_sections(ch):
    """Through the recorder: the camera's own motion detection and privacy masks, via
    the recorder's proxy documents. What the RECORDER does on an alarm, and the
    recorder's own smart detection, are recorder settings (a later part)."""
    ch = int(ch)
    note = "Changed through the recorder, which passes it on to the camera. What the recorder does on an alarm is set on the recorder."
    out = []
    for s in sections(1):
        if s["id"] == "motion":
            out.append({**s, "docs": {"md": f"/ISAPI/ContentMgmt/InputProxy/channels/{ch}/motionDetection"},
                        "nocaps": [], "readonly": [], "note": note})
        elif s["id"] == "masks":
            out.append({**s, "docs": {"pm": f"/ISAPI/ContentMgmt/InputProxy/channels/{ch}/privacyMask"},
                        "note": "Changed through the recorder, which passes it on to the camera."})
    return out
