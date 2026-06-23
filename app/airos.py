"""EXPERIMENTAL: change a Ubiquiti airOS (airMAX) radio's management IP over SSH.

airOS has no clean documented API like Hikvision's ISAPI, so this edits the
running config (/tmp/system.cfg) over SSH and persists it: it rewrites the
`netconf.N.ip` (+ netmask) entries that currently hold the radio's management
IP, updates the default-route gateway, then `cfgmtd -w` + reboot.

This is a blunt instrument on WIRELESS BACKHAUL — a wrong value can drop the
link to a whole building. It is gated behind a Settings feature flag and uses
the device's saved SSH credentials. Uses the system ssh client via sshpass (no
extra Python deps), with legacy algorithms enabled for older airOS firmware.
"""
import re
import subprocess

CFG_PATH = "/tmp/system.cfg"

_SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "ConnectTimeout=8",
    "-o", "NumberOfPasswordPrompts=1",
    # older airOS firmware speaks legacy key/host algorithms
    "-o", "HostKeyAlgorithms=+ssh-rsa",
    "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa",
]


def _ssh(ip, user, password, command, timeout=20, stdin_data=None):
    """Run a remote command over SSH (password auth via sshpass).
    Returns (rc, stdout, stderr); rc=255 is SSH/connection failure."""
    cmd = ["sshpass", "-p", password, "ssh", *_SSH_OPTS,
           f"{user}@{ip}", command]
    try:
        r = subprocess.run(cmd, input=stdin_data, capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except (OSError, subprocess.SubprocessError) as e:
        return 255, "", str(e)


def _read_cfg(ip, user, password):
    rc, out, err = _ssh(ip, user, password, f"cat {CFG_PATH}")
    if rc == 0 and "netconf." in out:
        return out
    return None


def _kv(text):
    d = {}
    for line in text.splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def get_network(ip, user, password):
    """Read the radio's current management IP / netmask / gateway over SSH."""
    if not ip:
        return {"ok": False, "error": "no IP"}
    if not (user and password):
        return {"ok": False, "error": "save the radio's SSH username/password first"}
    text = _read_cfg(ip, user, password)
    if text is None:
        return {"ok": False, "error": "SSH read failed — check credentials / SSH access"}
    kv = _kv(text)
    # the netconf entry whose .ip equals the address we reached = the mgmt iface
    idx = next((m.group(1) for k, v in kv.items()
                if (m := re.match(r"netconf\.(\d+)\.ip$", k)) and v == ip), None)
    mask = kv.get(f"netconf.{idx}.netmask", "") if idx else ""
    gw = next((v for k, v in kv.items() if re.match(r"route\.\d+\.gateway$", k) and v), "")
    return {"ok": True, "ipAddress": ip, "subnetMask": mask, "gateway": gw,
            "interface_index": idx}


def _edit_cfg(text, cur_ip, new_ip, mask, gateway):
    """Rewrite the mgmt IP everywhere netconf.*.ip holds it; update its netmask
    and the default gateway. Returns (new_text, changes[])."""
    out, changes = [], []
    for line in text.splitlines():
        m = re.match(r"(netconf\.(\d+)\.ip)=(.*)$", line)
        if m and m.group(3).strip() == cur_ip:
            out.append(f"{m.group(1)}={new_ip}")
            changes.append(f"{m.group(1)}: {cur_ip} -> {new_ip}")
            continue
        if mask:
            mm = re.match(r"(netconf\.\d+\.netmask)=(.*)$", line)
            # only touch netmask lines on an iface that had the mgmt IP — approximate
            # by updating any netmask whose value differs; safe for single-subnet radios
            if mm and mm.group(2).strip() != mask and _same_iface_as_ip(text, mm.group(1), cur_ip):
                out.append(f"{mm.group(1)}={mask}")
                changes.append(f"{mm.group(1)}: {mm.group(2).strip()} -> {mask}")
                continue
        if gateway:
            gm = re.match(r"(route\.\d+\.gateway)=(.*)$", line)
            if gm and gm.group(2).strip() != gateway:
                out.append(f"{gm.group(1)}={gateway}")
                changes.append(f"{gm.group(1)}: {gm.group(2).strip()} -> {gateway}")
                continue
        out.append(line)
    return "\n".join(out) + "\n", changes


def _same_iface_as_ip(text, netmask_key, cur_ip):
    idx = netmask_key.split(".")[1]
    return f"netconf.{idx}.ip={cur_ip}" in text


def set_ip(ip, user, password, new_ip, mask="", gateway=""):
    """Change the radio's management IP via SSH config edit + reboot. Returns
    {'ok', 'new_ip', 'changes'} or {'ok': False, 'error'}."""
    if not (ip and new_ip):
        return {"ok": False, "error": "missing current or new IP"}
    if not (user and password):
        return {"ok": False, "error": "save the radio's SSH username/password first"}
    text = _read_cfg(ip, user, password)
    if text is None:
        return {"ok": False, "error": "SSH read failed — check credentials / SSH access"}
    new_text, changes = _edit_cfg(text, ip, new_ip, mask, gateway)
    if not any(c.startswith("netconf.") and ".ip:" in c for c in changes):
        return {"ok": False, "error": f"could not find the management IP ({ip}) in the "
                f"radio's config — it may be on DHCP or an unusual layout"}
    # write the edited config back over stdin (avoids shell-quoting the whole file)
    rc, _out, err = _ssh(ip, user, password, f"cat > {CFG_PATH}", stdin_data=new_text)
    if rc != 0:
        return {"ok": False, "error": f"writing config failed (rc {rc}) {err[:120]}"}
    # persist to flash and reboot to apply (connection drops as it goes down)
    _ssh(ip, user, password, "cfgmtd -w -p /etc/ ; reboot", timeout=12)
    return {"ok": True, "new_ip": new_ip, "changes": changes,
            "msg": f"Radio is rebooting to apply {new_ip} (back in ~1–2 min)."}
