"""Risky camera work (Phase 4d, part 1): logins, remote access and the Netwatch account.

Everything here is "dangerous" (plan section 5.2): it needs the site's "Risky camera
work" switch as well as "Camera settings", the person types a confirmation word, only
one such job runs at a time on the whole site, and every step is read back.

Two things live here:

  * the "security" group — what the device shows about its users, ports, Hik-Connect
    and platform access (read-only), and three settings that may be changed: the
    login lock after wrong passwords, SSH and UPnP. Ports, HTTPS and Hik-Connect are
    only shown: a wrong port cuts Netwatch and the recorder off the device, and
    Hik-Connect is left as the client has it;

  * the guided "Netwatch account" job: create an Operator user called `netwatch` with
    a long random password (made here, never shown, never logged), give it the rights
    Netwatch uses, prove it logs in, and only then make it the saved login. The admin
    login moves to its own slot (creds.get_admin) that only risky jobs use, so normal
    polling can never lock the admin account. Any failure after the user was created
    removes it again and keeps the admin login. "Remove" (also Undo) puts it back.

The admin client gets its own key in hikisapi (`<key>#admin`) so the one-try rule
keeps the two logins apart: a refusal of one is never forgotten because the other
worked. This file does no HTTP of its own beyond the Client it is given.
"""
import copy
import secrets
import string

import creds
import hikisapi

ACCOUNT = "netwatch"
RIGHTS_MAX = 2 << 20
CONFIRM_WORD = "CHANGE"
LEVELS = {"Administrator": "Admin", "Operator": "Operator", "Viewer": "Viewer"}

# The rights the netwatch account gets: everything Netwatch uses (pictures, playback
# later, health, settings, restart, PTZ, alarms), nothing it doesn't (firmware, two-way
# audio, the transparent serial channel, local outputs).
RIGHTS_ON = ("preview", "playBack", "record", "logOrStateCheck", "parameterConfig",
             "restartOrShutdown", "ptzControl", "alarmOutOrUpload")
RIGHTS_OFF = ("upgrade", "voiceTalk", "transParentChannel", "contorlLocalOut")
CHANNEL_RIGHTS = ("preview", "playBack", "record", "ptzControl")
_PW_SPECIAL = "!#%*+-=?@^_"


def sections():
    return [
        {"id": "access", "label": "Logins and remote access", "risk": "dangerous", "optional": "ll|ssh|upnp",
         "docs": {"ll": "/ISAPI/Security/illegalLoginLock", "ssh": "/ISAPI/System/Network/ssh",
                  "upnp": "/ISAPI/System/Network/UPnP"},
         "nocaps": ["ll", "upnp"],
         "fields": [
             ("ll.enabled", "Lock the login after wrong passwords",
              "Keep it on: it stops password guessing. Netwatch never tries a refused login twice.", {"kind": "bool"}),
             ("ll.maxIllegalLoginTimes", "Wrong passwords before the lock", "",
              {"kind": "int", "min": 3, "max": 20, "show_if": ("ll.enabled", ["true"])}),
             ("ll.maxIllegalLoginLockTime", "Locked for (minutes)", "",
              {"kind": "int", "min": 1, "max": 120, "show_if": ("ll.enabled", ["true"])}),
             ("ssh.enabled", "SSH", "A maintenance door for Hikvision support. Leave it off.", {"kind": "bool"}),
             ("upnp.enabled", "UPnP (opens router ports by itself)",
              "Leave it off: it can put the device on the internet without anyone noticing.", {"kind": "bool"}),
         ],
         "note": "Ports, HTTPS and Hik-Connect are only shown here. A wrong port cuts Netwatch and the recorder "
                 "off the device; Hik-Connect is left as the client has it."},
    ]


SECTION_IDS = tuple(s["id"] for s in sections())


# ---- what the device shows (read-only) -----------------------------------------------
def _get(cl, path, max_bytes=256 << 10):
    try:
        r = cl.get(path, max_bytes=max_bytes)
    except (hikisapi.Unreachable, hikisapi.TooBig):
        return None
    return hikisapi.parse(r.content) if r.status == 200 else None


def users(root):
    out = []
    for u in (root.iter() if root is not None else ()):
        if hikisapi.tag(u) != "User":
            continue
        uid, name = hikisapi.child_text(u, "id"), hikisapi.child_text(u, "userName")
        if uid.isdigit() and name:
            lvl = hikisapi.child_text(u, "userLevel")
            out.append({"id": int(uid), "name": name[:32], "level": LEVELS.get(lvl, lvl[:20])})
    return out


def info(cl, key):
    """The read-only part of the Security tab. No secret is ever read into it."""
    out = {"users": users(_get(cl, "/ISAPI/Security/users")), "ports": [], "hik_connect": None, "isup": None}
    acc = _get(cl, "/ISAPI/Security/adminAccesses")
    for p in (acc.iter() if acc is not None else ()):
        if hikisapi.tag(p) == "AdminAccessProtocol":
            proto, port = hikisapi.child_text(p, "protocol"), hikisapi.child_text(p, "portNo")
            if proto and port.isdigit():
                out["ports"].append({"protocol": proto[:20], "port": int(port),
                                     "on": hikisapi.child_text(p, "enabled") == "true"})
    ez = _get(cl, "/ISAPI/System/Network/EZVIZ")
    if ez is not None:
        out["hik_connect"] = {"on": hikisapi.child_text(ez, "enabled") == "true",
                              "registered": hikisapi.child_text(ez, "registerStatus") == "true"}
    eh = _get(cl, "/ISAPI/System/Network/Ehome")
    if eh is not None:
        out["isup"] = {"on": hikisapi.child_text(eh, "enabled") == "true"}
    saved = creds.get(key)
    out["login"] = {"uses": saved["username"][:32], "netwatch": saved["username"] == ACCOUNT,
                    "admin_kept": bool(creds.get_admin(key)["username"])}
    warn = []
    on = {p["protocol"] for p in out["ports"] if p["on"]}
    if out["hik_connect"] and out["hik_connect"]["on"]:
        warn.append("Hik-Connect is on: the device can be reached through Hikvision's cloud.")
    if "HTTPS" not in on and out["ports"]:
        warn.append("HTTPS is off: the web page and logins travel unencrypted.")
    if not out["login"]["netwatch"] and out["login"]["uses"] == "admin":
        warn.append("Netwatch logs in as admin. The Netwatch account keeps admin safe from lock-outs.")
    out["warnings"] = warn
    return out


# ---- the netwatch account -------------------------------------------------------------
class AccountError(Exception):
    def __init__(self, msg, status=409):
        super().__init__(msg)
        self.status = status


def new_password(n=16):
    """A random password Hikvision accepts: 8–16 characters with upper and lower case,
    digits and symbols. Only XML-safe symbols."""
    n = max(8, min(16, n))
    pools = (string.ascii_uppercase, string.ascii_lowercase, string.digits, _PW_SPECIAL)
    chars = [secrets.choice(p) for p in pools for _ in range(2)]
    every = "".join(pools)
    chars += [secrets.choice(every) for _ in range(n - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def _sub(parent, name, text, ns):
    el = parent.makeelement(f"{{{ns}}}{name}" if ns else name, {})
    el.text = text
    parent.append(el)
    return el


def _set_child(el, name, text, ns):
    for c in el:
        if hikisapi.tag(c) == name:
            c.text = text
            return c
    return _sub(el, name, text, ns)


def _ns(root):
    return root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else ""


def _user_doc(template, uid, password, admin_pw, with_login):
    """A new User document shaped like the device's own admin user (recorders want their
    bound IP/MAC lists back), with the new name, level and password."""
    u = copy.deepcopy(template)
    ns = _ns(u)
    keep = {"id", "userName", "bondIpAddressList", "bondMacAddressList", "userLevel", "attribute"}
    for c in list(u):
        if hikisapi.tag(c) not in keep:
            u.remove(c)
    _set_child(u, "id", str(uid), ns)
    name = _set_child(u, "userName", ACCOUNT, ns)
    pw = u.makeelement(f"{{{ns}}}password" if ns else "password", {})
    pw.text = password
    u.insert(list(u).index(name) + 1, pw)
    _set_child(u, "userLevel", "Operator", ns)
    for c in u:
        if hikisapi.tag(c) == "attribute":
            for a in c:
                if hikisapi.tag(a) == "inherent":
                    a.text = "false"
    if with_login:
        _sub(u, "loginPassword", admin_pw, ns)
    return u


def _set_rights(perm):
    """Switch the netwatch account's remote rights to RIGHTS_ON / RIGHTS_OFF, all channels."""
    rp = next((c for c in perm.iter() if hikisapi.tag(c) == "remotePermission"), None)
    if rp is None:
        return False
    for c in rp:
        t = hikisapi.tag(c)
        if t in RIGHTS_ON:
            c.text = "true"
        elif t in RIGHTS_OFF:
            c.text = "false"
        elif t.endswith("PermissionList"):
            for ch in c:
                for x in ch:
                    if hikisapi.tag(x) in CHANNEL_RIGHTS:
                        x.text = "true"
    ut = next((c for c in perm if hikisapi.tag(c) == "userType"), None)
    if ut is not None:
        ut.text = "operator"
    return True


# Double verification: newer recorders want the admin password again INSIDE a user
# change, and answer a change without it with HTTP 401 — which the one-try rule must
# read as "login refused". So the admin password always goes in on the FIRST try; the
# plain form is tried only after a harmless non-401 refusal (firmware that doesn't
# know the element). A 401 raises LoginRefused in the Client and stops everything.
def _write(cl, H, method, path, root, admin_pw):
    tries = [root]
    if admin_pw is not None and not any(hikisapi.tag(c) == "loginPassword" for c in root):
        withpw = copy.deepcopy(root)
        _sub(withpw, "loginPassword", admin_pw, _ns(withpw))
        tries = [withpw, root]
    first = ""
    for doc in tries:
        r = cl.request(method, path, data=H.serialize(doc), headers={"Content-Type": "application/xml"}, max_bytes=64 << 10)
        ok, _rb, msg = H.status(r)
        if ok:
            return True, ""
        first = first or msg
    return False, first


# The admin password travels UNENCODED in the delete's address: a home recorder
# (DS-7732NXI V4.x) does not %-decode it and answers "loginPasswordError" to an encoded
# one, which may count towards its lock-out. So it is sent only when every character
# is one an address carries as it is; otherwise the account is not made (nor deleted).
RAW_OK = set(string.ascii_letters + string.digits + "-._~!$'()*,/:;@[]")


def _delete(cl, H, uid, admin_pw):
    """Delete a user. Current firmware wants the admin password as ?loginPassword= (a
    plain DELETE gets "Invalid XML Content", a body is refused) — checked on a
    DS-2CD2046G2 V5.5.8 camera and a DS-7732NXI recorder. The password is never logged.
    A wrong-password answer is never followed by another try."""
    if not admin_pw or not set(admin_pw) <= RAW_OK:
        return False, "the admin password has characters that can't be sent safely for a delete"
    r = cl.request("DELETE", f"/ISAPI/Security/users/{uid}?loginPassword={admin_pw}", max_bytes=64 << 10)
    ok, _rb, msg = H.status(r)
    if ok:
        return True, ""
    if "loginPassword" in msg or r.status in (401, 403):
        return False, msg
    r = cl.request("DELETE", f"/ISAPI/Security/users/{uid}", max_bytes=64 << 10)     # old firmware: no parameter
    ok2, _rb, _m = H.status(r)
    return (True, "") if ok2 else (False, msg)


def hikisapi_ns():
    return "http://www.hikvision.com/ver20/XMLSchema"


def _esc(t):
    return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _proof_path(d):
    return "/ISAPI/ContentMgmt/InputProxy/channels" if d.get("category") == "nvr" else "/ISAPI/Image/channels/1"


def create(cl, d, H, client_for):
    """Make the netwatch account on device `d` with the admin client `cl` and switch
    Netwatch to it. Returns a dict for the History entry. `client_for(user, pw)` makes a
    Client for the new login (its own one-try slot)."""
    key = d["key"]
    saved = creds.get(key)
    admin = creds.get_admin(key)
    if saved["username"] == ACCOUNT:
        raise AccountError("Netwatch already logs in to this device with its own account.")
    admin_u, admin_pw = (admin["username"], admin["password"]) if admin["username"] else (saved["username"], saved["password"])
    if not admin_pw:
        raise AccountError("No admin login is saved for this device.")
    if not set(admin_pw) <= RAW_OK:
        raise AccountError("The admin password has characters (such as & # % + = ? or a space) that Netwatch can't "
                           "send safely when it removes the account again, so it won't make one on this device.")
    root = _get(cl, "/ISAPI/Security/users")
    have = users(root)
    if not have:
        raise AccountError("The device did not list its users — the saved login may not be an admin.", 502)
    me = next((u for u in have if u["name"] == admin_u), None)
    if me is None or me["level"] != "Admin":
        raise AccountError("The saved login is not this device's admin, so it can't add a user.")
    if any(u["name"] == ACCOUNT for u in have):
        raise AccountError("A user called netwatch already exists on this device, but Netwatch has no password "
                           "for it. Remove it on the device first.")
    cap = _get(cl, "/ISAPI/Security/capabilities")
    most = hikisapi.text(cap, "supportUserNums") if cap is not None else ""
    if most.isdigit() and len(have) >= int(most):
        raise AccountError(f"The device already has its {most} users.")
    lim = hikisapi.find(cap, "LoginPasswordLenLimit") if cap is not None else None
    top = lim.get("max") if lim is not None else ""
    password = new_password(int(top) if (top or "").isdigit() else 16)
    template = next(u for u in root.iter() if hikisapi.tag(u) == "User"
                    and hikisapi.child_text(u, "userName") == admin_u)
    uid = max(u["id"] for u in have) + 1

    ok, msg = _write(cl, H, "POST", "/ISAPI/Security/users", _user_doc(template, uid, password, admin_pw, True), None)
    if not ok:
        ok, msg2 = _write(cl, H, "POST", "/ISAPI/Security/users", _user_doc(template, uid, password, admin_pw, False), None)
        if not ok:
            raise AccountError(f"The device refused the new user: {msg2 or msg}", 502)
    steps = ["user created"]
    try:
        made = next((u for u in users(_get(cl, "/ISAPI/Security/users")) if u["name"] == ACCOUNT), None)
        if made is None:
            raise AccountError("The device said yes but does not list the new user.", 502)
        uid = made["id"]
        # a recorder lists every channel for every user: 430 KB for two users on a 32-channel NVR
        perms = _get(cl, "/ISAPI/Security/UserPermission", max_bytes=RIGHTS_MAX)
        perm = next((p for p in (perms.iter() if perms is not None else ()) if hikisapi.tag(p) == "UserPermission"
                     and hikisapi.child_text(p, "userID") == str(uid)), None)
        # Without its rights the account can't change settings (a recorder answers
        # "lowPrivilege"), so the job stops here rather than leave a half-made account.
        if perm is None or not _set_rights(perm):
            raise AccountError("The device did not show the new account's rights, so they could not be set.", 502)
        pid = hikisapi.child_text(perm, "id")
        ok, msg = _write(cl, H, "PUT", f"/ISAPI/Security/UserPermission/{pid}", perm, admin_pw)
        if not ok:
            raise AccountError(f"The device refused the account's rights: {msg}", 502)
        steps.append("rights set")
        # the proof: the new login reads the device and a settings document
        nc = client_for(ACCOUNT, password)
        for path in ("/ISAPI/Security/userCheck", _proof_path(d)):
            try:
                r = nc.get(path, max_bytes=512 << 10)
            except hikisapi.LoginRefused:
                raise AccountError("The device did not accept the new account's login.", 502) from None
            if r.status != 200:
                raise AccountError(f"The new account can't read {path} (HTTP {r.status}).", 502)
        steps.append("login tested")
    except BaseException as ex:
        gone, why = _delete(cl, H, uid, admin_pw)
        if isinstance(ex, AccountError):
            raise AccountError(str(ex) + (" The new user was removed again; nothing else changed." if gone else
                                          f" Removing the new user failed too ({why}) — remove 'netwatch' on the device."),
                               ex.status) from None
        raise
    creds.set_(key, ACCOUNT, password, saved.get("notes") or "")
    creds.set_admin(key, admin_u, admin_pw)
    return {"user_id": uid, "admin": admin_u, "steps": steps}


def remove(cl, d, H):
    """Delete the netwatch account and go back to the admin login (also Undo of create)."""
    key = d["key"]
    saved, admin = creds.get(key), creds.get_admin(key)
    if saved["username"] != ACCOUNT or not admin["username"]:
        raise AccountError("Netwatch is not using its own account on this device.")
    made = next((u for u in users(_get(cl, "/ISAPI/Security/users")) if u["name"] == ACCOUNT), None)
    if made is not None:
        ok, msg = _delete(cl, H, made["id"], admin["password"])
        if not ok:
            raise AccountError(f"The device refused to remove the netwatch user ({msg}). Netwatch still logs in with "
                               "it; remove 'netwatch' on the device's own web page, then save the admin login again.", 502)
        if any(u["name"] == ACCOUNT for u in users(_get(cl, "/ISAPI/Security/users"))):
            raise AccountError("The device still lists the netwatch user.", 502)
    creds.set_(key, admin["username"], admin["password"], saved.get("notes") or "")
    creds.set_admin(key)
    return {"user_id": made["id"] if made else None, "admin": admin["username"], "steps": ["user removed" if made else "user was already gone"]}
