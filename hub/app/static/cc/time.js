/* Netwatch Control Center — Time: where the operator's hours go, per farm.
 * Data: /api/hub/time/* (hub/app/timeapi.py; the rules live in timekeeping.py).
 * Routes: #/time (one day, ?d=YYYY-MM-DD) · #/time/month (?m=YYYY-MM) · #/time/settings.
 * Also here: the activity heartbeat every Control Center page sends while you are
 * actively using it (a farm's pages count for that farm; the Time page itself never
 * counts), and the clock-in badge on the nav. Everything a site sends (device
 * names, MACs) is escaped. */
(function () {
  "use strict";
  const { $, esc, icon, api } = CC;
  const view = () => $("#view");
  const enc = encodeURIComponent;
  const T = { st: null, day: null, month: null, set: null };

  const KIND = {
    remote: { label: "Remote", cls: "k-remote" },
    onsite: { label: "On site", cls: "k-onsite" },
    travel: { label: "Travel", cls: "k-travel" },
    dev: { label: "Netwatch dev", cls: "k-dev" },
    general: { label: "General", cls: "k-general" },
  };
  const SRC = { cc: "Control Center", proxy: "farm page", tunnel: "tunnel / SSH", site: "farm Pi directly", vpn: "VPN to the farm", ssh: "SSH to the farm Pi", wifi: "phone on farm Wi-Fi",
    button: "buttons", manual: "typed in", claude: "Claude sessions", auto: "recorded" };

  // ---- helpers ------------------------------------------------------------------------
  const pad = (n) => String(n).padStart(2, "0");
  const isoDay = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const isoMonth = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}`;
  const parseDay = (s) => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); };
  const hhmm = (ts) => { const d = new Date(ts * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
  const hm = (sec) => { const m = Math.round(sec / 60); return `${Math.floor(m / 60)}:${pad(m % 60)}`; };
  const durText = (sec) => { const m = Math.round(sec / 60); return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${pad(m % 60)}`; };
  const money = (v, cur) => `${cur || "R"} ${Number(v || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  const sites = () => (T.st && T.st.sites) || [];
  const siteName = (id) => { const s = sites().find((x) => x.id === id); return s ? s.name : id; };
  const dur = (e, now) => Math.max(0, (e.end_ts || now || CC.now()) - e.start_ts);
  const srcText = (e) => (e.sources || e.source || "").split(",").filter(Boolean).map((s) => SRC[s] || s).join(" + ");
  const farmOptions = (sel, { blank } = {}) => (blank ? `<option value="">${esc(blank)}</option>` : "")
    + sites().map((s) => `<option value="${esc(s.id)}" ${s.id === sel ? "selected" : ""}>${esc(s.name)}</option>`).join("");

  async function loadState() {
    try { T.st = await api("/api/hub/time/state"); } catch (e) { /* keep the last one */ }
    navBadge();
    return T.st;
  }

  // ---- heartbeat ------------------------------------------------------------------------
  let lastInput = 0, lastMove = 0, lastBeat = 0;
  const touched = () => { lastInput = Date.now(); };
  ["pointerdown", "keydown", "wheel", "touchstart", "scroll"].forEach((ev) => window.addEventListener(ev, touched, { passive: true, capture: true }));
  window.addEventListener("mousemove", () => { const n = Date.now(); if (n - lastMove > 5000) { lastMove = n; lastInput = n; } }, { passive: true });
  function beat(force) {
    const now = Date.now();
    if (document.hidden || now - lastInput > 120000 || (!force && now - lastBeat < 55000) || now - lastBeat < 20000) return;
    const h = location.hash || "#/";
    if (h.startsWith("#/time")) return;
    const m = h.match(/^#\/site\/([^/?]+)/);
    lastBeat = now;
    api("/api/hub/time/beat", { method: "POST", body: { site: m ? decodeURIComponent(m[1]) : "" } }).catch(() => {});
  }
  setInterval(() => beat(false), 15000);
  window.addEventListener("hashchange", () => beat(true));

  // ---- nav badge: clocked in ---------------------------------------------------------------
  function navBadge() {
    const b = $("#nav-time-on");
    if (!b) return;
    const o = (T.st && T.st.open) || [];
    const cur = o[o.length - 1];
    b.hidden = !cur;
    if (cur) {
      b.textContent = cur.kind === "travel" ? "Driving" : "On site";
      b.title = `${cur.kind === "travel" ? "Driving" : "On site"} · ${siteName(cur.site_id)} since ${hhmm(cur.start_ts)}`;
    }
  }
  setInterval(() => { if (!document.hidden) loadState(); }, 60000);
  setTimeout(loadState, 1500);

  // ---- page shell -------------------------------------------------------------------------
  function shell(tab, actions = "") {
    CC.setCrumbs([{ label: "Overview", href: "#/" }, { label: "Time" }]);
    view().innerHTML = `<div class="ph"><div><div class="eyebrow">Time keeping</div><h1>Your hours</h1>
        <p>Remote work, farm visits, travel and Netwatch development — recorded as you work, checked by you, billed per farm.</p></div>
        <div class="actions">${actions}</div></div>
      <nav class="tabs"><a href="#/time" class="${tab === "day" ? "on" : ""}">${icon("clock")} Day</a><a href="#/time/month" class="${tab === "month" ? "on" : ""}">${icon("file")} Month &amp; timesheets</a><a href="#/time/settings" class="${tab === "set" ? "on" : ""}">${icon("gear")} Rates &amp; phones</a></nav>
      <div id="tm-body"><div class="skel" style="height:120px"></div></div>`;
  }

  // ---- clock card (the field buttons) --------------------------------------------------------
  function clockCard() {
    const o = (T.st && T.st.open) || [];
    const cur = o[o.length - 1];
    const farms = sites().filter((s) => s.enabled !== false);
    const pick = (T.clockSite && farms.some((s) => s.id === T.clockSite)) ? T.clockSite : (cur ? cur.site_id : (farms.find((s) => s.id !== "home") || farms[0] || {}).id);
    if (cur && cur.kind === "onsite") {
      return `<section class="panel tm-clock tm-on"><div class="st"><div class="eyebrow" style="color:var(--ok)">On site</div>
          <div class="big">${esc(siteName(cur.site_id))}</div><div class="muted">since ${hhmm(cur.start_ts)} · <span data-run="${cur.start_ts}">${durText(dur(cur))}</span></div></div>
          <div class="row"><button class="btn" data-act="clock" data-a="leave">Leave farm</button><button class="btn good" data-act="clock" data-a="leave_drive">${icon("logout")} Leave &amp; drive home</button></div></section>`;
    }
    if (cur && cur.kind === "travel") {
      return `<section class="panel tm-clock tm-drive"><div class="st"><div class="eyebrow">Driving</div>
          <div class="big">${esc(siteName(cur.site_id))}</div><div class="muted">since ${hhmm(cur.start_ts)} · <span data-run="${cur.start_ts}">${durText(dur(cur))}</span>${cur.km ? ` · ${esc(cur.km)} km` : ""}</div></div>
          <div class="row"><button class="btn" data-act="clock" data-a="home">Back home</button><button class="btn good" data-act="clock" data-a="arrive" data-site="${esc(cur.site_id)}">${icon("pin")} Arrived at ${esc(siteName(cur.site_id))}</button></div></section>`;
    }
    return `<section class="panel tm-clock"><div class="st"><div class="eyebrow">Field work</div><div class="big">Not clocked in</div>
        <div class="note">Driving logs a travel line (with the farm's usual km); Arrived starts the visit. Your phone on the farm's Wi-Fi also records visits by itself.</div></div>
        <div class="row"><select class="sel" id="tm-farm" aria-label="Farm">${farmOptions(pick)}</select>
        <button class="btn" data-act="clock" data-a="drive">Driving to farm</button><button class="btn good" data-act="clock" data-a="arrive">${icon("pin")} Arrived at farm</button></div></section>`;
  }

  async function clockAct(btn) {
    const a = btn.dataset.a;
    const site = btn.dataset.site || ($("#tm-farm") && $("#tm-farm").value) || "";
    if ((a === "drive" || a === "arrive") && !site) return CC.toast("Pick the farm first", "bad");
    await CC.busy(btn, async () => {
      try {
        const j = await api("/api/hub/time/clock", { method: "POST", body: { action: a, site_id: site } });
        T.st = { ...(T.st || {}), open: j.open };
        navBadge();
        CC.toast({ drive: "Travel started", arrive: "Visit started", leave: "Visit ended", leave_drive: "Visit ended — drive home started", home: "Trip ended" }[a] || "Done", "ok");
        T.day = null;
        dayView.load();
      } catch (e) { CC.toast(e.message, "bad"); }
    });
  }

  // ---- DAY ---------------------------------------------------------------------------------
  const dayView = {
    enter() {
      shell("day", `<button class="btn" id="tm-add">${icon("plus")} Add time</button>`);
      $("#tm-add").onclick = () => editDialog(null);
      $("#tm-body").addEventListener("click", (e) => this.click(e));
      $("#tm-body").addEventListener("change", (e) => this.change(e));
      this.day = CC.params().get("d") || isoDay(new Date());
      T.day = null;
      this.load();
      clearInterval(this.tick);
      this.tick = setInterval(() => { document.querySelectorAll("[data-run]").forEach((el) => { el.textContent = durText(CC.now() - Number(el.dataset.run)); }); }, 30000);
    },
    leave() { clearInterval(this.tick); },
    update() {},
    async load() {
      if (!T.st) await loadState();
      try { T.day = await api(`/api/hub/time/day?date=${enc(this.day)}`); } catch (e) { $("#tm-body").innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; return; }
      T.st = { ...(T.st || {}), open: T.day.open };
      navBadge();
      this.render();
    },
    render() {
      const d = T.day, body = $("#tm-body");
      if (!d || !body) return;
      const now = d.now;
      const date = parseDay(this.day);
      const isToday = this.day === isoDay(new Date());
      const ents = d.entries;
      const tot = { remote: 0, onsite: 0, travel: 0, dev: 0, general: 0, km: 0, unassigned: 0, drafts: 0 };
      ents.forEach((e) => {
        const s = dur(e, now);
        if (e.kind === "remote" && !e.site_id) tot.unassigned += s; else tot[e.kind] += s;
        tot.km += e.km || 0;
        tot.drafts += e.status === "draft" && !!e.end_ts;
      });
      const kpi = (k, v, s, cls = "") => `<div class="kpi ${cls}"><div class="k">${k}</div><div class="v">${v}</div><div class="s">${s}</div></div>`;
      body.innerHTML = clockCard()
        + `<div class="tm-date"><button class="btn icon" data-act="go" data-d="-1" aria-label="Previous day">‹</button>
            <h2>${date.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long", year: "numeric" })}</h2>
            <button class="btn icon" data-act="go" data-d="1" aria-label="Next day" ${isToday ? "disabled" : ""}>›</button>
            <input type="date" class="inp" id="tm-pick" value="${esc(this.day)}" max="${isoDay(new Date())}" style="width:auto">
            ${isToday ? "" : `<button class="btn sm" data-act="today">Today</button>`}</div>`
        + `<div class="kpis">${kpi("Remote", hm(tot.remote), "farm pages, hub, tunnels")}${kpi("On site", hm(tot.onsite), "farm visits")}
            ${kpi("Travel", hm(tot.travel), tot.km ? `${tot.km.toFixed(0)} km` : "no km yet")}${kpi("Netwatch dev", hm(tot.dev), "Claude sessions")}
            ${tot.unassigned ? kpi("Needs a farm", hm(tot.unassigned), "hub time to assign", "is-warn") : kpi("General", hm(tot.general), "not billed")}</div>`
        + this.strip(ents, now)
        + `<section class="panel"><div class="phd"><h2>${icon("list")} Entries <small>${ents.length ? `${ents.length} · ${tot.drafts} to review` : ""}</small></h2>
            <div class="row">${tot.drafts ? `<button class="btn sm good" data-act="approve-all">Approve all ${tot.drafts}</button>` : ""}<button class="btn sm" data-act="add">${icon("plus")} Add</button></div></div>
            ${this.list(ents, now)}</section>
          <p class="note" style="margin-top:12px">Amber = still a draft (recorded, not checked). Only approved time goes on a timesheet. Dismissing a recorded entry keeps it from coming back; your own entries are deleted.</p>`;
    },
    strip(ents, now) {
      if (!ents.length) return "";
      const a = T.day.start, span = T.day.end - T.day.start;
      const seg = (e) => {
        const x = Math.max(0, (e.start_ts - a) / span * 100), w = Math.max(0.25, ((e.end_ts || now) - Math.max(e.start_ts, a)) / span * 100);
        const k = e.kind === "remote" && !e.site_id ? "k-unassigned" : (KIND[e.kind] || {}).cls;
        const who = e.site_id ? siteName(e.site_id) : (KIND[e.kind] || {}).label;
        return `<i class="${k}${e.status === "draft" ? " draft" : ""}" style="left:${x.toFixed(2)}%;width:${w.toFixed(2)}%" title="${esc(`${hhmm(e.start_ts)}–${e.end_ts ? hhmm(e.end_ts) : "now"} · ${(KIND[e.kind] || {}).label} · ${who}`)}"></i>`;
      };
      const ticks = [0, 3, 6, 9, 12, 15, 18, 21].map((h) => `<span style="left:${(h / 24 * 100).toFixed(2)}%">${pad(h)}</span>`).join("");
      return `<div class="tm-strip" role="img" aria-label="The day's entries on a 24-hour line">${ents.map(seg).join("")}</div><div class="tm-ticks">${ticks}</div>
        <div class="tm-legend">${Object.entries(KIND).map(([k, v]) => `<span><i class="${v.cls}"></i>${v.label}</span>`).join("")}<span><i class="k-unassigned"></i>Needs a farm</span></div>`;
    },
    list(ents, now) {
      if (!ents.length) {
        return `<div class="empty"><b>Nothing recorded on this day</b>Work in the Control Center or on a farm's page, use the buttons above, or add time by hand.</div>`;
      }
      const groups = [];
      const add = (key, title, sub, items) => { if (items.length) groups.push({ key, title, sub, items }); };
      add("unassigned", "Needs a farm", "hub pages not tied to one farm — assign each to a farm or to General", ents.filter((e) => e.kind === "remote" && !e.site_id));
      sites().forEach((s) => add(s.id, s.name, "", ents.filter((e) => e.site_id === s.id)));
      const known = new Set(sites().map((s) => s.id));
      add("other", "Removed farms", "", ents.filter((e) => e.site_id && !known.has(e.site_id)));
      add("dev", "Netwatch development", "from your Claude sessions — not billed to a farm", ents.filter((e) => e.kind === "dev"));
      add("general", "General", "not billed", ents.filter((e) => e.kind === "general"));
      return groups.map((g) => {
        const sum = g.items.reduce((t, e) => t + dur(e, now), 0);
        return `<div class="tm-grp ${g.key === "unassigned" ? "warn" : ""}"><b>${esc(g.title)}</b><span class="note">${esc(g.sub)}</span><span class="tnum">${hm(sum)}</span></div>`
          + g.items.map((e) => this.row(e, now)).join("");
      }).join("");
    },
    row(e, now) {
      const k = KIND[e.kind] || { label: e.kind, cls: "" };
      const unassigned = e.kind === "remote" && !e.site_id;
      const running = !e.end_ts;
      const status = running ? `<span class="b info">running</span>` : e.status === "draft" ? `<span class="b warn">draft</span>` : `<span class="b ok">approved</span>`;
      const km = e.kind === "travel" ? ` · ${e.km != null ? esc(e.km) + " km" : `<span class="warn">km?</span>`}` : "";
      const acts = [];
      if (unassigned) {
        acts.push(`<select class="sel" data-act="assign" data-id="${e.id}" aria-label="Assign to"><option value="">Assign to…</option>${farmOptions("")}<option value="@general">General (not billed)</option></select>`);
      } else if (e.status === "draft" && !running) {
        acts.push(`<button class="btn sm good" data-act="approve" data-id="${e.id}">${icon("check")} Approve</button>`);
      }
      acts.push(`<button class="btn sm" data-act="edit" data-id="${e.id}">Edit</button>`);
      if (!running) acts.push(`<button class="btn sm ghost" data-act="del" data-id="${e.id}" title="${e.source === "manual" || e.source === "button" ? "Delete" : "Dismiss — it won't come back"}" aria-label="Remove">${icon("x")}</button>`);
      return `<div class="tm-row ${e.status === "draft" && !running ? "draft" : ""} ${unassigned ? "unassigned" : ""}">
          <div class="when">${hhmm(e.start_ts)}–${running ? "now" : hhmm(e.end_ts)}</div>
          <div class="dur">${hm(dur(e, now))}</div>
          <div class="what"><span class="tm-kind ${k.cls}">${esc(k.label)}</span> ${status} <span class="src">${esc(srcText(e))}${km}</span>${e.note ? `<div class="nt">${esc(e.note)}</div>` : ""}</div>
          <div class="acts">${acts.join("")}</div></div>`;
    },
    entry(id) { return ((T.day && T.day.entries) || []).find((e) => String(e.id) === String(id)); },
    async click(ev) {
      const b = ev.target.closest("[data-act]");
      if (!b || b.tagName === "SELECT") return;
      const a = b.dataset.act;
      if (a === "clock") return clockAct(b);
      if (a === "go") { const d = parseDay(this.day); d.setDate(d.getDate() + Number(b.dataset.d)); return CC.go("#/time?d=" + isoDay(d)); }
      if (a === "today") return CC.go("#/time");
      if (a === "add") return editDialog(null, this.day);
      if (a === "edit") return editDialog(this.entry(b.dataset.id));
      if (a === "approve") return this.save(b, b.dataset.id, { status: "approved" }, "Approved");
      if (a === "del") {
        const e = this.entry(b.dataset.id);
        const mine = e && (e.source === "manual" || e.source === "button");
        if (!(await CC.confirm(mine ? "Delete this entry?" : "Dismiss this entry?", mine ? "It is removed for good." : "It is hidden and the recorder won't bring the same time back.", { ok: mine ? "Delete" : "Dismiss", danger: true }))) return;
        try { await api(`/api/hub/time/entries/${enc(b.dataset.id)}`, { method: "DELETE" }); this.load(); } catch (err) { CC.toast(err.message, "bad"); }
        return;
      }
      if (a === "approve-all") {
        await CC.busy(b, async () => {
          try {
            const j = await api(`/api/hub/time/day/${enc(this.day)}/approve`, { method: "POST" });
            CC.toast(`Approved ${j.approved}` + (j.unassigned_left ? ` — ${j.unassigned_left} still need a farm` : ""), j.unassigned_left ? "" : "ok");
            this.load();
          } catch (err) { CC.toast(err.message, "bad"); }
        });
      }
    },
    async change(ev) {
      const t = ev.target;
      if (t.id === "tm-pick" && t.value) return CC.go("#/time?d=" + t.value);
      if (t.id === "tm-farm") { T.clockSite = t.value; return; }
      if (t.dataset.act === "assign" && t.value) {
        const body = t.value === "@general" ? { kind: "general" } : { site_id: t.value };
        await this.save(t, t.dataset.id, body, t.value === "@general" ? "Moved to General" : "Assigned to " + siteName(t.value));
      }
    },
    async save(el, id, body, ok) {
      try {
        el.disabled = true;
        await api(`/api/hub/time/entries/${enc(id)}`, { method: "POST", body });
        CC.toast(ok, "ok");
        this.load();
      } catch (err) { CC.toast(err.message, "bad"); el.disabled = false; }
    },
  };

  // ---- add / edit dialog ------------------------------------------------------------------
  function editDialog(e, day) {
    const isNew = !e;
    const d0 = e ? new Date(e.start_ts * 1000) : day ? parseDay(day) : new Date();
    const now = new Date();
    const start = e ? hhmm(e.start_ts) : `${pad(Math.max(0, now.getHours() - 1))}:00`;
    const end = e ? (e.end_ts ? hhmm(e.end_ts) : "") : `${pad(now.getHours())}:00`;
    const kind = e ? e.kind : "onsite";
    const dlg = CC.dialog(`<div class="dhd"><div><h2>${isNew ? "Add time" : "Edit entry"}</h2><p>${isNew ? "Typed entries count as approved." : e.status === "draft" ? "Saving your changes keeps it as a draft until you approve it." : "Changes are kept as you save them."}</p></div><button class="btn icon ghost" data-close aria-label="Close">${icon("x")}</button></div>
      <form class="dbd" id="tm-form">
        <div class="grid2"><label class="fld">Kind<select class="sel" name="kind" style="width:100%">${Object.entries(KIND).map(([k, v]) => `<option value="${k}" ${k === kind ? "selected" : ""}>${v.label}</option>`).join("")}</select></label>
        <label class="fld" data-farm>Farm<select class="sel" name="site_id" style="width:100%">${farmOptions(e ? e.site_id : (T.clockSite || ""), { blank: "— pick —" })}</select></label></div>
        <div class="grid2"><label class="fld">Date<input class="inp" type="date" name="date" value="${isoDay(d0)}" max="${isoDay(now)}" required></label>
        <div class="grid2"><label class="fld">From<input class="inp" type="time" name="start" value="${start}" required></label><label class="fld">To<input class="inp" type="time" name="end" value="${end}" ${e && !e.end_ts ? "" : "required"}></label></div></div>
        <label class="fld" data-km>Kilometres (this trip)<input class="inp" type="number" name="km" min="0" max="5000" step="1" value="${e && e.km != null ? esc(e.km) : ""}" placeholder="e.g. 140"></label>
        <label class="fld">Note <span class="note">(shows on the timesheet)</span><textarea class="inp" name="note" rows="2" maxlength="500" placeholder="What was done">${esc(e ? e.note : "")}</textarea></label>
        ${e && !e.end_ts ? `<p class="note" style="margin:0">This entry is still running — leave “To” empty to keep it running.</p>` : ""}
        <div class="bad note" id="tm-err"></div>
        <div class="row" style="justify-content:flex-end">${!isNew && e.status === "draft" && e.end_ts ? `<label class="chk" style="margin-right:auto"><input type="checkbox" name="approve" checked> Approve it too</label>` : ""}<button type="button" class="btn" data-close>Cancel</button><button class="btn pri">${isNew ? "Add" : "Save"}</button></div>
      </form>`);
    const f = $("#tm-form", dlg);
    const sync = () => {
      const k = f.kind.value;
      $("[data-farm]", dlg).hidden = k === "dev" || k === "general";
      $("[data-km]", dlg).hidden = k !== "travel";
    };
    f.kind.onchange = sync; sync();
    f.onsubmit = async (ev) => {
      ev.preventDefault();
      const [y, m, dd] = f.date.value.split("-").map(Number);
      const at = (t) => { const [h, mi] = t.split(":").map(Number); return Math.floor(new Date(y, m - 1, dd, h, mi).getTime() / 1000); };
      const body = { kind: f.kind.value, site_id: f.site_id.value, note: f.note.value.trim(), km: f.kind.value === "travel" ? f.km.value : "" };
      body.start_ts = at(f.start.value);
      if (f.end.value) {
        body.end_ts = at(f.end.value);
        if (body.end_ts <= body.start_ts) body.end_ts += 86400;      // past midnight
      } else if (isNew || e.end_ts) {
        $("#tm-err", dlg).textContent = "Give an end time."; return;
      } else body.end_ts = null;
      if (f.approve && f.approve.checked) body.status = "approved";
      await CC.busy(ev.submitter, async () => {
        try {
          await api(isNew ? "/api/hub/time/entries" : `/api/hub/time/entries/${enc(e.id)}`, { method: "POST", body });
          dlg.close();
          CC.toast(isNew ? "Added" : "Saved", "ok");
          if (isNew && f.date.value !== dayView.day) CC.go("#/time?d=" + f.date.value); else dayView.load();
        } catch (err) { $("#tm-err", dlg).textContent = err.message; }
      });
    };
  }

  // ---- MONTH --------------------------------------------------------------------------------
  const monthView = {
    enter() {
      shell("month");
      this.month = CC.params().get("m") || isoMonth(new Date());
      $("#tm-body").addEventListener("click", (e) => this.click(e));
      $("#tm-body").addEventListener("change", (e) => { if (e.target.id === "tm-drafts") { this.drafts = e.target.checked; this.render(); } });
      this.load();
    },
    update() {},
    async load() {
      if (!T.st) await loadState();
      try { T.month = await api(`/api/hub/time/month?month=${enc(this.month)}`); } catch (e) { $("#tm-body").innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; return; }
      this.render();
    },
    render() {
      const m = T.month, body = $("#tm-body");
      if (!m || !body) return;
      const cur = m.settings.global.currency;
      const [y, mo] = this.month.split("-").map(Number);
      const label = new Date(y, mo - 1, 1).toLocaleDateString([], { month: "long", year: "numeric" });
      const isNow = this.month === isoMonth(new Date());
      const drafts = m.farms.reduce((t, f) => t + f.drafts, 0) + Object.values(m.other).reduce((t, o) => t + o.drafts, 0);
      const noRates = (f) => !f.farm.rate_remote && !f.farm.rate_onsite && !f.farm.rate_travel && !f.farm.rate_km;
      const rows = m.farms.map((f) => {
        const t = f.totals;
        const has = t.remote || t.onsite || t.travel || t.km;
        const q = `site=${enc(f.site_id)}&month=${enc(this.month)}${this.drafts ? "&drafts=1" : ""}`;
        return `<tr style="cursor:default"><td class="nm">${esc(siteName(f.site_id))}</td>
          <td class="num tnum">${t.remote ? hm(t.remote * 60) : "—"}</td>
          <td class="num tnum">${t.onsite ? `${hm(t.onsite * 60)} <span class="sub">${t.visits}×</span>` : "—"}</td>
          <td class="num tnum">${t.travel ? hm(t.travel * 60) : "—"}</td>
          <td class="num tnum hide-m">${t.km ? t.km.toFixed(0) : "—"}</td>
          <td class="num tnum"><b>${has ? (noRates(f) ? `<a href="#/time/settings" class="warn">set rates</a>` : money(f.total, cur)) : "—"}</b></td>
          <td class="num">${f.drafts ? `<a class="b warn" href="#/time">${f.drafts} to review</a>` : ""}</td>
          <td class="num"><div class="row" style="justify-content:flex-end;flex-wrap:nowrap"><a class="btn sm" href="/api/hub/time/timesheet?${q}" download>${icon("down")} PDF</a><a class="btn sm ghost" href="/api/hub/time/timesheet?${q}&format=csv" download>CSV</a></div></td></tr>`;
      }).join("");
      const o = m.other;
      const otherRow = (k, title, sub, cls = "") => o[k] ? `<li><span class="sev ${cls || "info"}">${icon(k === "dev" ? "term" : k === "unassigned" ? "alert" : "clock")}</span><div><div class="t">${title} · ${hm(o[k].seconds)}</div><div class="d">${sub}</div></div>${k === "dev" && o[k].value ? `<b class="tnum">${money(o[k].value, cur)}</b>` : "<span></span>"}</li>` : "";
      body.innerHTML = `<div class="tm-date"><button class="btn icon" data-act="go" data-m="-1" aria-label="Previous month">‹</button><h2>${esc(label)}</h2>
          <button class="btn icon" data-act="go" data-m="1" aria-label="Next month" ${isNow ? "disabled" : ""}>›</button>${isNow ? "" : `<button class="btn sm" data-act="now">This month</button>`}</div>
        ${drafts ? `<div class="banner">${drafts} ${drafts === 1 ? "entry is" : "entries are"} still a draft this month. Timesheets only count approved time — review them on the <a href="#/time">Day</a> page${this.drafts ? "" : ", or tick below to include them"}.</div>` : ""}
        <section class="panel"><div class="phd"><h2>${icon("file")} Per farm <small>billable time after rounding</small></h2>
          <label class="chk note"><input type="checkbox" id="tm-drafts" ${this.drafts ? "checked" : ""}> Include drafts in timesheets</label></div>
          <div class="twrap"><table class="t"><thead><tr><th>Farm</th><th class="num">Remote</th><th class="num">On site</th><th class="num">Travel</th><th class="num hide-m">km</th><th class="num">Amount</th><th></th><th></th></tr></thead>
          <tbody>${rows || `<tr><td colspan="8" class="empty">No billable farms — see Rates &amp; phones.</td></tr>`}</tbody></table></div>
          <div class="pbd note" style="border-top:1px solid var(--line)">Rounded up to ${m.settings.global.round_min} minutes per day for each kind of work; each visit counts at least the farm's call-out minimum. Amounts use each farm's own rates. The table shows approved time${this.drafts ? "; the timesheet links include drafts" : ""}.</div></section>
        ${o.dev || o.general || o.unassigned ? `<section class="panel"><div class="phd"><h2>${icon("clock")} Not billed to a farm</h2></div><ul class="alist">
          ${otherRow("unassigned", "Needs a farm", "Hub time not yet assigned — assign it on the Day page.", "warn")}
          ${otherRow("dev", "Netwatch development", "Building Netwatch, from your Claude sessions.")}
          ${otherRow("general", "General", "Hub admin, not billed.")}</ul></section>` : ""}`;
    },
    click(ev) {
      const b = ev.target.closest("[data-act]");
      if (!b) return;
      if (b.dataset.act === "go") { const [y, mo] = this.month.split("-").map(Number); CC.go("#/time/month?m=" + isoMonth(new Date(y, mo - 1 + Number(b.dataset.m), 1))); }
      if (b.dataset.act === "now") CC.go("#/time/month");
    },
  };

  // ---- SETTINGS -----------------------------------------------------------------------------
  const FARM_FIELDS = [
    ["rate_remote", "Remote", "{c} per hour"], ["rate_onsite", "On site", "{c} per hour"], ["rate_travel", "Travel", "{c} per hour"],
    ["rate_km", "Distance", "{c} per km"], ["callout_min", "Call-out minimum", "minutes per visit"], ["km_one_way", "Usual km", "one way, prefills travel"],
  ];
  const GLOBAL_FIELDS = [
    ["round_min", "Round up to", "minutes"], ["idle_gap_min", "Remote pause", "minutes that still count"],
    ["wifi_gap_min", "Phone gap", "minutes between sightings"], ["dev_gap_min", "Claude pause", "minutes that still count"],
    ["min_session_min", "Shortest entry", "minutes"], ["dev_rate", "Dev time value", "{c} per hour, your records"],
  ];
  const settingsView = {
    enter() {
      shell("set", `<button class="btn pri" id="tm-save" disabled>${icon("save")} Save</button>`);
      $("#tm-save").onclick = (e) => this.save(e.currentTarget);
      const b = $("#tm-body");
      b.addEventListener("input", () => { $("#tm-save").disabled = false; });
      b.addEventListener("change", () => { $("#tm-save").disabled = false; });
      b.addEventListener("click", (e) => this.click(e));
      this.load();
    },
    update() {},
    async load() {
      try { T.set = await api("/api/hub/time/settings"); } catch (e) { $("#tm-body").innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; return; }
      T.st = { ...(T.st || {}), sites: T.set.sites };
      this.render();
    },
    render() {
      const s = T.set, g = s.settings.global, cur = g.currency;
      const num = (scope, id, k, label, hint, v) => `<label class="fld">${label} <span class="note">${esc(hint.replace("{c}", cur))}</span><input class="inp tnum" type="number" min="0" step="any" data-scope="${scope}" data-id="${esc(id)}" data-k="${k}" value="${esc(v)}"></label>`;
      const farm = (site) => {
        const f = s.farms[site.id];
        const phone = f.phone_key
          ? `<div class="row"><span class="b ok nodot">${icon("phone")} ${esc(f.phone_label || f.phone_key)}</span><button class="btn sm" data-act="phone" data-id="${esc(site.id)}">Change</button><button class="btn sm ghost" data-act="phone-clear" data-id="${esc(site.id)}">Clear</button></div>`
          : `<div class="row"><span class="note">Not set</span><button class="btn sm" data-act="phone" data-id="${esc(site.id)}">${icon("phone")} Choose my phone</button></div>`;
        return `<section class="panel"><div class="phd"><h2>${icon("site")} ${esc(site.name)}</h2>
            <label class="chk note"><input type="checkbox" data-scope="farm" data-id="${esc(site.id)}" data-k="billable" ${f.billable ? "checked" : ""}> Billable client</label></div>
          <div class="pbd"><div class="tm-fields">${FARM_FIELDS.map(([k, l, h]) => num("farm", site.id, k, l, h, f[k])).join("")}</div>
          <div class="tm-phone"><div class="fld" style="font-size:12px;color:var(--muted);font-weight:600;margin-bottom:6px">My phone on this farm's Wi-Fi <span class="note">— records your visits by itself</span></div>${phone}</div></div></section>`;
      };
      $("#tm-body").innerHTML = `<section class="panel"><div class="phd"><h2>${icon("gear")} Rules</h2></div><div class="pbd"><div class="tm-fields wide">
          ${GLOBAL_FIELDS.map(([k, l, h]) => num("global", "", k, l, h, g[k])).join("")}
          <label class="fld">Business name <span class="note">on the timesheet</span><input class="inp" data-scope="global" data-k="business" maxlength="60" value="${esc(g.business)}"></label>
          <label class="fld">Currency <span class="note">symbol</span><input class="inp" data-scope="global" data-k="currency" maxlength="6" value="${esc(g.currency)}"></label></div></div></section>
        <h2 class="tm-h2">Farms</h2><div class="tm-farms">${s.sites.map(farm).join("")}</div>
        <section class="panel" style="margin-top:16px"><div class="phd"><h2>${icon("term")} Netwatch development time</h2></div><div class="pbd note" style="font-size:13px">
          Read from your Claude Code sessions on this Pi every 15 minutes: every turn that worked on Netwatch (in <span class="mono">~/farm-netwatch</span>, or naming Netwatch) counts, joined into sessions with the Claude pause above. Only times leave the transcripts — never their text. It includes time Claude works on its own after your message, so check it on the Day page.</div></section>`;
    },
    click(ev) {
      const b = ev.target.closest("[data-act]");
      if (!b) return;
      const id = b.dataset.id;
      if (b.dataset.act === "phone") phoneDialog(id, (dev) => { this.setPhone(id, dev.key, dev.label); });
      if (b.dataset.act === "phone-clear") this.setPhone(id, "", "");
    },
    setPhone(id, key, label) {
      T.set.settings.farms[id] = { ...(T.set.farms[id] || {}), phone_key: key, phone_label: label };
      T.set.farms[id] = T.set.settings.farms[id];
      this.collect();
      this.render();
      $("#tm-save").disabled = false;
    },
    collect() {
      const out = { global: {}, farms: {} };
      document.querySelectorAll("#tm-body [data-scope]").forEach((el) => {
        const v = el.type === "checkbox" ? el.checked : el.value;
        if (el.dataset.scope === "global") out.global[el.dataset.k] = v;
        else (out.farms[el.dataset.id] = out.farms[el.dataset.id] || {})[el.dataset.k] = v;
      });
      Object.entries(T.set.farms).forEach(([id, f]) => { (out.farms[id] = out.farms[id] || {}); out.farms[id].phone_key = f.phone_key || ""; out.farms[id].phone_label = f.phone_label || ""; });
      // keep typed values across a re-render
      Object.assign(T.set.settings.global, out.global);
      Object.entries(out.farms).forEach(([id, f]) => { T.set.farms[id] = { ...(T.set.farms[id] || {}), ...f }; });
      return out;
    },
    async save(btn) {
      const body = this.collect();
      await CC.busy(btn, async () => {
        try {
          T.set = await api("/api/hub/time/settings", { method: "POST", body });
          CC.toast("Saved", "ok");
          this.render();
          btn.disabled = true;
          loadState();
        } catch (e) { CC.toast(e.message, "bad"); }
      });
    },
  };

  function phoneDialog(siteId, done) {
    const dlg = CC.dialog(`<div class="dhd"><div><h2>Which device is your phone?</h2><p>${esc(siteName(siteId))} · join the farm Wi-Fi once, then pick it here. Phones keep one private address per Wi-Fi network, so it stays recognisable.</p></div><button class="btn icon ghost" data-close aria-label="Close">${icon("x")}</button></div>
      <div class="dbd"><div class="search">${icon("search")}<input class="inp" id="ph-q" placeholder="Search name, IP, MAC, maker" aria-label="Search devices"></div><div id="ph-list" class="tm-phlist"><div class="skel" style="height:120px"></div></div></div>`, { cls: "modal wide" });
    let devs = [];
    const draw = () => {
      const q = $("#ph-q", dlg).value.trim().toLowerCase();
      const list = devs.filter((d) => !q || [d.name, d.ip, d.mac, d.vendor, d.category].join(" ").toLowerCase().includes(q)).slice(0, 80);
      $("#ph-list", dlg).innerHTML = list.length ? list.map((d, i) => `<button class="tm-ph" data-i="${devs.indexOf(d)}">
          <span><b>${esc(d.name || d.ip || d.mac)}</b><span class="note mono">${esc(d.ip || "")} · ${esc(d.mac || "")}</span></span>
          <span class="note">${esc(d.vendor || d.category || "")}</span>
          <span>${d.online ? `<span class="b ok">online</span>` : `<span class="note">seen ${CC.ago(d.last_seen)}</span>`}</span></button>`).join("")
        : `<div class="empty"><b>No devices match</b>The farm's list comes from its last scan.</div>`;
    };
    api(`/api/hub/time/phones/${enc(siteId)}`).then((j) => { devs = j.devices || []; draw(); })
      .catch((e) => { $("#ph-list", dlg).innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; });
    $("#ph-q", dlg).oninput = CC.debounce(draw, 120);
    $("#ph-list", dlg).onclick = (e) => {
      const b = e.target.closest("[data-i]");
      if (!b) return;
      const d = devs[Number(b.dataset.i)];
      done({ key: d.key, label: [d.name, d.ip].filter(Boolean).join(" · ") || d.mac || d.key });
      dlg.close();
    };
  }

  CC.route("/time/month", monthView);
  CC.route("/time/settings", settingsView);
  CC.route("/time", dayView);
})();
