"""Advanced settings of a Hikvision smart switch (DS-3E1520P-EI and friends, web app V3.x).

Everything the switch offers to keep a camera network up, read with ONE login and
changed one setting at a time:

  loop protection   RSTP (standard spanning tree) and Hikvision's own loop guard
  traffic           storm control, bandwidth limits, QoS with high-priority ports
  PoE and cabling   PoE watchdog, long-range ports, EEE, cable test, SFP diagnostics
  discovery         LLDP (neighbours), SADP agent, ONVIF discovery
  security          DHCP snooping with trusted ports, port isolation, Telnet
  time              the switch clock and its NTP server
  read only         VLANs, link aggregation, port mirroring, ERPS rings, port security

URLs, bodies and field names come from the switch's own web app (firmware V3.3.0
build 251010, framework V5.1.16_R0604): attribute reads are GET
attribute/get/<Group>/<Attr>, attribute writes POST attribute/set/<Group>/<Attr>
with {"data": {"Value": ...}}, services POST service/operate/<Group>/<Command>
with {"data": ...}. Every body here is the one that page sends.

Which parts a switch has comes from its own thing model (the list the web app
builds its menus from, hikswitch.load_model). Without one, each part is simply
tried and an answer of "not supported" marks it missing.

The switch monitor's poll does NOT call this: the advanced read happens when
someone opens the panel (GET /api/devices/<key>/switch/advanced).
"""
import ipaddress
import re
import time
import uuid
from datetime import datetime

import hikswitch
from hikswitch import HikSwitchError, Session, _int, _num, _on, _pid, _port_no, _not_supported

# ip -> parts answered "not supported" (asked again after a restart)
_UNSUPPORTED = {}
STP_DEFAULTS = (("sysPriority", 32768), ("helloTime", 2), ("maxAgingTime", 20), ("forwardDelayTime", 15))
STORM_MODES = {"broadCast": "broadcast", "multiCast": "multicast", "unknownUnicast": "unknown unicast",
               "broadcast": "broadcast", "multicast": "multicast", "unicast": "unknown unicast"}
FEATURES = ("rstp", "loop_guard", "erps", "storm", "rate_limit", "qos", "poe_watchdog", "long_range", "eee",
            "lldp", "dhcp_snooping", "isolation", "access", "time", "cable_test", "sfp", "vlan", "lag",
            "mirror", "port_security", "other")


# ---- reading --------------------------------------------------------------------------
def _search(s, group, cmd, page=500, extra=None, keys=("matchResults", "MatchResults")):
    """Every row of a paged Search… answer. Some commands name the list
    MatchResults (capital M) — the web app reads it that way for port rate limits."""
    rows, pos = [], 1
    for _ in range(hikswitch.MAX_PAGES):
        body = {"searchID": str(uuid.uuid4()), "searchResultPosition": pos, "maxResults": page}
        if extra:
            body.update(extra)
        res = s.svc(group, cmd, body)
        got = []
        if isinstance(res, dict):
            got = next((res[k] for k in keys if isinstance(res.get(k), list)), [])
        got = [x for x in got if isinstance(x, dict)]
        rows += got
        if not isinstance(res, dict) or res.get("responseStatus") != "MORE" or not got:
            break
        pos += _int(res.get("numOfMatches"), len(got)) or len(got)
    return rows


class _Reader:
    """One login's worth of reads: remembers what is missing and why."""

    def __init__(self, s, model, summ):
        self.s, self.model, self.summ = s, model, summ or {}
        self.raw, self.missing = {}, {}
        self.skip = _UNSUPPORTED.setdefault(s.ip, set())

    def get(self, part, fn, need=None):
        """fn(session) -> the switch's answer, or None. need = (group, name, kind):
        when the thing model says the switch lacks it, nothing is asked."""
        if part in self.skip:
            return None
        if need is not None and hikswitch.model_has(self.model, *need) is False:
            return None
        try:
            v = fn(self.s)
        except HikSwitchError as e:
            if e.kind == "unreachable":
                raise
            self.missing[part] = str(e)
            if _not_supported(e):
                self.skip.add(part)
            return None
        self.raw[part] = v
        return v

    def absent(self, part):
        if part in self.missing and part not in self.skip:
            return {"supported": None, "error": self.missing[part]}
        return {"supported": False}

    def caps(self, path):
        return hikswitch.model_caps(self.model, path)

    def rng(self, path, key, default=None):
        r = self.caps(path)["range"].get(key) or {}
        if r.get("min") is not None and r.get("max") is not None:
            return [_int(r["min"]), _int(r["max"])]
        return default


def _row_port(r, names, name_keys=("portName", "portAddress", "name")):
    for k in ("portID", "ID", "portNo"):
        n = _int(r.get(k))
        if n:
            return n
    for k in name_keys:
        v = r.get(k)
        if v is not None and str(v) in names:
            return names[str(v)]
    return None


def _port_list(status):
    out = []
    for r in sorted((r for r in status or [] if _int(r.get("ID")) is not None), key=lambda r: _int(r.get("ID"))):
        n = _int(r.get("ID"))
        up = _int(r.get("lnkSta")) == 1 and _on(r.get("en", 1))
        out.append({"id": _pid(n), "label": str(r.get("name") or ""), "up": up,
                    "speed": hikswitch.SPEED.get(_int(r.get("rate"))) if up else None,
                    "type": "sfp" if _int(r.get("lnkMode")) in (1, 4, 5) else "port"})
    return out


def _parse_time(v):
    try:
        return int(datetime.fromisoformat(str(v).strip().replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


def _vlan_ids(v):
    """A VLAN member list as the switch gives it: [1, 10], [{"VLANID": 1}], "1,10-12"."""
    out = []
    if isinstance(v, (int, float)):
        return [int(v)]
    if isinstance(v, list):
        for x in v:
            n = _int(x.get("VLANID") if isinstance(x, dict) else x)
            if n:
                out.append(n)
        return out
    for part in re.split(r"[,;\s]+", str(v or "")):
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2) or m.group(1))
            out += list(range(a, min(b, a + 64) + 1))
    return out


# each reader: (rd, ports, names) -> {"supported": ..., ...}
def _f_rstp(rd, ports, names):
    cfg = rd.get("stp.cfg", lambda s: s.attr("STPManage", "STPGlobalCfg"), ("STPManage", "STPGlobalCfg", "props"))
    if not isinstance(cfg, dict):
        return rd.absent("stp.cfg")
    on = _on(cfg.get("STPEnabled"))
    up = {p["id"]: p["up"] for p in ports}
    st = rd.get("stp.status", lambda s: s.attr("STPManage", "STPGlobalStatus"),
                ("STPManage", "STPGlobalStatus", "props")) if on else None
    pst = rd.get("stp.ports", lambda s: _search(s, "STPManage", "SearchSTPPortStatus", page=1000),
                 ("STPManage", "SearchSTPPortStatus", "actions")) if on else None
    pcfg = rd.get("stp.portcfg", lambda s: _search(s, "STPManage", "SearchSTPPortCfg", page=1000),
                  ("STPManage", "SearchSTPPortCfg", "actions"))
    st = st if isinstance(st, dict) else {}
    pmap, blocked = {}, []
    for r in pcfg or []:
        n = _row_port(r, names)
        if n:
            pmap.setdefault(_pid(n), {}).update(
                stp=_on(r.get("portEnabled", r.get("enabled", True))),
                priority=_int(r.get("portPriority", r.get("priority"))))
    for r in pst or []:
        n = _row_port(r, names)
        if not n:
            continue
        state, role = str(r.get("portStatus") or "").lower(), str(r.get("portRole") or "")
        pmap.setdefault(_pid(n), {}).update(role=role, state=state, cost=_int(r.get("pathCost")))
        if state in ("blocking", "discarding") and up.get(_pid(n)):
            blocked.append(_pid(n))
    bridge, root = str(st.get("bridgeID") or ""), str(st.get("rootBridgeID") or "")
    return {"supported": True, "enabled": on, "mode": str(cfg.get("STPMode") or "RSTP"),
            "priority": _int(cfg.get("sysPriority")), "hello_s": _int(cfg.get("helloTime")),
            "max_age_s": _int(cfg.get("maxAgingTime")), "fwd_delay_s": _int(cfg.get("forwardDelayTime")),
            "bridge_id": bridge, "root_id": root, "is_root": bool(bridge) and bridge == root,
            "ports": pmap, "blocked": blocked,
            "priority_range": rd.rng("STPManage/STPGlobalCfg", "sysPriority", [0, 61440])}


def _f_loop(rd, ports, names):
    cfg = rd.get("loop.cfg", lambda s: s.attr("STPManage", "PrivateLoopProtocolCfg"),
                 ("STPManage", "PrivateLoopProtocolCfg", "props"))
    if not isinstance(cfg, dict):
        return rd.absent("loop.cfg")
    on = _on(cfg.get("enabled"))
    blocked = []
    if on:
        rows = rd.get("loop.status", lambda s: _search(s, "STPManage", "SearchPortPrivateLoopStatus", page=1000),
                      ("STPManage", "SearchPortPrivateLoopStatus", "actions"))
        for r in rows or []:
            n = _row_port(r, names)
            if n and str(r.get("loopProtectionStatus") or "").lower() == "block":
                blocked.append(_pid(n))
    return {"supported": True, "enabled": on, "blocked": blocked}


def _f_erps(rd, ports, names):
    rows = rd.get("erps", lambda s: _search(s, "ERPSMgr", "SearchLoopParams", page=1), ("ERPSMgr", None, None))
    if rows is None:
        return rd.absent("erps")
    ring = next((r for r in rows if _int(r.get("loopID")) == 1), rows[0] if rows else {})
    sides = [x.get("portName") for x in (ring.get("port1"), ring.get("port2")) if isinstance(x, dict) and x.get("portName")]
    return {"supported": True, "enabled": _on(ring.get("ERPSEnabled")), "ports": sides}


def _f_storm(rd, ports, names):
    rows = rd.get("storm.ports", lambda s: _search(s, "PortCast", "SearchPortCastCfg", page=1000),
                  ("PortCast", "SearchPortCastCfg", "actions"))
    if rows is None:
        return rd.absent("storm.ports")
    g = rd.get("storm.global", lambda s: s.attr("PortCast", "PortStormGlobalCfg"),
               ("PortCast", "PortStormGlobalCfg", "props"))
    pmap = {}
    for r in rows:
        n = _row_port(r, names)
        if n:
            pmap[_pid(n)] = {"enabled": _on(r.get("enabled")), "type": str(r.get("stormControlType") or ""),
                             "limit_mbps": _num(r.get("limitRate"))}
    out = {"supported": True, "ports": pmap, "global": None}
    if isinstance(g, dict):
        c = rd.caps("PortCast/PortStormGlobalCfg")
        out["global"] = {"mode": str(g.get("castCtrlMode") or ""), "ratio_pct": _num(g.get("maxSpeedRatio")),
                         "modes": c["options"].get("castCtrlMode") or ["broadCast", "multiCast", "unknownUnicast"],
                         "ratio_range": rd.rng("PortCast/PortStormGlobalCfg", "maxSpeedRatio", [1, 100])}
    else:
        c = rd.caps("PortCast/ModifyPortCastCfgInfo")
        out["types"] = c["options"].get("stormControlType") or ["broadcast", "multicast", "unicast"]
        out["limit_range"] = rd.rng("PortCast/ModifyPortCastCfgInfo", "limitRate", [1, 1000])
    return out


def _f_rate(rd, ports, names):
    rows = rd.get("rate", lambda s: _search(s, "PortRate", "SearchPortRateCfg", page=1000),
                  ("PortRate", "SearchPortRateCfg", "actions"))
    if rows is None:
        return rd.absent("rate")
    pmap = {}
    for r in rows:
        n = _row_port(r, names)
        if n:
            pmap[_pid(n)] = {"rx_mbps": _num(r.get("rxLimitRate")) if _on(r.get("rxEnabled")) else None,
                             "tx_mbps": _num(r.get("txLimitRate")) if _on(r.get("txEnabled")) else None}
    return {"supported": True, "ports": pmap,
            "limit_range": rd.rng("PortRate/ModifyPortRateCfgInfo", "portRateList-rxLimitRate", [1, 1000])}


def _f_qos(rd, ports, names):
    cfg = rd.get("qos.cfg", lambda s: s.attr("NetworkQoS", "QoSCfg"), ("NetworkQoS", "QoSCfg", "props"))
    if not isinstance(cfg, dict):
        return rd.absent("qos.cfg")
    pr = rd.get("qos.ports", lambda s: s.attr("NetworkQoS", "QOSPortPriorityBatchCfg"),
                ("NetworkQoS", "QOSPortPriorityBatchCfg", "props"))
    c = rd.caps("NetworkQoS/QoSCfg")
    types = c["options"].get("QoSType") or []
    high = []
    for r in (pr.get("QOSPortPriorityList") or []) if isinstance(pr, dict) else []:
        n = _row_port(r, names) if isinstance(r, dict) else None
        if n and (_int(r.get("priority"), 0) or 0) > 0:
            high.append(_pid(n))
    on = _on(cfg.get("QoSEnabled"))
    return {"supported": True, "enabled": on, "type": str(cfg.get("QoSType") or "") if on else "",
            "algorithm": str(cfg.get("QoSAlgorithm") or ""), "high_weight": _int(cfg.get("highPriorityWeight")),
            "low_weight": _int(cfg.get("lowPriorityWeight")), "high_ports": high,
            "types": types, "algorithms": c["options"].get("QoSAlgorithm") or ["WRR", "SP"],
            "can_set": not types or "wholeQoS" in types,
            "port_range": rd.rng("NetworkQoS/QOSPortPriorityBatchCfg", "QOSPortPriorityList-portID")}


def _flag(part, group, name):
    def read(rd, ports, names):
        v = rd.get(part, lambda s: s.attr(group, name), (group, name, "props"))
        if not isinstance(v, dict):
            return rd.absent(part)
        return {"supported": True, "enabled": _on(v.get("enabled"))}
    return read


def _f_long(rd, ports, names):
    rows = rd.get("long.ports", lambda s: _search(s, "PortRemoteTrans", "SearchPortRemoteTrans", page=1000),
                  ("PortRemoteTrans", "SearchPortRemoteTrans", "actions"))
    if rows is None:
        return rd.absent("long.ports")
    g = rd.get("long.auto", lambda s: s.attr("PortRemoteTrans", "PortRemoteGlobalParams"),
               ("PortRemoteTrans", "PortRemoteGlobalParams", "props"))
    on = []
    for r in rows:
        n = _row_port(r, names)
        if n and _on(r.get("enabled")):
            on.append(_pid(n))
    return {"supported": True, "ports_on": on,
            "auto": _on(g.get("autoPortRemoteEnable")) if isinstance(g, dict) else None,
            "port_range": rd.rng("PortRemoteTrans/ModifyPortRemoteTrans", "portID")}


def _f_lldp(rd, ports, names):
    v = rd.get("lldp.cfg", lambda s: s.attr("LLDPMange", "LLDPCfg"), ("LLDPMange", "LLDPCfg", "props"))
    if not isinstance(v, dict):
        return rd.absent("lldp.cfg")
    on = _on(v.get("enabled"))
    nb = []
    if on:
        rows = rd.get("lldp.neighbors", lambda s: _search(s, "LLDPMange", "SearchLLDPNeighbor", page=500),
                      ("LLDPMange", "SearchLLDPNeighbor", "actions"))
        for r in (rows or [])[:200]:
            n = _row_port(r, names, name_keys=("portAddress", "portName"))
            nb.append({"port": _pid(n) if n else None, "port_label": str(r.get("portAddress") or r.get("portName") or ""),
                       "mac": hikswitch.normalize_mac(r.get("chassisID")) or str(r.get("chassisID") or ""),
                       "peer_port": str(r.get("neighborhoodPortID") or ""), "ip": str(r.get("peerIpv4Address") or ""),
                       "name": str(r.get("systemName") or "")})
    return {"supported": True, "enabled": on, "neighbors": nb}


def _f_dhcp(rd, ports, names):
    v = rd.get("dhcp.cfg", lambda s: s.attr("NetworkSecurityProtection", "DHCPSnoopingCfg"),
               ("NetworkSecurityProtection", "DHCPSnoopingCfg", "props"))
    if not isinstance(v, dict):
        return rd.absent("dhcp.cfg")
    on = _on(v.get("enabled"))
    trusted, listed = [], []
    for r in v.get("trustPortList") or []:
        n = _row_port(r, names) if isinstance(r, dict) else None
        if n:
            listed.append(n)
            if _on(r.get("enabled")):
                trusted.append(_pid(n))
    entries = []
    if on:
        rows = rd.get("dhcp.entries", lambda s: _search(s, "NetworkSecurityProtection", "SearchDHCPSnoopingEntry", page=500),
                      ("NetworkSecurityProtection", "SearchDHCPSnoopingEntry", "actions"))
        for r in (rows or [])[:200]:
            n = _row_port(r, names)
            entries.append({"port": _pid(n) if n else None, "ip": str(r.get("ipv4Address") or ""),
                            "mac": hikswitch.normalize_mac(r.get("macAddress")), "vlan": _int(r.get("VLANID")),
                            "lease_s": _int(r.get("leaseTimeRemaining"))})
    return {"supported": True, "enabled": on, "trusted": trusted, "entries": entries, "_listed": listed}


def _f_isolation(rd, ports, names):
    v = rd.get("isolation", lambda s: s.svc("PortIsolationGroup", "GetIsolationGroupList", {"isolationGroupID": 1}),
               ("PortIsolationGroup", "GetIsolationGroupList", "actions"))
    if not isinstance(v, dict):
        return rd.absent("isolation")
    grp = next((g for g in v.get("isolationGroupList") or []
                if isinstance(g, dict) and _int(g.get("isolationGroupID")) == 1), {})
    members = []
    for r in grp.get("portList") or []:
        n = _row_port(r, names) if isinstance(r, dict) else None
        if n:
            members.append(_pid(n))
    return {"supported": True, "ports": members,
            "port_range": rd.rng("PortIsolationGroup/ModifyIsolationGroup", "portList-portID")}


def _f_access(rd, ports, names):
    def one(part, group, name):
        v = rd.get(part, lambda s: s.attr(group, name), (group, name, "props"))
        return v if isinstance(v, dict) else None
    t = one("telnet", "SystemMaintenance", "TelnetServerCfg")
    ssh = one("ssh", "SystemMaintenance", "SSHCfg")
    http = one("http", "HTTPAccess", "HTTPAccessCfg")
    https = one("https", "HTTPAccess", "HTTPSAccessCfg")
    sadp = one("sadp", "SADPMgr", "SADPCfg")
    onvif = one("onvif", "DeviceNetworkDiscovery", "ONVIFDiscoveryParams")
    nm = one("netmanage", "NetworkFilter", "DevNetManageFunctionSwitch")
    sub = lambda d, k: d.get(k) if isinstance(d, dict) and isinstance(d.get(k), dict) else None  # noqa: E731
    return {"supported": True,
            "telnet": _on(t.get("enabled")) if t else None,
            "ssh": _on(ssh.get("enabled")) if ssh else None,
            "http_port": _int(http.get("portNo")) if http else None,
            "https": _on(https.get("enabled")) if https and "enabled" in https else None,
            "https_port": _int(https.get("portNo")) if https else None,
            "https_redirect": _on(https.get("redirectEnabled")) if https and "redirectEnabled" in https else None,
            "sadp_server": _on(sub(sadp, "SADPDevive").get("enabled")) if sub(sadp, "SADPDevive") else None,
            "sadp_agent": _on(sub(sadp, "SADPClient").get("enabled")) if sub(sadp, "SADPClient") else None,
            "onvif": _on(onvif.get("enabled")) if onvif else None,
            "net_manage": _on(nm.get("netManageEnabled")) if nm else None,
            "cloud": _on(rd.summ.get("EZEnabled")) if "EZEnabled" in rd.summ else None}


def _f_time(rd, ports, names):
    v = rd.get("time", lambda s: s.attr("TimeMgr", "SystemDateTime"), ("TimeMgr", "SystemDateTime", "props"))
    if not isinstance(v, dict):
        return rd.absent("time")
    ntp = v.get("NTPServer") if isinstance(v.get("NTPServer"), dict) else {}
    if str(ntp.get("addressingFormatType") or "") == "hostname":
        server = str(ntp.get("hostName") or "")
    else:
        server = str(ntp.get("ipV4Address") or ntp.get("hostName") or "")
    ts = _parse_time(v.get("time"))
    modes = rd.caps("TimeMgr/SystemDateTime")["options"].get("dateTimeType") or []
    return {"supported": True, "mode": str(v.get("dateTimeType") or ""), "time": str(v.get("time") or ""),
            "drift_s": round(ts - time.time()) if ts else None,
            "ntp_server": server, "ntp_port": _int(ntp.get("portNo")), "interval_min": _int(ntp.get("synchronizeInterval")),
            "can_ntp": not modes or "NTP" in modes,
            "interval_range": rd.rng("TimeMgr/SystemDateTime", "NTPServer-synchronizeInterval", [1, 1440])}


def _f_cable(rd, ports, names):
    has = hikswitch.model_has(rd.model, "PortMgr", "GetPortLineStatus", "actions")
    if has is False:
        return {"supported": False}
    return {"supported": True if has else None, "port_range": rd.rng("PortMgr/GetPortLineStatus", "portID")}


def _f_sfp(rd, ports, names):
    has = hikswitch.model_has(rd.model, "PortMgr", "GetOpticalModulesDiagnosticInfo", "actions")
    if has is False:
        return {"supported": False}
    rng = rd.rng("PortMgr/GetOpticalModulesDiagnosticInfo", "portID")
    mods = {}
    for p in ports:
        n = _port_no(p["id"])
        if p["type"] != "sfp" or not p["up"] or (rng and not rng[0] <= n <= rng[1]):
            continue
        part = f"sfp.{n}"
        v = rd.get(part, lambda s, n=n: s.svc("PortMgr", "GetOpticalModulesDiagnosticInfo", {"portID": n}))
        if isinstance(v, dict):
            rt = v.get("realtimeParam") if isinstance(v.get("realtimeParam"), dict) else {}
            ti = v.get("transceiverInfo") if isinstance(v.get("transceiverInfo"), dict) else {}
            mods[p["id"]] = {"temp_c": _num(rt.get("temp")), "voltage_v": _num(rt.get("voltage")),
                             "bias_ma": _num(rt.get("biasCurrent")), "rx_dbm": _num(rt.get("receivedPower")),
                             "tx_dbm": _num(rt.get("transmitPower")), "vendor": str(ti.get("vendorName") or ""),
                             "type": str(ti.get("transceiverType") or ""), "connector": str(ti.get("connectorType") or ""),
                             "wavelength_nm": _num(ti.get("wavelength")),
                             "thresholds": v.get("paramThreshold") if isinstance(v.get("paramThreshold"), dict) else {}}
        elif part in rd.missing:
            mods[p["id"]] = {"error": rd.missing[part]}
    return {"supported": True if has else None, "modules": mods}


def _f_vlan(rd, ports, names):
    rows = rd.get("vlan.ports", lambda s: _search(s, "L2Mgr", "SearchPortVLAN", page=500),
                  ("L2Mgr", "SearchPortVLAN", "actions"))
    if rows is None:
        return rd.absent("vlan.ports")
    pmap, ids = {}, set()
    for r in rows:
        n = _row_port(r, names)
        if not n:
            continue
        t = str(r.get("VLANType") or "").lower()
        mode = r.get(f"{t}Mode") if isinstance(r.get(f"{t}Mode"), dict) else {}
        if t == "access":
            pvid = _int(mode.get("VLANID"))
            members = [pvid] if pvid else []
        else:
            pvid = _int(mode.get("pVid"))
            members = _vlan_ids(mode.get("VLANIDList") if t == "trunk" else mode.get("VLANID"))
        pmap[_pid(n)] = {"type": "hybrid" if t == "hybird" else t, "pvid": pvid, "vlans": members[:64]}
        ids.update(members)
    return {"supported": True, "ports": pmap, "ids": sorted(ids)[:200]}


def _f_lag(rd, ports, names):
    rows = rd.get("lag", lambda s: _search(s, "L2Mgr", "SearchAggregatedLink", page=10),
                  ("L2Mgr", "SearchAggregatedLink", "actions"))
    if rows is None:
        return rd.absent("lag")
    groups = []
    for r in rows:
        members = []
        for x in r.get("groupPortList") or []:
            n = _row_port(x, names) if isinstance(x, dict) else None
            if n:
                members.append(_pid(n))
        if members:
            groups.append({"id": _int(r.get("aggLinkID")), "ports": members})
    return {"supported": True, "groups": groups}


def _f_mirror(rd, ports, names):
    v = rd.get("mirror", lambda s: s.svc("MirrorGroup", "GetMirrorGroup", {"mirroringGroupID": 1}),
               ("MirrorGroup", "GetMirrorGroup", "actions"))
    if not isinstance(v, dict):
        return rd.absent("mirror")
    g = next((x for x in v.get("mirroringGroupList") or []
              if isinstance(x, dict) and _int(x.get("mirroringGroupID")) == 1), {})
    dest = _int(g.get("destinationPortNo"))
    src = []
    for x in g.get("sourcePortInfo") or []:
        n = _row_port(x, names) if isinstance(x, dict) else None
        if n:
            src.append(_pid(n))
    return {"supported": True, "enabled": bool(dest and src), "to": _pid(dest) if dest else None, "from": src}


def _f_portsec(rd, ports, names):
    v = rd.get("portsec", lambda s: s.attr("NetworkSecurityProtection", "PortSecurity"),
               ("NetworkSecurityProtection", "PortSecurity", "props"))
    if not isinstance(v, dict):
        return rd.absent("portsec")
    return {"supported": True, "enabled": _on(v.get("enabled")), "mode": str(v.get("portSecurityType") or "")}


def _f_other(rd, ports, names):
    """Settings this switch has that Netwatch only names (set them on its own page)."""
    m = rd.model
    return {"supported": m is not None,
            "acl": hikswitch.model_has(m, "ACLMgr"),
            "ip_source_guard": hikswitch.model_has(m, "NetworkSecurityProtection", "AddIPSGAddressCheck", "actions"),
            "arp_protection": hikswitch.model_has(m, "NetworkSecurityProtection", "GetARPGatewayProtectionEntry", "actions"),
            "dscp": hikswitch.model_has(m, "NetworkQoS", "DSCPGlobalParams", "props"),
            "power_rules": hikswitch.model_has(m, "PowerMgr", "SearchBasicPowerSavingRuleList", "actions")}


_READERS = {
    "rstp": _f_rstp, "loop_guard": _f_loop, "erps": _f_erps, "storm": _f_storm, "rate_limit": _f_rate,
    "qos": _f_qos, "poe_watchdog": _flag("watchdog", "POE", "PoEWatchdogParams"), "long_range": _f_long,
    "eee": _flag("eee", "PowerMgr", "PowerSavingModeParam"), "lldp": _f_lldp, "dhcp_snooping": _f_dhcp,
    "isolation": _f_isolation, "access": _f_access, "time": _f_time, "cable_test": _f_cable, "sfp": _f_sfp,
    "vlan": _f_vlan, "lag": _f_lag, "mirror": _f_mirror, "port_security": _f_portsec, "other": _f_other,
}


def _open(s):
    summ = s.svc("PortMgr", "GetSwitchSumInfo") or {}
    status = s.search("PortMgr", "SearchPortStatus", page=1000)
    model = hikswitch.load_model(s, summ.get("softwareVersion"))
    ports = _port_list(status)
    names = {p["label"]: _port_no(p["id"]) for p in ports if p["label"]}
    return summ, model, ports, names


def read_advanced(ip, username, password, keep_raw=False):
    """Every advanced setting the switch has. Never raises: {ok: False, error, kind}."""
    t0 = time.time()
    try:
        with Session(ip, username, password, timeout=(5, 20)) as s:
            summ, model, ports, names = _open(s)
            rd = _Reader(s, model, summ)
            feats = {k: _READERS[k](rd, ports, names) for k in FEATURES}
    except HikSwitchError as e:
        return {"ok": False, "error": str(e), "kind": e.kind, "ip": ip}
    out = {"ok": True, "ts": int(time.time()), "ip": ip, "read_s": round(time.time() - t0, 1),
           "model": str(summ.get("model") or ""), "firmware": str(summ.get("softwareVersion") or ""),
           "model_known": model is not None, "ports": ports, "features": feats}
    if rd.missing:
        out["missing"] = rd.missing
    if rd.skip:
        out["not_supported"] = sorted(rd.skip)
    if keep_raw:
        out["raw"] = rd.raw
    return out


# ---- changing ---------------------------------------------------------------------------
class Refused(ValueError):
    """A change Netwatch will not send (bad value, or the switch lacks the setting)."""


def _bool(body, key):
    if key not in body or not isinstance(body[key], bool):
        raise Refused(f"say {key}: true or false")
    return body[key]


def _ports_arg(body, ports, key="ports", rng=None, required=True):
    """["0/3", …] -> [3, …], each checked against the switch's own port list."""
    raw = body.get(key)
    if raw is None and not required:
        return []
    if not isinstance(raw, list) or (required and not raw):
        raise Refused("which ports? (a list like [\"0/3\"])")
    have = {_port_no(p["id"]) for p in ports}
    out = []
    for pid in raw:
        n = _port_no(pid)
        if n is None or n not in have:
            raise Refused(f"port {pid} not found on the switch")
        if rng and not rng[0] <= n <= rng[1]:
            raise Refused(f"port {pid} doesn't have this setting (only ports {rng[0]}–{rng[1]} do)")
        if n not in out:
            out.append(n)
    return sorted(out)


def _need(before, what):
    if not before.get("supported"):
        raise Refused(f"this switch has no {what}" + (f" ({before['error']})" if before.get("error") else ""))


def _in_range(v, rng, what):
    n = _int(v)
    if n is None or (rng and not rng[0] <= n <= rng[1]):
        raise Refused(f"{what} must be {rng[0]}–{rng[1]}" if rng else f"{what} must be a number")
    return n


def _set_rstp(s, body, before, ports):
    _need(before, "spanning tree (STP/RSTP)")
    want = _bool(body, "enabled")
    data = {"STPEnabled": want, "STPMode": "RSTP"}       # what the switch's page sends
    if want:
        cur = s.attr("STPManage", "STPGlobalCfg") or {}
        for k, dflt in STP_DEFAULTS:
            v = _int(cur.get(k))
            data[k] = v if v is not None else dflt
        if body.get("priority") is not None:
            p = _in_range(body["priority"], before.get("priority_range") or [0, 61440], "bridge priority")
            if p % 4096:
                raise Refused("bridge priority goes in steps of 4096 (0, 4096, 8192 … 61440)")
            data["sysPriority"] = p
    # Ports re-learn while spanning tree starts (up to 2 × forward delay): wait for the answer.
    s.set_attr("STPManage", "STPGlobalCfg", data, timeout=(5, 60))


def _set_flag(group, name, what):
    def setter(s, body, before, ports):
        _need(before, what)
        s.set_attr(group, name, {"enabled": _bool(body, "enabled")})
    return setter


def _set_storm(s, body, before, ports):
    _need(before, "storm control")
    g = before.get("global")
    if "mode" in body or "ratio_pct" in body:
        if not g:
            raise Refused("this switch sets the storm control limit per port")
        mode = str(body.get("mode") or g.get("mode") or "")
        if mode not in (g.get("modes") or []):
            raise Refused(f"storm control type must be one of {', '.join(g.get('modes') or [])}")
        ratio = _in_range(body.get("ratio_pct", g.get("ratio_pct")), g.get("ratio_range") or [1, 100], "limit (%)")
        s.set_attr("PortCast", "PortStormGlobalCfg", {"castCtrlMode": mode, "maxSpeedRatio": ratio})
    plist = _ports_arg(body, ports, required="mode" not in body and "ratio_pct" not in body)
    if not plist:
        return
    on = _bool(body, "enabled")
    if g:
        items = [{"portID": n, "enabled": on} for n in plist]
    else:
        items = []
        typ = str(body.get("type") or "broadcast")
        if on and typ not in (before.get("types") or []):
            raise Refused(f"storm control type must be one of {', '.join(before.get('types') or [])}")
        lim = _in_range(body.get("limit_mbps", 10), before.get("limit_range") or [1, 1000], "limit (Mbit/s)") if on else None
        for n in plist:
            it = {"portID": n, "enabled": on}
            if on:
                it.update(stormControlType=typ, limitRate=lim)
            items.append(it)
    s.svc("PortCast", "ModifyPortCastCfgInfo", items)


def _set_rate(s, body, before, ports):
    _need(before, "bandwidth limits")
    plist = _ports_arg(body, ports)
    rng = before.get("limit_range") or [1, 1000]

    def lim(k):
        v = body.get(k)
        return None if v in (None, 0, "", "0") else _in_range(v, rng, "limit (Mbit/s)")
    rx, tx = lim("rx_mbps"), lim("tx_mbps")
    items = []
    for n in plist:
        it = {"portID": n, "rxEnabled": rx is not None, "txEnabled": tx is not None}
        if rx is not None:
            it["rxLimitRate"] = rx
        if tx is not None:
            it["txLimitRate"] = tx
        items.append(it)
    s.svc("PortRate", "ModifyPortRateCfgInfo", {"portRateList": items})


def _set_qos(s, body, before, ports):
    _need(before, "QoS")
    if not before.get("can_set"):
        raise Refused("this switch only has its fixed high-priority port mode — set it on the switch's own page")
    if "enabled" in body:
        if _bool(body, "enabled"):
            alg = str(body.get("algorithm") or before.get("algorithm") or "WRR")
            if alg not in (before.get("algorithms") or ["WRR", "SP"]):
                raise Refused("QoS algorithm must be WRR or SP")
            data = {"QoSEnabled": True, "QoSType": "wholeQoS", "QoSAlgorithm": alg}
            if alg == "WRR":
                data["lowPriorityWeight"] = _in_range(body.get("low_weight", before.get("low_weight") or 1), [1, 16], "low weight")
                data["highPriorityWeight"] = _in_range(body.get("high_weight", before.get("high_weight") or 8), [1, 16], "high weight")
        else:
            data = {"QoSEnabled": False}
        s.set_attr("NetworkQoS", "QoSCfg", data)
    plist = _ports_arg(body, ports, rng=before.get("port_range"), required="enabled" not in body)
    if plist:
        high = _bool(body, "high")
        s.set_attr("NetworkQoS", "QOSPortPriorityBatchCfg",
                   {"QOSPortPriorityList": [{"portID": n, "priority": 1 if high else 0} for n in plist]})


def _set_long(s, body, before, ports):
    _need(before, "long-range ports")
    if "auto" in body:
        if before.get("auto") is None:
            raise Refused("this switch has no automatic long-range mode")
        s.set_attr("PortRemoteTrans", "PortRemoteGlobalParams", {"autoPortRemoteEnable": _bool(body, "auto")})
        return
    if before.get("auto"):
        raise Refused("automatic long-range mode is on — turn it off to choose the ports yourself")
    plist = _ports_arg(body, ports, rng=before.get("port_range"))
    on = _bool(body, "enabled")
    s.svc("PortRemoteTrans", "ModifyPortRemoteTrans", [{"portID": n, "enabled": on} for n in plist])


def _set_dhcp(s, body, before, ports):
    _need(before, "DHCP snooping")
    if not _bool(body, "enabled"):
        s.set_attr("NetworkSecurityProtection", "DHCPSnoopingCfg", {"enabled": False})
        return
    trusted = set(_ports_arg(body, ports, key="trusted"))
    # the ports the switch itself lists, each with its trust flag (all ports if it lists none)
    every = sorted(set(before.get("_listed") or []) or {_port_no(p["id"]) for p in ports})
    if not trusted <= set(every):
        raise Refused("the switch can't trust port " + ", ".join(str(n) for n in sorted(trusted - set(every))))
    s.set_attr("NetworkSecurityProtection", "DHCPSnoopingCfg",
               {"enabled": True, "trustPortList": [{"portID": n, "enabled": n in trusted} for n in every]})


def _set_isolation(s, body, before, ports):
    _need(before, "port isolation")
    plist = _ports_arg(body, ports, rng=before.get("port_range"))
    cur = {_port_no(p) for p in before.get("ports") or []}
    new = cur | set(plist) if _bool(body, "isolated") else cur - set(plist)
    s.svc("PortIsolationGroup", "ModifyIsolationGroup",
          {"isolationGroupID": 1, "portList": [{"portID": n} for n in sorted(new)]})


def _set_access(field, group, name, wrap=None):
    def setter(s, body, before, ports):
        if before.get(field) is None:
            raise Refused("this switch doesn't have that setting")
        on = _bool(body, "enabled")
        s.set_attr(group, name, wrap(on) if wrap else {"enabled": on})
    return setter


_HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")


def _set_ntp(s, body, before, ports):
    _need(before, "clock settings")
    if not before.get("can_ntp"):
        raise Refused("this switch can't take its time from NTP")
    server = str(body.get("server") or "").strip()
    srv = {"portNo": 123}
    try:
        ipaddress.IPv4Address(server)
        srv.update(addressingFormatType="ipaddress", ipV4Address=server)
    except ValueError:
        if not _HOST_RE.match(server):
            raise Refused("NTP server: an IPv4 address or a host name like pool.ntp.org")
        srv.update(addressingFormatType="hostname", hostName=server)
    srv["synchronizeInterval"] = _in_range(body.get("interval_min", before.get("interval_min") or 60),
                                           before.get("interval_range") or [1, 1440], "sync interval (minutes)")
    s.set_attr("TimeMgr", "SystemDateTime", {"dateTimeType": "NTP", "NTPServer": srv})


# feature -> (the read that shows it, the change)
_SETTERS = {
    "rstp": ("rstp", _set_rstp),
    "loop_guard": ("loop_guard", _set_flag("STPManage", "PrivateLoopProtocolCfg", "loop protection")),
    "storm": ("storm", _set_storm),
    "rate_limit": ("rate_limit", _set_rate),
    "qos": ("qos", _set_qos),
    "poe_watchdog": ("poe_watchdog", _set_flag("POE", "PoEWatchdogParams", "PoE watchdog")),
    "long_range": ("long_range", _set_long),
    "eee": ("eee", _set_flag("PowerMgr", "PowerSavingModeParam", "EEE / power saving")),
    "lldp": ("lldp", _set_flag("LLDPMange", "LLDPCfg", "LLDP")),
    "dhcp_snooping": ("dhcp_snooping", _set_dhcp),
    "isolation": ("isolation", _set_isolation),
    "telnet": ("access", _set_access("telnet", "SystemMaintenance", "TelnetServerCfg")),
    "onvif": ("access", _set_access("onvif", "DeviceNetworkDiscovery", "ONVIFDiscoveryParams")),
    "sadp_agent": ("access", _set_access("sadp_agent", "SADPMgr", "SADPCfg", lambda on: {"SADPClient": {"enabled": on}})),
    "ntp": ("time", _set_ntp),
}
SETTINGS = tuple(_SETTERS) + ("counters",)


def set_feature(ip, username, password, feature, body):
    """Change ONE setting; answers {ok, feature, shows, before, after} where before and
    after are that setting as read_advanced() shows it, read in the same login."""
    body = body if isinstance(body, dict) else {}
    if feature == "counters":                    # the switch's own "clear statistics"
        try:
            with Session(ip, username, password) as s:
                s.svc("PortMgr", "ResetAllPortStatistics", {})
            return {"ok": True, "feature": feature}
        except HikSwitchError as e:
            return {"ok": False, "error": str(e), "kind": e.kind}
    if feature not in _SETTERS:
        return {"ok": False, "error": f"unknown setting {feature!r}"}
    shows, change = _SETTERS[feature]
    long_wait = feature == "rstp"
    try:
        with Session(ip, username, password, timeout=(5, 60 if long_wait else 20)) as s:
            summ, model, ports, names = _open(s)
            before = _READERS[shows](_Reader(s, model, summ), ports, names)
            change(s, body, before, ports)
            after = _READERS[shows](_Reader(s, model, summ), ports, names)
        return {"ok": True, "feature": feature, "shows": shows, "before": before, "after": after}
    except Refused as e:
        return {"ok": False, "error": str(e), "refused": True}
    except HikSwitchError as e:
        return {"ok": False, "error": str(e), "kind": e.kind}


# ---- what Netwatch suggests -----------------------------------------------------------------
CABLE_SYMPTOMS = ("port_errors", "port_flapping", "speed_drop", "half_duplex")


def advice(adv, protected=None, uplinks=None, problems=None, dhcp_ports=None, snap_ports=None):
    """Netwatch's suggestions for one switch, most important first. Each one names
    the setting and, where one click does it, the change (a set_feature body).

    protected / uplinks: {port_id: …} from the switch monitor (the Pi's and the
    router's ports; ports with many devices behind them). problems: the monitor's
    problems for this switch. dhcp_ports: ports a legitimate DHCP server is on.
    snap_ports: the monitor's ports (PoE draw)."""
    f = (adv or {}).get("features") or {}
    protected, uplinks = protected or {}, uplinks or {}
    out = []

    def add(feature, level, title, why, change=None):
        out.append({"feature": feature, "level": level, "title": title, "why": why, "change": change})

    ports = (adv or {}).get("ports") or []
    edge = [p["id"] for p in ports if p["up"] and p["id"] not in protected and p["id"] not in uplinks]
    lg, st = f.get("loop_guard") or {}, f.get("rstp") or {}
    if lg.get("supported") and not lg.get("enabled"):
        add("loop_guard", "do", "Turn on loop protection",
            "If someone plugs both ends of a cable into this network (a loop), every broadcast circles for ever "
            "and the whole site stops. Loop protection notices and blocks just the looping port.",
            {"enabled": True})
    if st.get("supported") and not st.get("enabled"):
        add("rstp", "do" if (uplinks or not lg.get("supported")) else "consider", "Turn on RSTP (spanning tree)",
            "RSTP lets switches agree on one path: a second cable between two switches becomes a standby link "
            "instead of a broadcast storm. While it starts, ports re-learn for up to 30 seconds.",
            {"enabled": True})
    if st.get("enabled") and st.get("blocked"):
        add("rstp", "info", f"Spanning tree is blocking {', '.join('port ' + p.split('/')[-1] for p in st['blocked'])}",
            "That is RSTP doing its job: there are two paths, and one waits as a standby. "
            "If you didn't mean to have two cables, find and remove the second one.")
    wd = f.get("poe_watchdog") or {}
    powered = any((p.get("poe_w") or 0) >= 0.5 for p in snap_ports or [])
    if wd.get("supported") and not wd.get("enabled") and (powered or snap_ports is None):
        add("poe_watchdog", "do", "Turn on the PoE watchdog",
            "A camera that hangs keeps drawing power but stops sending video. The watchdog notices and "
            "power-cycles that port by itself, day or night.", {"enabled": True})
    storm = f.get("storm") or {}
    if storm.get("supported") and edge:
        off = [p for p in edge if not ((storm.get("ports") or {}).get(p) or {}).get("enabled")]
        if off:
            add("storm", "consider", f"Turn on storm control on {len(off)} camera/device port{'s' if len(off) != 1 else ''}",
                "A faulty device or a loop behind one port then can't flood the rest of the network with broadcasts.",
                {"ports": off, "enabled": True})
    errs = [p for p in problems or [] if p.get("metric") in CABLE_SYMPTOMS]
    cable = f.get("cable_test") or {}
    for pid in list(dict.fromkeys(p.get("port") for p in errs if p.get("port")))[:3]:
        pr = next(p for p in errs if p.get("port") == pid)
        if cable.get("supported") is not False and pid not in protected:
            add("cable_test", "do", f"Run a cable test on port {str(pid).split('/')[-1]}",
                f"{pr.get('what') or 'This port has errors'} — the cable test says if a pair is broken, short or "
                "badly crimped, and roughly where. The link drops for a few seconds while it measures.",
                {"port": pid})
    eee = f.get("eee") or {}
    if eee.get("enabled") and errs:
        add("eee", "consider", "Turn off EEE (energy saving)",
            "Energy-efficient Ethernet puts quiet links to sleep; some cameras and radios drop or count errors "
            "when it does. With errors on this switch, switch it off and see if they stop.", {"enabled": False})
    acc = f.get("access") or {}
    if acc.get("telnet"):
        add("telnet", "do", "Turn Telnet off",
            "Telnet sends the switch's password across the network in plain text, and nothing here needs it.",
            {"enabled": False})
    dh = f.get("dhcp_snooping") or {}
    trusted = sorted(set(dhcp_ports or []) | {p for p, why in protected.items() if "router" in str(why)})
    if dh.get("supported") and not dh.get("enabled") and trusted:
        add("dhcp_snooping", "consider", "Turn on DHCP snooping",
            "Only " + ", ".join("port " + p.split("/")[-1] for p in trusted) + " may then hand out addresses: a "
            "home router or phone hotspot plugged in by mistake can't knock cameras off the network.",
            {"enabled": True, "trusted": trusted})
    ld = f.get("lldp") or {}
    if ld.get("supported") and not ld.get("enabled"):
        add("lldp", "consider", "Turn on LLDP",
            "Switches, routers and many cameras then announce themselves, so you can see what is on each port.",
            {"enabled": True})
    if acc.get("sadp_agent"):
        add("sadp_agent", "consider", "Turn off the SADP agent",
            "It broadcasts a discovery packet to the whole network every minute to draw Hikvision's own topology; "
            "Netwatch maps the network without it.", {"enabled": False})
    tm = f.get("time") or {}
    if tm.get("supported") and tm.get("can_ntp") and (tm.get("mode") != "NTP" or abs(tm.get("drift_s") or 0) > 120):
        add("ntp", "consider", "Set the switch's clock by NTP",
            "With the right time, the switch's log lines up with the cameras and the recorder when you look into a fault.")
    order = {"do": 0, "consider": 1, "info": 2}
    out.sort(key=lambda a: order.get(a["level"], 3))
    return out
