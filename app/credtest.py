"""Test a device login without saving it: does this username/password get in?

One method per device, chosen from what it is and which ports answer:
  Hikvision camera/NVR  ISAPI deviceInfo (HTTP Digest)
  Ubiquiti EdgeSwitch   the switch's own JSON API login (what switchmon uses)
  MikroTik SwOS         one digest-auth read of /sys.b (what switchmon uses)
  anything with SSH     an SSH login (Ubiquiti, MikroTik, Linux, switches)
  MikroTik web          RouterOS REST (/rest, Basic)
  other web pages       only when the page itself asks for HTTP authentication

Every wrong password counts against the device — Hikvision locks the account
for 30 minutes after a handful of failures — so a test makes AT MOST ONE
authenticated attempt: an unauthenticated request first learns which scheme the
device wants, then one try with the right one. A definite answer (worked /
rejected) ends the test; only "could not ask" moves on to the next method.

Result: {"result": ok | auth_failed | unreachable | untestable, "method", "detail",
         "tried": [...], "info": {...}}
"""
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

import airos
import edgeswitch
import swos
import hikvision

PROBE_PORTS = (22, 80, 443, 8080, 8443)
DEFINITE = ("ok", "auth_failed")


def _open(ip, port, timeout=1.2):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def open_ports(ip, known=()):
    """Which of the ports a test can use answer right now (known scan results
    alone go stale: a camera's web server can be switched off since)."""
    with ThreadPoolExecutor(max_workers=len(PROBE_PORTS)) as pool:
        found = dict(zip(PROBE_PORTS, pool.map(lambda p: _open(ip, p), PROBE_PORTS)))
    return [p for p in PROBE_PORTS if found[p]]


def _r(result, method, detail, **info):
    return {"result": result, "method": method, "detail": detail, "info": info}


# ---- SSH ---------------------------------------------------------------------
def test_ssh(ip, username, password, timeout=15):
    """One password login over SSH. sshpass exits 5 when the password is
    refused; some devices (switches, RouterOS) refuse to run a command but only
    AFTER a successful login, so a non-zero exit without a refusal still counts."""
    cmd = ["sshpass", "-p", password, "ssh", *airos._SSH_OPTS,
           "-o", "PubkeyAuthentication=no",
           "-o", "PreferredAuthentications=password,keyboard-interactive",
           "-p", "22", f"{username}@{ip}", "exit"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        # No refusal within the timeout usually means an interactive shell
        # opened and ignored "exit" — i.e. we are in.
        return _r("ok", "SSH", f"Logged in over SSH as {username} (the device opened a shell)")
    except OSError as e:
        return _r("untestable", "SSH", f"SSH test unavailable: {e}")
    err = (r.stderr or "").lower()
    if r.returncode == 5 or "permission denied" in err or "authentication failed" in err:
        return _r("auth_failed", "SSH", f"The device rejected {username} / this password over SSH")
    if r.returncode == 0:
        return _r("ok", "SSH", f"Logged in over SSH as {username}")
    if any(x in err for x in ("connection refused", "timed out", "no route", "unreachable",
                              "connection reset", "kex_exchange", "closed by remote host")):
        return _r("unreachable", "SSH", "SSH did not answer: " + (r.stderr or "").strip().splitlines()[-1][:120])
    if r.returncode == 6:
        return _r("untestable", "SSH", "SSH host key problem")
    # Logged in, but the device would not run a command (common on switches).
    return _r("ok", "SSH", f"Logged in over SSH as {username} (the device does not run commands)")


# ---- HTTP ----------------------------------------------------------------------
def _scheme_auth(www_auth, username, password):
    """The auth object for the scheme the device asked for, or None."""
    w = (www_auth or "").lower()
    if "digest" in w:
        return HTTPDigestAuth(username, password), "Digest"
    if "basic" in w:
        return HTTPBasicAuth(username, password), "Basic"
    return None, ""


def _http_once(url, username, password, timeout):
    """(result, detail, response). One unauthenticated request, then at most
    one authenticated request with the scheme the 401 asked for."""
    try:
        r0 = requests.get(url, timeout=timeout, verify=False, allow_redirects=False)
    except requests.RequestException as e:
        return "unreachable", f"no answer ({e.__class__.__name__})", None
    if r0.status_code != 401:
        return "no_http_auth", f"HTTP {r0.status_code} without a login", r0
    auth, scheme = _scheme_auth(r0.headers.get("WWW-Authenticate"), username, password)
    if not auth:
        return "untestable", "asks for a login scheme Netwatch can't test", r0
    try:
        r1 = requests.get(url, auth=auth, timeout=timeout, verify=False, allow_redirects=False)
    except requests.RequestException as e:
        return "unreachable", f"no answer to the login ({e.__class__.__name__})", None
    if r1.status_code in (401, 403):
        return "auth_failed", f"rejected ({scheme})", r1
    if r1.status_code < 400 or r1.status_code == 404:
        return "ok", f"accepted ({scheme}, HTTP {r1.status_code})", r1
    return "untestable", f"HTTP {r1.status_code} after the login", r1


def _bases(ports):
    out = []
    for port, scheme in ((80, "http"), (443, "https"), (8080, "http"), (8443, "https")):
        if port in ports:
            out.append(f"{scheme}://{{ip}}" + ("" if port in (80, 443) else f":{port}"))
    return out


def test_hikvision(ip, username, password, ports, timeout=6):
    for base in _bases(ports)[:1]:          # one port only: every failure counts toward the lockout
        res, detail, resp = _http_once(base.format(ip=ip) + "/ISAPI/System/deviceInfo", username, password, timeout)
        if res == "ok" and resp is not None:
            info = hikvision._parse(resp.text)
            name = hikvision._channel_name(base.format(ip=ip), _scheme_auth(
                "digest", username, password)[0], timeout) if info else ""
            return _r("ok", "Hikvision ISAPI", f"Camera accepted the login ({detail.split('(')[-1].rstrip(')')})",
                      model=info.get("model", ""), name=name or info.get("deviceName", ""))
        if res == "auth_failed":
            return _r("auth_failed", "Hikvision ISAPI",
                      "The camera rejected this username or password. Hikvision locks the account "
                      "for about 30 minutes after several wrong tries — check before testing again.")
        if res == "no_http_auth":
            return _r("untestable", "Hikvision ISAPI", "The device's web API does not ask for a login")
        return _r(res if res in ("unreachable", "untestable") else "untestable", "Hikvision ISAPI", detail)
    return _r("unreachable", "Hikvision ISAPI", "No web port (80/443) answers")


def test_mikrotik_rest(ip, username, password, ports, timeout=6):
    for base in _bases(ports)[:1]:
        res, detail, resp = _http_once(base.format(ip=ip) + "/rest/system/identity", username, password, timeout)
        if res == "ok" and resp is not None and resp.status_code == 404:
            return _r("untestable", "RouterOS REST", "REST API not available (RouterOS 6?)")
        if res in DEFINITE:
            ident = ""
            if res == "ok":
                try:
                    ident = (resp.json() or {}).get("name", "")
                except ValueError:
                    pass
            return _r(res, "RouterOS REST", ("Router accepted the login" + (f" — {ident}" if ident else "")) if res == "ok"
                      else "The router rejected this username or password", name=ident)
        return _r("untestable" if res == "no_http_auth" else res, "RouterOS REST", detail)
    return _r("unreachable", "RouterOS REST", "No web port answers")


def test_edgeswitch(ip, username, password):
    """One login on the switch's API — the same call the switch monitor makes."""
    try:
        with edgeswitch.Session(ip, username, password, timeout=(5, 12)) as s:
            dev = s.get("/device") or {}
        ident = dev.get("identification") or {}
        return _r("ok", "EdgeSwitch API", "The switch accepted the login", model=ident.get("model", ""),
                  name=ident.get("name", ""))
    except edgeswitch.SwitchError as e:
        if e.kind == "auth_failed":
            return _r("auth_failed", "EdgeSwitch API",
                      "The switch rejected this username or password (use the login of the switch's own web page)")
        return _r("unreachable" if e.kind == "unreachable" else "untestable", "EdgeSwitch API", str(e)[:160])


def test_swos(ip, username, password):
    """One read of the SwOS system page — the same call the switch monitor makes.
    Its factory login is admin with a blank password."""
    try:
        with swos.Session(ip, username, password, timeout=(5, 12)) as s:
            sysb = s.get("/sys.b")
        return _r("ok", "SwOS web login", "The switch accepted the login", model=swos.hexstr(sysb.get("brd")),
                  name=swos.hexstr(sysb.get("id")))
    except swos.SwosError as e:
        if e.kind == "auth_failed":
            return _r("auth_failed", "SwOS web login",
                      "The switch rejected this username or password (use the login of the switch's own web page)")
        return _r("unreachable" if e.kind == "unreachable" else "untestable", "SwOS web login", str(e)[:160])


def test_web(ip, username, password, ports, timeout=6):
    for base in _bases(ports)[:1]:
        res, detail, _resp = _http_once(base.format(ip=ip) + "/", username, password, timeout)
        if res in DEFINITE:
            return _r(res, "Web login (HTTP auth)", ("The web page accepted the login " if res == "ok"
                                                    else "The web page rejected this login ") + detail.split(" ", 1)[-1])
        if res == "no_http_auth":
            return _r("untestable", "Web page",
                      "The web page uses its own login form, which can't be tested automatically — log in once in the browser to check")
        return _r(res, "Web page", detail)
    return _r("unreachable", "Web page", "No web port answers")


# ---- entry -----------------------------------------------------------------------------
def plan(dev, ports):
    """Methods to try, best first, from what the device is and which ports answer."""
    v = " ".join(str(dev.get(k) or "") for k in ("vendor", "model", "hostname", "banner", "os")).lower()
    cat = dev.get("category") or ""
    web = any(p in ports for p in (80, 443, 8080, 8443))
    hik = "hikvision" in v or "hangzhou" in v or (cat in ("camera", "nvr") and web)
    mikrotik = "mikrotik" in v or "routerboard" in v or "routeros" in v
    steps = []
    if hik and web:
        steps.append("hikvision")
    if web and swos.is_swos(dev):
        return ["swos"]               # no SSH, no API: the web login is the only one
    if web and edgeswitch.is_edgeswitch(dev):
        return ["edgeswitch"]         # its web page is a login form; SSH users can differ
    if 22 in ports:
        steps.append("ssh")
    if mikrotik and web:
        steps.append("mikrotik")
    if web and not hik:
        steps.append("web")
    return steps


def test(dev, username, password):
    ip = dev.get("ip")
    if not ip:
        return {"result": "untestable", "method": "", "detail": "This device has no IP address right now", "tried": []}
    if not (username or password):
        return {"result": "untestable", "method": "", "detail": "Type a username and password first", "tried": []}
    t0 = time.time()
    ports = open_ports(ip)
    steps = plan(dev, ports)
    if not ports:
        return {"result": "unreachable", "method": "", "tried": [], "ports": [],
                "detail": f"{ip} doesn't answer on SSH or web ports — is it online?"}
    if not steps:
        return {"result": "untestable", "method": "", "tried": [], "ports": ports,
                "detail": "No login Netwatch can test on this device"}
    tried, last = [], None
    for step in steps:
        if step == "hikvision":
            res = test_hikvision(ip, username, password, ports)
        elif step == "edgeswitch":
            res = test_edgeswitch(ip, username, password)
        elif step == "swos":
            res = test_swos(ip, username, password)
        elif step == "ssh":
            res = test_ssh(ip, username, password)
        elif step == "mikrotik":
            res = test_mikrotik_rest(ip, username, password, ports)
        else:
            res = test_web(ip, username, password, ports)
        tried.append({k: res[k] for k in ("method", "result", "detail")})
        last = res
        if res["result"] in DEFINITE:
            break
    out = dict(last)
    out["tried"] = tried
    out["ports"] = ports
    out["seconds"] = round(time.time() - t0, 1)
    return out
