"""Read a site's Uptime Kuma through its public status-page JSON endpoints.

Kuma 1.x exposes, without auth, for any *published* status page:
  GET /api/status-page/<slug>            -> page config + publicGroupList
  GET /api/status-page/heartbeat/<slug>  -> last ~50 beats + 24h uptime per monitor

Only monitors placed in a public group of the page appear, so the per-site
setup is: Kuma UI -> Status Pages -> create slug (default "farm"), add the
monitors to a group, publish.

Trap (verified in Kuma 1.23.17 source): requesting a slug that does not exist
returns NO HTTP response at all — the request just hangs. Every call here must
use a short read timeout, and a read timeout is reported as "no-status-page"
(distinct from a connect error = Kuma itself unreachable).
"""
import requests

# Kuma beat status codes: 0=down 1=up 2=pending 3=maintenance


def fetch(kuma_url, slug, timeout=(3, 6)):
    base = (kuma_url or "").rstrip("/")
    slug = (slug or "").strip()
    if not (base and slug):
        return {"ok": False, "reason": "not-configured"}

    try:
        page = requests.get(f"{base}/api/status-page/{slug}", timeout=timeout)
        page.raise_for_status()
        page = page.json()
        beats = requests.get(f"{base}/api/status-page/heartbeat/{slug}", timeout=timeout)
        beats.raise_for_status()
        beats = beats.json()
    except requests.exceptions.ReadTimeout:
        return {"ok": False, "reason": "no-status-page"}
    except (requests.exceptions.ConnectionError, requests.exceptions.ConnectTimeout):
        return {"ok": False, "reason": "unreachable"}
    except (requests.exceptions.RequestException, ValueError):
        return {"ok": False, "reason": "error"}

    heartbeat_list = beats.get("heartbeatList") or {}
    uptime_list = beats.get("uptimeList") or {}

    monitors, up, down = [], 0, 0
    for group in page.get("publicGroupList") or []:
        for mon in group.get("monitorList") or []:
            mid = mon.get("id")
            mbeats = heartbeat_list.get(str(mid)) or []
            last = mbeats[-1] if mbeats else None
            last_status = last.get("status") if last else None
            if last_status == 1:
                up += 1
            elif last_status is not None:
                down += 1
            uptime = uptime_list.get(f"{mid}_24")
            monitors.append({
                "id": mid,
                "name": mon.get("name", ""),
                "group": group.get("name", ""),
                "beats": [b.get("status") for b in mbeats],
                "ping": last.get("ping") if last else None,
                "uptime_24h": round(uptime * 100, 1) if uptime is not None else None,
                "last_status": last_status,
            })

    return {
        "ok": True,
        "title": (page.get("config") or {}).get("title", ""),
        "monitors": monitors,
        "up": up,
        "down": down,
        "url": f"{base}/status/{slug}",
    }
