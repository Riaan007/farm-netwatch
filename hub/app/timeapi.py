"""HTTP side of time keeping (timekeeping.py holds the rules): the Control Center's
Time page, its heartbeat, the field buttons and the month-end timesheets.

Every route sits behind the hub login (server._guard). Site ids come from
hub.json; device names come from a site and are treated as hostile text.
"""
import csv
import datetime as dt
import io

from flask import Blueprint, jsonify, request, send_file

import hubconfig
import timekeeping as tk

bp = Blueprint("time", __name__)

KIND_LABEL = {"remote": "Remote", "onsite": "On site", "travel": "Travel", "dev": "Netwatch dev",
              "general": "General"}


def _err(e):
    return jsonify({"ok": False, "error": str(e)}), getattr(e, "status", 400)


def _sites():
    return [{"id": s["id"], "name": s.get("name") or s["id"], "enabled": s.get("enabled", True)}
            for s in hubconfig.load().get("sites") or []]


def _site_ok(sid):
    return bool(sid) and hubconfig.get_site(sid) is not None


def _body():
    b = request.get_json(silent=True)
    return b if isinstance(b, dict) else {}


@bp.route("/api/hub/time/state")
def time_state():
    st = tk.settings()
    return jsonify({"ok": True, "open": tk.open_entries(), "today": tk.today(), "sites": _sites(),
                    "settings": st, "farms": {s["id"]: tk.farm_settings(s["id"], st) for s in _sites()}})


@bp.route("/api/hub/time/beat", methods=["POST"])
def time_beat():
    """The Control Center says the operator is actively on a page. `site` is the
    farm whose page it is, or '' for the all-farm pages."""
    sid = str(_body().get("site") or "")
    if sid and not _site_ok(sid):
        sid = ""
    tk.mark("cc", sid)
    return jsonify({"ok": True})


@bp.route("/api/hub/time/day")
def time_day():
    try:
        return jsonify({"ok": True, **tk.day_view(request.args.get("date") or tk.today())})
    except tk.TimeError as e:
        return _err(e)


@bp.route("/api/hub/time/day/<day>/approve", methods=["POST"])
def time_day_approve(day):
    try:
        return jsonify({"ok": True, **tk.approve_day(day)})
    except tk.TimeError as e:
        return _err(e)


def _check_site(body):
    sid = body.get("site_id")
    if sid and not _site_ok(sid):
        raise tk.TimeError("unknown farm")


@bp.route("/api/hub/time/entries", methods=["POST"])
def time_add():
    body = _body()
    try:
        _check_site(body)
        return jsonify({"ok": True, "entry": tk.add_entry(body)})
    except tk.TimeError as e:
        return _err(e)


@bp.route("/api/hub/time/entries/<int:eid>", methods=["POST", "DELETE"])
def time_entry(eid):
    try:
        if request.method == "DELETE":
            tk.delete_entry(eid)
            return jsonify({"ok": True})
        body = _body()
        _check_site(body)
        return jsonify({"ok": True, "entry": tk.update_entry(eid, body)})
    except tk.TimeError as e:
        return _err(e)


@bp.route("/api/hub/time/clock", methods=["POST"])
def time_clock():
    body = _body()
    try:
        _check_site(body)
        km = body.get("km")
        km = float(km) if km not in (None, "") else None
        return jsonify({"ok": True, "open": tk.clock(str(body.get("action") or ""), body.get("site_id") or "", km)})
    except (TypeError, ValueError):
        return _err(tk.TimeError("km must be a number"))
    except tk.TimeError as e:
        return _err(e)


@bp.route("/api/hub/time/month")
def time_month():
    month = request.args.get("month") or dt.date.today().strftime("%Y-%m")
    try:
        sites = _sites()
        st = tk.settings()
        ids = [s["id"] for s in sites if tk.farm_settings(s["id"], st)["billable"]]
        v = tk.month_view(month, ids)
        return jsonify({"ok": True, "sites": sites, **v})
    except tk.TimeError as e:
        return _err(e)


@bp.route("/api/hub/time/settings", methods=["GET", "POST"])
def time_settings():
    if request.method == "POST":
        body = _body()
        try:
            for sid in (body.get("farms") or {}):
                if not _site_ok(sid):
                    raise tk.TimeError("unknown farm")
            tk.save_settings(body)
        except tk.TimeError as e:
            return _err(e)
    st = tk.settings()
    return jsonify({"ok": True, "settings": st, "sites": _sites(),
                    "farms": {s["id"]: tk.farm_settings(s["id"], st) for s in _sites()}})


@bp.route("/api/hub/time/phones/<site_id>")
def time_phones(site_id):
    """The farm's devices to pick the operator's phone from (the one that joins
    the farm Wi-Fi). Newest-seen first; values are the site's — escape them."""
    if not _site_ok(site_id):
        return _err(tk.TimeError("unknown farm", 404))
    from poller import poller
    devs = ((poller.snapshot(site_id) or {}).get("devices") or {}).get("devices") or []
    out = [{"key": d.get("key"), "name": d.get("name") or d.get("device_name") or d.get("hostname") or "",
            "ip": d.get("ip"), "mac": d.get("mac"), "vendor": d.get("vendor"), "category": d.get("category"),
            "online": bool(d.get("online")), "last_seen": d.get("last_seen"), "first_seen": d.get("first_seen")}
           for d in devs if d.get("key")]
    out.sort(key=lambda d: -(d["last_seen"] or 0))
    return jsonify({"ok": True, "devices": out})


# ---- timesheets --------------------------------------------------------------------------
def _hm(minutes):
    m = int(round(minutes))
    return f"{m // 60}:{m % 60:02d}"


def _money(cur, v):
    return f"{cur} {v:,.2f}"


def _clock(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M")


def _month_label(month):
    return dt.datetime.strptime(month + "-01", "%Y-%m-%d").strftime("%B %Y")


def _lines(b):
    """Invoice lines of one farm's month: (label, qty text, rate text, amount)."""
    f, t, a, cur = b["farm"], b["totals"], b["amounts"], b["currency"]
    out = []
    if t["remote"]:
        out.append(("Remote support", f"{_hm(t['remote'])} h", f"{_money(cur, f['rate_remote'])}/h", a["remote"]))
    if t["onsite"]:
        out.append((f"On site ({t['visits']} visit{'s' * (t['visits'] != 1)})", f"{_hm(t['onsite'])} h",
                    f"{_money(cur, f['rate_onsite'])}/h", a["onsite"]))
    if t["travel"]:
        out.append(("Travel time", f"{_hm(t['travel'])} h", f"{_money(cur, f['rate_travel'])}/h", a["travel"]))
    if t["km"]:
        out.append(("Distance", f"{t['km']:,.0f} km", f"{_money(cur, f['rate_km'])}/km", a["km"]))
    return out


def _rules_text(b):
    f = b["farm"]
    s = f"Billable time is rounded up to {b['round_min']} minutes per day for each kind of work."
    if f["callout_min"]:
        s += f" Each on-site visit counts at least {_hm(f['callout_min'])} h (call-out minimum)."
    return s


def timesheet_csv(b, farm_name):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Farm", farm_name, "Month", _month_label(b["month"])])
    w.writerow([])
    w.writerow(["Date", "Kind", "From", "To", "Minutes", "km", "Note", "Status"])
    for day, d in b["days"].items():
        for e in d["entries"]:
            w.writerow([day, KIND_LABEL.get(e["kind"], e["kind"]), _clock(e["start_ts"]), _clock(e["end_ts"]),
                        e["minutes"], e["km"] if e["km"] is not None else "", e["note"], e["status"]])
    w.writerow([])
    w.writerow(["Day", "Remote billed (min)", "On site billed (min)", "Travel billed (min)", "km"])
    for day, d in b["days"].items():
        w.writerow([day, d["remote_billed"], d["onsite_billed"], d["travel_billed"], round(d["km"], 1)])
    w.writerow([])
    w.writerow(["Line", "Quantity", "Rate", "Amount"])
    for label, qty, rate, amt in _lines(b):
        w.writerow([label, qty, rate, f"{amt:.2f}"])
    w.writerow(["Total", "", "", f"{b['total']:.2f}"])
    w.writerow([])
    w.writerow([_rules_text(b)])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def timesheet_pdf(b, farm_name, business):
    from fpdf import FPDF
    font_dir = "/usr/share/fonts/truetype/dejavu"
    navy, muted, ink, line = (6, 10, 19), (100, 112, 130), (20, 24, 32), (210, 215, 222)
    cur = b["currency"]

    class PDF(FPDF):
        def footer(self):
            self.set_y(-12)
            self.set_font("DejaVu", "", 7)
            self.set_text_color(*muted)
            self.cell(0, 5, f"{business} · Timesheet {farm_name} · {_month_label(b['month'])} · "
                            f"page {self.page_no()}/{{nb}}", align="C")

    pdf = PDF(format="A4")
    pdf.add_font("DejaVu", "", f"{font_dir}/DejaVuSans.ttf")
    pdf.add_font("DejaVu", "B", f"{font_dir}/DejaVuSans-Bold.ttf")
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(True, margin=16)
    pdf.set_margins(14, 14, 14)
    pdf.add_page()
    W = pdf.w - 28

    pdf.set_fill_color(*navy)
    pdf.rect(0, 0, pdf.w, 30, style="F")
    pdf.set_xy(14, 8)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("DejaVu", "B", 16)
    pdf.cell(W * 0.6, 8, "Timesheet")
    pdf.set_font("DejaVu", "", 9)
    pdf.cell(W * 0.4, 8, business, align="R")
    pdf.set_xy(14, 17)
    pdf.set_font("DejaVu", "", 10)
    pdf.cell(W * 0.6, 6, f"{farm_name} · {_month_label(b['month'])}")
    pdf.set_font("DejaVu", "", 8)
    pdf.cell(W * 0.4, 6, f"made {dt.date.today():%d %b %Y}", align="R")
    pdf.set_y(36)

    # summary
    pdf.set_text_color(*ink)
    pdf.set_font("DejaVu", "B", 11)
    pdf.cell(0, 7, "Summary", new_x="LMARGIN", new_y="NEXT")
    cols = (W * 0.46, W * 0.16, W * 0.18, W * 0.20)
    pdf.set_font("DejaVu", "B", 8)
    pdf.set_text_color(*muted)
    for wd, h, al in zip(cols, ("Item", "Quantity", "Rate", "Amount"), ("L", "R", "R", "R")):
        pdf.cell(wd, 6, h, align=al)
    pdf.ln(6)
    pdf.set_draw_color(*line)
    pdf.line(14, pdf.get_y(), 14 + W, pdf.get_y())
    pdf.set_text_color(*ink)
    pdf.set_font("DejaVu", "", 9)
    lines = _lines(b)
    if not lines:
        pdf.set_text_color(*muted)
        pdf.cell(W, 7, "No approved time for this month.", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*ink)
    for label, qty, rate, amt in lines:
        pdf.cell(cols[0], 7, label)
        pdf.cell(cols[1], 7, qty, align="R")
        pdf.cell(cols[2], 7, rate, align="R")
        pdf.cell(cols[3], 7, _money(cur, amt), align="R")
        pdf.ln(7)
    pdf.line(14, pdf.get_y(), 14 + W, pdf.get_y())
    pdf.set_font("DejaVu", "B", 10)
    pdf.cell(sum(cols[:3]), 8, "Total")
    pdf.cell(cols[3], 8, _money(cur, b["total"]), align="R")
    pdf.ln(9)
    pdf.set_font("DejaVu", "", 7.5)
    pdf.set_text_color(*muted)
    pdf.multi_cell(W, 4, _rules_text(b) + (" Includes entries not yet reviewed." if b["include_drafts"] else ""),
                   new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # detail
    pdf.set_text_color(*ink)
    pdf.set_font("DejaVu", "B", 11)
    pdf.cell(0, 7, "Detail", new_x="LMARGIN", new_y="NEXT")
    dc = (W * 0.13, W * 0.14, W * 0.10, W * 0.08, W * 0.55)
    for day, d in b["days"].items():
        if pdf.get_y() > pdf.h - 40:
            pdf.add_page()
        pdf.set_fill_color(240, 243, 247)
        pdf.set_font("DejaVu", "B", 8.5)
        billed = []
        if d["remote_billed"]:
            billed.append(f"remote {_hm(d['remote_billed'])}")
        if d["onsite_billed"]:
            billed.append(f"on site {_hm(d['onsite_billed'])}")
        if d["travel_billed"]:
            billed.append(f"travel {_hm(d['travel_billed'])}")
        if d["km"]:
            billed.append(f"{d['km']:,.0f} km")
        pdf.cell(W * 0.35, 6.5, dt.date.fromisoformat(day).strftime("%a %d %b"), fill=True)
        pdf.set_font("DejaVu", "", 8)
        pdf.cell(W * 0.65, 6.5, "Billed: " + ", ".join(billed), fill=True, align="R")
        pdf.ln(6.5)
        pdf.set_font("DejaVu", "", 8)
        for e in d["entries"]:
            note = e["note"] or ""
            if e["kind"] == "travel" and e["km"]:
                note = (f"{e['km']:,.0f} km · " + note).strip(" ·")
            pdf.cell(dc[0], 5.5, KIND_LABEL.get(e["kind"], e["kind"]))
            pdf.cell(dc[1], 5.5, f"{_clock(e['start_ts'])}–{_clock(e['end_ts'])}")
            pdf.cell(dc[2], 5.5, _hm(e["minutes"]), align="R")
            pdf.cell(dc[3], 5.5, "")
            pdf.multi_cell(dc[4], 5.5, note[:300], new_x="LMARGIN", new_y="NEXT")
    if not b["days"]:
        pdf.set_font("DejaVu", "", 9)
        pdf.set_text_color(*muted)
        pdf.cell(0, 7, "Nothing recorded.", new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


@bp.route("/api/hub/time/timesheet")
def time_timesheet():
    sid = request.args.get("site") or ""
    month = request.args.get("month") or dt.date.today().strftime("%Y-%m")
    fmt = request.args.get("format") or "pdf"
    site = hubconfig.get_site(sid) if sid else None
    if not site:
        return _err(tk.TimeError("unknown farm", 404))
    try:
        b = tk.bill(sid, month, include_drafts=request.args.get("drafts") == "1")
    except tk.TimeError as e:
        return _err(e)
    name = site.get("name") or sid
    safe = "".join(ch if ch.isalnum() else "-" for ch in name).strip("-") or sid
    if fmt == "csv":
        return send_file(io.BytesIO(timesheet_csv(b, name)), mimetype="text/csv", as_attachment=True,
                         download_name=f"timesheet-{safe}-{month}.csv")
    pdf = timesheet_pdf(b, name, tk.settings()["global"]["business"])
    return send_file(io.BytesIO(pdf), mimetype="application/pdf", as_attachment=True,
                     download_name=f"timesheet-{safe}-{month}.pdf")
