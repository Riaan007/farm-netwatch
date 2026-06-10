"""Push alerts via ntfy (https://ntfy.sh or a self-hosted server).

A blank topic disables alerts entirely. Failures are swallowed so a flaky
internet link never stops the scanner. `push` returns the published message id
(used by the command listener to ignore its own messages).

`actions` are ntfy action buttons. We use `http` actions that POST a command
back to the same topic, so tapping "Deep scan" on a phone publishes
"deepscan <ip>" which the listener then executes.
"""
import requests


def _server(cfg_alerts):
    return (cfg_alerts.get("ntfy_server") or "https://ntfy.sh").rstrip("/")


def push(cfg_alerts, title, message, priority="default", tags=None, actions=None):
    topic = (cfg_alerts or {}).get("ntfy_topic", "").strip()
    if not topic:
        return None
    server = _server(cfg_alerts)
    # HTTP headers must be latin-1; emoji/unicode in the title would crash the
    # request. Keep accents, drop anything latin-1 can't represent (e.g. emoji).
    safe_title = (title or "Netwatch").encode("latin-1", "ignore").decode("latin-1") or "Netwatch"
    headers = {"Title": safe_title, "Priority": priority}
    if tags:
        headers["Tags"] = ",".join(tags)
    if actions:
        headers["Actions"] = "; ".join(actions)
    try:
        r = requests.post(f"{server}/{topic}", data=message.encode("utf-8"),
                          headers=headers, timeout=6)
        if r.status_code < 300:
            try:
                return r.json().get("id")
            except ValueError:
                return "ok"
        return None
    except requests.RequestException:
        return None


def _cmd_action(label, cfg_alerts, body):
    """Build an http action that publishes `body` as a command back to the topic."""
    topic = cfg_alerts.get("ntfy_topic", "").strip()
    url = f"{_server(cfg_alerts)}/{topic}"
    return f"http, {label}, {url}, method=POST, body='{body}', clear=true"


def notify_new_device(cfg_alerts, dev):
    if not cfg_alerts.get("notify_new", True):
        return None
    ports = ",".join(str(p) for p in dev.get("ports", [])[:6]) or "none"
    ip = dev.get("ip", "?")
    actions = []
    if cfg_alerts.get("allow_commands", True) and ip != "?":
        actions = [_cmd_action("Deep scan", cfg_alerts, f"deepscan {ip}"),
                   _cmd_action("Ping", cfg_alerts, f"ping {ip}")]
    return push(
        cfg_alerts,
        "New device on network",
        f"{ip} — {dev.get('vendor') or dev.get('type') or 'unknown'}\n"
        f"MAC {dev.get('mac') or 'n/a'} • ports {ports}",
        priority="high",
        tags=["warning", "satellite"],
        actions=actions,
    )


def notify_offline(cfg_alerts, dev):
    ip = dev.get("ip", "?")
    actions = []
    if cfg_alerts.get("allow_commands", True) and ip != "?":
        actions = [_cmd_action("Ping", cfg_alerts, f"ping {ip}")]
    return push(
        cfg_alerts,
        "Device offline",
        f"{dev.get('name') or dev.get('type') or 'Device'} ({ip}) went OFFLINE",
        priority="high",
        tags=["red_circle"],
        actions=actions,
    )


def notify_online(cfg_alerts, dev):
    """A device that was offline has come back."""
    ip = dev.get("ip", "?")
    actions = []
    if cfg_alerts.get("allow_commands", True) and ip != "?":
        actions = [_cmd_action("Ping", cfg_alerts, f"ping {ip}")]
    return push(
        cfg_alerts,
        "Device back online",
        f"{dev.get('name') or dev.get('type') or 'Device'} ({ip}) is back ONLINE",
        priority="default",
        tags=["green_circle"],
        actions=actions,
    )
