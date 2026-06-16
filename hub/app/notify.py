"""ntfy push alerts for the hub (mirrors the site's notify.push).

Used to alert when a farm site drops off the hub (or returns). A blank topic
disables alerts; failures are swallowed so a flaky link never breaks the poller.
"""
import requests


def _server(alerts):
    return (alerts.get("ntfy_server") or "https://ntfy.sh").rstrip("/")


def push(alerts, title, message, priority="default", tags=None):
    topic = (alerts or {}).get("ntfy_topic", "").strip()
    if not topic:
        return None
    # ntfy headers are latin-1; drop anything it can't encode (e.g. emoji).
    safe_title = (title or "Netwatch Hub").encode("latin-1", "ignore").decode("latin-1") \
        or "Netwatch Hub"
    headers = {"Title": safe_title, "Priority": priority}
    if tags:
        headers["Tags"] = ",".join(tags)
    try:
        r = requests.post(f"{_server(alerts)}/{topic}", data=message.encode("utf-8"),
                          headers=headers, timeout=6)
        return r.status_code < 300
    except requests.RequestException:
        return False
