"""Wireless link diagnosis: from radio telemetry to "what is wrong and what to do".

radiomon.py raises alerts one metric at a time ("upload score 40"). A technician
needs the reading behind that: the same physical link seen from BOTH radios, what
the numbers point at together, and the steps to try — in the order that costs the
least (settings changed from the office before a trip up a mast).

Everything here is rules over the data already stored — no internet, no AI — so
it works on a farm whose uplink is the thing being diagnosed. Thresholds come
from radiomon so the page, the alerts and the hub's AI analysis never disagree.

Terms used in the output:
  AP end / station end  the access point and the radio that connects to it.
  download / upload     AP → station / station → AP (airMAX's own dl/ul).
  score                 airMAX link score 0-100 (modulation actually achieved).
"""
import bisect
import math
import time

from radiomon import ABS, DELTA, MIN_BASELINE

MAX_POINTS = 300
SETTLE_S = 3600          # baselines ignore the newest hour (same rule as history.radio_baseline)

# Antenna type by model, only where it changes the advice (a sector is aimed
# once at the area it covers; a dish is aimed at exactly one far end).
SECTOR_MODELS = ("liteap", "prismap", "sector", "ac-ptmp", "rocket prism", "nanostation")


# ---- small helpers -------------------------------------------------------
def _vals(rows, col):
    return [r[col] for r in rows if r.get(col) is not None]


def _median(vals):
    if not vals:
        return None
    s = sorted(vals)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


def _stat(rows, col, now):
    """now / avg / min / max / baseline over the window for one column."""
    vals = _vals(rows, col)
    if not vals:
        return None
    old = [r[col] for r in rows if r.get(col) is not None and r["ts"] <= now - SETTLE_S]
    # `recent` (median of the last 3 readings) is what the rules judge: one odd
    # poll — a truck in the path, a burst of rain — must not raise a finding.
    return {"now": vals[-1], "recent": _median(vals[-3:]), "avg": round(sum(vals) / len(vals), 1),
            "min": min(vals), "max": max(vals), "n": len(vals),
            "baseline": _median(old) if len(old) >= MIN_BASELINE else None}


def _pct_below(rows, col, limit):
    vals = _vals(rows, col)
    return round(100.0 * sum(1 for v in vals if v < limit) / len(vals)) if vals else None


def _dbm(v):
    return "—" if v is None else f"{v:.0f} dBm"


def _km(m):
    if m is None:
        return "—"
    return f"{m / 1000:.1f} km" if m >= 1000 else f"{m:.0f} m"


def _ago(ts, now):
    s = max(0, now - ts)
    if s < 5400:
        return f"{round(s / 60)} min ago"
    if s < 172800:
        return f"{round(s / 3600)} h ago"
    return f"{round(s / 86400)} days ago"


def _freq_mhz(v):
    try:
        return float(str(v).split()[0])
    except (TypeError, ValueError, IndexError):
        return None


def _is_sector(model):
    m = (model or "").lower()
    return any(x in m for x in SECTOR_MODELS)


def fresnel(distance_m, freq_mhz):
    """First Fresnel zone radius at mid-path and the 60 % that must stay clear."""
    if not distance_m or not freq_mhz:
        return None
    r = 8.656 * math.sqrt((distance_m / 1000.0) / (freq_mhz / 1000.0))
    return {"radius_m": round(r, 1), "clear_m": round(0.6 * r, 1)}


def hourly(rows, col, hour_of, poor_below=None):
    """Per hour of the day (24 slots, None where no data): the average of `col`,
    or with `poor_below` the % of readings under that value."""
    buckets = [[] for _ in range(24)]
    for r in rows:
        if r.get(col) is not None:
            buckets[hour_of(r["ts"])].append(r[col])
    if poor_below is None:
        return [round(sum(b) / len(b), 1) if b else None for b in buckets]
    return [round(100.0 * sum(1 for v in b if v < poor_below) / len(b)) if len(b) >= 3 else None
            for b in buckets]


DAY_HOURS = range(7, 19)


def worst_hours(poor_pct):
    """When a link's poor readings cluster: {'kind': 'day'|'night'|None, 'label', ...}.

    Works on the share of poor readings per hour, not the average score — a link
    that dips hard for a minute in every afternoon poll barely moves an hourly
    average but doubles its share of poor readings.
    """
    have = [v for v in poor_pct if v is not None]
    if len(have) < 18:
        return None
    overall = sum(have) / len(have)
    if overall < 5:
        return None
    bad = [h for h, v in enumerate(poor_pct) if v is not None and v >= overall + max(8, overall * 0.3)]
    if not bad or len(bad) > 14:
        return None
    day = [h for h in bad if h in DAY_HOURS]
    night = [h for h in bad if h not in DAY_HOURS]
    kind = "day" if len(day) >= 0.75 * len(bad) else "night" if len(night) >= 0.75 * len(bad) else None
    pick = day if kind == "day" else bad
    if kind == "night":       # wrap evening → morning: order from 19:00 on
        pick = sorted(night, key=lambda h: (h - 19) % 24)
    start, end = pick[0], pick[-1]
    worst = max(bad, key=lambda h: poor_pct[h])
    label = f"{start:02d}:00–{(end + 1) % 24:02d}:00"
    return {"kind": kind, "from": start, "to": (end + 1) % 24, "label": label, "hours": bad,
            "overall_pct": round(overall), "bad_pct": round(sum(poor_pct[h] for h in bad) / len(bad)),
            "worst_hour": worst}


def drops(radio_rows, link_rows, poll_gap_s):
    """Times the link vanished while its radio kept answering.

    Every poll writes one radio sample and one row per connected peer at the same
    ts, so a radio sample with no row for this peer is a poll where the link was
    down. Consecutive misses are one episode.
    """
    have = {r["ts"] for r in link_rows}
    if not have:
        return {"count": 0, "episodes": []}
    first = min(have)
    episodes, cur = [], None
    for r in radio_rows:
        ts = r["ts"]
        if ts < first:
            continue
        if ts not in have:
            if cur is None:
                cur = {"from": ts, "to": ts}
            else:
                cur["to"] = ts
        elif cur is not None:
            cur["to_back"] = ts
            episodes.append(cur)
            cur = None
    if cur is not None:
        episodes.append(cur)          # still down at the newest poll
    for e in episodes:
        e["minutes"] = round(((e.get("to_back") or e["to"] + poll_gap_s) - e["from"]) / 60)
    return {"count": len(episodes), "episodes": episodes}


def _thin(rows):
    step = max(1, -(-len(rows) // MAX_POINTS))       # ceil: never more than MAX_POINTS
    return rows[::step] if step > 1 else rows


def _step(text, where):
    return {"text": text, "where": where}         # where: "remote" (from the office) | "site"


# ---- assembling physical links -------------------------------------------
def _is_ap(radio):
    mode = str((radio.get("current") or {}).get("mode") or radio.get("mode_raw") or "")
    return mode.startswith("ap") or "access point" in str(radio.get("mode") or "").lower()


def _pair_links(radios):
    """One entry per physical link, whichever of its radios we can read."""
    by_mac = {r["key"].upper(): r for r in radios if r.get("ok")}
    pairs = {}
    for r in radios:
        if not r.get("ok"):
            continue
        for ln in r.get("links") or []:
            peer = (ln.get("peer") or "").upper()
            if not peer:
                continue
            pid = "|".join(sorted((r["key"].upper(), peer)))
            p = pairs.setdefault(pid, {"id": pid.replace(":", "").replace("|", "-").lower(),
                                       "views": {}})
            p["views"][r["key"].upper()] = (r, ln)
            p.setdefault("macs", (r["key"].upper(), peer))
    out = []
    for p in pairs.values():
        a, b = p["macs"]
        ra, rb = by_mac.get(a), by_mac.get(b)
        # Decide which end is the AP: a readable radio says so in its mode; else
        # the unreadable end is whatever the readable one is not.
        if ra and _is_ap(ra):
            ap_mac, sta_mac = a, b
        elif rb and _is_ap(rb):
            ap_mac, sta_mac = b, a
        elif ra and not _is_ap(ra):
            ap_mac, sta_mac = b, a
        else:
            ap_mac, sta_mac = a, b
        out.append({"id": p["id"], "ap_mac": ap_mac, "sta_mac": sta_mac,
                    "ap_view": p["views"].get(ap_mac), "sta_view": p["views"].get(sta_mac),
                    "ap_radio": by_mac.get(ap_mac), "sta_radio": by_mac.get(sta_mac)})
    return out


def _end(mac, radio, other_view, names):
    """What we know about one end of a link."""
    ln = other_view[1] if other_view else {}
    if radio:
        name = radio.get("name") or ""
        if not name or name == radio.get("ip") or name.lower() == radio["key"].lower():
            name = names.get(mac) or name
        model = radio.get("model") or names.get(mac + "#model") or ""
        return {"mac": mac, "key": radio["key"], "name": name or radio.get("ip") or mac,
                "ip": radio.get("ip") or ln.get("ip") or "", "model": model, "monitored": True}
    return {"mac": mac, "key": mac.lower(), "name": ln.get("name") or mac, "ip": ln.get("ip") or "",
            "model": ln.get("model") or "", "monitored": False}


# ---- the rules -------------------------------------------------------------
def _diagnose(L, now, hour_of):
    """Findings for one physical link. Each: level, title, evidence, causes, steps."""
    out = []
    ap, sta = L["ap"], L["sta"]
    m = L["metrics"]
    fz = L.get("fresnel")
    freq = m.get("freq_label") or "this channel"
    fz_txt = (f"At the middle of the path keep at least {fz['clear_m']:.1f} m clear around the "
              f"straight line between the two radios (60 % of the {fz['radius_m']:.1f} m Fresnel zone)"
              if fz else "Keep the middle of the path clear of trees and roofs")

    def add(level, fid, title, evidence, causes, steps, good=None):
        out.append({"id": fid, "level": level, "title": title,
                    "evidence": [e for e in evidence if e], "causes": causes,
                    "steps": steps, "good": good})

    s_ap, s_sta = m.get("signal_ap") or {}, m.get("signal_sta") or {}
    dl, ul = m.get("score_dl") or {}, m.get("score_ul") or {}
    sig_now = [v for v in (s_ap.get("recent"), s_sta.get("recent")) if v is not None]
    worst_sig = min(sig_now) if sig_now else None
    q_avg = min(v for v in (dl.get("avg"), ul.get("avg"), 100) if v is not None)
    chains = m.get("chains") or {}

    # 1. Weak signal (absolute)
    if worst_sig is not None and worst_sig <= ABS["signal"]["warn"]:
        lvl = "crit" if worst_sig <= ABS["signal"]["crit"] else "warn"
        add(lvl, "weak_signal", f"Weak signal ({worst_sig:.0f} dBm)",
            [f"{ap['name']} hears {sta['name']} at {_dbm(s_ap.get('recent'))}" if s_ap else "",
             f"{sta['name']} hears {ap['name']} at {_dbm(s_sta.get('recent'))}" if s_sta else "",
             f"Distance {_km(m.get('distance_m'))} on {freq}"],
            ["The dish has moved off aim (wind, a loose mount, someone bumped the pole)",
             "Something is now in the path: trees that grew, a new building, a shed roof",
             "Output power was turned down on one of the radios",
             "Water in a connector or a damaged cable at one end"],
            [_step(f"Check Output Power on both radios (airOS → Wireless) — it should not be turned down.", "remote"),
             _step(f"On {sta['name']} open Tools → Align Antenna. Loosen the mount, sweep slowly left/right, "
                   f"then up/down, and lock it where the signal peaks.", "site"),
             _step(("Re-aim the AP sector so it faces this station's direction." if _is_sector(ap.get("model"))
                    else f"Do the same fine alignment at {ap['name']}."), "site"),
             _step(fz_txt + ".", "site"),
             _step("Look for water in the connectors and replace any cracked weatherproofing.", "site")],
            good="Better than -65 dBm at both ends and a link score above 80.")

    # 2. Signal fell below its own normal — comparing both ends says where to look.
    drop_ap = (s_ap["baseline"] - s_ap["recent"]) if s_ap.get("baseline") is not None else None
    drop_sta = (s_sta["baseline"] - s_sta["recent"]) if s_sta.get("baseline") is not None else None
    worst_drop = max([d for d in (drop_ap, drop_sta) if d is not None] or [0])
    if worst_drop >= DELTA["signal_drop"]["warn"]:
        lvl = "crit" if worst_drop >= DELTA["signal_drop"]["crit"] else "warn"
        both = drop_ap is not None and drop_sta is not None and min(drop_ap, drop_sta) >= 4
        ev = [f"{ap['name']} hears {_dbm(s_ap.get('recent'))}, normally {_dbm(s_ap.get('baseline'))}" if drop_ap is not None else "",
              f"{sta['name']} hears {_dbm(s_sta.get('recent'))}, normally {_dbm(s_sta.get('baseline'))}" if drop_sta is not None else "",
              "Judged on the last three readings, so one odd poll does not count"]
        if both:
            causes = ["Both ends lost signal together, so it is the path or the aim — not one radio",
                      "A dish moved (storm, wind, loose bracket)",
                      "New obstruction in the path (tree growth, building, parked equipment)"]
            steps = [_step("Watch the Signal chart for a few hours: rain or a vehicle in the path gives a short dip in "
                           "both directions, a dish that moved stays down.", "remote"),
                     _step(f"If it stays down: on {sta['name']} use Tools → Align Antenna and re-peak the dish.", "site"),
                     _step(fz_txt + ".", "site")]
        else:
            quiet = ap if (drop_sta or 0) > (drop_ap or 0) else sta     # the end the OTHER one hears less of
            causes = [f"Only one direction got weaker, so {quiet['name']} is transmitting less",
                      f"Output power changed on {quiet['name']}",
                      f"Cable, connector or radio fault at {quiet['name']}"]
            steps = [_step(f"Check Output Power and any recent settings change on {quiet['name']}.", "remote"),
                     _step(f"Check {quiet['name']}'s uptime and log — a reboot or reset can drop power settings.", "remote"),
                     _step(f"Inspect the cable and connectors at {quiet['name']}.", "site")]
        add(lvl, "signal_drop", f"Signal {worst_drop:.0f} dB below normal", ev, causes, steps,
            good="Signal back within 3 dB of its normal level.")

    # 3. Strong signal but a poor score: not a strength problem.
    if worst_sig is not None and worst_sig > -68 and q_avg < 65:
        worse = "upload" if (ul.get("avg") or 100) <= (dl.get("avg") or 100) else "download"
        lvl = "crit" if q_avg < ABS["score"]["crit"] else "warn" if q_avg < ABS["score"]["warn"] + 10 else "info"
        causes = []
        if chains.get("gap_avg") is not None and chains["gap_avg"] >= 5:
            causes.append(f"The antenna chains differ by {chains['gap_avg']:.0f} dB on average — the dish "
                          f"polarisation or feed is not right (see below)")
        causes += ["Something partly blocks the path (trees near the line, a roof edge): the signal still "
                   "arrives strong but the data rate falls",
                   f"Interference from other radios on or near {freq}"]
        if _is_sector(ap.get("model")):
            causes.append(f"{sta['name']} may sit near the edge of {ap['name']}'s sector, where reflections "
                          f"(multipath) are worse")
        if m.get("ap_mixed"):
            causes.append(f"{ap['name']} runs airMAX Mixed mode (see the AP finding)")
        add(lvl, "poor_quality", f"Strong signal, but poor link quality (score {q_avg:.0f})",
            [f"Signal is good: {_dbm(s_ap.get('recent'))} at {ap['name']}, {_dbm(s_sta.get('recent'))} at {sta['name']}",
             f"Average link score {dl.get('avg', '—')} download / {ul.get('avg', '—')} upload over the period — {worse} is worse",
             f"Noise floor {_dbm((m.get('noise_ap') or {}).get('now'))} at the AP, "
             f"{_dbm((m.get('noise_sta') or {}).get('now'))} at the station" if m.get("noise_ap") or m.get("noise_sta") else ""],
            causes,
            [_step(f"Run Tools → airView on {ap['name']} and on {sta['name']} for a few minutes each. Look for "
                   f"other signals on or next to {freq}.", "remote"),
             _step("If the channel is busy, move the AP to the quietest channel. First check every station's "
                   "Frequency Scan List includes the new channel, or they will not reconnect.", "remote"),
             _step(fz_txt + " — trees close to the line are the usual cause when the signal is strong.", "site"),
             _step(f"Check {sta['name']}'s radio sits squarely in the dish feed and the dish is not rotated.", "site")],
            good="Link score above 80 in both directions.")

    # 4. One direction consistently worse: the receiving site hears interference.
    if dl.get("avg") is not None and ul.get("avg") is not None and abs(dl["avg"] - ul["avg"]) >= 15:
        up_worse = ul["avg"] < dl["avg"]
        rx_end = ap if up_worse else sta
        worse_avg = min(dl["avg"], ul["avg"])
        add("warn" if worse_avg < ABS["score"]["warn"] else "info", "asymmetric_quality",
            f"{'Upload' if up_worse else 'Download'} is worse than {'download' if up_worse else 'upload'}",
            [f"Average link score {dl['avg']:.0f} download vs {ul['avg']:.0f} upload",
             f"The weak direction is received at {rx_end['name']}"],
            [f"{rx_end['name']} hears interference that the other end does not",
             "A wide-angle antenna at that end (a sector) picks up more noise than a dish" if _is_sector(rx_end.get("model")) else
             "Other equipment near that radio (another AP, a mast full of radios) is too close in frequency"],
            [_step(f"Run Tools → airView on {rx_end['name']} and note what else transmits on or near {freq}.", "remote"),
             _step("Move to a cleaner channel if airView shows one.", "remote"),
             _step(f"Fit an isolator/shield kit at {rx_end['name']}, or move other radios on that mast further apart.", "site")],
            good="Both directions within about 10 points of each other.")

    # 5. Quality keeps dipping — and when.
    worse_col = "score_ul" if (ul.get("avg") or 100) <= (dl.get("avg") or 100) else "score_dl"
    pct = m.get("pct_below_50", {}).get(worse_col)
    if pct is not None and pct >= 10:
        wh = m.get("worst_hours")
        lvl = "warn" if pct >= 25 else "info"
        direction = "upload" if worse_col == "score_ul" else "download"
        causes = []
        if wh and wh["kind"] == "day":
            causes.append(f"It is worse by day ({wh['label']}) — daytime wind moving the mast, the dish or trees "
                          f"in the path, or more camera traffic while people are working")
        elif wh and wh["kind"] == "night":
            causes.append(f"It is worse in the evening and night ({wh['label']}) — neighbouring networks are busy "
                          f"then, or recordings/backups copy at that time")
        causes += ["Bursts of traffic (camera playback, recordings copied, updates) filling the link",
                   "Intermittent interference"]
        add(lvl, "unstable", f"Link quality keeps dipping ({pct}% of the time)",
            [f"{direction.capitalize()} score below 50 for {pct}% of the samples (lowest {(m[worse_col] or {}).get('min', '—')})",
             f"{wh['bad_pct']}% of readings are poor in {wh['label']}, against {wh['overall_pct']}% over the whole day" if wh else ""],
            causes,
            [_step(f"Check whether the dips line up with traffic: {sta['name']}'s air time and capacity charts below.", "remote"),
             _step(f"Run airView at {'the AP' if direction == 'upload' else sta['name']} during the worst hours" + (f" ({wh['label']})" if wh else "") + ".", "remote"),
             _step("Check the mast and brackets are rigid — a pole that sways in the wind shows exactly this pattern.", "site")],
            good="Below 50 for less than 5 % of the time.")

    # 6. Antenna chains out of balance (read at the station, where chains belong to this link).
    gap = chains.get("gap_avg")
    if gap is not None and gap >= 5:
        lvl = "crit" if gap >= ABS["chain_gap"]["crit"] else "warn" if gap >= ABS["chain_gap"]["warn"] else "info"
        add(lvl, "chain_gap", f"Antenna chains differ by {gap:.0f} dB",
            [f"{chains['radio']}: chain 0 {_dbm(chains.get('c0'))}, chain 1 {_dbm(chains.get('c1'))} now",
             f"Average difference over the period {gap:.1f} dB (normal is under 4 dB)"],
            ["The radio is not fully seated in the dish feed, or is rotated in it",
             "The two dishes are not at the same polarisation (one is twisted on its mount)",
             "Water in, or damage to, one of the feed connectors"],
            [_step(f"At {chains['radio']}: take the radio out of the dish, check the feed for water or corrosion, "
                   f"and click it back in squarely.", "site"),
             _step("Check both dishes are level (not rotated around their own axis).", "site"),
             _step("Replace damaged weatherproofing.", "site")],
            good="Chains within 3–4 dB of each other.")

    # 7. A steady difference between the two ends = unequal transmit power.
    if s_ap.get("avg") is not None and s_sta.get("avg") is not None:
        asym = s_ap["avg"] - s_sta["avg"]
        if abs(asym) >= 6:
            louder, quieter = (sta, ap) if asym > 0 else (ap, sta)
            add("warn" if abs(asym) >= 10 else "info", "power_mismatch",
                f"Ends differ by {abs(asym):.0f} dB",
                [f"{ap['name']} hears {s_ap['avg']:.0f} dBm, {sta['name']} hears {s_sta['avg']:.0f} dBm (averages)",
                 "A radio path loses the same in both directions, so a steady difference comes from the radios"],
                [f"{quieter['name']} is set to lower output power than {louder['name']}",
                 f"Automatic power control or a country power limit on {quieter['name']}",
                 f"A weak transmitter or damaged cable at {quieter['name']}"],
                [_step(f"Compare Output Power on {ap['name']} and {sta['name']} (airOS → Wireless) and match them.", "remote"),
                 _step(f"If the settings match, inspect {quieter['name']}'s cable and radio.", "site")],
                good="Both ends within about 5 dB.")

    # 8. Disconnections
    d = m.get("drops") or {}
    if d.get("count"):
        last = d["episodes"][-1]
        longest = max(e["minutes"] for e in d["episodes"])
        add("crit" if d["count"] >= 3 else "warn", "drops",
            f"Disconnected {d['count']} time{'s' if d['count'] != 1 else ''}",
            [f"Longest {longest} min, last {_ago(last['from'], now)}"
             + (" — still down at the last reading" if "to_back" not in last else ""),
             f"{ap['name']} kept answering meanwhile, so the link dropped, not the whole AP"],
            [f"{sta['name']} lost power (PoE injector, UPS or battery — check solar sites at night)",
             "Lightning or water damage to the cable",
             "Severe interference or a big alignment change"],
            [_step(f"Check {sta['name']}'s uptime in airOS: a short uptime means it rebooted — a power problem.", "remote"),
             _step(f"Check the power supply, PoE injector and surge protection at {sta['name']}.", "site")],
            good="No disconnections.")

    # 9. Capacity fell
    cap = m.get("capacity") or {}
    if cap.get("now") and cap.get("baseline"):
        frac = cap["now"] / cap["baseline"]
        if frac <= DELTA["capacity_drop"]["warn"]:
            add("crit" if frac <= DELTA["capacity_drop"]["crit"] else "warn", "capacity",
                f"Capacity down to {cap['now'] / 1000:.0f} Mbps",
                [f"Normally {cap['baseline'] / 1000:.0f} Mbps"],
                ["A weaker signal or more interference forced a lower data rate"],
                [_step("Work through the signal and quality findings above.", "remote")])

    # 10. Station suddenly busy
    at = m.get("airtime_sta") or {}
    if at.get("now") is not None and at["now"] >= 20 and (at.get("baseline") or 0) * 5 <= at["now"]:
        add("info", "traffic", f"{sta['name']} is much busier than usual",
            [f"Air time {at['now']:.0f}% now, normally {at.get('baseline') or 0:.0f}%"],
            ["Something behind this radio is sending a lot: camera playback, a recorder copying footage, an update"],
            [_step(f"Check which cameras/NVRs behind {sta['name']} are streaming, and lower their bitrate "
                   f"(H.265+, sub-stream for remote viewing).", "remote")])

    # 11. Far end not readable: half the picture is missing.
    if not sta["monitored"] or not ap["monitored"]:
        blind = sta if not sta["monitored"] else ap
        add("info", "blind_end", f"Can't see {blind['name']}'s side",
            [f"No SSH login saved for {blind['name']}" + (f" ({blind['ip']})" if blind["ip"] else "")],
            ["Chains, noise and capacity at that end are unknown, so some causes can't be told apart"],
            [_step(f"Save {blind['name']}'s SSH login under Logins — the next poll reads it.", "remote")])

    order = {"crit": 0, "warn": 1, "info": 2}
    out.sort(key=lambda f: order[f["level"]])
    return out


def _radio_findings(r, stations, now):
    """Findings that belong to a radio rather than one link (mostly the AP)."""
    out = []
    cur = r.get("current") or {}
    mode = str(cur.get("mode") or "")
    st = r.get("stats") or {}
    if not r.get("ok"):
        err = (r.get("error") or "").lower()
        if "refused" in err:
            why, fix = ("SSH is switched off on this device, or it is not an airOS radio",
                        "Enable the SSH server (airOS → Services), or switch radio monitoring off for this device if it isn't a radio.")
        elif "permission denied" in err or "auth" in err:
            why, fix = "The saved login is wrong", "Update the SSH username/password under Logins."
        else:
            why, fix = "The radio did not answer over SSH", "Check it is online; if it is, check SSH access and the saved login."
        return [{"id": "unreadable", "level": "info", "title": "Can't read this radio",
                 "evidence": [r.get("error") or "no reply"], "causes": [why],
                 "steps": [_step(fix, "remote")], "good": None}]
    if "mixed" in mode and stations and all("ac" in (s.get("model") or "").lower() for s in stations):
        out.append({"id": "mixed_mode", "level": "warn", "title": "AP in airMAX Mixed mode, but every station is AC",
                    "evidence": [f"Wireless mode {mode}",
                                 f"{len(stations)} connected station{'s' if len(stations) != 1 else ''}: "
                                 + ", ".join(sorted({s.get('model') or '?' for s in stations}))],
                    "causes": ["Mixed mode keeps older airMAX M radios able to connect and gives up some AC "
                               "performance for every station on this AP"],
                    "steps": [_step("On the AP set airOS → Wireless → Wireless Mode to Access Point PtMP airMAX AC and save. "
                                    "Every connected station is an AC radio, so none needs Mixed mode.", "remote"),
                              _step("Before saving, check each station shows airOS 8 firmware; do it when someone could "
                                    "reach the AP if a station does not come back.", "remote")],
                    "good": "Higher link scores and capacity on the AP's links."})
    air = st.get("airtime") or {}
    if air.get("max") is not None and (air["max"] >= ABS["airtime"]["warn"] or (air.get("avg") or 0) >= 50):
        out.append({"id": "airtime", "level": "crit" if (air.get("avg") or 0) >= ABS["airtime"]["warn"] else "warn",
                    "title": f"Air time peaks at {air['max']:.0f}%",
                    "evidence": [f"Average {air['avg']:.0f}%, now {air['now']:.0f}%"],
                    "causes": ["The channel is shared by every station — heavy camera streams fill it"],
                    "steps": [_step("Lower camera bitrates behind the busiest stations (H.265+, sub-streams).", "remote"),
                              _step("Split heavy stations onto their own point-to-point link or a second AP.", "site")],
                    "good": "Air time under 60 % at the busiest time."})
    noise = st.get("noise") or {}
    if noise.get("now") is not None:
        rise = noise["now"] - noise["baseline"] if noise.get("baseline") is not None else 0
        if noise["now"] >= -85 or rise >= DELTA["noise_rise"]["warn"]:
            out.append({"id": "noise", "level": "warn", "title": f"Noisy channel ({noise['now']:.0f} dBm)",
                        "evidence": [f"Noise floor {noise['now']:.0f} dBm"
                                     + (f", normally {noise['baseline']:.0f} dBm" if noise.get("baseline") is not None else "")],
                        "causes": ["Another network is transmitting on or near this frequency"],
                        "steps": [_step("Run Tools → airView and move to the quietest channel.", "remote")],
                        "good": "Noise floor around -90 dBm or lower."})
    return out


# ---- entry point ---------------------------------------------------------
def build(radios, now=None, hour_of=None, poll_gap_s=900):
    """radios: [{key, name, ip, model, mode, ok, error, current, stats,
                 rows: raw radio samples, links: [{peer, name, ip, model, rows}]}]
    Returns {"links": [...], "radios": [...], "fixes": [...], "summary": {...}}."""
    now = int(now or time.time())
    hour_of = hour_of or (lambda ts: time.localtime(ts).tm_hour)

    names = {}
    for r in radios:
        for ln in r.get("links") or []:
            rows = ln.get("rows") or []
            newest = rows[-1] if rows else {}
            peer = (ln.get("peer") or "").upper()
            if newest.get("name") or ln.get("name"):
                names.setdefault(peer, newest.get("name") or ln.get("name"))
            if newest.get("model") or ln.get("model"):
                names.setdefault(peer + "#model", newest.get("model") or ln.get("model"))
    for r in radios:          # a radio with no model of its own gets the one its peers see
        if not r.get("model"):
            r["model"] = names.get(r["key"].upper() + "#model", "")

    links = []
    for p in _pair_links(radios):
        ap = _end(p["ap_mac"], p["ap_radio"], p["sta_view"], names)
        sta = _end(p["sta_mac"], p["sta_radio"], p["ap_view"], names)
        ap_rows = (p["ap_view"][1].get("rows") if p["ap_view"] else None) or []
        sta_rows = (p["sta_view"][1].get("rows") if p["sta_view"] else None) or []
        main_rows = ap_rows or sta_rows            # the AP's view carries the airMAX scores both ways
        ap_r, sta_r = p["ap_radio"], p["sta_radio"]
        ap_cur = (ap_r or {}).get("current") or {}
        sta_cur = (sta_r or {}).get("current") or {}
        cur = ap_cur or sta_cur
        freq = _freq_mhz(cur.get("freq"))
        dists = [v for v in (_vals(ap_rows[-1:], "distance") + _vals(sta_rows[-1:], "distance")) if v]
        dist = round(sum(dists) / len(dists)) if dists else None

        # Signal each end hears: from its own view, else the far end's "remote" column.
        sig_ap = _stat(ap_rows, "signal", now) if ap_rows else _stat(sta_rows, "remote_signal", now)
        sig_sta = _stat(sta_rows, "signal", now) if sta_rows else _stat(ap_rows, "remote_signal", now)
        chains = {}
        if sta_r:
            srows = sta_r.get("rows") or []
            gaps = [abs(r["chain0"] - r["chain1"]) for r in srows
                    if r.get("chain0") is not None and r.get("chain1") is not None]
            if gaps:
                chains = {"radio": sta["name"], "c0": sta_cur.get("chain0"), "c1": sta_cur.get("chain1"),
                          "gap_avg": round(sum(gaps) / len(gaps), 1)}
        poor_ul = hourly(main_rows, "score_ul", hour_of, poor_below=50)
        poor_dl = hourly(main_rows, "score_dl", hour_of, poor_below=50)
        worse_poor = poor_ul if sum(v or 0 for v in poor_ul) >= sum(v or 0 for v in poor_dl) else poor_dl
        metrics = {
            "signal_ap": sig_ap, "signal_sta": sig_sta,
            "score_dl": _stat(main_rows, "score_dl", now), "score_ul": _stat(main_rows, "score_ul", now),
            "pct_below_50": {c: _pct_below(main_rows, c, 50) for c in ("score_dl", "score_ul")},
            "tx": _stat(main_rows, "tx", now), "rx": _stat(main_rows, "rx", now),
            "latency": _stat(main_rows, "latency", now),
            "noise_ap": (ap_r or {}).get("stats", {}).get("noise"),
            "noise_sta": (sta_r or {}).get("stats", {}).get("noise"),
            "airtime_sta": _stat(sta_r.get("rows") or [], "airtime", now) if sta_r else None,
            "capacity": _stat(sta_r.get("rows") or [], "cap_dl", now) if sta_r else None,
            "chains": chains,
            "distance_m": dist, "distance_range": [min(dists), max(dists)] if dists else None,
            "freq_mhz": freq, "freq_label": f"{freq:.0f} MHz" if freq else "",
            "width": cur.get("chanbw"), "ap_mode": ap_cur.get("mode"),
            "ap_mixed": "mixed" in str(ap_cur.get("mode") or ""),
            "hourly_score_ul": hourly(main_rows, "score_ul", hour_of),
            "hourly_score_dl": hourly(main_rows, "score_dl", hour_of),
            "hourly_poor_ul": poor_ul, "hourly_poor_dl": poor_dl,
            "worst_hours": worst_hours(worse_poor),
            "drops": drops((ap_r or sta_r or {}).get("rows") or [], main_rows, poll_gap_s),
            "last_ts": max([r["ts"] for r in ap_rows[-1:] + sta_rows[-1:]] or [0]),
        }
        L = {"id": p["id"], "ap": ap, "sta": sta, "metrics": metrics,
             "fresnel": fresnel(dist, freq)}
        L["findings"] = _diagnose(L, now, hour_of)
        levels = {f["level"] for f in L["findings"]}
        watch = any(f["id"] != "blind_end" for f in L["findings"])
        L["grade"] = "crit" if "crit" in levels else "warn" if "warn" in levels else "watch" if watch else "good"
        q = [v for v in ((metrics["score_dl"] or {}).get("avg"), (metrics["score_ul"] or {}).get("avg")) if v is not None]
        L["quality"] = round(min(q)) if q else None

        # One series for the charts, on the main view's timeline. Each radio stamps
        # its own polls, so the other radio's readings are matched to the nearest
        # of its polls within half a poll interval rather than by identical ts.
        def nearest(rows_other):
            ts_other = [r["ts"] for r in rows_other]

            def at(ts):
                i = bisect.bisect_left(ts_other, ts)
                best = None
                for j in (i - 1, i):
                    if 0 <= j < len(ts_other) and abs(ts_other[j] - ts) <= poll_gap_s / 2:
                        if best is None or abs(ts_other[j] - ts) < abs(ts_other[best] - ts):
                            best = j
                return rows_other[best] if best is not None else None
            return at

        sta_view_at = nearest(sta_rows) if ap_rows and sta_rows else (lambda ts: None)
        sta_radio_at = nearest((sta_r or {}).get("rows") or [])
        rows = []
        for r in _thin(main_rows):
            e = {"ts": r["ts"], "score_dl": r.get("score_dl"), "score_ul": r.get("score_ul"),
                 "tx": r.get("tx"), "rx": r.get("rx"), "latency": r.get("latency")}
            if ap_rows:
                e["sig_ap"], e["sig_sta"] = r.get("signal"), r.get("remote_signal")
                sv = sta_view_at(r["ts"])
                if sv and sv.get("signal") is not None:
                    e["sig_sta"] = sv["signal"]
            else:
                e["sig_sta"], e["sig_ap"] = r.get("signal"), r.get("remote_signal")
            sr = sta_radio_at(r["ts"])
            if sr:
                e["cap_dl"], e["airtime"] = sr.get("cap_dl"), sr.get("airtime")
                if sr.get("chain0") is not None and sr.get("chain1") is not None:
                    e["chain_gap"] = abs(sr["chain0"] - sr["chain1"])
            rows.append(e)
        cols = ("sig_ap", "sig_sta", "score_dl", "score_ul", "tx", "rx", "latency", "cap_dl", "airtime", "chain_gap")
        L["series"] = {"ts": [r["ts"] for r in rows], **{c: [r.get(c) for r in rows] for c in cols}}
        links.append(L)

    # Radio-level findings, with each AP's stations for the mixed-mode check.
    radio_out = []
    for r in radios:
        stations = [L["sta"] for L in links if L["ap"]["mac"] == r["key"].upper()]
        ends = [L["ap"] if L["ap"]["mac"] == r["key"].upper() else L["sta"]
                for L in links if r["key"].upper() in (L["ap"]["mac"], L["sta"]["mac"])]
        name = ends[0]["name"] if ends else (r.get("name") or r.get("ip") or r["key"])
        cur = r.get("current") or {}
        radio_out.append({
            "key": r["key"], "name": name, "ip": r.get("ip"), "model": r.get("model") or "",
            "ok": bool(r.get("ok")), "error": r.get("error"), "is_ap": bool(r.get("ok")) and _is_ap(r),
            "mode": r.get("mode"), "mode_raw": cur.get("mode"), "ssid": r.get("ssid"),
            "freq": cur.get("freq"), "width": cur.get("chanbw"), "last_ts": r.get("last_ts"),
            "current": cur, "stats": r.get("stats") or {},
            "series": r.get("series") or {},
            "stations": len(stations),
            "findings": _radio_findings(r, stations, now),
        })
    for L in links:
        L["name"] = f"{L['ap']['name']} → {L['sta']['name']}"

    # The fix list: every step, worst first, office work before site visits.
    order = {"crit": 0, "warn": 1, "info": 2}
    fixes = []
    for r in radio_out:
        for f in r["findings"]:
            fixes.append({"level": f["level"], "title": f["title"], "target": r["name"], "radio": r["key"],
                          "where": "remote" if all(s["where"] == "remote" for s in f["steps"]) else "site",
                          "first": f["steps"][0]["text"] if f["steps"] else "", "kind": "radio"})
    for L in links:
        found = [f for f in L["findings"] if f["id"] != "blind_end"] or L["findings"]
        if not found:
            continue
        top = found[0]
        more = len(found) - 1
        fixes.append({"level": top["level"], "title": top["title"], "target": L["name"], "link": L["id"],
                      "where": top["steps"][0]["where"] if top["steps"] else "remote",
                      "first": top["steps"][0]["text"] if top["steps"] else "",
                      "more": more, "kind": "link"})
    # Several links on one AP with the same quality problem: start at the AP.
    shared = {}
    for L in links:
        if any(f["id"] in ("poor_quality", "asymmetric_quality", "unstable") and f["level"] != "info" for f in L["findings"]):
            shared.setdefault(L["ap"]["name"], []).append(L["sta"]["name"])
    groups = []
    for ap_name, stas in shared.items():
        if len(stas) >= 2:
            groups.append({"ap": ap_name, "stations": stas})
            fixes.append({"level": "warn", "title": f"{len(stas)} links on {ap_name} share the same quality problem",
                          "target": ap_name, "where": "remote",
                          "first": f"Start at {ap_name} (channel, interference, AP mode, sector aim) before "
                                   f"visiting {', '.join(stas)} one by one.", "kind": "shared"})
    kind_rank = {"radio": 0, "shared": 1, "link": 2}
    fixes.sort(key=lambda f: (order[f["level"]], kind_rank[f["kind"]], f["where"] != "remote"))

    grades = [L["grade"] for L in links]
    rank = {"crit": 0, "warn": 1, "watch": 2, "good": 3}
    return {
        "links": sorted(links, key=lambda L: (L["ap"]["name"].lower(), rank[L["grade"]], L["sta"]["name"].lower())),
        "radios": radio_out,
        "fixes": fixes,
        "shared": groups,
        "summary": {"links": len(links), "good": grades.count("good") + grades.count("watch"),
                    "warn": grades.count("warn"), "crit": grades.count("crit"),
                    "radios": len(radios), "radios_read": sum(1 for r in radios if r.get("ok")),
                    "remote_fixes": sum(1 for f in fixes if f["where"] == "remote" and f["level"] != "info"),
                    "site_fixes": sum(1 for f in fixes if f["where"] == "site" and f["level"] != "info")},
    }
