"""AI analysis of a site's wireless links.

The site already decides WHAT is wrong (radiomon.py: signal below its own
baseline, chains out of balance, air time saturated) and carries a stock hint
for each rule. This adds the part rules can't do: reading the whole picture at
once — which links share a radio, which degraded together, what the trend looks
like — and turning it into an ordered list of things to actually go and do.

Facts in, strict JSON out. If Gemini is unreachable or unconfigured the caller
still has the site's own findings, so this is an enhancement and never a
dependency.
"""
import json

import requests

MODEL_DEFAULT = "gemini-2.5-flash"


class DoctorError(Exception):
    def __init__(self, msg, status=502):
        super().__init__(msg)
        self.status = status


def _trend(points, field):
    """First/last/min/max of a numeric series — enough for the model to see a
    direction without shipping hundreds of raw samples into the prompt."""
    vals = [p[field] for p in points if p.get(field) is not None]
    if not vals:
        return None
    return {"first": round(vals[0], 1), "last": round(vals[-1], 1),
            "min": round(min(vals), 1), "max": round(max(vals), 1), "n": len(vals)}


def gather_facts(site_label, overview, histories, devices):
    """Shape the site's radio state into the payload the model reasons over.

    `histories` is {device_key: /api/devices/<key>/radio-history payload}.
    """
    dev_by_key = {d.get("key"): d for d in devices or []}
    radios = []
    for key, last in (overview.get("radios") or {}).items():
        if not last.get("ok"):
            radios.append({"name": last.get("name") or key, "ip": last.get("ip"),
                           "reachable": False, "error": last.get("error")})
            continue
        s = last.get("sample") or {}
        hist = histories.get(key) or {}
        links = []
        for ln in hist.get("links") or []:
            pts = ln.get("points") or []
            cur = pts[-1] if pts else {}
            links.append({
                "far_end": ln.get("name") or ln.get("peer"),
                "model": ln.get("model"), "ip": cur.get("ip"),
                "distance_m": cur.get("distance"),
                "signal_now": cur.get("signal"), "signal_trend": _trend(pts, "signal"),
                "far_end_signal": cur.get("remote_signal"),
                "score_dl": cur.get("score_dl"), "score_ul": cur.get("score_ul"),
                "score_dl_trend": _trend(pts, "score_dl"),
                "tx_mbps": cur.get("tx"), "rx_mbps": cur.get("rx"),
                "latency_ms": cur.get("latency"),
            })
        samples = hist.get("samples") or []
        radios.append({
            "name": last.get("name") or key, "ip": last.get("ip"),
            "reachable": True, "mode": last.get("mode"), "ssid": last.get("ssid"),
            "frequency": s.get("freq"), "channel_width": s.get("chanbw"),
            "noise_dbm": s.get("noise"), "noise_trend": _trend(samples, "noise"),
            "airtime_pct": s.get("airtime"), "airtime_trend": _trend(samples, "airtime"),
            "chain0_dbm": s.get("chain0"), "chain1_dbm": s.get("chain1"),
            "capacity_down_kbps": s.get("cap_dl"), "capacity_up_kbps": s.get("cap_ul"),
            "links": links,
            "asset": (dev_by_key.get(key) or {}).get("asset") or {},
        })
    return {
        "site": site_label,
        "findings": [{"device": p.get("device"), "level": p.get("level"),
                      "metric": p.get("metric"), "detail": p.get("what"),
                      "far_end": p.get("peer_name")}
                     for p in overview.get("problems") or []],
        "radios": radios,
    }


PROMPT = """You are a wireless network engineer reviewing a farm's point-to-multipoint
Ubiquiti airMAX network. Long outdoor links carry cameras and site connectivity;
a site visit costs half a day, so advice must be specific enough to act on in one trip.

Interpretation guide:
- Signal is dBm (closer to 0 is better). -50 to -65 is healthy for these links;
  below -75 is poor. A DROP from the link's own normal matters more than the absolute.
- The two antenna chains should be within a few dB. A large gap means a mis-aimed
  dish, a damaged/wet pigtail, or water in a connector.
- airMAX link score is 0-100; below 50 is poor. Air time above 80% is congestion.
- Noise floor rising means new interference nearby.
- Asset details (bearing, mast height, dish model) are the installer's notes — use
  them when suggesting a re-aim, and say when they are missing and would help.

Return ONLY JSON:
{"summary": "2-3 sentences on the state of the wireless network",
 "overall": "healthy" | "watch" | "degraded" | "critical",
 "links": [{"name": "...", "verdict": "one line", "severity": "high|medium|low",
            "likely_causes": ["most likely first"],
            "actions": ["concrete, ordered steps for the technician"]}],
 "site_actions": ["work that fixes more than one link, most valuable first"],
 "watch": ["things that are not yet a problem but are trending the wrong way"]}
Only mention links that need attention. Be concrete: name the radio, say what to
check and what a good result looks like. If the data is too thin to judge, say so
in the summary rather than guessing.

DATA:
"""


def diagnose(facts, ai_cfg):
    key = (ai_cfg.get("gemini_api_key") or "").strip()
    if not key:
        raise DoctorError("No Gemini API key configured — set it on the hub under "
                          "Alerts & AI.", 400)
    model = (ai_cfg.get("gemini_model") or MODEL_DEFAULT).strip()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "contents": [{"role": "user",
                      "parts": [{"text": PROMPT + json.dumps(facts, ensure_ascii=False)}]}],
        "generationConfig": {"response_mime_type": "application/json",
                             "temperature": 0.3, "maxOutputTokens": 3072},
    }
    try:
        r = requests.post(url, params={"key": key}, json=body, timeout=(5, 75))
    except requests.RequestException as e:
        raise DoctorError(f"Gemini unreachable: {e}")
    if r.status_code != 200:
        try:
            msg = r.json().get("error", {}).get("message", r.text[:200])
        except ValueError:
            msg = r.text[:200]
        raise DoctorError(f"Gemini API error ({r.status_code}): {msg}")
    try:
        parts = r.json()["candidates"][0]["content"]["parts"]
        out = json.loads("".join(p.get("text", "") for p in parts))
    except (KeyError, IndexError, ValueError) as e:
        raise DoctorError(f"Gemini returned an unparseable answer: {e}")
    out.setdefault("summary", "")
    out.setdefault("overall", "unknown")
    out.setdefault("links", [])
    out.setdefault("site_actions", [])
    out.setdefault("watch", [])
    out["model"] = model
    return out
