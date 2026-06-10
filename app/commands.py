"""On-demand network probes (ping / port / traceroute) and a text-command
dispatcher shared by the dashboard buttons and the ntfy remote-control listener.

All probes use nmap (already in the image) so no extra packages are needed.

Safety: commands that name an IP/CIDR are only allowed against addresses inside
the site's configured target networks (or a local interface subnet). This stops
a public ntfy topic from being used to scan arbitrary internet hosts.
"""
import ipaddress
import re
import subprocess

import config

COMMANDS = ("help", "status", "ping", "port", "tracert", "traceroute",
            "scan", "quickscan", "deepscan", "test", "quality")


def _run(args, timeout):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError, subprocess.SubprocessError):
        return None


def _allowed_networks(cfg):
    nets = []
    for t in cfg.get("targets", []):
        try:
            nets.append(ipaddress.ip_network(t["cidr"], strict=False))
        except (ValueError, KeyError):
            pass
    # also allow the Pi's own local subnets
    from scanner import scanner
    nets += [n for n in scanner.local_networks() if not n.is_loopback]
    return nets


def ip_allowed(ip, cfg=None):
    cfg = cfg or config.load()
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in n for n in _allowed_networks(cfg))


def cidr_allowed(cidr, cfg=None):
    cfg = cfg or config.load()
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False
    return any(net.subnet_of(n) or net.overlaps(n) for n in _allowed_networks(cfg))


# ---- individual probes -------------------------------------------------
def ping(ip):
    if not ip_allowed(ip):
        return f"refused: {ip} is outside configured networks"
    r = _run(["nmap", "-sn", "-n", ip], 20)
    if not r:
        return f"ping {ip}: timed out"
    if "Host is up" in r.stdout:
        m = re.search(r"Host is up \(([^)]+)\)", r.stdout)
        return f"ping {ip}: UP" + (f" ({m.group(1)})" if m else "")
    return f"ping {ip}: no response"


def port_check(ip, port):
    if not ip_allowed(ip):
        return f"refused: {ip} is outside configured networks"
    try:
        port = int(port)
        assert 1 <= port <= 65535
    except (ValueError, AssertionError):
        return f"invalid port: {port}"
    r = _run(["nmap", "-n", "-Pn", "-p", str(port), ip], 30)
    if not r:
        return f"port {ip}:{port}: timed out"
    m = re.search(rf"{port}/tcp\s+(\S+)\s*(\S*)", r.stdout)
    if m:
        svc = f" ({m.group(2)})" if m.group(2) else ""
        return f"port {ip}:{port}: {m.group(1).upper()}{svc}"
    return f"port {ip}:{port}: no result"


def traceroute(ip):
    if not ip_allowed(ip):
        return f"refused: {ip} is outside configured networks"
    r = _run(["nmap", "-sn", "-n", "--traceroute", ip], 60)
    if not r:
        return f"tracert {ip}: timed out"
    hops = []
    capture = False
    for line in r.stdout.splitlines():
        if line.startswith("TRACEROUTE"):
            capture = True
            continue
        if capture:
            m = re.match(r"\s*(\d+)\s+([\d.]+ ms|\.\.\.)\s+([\d.]+)?", line)
            if m and m.group(3):
                hops.append(f"{m.group(1)}. {m.group(3)} ({m.group(2)})")
            elif not line.strip():
                break
    if not hops:
        return f"tracert {ip}: direct (same subnet) or no hops"
    return f"tracert {ip}:\n" + "\n".join(hops[:15])


def _rate(loss, avg, jitter):
    """Map loss% / avg-latency-ms / jitter-ms to a connection-quality grade."""
    if loss >= 100:
        return "down"
    if loss > 5 or avg > 200 or jitter > 50:
        return "poor"
    if loss > 1 or avg > 100 or jitter > 30:
        return "fair"
    if loss == 0 and avg < 50 and jitter < 10:
        return "excellent"
    return "good"


def quality_test(ip, count=20):
    """Run an ICMP ping burst and summarise connection quality as a dict."""
    if not ip_allowed(ip):
        return {"ok": False, "error": f"refused: {ip} is outside configured networks"}
    count = max(5, min(int(count), 50))
    r = _run(["ping", "-n", "-c", str(count), "-i", "0.2", "-W", "1", ip], count + 20)
    if not r:
        return {"ok": False, "ip": ip, "error": "timed out"}
    out = r.stdout
    loss = 100.0
    m = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received.*?([\d.]+)% packet loss",
                  out, re.S)
    sent = recv = 0
    if m:
        sent, recv, loss = int(m.group(1)), int(m.group(2)), float(m.group(3))
    stats = {"ok": True, "ip": ip, "sent": sent, "recv": recv, "loss": round(loss, 1),
             "min": None, "avg": None, "max": None, "jitter": None}
    rm = re.search(r"=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)\s*ms", out)
    if rm:
        stats.update(min=float(rm.group(1)), avg=float(rm.group(2)),
                     max=float(rm.group(3)), jitter=float(rm.group(4)))
    stats["rating"] = _rate(loss, stats["avg"] or 0, stats["jitter"] or 0)
    if loss >= 100:
        stats["note"] = "no ICMP reply — device may be down or blocking ping"
    return stats


def quality_text(ip):
    s = quality_test(ip)
    if not s.get("ok"):
        return f"conn test {ip}: {s.get('error', 'failed')}"
    if s["avg"] is None:
        return f"conn test {ip}: {s['rating'].upper()} — {s['loss']}% loss ({s.get('note', '')})"
    return (f"conn test {ip}: {s['rating'].upper()}\n"
            f"loss {s['loss']}% | avg {s['avg']}ms | jitter {s['jitter']}ms "
            f"(min {s['min']} / max {s['max']})")


def status():
    from scanner import scanner
    st = scanner.get_status()
    n = len(scanner.get_devices())
    online = sum(1 for d in scanner.get_devices() if d.get("online"))
    return (f"status: {'scanning ' + (st['mode'] or '') if st['is_scanning'] else 'idle'}; "
            f"{online}/{n} devices online; last scan {st['last_scan']}")


HELP = ("Netwatch commands:\n"
        "ping <ip>\n"
        "port <ip> <port>\n"
        "tracert <ip>\n"
        "test <ip>   (connection quality)\n"
        "quickscan [cidr]\n"
        "deepscan <ip|cidr>\n"
        "status")


def dispatch(text):
    """Parse a text command and run it. Returns a result string, or None if the
    text is not a recognised command (so the listener ignores chatter)."""
    if not text:
        return None
    parts = text.strip().split()
    cmd = parts[0].lower()
    args = parts[1:]
    if cmd not in COMMANDS:
        return None
    if cmd == "help":
        return HELP
    if cmd == "status":
        return status()
    if cmd == "ping":
        return ping(args[0]) if args else "usage: ping <ip>"
    if cmd == "port":
        return port_check(args[0], args[1]) if len(args) >= 2 else "usage: port <ip> <port>"
    if cmd in ("tracert", "traceroute"):
        return traceroute(args[0]) if args else "usage: tracert <ip>"
    if cmd in ("test", "quality"):
        return quality_text(args[0]) if args else "usage: test <ip>"
    if cmd in ("scan", "quickscan"):
        from scanner import scanner
        tgt = args[0] if args else None
        if tgt and not cidr_allowed(tgt):
            return f"refused: {tgt} is outside configured networks"
        scanner.trigger("quick", target=tgt)
        return f"quick scan started{(' on ' + tgt) if tgt else ''}"
    if cmd == "deepscan":
        from scanner import scanner
        if not args:
            scanner.trigger("deep")
            return "deep scan started on primary target"
        tgt = args[0]
        if "/" in tgt:
            if not cidr_allowed(tgt):
                return f"refused: {tgt} is outside configured networks"
            scanner.trigger("deep", target=tgt)
            return f"deep scan started on {tgt}"
        if not ip_allowed(tgt):
            return f"refused: {tgt} is outside configured networks"
        scanner.trigger("deep", hosts=[tgt])
        return f"deep scan started on host {tgt}"
    return None
