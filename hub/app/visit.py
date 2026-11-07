"""The visit brief (Telegram phase 6): if someone must drive to a farm, they
drive ONCE and bring the right things. Built only from what the hub already
holds — its poller snapshot (last good status + devices + Pi health), the
last heartbeat over the farm's own internet, and the config backups.

  * what is wrong, since when, and the diagnosis (alive / dark / ...);
  * the devices that are down, with their GPS position as a map link;
  * the Pi (model, last temperature, undervoltage) and the spares that fit it;
  * spares by the kind of device that is down;
  * the last config backup (a swapped Pi is a restore, not a rebuild);
  * the farm's own location text from its settings.
"""
import time

import backups
import heartbeats
import tgapi
from tgbotcore import ago, dhm

SPARES = {
    "camera": "a spare camera of the same family, a PoE injector, RJ45 plugs + crimper",
    "nvr": "a known-good HDD and the NVR's power brick",
    "network": "a spare switch / PoE injector and patch leads",
    "internet-ap": "a spare radio, PoE injector, and the alignment tool",
    "radio": "a spare radio, PoE injector, and the alignment tool",
    "router": "a spare router with the site's config backup",
    "solar": "a multimeter and fuses for the solar controller",
}


def pi_spares(model):
    m = (model or "").lower()
    if "pi 3" in m:
        return "Pi 3 B+: official 5.1 V / 2.5 A micro-USB supply, powered USB hub for a disk, imaged SD card"
    if "pi 4" in m:
        return "Pi 4: official 5.1 V / 3 A USB-C supply, imaged SD/SSD"
    if "pi 5" in m:
        return "Pi 5: official 27 W USB-C supply, imaged SD/NVMe"
    return "the Pi's own power supply and an imaged SD card"


def _map(geo):
    try:
        return f"https://maps.google.com/?q={float(geo['lat']):.6f},{float(geo['lon']):.6f}"
    except (TypeError, ValueError, KeyError):
        return ""


def brief(site, snap, now=None):
    now = now or time.time()
    sid = site["id"]
    st = (snap or {}).get("status") or {}
    cfg = st.get("site") or {}
    name = cfg.get("name") or site.get("name") or sid
    lines = [f"🧰 <b>Visit brief · {tgapi.esc(name)}</b>"]
    if cfg.get("location"):
        lines.append(f"📍 {tgapi.esc(cfg['location'])}")
    site_map = _map(cfg)
    if site_map:
        lines.append(f'<a href="{site_map}">Farm on the map</a>')
    # state
    if (snap or {}).get("reachable"):
        lines.append("Hub link: ✅ reachable now")
    else:
        seen = (snap or {}).get("status_fetched")
        lines.append(f"Hub link: ❌ not reachable — last answer {dhm(seen) if seen else 'never'}")
        hb = heartbeats.latest(sid, now)
        if hb:
            lines.append(f"Last heartbeat {ago(hb['age'])} ago: "
                         f"{tgapi.esc(heartbeats.STATE_TEXT.get(hb.get('st'), hb.get('st')))}")
        elif heartbeats.is_vpn_site(site):
            lines.append("No heartbeat over its own internet → power or internet at the farm.")
    # devices down
    devs = ((snap or {}).get("devices") or {}).get("devices") or []
    down = [d for d in devs if d.get("watch") and not d.get("online")]
    kinds = set()
    if down:
        lines.append(f"\n<b>Down ({len(down)})</b>")
        for d in down[:12]:
            kinds.add(d.get("category") or "")
            link = _map(d.get("geo") or {})
            nm = tgapi.esc(d.get("name") or d.get("device_name") or d.get("ip") or "?")
            where = f' · <a href="{link}">map</a>' if link else ""
            model = f" · {tgapi.esc(d.get('model'))}" if d.get("model") else ""
            lines.append(f"• {nm} ({tgapi.esc(d.get('ip') or '-')}){model}{where}")
    elif devs:
        lines.append("\nNo monitored device is down (as last seen).")
    # the Pi
    si = (snap or {}).get("sysinfo") or {}
    th = si.get("throttled") or {}
    lines.append("\n<b>The Pi</b>")
    lines.append(f"{tgapi.esc(si.get('model') or 'model unknown')} · last {si.get('temp_c', '?')} °C"
                 + (" · ⚡ undervoltage" if th.get("undervoltage_now") or th.get("undervoltage_ever") else ""))
    # spares
    lines.append("\n<b>Bring</b>")
    lines.append("• " + tgapi.esc(pi_spares(si.get("model"))))
    for k in sorted(kinds):
        if SPARES.get(k):
            lines.append(f"• for the {tgapi.esc(k)}: {tgapi.esc(SPARES[k])}")
    lines.append("• laptop with the hub's VPN, a patch lead, a 12 V / PoE tester")
    # backups
    bl = backups.list_(sid)
    if bl:
        lines.append(f"\nLast config backup: {ago(now - bl[0]['ts'])} ago ({len(bl)} kept on the hub)")
    else:
        lines.append("\nNo config backup on the hub.")
    return "\n".join(lines)
