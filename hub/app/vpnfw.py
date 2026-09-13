"""VPN client isolation: farm sites may not reach each other, the office LAN,
the operators' devices or the hub's own services.

wg-easy's PostUp forwards everything that arrives on wg0 (`FORWARD -i wg0 -j
ACCEPT`), and every peer sits in one 10.8.0.0/24 — so one client's Pi could
reach another client's Pi, the office LAN and the hub relay ports. WireGuard
AllowedIPs only stops a peer from SPOOFING another peer's address; it is not an
authorisation boundary.

The hub shares wg-easy's network namespace, so it manages two chains there,
jumped to FIRST for traffic entering from wg0:

  NW-VPN-FWD  (FORWARD -i wg0)
      replies (ESTABLISHED,RELATED)                 accept
      operator devices (hub.json remote_clients)    accept — sites, office LAN
      everything else (sites, unknown peers)        DROP
  NW-VPN-IN   (INPUT -i wg0, i.e. to 10.8.0.1 itself)
      replies to the hub's own polls                accept
      operator devices                              accept — hub UI, proxies, relays
      ping from anyone                              accept — sites alert on hub loss
      everything else                               DROP

The hub never needs a site to open a connection to it — it pulls. Unknown
peers are dropped by default, so a new site created by the wizard is isolated
from its first packet, while a new remote client is let in as soon as the hub
records it.

A 60 s loop re-asserts the rules (wg-easy restarts rebuild the netns and its
PostUp re-adds the ACCEPTs after ours). It uses the same iptables backend as
wg-easy (legacy vs nft), detected from where wg-easy's wg0 rules live.

Kill switch (persisted, the loop stops re-adding):
    docker exec netwatch-hub python vpnfw.py off
    docker exec netwatch-hub python vpnfw.py on
    docker exec netwatch-hub python vpnfw.py status
"""
import ipaddress
import json
import shutil
import subprocess
import sys
import threading
import time

import hubconfig

IFACE = "wg0"
FWD, INP = "NW-VPN-FWD", "NW-VPN-IN"
LOOP_S = 60
VPN_NET = ipaddress.ip_network("10.8.0.0/24")

_lock = threading.Lock()
_state = {"applied": False, "error": "", "checked": 0, "operators": [], "backend": ""}


def _run(args, stdin=None):
    try:
        p = subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=20)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def _backend():
    """'iptables-legacy' or 'iptables-nft' — whichever holds wg-easy's rules."""
    for cand in ("iptables-legacy", "iptables-nft"):
        if shutil.which(cand):
            rc, out = _run([cand, "-S", "FORWARD"])
            if rc == 0 and f"-i {IFACE}" in out:
                return cand
    return "iptables-legacy" if shutil.which("iptables-legacy") else "iptables"


def enabled():
    return bool((hubconfig.load().get("vpn") or {}).get("isolation", True))


def operators(cfg=None):
    """VPN addresses of the operator's own devices (road-warrior clients)."""
    cfg = cfg or hubconfig.load()
    out = []
    for c in cfg.get("remote_clients") or []:
        try:
            ip = ipaddress.ip_address((c.get("address") or "").strip())
        except ValueError:
            continue
        if ip in VPN_NET and ip != ipaddress.ip_address("10.8.0.1"):
            out.append(str(ip))
    return sorted(set(out), key=ipaddress.ip_address)


def desired_rules(ops):
    fwd = [f"-A {FWD} -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT"]
    fwd += [f"-A {FWD} -s {ip}/32 -j ACCEPT" for ip in ops]
    fwd += [f"-A {FWD} -j DROP"]
    inp = [f"-A {INP} -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT"]
    inp += [f"-A {INP} -s {ip}/32 -j ACCEPT" for ip in ops]
    inp += [f"-A {INP} -p icmp -m icmp --icmp-type 8 -j ACCEPT",
            f"-A {INP} -j DROP"]
    return fwd, inp


def _chain_rules(ipt, chain):
    rc, out = _run([ipt, "-S", chain])
    if rc != 0:
        return None
    return [l.strip() for l in out.splitlines() if l.startswith("-A ")]


def _jump_first(ipt, builtin, chain):
    rc, out = _run([ipt, "-S", builtin])
    lines = [l for l in out.splitlines() if l.startswith("-A ")]
    return bool(lines) and lines[0].strip() == f"-A {builtin} -i {IFACE} -j {chain}"


def apply():
    """Make the live rules match hub.json. Idempotent; atomic per chain."""
    with _lock:
        ipt = _backend()
        _state.update(backend=ipt, checked=int(time.time()))
        if not enabled():
            _state.update(applied=False, error="")
            return remove(locked=True)
        ops = operators()
        fwd, inp = desired_rules(ops)
        _state["operators"] = ops
        if (_chain_rules(ipt, FWD) == fwd and _chain_rules(ipt, INP) == inp
                and _jump_first(ipt, "FORWARD", FWD) and _jump_first(ipt, "INPUT", INP)):
            _state.update(applied=True, error="")
            return True, "unchanged"
        # iptables-restore --noflush: declared chains are flushed and refilled in one
        # commit, so there is no moment where a half-written chain lets traffic by.
        restore = ipt + "-restore" if ipt.startswith("iptables-") else "iptables-restore"
        text = "\n".join(["*filter", f":{FWD} - [0:0]", f":{INP} - [0:0]", *fwd, *inp,
                          "COMMIT", ""])
        rc, out = _run([restore, "--noflush"], stdin=text)
        if rc != 0:
            _state.update(applied=False, error=out.strip()[:300])
            print(f"[vpnfw] apply failed: {out.strip()}", flush=True)
            return False, out
        for builtin, chain in (("FORWARD", FWD), ("INPUT", INP)):
            if not _jump_first(ipt, builtin, chain):
                while _run([ipt, "-D", builtin, "-i", IFACE, "-j", chain])[0] == 0:
                    pass
                rc, out = _run([ipt, "-I", builtin, "1", "-i", IFACE, "-j", chain])
                if rc != 0:
                    _state.update(applied=False, error=out.strip()[:300])
                    return False, out
        _state.update(applied=True, error="")
        print(f"[vpnfw] isolation applied ({ipt}); operator devices: "
              f"{', '.join(ops) or 'none'}", flush=True)
        return True, "applied"


def remove(locked=False):
    def _do():
        ipt = _backend()
        for builtin, chain in (("FORWARD", FWD), ("INPUT", INP)):
            while _run([ipt, "-D", builtin, "-i", IFACE, "-j", chain])[0] == 0:
                pass
            _run([ipt, "-F", chain])
            _run([ipt, "-X", chain])
        _state.update(applied=False)
        return True, "removed"
    if locked:
        return _do()
    with _lock:
        return _do()


def status():
    """Read from the live firewall, not this process's memory — the CLI runs in
    a fresh process that never applied anything itself."""
    ipt = _backend()
    ops = operators()
    fwd, inp = desired_rules(ops)
    live_fwd, live_in = _chain_rules(ipt, FWD), _chain_rules(ipt, INP)
    jf, ji = _jump_first(ipt, "FORWARD", FWD), _jump_first(ipt, "INPUT", INP)
    return {**_state, "enabled": enabled(), "backend": ipt, "operators": ops,
            "applied": bool(jf and ji and live_fwd == fwd and live_in == inp),
            "live_fwd": live_fwd, "live_in": live_in, "jump_forward": jf, "jump_input": ji}


def _loop():
    while True:
        try:
            apply()
        except Exception as e:  # noqa: BLE001 — never kill the loop
            _state["error"] = str(e)
            print(f"[vpnfw] loop error: {e}", flush=True)
        time.sleep(LOOP_S)


def start():
    threading.Thread(target=_loop, daemon=True, name="vpnfw").start()


def _cli(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd in ("on", "off"):
        hubconfig.update({"vpn": {"isolation": cmd == "on"}})
        res = apply() if cmd == "on" else remove()
        print(f"isolation {cmd}: {res[1]}"
              + ("" if cmd == "on" else " (the running hub will not re-add it)"))
    elif cmd == "status":
        print(json.dumps(status(), indent=2))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
