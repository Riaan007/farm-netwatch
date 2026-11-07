"""A recorder's own alarms: what it does when something goes wrong with IT (not with a
camera's picture) — a disk fills up or fails, its network cable is pulled, an address
clashes, someone keeps trying wrong passwords, a recording fails.

Each is an "exception trigger" (/ISAPI/Event/triggers/<event>) holding a list of
actions; the ones Netwatch offers are the recorder's buzzer ("beep"), telling the app
and any platform watching it ("center" — Hik-Connect, iVMS) and email ("email" — only
of use once the recorder has a mail server set up). /ISAPI/Event/triggersCap says which
actions each event takes on this recorder; an action it doesn't take is never offered.

Seen on the home DS-7732NXI-I4/S V5.04.066 (2026-11-07): "disk full" and "network cable
unplugged" did NOTHING, "disk error" beeped and told the app. Writing a trigger replaces
its whole action list, so actions Netwatch doesn't show (an alarm output, …) are kept as
they are. hiksettings.py does the locks, History and Undo; this file only reads and
builds the documents.
"""
import copy
import re

import hikisapi

# event id, label, why it matters, the capability block's name in triggersCap
EVENTS = [
    ("diskfull", "Disk full", "Only matters when the recorder is set to stop (not overwrite) when full.", "DiskfullTriggerCap"),
    ("diskerror", "Disk error", "A failing disk loses footage — the first sign is this.", "DiskerrorTriggerCap"),
    ("nicbroken", "Network cable unplugged", "Cameras drop off the recorder all at once.", "NicbrokenTriggerCap"),
    ("ipconflict", "IP address clash", "Another device took the recorder's address.", "IpconflictTriggerCap"),
    ("illaccess", "Wrong passwords tried", "Someone (or something) keeps guessing its login.", "IllaccesTriggerCap"),
    # the home V5.04.066 calls this block RecordExceptionTriggerCap (seen 2026-11-07)
    ("recordingfailure", "Recording failed", "A camera's footage is not being kept.", "RecordExceptionTriggerCap|RecordingfailureTriggerCap"),
]
EVENT_IDS = [e[0] for e in EVENTS]
METHODS = [("beep", "Beep", "isSupportBeep"), ("center", "Tell the app", "isSupportCenter"),
           ("email", "Email", "isSupportEmail")]
METHOD_IDS = [m[0] for m in METHODS]
MAX_DOC = 256 << 10
CAP_DOC = 2 << 20


def _ns(root):
    return root.tag[1:].split("}", 1)[0] if root is not None and root.tag.startswith("{") else ""


def _q(root, name):
    ns = _ns(root)
    return f"{{{ns}}}{name}" if ns else name


def caps(cl):
    """{event: set of method ids it takes} from triggersCap; None when unreadable (then
    every method is offered and the recorder is left to refuse)."""
    try:
        r = cl.get("/ISAPI/Event/triggersCap", max_bytes=CAP_DOC)
    except (hikisapi.Unreachable, hikisapi.TooBig):
        return None
    if r.status != 200:
        return None
    text = r.content.decode("utf-8", "replace")
    out = {}
    for ev, _l, _w, blocks in EVENTS:
        m = next((x for x in (re.search(rf"<{b}\b[^>]*>(.*?)</{b}>", text, re.S) for b in blocks.split("|")) if x), None)
        if not m:
            continue
        out[ev] = {mid for mid, _ml, tag in METHODS if re.search(rf"<{tag}>\s*true\s*</{tag}>", m.group(1))}
    return out


def methods_of(doc):
    """The action ids in a trigger document, in order."""
    out = []
    for el in doc.iter():
        if hikisapi.tag(el) == "notificationMethod" and (el.text or "").strip():
            out.append(el.text.strip())
    return out


def read(cl):
    """[{id, label, why, methods: [...], can: [...]}] for the events this recorder has."""
    cp = caps(cl)
    out = []
    for ev, label, why, _b in EVENTS:
        r = cl.get(f"/ISAPI/Event/triggers/{ev}", max_bytes=MAX_DOC)
        doc = hikisapi.parse(r.content) if r.status == 200 else None
        if doc is None:
            continue
        can = [m for m in METHOD_IDS if cp is None or m in cp.get(ev, set())] if (cp is None or ev in cp) else []
        if not can:
            continue
        out.append({"id": ev, "label": label, "why": why, "methods": methods_of(doc), "can": can})
    return out


def build(doc, want):
    """The trigger document with Netwatch's actions set to `want` (a list of method ids);
    any other action in it (alarm output, …) is kept."""
    new = copy.deepcopy(doc)
    lst = next((el for el in new.iter() if hikisapi.tag(el) == "EventTriggerNotificationList"), None)
    if lst is None:
        lst = new.makeelement(_q(new, "EventTriggerNotificationList"), {})
        new.append(lst)
    keep = [n for n in list(lst) if hikisapi.tag(n) == "EventTriggerNotification"
            and (hikisapi.child_text(n, "notificationMethod") or "") not in METHOD_IDS]
    for n in list(lst):
        lst.remove(n)
    for n in keep:
        lst.append(n)
    for m in want:
        n = lst.makeelement(_q(new, "EventTriggerNotification"), {})
        for tag, val in (("id", m), ("notificationMethod", m)):     # exactly what the recorder writes itself
            c = n.makeelement(_q(new, tag), {})
            c.text = val
            n.append(c)
        lst.append(n)
    return new
