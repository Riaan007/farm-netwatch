"""A fake Ubiquiti EdgeSwitch (ES-8-150W firmware API) for tests and UI previews.

Serves the /api/v1.0 calls app/edgeswitch.py uses, shaped like the answers the
switch's own web page reads, over plain HTTP (the client falls back to it when
443 is closed). Login ubnt/ubnt; anything else gets the switch's 403.

  python tests/edgeswitch_mock.py [port]          # standalone
  from edgeswitch_mock import fixture              # in unit tests

State changes (PUT /interfaces) stick for the life of the process, so a port
turned off in the UI shows as off on the next read.
"""
import copy
import json
import random
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "mock-token-1"
START = time.time() - 3 * 86400 - 5 * 3600


def _port(n, name, plugged, speed="1000-full", poe="off", sfp=False):
    return {
        "identification": {"id": f"0/{n}", "name": name, "mac": f"d8:b3:70:73:bc:{0xe5 + n:02x}",
                           "type": "port"},
        "status": {"enabled": True, "plugged": plugged, "currentSpeed": speed if plugged else "",
                   "speed": "auto", "mtu": 1518, "arpProxy": False},
        "addresses": [],
        "port": {"stp": {"enabled": True, "edgePort": "auto", "pathCost": 0, "portPriority": 128,
                         "state": "forwarding" if plugged else "disabled"},
                 "poe": poe, "flowControl": False, "routed": False, "isolated": False,
                 "pingWatchdog": {"enabled": False, "address": "", "failureCount": 3, "interval": 30},
                 "speedLimit": {"enabled": False, "rx": 0, "tx": 0},
                 "sfp": {"present": sfp, "vendor": "UBNT" if sfp else "", "part": "UF-MM-1G" if sfp else ""},
                 "mirrorPorts": [], "dhcpSnooping": False},
    }


def fixture(pi_mac="02:42:ac:11:00:02", gw_mac="70:a7:41:45:cf:aa"):
    interfaces = [
        _port(1, "Uplink router", True, poe="off"),
        _port(2, "Port 2", True, poe="active"),
        _port(3, "Gate camera", True, speed="100-full", poe="active"),
        _port(4, "Port 4", False, poe="active"),
        _port(5, "NVR", True, poe="off"),
        _port(6, "Port 6", True, poe="24v"),
        _port(7, "Port 7", False, poe="off"),
        _port(8, "Port 8", False, poe="off"),
        _port(9, "SFP 1", True, poe="off", sfp=True),
        _port(10, "SFP 2", False, poe="off"),
    ]
    for i in interfaces[8:]:
        i["identification"]["type"] = "port"
    device = {
        "identification": {"mac": "d8:b3:70:73:bc:e5", "model": "ES-8-150W", "family": "EdgeSwitch",
                           "subsystemID": "eeab", "firmwareVersion": "v1.9.3", "product": "EdgeSwitch 8 150W",
                           "serialNumber": "D8B37073BCE5", "name": "Tower Switch"},
        "capabilities": {"interfaces": [
            {"id": f"0/{n}", "type": "port", "supportPOE": n <= 8,
             "poeValues": ["off", "active", "24v", "48v", "54v"] if n <= 8 else [],
             "speedValues": ["auto", "1000-full", "100-full", "100-half", "10-full", "10-half"]}
            for n in range(1, 11)]},
    }
    system = {"hostname": "Tower Switch", "timezone": "Africa/Johannesburg", "analyticsEnabled": False}
    services = {"sshServer": {"enabled": True, "sshPort": 22}, "telnetServer": {"enabled": False, "port": 23},
                "webServer": {"enabled": True, "httpPort": 80, "httpsPort": 443},
                "snmpAgent": {"enabled": True, "community": "public", "contact": "", "location": ""},
                "ntpClient": {"enabled": True, "ntpServers": ["0.ubnt.pool.ntp.org"]},
                "systemLog": {"enabled": False, "server": ""}, "unms": {"enabled": False, "key": ""}}
    vlans = [{"id": 1, "name": "default", "participation": [
        {"interface": {"id": f"0/{n}"}, "mode": "untagged"} for n in range(1, 11)]},
        {"id": 20, "name": "cameras", "participation": [
            {"interface": {"id": "0/1"}, "mode": "tagged"}, {"interface": {"id": "0/3"}, "mode": "tagged"}]}]
    mac_table = [
        {"mac": pi_mac, "vlan": 1, "port": "0/1"},
        {"mac": gw_mac, "vlan": 1, "port": "0/1"},
        {"mac": "44:19:b6:10:20:30", "vlan": 1, "port": "0/3"},
        {"mac": "c0:56:e3:aa:bb:cc", "vlan": 1, "port": "0/5"},
        {"mac": "f4:e2:c6:8a:f8:45", "vlan": 1, "port": "0/6"},
        {"mac": "0c:ea:14:ac:03:9c", "vlan": 1, "port": "0/2"},
        {"mac": "02:00:00:00:00:01", "vlan": 1, "port": "0/2"},
    ]
    return {"device": device, "system": system, "interfaces": interfaces, "services": services,
            "vlans": vlans, "mac_table": mac_table}


def statistics(state):
    ifs = []
    for i in state["interfaces"]:
        n = int(i["identification"]["id"].split("/")[1])
        up = i["status"]["plugged"] and i["status"]["enabled"]
        poe = {"active": 6.4 if n == 3 else 4.1, "24v": 3.2}.get(i["port"]["poe"], 0.0) if up else 0.0
        rate = (random.randint(2, 40) * 10 ** 6 if n in (1, 9) else random.randint(1, 8) * 10 ** 6) if up else 0
        ifs.append({"id": i["identification"]["id"], "name": i["identification"]["name"],
                    "statistics": {"rxRate": rate, "txRate": rate // 3, "rxBytes": 10 ** 9 * n, "txBytes": 4 * 10 ** 8 * n,
                                   "dropped": 3 if n == 3 else 0, "errors": state["errors"] if n == 3 else 0,
                                   "poePower": poe, "rxBroadcast": 10, "txBroadcast": 5}})
    state["errors"] += 400          # port 3 keeps collecting errors -> the error rule fires
    return [{"timestamp": int(time.time() * 1000), "device": {
        "cpu": [{"identifier": "ARM", "usage": 11}], "ram": {"usage": 43, "free": 90000, "total": 160000},
        "temperatures": [{"name": "Board", "type": "board", "value": 52.5}],
        "power": [{"psuType": "primary", "connected": True, "voltage": 54.1, "power": 22}],
        "fanSpeeds": [], "uptime": int(time.time() - START)}, "interfaces": ifs}]


class Handler(BaseHTTPRequestHandler):
    state = None

    def log_message(self, *a):
        pass

    def _send(self, code, body=None, headers=None, raw=None, ctype="application/json"):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"null") if n else None

    def _authed(self):
        if self.headers.get("x-auth-token") != TOKEN:
            self._send(401, {"statusCode": 401, "error": "Unauthorized"})
            return False
        return True

    def do_GET(self):
        st, p = self.state, self.path.split("?")[0]
        if p == "/":
            return self._send(200, raw=b"<!doctype html><html><head><title>Ubiquiti EdgeSwitch</title></head><body></body></html>",
                              ctype="text/html")
        if p == "/api/v1.0/public/device":
            return self._send(200, {"identification": {"product": "EdgeSwitch 8 150W", "model": "ES-8-150W",
                                                       "family": "EdgeSwitch"}, "isFactoryDefault": False})
        if not p.startswith("/api/v1.0/") or not self._authed():
            return None if p.startswith("/api/v1.0/") else self._send(404, {"error": "not found"})
        route = p[len("/api/v1.0/"):]
        if route == "statistics":
            return self._send(200, statistics(st))
        if route == "system/backup":
            return self._send(200, raw=b"\x1f\x8bmock-backup", ctype="application/gzip",
                              headers={"Content-Disposition": 'attachment; filename="Tower_Switch.tar.gz"'})
        key = {"device": "device", "system": "system", "interfaces": "interfaces", "services": "services",
               "vlans": "vlans", "tools/mac-table": "mac_table"}.get(route)
        if key:
            return self._send(200, st[key])
        return self._send(404, {"error": "no route"})

    def do_POST(self):
        st, p = self.state, self.path.split("?")[0]
        if p == "/api/v1.0/user/login":
            b = self._body() or {}
            if b.get("username") == "ubnt" and b.get("password") == "ubnt":
                return self._send(200, {"statusCode": 200}, headers={"x-auth-token": TOKEN})
            return self._send(403, raw=b"<html><title>403 - Forbidden</title></html>", ctype="text/html")
        if not self._authed():
            return None
        route = p[len("/api/v1.0/"):]
        if route in ("user/logout", "system/reboot", "device/locate/start", "device/locate/stop"):
            return self._send(200, {})
        if route == "tools/cable-test":
            return self._send(200, {"pairs": [{"pair": "A", "status": "ok", "length": 42}, {"pair": "B", "status": "ok", "length": 42}]})
        if route == "tools/discovery/neighbors":
            return self._send(200, [])
        return self._send(404, {"error": "no route"})

    def do_PUT(self):
        st, p = self.state, self.path.split("?")[0]
        if not self._authed():
            return None
        if p == "/api/v1.0/interfaces":
            for new in self._body() or []:
                for i, cur in enumerate(st["interfaces"]):
                    if cur["identification"]["id"] == new["identification"]["id"]:
                        st["interfaces"][i] = copy.deepcopy(new)
            return self._send(200, st["interfaces"])
        return self._send(404, {"error": "no route"})


def serve(port=80, **kw):
    Handler.state = {**fixture(**kw), "errors": 100}
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 80,
          pi_mac=sys.argv[2] if len(sys.argv) > 2 else "02:42:ac:11:00:02")
