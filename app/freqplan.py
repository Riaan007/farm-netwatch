"""Frequency plan per tower: which wireless links share a mast, where their
channels sit, which ones clash, and a conflict-free move for each clash.

Riaan's rule (2026-10-26): the radios on one high point / tower (a "tower"
group on the Network diagram) must keep their frequencies apart; towers far
from each other don't matter, towers close together can still hear each
other.

All frequencies here are CENTRE frequencies — the middle of the channel. airOS
reports the control channel as `freq` and the middle as `centerFreq`; on a
40 MHz channel they are 10 MHz apart (Riaan's Tweespruit, 2026-10-26: .201 freq
5495 / centre 5505), so the band must be built around the centre.

A LINK is an access point and the stations registered on it — they share one
channel, so the plan moves links, never single radios, and the AP is the one
that changes (the stations follow). Frequencies and widths come from the radio
monitor's last read; nothing here talks to a radio. Applying a move goes
through wififix's channel fix (preview, confirm, auto-undo), which also checks
the stations' scan lists — this plan cannot see those.

OTHER Wi-Fi (not ours) comes from site surveys a station on a tower ran
(wifiscan.py, on demand). Each network heard there costs a channel it touches,
more the louder it is, and moves and the "best channels" per link prefer quiet
ones — after keeping clear of our own links, which stays the hard rule.
"""
import math

# 5 GHz ranges an outdoor link may use in South Africa (ICASA, matching the
# airOS ZA country table); 5470-5725 is DFS (radar check before transmitting).
RANGES = ((5150, 5350), (5470, 5725), (5725, 5850))
DFS = ((5250, 5350), (5470, 5725))
GUARD_MHZ = 10        # edge-to-edge gap wanted between two links (a move keeps it to EVERY tower)
NEAR_KM = 5.0         # towers this close, joined by a radio link, or with no GPS are "near":
                      # their overlaps and close calls are reported as clashes
STEP = 5
BUSY_COST = 2.0       # a link this "busy" with other networks gets a quieter channel suggested


def _loud(signal):
    """How much one other network on a channel costs, by how loud it is heard. Channels are
    compared in whole steps, so one faint network (under about -80 dBm) never forces a move."""
    if signal is None:
        return 1.0
    return 4.0 if signal >= -60 else 2.0 if signal >= -70 else 1.0 if signal >= -80 else 0.3


def _num(v):
    try:
        return float(str(v).split()[0])
    except (TypeError, ValueError, IndexError):
        return None


def is_dfs(lo, hi):
    return any(lo < b and hi > a for a, b in DFS)


def _band(freq, width):
    return freq - width / 2, freq + width / 2


def _gap(a, b):
    """Edge gap in MHz between two (lo, hi) bands; negative = overlap."""
    return max(a[0], b[0]) - min(a[1], b[1])


def _km(a, b):
    if None in (a.get("lat"), a.get("lon"), b.get("lat"), b.get("lon")):
        return None
    la1, lo1, la2, lo2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def candidates(width):
    out = []
    for lo, hi in RANGES:
        c = lo + width / 2
        while c + width / 2 <= hi:
            out.append(int(c))
            c += STEP
    return sorted(set(out))


def _links(radios, by_mac, by_ip):
    """Group the radios into links: {ap key: {"ap", "members", "freq", "width"}}."""
    ap_of = {}
    for k, r in radios.items():
        if not r.get("ok"):
            continue
        mode = str((r.get("sample") or {}).get("mode") or r.get("mode") or "").lower()
        is_ap = mode.startswith("ap") or "access point" in mode
        for ln in r.get("links") or []:
            peer = by_mac.get((ln.get("peer") or "").lower())
            if not peer and ln.get("ip") and len(by_ip.get(ln["ip"], [])) == 1:
                peer = by_ip[ln["ip"]][0]
            if not peer:
                continue
            ap, sta = (k, peer) if is_ap else (peer, k)
            ap_of[sta] = ap
            ap_of.setdefault(ap, ap)
        if is_ap:
            ap_of.setdefault(k, k)
    links = {}
    for member, ap in ap_of.items():
        L = links.setdefault(ap, {"ap": ap, "members": set(), "freq": None, "width": None,
                                  "control": None, "centre_known": False})
        L["members"].update({member, ap})
    for L in links.values():
        for k in [L["ap"]] + sorted(L["members"] - {L["ap"]}):
            s = (radios.get(k) or {}).get("sample") or {}
            f, c, w = _num(s.get("freq")), _num(s.get("center")), _num(s.get("chanbw"))
            if f and w and L["freq"] is None:
                # No centre reported (older firmware / not polled since the update):
                # on 20 MHz the control channel IS the centre; wider is a guess.
                L["freq"], L["width"], L["control"] = int(c or f), int(w), int(f)
                L["centre_known"] = bool(c) or int(w) <= 20
    return links


def analyse(groups, group_of, radios, devices, names=None, scans=None):
    """groups {gid: {name, kind, lat, lon}}; group_of {device key: gid};
    radios = radiomon snapshot {key: …}; devices {key: device};
    scans = wifiscan.load() {radio key: {ts, name, networks: [...]}}.
    Returns {"towers": [...], "moves": [...], "summary": {...}}."""
    names, scans = names or {}, scans or {}
    by_mac, by_ip = {}, {}
    for k, d in devices.items():
        m = (d.get("mac") or (k if ":" in k else "")).lower()
        if m:
            by_mac[m] = k
        if d.get("ip"):
            by_ip.setdefault(d["ip"], []).append(k)
    for k in radios:
        by_mac.setdefault(k.lower(), k)
    links = _links(radios, by_mac, by_ip)
    link_of = {m: ap for ap, L in links.items() for m in L["members"]}
    # A radio the scanner only knows by IP is still named by its peers (wstalist hostname).
    heard = {}
    for r in radios.values():
        for ln in r.get("links") or []:
            peer = by_mac.get((ln.get("peer") or "").lower())
            if peer and ln.get("name"):
                heard.setdefault(peer, ln["name"])
    is_ip = lambda v: bool(v) and all(p.isdigit() for p in str(v).split(".")) and str(v).count(".") == 3  # noqa: E731

    def name(k):
        for v in (names.get(k), (devices.get(k) or {}).get("name"), (radios.get(k) or {}).get("name")):
            if v and not is_ip(v) and v.lower() != k.lower():
                return v
        return heard.get(k) or (devices.get(k) or {}).get("ip") or k

    towers = {gid: g for gid, g in groups.items() if g.get("kind") == "tower"}
    on = {gid: sorted({link_of[k] for k, g in group_of.items() if g == gid and k in link_of}) for gid in towers}
    unknown_here = {gid: [k for k, g in group_of.items() if g == gid and k in radios and k not in link_of]
                    for gid in towers}
    # A radio link between two towers points their dishes at each other — near at any distance.
    tower_of = {}
    for k, g in group_of.items():
        if g in towers:
            tower_of.setdefault(k, set()).add(g)
    joined = set()
    for ap, L in links.items():
        ts = set().union(*(tower_of.get(m, set()) for m in L["members"]))
        joined |= {(a, b) for a in ts for b in ts if a != b}

    def is_near(a, b):
        km = _km(towers[a], towers[b])
        return (a, b) in joined or km is None or km <= NEAR_KM
    near = {gid: [o for o in towers if o != gid and is_near(gid, o)] for gid in towers}

    # Other networks heard from each tower (site surveys), never our own radios.
    own_macs = {m.upper() for m in by_mac} | {str(k).upper() for k in radios}
    for r in radios.values():
        own_macs |= {str(ln.get("peer") or "").upper() for ln in r.get("links") or []}
    own_ssids = {str((r.get("sample") or {}).get("ssid") or "") for r in radios.values()} - {""}
    gid_of = {k.lower(): g for k, g in group_of.items()}
    others = {gid: {} for gid in towers}
    scanned = {gid: [] for gid in towers}
    for k, rec in scans.items():
        gid = gid_of.get(str(k).lower())
        if gid not in towers:
            continue
        scanned[gid].append({"ts": rec.get("ts"), "by": name(k) if k in devices or k in radios else rec.get("name") or k})
        for n in rec.get("networks") or []:
            if n.get("bssid", "").upper() in own_macs or (n.get("ssid") and n["ssid"] in own_ssids):
                continue
            cur = others[gid].get(n["bssid"])
            if not cur or (n.get("signal") or -200) > (cur.get("signal") or -200):
                others[gid][n["bssid"]] = {**n, "seen": rec.get("ts")}
    others = {gid: sorted(v.values(), key=lambda n: n["lo"]) for gid, v in others.items()}

    def heard_on(gids, band):
        """(cost, count) of other networks on these towers touching this band (or within 5 MHz)."""
        hit = [n for g in gids for n in others.get(g, []) if _gap(band, (n["lo"], n["hi"])) < 5]
        return round(sum(_loud(n.get("signal")) for n in hit), 1), len(hit)

    def clashes(gid, band_of):
        out = []
        here = [ap for ap in on[gid] if band_of.get(ap)]
        for i, a in enumerate(here):
            for b in here[i + 1:]:
                g = _gap(band_of[a], band_of[b])
                if g < GUARD_MHZ:
                    out.append({"a": a, "b": b, "gap": g, "level": "crit" if g < 0 else "warn", "where": "same"})
        for o in near[gid]:
            for a in here:
                for b in on[o]:
                    if not band_of.get(b) or a == b or b in here:
                        continue
                    if any(a in on[x] and b in on[x] for x in towers):
                        continue                    # already a same-tower clash where both are
                    g = _gap(band_of[a], band_of[b])
                    if g < GUARD_MHZ:
                        out.append({"a": a, "b": b, "gap": g, "level": "warn" if g < 0 else "info",
                                    "where": "near", "tower": towers[o].get("name") or o})
        return out

    band_now = {ap: _band(L["freq"], L["width"]) for ap, L in links.items() if L["freq"]}
    # Moves: greedy, worst-clashing first; only links whose AP Netwatch can read.
    band_plan = dict(band_now)
    moves = []
    # Worst-clashing first; on a tie move the link that disturbs less: fewer radios on it, then narrower.
    order = sorted(links, key=lambda ap: (-sum(1 for gid in towers for c in clashes(gid, band_plan) if ap in (c["a"], c["b"])),
                                          len(links[ap]["members"]), links[ap]["width"] or 0))
    for ap in order:
        L = links[ap]
        if not L["freq"] or not (radios.get(ap) or {}).get("ok"):
            continue
        mine = [gid for gid in towers if ap in on[gid]]
        if not any(ap in (c["a"], c["b"]) and c["level"] != "info" for gid in mine for c in clashes(gid, band_plan)):
            continue
        everywhere = {x for gid in towers for x in on[gid]} - {ap}
        close_by = {x for gid in mine for x in on[gid] + [y for o in near[gid] for y in on[o]]} - {ap}

        def pick(strict):
            """strict: GUARD_MHZ clear of every link on every tower of the site.
            Fallback: GUARD_MHZ to this tower and its near towers, no overlap elsewhere."""
            best = None
            for c in candidates(L["width"]):
                b = _band(c, L["width"])
                if any(band_plan.get(x) and _gap(b, band_plan[x]) < (GUARD_MHZ if strict or x in close_by else 0)
                       for x in everywhere):
                    continue
                score = (math.floor(heard_on(mine, b)[0]), is_dfs(*b), abs(c - L["freq"]))   # quiet, then non-DFS, then the smallest move
                if best is None or score < best[0]:
                    best = (score, c)
            return best
        best = pick(True)
        relaxed = False
        if not best:
            best, relaxed = pick(False), True
        if best:
            c = best[1]
            band_plan[ap] = _band(c, L["width"])
            moves.append({"ap": ap, "name": name(ap), "from": L["freq"], "to": c, "width": L["width"],
                          "dfs": is_dfs(*band_plan[ap]), "why": "clash",
                          "heard_from": heard_on(mine, band_now[ap])[1], "heard_to": heard_on(mine, band_plan[ap])[1],
                          "towers": [towers[g].get("name") or g for g in mine],
                          "checked": ("clear of every link on every tower" if not relaxed else
                                      "clear of this tower and the towers near it; it overlaps no other tower, "
                                      "but is closer than " + str(GUARD_MHZ) + " MHz to one far away")})
        else:
            moves.append({"ap": ap, "name": name(ap), "from": L["freq"], "to": None, "width": L["width"],
                          "dfs": False, "why": "clash", "towers": [towers[g].get("name") or g for g in mine],
                          "note": "no free channel at this width — try a narrower channel"})

    def ranked(ap):
        """Channels for this link, best first: clear of every other link (as planned), then quiet."""
        L, mine = links[ap], [gid for gid in towers if ap in on[gid]]
        rest = {x for gid in towers for x in on[gid]} - {ap}
        out = []
        for c in candidates(L["width"]):
            b = _band(c, L["width"])
            if any(band_plan.get(x) and _gap(b, band_plan[x]) < GUARD_MHZ for x in rest):
                continue
            cost, n = heard_on(mine, b)
            out.append(((math.floor(cost), is_dfs(*b), abs(c - L["freq"])), {"freq": c, "dfs": is_dfs(*b), "heard": n, "cost": cost}))
        out.sort(key=lambda x: x[0])
        return [x[1] for x in out]

    # Links that clash with nothing of ours but share their channel with loud neighbours.
    moved_aps = {m["ap"] for m in moves}
    for ap, L in links.items():
        if ap in moved_aps or not L["freq"] or not (radios.get(ap) or {}).get("ok"):
            continue
        mine = [gid for gid in towers if ap in on[gid]]
        cost_now, n_now = heard_on(mine, band_now[ap])
        if cost_now < BUSY_COST:
            continue
        best = ranked(ap)[:1]
        if best and best[0]["cost"] <= cost_now - BUSY_COST:
            c = best[0]["freq"]
            band_plan[ap] = _band(c, L["width"])
            moves.append({"ap": ap, "name": name(ap), "from": L["freq"], "to": c, "width": L["width"],
                          "dfs": best[0]["dfs"], "why": "busy", "heard_from": n_now, "heard_to": best[0]["heard"],
                          "towers": [towers[g].get("name") or g for g in mine],
                          "checked": f"quieter: {n_now} other network{'s' if n_now != 1 else ''} heard on the "
                                     f"current channel, {best[0]['heard']} on this one · clear of every link on every tower"})

    out_towers = []
    for gid, g in towers.items():
        cl = clashes(gid, band_now)
        bad = {x for c in cl for x in (c["a"], c["b"])}
        out_towers.append({
            "id": gid, "name": g.get("name") or gid,
            "near": [towers[o].get("name") or o for o in near[gid]],
            "links": [{"ap": ap, "name": name(ap), "freq": links[ap]["freq"], "width": links[ap]["width"],
                       "control": links[ap]["control"], "centre_known": links[ap]["centre_known"],
                       "lo": band_now[ap][0] if ap in band_now else None, "hi": band_now[ap][1] if ap in band_now else None,
                       "dfs": is_dfs(*band_now[ap]) if ap in band_now else False,
                       "here": sorted(name(k) for k in links[ap]["members"] if group_of.get(k) == gid),
                       "clash": ap in bad, "readable": bool((radios.get(ap) or {}).get("ok")),
                       "heard": heard_on([gid], band_now[ap])[1] if ap in band_now else 0,
                       "best": ranked(ap)[:3] if ap in band_now and (radios.get(ap) or {}).get("ok") else []}
                      for ap in on[gid]],
            "others": others[gid],
            "scanned": sorted(scanned[gid], key=lambda x: -(x["ts"] or 0))[:1],
            "scan_from": [{"key": k, "name": name(k)} for k, gg in group_of.items()
                          if gg == gid and (radios.get(k) or {}).get("ok")
                          and str((radios[k].get("sample") or {}).get("mode") or radios[k].get("mode") or "").lower().startswith("sta")],
            "unknown": [name(k) for k in unknown_here[gid]],
            "near_ids": near[gid],
            "clashes": [{**c, "a_name": name(c["a"]), "b_name": name(c["b"])} for c in cl],
        })
    out_towers.sort(key=lambda t: (-len(t["clashes"]), t["name"]))
    return {"towers": out_towers, "moves": moves, "guard_mhz": GUARD_MHZ, "near_km": NEAR_KM,
            "ranges": [list(r) for r in RANGES], "dfs": [list(r) for r in DFS],
            "summary": {"towers": len(out_towers), "clashes": sum(len(t["clashes"]) for t in out_towers),
                        "moves": sum(1 for m in moves if m["to"]),
                        "others": len({n["bssid"] for v in others.values() for n in v}),
                        # wide links whose centre the monitor has not read yet (until the next poll)
                        "centre_unknown": sum(1 for L in links.values() if L["freq"] and not L["centre_known"])}}
