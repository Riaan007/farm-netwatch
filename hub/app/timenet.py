"""Time keeping: work on a farm over the VPN, seen as traffic through the hub.

The hub shares wg-easy's network namespace, so every packet to a farm Pi's VPN
address passes here. Two counting chains (they only count — every rule RETURNs,
nothing is ever accepted or dropped by them):

  NW-TIME-FWD  (FORWARD -o wg0)  traffic FORWARDED to a farm: from the operator's
               own VPN devices (hub.json remote_clients — laptop, phone) or from
               the office LAN / this Pi's host routed through the hub. A farm's
               own packets to another farm are not counted (vpnfw drops them).
  NW-TIME-OUT  (OUTPUT -o wg0)   SSH (tcp/22) to a farm Pi from the hub's own
               namespace: the pi-ssh relays and SSH sessions started in it.
               The hub's polls, backups and proxy go to :8090/:3001 and are not
               counted.

Every 30 s timekeeping._loop asks `deltas()`; a farm whose counter grew by at
least the minimum packets marks that minute as source "vpn" or "ssh". Counters
reset when wg-easy rebuilds the namespace; `ensure()` re-adds the chains.
"""
import ipaddress
import threading

import vpnfw

FWD, OUT = "NW-TIME-FWD", "NW-TIME-OUT"
IFACE = vpnfw.IFACE
MIN_PKTS = {"vpn": 8, "ssh": 4}        # per 30 s read; idle keep-alives stay below

_lock = threading.Lock()
_last = {}                             # (source, site) -> packet counter last read
_applied = None                        # the rule set last written


def farms(cfg):
    """{vpn_ip: site_id} for sites reached over the VPN."""
    out = {}
    for s in cfg.get("sites") or []:
        try:
            ip = ipaddress.ip_address((s.get("vpn_ip") or "").strip())
        except ValueError:
            continue
        if ip in vpnfw.VPN_NET:
            out[str(ip)] = s["id"]
    return out


def desired(cfg):
    """(forward rules, output rules) — each rule as an iptables -A line; the
    order is the order `_read` maps counters back with."""
    ops = vpnfw.operators(cfg)
    fwd, out = [], []
    for ip in sorted(farms(cfg), key=ipaddress.ip_address):
        fwd += [f"-A {FWD} -s {op}/32 -d {ip}/32 -j RETURN" for op in ops]
        fwd.append(f"-A {FWD} -d {ip}/32 ! -i {IFACE} -j RETURN")
        out.append(f"-A {OUT} -d {ip}/32 -p tcp -m tcp --dport 22 -j RETURN")
    return fwd, out


def _rules(ipt, chain):
    rc, txt = vpnfw._run([ipt, "-S", chain])
    return None if rc else [l.strip() for l in txt.splitlines() if l.startswith("-A ")]


def _jumped(ipt, builtin, chain):
    rc, txt = vpnfw._run([ipt, "-S", builtin])
    return f"-A {builtin} -o {IFACE} -j {chain}" in txt


def ensure(cfg, ipt=None):
    """Make the counting chains match the config. Returns the backend used."""
    global _applied
    ipt = ipt or vpnfw._backend()
    fwd, out = desired(cfg)
    if (_rules(ipt, FWD) == fwd and _rules(ipt, OUT) == out
            and _jumped(ipt, "FORWARD", FWD) and _jumped(ipt, "OUTPUT", OUT)):
        return ipt
    restore = ipt + "-restore" if ipt.startswith("iptables-") else "iptables-restore"
    text = "\n".join(["*filter", f":{FWD} - [0:0]", f":{OUT} - [0:0]", *fwd, *out, "COMMIT", ""])
    rc, msg = vpnfw._run([restore, "--noflush"], stdin=text)
    if rc:
        raise RuntimeError(f"timenet: {msg.strip()[:200]}")
    for builtin, chain in (("FORWARD", FWD), ("OUTPUT", OUT)):
        if not _jumped(ipt, builtin, chain):
            vpnfw._run([ipt, "-I", builtin, "1", "-o", IFACE, "-j", chain])
    _applied = (fwd, out)
    _last.clear()                      # rules rewritten: counters start again
    return ipt


def parse_counts(listing):
    """`iptables -L CHAIN -n -v -x` -> packet counters in rule order."""
    out = []
    for line in listing.splitlines():
        parts = line.split()
        if parts and parts[0].isdigit() and len(parts) > 2 and parts[2] == "RETURN":
            out.append(int(parts[0]))
    return out


def counters(cfg, ipt):
    """{(source, site_id): packets} summed per farm."""
    ips = sorted(farms(cfg), key=ipaddress.ip_address)
    per = len(vpnfw.operators(cfg)) + 1
    res = {}
    rc, txt = vpnfw._run([ipt, "-L", FWD, "-n", "-v", "-x"])
    f = parse_counts(txt) if not rc else []
    if len(f) == per * len(ips):
        for i, ip in enumerate(ips):
            res[("vpn", farms(cfg)[ip])] = sum(f[i * per:(i + 1) * per])
    rc, txt = vpnfw._run([ipt, "-L", OUT, "-n", "-v", "-x"])
    o = parse_counts(txt) if not rc else []
    if len(o) == len(ips):
        for ip, n in zip(ips, o):
            res[("ssh", farms(cfg)[ip])] = n
    return res


def deltas(cfg, now):
    """[(source, site_id, ts)] for farms worked on since the last call."""
    with _lock:
        ipt = ensure(cfg)
        cur = counters(cfg, ipt)
        hits = []
        for k, n in cur.items():
            prev = _last.get(k)
            _last[k] = n
            if prev is None:
                continue                       # first read: a baseline, not work
            d = n - prev if n >= prev else n   # counter reset (namespace rebuilt)
            if d >= MIN_PKTS[k[0]]:
                hits.append((k[0], k[1], now))
        return hits
