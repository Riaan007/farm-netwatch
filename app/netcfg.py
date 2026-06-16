"""Host network configuration.

Two independent capabilities:

1. **Managed secondary IPs** (always available, `ip` only): extra addresses Netwatch
   adds with `ip addr add` at boot + on change, so the Pi sits on several subnets at
   once and nmap ARP-scans each. ADDITIVE only — it never deletes the primary/DHCP
   address, so a bad entry can't cut the Pi off. Re-applied every boot (reboot-durable
   without touching NetworkManager).

2. **DHCP <-> static via NetworkManager** (opt-in image only — needs nmcli + the host
   D-Bus socket; see docker-compose.netcfg.yml). Feature-detected: `nm_available()` is
   False on the lean image / a non-NM host and the UI hides the static control. A static
   change is applied behind an **auto-revert watchdog**: snapshot -> apply -> revert
   after a timeout unless confirmed, with the pending state persisted to /data so a
   reboot or operator lockout self-heals.
"""
import ipaddress
import json
import os
import re
import subprocess
import threading
import time

import config

DATA_DIR = os.environ.get("NETWATCH_DATA", "/data")
PENDING_PATH = os.path.join(DATA_DIR, "network_pending.json")
DEFAULT_REVERT_S = 120
_SKIP_IFACE = ("lo", "docker", "br-", "veth", "wg", "tailscale")


def _run(args, timeout=20):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, f"{args[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"{args[0]}: timed out"
    except Exception as e:  # noqa: BLE001
        return 1, str(e)


# ---- managed secondary addresses (ip only) -------------------------------

def list_interfaces():
    """[{iface, addrs:[cidr,…]}] for real NICs (skips lo/docker/bridges/vpn)."""
    rc, out = _run(["ip", "-o", "-4", "addr", "show"])
    ifs = {}
    if rc == 0:
        for line in out.splitlines():
            p = line.split()
            if len(p) < 4 or p[1].startswith(_SKIP_IFACE):
                continue
            ifs.setdefault(p[1], []).append(p[3])
    return [{"iface": k, "addrs": v} for k, v in ifs.items()]


def _addr_present(iface, cidr):
    rc, out = _run(["ip", "-o", "-4", "addr", "show", "dev", iface])
    return rc == 0 and ("inet " + cidr) in out


def apply_addresses(cfg=None):
    """Add every configured secondary address that isn't already present. Only adds —
    never deletes — so the primary link is never disturbed."""
    cfg = cfg or config.load()
    for a in cfg.get("network", {}).get("addresses", []):
        iface, cidr = a.get("iface"), a.get("cidr")
        if not (iface and cidr):
            continue
        try:
            ipaddress.ip_interface(cidr)
        except ValueError:
            continue
        if not _addr_present(iface, cidr):
            rc, out = _run(["ip", "addr", "add", cidr, "dev", iface])
            print(f"[netcfg] add {cidr} dev {iface} -> rc{rc} {out}".strip(), flush=True)


def remove_address(iface, cidr):
    if _addr_present(iface, cidr):
        _run(["ip", "addr", "del", cidr, "dev", iface])


def derive_network_cidr(host_cidr):
    return str(ipaddress.ip_interface(host_cidr).network)


def sync_targets(cfg):
    """Keep a managed scan target per address with target:true (idempotent); drop
    managed targets no longer backed by an address. Never removes hand-made targets."""
    addresses = cfg.get("network", {}).get("addresses", [])
    targets = cfg.setdefault("targets", [])
    wanted = {}
    for a in addresses:
        if a.get("target") and a.get("cidr"):
            try:
                wanted[derive_network_cidr(a["cidr"])] = a.get("label") or "Extra LAN"
            except ValueError:
                pass
    have = {t.get("cidr") for t in targets}
    for net, label in wanted.items():
        if net not in have:
            targets.append({"cidr": net, "label": label, "local": "auto", "managed": True})
    cfg["targets"] = [t for t in targets
                      if not (t.get("managed") and t.get("cidr") not in wanted)]
    return cfg


# ---- NetworkManager (nmcli) — opt-in, feature-detected -------------------

_nm_cache = None


def nm_available():
    global _nm_cache
    if _nm_cache is None:
        rc, out = _run(["nmcli", "-t", "-f", "RUNNING", "general"], timeout=8)
        _nm_cache = rc == 0 and "running" in out.lower()
    return _nm_cache


def _default_dev():
    rc, out = _run(["ip", "route", "show", "default"])
    m = re.search(r"dev\s+(\S+)", out) if rc == 0 else None
    return m.group(1) if m else None


def _nm_kv(out):
    """Parse `nmcli -t -f a,b,c con show` 'field:value' lines into a dict."""
    d = {}
    for line in out.splitlines():
        m = re.match(r"^([^:]+):(.*)$", line)
        if m:
            d[m.group(1)] = m.group(2)
    return d


_IPV4 = ["ipv4.method", "ipv4.addresses", "ipv4.gateway", "ipv4.dns"]


def read_ipv4(con):
    rc, out = _run(["nmcli", "-t", "-f", ",".join(_IPV4), "connection", "show", con])
    d = _nm_kv(out) if rc == 0 else {}
    return {"method": d.get("ipv4.method", ""), "addresses": d.get("ipv4.addresses", ""),
            "gateway": d.get("ipv4.gateway", ""), "dns": d.get("ipv4.dns", "")}


def list_connections():
    rc, out = _run(["nmcli", "-t", "-f", "NAME,DEVICE,TYPE", "connection", "show", "--active"])
    cons = []
    if rc == 0:
        primary = _default_dev()
        for line in out.splitlines():
            f = re.split(r"(?<!\\):", line)
            if len(f) >= 3 and f[1] and f[1] != "--" and not f[1].startswith(_SKIP_IFACE):
                name = f[0].replace("\\:", ":")
                c = {"name": name, "device": f[1], "type": f[2],
                     "primary": f[1] == primary}
                c.update(read_ipv4(name))
                cons.append(c)
    return cons


def _con_up(con):
    return _run(["nmcli", "connection", "up", con], timeout=45)


def _mod(con, fields):
    args = ["nmcli", "connection", "modify", con]
    for k, v in fields:
        args += [k, v]
    return _run(args)


def set_static(con, ip_prefix, gateway, dns):
    rc, out = _mod(con, [("ipv4.method", "manual"), ("ipv4.addresses", ip_prefix),
                         ("ipv4.gateway", gateway or ""), ("ipv4.dns", dns or "")])
    if rc != 0:
        return False, out
    rc, out = _con_up(con)
    return rc == 0, out


def restore_ipv4(con, snap):
    _mod(con, [("ipv4.method", snap.get("method") or "auto"),
               ("ipv4.addresses", snap.get("addresses") or ""),
               ("ipv4.gateway", snap.get("gateway") or ""),
               ("ipv4.dns", snap.get("dns") or "")])
    _con_up(con)


def set_dhcp(con):
    rc, out = _mod(con, [("ipv4.method", "auto"), ("ipv4.addresses", ""),
                         ("ipv4.gateway", ""), ("ipv4.dns", "")])
    if rc != 0:
        return False, out
    rc, out = _con_up(con)
    return rc == 0, out


# ---- auto-revert watchdog -------------------------------------------------

_lock = threading.Lock()
_active_cancel = None       # Event for the currently-armed revert


def _write_pending(p):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = PENDING_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(p, f)
    os.replace(tmp, PENDING_PATH)


def _read_pending():
    try:
        with open(PENDING_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _clear_pending():
    try:
        os.remove(PENDING_PATH)
    except FileNotFoundError:
        pass


def _arm_revert(con, snap, deadline):
    global _active_cancel
    if _active_cancel:
        _active_cancel.set()                 # stop any previous armed revert
    cancel = threading.Event()
    _active_cancel = cancel

    def run():
        while time.time() < deadline:
            if cancel.wait(timeout=2):
                return                        # confirmed / superseded
        if not cancel.is_set():
            restore_ipv4(con, snap)
            _clear_pending()
            print(f"[netcfg] auto-reverted {con} (change unconfirmed)", flush=True)
    threading.Thread(target=run, daemon=True).start()


def apply_static_with_revert(con, ip_prefix, gateway, dns, revert_s=DEFAULT_REVERT_S):
    """Snapshot -> apply static -> arm the auto-revert. Returns (ok, msg, deadline_ts)."""
    snap = read_ipv4(con)
    if snap.get("method") == "manual" and snap.get("addresses") == ip_prefix:
        return False, "Already set to that static address.", None
    ok, msg = set_static(con, ip_prefix, gateway, dns)
    if not ok:
        return False, msg or "nmcli failed", None
    deadline = int(time.time()) + int(revert_s)
    with _lock:
        _write_pending({"connection": con, "snapshot": snap,
                        "applied_ts": int(time.time()), "revert_deadline_ts": deadline,
                        "reason": f"static {ip_prefix}"})
        _arm_revert(con, snap, deadline)
    return True, "", deadline


def confirm_static():
    global _active_cancel
    if _active_cancel:
        _active_cancel.set()
    _clear_pending()


def switch_to_dhcp(con):
    """Immediate revert to DHCP and cancel any armed revert."""
    confirm_static()
    return set_dhcp(con)


def pending_state():
    p = _read_pending()
    if not p:
        return None
    return {"connection": p["connection"], "revert_deadline_ts": p["revert_deadline_ts"],
            "reason": p.get("reason", "")}


def recover_pending():
    """Boot recovery: a pending file means a static change was never confirmed (we
    restarted) — restore the snapshot. Self-heals a lockout."""
    p = _read_pending()
    if not p:
        return
    print(f"[netcfg] boot recovery: reverting unconfirmed change on {p['connection']}",
          flush=True)
    if nm_available():
        restore_ipv4(p["connection"], p["snapshot"])
    _clear_pending()
