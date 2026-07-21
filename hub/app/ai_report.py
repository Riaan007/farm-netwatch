"""AI-driven site report: Gemini analysis + PDF rendering (fpdf2).

The hub gathers one site's snapshot (stats, devices, conflicts, events,
reachability, internet state), asks Gemini for an operations analysis
(strict-JSON answer), and renders a print-grade A4 PDF. The Gemini key/model
live in hub.json under "ai" (Settings -> Alerts & AI on the hub).
"""
import json
import time

import requests
from fpdf import FPDF
from fpdf.enums import XPos, YPos

FONT_DIR = "/usr/share/fonts/truetype/dejavu"

# palette (print on white)
NAVY = (10, 16, 30)
CYAN = (8, 145, 178)
EMERALD = (5, 150, 105)
AMBER = (180, 83, 9)
RED = (190, 18, 60)
TEXT = (30, 41, 59)
MUTED = (100, 116, 139)
PANEL = (243, 246, 250)
BORDER = (226, 232, 240)

RISKY_PORTS = {21: "FTP", 23: "Telnet", 2323: "Telnet", 512: "rexec",
               513: "rlogin", 514: "rsh"}


class ReportError(Exception):
    def __init__(self, message, status=500):
        super().__init__(message)
        self.status = status


def _ago(ts):
    if not ts:
        return "-"
    s = max(0, int(time.time() - ts))
    if s < 90:
        return f"{s}s ago"
    if s < 5400:
        return f"{s // 60}m ago"
    if s < 129600:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _dev_name(d):
    return (d.get("name") or
            (d.get("type") if d.get("type") not in (None, "", "unknown") else "") or
            d.get("vendor") or d.get("hostname") or "Unknown device")


# ---- facts: the compact JSON snapshot both Gemini and the PDF work from ------
def gather_facts(card, devices, conflicts, events, internet, reach, sysinfo=None):
    cats = {}
    for d in devices:
        c = d.get("category") or "unknown"
        cats.setdefault(c, {"total": 0, "online": 0})
        cats[c]["total"] += 1
        if d.get("online"):
            cats[c]["online"] += 1
    offline = [{"name": _dev_name(d), "ip": d.get("ip"),
                "category": d.get("category"), "last_seen": _ago(d.get("last_seen")),
                "watched": bool(d.get("watch"))}
               for d in devices if not d.get("online")]
    offline.sort(key=lambda x: not x["watched"])
    risky = [{"name": _dev_name(d), "ip": d.get("ip"),
              "ports": sorted(p for p in (d.get("ports") or []) if p in RISKY_PORTS)}
             for d in devices if any(p in RISKY_PORTS for p in (d.get("ports") or []))]
    mystery = sum(1 for d in devices
                  if not d.get("name") and (d.get("category") in (None, "", "unknown")))
    ev_counts = {}
    for e in events:
        ev_counts[e.get("type") or "?"] = ev_counts.get(e.get("type") or "?", 0) + 1
    new_devices = [{"who": e.get("name") or e.get("vendor") or e.get("mac") or "?",
                    "ip": e.get("ip"), "when": _ago(e.get("ts"))}
                   for e in events if e.get("type") == "new"][:10]
    return {
        "site": {"name": card.get("name"), "location": card.get("location"),
                 "vpn_latency_ms": card.get("latency_ms"),
                 "data_stale": card.get("stale", False)},
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "totals": {"devices": len(devices),
                   "online": sum(1 for d in devices if d.get("online")),
                   "offline": len(offline), "mystery_unidentified": mystery,
                   "watched_down": card.get("watched_down") or 0},
        "vpn_reachability_pct": reach,
        "internet": internet,
        "last_scan": _ago(card.get("last_scan_ts")),
        "categories": cats,
        "offline_devices": offline[:25],
        "ip_conflicts": [{"ip": c["ip"],
                          "devices": [{"name": d.get("name") or d.get("vendor") or "?",
                                       "mac": d.get("mac"),
                                       "online": d.get("online")} for d in c["devices"]]}
                         for c in conflicts],
        "risky_open_ports": risky[:10],
        "kuma": card.get("kuma"),
        "events_last_7d_counts": ev_counts,
        "recent_new_devices": new_devices,
        "pi_health": ({
            "model": sysinfo.get("model"),
            "temp_c": sysinfo.get("temp_c"),
            "cpu_pct": sysinfo.get("cpu_pct"),
            "mem_used_pct": (sysinfo.get("mem") or {}).get("used_pct"),
            "disks": sysinfo.get("disks"),
            "uptime_days": round((sysinfo.get("uptime_s") or 0) / 86400, 1),
            "throttled": sysinfo.get("throttled"),
        } if sysinfo else None),
    }


# ---- Gemini ------------------------------------------------------------------
PROMPT = """You are the network operations analyst for a farm-security company.
Below is a JSON snapshot of one farm site's network, collected by Farm Netwatch
(a LAN scanner on the site's Pi) via the central hub.

Write an operations analysis. Be concrete and specific: name devices, IPs and
counts from the data. No generic filler and no repetition of raw JSON. The
audience is the technical owner-operator.

Glossary: watched = user-flagged critical device, alerts on state change;
IP conflict = two devices answering one IP (causes false offline readings);
risky ports = Telnet/FTP/r-services exposed on the LAN; mystery = device the
scanner could not identify; vpn_reachability = hub-to-site VPN uptime;
kuma = Uptime Kuma monitor counts (up/down).

Return STRICT JSON only, exactly this schema:
{"health_grade":"A|B|C|D|F",
 "health_summary":"<= 12 words",
 "executive_summary":"3-5 sentences",
 "key_risks":[{"title":"...","detail":"1-2 sentences","severity":"high|medium|low"}],
 "recommendations":[{"title":"...","detail":"1-2 sentences","priority":"now|soon|later"}],
 "notable_observations":["..."]}
Max 5 key_risks, 5 recommendations, 4 notable_observations. If the site is
healthy, say so plainly instead of inventing problems.

DATA:
"""


def analyze(facts, ai_cfg):
    key = (ai_cfg.get("gemini_api_key") or "").strip()
    if not key:
        raise ReportError("No Gemini API key configured — set it on the hub under "
                          "Alerts & AI.", 400)
    model = (ai_cfg.get("gemini_model") or "gemini-2.5-flash").strip()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "contents": [{"role": "user",
                      "parts": [{"text": PROMPT + json.dumps(facts, ensure_ascii=False)}]}],
        "generationConfig": {"response_mime_type": "application/json",
                             "temperature": 0.3, "maxOutputTokens": 4096},
    }
    try:
        r = requests.post(url, params={"key": key}, json=body, timeout=(5, 75))
    except requests.RequestException as e:
        raise ReportError(f"Gemini unreachable: {e}", 502)
    if r.status_code != 200:
        try:
            msg = r.json().get("error", {}).get("message", r.text[:200])
        except ValueError:
            msg = r.text[:200]
        raise ReportError(f"Gemini API error ({r.status_code}): {msg}", 502)
    try:
        parts = r.json()["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts)
        ai = json.loads(text)
    except (KeyError, IndexError, ValueError) as e:
        raise ReportError(f"Gemini returned an unparseable answer: {e}", 502)
    ai.setdefault("health_grade", "?")
    ai.setdefault("health_summary", "")
    ai.setdefault("executive_summary", "")
    ai.setdefault("key_risks", [])
    ai.setdefault("recommendations", [])
    ai.setdefault("notable_observations", [])
    ai["model"] = model
    return ai


# ---- PDF ---------------------------------------------------------------------
class _PDF(FPDF):
    site_label = ""

    def footer(self):
        self.set_y(-12)
        self.set_font("DejaVu", "", 7)
        self.set_text_color(*MUTED)
        self.cell(0, 5, f"Farm Netwatch Hub - {self.site_label}  |  AI analysis by Gemini  |  "
                        f"page {self.page_no()}/{{nb}}", align="C")


def _grade_color(g):
    return {"A": EMERALD, "B": EMERALD, "C": AMBER}.get(str(g)[:1].upper(), RED)


def _sev_color(s):
    return {"high": RED, "medium": AMBER}.get(str(s).lower(), MUTED)


def build_pdf(facts, ai, devices, events):
    pdf = _PDF(format="A4")
    pdf.site_label = facts["site"]["name"] or "site"
    pdf.add_font("DejaVu", "", f"{FONT_DIR}/DejaVuSans.ttf")
    pdf.add_font("DejaVu", "B", f"{FONT_DIR}/DejaVuSans-Bold.ttf")
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(True, margin=16)
    pdf.set_margins(12, 12, 12)
    pdf.add_page()
    W = pdf.w - 24

    # header band
    pdf.set_fill_color(*NAVY)
    pdf.rect(0, 0, pdf.w, 34, style="F")
    pdf.set_fill_color(34, 211, 238)
    pdf.rect(0, 34, pdf.w, 0.8, style="F")
    pdf.set_xy(12, 7)
    pdf.set_font("DejaVu", "B", 8)
    pdf.set_text_color(103, 232, 249)
    pdf.cell(0, 4, "FARM NETWATCH  -  AI SITE REPORT", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("DejaVu", "B", 17)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(0, 9, str(facts["site"]["name"]), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("DejaVu", "", 8)
    pdf.set_text_color(148, 163, 184)
    loc = facts["site"].get("location") or ""
    pdf.cell(0, 4, f"{loc + '  |  ' if loc else ''}generated {facts['generated']}"
                   f"  |  last scan {facts['last_scan']}",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_y(40)

    # stat boxes
    t = facts["totals"]
    reach = facts.get("vpn_reachability_pct") or {}
    inet = facts.get("internet") or {}
    inet_txt = ("OK" if inet.get("ok") else
                ("gateway down" if inet.get("has_gateway") and not inet.get("gateway")
                 else "degraded")) if inet.get("checked_ts") is not None else "n/a"
    stats = [
        ("DEVICES ONLINE", f"{t['online']}/{t['devices']}",
         EMERALD if t["online"] == t["devices"] else TEXT),
        ("WATCHED DOWN", str(t["watched_down"]), AMBER if t["watched_down"] else EMERALD),
        ("IP CONFLICTS", str(len(facts["ip_conflicts"])),
         AMBER if facts["ip_conflicts"] else EMERALD),
        ("VPN 24H", f"{reach.get('24h', '-')}%", TEXT),
        ("INTERNET", inet_txt, EMERALD if inet_txt == "OK" else AMBER),
    ]
    bw = (W - 4 * 3) / 5
    x = 12
    for label, val, color in stats:
        pdf.set_fill_color(*PANEL)
        pdf.set_draw_color(*BORDER)
        pdf.rect(x, pdf.get_y(), bw, 15, style="FD")
        pdf.set_xy(x + 2.5, pdf.get_y() + 2.5)
        pdf.set_font("DejaVu", "B", 5.6)
        pdf.set_text_color(*MUTED)
        pdf.cell(bw - 5, 3, label)
        pdf.set_xy(x + 2.5, pdf.get_y() + 4)
        pdf.set_font("DejaVu", "B", 10.5)
        pdf.set_text_color(*color)
        pdf.cell(bw - 5, 6, str(val))
        pdf.set_y(pdf.get_y() - 6.5)
        x += bw + 3
    pdf.set_y(pdf.get_y() + 21)

    def heading(txt, color=CYAN):
        pdf.set_font("DejaVu", "B", 9.5)
        pdf.set_text_color(*color)
        pdf.cell(0, 6, txt, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_draw_color(*BORDER)
        pdf.line(12, pdf.get_y(), 12 + W, pdf.get_y())
        pdf.set_y(pdf.get_y() + 2)

    # AI analysis
    grade = str(ai.get("health_grade", "?"))
    heading(f"AI ANALYSIS  (Gemini {ai.get('model', '')})")
    gc = _grade_color(grade)
    pdf.set_fill_color(*gc)
    pdf.rect(12, pdf.get_y(), 11, 11, style="F")
    pdf.set_xy(12, pdf.get_y() + 1.5)
    pdf.set_font("DejaVu", "B", 13)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(11, 8, grade, align="C")
    pdf.set_xy(26, pdf.get_y() - 1)
    pdf.set_font("DejaVu", "B", 9)
    pdf.set_text_color(*TEXT)
    pdf.cell(0, 5, f"Health grade: {grade}  -  {ai.get('health_summary', '')}",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_x(26)
    pdf.set_font("DejaVu", "", 8.5)
    pdf.set_text_color(*TEXT)
    pdf.multi_cell(W - 14, 4.4, ai.get("executive_summary", ""))
    pdf.set_y(pdf.get_y() + 3)

    def bullets(title, items, render):
        if not items:
            return
        pdf.set_font("DejaVu", "B", 8.5)
        pdf.set_text_color(*TEXT)
        pdf.cell(0, 5, title, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        for it in items:
            render(it)
        pdf.set_y(pdf.get_y() + 2)

    def risk_row(r_):
        c = _sev_color(r_.get("severity"))
        pdf.set_fill_color(*c)
        pdf.rect(13, pdf.get_y() + 1.4, 2, 2, style="F")
        pdf.set_x(17)
        pdf.set_font("DejaVu", "B", 8)
        pdf.set_text_color(*TEXT)
        sev = str(r_.get("severity", "")).upper()
        pdf.multi_cell(W - 5, 4.2, f"{r_.get('title', '')}  [{sev}]")
        pdf.set_x(17)
        pdf.set_font("DejaVu", "", 8)
        pdf.set_text_color(*MUTED)
        pdf.multi_cell(W - 5, 4.0, str(r_.get("detail", "")))
        pdf.set_y(pdf.get_y() + 1)

    def rec_row(r_):
        pdf.set_fill_color(*CYAN)
        pdf.rect(13, pdf.get_y() + 1.4, 2, 2, style="F")
        pdf.set_x(17)
        pdf.set_font("DejaVu", "B", 8)
        pdf.set_text_color(*TEXT)
        pri = str(r_.get("priority", "")).upper()
        pdf.multi_cell(W - 5, 4.2, f"{r_.get('title', '')}  [{pri}]")
        pdf.set_x(17)
        pdf.set_font("DejaVu", "", 8)
        pdf.set_text_color(*MUTED)
        pdf.multi_cell(W - 5, 4.0, str(r_.get("detail", "")))
        pdf.set_y(pdf.get_y() + 1)

    def obs_row(o):
        pdf.set_fill_color(*MUTED)
        pdf.rect(13, pdf.get_y() + 1.4, 2, 2, style="F")
        pdf.set_x(17)
        pdf.set_font("DejaVu", "", 8)
        pdf.set_text_color(*TEXT)
        pdf.multi_cell(W - 5, 4.2, str(o))

    bullets("Key risks", ai.get("key_risks") or [], risk_row)
    bullets("Recommendations", ai.get("recommendations") or [], rec_row)
    bullets("Notable observations", ai.get("notable_observations") or [], obs_row)

    # IP conflicts
    if facts["ip_conflicts"]:
        heading("IP CONFLICTS", AMBER)
        pdf.set_font("DejaVu", "", 8)
        for c in facts["ip_conflicts"]:
            pdf.set_text_color(*TEXT)
            who = "  vs  ".join(
                f"{d.get('name') or '?'} ({d.get('mac') or 'no-mac'}, "
                f"{'online' if d.get('online') else 'offline'})" for d in c["devices"])
            pdf.multi_cell(W, 4.4, f"{c['ip']}:  {who}")
        pdf.set_y(pdf.get_y() + 3)

    # device inventory table
    heading("DEVICE INVENTORY")
    cols = [("Device", 42), ("Category", 18), ("IP", 25), ("MAC", 29),
            ("Vendor", 36), ("State", 11), ("Last seen", 15), ("RTT", 10)]

    def table_header():
        pdf.set_font("DejaVu", "B", 6.3)
        pdf.set_text_color(*MUTED)
        pdf.set_fill_color(*PANEL)
        for name, cw in cols:
            pdf.cell(cw, 4.6, name, fill=True, border="B")
        pdf.ln()

    def fit(txt, cw):
        txt = str(txt or "")
        while pdf.get_string_width(txt) > cw - 1.6 and len(txt) > 1:
            txt = txt[:-1]
        return txt

    table_header()
    devs = sorted(devices, key=lambda d: (d.get("online", False),
                                          _ip_num(d.get("ip"))))
    pdf.set_font("DejaVu", "", 6.3)
    for d in devs:
        if pdf.get_y() > pdf.h - 22:
            pdf.add_page()
            table_header()
            pdf.set_font("DejaVu", "", 6.3)
        online = d.get("online")
        vals = [_dev_name(d), d.get("category") or "-", d.get("ip") or "-",
                d.get("mac") or "-", d.get("vendor") or "-",
                "online" if online else "OFFLINE",
                "now" if online else _ago(d.get("last_seen")),
                f"{d.get('rtt')} ms" if d.get("rtt") is not None else "-"]
        for (name, cw), v in zip(cols, vals):
            if name == "State":
                pdf.set_text_color(*(EMERALD if online else RED))
            else:
                pdf.set_text_color(*TEXT)
            pdf.cell(cw, 4.2, fit(v, cw), border="B")
        pdf.ln()

    # recent events
    if events:
        pdf.set_y(pdf.get_y() + 4)
        if pdf.get_y() > pdf.h - 45:
            pdf.add_page()
        heading("RECENT EVENTS")
        pdf.set_font("DejaVu", "", 7)
        labels = {"new": "new device", "offline": "went offline",
                  "online": "back online", "ip_change": "IP reassigned"}
        for e in events[:22]:
            if pdf.get_y() > pdf.h - 20:
                pdf.add_page()
            who = e.get("name") or e.get("vendor") or e.get("hostname") or e.get("mac") or "?"
            pdf.set_text_color(*MUTED)
            pdf.cell(20, 4.2, _ago(e.get("ts")))
            pdf.set_text_color(*CYAN)
            pdf.cell(24, 4.2, labels.get(e.get("type"), e.get("type") or "?"))
            pdf.set_text_color(*TEXT)
            pdf.cell(0, 4.2, fit(f"{who}  -  {e.get('ip') or ''}", W - 44),
                     new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    return bytes(pdf.output())


def _ip_num(ip):
    try:
        a, b, c, d = (int(x) for x in (ip or "").split("."))
        return ((a * 256 + b) * 256 + c) * 256 + d
    except (ValueError, TypeError):
        return 1 << 40


def generate(card, devices, conflicts, events, internet, reach, ai_cfg, sysinfo=None):
    """One-stop: facts -> Gemini -> PDF bytes. Raises ReportError on failure."""
    facts = gather_facts(card, devices, conflicts, events, internet, reach, sysinfo)
    ai = analyze(facts, ai_cfg)
    return build_pdf(facts, ai, devices, events)
