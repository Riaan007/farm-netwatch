"""ntfy push alerts for the hub (mirrors the site's notify.push).

Used to alert when a farm site drops off the hub (or returns). A blank topic
disables alerts; failures are swallowed so a flaky link never breaks the poller.
"""
import requests

import telegram


def _server(alerts):
    return (alerts.get("ntfy_server") or "https://ntfy.sh").rstrip("/")


def level_for(priority):
    p = str(priority or "default").lower()
    if p in ("urgent", "max", "5"):
        return "critical"
    if p in ("high", "4"):
        return "warning"
    return "info"


def push(alerts, title, message, priority="default", tags=None, *, site=None, level=None,
         tg_title=None, ref=None, reply_ref=None, resolves=None, telegram_too=True,
         site_buttons=None):
    """ntfy push + (Telegram phase 1) the same alert to the Ops group and, with
    `site=`, that site's group. `tg_title` is the title without the site name
    (Telegram puts the name in front); `ref`/`resolves` tie an incident's
    "back" message to its first one, which gets edited with the outcome."""
    if telegram_too:
        telegram.alert(level or level_for(priority), tg_title or title, message, site=site,
                       ref=ref, reply_ref=reply_ref, resolves=resolves, site_buttons=site_buttons)
    topic = (alerts or {}).get("ntfy_topic", "").strip()
    if not topic:
        return None
    # ntfy headers are latin-1; drop anything it can't encode (e.g. emoji).
    safe_title = " ".join((title or "").split())          # no CR/LF in a header
    safe_title = safe_title.encode("latin-1", "ignore").decode("latin-1") or "Netwatch Hub"
    headers = {"Title": safe_title, "Priority": priority}
    if tags:
        headers["Tags"] = ",".join(tags)
    try:
        r = requests.post(f"{_server(alerts)}/{topic}", data=message.encode("utf-8"),
                          headers=headers, timeout=6)
        return r.status_code < 300
    except requests.RequestException:
        return False
