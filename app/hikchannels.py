"""Recorder channels and the camera password job (Phase 4d, part 2).

Both are "dangerous" (see hiksecurity.py for the gates: "Risky camera work", the typed
word, one job at a time on the site).

Recorder channels (/ISAPI/ContentMgmt/InputProxy/channels): which camera each channel
records, with the login the recorder uses for it. Netwatch can
  * "fix login": write the camera's admin login that Netwatch has saved into the
    channel (the recorder says "password error" after someone changed it on the camera);
  * change a channel's address and port (after a camera moved to another IP);
  * add a camera Netwatch knows to a free channel, and remove a channel.
The password a channel gets ALWAYS comes from Netwatch's saved login for that camera:
nobody types it, it is never shown or logged, and a channel is never written without
it (a channel written without its password could be left with none). A camera with no
saved admin login can't be added, fixed or moved — only removed.

The password job changes a camera's admin password and then every recorder channel
that logs in to it with that user, so no recorder loses the camera. While the
recorder still knows only the old password it keeps knocking, and the camera's
login lock (7 tries, 30 minutes — on newer firmware the lock is by ADDRESS, so it
would lock the recorder out) would trip: the job turns the lock off first and puts
it back at the end. The new password is saved in Netwatch the moment the camera
accepts it. A channel that doesn't come back is reported, and "fix login" on the
recorder repairs it from the saved login.
"""
import copy
import string
import time

import creds
import hikisapi
from hiksecurity import ACCOUNT, RAW_OK, AccountError, _get, _ns, _set_child, _write, users

LIST = "/ISAPI/ContentMgmt/InputProxy/channels"
LOCK = "/ISAPI/Security/illegalLoginLock"
# filled in by the recorder, refused (or ignored) when written back
READ_ONLY = ("model", "serialNumber", "firmwareVersion", "devIndex", "twoWayAudioChannelIDList",
             "isSupportChannelDeviceInfo")
PW_MIN, PW_MAX = 8, 16
WAIT_S = 60
SETTLE_S = 8


class JobError(AccountError):
    pass


# ---- logins Netwatch knows ------------------------------------------------------------
def admin_login(key):
    """(username, password) of the camera's ADMIN login that Netwatch has: the admin slot
    when Netwatch uses its own account, else the saved login (never the netwatch one)."""
    a = creds.get_admin(key)
    if a["username"]:
        return a["username"], a["password"]
    s = creds.get(key)
    if s["username"] and s["username"] != ACCOUNT and s["password"]:
        return s["username"], s["password"]
    return None


def check_password(new, again, user, old):
    """The rules a Hikvision device and a recorder channel both accept, plus Netwatch's
    own: only characters that can travel in a delete's address unencoded."""
    if new != again:
        raise JobError("The two new passwords are not the same.", 400)
    if not PW_MIN <= len(new) <= PW_MAX:
        raise JobError(f"The new password must be {PW_MIN}–{PW_MAX} characters.", 400)
    if not set(new) <= RAW_OK:
        raise JobError("Use letters, digits and these symbols only: - . _ ~ ! $ ' ( ) * , / : ; @ [ ]", 400)
    kinds = sum(any(c in pool for c in new) for pool in
                (string.ascii_lowercase, string.ascii_uppercase, string.digits, "-._~!$'()*,/:;@[]"))
    if kinds < 2:
        raise JobError("Mix at least two of: small letters, capitals, digits, symbols.", 400)
    if user and user.lower() in new.lower():
        raise JobError("The password may not contain the user name.", 400)
    if new == old:
        raise JobError("That is the password the camera has now.", 400)


# ---- channels -------------------------------------------------------------------------
def _txt(el, *path):
    for p in path:
        el = next((c for c in (el if el is not None else ()) if hikisapi.tag(c) == p), None)
    return (el.text or "").strip() if el is not None and el.text else ""


def listing(cl):
    """The recorder's channels: what it records and whether it is connected."""
    root = _get(cl, LIST, max_bytes=1 << 20)
    stat = _get(cl, LIST + "/status", max_bytes=1 << 20)
    st = {}
    for s in (stat.iter() if stat is not None else ()):
        if hikisapi.tag(s) == "InputProxyChannelStatus":
            st[_txt(s, "id")] = (_txt(s, "online") == "true", _txt(s, "chanDetectResult"))
    out = []
    for c in (root.iter() if root is not None else ()):
        if hikisapi.tag(c) != "InputProxyChannel":
            continue
        cid = _txt(c, "id")
        if not cid.isdigit():
            continue
        on, why = st.get(cid, (None, ""))
        port = _txt(c, "sourceInputPortDescriptor", "managePortNo")
        out.append({"id": int(cid), "name": _txt(c, "name")[:64],
                    "ip": _txt(c, "sourceInputPortDescriptor", "ipAddress")[:64],
                    "port": int(port) if port.isdigit() else None,
                    "protocol": _txt(c, "sourceInputPortDescriptor", "proxyProtocol")[:20],
                    "user": _txt(c, "sourceInputPortDescriptor", "userName")[:32],
                    "input": _txt(c, "sourceInputPortDescriptor", "srcInputPort")[:4],
                    "model": _txt(c, "sourceInputPortDescriptor", "model")[:40],
                    "online": on, "state": why[:40]})
    out.sort(key=lambda x: x["id"])
    return out


def caps(cl):
    """Channel numbers the recorder takes (min, max)."""
    c = _get(cl, LIST + "/capabilities")
    el = hikisapi.find(c, "id") if c is not None else None
    try:
        return int(el.get("min")), int(el.get("max"))
    except (AttributeError, TypeError, ValueError):
        return 1, 64


def _doc(cl, ch):
    root = _get(cl, f"{LIST}/{int(ch)}")
    if root is None:
        raise JobError(f"The recorder did not give channel {ch}.", 502)
    for el in list(root.iter()):
        for c in list(el):
            if hikisapi.tag(c) in READ_ONLY:
                el.remove(c)
    return root


def _set_login(doc, user, pw, ip=None, port=None):
    """The channel's address and the login it uses; the password goes right after the
    user name (where the recorder expects it)."""
    ns = _ns(doc)
    src = next((c for c in doc if hikisapi.tag(c) == "sourceInputPortDescriptor"), None)
    if src is None:
        raise JobError("The recorder's channel document has no address part.", 502)
    if ip is not None:
        _set_child(src, "addressingFormatType", "ipaddress", ns)
        _set_child(src, "ipAddress", ip, ns)
    if port is not None:
        _set_child(src, "managePortNo", str(port), ns)
    name = _set_child(src, "userName", user, ns)
    for c in list(src):
        if hikisapi.tag(c) == "password":
            src.remove(c)
    pw_el = src.makeelement(f"{{{ns}}}password" if ns else "password", {})
    pw_el.text = pw
    src.insert(list(src).index(name) + 1, pw_el)
    return doc


def wait_online(cl, ch, timeout=None, sleep=time.sleep):
    """True once the recorder has the channel connected again — after a settle time
    (right after a change a recorder still reports the OLD connection for a moment:
    seen live, "connected" and then "connecting"), and on two reads in a row."""
    end = time.monotonic() + (WAIT_S if timeout is None else timeout)
    if SETTLE_S:
        sleep(SETTLE_S)
    good = 0
    while True:
        s = _get(cl, f"{LIST}/{int(ch)}/status")
        on = s is not None and hikisapi.text(s, "online") == "true" and \
            hikisapi.text(s, "chanDetectResult") in ("", "connect")
        good = good + 1 if on else 0
        if good >= 2:
            return True
        if time.monotonic() >= end and not on:     # a good read always gets its confirming read
            return False
        sleep(3)


def put_channel(cl, H, ch, doc):
    ok, msg = _write(cl, H, "PUT", f"{LIST}/{int(ch)}", doc, None)
    if not ok:
        raise JobError(f"The recorder refused the channel change: {msg}", 502)


def new_channel(template, ch, name, ip, port, user, pw):
    """A channel document for a camera Netwatch knows, shaped like one the recorder has."""
    doc = copy.deepcopy(template)
    ns = _ns(doc)
    _set_child(doc, "id", str(ch), ns)
    _set_child(doc, "name", name[:32], ns)
    src = next((c for c in doc if hikisapi.tag(c) == "sourceInputPortDescriptor"), None)
    if src is not None:
        _set_child(src, "proxyProtocol", "HIKVISION", ns)
        _set_child(src, "srcInputPort", "1", ns)
        for c in list(src):
            if hikisapi.tag(c) in ("deviceID",):
                c.text = ""
    return _set_login(doc, user, pw, ip=ip, port=port)


def add_channel(cl, H, doc):
    ok, msg = _write(cl, H, "POST", LIST, doc, None)
    if not ok:
        raise JobError(f"The recorder refused the new channel: {msg}", 502)


def delete_channel(cl, H, ch, admin_pw):
    """Remove a channel. Plain first (no password, so nothing can count towards a lock);
    a recorder that wants its admin password again gets it once, unencoded."""
    r = cl.request("DELETE", f"{LIST}/{int(ch)}", max_bytes=64 << 10)
    ok, _rb, msg = H.status(r)
    if ok:
        return
    if admin_pw and set(admin_pw) <= RAW_OK and ("XML" in msg or "Parameter" in msg or "loginPassword" in msg):
        r = cl.request("DELETE", f"{LIST}/{int(ch)}?loginPassword={admin_pw}", max_bytes=64 << 10)
        ok, _rb, msg2 = H.status(r)
        if ok:
            return
        msg = msg2 or msg
    raise JobError(f"The recorder refused to remove channel {ch}: {msg}", 502)


# ---- the password job -----------------------------------------------------------------
def change_password(cl, d, H, new, again, recorders, client_for, sleep=time.sleep):
    """Change the camera's admin password and every recorder channel that uses it.
    `cl` = the camera's admin client; `recorders` = [(name, recorder admin client,
    [channel ids])]; `client_for(user, pw)` = a client with its own one-try slot.
    Returns the steps and per-channel results (no password anywhere)."""
    key = d["key"]
    login = admin_login(key)
    if login is None:
        raise JobError("Netwatch has no admin login for this camera.")
    user, old = login
    check_password(new, again, user, old)
    have = users(_get(cl, "/ISAPI/Security/users"))
    me = next((u for u in have if u["name"] == user), None)
    if me is None or me["level"] != "Admin":
        raise JobError("The saved admin login is not this camera's admin.")
    root = _get(cl, "/ISAPI/Security/users")
    template = next(u for u in root.iter() if hikisapi.tag(u) == "User" and hikisapi.child_text(u, "userName") == user)
    steps, lock_back = [], None

    lock = _get(cl, LOCK)
    if lock is not None and hikisapi.text(lock, "enabled") == "true":
        off = copy.deepcopy(lock)
        _set_child(off, "enabled", "false", _ns(off))
        ok, msg = _write(cl, H, "PUT", LOCK, off, None)
        if not ok:
            raise JobError(f"The camera would not pause its login lock ({msg}); nothing was changed.", 502)
        lock_back = lock
        steps.append("login lock paused")

    doc = copy.deepcopy(template)
    ns = _ns(doc)
    keep = {"id", "userName", "bondIpAddressList", "bondMacAddressList", "userLevel", "attribute"}
    for c in list(doc):
        if hikisapi.tag(c) not in keep:
            doc.remove(c)
    name_el = _set_child(doc, "userName", user, ns)
    pw_el = doc.makeelement(f"{{{ns}}}password" if ns else "password", {})
    pw_el.text = new
    doc.insert(list(doc).index(name_el) + 1, pw_el)
    ok, msg = _write(cl, H, "PUT", f"/ISAPI/Security/users/{me['id']}", doc, old)
    if not ok:
        _restore_lock(cl, H, lock_back, steps)
        raise JobError(f"The camera refused the new password ({msg}); nothing was changed.", 502)
    nc = client_for(user, new)
    try:
        r = nc.get("/ISAPI/Security/userCheck")
        took = r.status == 200
    except hikisapi.LoginRefused:
        took = False
    if not took:
        # did it keep the old one? (one try with it; the camera's lock is paused)
        oc = client_for(user, old)
        try:
            still_old = oc.get("/ISAPI/Security/userCheck").status == 200
        except hikisapi.LoginRefused:
            still_old = False
        if still_old:
            _restore_lock(oc, H, lock_back, steps)
            raise JobError("The camera said yes but still takes the old password; nothing was changed.", 502)
        raise JobError("The camera takes neither the old nor the new password now. Its login lock is paused; "
                       "check the camera's own web page.", 502)
    # the camera has the new password: save it before anything else
    if creds.get_admin(key)["username"]:
        creds.set_admin(key, user, new)
    else:
        creds.set_(key, user, new, creds.get(key).get("notes") or "")
    steps.append("camera password changed and saved")

    results = []
    for rname, rcl, chans in recorders:
        for ch in chans:
            row = {"recorder": rname, "channel": ch, "updated": False, "online": False, "error": ""}
            try:
                put_channel(rcl, H, ch, _set_login(_doc(rcl, ch), user, new))
                row["updated"] = True
                row["online"] = wait_online(rcl, ch, sleep=sleep)
            except (JobError, hikisapi.Unreachable, hikisapi.TooBig, hikisapi.LoginRefused) as ex:
                row["error"] = str(ex)[:200] or ex.__class__.__name__
            results.append(row)
    if recorders:
        steps.append("recorder channels updated")
    _restore_lock(nc, H, lock_back, steps)
    return {"user": user, "steps": steps, "channels": results}


def _restore_lock(cl, H, lock, steps):
    if lock is None:
        return
    try:
        ok, _msg = _write(cl, H, "PUT", LOCK, lock, None)
    except (hikisapi.Unreachable, hikisapi.LoginRefused):
        ok = False
    steps.append("login lock back on" if ok else "LOGIN LOCK STILL PAUSED — switch it on in Logins and security")
