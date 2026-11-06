"""A fake Hikvision smart switch (DS-3E1520P-EI, web app V3.x) for the tests.

It speaks the JSON "thing model" API the way the real switch's own web page
does (see app/hikswitch.py): every POST body is {"data": ...} (attribute sets
{"data": {"Value": ...}}), answers are {"status", "code", "errorMsg", "data"}.
Login is Session/AuthInfo + Session/Login with the PBKDF2 hash, checked here
with an independent copy of the formula; a wrong password counts down
retryLoginTime and then locks the account, like the real one. Everything else
needs the SessionTag header. Writes change the state the reads return.
"""
import copy
import hashlib
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API = "/iot/global/0-global/model"
OK = {"status": 200, "code": "0x00000000", "errorMsg": "Succeeded."}


def _hash(user, password, challenge, salt, iterations):
    inner = hashlib.sha256(f"{user}{salt}{password}".encode()).hexdigest()
    return hashlib.pbkdf2_hmac("sha256", (inner + challenge).encode(), salt.encode(), int(iterations), 64).hex()


def _ports():
    ports = []
    for n in range(1, 21):
        lnk_mode = 1 if n >= 19 else 0                       # 19/20 = SFP cages
        ports.append({"ID": n, "name": f"Ge{n}" if n < 19 else f"SFP{n - 18}", "en": 1,
                      "lnkSta": 1 if n in (1, 2, 3, 5, 17, 19) else 0, "lnkMode": lnk_mode,
                      "rate": 4 if n in (17, 19) else 3, "dx": 1, "flowCtrlEn": 0,
                      "poePow": {1: 6.4, 2: 4.1, 3: 0.0}.get(n, 0.0) if n <= 16 else None,
                      "stats": {"rxPktS": 100, "txPktS": 80}})
    ports[4]["dx"] = 0                                        # port 5: half duplex
    return ports


class FakeHikSwitch:
    def __init__(self, user="admin", password="Secret#123", tries=3, page=None):
        self.user, self.password = user, password
        self.tries_left = tries
        self.locked = False
        self.page = page                 # force small pages to test MORE paging
        self.calls = []                  # (method, path, body)
        self.tag = None
        self.pending = {}                # sessionID -> (challenge, salt, iterations)
        self.rebooted = False
        self.ports = _ports()
        self.basic = [{"ID": p["ID"], "en": 1, "rate": 0, "dx": 3, "flowCtrlEn": 0, "ANEn": 1,
                       "des": "Gate cam" if p["ID"] == 1 else ""} for p in self.ports]
        self.basic[16].update({"rate": 4, "dx": 3})          # port 17 fixed 1000M-auto
        self.stats = [{"ID": p["ID"], "portName": p["name"], "lnkSta": p["lnkSta"],
                       "recvBytes": str(1000000 * p["ID"]), "sendBytes": str(2000000 * p["ID"]),
                       "recvBytesSpeed": 125000 * p["ID"] if p["lnkSta"] else 0,
                       "sendBytesSpeed": 64000 if p["lnkSta"] else 0,
                       "inErrorPacket": "7" if p["ID"] == 3 else "0", "inLossPkt": "0"} for p in self.ports]
        self.poe = [{"portID": n, "portName": f"Ge{n}", "enabled": True, "nonStandardEnabled": False,
                     "poePower": {1: 6.4, 2: 4.1}.get(n, 0.0),
                     "portStatus": {3: "abnormal", 4: "detectAbnormal"}.get(n, "normal" if n in (1, 2) else "testing"),
                     "PDTestInfo": {1: {"testLevel": "class4"}, 2: {"testLevel": "class3"},
                                    3: {"powerAbnormal": "icutFault"}, 4: {"detectAbnormal": "bigResistance"}}.get(n, {})}
                    for n in range(1, 17)]
        self.macs = ([{"macAddress": "C0-51-7E-00-00-01", "addressType": "dynamic", "portName": "Ge1"},
                      {"macAddress": "04:ee:cd:00:00:65", "addressType": "dynamic", "portID": 2},
                      {"macAddress": "04EECD000066", "addressType": "dynamic", "portID": 2},
                      {"macAddress": "aa:bb:cc:00:00:01", "addressType": "dynamic", "portID": 17}]
                     + [{"macAddress": f"aa:bb:cc:00:01:{i:02x}", "addressType": "dynamic", "portID": 17}
                        for i in range(8)]
                     + [{"macAddress": "11:22:33:44:55:66", "addressType": "static", "portID": 0}])
        self.loop = [{"portID": n, "loopProtectionStatus": "block" if n == 5 else "forward"} for n in range(1, 21)]
        self.summary = {"model": "DS-3E1520P-EI", "softwareVersion": "V3.3.0 build 251010",
                        "serialNumber": "GV0000001", "ipv4Address": "192.168.1.243",
                        "macAddress": "84:94:59:00:02:43", "deviceUpTime": 86400 * 3 + 125,
                        "EZRegisterStatus": False, "EZEnabled": False, "VLANMaxNum": 4094,
                        "maxPower": 230, "day7PeakPower": 41.2, "power": 10.5, "hardwareVersion": "0x0",
                        "CPUList": [{"CPUUtilization": 12}], "memoryList": [{"memoryUtilization": 41}]}
        self.attrs = {"POE/PoEWatchdogParams": {"enabled": True},
                      "InfoMgr/DeviceDescription": {"deviceName": "Yard Switch", "deviceType": "Switch",
                                                    "serialNumber": "DS-3E1520P-EI20260101GV0000001"},
                      "SystemMaintenance/SSHCfg": {"enabled": False},
                      "SystemMaintenance/TelnetServerCfg": {"enabled": True},
                      "SNMPAccess/SNMPCfg": {"enabled": False}}
        self.unsupported = set()         # e.g. {"STPManage/SearchPortPrivateLoopStatus"}
        self._advanced()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=UTF-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return True                      # "answered" (the advanced handlers rely on it)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    return json.loads(self.rfile.read(n) or b"null")
                except ValueError:
                    return "BAD"

            def do_GET(self):
                fake._handle(self, "GET", None)

            def do_POST(self):
                fake._handle(self, "POST", self._body())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    # ---- advanced settings (hikswitch_adv) ---------------------------------------------
    def _advanced(self):
        """The advanced settings a DS-3E1520P-EI shows in its web app, factory-ish:
        RSTP and loop protection off, storm control global (10 % broadcast) and off
        per port, PoE watchdog on, EEE and LLDP on, DHCP snooping off, Telnet,
        ONVIF and the SADP agent on, clock set by hand."""
        self.attrs.update({
            "STPManage/STPGlobalCfg": {"STPEnabled": False, "STPMode": "RSTP", "sysPriority": 32768,
                                       "helloTime": 2, "maxAgingTime": 20, "forwardDelayTime": 15},
            "STPManage/STPGlobalStatus": {"bridgeID": "32768.84:94:59:00:02:43", "rootBridgeID": "4096.48:a9:8a:00:00:01",
                                          "rootBridgeHelloTime": 2, "rootBridgeMaxAgingTime": 20,
                                          "rootBridgeForwardDelayTime": 15},
            "STPManage/PrivateLoopProtocolCfg": {"enabled": False},
            "PortCast/PortStormGlobalCfg": {"castCtrlMode": "broadCast", "maxSpeedRatio": 10},
            "NetworkQoS/QoSCfg": {"QoSEnabled": False, "QoSType": "wholeQoS", "QoSAlgorithm": "WRR",
                                  "lowPriorityWeight": 1, "highPriorityWeight": 8},
            "NetworkQoS/QOSPortPriorityBatchCfg": {"QOSPortPriorityList": [{"portID": n, "priority": 0} for n in range(1, 17)]},
            "PowerMgr/PowerSavingModeParam": {"enabled": True},
            "LLDPMange/LLDPCfg": {"enabled": True},
            "NetworkSecurityProtection/DHCPSnoopingCfg": {"enabled": False,
                                                          "trustPortList": [{"portID": n, "enabled": False} for n in range(1, 21)]},
            "NetworkSecurityProtection/PortSecurity": {"enabled": False, "portSecurityType": "none", "portSecurityList": []},
            "HTTPAccess/HTTPAccessCfg": {"enabled": True, "portNo": 80},
            "HTTPAccess/HTTPSAccessCfg": {"enabled": True, "portNo": 443, "redirectEnabled": False},
            "SADPMgr/SADPCfg": {"SADPDevive": {"enabled": True}, "SADPClient": {"enabled": True}},
            "DeviceNetworkDiscovery/ONVIFDiscoveryParams": {"enabled": True},
            "TimeMgr/SystemDateTime": {"dateTimeType": "manually", "time": "2026-11-05T21:00:00+02:00",
                                       "NTPServer": {"addressingFormatType": "hostname", "hostName": "time.ys7.com",
                                                     "portNo": 123, "synchronizeInterval": 60}},
        })
        self.storm = [{"portID": n, "enabled": False, "stormControlType": "broadcast", "limitRate": 100}
                      for n in range(1, 21)]
        self.rate = [{"portID": n, "portName": f"Ge{n}", "rxEnabled": n == 6, "rxLimitRate": 20 if n == 6 else 0,
                      "txEnabled": False, "txLimitRate": 0} for n in range(1, 21)]
        self.remote = [{"portID": n, "enabled": n == 8} for n in range(1, 9)]
        self.isolation = []
        self.lldp = [{"portAddress": "Ge17", "chassisID": "48:a9:8a:00:00:01", "neighborhoodPortID": "ether3",
                      "peerIpv4Address": "192.168.1.20", "systemName": "Yard Router"}]
        self.stp_ports = []
        self.reset_count = 0
        self.model_file = True              # False: DynamicCapability/GetModelFile answers "not supported"
        self.model_drop = set()             # thing-model entries left out, e.g. {"PortCast/PortStormGlobalCfg"}

    def thing_model(self):
        """The switch's capability file, the parts hikswitch_adv reads (schemas only
        where the web app takes ranges or options from them)."""
        rng = lambda lo, hi: {"type": "integer", "minimum": lo, "maximum": hi}  # noqa: E731
        port_list = lambda lo, hi: {"type": "array", "items": {"type": "object", "properties": {"portID": rng(lo, hi)}}}  # noqa: E731
        doms = {
            "STPManage": {"props": {"STPGlobalCfg": {"sysPriority": rng(0, 61440), "helloTime": rng(1, 10)},
                                    "STPGlobalStatus": {}, "PrivateLoopProtocolCfg": {}},
                          "actions": {"SearchSTPPortStatus": None, "SearchSTPPortCfg": None, "ModifyBatchSTPPortCfg": None,
                                      "SearchPortPrivateLoopStatus": None}},
            "PortCast": {"props": {"PortStormGlobalCfg": {"castCtrlMode": {"type": "string", "enum": ["broadCast", "multiCast", "unknownUnicast"]},
                                                          "maxSpeedRatio": rng(1, 100)}},
                         "actions": {"SearchPortCastCfg": None,
                                     "ModifyPortCastCfgInfo": {"stormControlType": {"type": "string", "enum": ["broadcast", "multicast", "unicast"]},
                                                               "limitRate": rng(1, 1000)}}},
            "PortRate": {"props": {}, "actions": {"SearchPortRateCfg": None, "ModifyPortRateCfgInfo": {
                "portRateList": {"type": "array", "items": {"type": "object", "properties": {
                    "portID": rng(1, 20), "rxLimitRate": rng(1, 1000), "txLimitRate": rng(1, 1000)}}}}}},
            "NetworkQoS": {"props": {"QoSCfg": {"QoSType": {"type": "string", "enum": ["redPort", "wholeQoS"]},
                                                "QoSAlgorithm": {"type": "string", "enum": ["WRR", "SP"]}},
                                     "QOSPortPriorityBatchCfg": {"QOSPortPriorityList": port_list(1, 16)},
                                     "DSCPGlobalParams": {}}, "actions": {}},
            "POE": {"props": {"PoEWatchdogParams": {}}, "actions": {"SearchPortPoeInfo": None, "ModifyPortPoeInfo": None}},
            "PortRemoteTrans": {"props": {}, "actions": {"SearchPortRemoteTrans": None,
                                                         "ModifyPortRemoteTrans": {"__array__": {"portID": rng(1, 8)}}}},
            "PowerMgr": {"props": {"PowerSavingModeParam": {}}, "actions": {}},
            "LLDPMange": {"props": {"LLDPCfg": {}}, "actions": {"SearchLLDPNeighbor": None}},
            "NetworkSecurityProtection": {"props": {"DHCPSnoopingCfg": {}, "PortSecurity": {}},
                                          "actions": {"SearchDHCPSnoopingEntry": None, "GetARPGatewayProtectionEntry": None,
                                                      "AddIPSGAddressCheck": None}},
            "PortIsolationGroup": {"props": {}, "actions": {"GetIsolationGroupList": None,
                                                            "ModifyIsolationGroup": {"portList": port_list(1, 18)}}},
            "SystemMaintenance": {"props": {"TelnetServerCfg": {}}, "actions": {"SystemReboot": None}},
            "HTTPAccess": {"props": {"HTTPAccessCfg": {}, "HTTPSAccessCfg": {}}, "actions": {}},
            "SADPMgr": {"props": {"SADPCfg": {}}, "actions": {}},
            "DeviceNetworkDiscovery": {"props": {"ONVIFDiscoveryParams": {}}, "actions": {}},
            "TimeMgr": {"props": {"SystemDateTime": {"dateTimeType": {"type": "string", "enum": ["NTP", "manually"]},
                                                     "NTPServer": {"type": "object", "properties": {
                                                         "synchronizeInterval": rng(1, 1440), "portNo": rng(1, 65535)}}}},
                        "actions": {}},
            "PortMgr": {"props": {}, "actions": {"GetPortLineStatus": {"portID": rng(1, 16)},
                                                 "GetOpticalModulesDiagnosticInfo": {"portID": rng(19, 20)},
                                                 "ResetAllPortStatistics": None, "SearchPortStatus": None}},
            "L2Mgr": {"props": {}, "actions": {"SearchPortVLAN": None, "SearchVLAN": None, "SearchAggregatedLink": None}},
            "MirrorGroup": {"props": {}, "actions": {"GetMirrorGroup": None}},
            "ACLMgr": {"props": {}, "actions": {"SearchACL": None}},
        }
        out = []
        for dom, parts in doms.items():
            d = {"identifier": dom, "props": [], "actions": [], "events": []}
            for name, props in parts["props"].items():
                if f"{dom}/{name}" not in self.model_drop:
                    d["props"].append({"identifier": name, "access": "rw",
                                       "schema": {"type": "object", "properties": props or {}}})
            for name, props in parts["actions"].items():
                if f"{dom}/{name}" in self.model_drop:
                    continue
                if props and "__array__" in props:
                    sch = {"type": "array", "items": {"type": "object", "properties": props["__array__"]}}
                else:
                    sch = {"type": "object", "properties": props or {}}
                d["actions"].append({"identifier": name, "input": {"schema": sch}, "output": {"schema": {"type": "object"}}})
            out.append(d)
        return {"resources": [{"domains": out}], "capacities": {"support_token_deviceadd": 0}}

    @staticmethod
    def _merge(old, new):
        """An attribute write the way the switch takes it: given fields replace,
        others stay; port lists merge by portID."""
        if not isinstance(old, dict) or not isinstance(new, dict):
            return copy.deepcopy(new)
        out = copy.deepcopy(old)
        for k, v in new.items():
            if isinstance(v, list) and isinstance(out.get(k), list) and all(isinstance(x, dict) and "portID" in x for x in v):
                by = {x.get("portID"): x for x in out[k] if isinstance(x, dict)}
                for x in v:
                    by.setdefault(x["portID"], {}).update(copy.deepcopy(x))
                out[k] = sorted(by.values(), key=lambda x: x.get("portID") or 0)
            elif isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k] = FakeHikSwitch._merge(out[k], v)
            else:
                out[k] = copy.deepcopy(v)
        return out

    def _bad(self, h, code="0x00000002", msg="Invalid Content.", status=400):
        return h._send(status, {"status": status, "code": code, "errorMsg": msg})

    def _adv_service(self, h, key, data):
        """The advanced settings' service commands; None = not one of them."""
        if key == "DynamicCapability/GetModelFile":
            if not self.model_file:
                return self._bad(h, "0x00100003", "This function is not supported.", 404)
            return h._send(200, {**OK, "data": {"filePathType": "text", "filePath": json.dumps(self.thing_model())}})
        if key == "STPManage/SearchSTPPortStatus":
            if not self.attrs["STPManage/STPGlobalCfg"]["STPEnabled"]:
                return self._page(h, [], data)
            rows = [{"portID": p["ID"], "portName": p["name"], "pathCost": 20000 if p["rate"] == 4 else 200000,
                     "portRole": {17: "root", 19: "alternate"}.get(p["ID"], "designated" if p["lnkSta"] else "disabled"),
                     "portStatus": "blocking" if p["ID"] == 19 else "forwarding" if p["lnkSta"] else "discarding"}
                    for p in self.ports]
            return self._page(h, rows, data)
        if key == "STPManage/SearchSTPPortCfg":
            return self._page(h, [{"portID": p["ID"], "portEnabled": True, "portPriority": 128} for p in self.ports], data)
        if key == "PortCast/SearchPortCastCfg":
            return self._page(h, self.storm, data)
        if key == "PortCast/ModifyPortCastCfgInfo":
            if not isinstance(data, list) or not all(isinstance(x, dict) and "portID" in x and isinstance(x.get("enabled"), bool) for x in data):
                return self._bad(h)
            for x in data:
                row = next((r for r in self.storm if r["portID"] == x["portID"]), None)
                if row is None:
                    return self._bad(h, "0x00000003", "Invalid port.")
                row.update(x)
            return h._send(200, {**OK, "data": {}})
        if key == "PortRate/SearchPortRateCfg":
            return self._page(h, self.rate, data, "MatchResults")
        if key == "PortRate/ModifyPortRateCfgInfo":
            items = (data or {}).get("portRateList") if isinstance(data, dict) else None
            if not isinstance(items, list):
                return self._bad(h)
            for x in items:
                if not {"portID", "rxEnabled", "txEnabled"} <= set(x):
                    return self._bad(h, "0x004000b3", "Invalid port rate limiting mode.")
                row = next(r for r in self.rate if r["portID"] == x["portID"])
                row.update(x)
            return h._send(200, {**OK, "data": {}})
        if key == "PortRemoteTrans/SearchPortRemoteTrans":
            return self._page(h, self.remote, data)
        if key == "PortRemoteTrans/ModifyPortRemoteTrans":
            if not isinstance(data, list):
                return self._bad(h)
            for x in data:
                row = next((r for r in self.remote if r["portID"] == x.get("portID")), None)
                if row is None:
                    return self._bad(h, "0x00400098", "Invalid long-range port number.")
                row["enabled"] = bool(x.get("enabled"))
            return h._send(200, {**OK, "data": {}})
        if key == "LLDPMange/SearchLLDPNeighbor":
            return self._page(h, self.lldp if self.attrs["LLDPMange/LLDPCfg"]["enabled"] else [], data)
        if key == "NetworkSecurityProtection/SearchDHCPSnoopingEntry":
            return self._page(h, [{"portName": "Ge2", "ipv4Address": "192.168.1.65", "macAddress": "04:ee:cd:00:00:65",
                                   "VLANID": 1, "leaseTimeRemaining": 3500}], data)
        if key == "PortIsolationGroup/GetIsolationGroupList":
            return h._send(200, {**OK, "data": {"isolationGroupList": [
                {"isolationGroupID": 1, "portList": [{"portID": n} for n in self.isolation]}]}})
        if key == "PortIsolationGroup/ModifyIsolationGroup":
            if (data or {}).get("isolationGroupID") != 1 or not isinstance(data.get("portList"), list):
                return self._bad(h)
            self.isolation = sorted(x["portID"] for x in data["portList"])
            return h._send(200, {**OK, "data": {}})
        if key == "PortMgr/GetPortLineStatus":
            n = (data or {}).get("portID")
            if not isinstance(n, int) or n > 16:
                return self._bad(h, "0x004000bc", "Cable detection is not supported by this port.")
            res = {3: {"lineStatus": "abnormal", "lineLength": 46, "description": "B_OPEN"}}.get(
                n, {"lineStatus": "normal", "lineLength": 23, "description": "NORMAL"})
            return h._send(200, {**OK, "data": res})
        if key == "PortMgr/GetOpticalModulesDiagnosticInfo":
            if (data or {}).get("portID") != 19:
                return self._bad(h, "0x0040012a", "DDM is not supported by the optical module plugged into this port.")
            return h._send(200, {**OK, "data": {
                "realtimeParam": {"temp": 41.5, "voltage": 3.29, "biasCurrent": 6.1, "receivedPower": -9.8, "transmitPower": -5.2},
                "paramThreshold": {"receivedPowerLowAlarm": -23.0},
                "transceiverInfo": {"connectorType": "LC", "transceiverType": "1000BASE-LX", "wavelength": 1310,
                                    "vendorName": "HIKVISION", "singleModeKM": 20}}})
        if key == "PortMgr/ResetAllPortStatistics":
            self.reset_count += 1
            for r in self.stats:
                r["inErrorPacket"] = "0"
            return h._send(200, {**OK, "data": {}})
        if key == "L2Mgr/SearchPortVLAN":
            return self._page(h, [{"portName": p["name"], "VLANType": "access", "accessMode": {"VLANID": 1}}
                                  for p in self.ports], data)
        if key == "L2Mgr/SearchAggregatedLink":
            return self._page(h, [], data)
        if key == "MirrorGroup/GetMirrorGroup":
            return h._send(200, {**OK, "data": {"mirroringGroupList": [
                {"mirroringGroupID": 1, "destinationPortNo": 0, "sourcePortInfo": []}]}})
        return None

    @property
    def ip(self):
        return f"127.0.0.1:{self.port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def names(self):
        return [p.rsplit("/", 1)[-1] for _, p, _ in self.calls]

    # ---- the API ---------------------------------------------------------------------
    def _handle(self, h, method, body):
        path = h.path
        self.calls.append((method, path, copy.deepcopy(body)))
        if not path.startswith(API + "/"):
            return h._send(404, {"status": 404, "code": "0x00000004", "errorMsg": "not found"})
        rest = path[len(API) + 1:]
        if method == "POST":
            if not isinstance(body, dict) or "data" not in body:
                return h._send(400, {"status": 400, "code": "0x00000002", "errorMsg": "Invalid Format."})
            data = body["data"]
        else:
            data = None
        if rest == "service/operate/Activate/GetActivateStatus":
            return h._send(200, {**OK, "data": {"activated": True, "supportVersion": [2]}})
        if rest == "service/operate/Session/AuthInfo":
            sid = secrets.token_hex(8)
            info = {"challenge": secrets.token_hex(16), "salt": secrets.token_hex(16), "iterations": 100,
                    "sessionID": sid, "SessionIDVersion": 2}
            self.pending[sid] = info
            return h._send(200, {**OK, "data": info})
        if rest == "service/operate/Session/Login":
            info = self.pending.pop((data or {}).get("sessionID"), None)
            if self.locked:
                return h._send(401, {"status": 401, "code": "0x00200006", "errorMsg": "Locked.",
                                     "data": {"lockStatus": "lock", "retryLoginTime": 0, "unlockTime": 1800}})
            want = info and _hash(self.user, self.password, info["challenge"], info["salt"], info["iterations"])
            if not info or data.get("username") != self.user or data.get("password") != want \
                    or data.get("sessionIDVersion") != info["SessionIDVersion"]:
                self.tries_left -= 1
                if self.tries_left <= 0:
                    self.locked = True
                return h._send(401, {"status": 401, "code": "0x00200006", "errorMsg": "Password error.",
                                     "data": {"lockStatus": "lock" if self.locked else "unlock",
                                              "retryLoginTime": max(0, self.tries_left),
                                              "unlockTime": 1800 if self.locked else 0}})
            self.tag = secrets.token_hex(12)
            return h._send(200, {**OK, "data": {"sessionTag": self.tag}})
        if not self.tag or h.headers.get("SessionTag") != self.tag:
            return h._send(401, {"status": 401, "code": "0x00200010", "errorMsg": "Unauthorized device.",
                                 "data": {"Value": {"statusValue": 401, "statusString": "Unauthorized"}}})
        parts = rest.split("/")
        if parts[0] == "attribute":
            key = "/".join(parts[2:])
            if key in self.unsupported or key not in self.attrs:
                return h._send(404, {"status": 404, "code": "0x00100003", "errorMsg": "This function is not supported."})
            if parts[1] == "get":
                return h._send(200, {**OK, "data": {"Value": self.attrs[key]}})
            if parts[1] != "set" or not isinstance(data, dict) or not isinstance(data.get("Value"), dict):
                return self._bad(h)
            self.attrs[key] = self._merge(self.attrs[key], data["Value"])
            return h._send(200, {**OK, "data": {}})
        key = "/".join(parts[2:])
        if key in self.unsupported:
            return h._send(404, {"status": 404, "code": "0x00100003", "errorMsg": "This function is not supported."})
        return self._service(h, key, data)

    def _page(self, h, rows, data, list_key="matchResults"):
        start = max(1, int((data or {}).get("searchResultPosition") or 1)) - 1
        size = int((data or {}).get("maxResults") or 100)
        if self.page:
            size = min(size, self.page)
        if (data or {}).get("lastMacAddress") and list_key == "matchResults":
            pass                          # the real switch pages by position AND last MAC
        chunk = rows[start:start + size]
        more = start + size < len(rows)
        return h._send(200, {**OK, "data": {"searchID": (data or {}).get("searchID"),
                                            "responseStatus": "MORE" if more else "OK",
                                            "numOfMatches": len(chunk), "totalMatches": len(rows),
                                            list_key: copy.deepcopy(chunk)}})

    def _service(self, h, key, data):
        if self._adv_service(h, key, data):
            return True
        if key == "Session/Logout":
            self.tag = None
            return h._send(200, {**OK, "data": {}})
        if key == "Session/HeartBeat":
            return h._send(200, {**OK, "data": {}})
        if key == "PortMgr/GetSwitchSumInfo":
            return h._send(200, {**OK, "data": copy.deepcopy(self.summary)})
        if key == "PortMgr/SearchPortStatus":
            return self._page(h, self.ports, data)
        if key == "PortMgr/SearchPortBasicParam":
            return self._page(h, self.basic, data)
        if key == "PortMgr/SearchPortStatistics":
            return self._page(h, self.stats, data)
        if key == "POE/SearchPortPoeInfo":
            return self._page(h, self.poe, data, "portPoeInfoList")
        if key == "L2TableMgr/SearchPortMacAddress":
            return self._page(h, self.macs, data)
        if key == "STPManage/SearchPortPrivateLoopStatus":
            return self._page(h, self.loop, data)
        if key == "PortMgr/ModifyBatchPortBasicParam":
            for item in (data or {}).get("portBasicList") or []:
                if not {"portID", "enabled", "speed", "duplexMode", "flowCtrlEnabled"} <= set(item):
                    return h._send(400, {"status": 400, "code": "0x00000002", "errorMsg": "Invalid Content."})
                row = next(r for r in self.basic if r["ID"] == item["portID"])
                row["en"] = 1 if item["enabled"] else 0
                if "describe" in item:
                    row["des"] = item["describe"]
                port = next(p for p in self.ports if p["ID"] == item["portID"])
                port["en"] = row["en"]
            return h._send(200, {**OK, "data": {}})
        if key == "POE/ModifyPortPoeInfo":
            if not isinstance(data, list):
                return h._send(400, {"status": 400, "code": "0x00000002", "errorMsg": "Invalid Content."})
            for item in data:
                row = next((r for r in self.poe if r["portID"] == item.get("portID")), None)
                if row is None:
                    return h._send(400, {"status": 400, "code": "0x00000003", "errorMsg": "Invalid port."})
                for k in ("enabled", "nonStandardEnabled", "poeMode"):
                    if k in item:
                        row[k] = item[k]
                if not row["enabled"]:
                    row.update({"poePower": 0.0, "portStatus": "down"})
            return h._send(200, {**OK, "data": {}})
        if key == "SystemMaintenance/SystemReboot":
            self.rebooted = True
            return h._send(200, {**OK, "data": {}})
        return h._send(404, {"status": 404, "code": "0x00100003", "errorMsg": "This function is not supported."})
