"""Scan engine: runs nmap across one or more targets (local + remote), parses the
results, identifies devices, records uptime history, and fires ntfy alerts.

Local segments are discovered via ARP (nmap reports MACs); remote/routed subnets
are discovered via ICMP/TCP probes (no MAC available at L3 — identification then
falls back to hostname + open ports + HTTP banner).
"""
import concurrent.futures
import ipaddress
import json
import os
import re
import subprocess
import threading
import time
import xml.etree.ElementTree as ET

import config
import creds
import discovery
import hikvision
import history
import hubvpn
import identify
import kuma
import notify

DATA_DIR = os.environ.get("NETWATCH_DATA", "/data")
STATE_PATH = os.path.join(DATA_DIR, "scan_state.json")
REGISTRY_PATH = os.path.join(DATA_DIR, "devices.json")
SEED_PATH = os.path.join(os.path.dirname(__file__), "defaults", "devices.json")

QUICK_PORTS = "22,53,80,443,515,554,631,1883,2000,5000,5060,8000,8080,8291,8443,9000,9100"

# Two distinct MACs both seen at one IP within this window = a live address
# conflict (e.g. a Wi-Fi bridge and a camera sharing an IP). Old stale records
# whose IP was later reused by a different device fall outside the window and
# are not flagged.
CONFLICT_WINDOW_S = 24 * 3600

# Plaintext / remote-admin ports worth flagging as a security problem.
RISKY_PORTS = {21: "FTP", 23: "Telnet", 2323: "Telnet (alt)",
               512: "rexec", 513: "rlogin", 514: "rsh"}


def _now_str():
    return time.strftime("%H:%M:%S")


def _icmp_up(ip):
    """True if the host answers ICMP. Two packets with a 2s wait so a single
    dropped packet on a busy wireless link doesn't read as 'down' — real
    reachability, unlike an ARP reply a sleeping NIC still sends."""
    try:
        return subprocess.run(["ping", "-c", "2", "-W", "2", ip],
                              capture_output=True, timeout=8).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _icmp_sweep_alive(ip):
    """One ICMP echo with a generous 2s wait, for the subnet sweep that catches
    high-latency wireless hosts nmap's ARP discovery misses on a busy /24."""
    try:
        return subprocess.run(["ping", "-c", "1", "-W", "2", ip],
                              capture_output=True, timeout=4).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


_RTT_RE = re.compile(r"time[=<]([\d.]+)\s*ms")


def _icmp_rtt(ip):
    """(up, rtt_ms) from a single ICMP echo — for the latency heartbeat sampler."""
    try:
        r = subprocess.run(["ping", "-c", "1", "-W", "1", ip],
                           capture_output=True, text=True, timeout=4)
    except (OSError, subprocess.SubprocessError):
        return False, None
    if r.returncode != 0:
        return False, None
    m = _RTT_RE.search(r.stdout)
    return True, (round(float(m.group(1)), 1) if m else None)


def _dns_ok(host="google.com"):
    """True if `host` resolves — confirms DNS works, not just raw IP reachability."""
    import socket
    try:
        socket.setdefaulttimeout(3)
        socket.getaddrinfo(host, None)
        return True
    except OSError:
        return False


def default_gateway():
    """The site's default-route gateway IP (via `ip route show default`), or None."""
    try:
        out = subprocess.run(["ip", "route", "show", "default"],
                            capture_output=True, text=True, timeout=4).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"via\s+([0-9.]+)", out)
    return m.group(1) if m else None


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


class Scanner:
    def __init__(self):
        self.lock = threading.Lock()
        self.status = {
            "is_scanning": False,
            "mode": None,
            "target": None,
            "progress": "",
            "last_scan": "--:--:--",
            "last_scan_ts": None,
        }
        self.devices = {}          # key -> live device record
        self.miss = {}             # key -> consecutive missed scans
        self.seen_keys = set()     # every key ever observed (for "new device")
        self.mac_multi_ip = {}     # mac -> [ips] when one MAC answers on 2+ IPs (last scan)
        self.registry = self._load_registry()
        self._wake = threading.Event()
        self._hb_wake = threading.Event()
        self._hub_up = None        # last-known hub VPN link state (for alerts)
        self._stop = False
        self._scan_lock = threading.Lock()   # serialise scans (no concurrent runs)
        self._load_state()

    # ---- persistence ---------------------------------------------------
    def _load_registry(self):
        reg = _read_json(REGISTRY_PATH, None)
        if reg is None:
            reg = _read_json(SEED_PATH, {})
            _write_json(REGISTRY_PATH, reg)
        return reg

    def save_registry(self):
        _write_json(REGISTRY_PATH, self.registry)

    def _load_state(self):
        st = _read_json(STATE_PATH, {})
        self.devices = st.get("devices", {})
        self.miss = st.get("miss", {})
        self.seen_keys = set(st.get("seen_keys", []))
        self.status["last_scan"] = st.get("last_scan", "--:--:--")
        self.status["last_scan_ts"] = st.get("last_scan_ts")

    def _save_state(self):
        _write_json(STATE_PATH, {
            "devices": self.devices,
            "miss": self.miss,
            "seen_keys": sorted(self.seen_keys),
            "last_scan": self.status["last_scan"],
            "last_scan_ts": self.status["last_scan_ts"],
        })

    # ---- network helpers ----------------------------------------------
    def local_networks(self):
        nets = []
        try:
            out = subprocess.run(["ip", "-o", "-4", "addr", "show"],
                                 capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                parts = line.split()
                for i, tok in enumerate(parts):
                    if tok == "inet" and i + 1 < len(parts):
                        try:
                            nets.append(ipaddress.ip_network(parts[i + 1], strict=False))
                        except ValueError:
                            pass
        except (subprocess.SubprocessError, OSError):
            pass
        return nets

    def _is_local(self, cidr, target_flag):
        if target_flag in (True, False):
            return target_flag
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            return False
        return any(net.overlaps(n) for n in self.local_networks() if not n.is_loopback)

    def _is_local_ip(self, ip):
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in n for n in self.local_networks() if not n.is_loopback)

    def _target_for_ip(self, ip, cfg):
        """Which configured target CIDR an IP belongs to (for grouping); /32 fallback."""
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return ip
        for t in cfg["targets"]:
            try:
                if addr in ipaddress.ip_network(t["cidr"], strict=False):
                    return t["cidr"]
            except ValueError:
                pass
        return f"{ip}/32"

    # ---- nmap ----------------------------------------------------------
    def _nmap(self, targets, mode, is_local):
        """`targets` is a list of nmap target tokens (a CIDR, or several IPs)."""
        if mode == "deep":
            args = ["nmap", "-n", "-T4", "-p-", "-sV", "-O", "--osscan-guess",
                    "--max-retries", "2", "-oX", "-"] + list(targets)
        else:
            args = ["nmap", "-n", "-T4", "-p", QUICK_PORTS, "-oX", "-"] + list(targets)
        if not is_local:
            # routed target: standard host discovery probes instead of ARP
            args[1:1] = ["-PE", "-PS80,443,22", "-PA80"]
        timeout = 3600 if mode == "deep" else 600
        try:
            res = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
            return self._parse_nmap(res.stdout)
        except subprocess.TimeoutExpired:
            return []
        except (OSError, subprocess.SubprocessError):
            return []

    @staticmethod
    def _parse_nmap(xml_text):
        hosts = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return hosts
        for h in root.findall("host"):
            st = h.find("status")
            if st is None or st.get("state") != "up":
                continue
            ip = mac = vendor = ""
            for addr in h.findall("address"):
                t = addr.get("addrtype")
                if t == "ipv4":
                    ip = addr.get("addr", "")
                elif t == "mac":
                    mac = addr.get("addr", "")
                    vendor = addr.get("vendor", "")
            if not ip:
                continue
            hostname = ""
            hn = h.find("hostnames")
            if hn is not None:
                first = hn.find("hostname")
                if first is not None:
                    hostname = first.get("name", "")
            ports, services = [], {}
            pe = h.find("ports")
            if pe is not None:
                for p in pe.findall("port"):
                    ps = p.find("state")
                    if ps is None or ps.get("state") != "open":
                        continue
                    portid = int(p.get("portid"))
                    ports.append(portid)
                    svc = p.find("service")
                    if svc is not None:
                        services[str(portid)] = " ".join(
                            x for x in (svc.get("name"), svc.get("product"), svc.get("version")) if x
                        )
            osname = ""
            oe = h.find("os")
            if oe is not None:
                m = oe.find("osmatch")
                if m is not None:
                    osname = m.get("name", "")
            rtt = None
            times = h.find("times")
            if times is not None and times.get("srtt"):
                try:
                    rtt = round(int(times.get("srtt")) / 1000.0, 1)  # us -> ms
                except ValueError:
                    pass
            hosts.append({"ip": ip, "mac": mac, "nmap_vendor": vendor,
                          "hostname": hostname, "ports": sorted(ports),
                          "services": services, "os": osname, "rtt": rtt})
        return hosts

    # ---- scan orchestration -------------------------------------------
    def run_scan(self, mode="quick", only_target=None, only_hosts=None):
        with self._scan_lock:                # only one scan runs at a time
            self._run_scan(mode, only_target, only_hosts)

    def _run_scan(self, mode, only_target, only_hosts):
        cfg = config.load()
        online_ok = cfg["scan"]["online_lookup"]

        # Build the list of nmap jobs: (group_label, [target tokens], is_local).
        if only_hosts:
            jobs = [(None, list(only_hosts), self._is_local_ip(only_hosts[0]))]
        else:
            targets = cfg["targets"]
            if only_target:
                targets = [t for t in targets if t["cidr"] == only_target] or targets
            jobs = [(t["cidr"], [t["cidr"]], self._is_local(t["cidr"], t.get("local", "auto")))
                    for t in targets]

        label = only_target or (",".join(only_hosts) if only_hosts else "all")
        with self.lock:
            self.status.update(is_scanning=True, mode=mode, target=label, progress="starting")

        found = {}
        new_devices = []
        superseded = set()
        reg_changed = False
        extra_events = []          # ip_change / replaced rows for the audit log
        replaced_cands = []        # (rec, prev_mac_key) — confirmed after the scan
        mac_ips = {}               # normalised MAC -> {ips} this scan (dup-MAC detector)
        # Snapshot of which device currently sits at each IP, to reconcile a
        # device that moved IP or whose MAC wasn't resolved this round.
        ip_index = {d["ip"]: k for k, d in self.devices.items() if d.get("ip")}
        try:
            for group, tokens, is_local in jobs:
                with self.lock:
                    self.status["progress"] = f"{mode} scan {group or ' '.join(tokens)}"
                for h in self._nmap(tokens, mode, is_local):
                    tgt = group if group else self._target_for_ip(h["ip"], cfg)
                    key, old_key = self._resolve_key(h, ip_index)
                    rec = self._build_record(h, tgt, is_local, online_ok, (mode == "deep"), key)
                    prev = self.devices.get(key, {})
                    nmac = identify.normalize_mac(h.get("mac", ""))
                    if nmac and h.get("ip"):
                        mac_ips.setdefault(nmac, set()).add(h["ip"])
                    # DHCP drift: same device (MAC key), new IP since last seen.
                    if prev.get("ip") and prev["ip"] != rec["ip"]:
                        extra_events.append(history.build_event(
                            "ip_change", rec, {"prev_ip": prev["ip"]}))
                    # Possible takeover: a different MAC now holds this IP (confirm
                    # after the scan that the old occupant is gone, not a live conflict).
                    pk = ip_index.get(rec["ip"])
                    if pk and pk != key and ":" in pk and ":" in key and not old_key:
                        replaced_cands.append((rec, pk))
                    # keep deep-scan detail across quick scans
                    if not rec.get("deep") and prev.get("deep"):
                        for f in ("services", "os", "deep"):
                            if prev.get(f):
                                rec[f] = prev[f]
                        if "+" not in rec["features"]:
                            rec["features"].append("+")
                    # a sparse sighting (e.g. ARP miss → no MAC/vendor) must not wipe identity
                    for f in ("vendor", "mac", "hostname"):
                        if not rec.get(f) and prev.get(f):
                            rec[f] = prev[f]
                    if rec["category"] == "unknown" and prev.get("category") not in (None, "unknown"):
                        rec["category"], rec["type"] = prev["category"], prev.get("type", rec["type"])
                    rec["first_seen"] = prev.get("first_seen", rec["last_seen"])

                    was_seen = key in self.seen_keys
                    if old_key:  # this device used to be tracked under a different key (IP→MAC)
                        superseded.add(old_key)
                        old_rec = self.devices.get(old_key, {})
                        if old_key in self.registry and key not in self.registry:
                            self.registry[key] = self.registry.pop(old_key)
                            reg_changed = True
                        if old_rec.get("first_seen"):
                            rec["first_seen"] = min(rec["first_seen"], old_rec["first_seen"])
                        if not rec["name"] and old_rec.get("name"):
                            rec["name"] = old_rec["name"]
                        if old_key in self.seen_keys:
                            was_seen = True

                    found[key] = rec
                    if not was_seen:
                        if key not in self.registry:
                            new_devices.append(rec)
                    self.seen_keys.add(key)

            # Local L2 discovery: enrich + surface devices nmap missed.
            if mode == "quick" and not only_hosts and cfg["scan"].get("discovery", True):
                if self._discovery_pass(found, new_devices, superseded, ip_index, jobs, cfg, online_ok):
                    reg_changed = True

            if reg_changed:
                self.save_registry()

            # Reachability gate: a host that only answered ARP — no open scanned
            # port AND no ICMP reply — isn't really reachable (e.g. a Wi-Fi NIC
            # answering ARP while the host sleeps). Drop it from `found` so the
            # offline state machine treats it as down. (cfg scan.require_reachable)
            if cfg["scan"].get("require_reachable", True):
                portless = [k for k, r in found.items() if not r.get("ports")]
                if portless:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
                        up = dict(zip(portless,
                                      ex.map(lambda k: _icmp_up(found[k]["ip"]), portless)))
                    dropped = {k for k in portless if not up.get(k)}
                    for k in dropped:
                        found.pop(k, None)
                    if dropped:
                        new_devices[:] = [d for d in new_devices
                                          if d.get("key") not in dropped]

            # ICMP-sweep fallback: add live hosts nmap's ARP discovery missed
            # (busy wireless /24). Runs AFTER the gate so these ICMP-proven hosts
            # are never dropped by it.
            if not only_hosts and cfg["scan"].get("icmp_sweep", True):
                if self._sweep_pass(found, new_devices, superseded, ip_index, jobs,
                                    cfg, online_ok, mode):
                    self.save_registry()

            # Devices that were *expected* in this scan's scope (for offline detection).
            if only_hosts:
                host_set = set(only_hosts)
                expected = {k for k, v in self.devices.items() if v.get("ip") in host_set}
            elif only_target:
                expected = {k for k, v in self.devices.items() if v.get("target") == only_target}
            else:
                expected = set(self.devices)
            # One MAC answering on 2+ IPs = duplicate / spoof / bridge.
            self.mac_multi_ip = {m: sorted(ips) for m, ips in mac_ips.items()
                                 if len(ips) > 1}
            # A "replaced" event only when the old occupant didn't show this scan
            # (otherwise it's a live IP conflict, surfaced separately).
            for rec, pk in replaced_cands:
                if pk not in found:
                    old = self.devices.get(pk, {})
                    extra_events.append(history.build_event("replaced", rec, {
                        "prev_key": pk, "prev_name": old.get("name"),
                        "prev_vendor": old.get("vendor"), "prev_mac": old.get("mac")}))

            scanned_keys = (expected | set(found)) - superseded
            self._finalize(found, new_devices, scanned_keys, superseded, cfg, extra_events)
            # Adaptive identification: a regular scan that turns up a *new* device
            # queues a deep scan of just that host to identify it accurately.
            if mode == "quick" and cfg["scan"].get("deep_on_new"):
                fresh = [d["ip"] for d in new_devices if d.get("ip")]
                if fresh:
                    self.trigger("deep", hosts=fresh)
        finally:
            with self.lock:
                self.status.update(is_scanning=False, progress="", mode=None, target=None)

    def _resolve_key(self, h, ip_index):
        """Pick a stable device key for an nmap host, returning (key, old_key).

        MAC is the identity when available. A sighting with no MAC is matched to
        whatever device was last at that IP (so an ARP miss doesn't fork the
        device). When an IP-keyed device gains a MAC, old_key flags the previous
        IP key to be superseded (migrated, not orphaned)."""
        mac = identify.normalize_mac(h["mac"])
        ip = h["ip"]
        prev_at_ip = ip_index.get(ip)
        if mac:
            key = ":".join(mac[i:i + 2] for i in range(0, 12, 2))
            old_key = prev_at_ip if (prev_at_ip and prev_at_ip != key and ":" not in prev_at_ip) else None
            return key, old_key
        if prev_at_ip and ":" in prev_at_ip:  # reuse the known MAC key for this IP
            return prev_at_ip, None
        return ip, None

    def _build_record(self, h, cidr, is_local, online_ok, deep, key):
        mac = identify.normalize_mac(h["mac"])
        mac_disp = (":".join(mac[i:i + 2] for i in range(0, 12, 2)) if mac
                    else (key if ":" in key else ""))
        vendor = h["nmap_vendor"] or (identify.vendor_for_mac(mac, online_ok) if mac else "")
        hostname = h["hostname"] or identify.reverse_dns(h["ip"])
        banner = identify.http_banner(h["ip"], h["ports"], online_ok)
        category, type_label, conf = identify.classify(vendor, h["ports"], banner, hostname)
        features = identify.features_for_ports(h["ports"], deep)
        # Prefer a MAC-keyed registry entry. Only fall back to an IP-keyed entry
        # for IP-only devices (no MAC) — a NEW device that has its own MAC must
        # never inherit the saved name/category of whatever used to hold this IP
        # (e.g. a router replacing a camera that kept the address).
        reg = self.registry.get(key)
        if reg is None and key == h["ip"]:
            reg = self.registry.get(h["ip"])
        reg = reg or {}
        ts = int(time.time())
        rec = {
            "key": key, "ip": h["ip"], "mac": mac_disp,
            "vendor": vendor, "hostname": hostname,
            "ports": h["ports"], "services": h["services"], "os": h["os"],
            "banner": banner, "features": features,
            "category": reg.get("category") or category,
            "type": reg.get("type") or type_label,
            "confidence": conf,
            "name": reg.get("name", ""),
            "watch": reg.get("watch", False),
            "serial": reg.get("serial", ""),
            "model": reg.get("model", ""),
            "firmware": reg.get("firmware", ""),
            "link": reg.get("link", ""),
            "target": cidr, "local": is_local,
            "online": True, "status": "online",
            "last_seen": ts, "rtt": h["rtt"], "deep": deep,
        }
        if deep:
            self._enrich_hik(rec)
        return rec

    def _enrich_hik(self, rec):
        """On a deep scan, pull model/serial/firmware from a Hikvision device
        using its saved credentials (best-effort)."""
        v = (rec.get("vendor") or "").lower()
        if not (any(x in v for x in ("hikvision", "hangzhou")) or rec.get("category") in ("camera", "nvr")):
            return
        c = creds.get(rec["key"])
        if not (c["username"] or c["password"]):
            return
        res = hikvision.fetch(rec["ip"], c["username"], c["password"], timeout=5)
        if not res.get("ok"):
            return
        info = res["info"]
        if info.get("model"):
            rec["model"] = info["model"]
        if info.get("serialNumber"):
            rec["serial"] = info["serialNumber"]
        if info.get("firmwareVersion"):
            rec["firmware"] = info["firmwareVersion"]
        if info.get("deviceName") and not rec.get("hostname"):
            rec["hostname"] = info["deviceName"]
        self.set_device_meta(rec["key"], serial=rec.get("serial") or None,
                             model=rec.get("model") or None)

    # ---- local L2 discovery (mDNS / SSDP / NetBIOS / ARP) --------------
    def _apply_disc(self, rec, info):
        if info.get("hostname") and not rec.get("hostname"):
            rec["hostname"] = info["hostname"]
        if info.get("model") and not rec.get("model"):
            rec["model"] = info["model"]
        if info.get("serial") and not rec.get("serial"):
            rec["serial"] = info["serial"]
        if info.get("manufacturer") and not rec.get("vendor"):
            rec["vendor"] = info["manufacturer"]
        if info.get("mdns_category") and rec.get("category") == "unknown":
            rec["category"] = info["mdns_category"]
            rec["type"] = info["mdns_category"].upper()
        rec["discovery"] = info.get("source", "")

    def _discovery_pass(self, found, new_devices, superseded, ip_index, jobs, cfg, online_ok):
        """Enrich found records and surface devices that answered only mDNS/SSDP/
        ARP. Returns True if the registry changed (a phantom was superseded)."""
        local_nets = []
        for group, _tokens, is_local in jobs:
            if is_local and group:
                try:
                    local_nets.append(ipaddress.ip_network(group, strict=False))
                except ValueError:
                    pass
        if not local_nets:
            return False
        with self.lock:
            self.status["progress"] = "discovery (mDNS / SSDP / NetBIOS)"
        rec_by_ip = {r["ip"]: r for r in found.values()}
        disc = discovery.gather(live_ips=list(rec_by_ip), timeout=3)
        reg_changed = False
        for ip, info in disc.items():
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                continue
            if not any(addr in n for n in local_nets):
                continue
            if ip in rec_by_ip:
                self._apply_disc(rec_by_ip[ip], info)
                continue
            # a device nmap missed but that answered discovery
            h = {"ip": ip, "mac": info.get("mac", ""), "nmap_vendor": "",
                 "hostname": info.get("hostname", ""), "ports": [],
                 "services": {}, "os": "", "rtt": None}
            key, old_key = self._resolve_key(h, ip_index)
            rec = self._build_record(h, self._target_for_ip(ip, cfg), True, online_ok, False, key)
            self._apply_disc(rec, info)
            prev = self.devices.get(key, {})
            rec["first_seen"] = prev.get("first_seen", rec["last_seen"])
            was_seen = key in self.seen_keys
            if old_key:
                superseded.add(old_key)
                if old_key in self.registry and key not in self.registry:
                    self.registry[key] = self.registry.pop(old_key)
                    reg_changed = True
                if old_key in self.seen_keys:
                    was_seen = True
            found[key] = rec
            rec_by_ip[ip] = rec
            if not was_seen and key not in self.registry:
                new_devices.append(rec)
            self.seen_keys.add(key)
        return reg_changed

    def _sweep_pass(self, found, new_devices, superseded, ip_index, jobs, cfg, online_ok, mode):
        """Catch hosts nmap's ARP discovery missed — common for high-latency
        wireless backhaul radios on a busy /24, which answer ICMP fine but lose
        the ARP race during a full-subnet sweep. ICMP-sweep each local target,
        then targeted-nmap the responders nmap didn't already see (a small set,
        so MAC + ports come back reliably); any that still give nothing are added
        as an online IP-only device so they at least appear."""
        local_nets = []
        for group, _tokens, is_local in jobs:
            if is_local and group:
                try:
                    n = ipaddress.ip_network(group, strict=False)
                    if n.num_addresses <= 1024:          # don't sweep huge ranges
                        local_nets.append(n)
                except ValueError:
                    pass
        if not local_nets:
            return False
        have = {r["ip"] for r in found.values()}
        candidates = [str(h) for n in local_nets for h in n.hosts() if str(h) not in have]
        if not candidates:
            return False
        with self.lock:
            self.status["progress"] = "icmp sweep (catching missed hosts)"
        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as ex:
            alive = [ip for ip, up in zip(candidates, ex.map(_icmp_sweep_alive, candidates)) if up]
        if not alive:
            return False
        detail = {h["ip"]: h for h in self._nmap(alive, mode, True)}
        reg_changed = False
        for ip in alive:
            if any(r.get("ip") == ip for r in found.values()):
                continue
            h = detail.get(ip) or {"ip": ip, "mac": "", "nmap_vendor": "", "hostname": "",
                                   "ports": [], "services": {}, "os": "", "rtt": None}
            key, old_key = self._resolve_key(h, ip_index)
            rec = self._build_record(h, self._target_for_ip(ip, cfg), True, online_ok,
                                     (mode == "deep"), key)
            rec["discovery"] = rec.get("discovery") or "icmp"
            prev = self.devices.get(key, {})
            rec["first_seen"] = prev.get("first_seen", rec["last_seen"])
            was_seen = key in self.seen_keys
            if old_key:
                superseded.add(old_key)
                if old_key in self.registry and key not in self.registry:
                    self.registry[key] = self.registry.pop(old_key)
                    reg_changed = True
                if old_key in self.seen_keys:
                    was_seen = True
            found[key] = rec
            if not was_seen and key not in self.registry:
                new_devices.append(rec)
            self.seen_keys.add(key)
        return reg_changed

    def _finalize(self, found, new_devices, scanned_keys, superseded, cfg, extra_events=None):
        """Update state for the scanned scope only; carry untouched devices over.
        `superseded` keys (a device re-identified under a new key) are dropped so
        they are neither marked offline nor counted as a separate device."""
        offline_after = cfg["alerts"]["offline_after"]
        alerts = cfg["alerts"]

        for k in superseded:
            self.miss.pop(k, None)
            self.seen_keys.discard(k)
        # offline_now / online_now feed the audit log only (up/down alerting is
        # Uptime Kuma's job now), so they record every transition, not just the
        # ones an alert toggle was set for.
        samples, offline_now, online_now = [], [], []
        for key in scanned_keys:
            prev_miss = self.miss.get(key, 0)
            if key in found:
                if prev_miss >= offline_after:        # was counted offline, now back
                    online_now.append(found[key])
                self.miss[key] = 0
                samples.append((key, True, found[key]["ip"], found[key].get("rtt")))
            else:
                self.miss[key] = prev_miss + 1
                old = self.devices.get(key)
                ip = old["ip"] if old else (key if ":" not in key else "")
                samples.append((key, False, ip, None))
                if old:
                    old = dict(old)
                    old.update(online=False, status="offline")
                    found[key] = old
                if self.miss[key] == offline_after and old:
                    offline_now.append(found[key])

        # Carry over devices that were out of this scan's scope, unchanged;
        # drop any superseded keys so a moved/re-keyed device leaves no phantom.
        result = {k: v for k, v in self.devices.items()
                  if k not in scanned_keys and k not in superseded}
        result.update(found)
        # forget miss counters for devices we no longer track (e.g. stale seeds)
        self.miss = {k: v for k, v in self.miss.items() if k in result}

        history.record(samples)

        # ---- append to the IP/device event log (survives prune) -----------
        ev_rows = [history.build_event("new", d) for d in new_devices]
        ev_rows += [history.build_event("offline", d) for d in offline_now]
        ev_rows += [history.build_event("online", d) for d in online_now]
        for old_key in superseded:
            old = self.devices.get(old_key)
            if not old:
                continue
            ip = old.get("ip")
            new = next((r for r in found.values()
                        if r.get("ip") == ip and r.get("key") != old_key), None)
            extra = {"prev_key": old_key, "prev_name": old.get("name"),
                     "prev_vendor": old.get("vendor"), "prev_mac": old.get("mac")}
            ev_rows.append(history.build_event("ip_change", new or old, extra))
        if extra_events:
            ev_rows += list(extra_events)            # ip_change / replaced from this scan
        try:
            history.log_events(ev_rows)
        except Exception:  # noqa: BLE001 - logging must never break a scan
            pass

        # Discovery is Netwatch's to announce; up/down alerts are Uptime Kuma's
        # job now, so Netwatch no longer sends its own offline/online ntfy.
        for dev in new_devices:
            notify.notify_new_device(alerts, dev)

        # Baseline each seen device's "home" IP the first time we have one (also
        # back-fills existing devices on upgrade), so only LATER moves flag as
        # drift — and acknowledging just rewrites this value.
        reg_baseline = False
        for key, dev in found.items():
            ip = dev.get("ip")
            if ip and not self.registry.get(key, {}).get("known_ip"):
                self.registry.setdefault(key, {})["known_ip"] = ip
                reg_baseline = True
        if reg_baseline:
            self.save_registry()

        with self.lock:
            self.devices = result
            self.status["last_scan"] = _now_str()
            self.status["last_scan_ts"] = int(time.time())
        self._save_state()
        self._kuma_sync(cfg, result)   # keep Kuma monitors pointed at the right IP / push manual ones
        try:
            history.prune(cfg["scan"]["history_days"])
            # Event log keeps a long, independent retention (default ~1 year) so a
            # device's history outlives both the sample retention and a prune.
            history.prune_events(cfg["scan"].get("event_log_days", 365))
        except Exception:
            pass

    def _ensure_internet_monitors(self, cfg, ki, base):
        """Create the default internet-uptime monitors once, when Kuma is enabled.
        Idempotent via a registry marker (re-detects the gateway if it changes)."""
        # "By default" = whenever Kuma is actually configured (admin creds present),
        # not gated behind a separate toggle.
        if not ki.get("internet_monitors", True):
            return
        user = ki.get("username", "")
        pw = creds.get("@kuma").get("password", "")
        if not (user and pw):
            return
        marker = self.registry.get("__internet__") or {}
        gw = default_gateway()
        if marker and marker.get("gateway_ip") == gw:
            return   # already provisioned for this gateway
        try:
            res = kuma.provision_internet(base, user, pw, gw)
        except Exception as e:   # noqa: BLE001
            print("internet-monitor provision error:", e, flush=True)
            return
        if res.get("ok"):
            ids = {n: v.get("monitor_id") for n, v in (res.get("monitors") or {}).items()
                   if v.get("ok")}
            self.registry["__internet__"] = {"gateway_ip": gw, "monitors": ids}
            self.save_registry()
            print(f"[kuma] internet monitors provisioned (gateway={gw}): {list(ids)}", flush=True)

    @staticmethod
    def _kuma_name(dev):
        # Name by device identity, NOT the IP — the monitor's hostname carries the
        # IP and ensure_ping keeps it current, so the name never goes stale when
        # the device moves address.
        label = dev.get("name") or dev.get("type") or dev.get("vendor") or "device"
        return label[:150]

    def _kuma_sync(self, cfg, devices):
        """Auto (ping) monitors: Kuma pings the device itself, so we only repoint
        the monitor when the device's IP changes. Manual push-token monitors (no
        monitor_id): push status each scan as before."""
        ki = cfg.get("integrations", {}).get("kuma", {})
        base = ki.get("base_url")
        if not base:
            return
        self._ensure_internet_monitors(cfg, ki, base)
        host_changes = []   # (monitor_id, new_ip, key) for ping monitors that moved
        for key, dev in devices.items():
            reg = self.registry.get(key, {})
            mid, token = reg.get("kuma_monitor_id"), reg.get("kuma_token")
            if mid:
                ip = dev.get("ip")
                if ip and ip != reg.get("kuma_ip"):
                    host_changes.append((mid, ip, key))
            elif token:   # manual push monitor
                up = dev.get("online", False)
                label = dev.get("name") or dev.get("type") or dev.get("ip", "")
                kuma.push(base, token, up, msg=f"{label} {dev.get('ip', '')}".strip(),
                          ping_ms=dev.get("rtt") if up else None)
        if host_changes:
            user = ki.get("username", "")
            pw = creds.get("@kuma").get("password", "")
            if user and pw:
                try:
                    kuma.ensure_ping(base, user, pw, [(m, ip) for m, ip, _ in host_changes])
                    for m, ip, key in host_changes:
                        self.registry.setdefault(key, {})["kuma_ip"] = ip
                    self.save_registry()
                except Exception as e:
                    print("kuma host-sync error:", e, flush=True)

    # ---- public api ----------------------------------------------------
    def trigger(self, mode="quick", target=None, hosts=None):
        threading.Thread(
            target=self.run_scan,
            kwargs={"mode": mode, "only_target": target, "only_hosts": hosts},
            daemon=True,
        ).start()

    def _conflict_map(self, devs, window_s=None):
        """{ip: [device, ...]} for IPs claimed by 2+ distinct devices (MAC keys)
        both seen within `window_s` — a live address conflict."""
        cutoff = int(time.time()) - (window_s or CONFLICT_WINDOW_S)
        by_ip = {}
        for d in devs:
            ip = d.get("ip")
            if ip and d.get("last_seen", 0) >= cutoff:
                by_ip.setdefault(ip, []).append(d)
        return {ip: ds for ip, ds in by_ip.items()
                if len({d.get("key") for d in ds}) > 1}

    def ip_conflicts(self, window_s=None):
        """Current IP address conflicts, for the dashboard's conflict monitor and
        the /api/conflicts endpoint. One entry per conflicted IP, newest sighting
        first, so the operator can renumber the offending device."""
        with self.lock:
            devs = list(self.devices.values())
        cmap = self._conflict_map(devs, window_s)

        def _ipkey(ip):
            try:
                return tuple(int(o) for o in ip.split("."))
            except ValueError:
                return (9999,)

        out = []
        for ip in sorted(cmap, key=_ipkey):
            ds = sorted(cmap[ip], key=lambda d: d.get("last_seen", 0), reverse=True)
            out.append({
                "ip": ip,
                "count": len(ds),
                "any_online": any(d.get("online") for d in ds),
                "devices": [{
                    "key": d.get("key"), "mac": d.get("mac"), "vendor": d.get("vendor"),
                    "name": d.get("name"), "category": d.get("category"),
                    "online": d.get("online"), "last_seen": d.get("last_seen"),
                    "ports": d.get("ports", []),
                } for d in ds],
            })
        return out

    def problems(self, window_s=None):
        """All detected problems as one typed list for the dashboard's Problems
        panel and /api/problems — all derived from the CURRENT device list (plus
        the acknowledged home-IP baseline), so problems self-clear when fixed,
        acknowledged or purged. No stale ghosts from the event log."""
        window = window_s or CONFLICT_WINDOW_S
        with self.lock:
            devs = list(self.devices.values())
            mac_multi = dict(self.mac_multi_ip)

        def _ipkey(ip):
            try:
                return tuple(int(o) for o in (ip or "").split("."))
            except ValueError:
                return (9999,)

        def _brief(d):
            return {"key": d.get("key"), "mac": d.get("mac"), "vendor": d.get("vendor"),
                    "name": d.get("name"), "category": d.get("category"),
                    "online": d.get("online"), "last_seen": d.get("last_seen")}

        out = []
        # 1. IP conflicts (two live MACs on one address)
        cmap = self._conflict_map(devs, window)
        for ip in sorted(cmap, key=_ipkey):
            ds = sorted(cmap[ip], key=lambda d: d.get("last_seen", 0), reverse=True)
            out.append({
                "type": "ip_conflict", "severity": "high", "ip": ip,
                "devices": [_brief(d) for d in ds],
                "detail": f"{len(ds)} devices answer {ip}",
                "fix": "Give one device a unique IP, then add a DHCP reservation.",
            })
        # 2. Risky exposed ports
        for d in devs:
            if not d.get("online"):
                continue
            risky = [RISKY_PORTS[p] for p in (d.get("ports") or []) if p in RISKY_PORTS]
            if risky:
                out.append({
                    "type": "risky_ports", "severity": "medium", "ip": d.get("ip"),
                    "devices": [_brief(d)],
                    "detail": "Exposes " + ", ".join(risky),
                    "fix": "Disable the plaintext/admin service or restrict access.",
                })
        # 3. One MAC on multiple IPs
        for mac, ips in sorted(mac_multi.items()):
            dev = next((d for d in devs if (d.get("mac") or "").lower() == mac.lower()), None)
            out.append({
                "type": "same_mac_multi_ip", "severity": "medium",
                "ip": ", ".join(ips), "devices": [_brief(dev)] if dev else [],
                "detail": f"MAC {mac} answers on {len(ips)} IPs: {', '.join(ips)}",
                "fix": "Check for a bridge, or a duplicate / spoofed MAC.",
            })
        # 4. IP drift — current state, not the event log. A device whose live IP
        # differs from its acknowledged "home" IP. Self-clears when you Acknowledge
        # (sets home = current) or when the device is purged; no stale ghosts.
        for d in devs:
            if not d.get("online"):
                continue
            home = self.registry.get(d.get("key"), {}).get("known_ip")
            if home and d.get("ip") and home != d["ip"]:
                label = d.get("name") or d.get("vendor") or "device"
                out.append({
                    "type": "ip_change", "severity": "low", "ip": d["ip"],
                    "devices": [_brief(d)],
                    "detail": f"{label} is now at {d['ip']} (home was {home})",
                    "fix": "Acknowledge to accept the new address, or reserve it in DHCP.",
                    "ack_key": d.get("key"),       # the device to acknowledge
                })
        return out

    def acknowledge_ip(self, key):
        """Accept a device's current IP as its new 'home' — clears its drift flag."""
        with self.lock:
            dev = self.devices.get(key)
            ip = dev.get("ip") if dev else None
        if not ip:
            return {"ok": False, "error": "device not currently online"}
        self.registry.setdefault(key, {})["known_ip"] = ip
        self.save_registry()
        return {"ok": True, "key": key, "known_ip": ip}

    def acknowledge_all_drift(self):
        """Acknowledge every device currently flagged for IP drift in one go."""
        with self.lock:
            items = [(k, d.get("ip")) for k, d in self.devices.items()
                     if d.get("online") and d.get("ip")
                     and self.registry.get(k, {}).get("known_ip")
                     and self.registry[k]["known_ip"] != d["ip"]]
        for key, ip in items:
            self.registry.setdefault(key, {})["known_ip"] = ip
        if items:
            self.save_registry()
        return {"ok": True, "acknowledged": len(items)}

    def get_devices(self):
        with self.lock:
            devs = list(self.devices.values())
        cmap = self._conflict_map(devs)
        for d in devs:
            others = [p for p in cmap.get(d.get("ip"), []) if p.get("key") != d.get("key")]
            d["ip_conflict"] = bool(others)
            d["ip_conflict_with"] = [{
                "key": p.get("key"), "mac": p.get("mac"), "vendor": p.get("vendor"),
                "name": p.get("name"), "category": p.get("category"),
                "online": p.get("online"), "last_seen": p.get("last_seen"),
            } for p in others]
        return devs

    def get_status(self):
        with self.lock:
            return dict(self.status)

    def set_device_meta(self, key, name=None, category=None, type_label=None,
                        watch=None, serial=None, model=None, link=None, kuma_token=None,
                        kuma_monitor_id=None, kuma_ip=None):
        reg = self.registry.get(key, {})
        for field, val in (("name", name), ("category", category), ("type", type_label),
                           ("serial", serial), ("model", model)):
            if val is not None:
                reg[field] = val
        if watch is not None:
            reg["watch"] = bool(watch)
        if link is not None:
            reg["link"] = link or ""        # "" clears the link
        if kuma_token is not None:
            reg["kuma_token"] = kuma_token.strip()
        if kuma_monitor_id is not None:
            reg["kuma_monitor_id"] = kuma_monitor_id or 0
        if kuma_ip is not None:
            reg["kuma_ip"] = kuma_ip
        self.registry[key] = reg
        self.save_registry()
        with self.lock:
            if key in self.devices:
                self.devices[key].update({k: v for k, v in
                                          (("name", name), ("category", category),
                                           ("type", type_label), ("watch", watch),
                                           ("serial", serial), ("model", model), ("link", link))
                                          if v is not None})
        return reg

    def delete_device(self, key):
        """Forget a device entirely: registry, live state, miss counter,
        seen-set and its uptime history. Returns True if anything was removed."""
        removed = False
        if key in self.registry:
            del self.registry[key]
            self.save_registry()
            removed = True
        with self.lock:
            if key in self.devices:
                del self.devices[key]
                removed = True
            self.miss.pop(key, None)
            self.seen_keys.discard(key)
        self._save_state()
        try:
            history.delete_key(key)
        except Exception:  # noqa: BLE001 - history cleanup is best-effort
            pass
        return removed

    def prune_devices(self, days=None, only_offline=True):
        """Forget devices not seen recently. With days=None, removes every
        currently-offline device; otherwise those whose last_seen is older than
        `days`. Returns the list of removed keys.

        Done as ONE atomic batch (single registry/state save + a single history
        delete) — a per-device loop rewrote devices.json/state.json and committed
        SQLite once per victim, which on a busy Pi could take minutes and make the
        UI button look hung."""
        cutoff = (time.time() - days * 86400) if days else None
        with self.lock:
            victims = []
            for key, d in list(self.devices.items()):
                if only_offline and d.get("online"):
                    continue
                if cutoff is not None and (d.get("last_seen") or 0) > cutoff:
                    continue
                victims.append(key)
            for key in victims:
                self.devices.pop(key, None)
                self.miss.pop(key, None)
                self.seen_keys.discard(key)
                self.registry.pop(key, None)
        if victims:
            self.save_registry()
            self._save_state()
            try:
                history.delete_keys(victims)
            except Exception:  # noqa: BLE001 - history cleanup is best-effort
                pass
        return victims

    # ---- background loop ----------------------------------------------
    def loop(self):
        while not self._stop:
            cfg = config.load()
            if cfg.get("configured"):
                try:
                    self.run_scan("quick")
                except Exception as e:
                    print("scan error:", e, flush=True)
            interval = max(1, int(config.load()["scan"]["interval_min"]))
            self._wake.wait(timeout=interval * 60)
            self._wake.clear()

    def wake(self):
        self._wake.set()

    # ---- latency heartbeat sampler (Kuma-style chart data + internet check) ----
    def _heartbeat_loop(self):
        last_prune = 0.0
        while not self._stop:
            cfg = config.load()
            try:
                self._heartbeat_tick(cfg)
            except Exception as e:   # noqa: BLE001 - never let a cycle kill the thread
                print("heartbeat error:", e, flush=True)
            if time.time() - last_prune > 3600:
                last_prune = time.time()
                try:
                    history.prune_beats(cfg["scan"].get("heartbeat_retention_days", 3))
                except Exception:    # noqa: BLE001
                    pass
            interval = max(15, int(cfg["scan"].get("heartbeat_interval_s", 60)))
            self._hb_wake.wait(timeout=interval)
            self._hb_wake.clear()

    def _heartbeat_tick(self, cfg):
        scope = cfg["scan"].get("heartbeat_scope", "watched_named")
        with self.lock:
            online = [(k, d.get("ip")) for k, d in self.devices.items()
                      if d.get("online") and d.get("ip")]
        targets = []
        for k, ip in online:
            if scope == "online":
                targets.append((k, ip))
            else:
                reg = self.registry.get(k, {})
                if reg.get("watch") or reg.get("name"):
                    targets.append((k, ip))
        # Internet-uptime probes (synthetic keys), always sampled for the badge.
        gw = default_gateway()
        if gw:
            targets.append(("__inet__gateway", gw))
        targets.append(("__inet__8.8.8.8", "8.8.8.8"))
        targets.append(("__inet__1.1.1.1", "1.1.1.1"))
        rows = []
        if targets:
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(32, len(targets))) as ex:
                for key, (up, rtt) in zip(
                        [t[0] for t in targets],
                        ex.map(lambda t: _icmp_rtt(t[1]), targets)):
                    rows.append((key, up, rtt))
        rows.append(("__inet__dns", _dns_ok("google.com"), None))
        # Hub VPN link health — only when this site is joined to a hub.
        if hubvpn.has_config():
            hub_up, hub_rtt = _icmp_rtt("10.8.0.1")
            rows.append(("__hub__", hub_up, hub_rtt))
            self._check_hub_link(cfg, hub_up)
        history.record_beats(rows)

    def _check_hub_link(self, cfg, up):
        """Notify (once) when the VPN link to the Central Hub drops or recovers."""
        prev = self._hub_up
        self._hub_up = up
        if prev is None or prev == up:
            return
        alerts = cfg.get("alerts", {})
        if not alerts.get("notify_hub_offline", True):
            return
        site = cfg.get("site", {}).get("name") or "This site"
        if not up:
            notify.push(alerts, "🔌 Hub link DOWN",
                        f"{site} lost its VPN link to the Central Hub.",
                        priority="high", tags=["warning"])
        else:
            notify.push(alerts, "✅ Hub link restored",
                        f"{site} is reconnected to the Central Hub.",
                        tags=["white_check_mark"])

    def start(self):
        threading.Thread(target=self.loop, daemon=True).start()
        if config.load()["scan"].get("heartbeat_enabled", True):
            threading.Thread(target=self._heartbeat_loop, daemon=True).start()


scanner = Scanner()
