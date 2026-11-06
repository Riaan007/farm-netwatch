"""The ISAPI explorer (Phase 4d, part 3): read any device document, change its VALUES.

For the settings no panel covers. It is risky work (see hiksecurity.py: "Risky camera
work", the typed word, one job at a time), and narrower than "anything":

  * XML documents under /ISAPI/ only, no query string, at most MAX_DOC;
  * paths with their own, safer flow — or that can't be undone — are refused: restart,
    factory reset, firmware, configuration import/export, disk format, users and
    rights, ports, the network address, recorder channels (BLOCKED);
  * a change may only change values: the edited document must have exactly the same
    elements, in the same order, with the same attributes, as the device's own. Nothing
    is added or removed (a missing element is how a Hikvision write resets settings);
  * secrets — passwords, keys, codes — are never sent to the page: they read as
    SECRET, and a SECRET written back is replaced by what the device has. A changed
    secret is refused (passwords go through the password job);
  * the change list is computed here and shown before anything is written; the device
    is read back afterwards; the log keeps the redacted before and after, and Undo
    writes the before back only if the device still shows the after.
"""
import copy
import re

import hikisapi

SECRET = "••••••"
MAX_DOC = 256 << 10
PATH_RE = re.compile(r"^/ISAPI(/[A-Za-z0-9_.-]{1,64}){1,8}$")
SECRET_TAG = re.compile(r"(pass(word|wd)?|pwd|secret|token|psk|verificationcode|salt|privatekey|"
                        r"(shared|encrypt|encryption|wpa|aes|auth|api|access)key|^key)$", re.I)
# (pattern, why, may it be READ?) — actions are never even read; the rest can be looked at
BLOCKED = [
    (re.compile(r"/ISAPI/System/(reboot|factoryReset|updateFirmware|upgradeStatus|configurationData|shutdown)", re.I),
     "restart, reset, firmware and configuration files are not done from the explorer", False),
    (re.compile(r"/ISAPI/ContentMgmt/Storage/hdd(/[^/]+)?/format|/format$", re.I),
     "formatting a disk is not done from the explorer", False),
    (re.compile(r"/ISAPI/Security/(challenge|sessionLogin|userCheck)", re.I), "not a settings document", False),
    (re.compile(r"/ISAPI/Security/(users|UserPermission|adminAccesses)", re.I),
     "users, rights and ports are changed in Logins and security", True),
    (re.compile(r"/ISAPI/System/Network/interfaces", re.I), "the network address has its own Change IP flow", True),
    (re.compile(r"/ISAPI/ContentMgmt/InputProxy/channels(/\d+)?$", re.I),
     "recorder channels are changed in Cameras on this recorder", True),
]
COMMON = ["/ISAPI/System/deviceInfo", "/ISAPI/System/time", "/ISAPI/System/time/ntpServers",
          "/ISAPI/Image/channels/1", "/ISAPI/System/Video/inputs/channels/1/overlays",
          "/ISAPI/Streaming/channels/101", "/ISAPI/Streaming/channels/102", "/ISAPI/Event/triggers",
          "/ISAPI/System/Network/UPnP", "/ISAPI/System/Network/EZVIZ", "/ISAPI/Security/illegalLoginLock",
          "/ISAPI/ContentMgmt/Storage", "/ISAPI/ContentMgmt/record/tracks", "/ISAPI/System/IO/inputs",
          "/ISAPI/System/IO/outputs", "/ISAPI/System/Audio/channels", "/ISAPI/System/logServer"]


class ExplorerError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


def check_path(path, write=False):
    path = str(path or "").strip()
    if not PATH_RE.match(path):
        raise ExplorerError("a path looks like /ISAPI/System/time (letters, digits, - _ . only, no ?query)")
    for rx, why, read_ok in BLOCKED:
        if rx.search(path) and (write or not read_ok):
            raise ExplorerError(why, 403)
    return path


def _leaf(el):
    return len(el) == 0


def _secret(el):
    return _leaf(el) and bool(SECRET_TAG.search(hikisapi.tag(el))) and bool((el.text or "").strip())


def redact(root):
    """A copy with every secret value replaced by SECRET."""
    r = copy.deepcopy(root)
    for el in r.iter():
        if _secret(el):
            el.text = SECRET
    return r


def _shape(el, out):
    """[(tag, attributes, children count)] in document order: what must not change."""
    out.append((el.tag, tuple(sorted(el.attrib.items())), len(el)))
    for c in el:
        _shape(c, out)
    return out


def _leaves_into(el, me, out):
    count, idx = {}, {}
    for c in el:
        count[hikisapi.tag(c)] = count.get(hikisapi.tag(c), 0) + 1
    for c in el:
        t = hikisapi.tag(c)
        idx[t] = idx.get(t, 0) + 1
        name = f"{t}[{idx[t]}]" if count[t] > 1 else t
        if _leaf(c):
            out[f"{me}/{name}"] = c
        else:
            _leaves_into(c, f"{me}/{name}", out)


def leaves(root):
    out = {}
    if _leaf(root):
        out["/" + hikisapi.tag(root)] = root
    else:
        _leaves_into(root, "/" + hikisapi.tag(root), out)
    return out


def merge(current, edited_text):
    """The document to write: the device's own `current`, with the values changed in
    `edited_text` (the page's edit of the REDACTED copy). Returns (document, changes)
    where changes = [{path, before, after}] with secrets shown as SECRET."""
    if len(edited_text.encode()) > MAX_DOC:
        raise ExplorerError("that document is too big to write from here")
    new = hikisapi.parse(edited_text.encode() if isinstance(edited_text, str) else edited_text)
    if new is None:
        raise ExplorerError("that is not a document the device would read (XML, no DOCTYPE)")
    if _shape(new, []) != _shape(current, []):
        raise ExplorerError("only values may change: the document must keep every element, in the same order, "
                            "with the same attributes", 409)
    out = copy.deepcopy(current)
    have, want, now = leaves(out), leaves(new), leaves(current)
    changes = []
    for p, el in have.items():
        nv = (want[p].text or "").strip()
        ov = (now[p].text or "").strip()
        if _secret(now[p]):
            if nv != SECRET:
                raise ExplorerError(f"{p}: secrets can't be changed here (passwords go through the password job)", 403)
            continue
        if nv != ov:
            if len(nv) > 512 or any(ord(ch) < 32 for ch in nv):
                raise ExplorerError(f"{p}: that value is too long or has control characters")
            el.text = nv
            changes.append({"path": p, "before": ov, "after": nv})
    return out, changes


def text_of(root, serialize):
    return serialize(root).decode("utf-8", "replace")
