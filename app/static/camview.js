/* Camera pictures — the Cameras tab's grid and the camera page. ONE module shown in
 * two places: the site page (app/static/camview.*) and the hub's Control Center
 * (hub/app/static/cc/camview.*). They are copied, not shared (separate images);
 * tests/test_camview_same.py keeps the copies identical — edit app/static, then cp.
 *
 * The caller hands over the data (a site's /api/cameras, or the hub's forward of
 * it) and a picUrl(group, cam, {size, fresh}). Pictures are fetched here: only for
 * tiles on screen, at most 3 at a time per site (a farm link is thin), as blobs
 * shown through object URLs so the page can read each picture's age and route
 * from its X-Picture-* headers. A camera's picture is drawn in the camera's own
 * shape (cam.aspect): an NVR squeezes every camera into 704x576, a 32:9 panorama
 * sends 640x360 — object-fit:fill on a box of the right shape undoes both.
 * Everything a site sends is escaped before it reaches innerHTML. */
(function () {
  "use strict";
  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ESC[c]);
  const mem = {
    get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : v; } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private window */ } },
  };
  const WHY = {
    offline: "Offline", no_login: "No login", login_refused: "Login refused",
    nvr_login_refused: "Recorder login refused", nvr_offline: "Recorder offline",
    chan_offline: "Empty channel", not_supported: "Not Hikvision", failed: "Last read failed", off: "Pictures off",
  };
  const ROUTE = { direct: "its own login", "own-generic": "its own login", "own-rtsp": "its RTSP stream", nvr: "its recorder", saved: "the saved copy", "hub-saved": "the hub's saved copy" };
  const ownLogin = (r) => r === "direct" || r === "own-generic" || r === "own-rtsp";   // the camera's own login (ISAPI, a snapshot URL, or an RTSP frame)
  const ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 7h3l2-3h8l2 3h3v13H3z"/><circle cx="12" cy="13" r="4"/></svg>';

  function ago(ts) {
    if (!ts) return "";
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 60) return Math.round(s) + " s ago";
    if (s < 3600) return Math.round(s / 60) + " min ago";
    if (s < 86400) return Math.round(s / 3600) + " h ago";
    const d = new Date(ts * 1000);
    return d.toLocaleDateString([], { day: "numeric", month: "short" }) + " " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  // ---- loading pictures: per-site lanes, 3 at a time ----------------------------------
  const LANES = new Map();
  function lane(id) {
    if (!LANES.has(id)) LANES.set(id, { active: 0, q: [] });
    return LANES.get(id);
  }
  function queue(id, job) {
    const l = lane(id);
    return new Promise((res, rej) => { l.q.push({ job, res, rej }); pump(l); });
  }
  function pump(l) {
    while (l.active < 3 && l.q.length) {
      const t = l.q.shift();
      l.active++;
      t.job().then(t.res, t.rej).finally(() => { l.active--; pump(l); });
    }
  }
  async function fetchPic(url) {
    const r = await fetch(url, { credentials: "same-origin", cache: "no-store" });
    if (!r.ok) {
      let j = {};
      try { j = await r.json(); } catch (e) { /* not JSON */ }
      const err = new Error(j.error || j.message || "HTTP " + r.status);
      err.why = j.why || ""; err.status = r.status;
      throw err;
    }
    if (!/^image\//.test(r.headers.get("Content-Type") || "")) throw new Error("the answer was not a picture");
    const blob = await r.blob();
    const size = (r.headers.get("X-Picture-Size") || "").split("x").map(Number);
    return {
      src: URL.createObjectURL(blob), ts: Number(r.headers.get("X-Picture-Ts")) || Date.now() / 1000,
      stale: r.headers.get("X-Picture-Stale") === "1", route: r.headers.get("X-Picture-Route") || "",
      why: r.headers.get("X-Picture-Why") || "", w: size[0] || 0, h: size[1] || 0, bytes: blob.size,
    };
  }

  const aspectOf = (c, p, learned) => c.aspect || learned || (p && ownLogin(p.route) && p.w && p.h ? p.w / p.h : 0) || 16 / 9;
  // Every tile has the same 16:9 frame; the picture sits inside it in the camera's own shape.
  const TILE = 16 / 9;
  const fitStyle = (a, box = TILE) => (a >= box ? `width:100%;height:${(100 * box / a).toFixed(2)}%` : `height:100%;width:${(100 * a / box).toFixed(2)}%`);
  // A 2-lens camera (thermal + normal, dual-lens) has views: one tile, the pictures side by side.
  const viewsOf = (c) => (c.views && c.views.length > 1 ? c.views.map((v) => v.n) : [1]);
  const vk = (k, n) => (n === 1 ? k : `${k}#v${n}`);
  // Another lens's shape is only known from its picture (a thermal one is often 4:3).
  const viewAspect = (c, n, p, learned) => (n === 1 ? aspectOf(c, p, learned) : (p && p.w && p.h ? p.w / p.h : 4 / 3));
  // Health (phase 2): the worst finding shown as a badge on the tile.
  const SEV = { crit: "cv-bad", warn: "cv-warn", info: "cv-dim" };
  const worst = (hs) => (hs || []).slice().sort((a, b) => ["crit", "warn", "info"].indexOf(a.sev) - ["crit", "warn", "info"].indexOf(b.sev))[0];

  // A recorder channel that is set up but has no camera behind it (often an old address
  // the camera moved off). Not "a camera is offline": kept apart, folded, below the grid.
  // Older site code says "offline" for these, so also read it from the channel itself.
  const emptyChan = (c) => c.kind === "channel" && (c.why === "chan_offline" || (c.why === "offline" && !!c.nvr && c.nvr.chan_online === false));
  function needsLook(c) { return !c.route || !c.online || c.why === "failed" || (c.health || []).some((h) => h.sev !== "info"); }

  // ---- the grid --------------------------------------------------------------------------
  function mount(el, opts) {
    const pre = opts.storeKey || "cv";
    const st = {
      // The filter always starts at All: a remembered one hid cameras without saying so.
      q: "", filt: "all", size: mem.get(pre + ".size", "m"),
      pics: new Map(), learned: new Map(), keepUntil: 0, dead: false, obs: null, timers: [], nvr: opts.nvr || "", recSet: new Map(),
    };
    el.innerHTML = `<div class="cv">
      <div class="cv-bar">
        <input type="search" class="cv-q" placeholder="Search cameras" aria-label="Search cameras" autocomplete="off">
        <div class="cv-chips" role="group" aria-label="Show"></div>
        <div class="cv-seg" role="group" aria-label="Picture size">${["s", "m", "l"].map((s) => `<button type="button" data-size="${s}" aria-pressed="false">${s.toUpperCase()}</button>`).join("")}</div>
        <button type="button" class="cv-btn" data-act="fresh" title="Read a new picture from every camera on screen">↻ New pictures</button>
        <button type="button" class="cv-btn" data-act="keep" title="Read new pictures every 30 s for 5 minutes">▶ Keep refreshing</button>
      </div>
      <div class="cv-body"></div>
    </div>`;
    const root = el.querySelector(".cv"), body = root.querySelector(".cv-body");
    const qIn = root.querySelector(".cv-q");

    const groups = () => (opts.groups() || []).filter(Boolean);
    const camsOf = (g) => ((g.data && g.data.cameras) || []);
    const visible = (g, c) => {
      if (st.nvr && !(c.nvr && c.nvr.key === st.nvr)) return false;
      if (st.filt === "look" && !needsLook(c)) return false;
      if (st.filt === "nvr" && c.route !== "nvr") return false;
      if (st.filt === "own" && !ownLogin(c.route)) return false;
      if (!st.q) return true;
      const hay = [c.name, c.ip, c.model, c.mac, c.nvr && c.nvr.name, g.title].join(" ").toLowerCase();
      return st.q.split(/\s+/).every((w) => hay.includes(w));
    };

    function chips() {
      const all = groups().flatMap((g) => camsOf(g).filter((c) => !st.nvr || (c.nvr && c.nvr.key === st.nvr)));
      const n = { all: all.length, look: all.filter(needsLook).length, nvr: all.filter((c) => c.route === "nvr").length, own: all.filter((c) => ownLogin(c.route)).length };
      const lbl = { all: "All", look: "Need a look", own: "Picture via own login", nvr: "Picture via recorder" };
      root.querySelector(".cv-chips").innerHTML = Object.keys(lbl).map((k) =>
        `<button type="button" class="cv-chip ${st.filt === k ? "cv-on" : ""}" data-filt="${k}" aria-pressed="${st.filt === k}">${lbl[k]} <span>${n[k]}</span></button>`).join("")
        + (st.nvr ? `<button type="button" class="cv-chip cv-on" data-clear-nvr title="Show every recorder">Recorder only ✕</button>` : "");
      root.querySelectorAll(".cv-seg button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.size === st.size)));
    }

    function tileHtml(g, c) {
      const k = g.id + "|" + c.key, p = st.pics.get(k);
      const vs = viewsOf(c), box = TILE / vs.length;
      const fits = vs.map((n) => {
        const pv = st.pics.get(vk(k, n));
        return `<div class="cv-fit" data-v="${n}" style="${fitStyle(viewAspect(c, n, pv, n === 1 ? st.learned.get(k) : 0), box)}"><img alt="" hidden></div>`;
      });
      const inner = vs.length > 1 ? `<div class="cv-duo">${fits.map((f) => `<div class="cv-half">${f}</div>`).join("")}</div>` : fits[0];
      const h = worst(c.health);
      const why = emptyChan(c) ? "chan_offline" : c.why;
      const badges = (why && WHY[why] ? `<span class="cv-badge ${why === "offline" ? "cv-bad" : "cv-warn"}">${esc(WHY[why])}</span>` : "")
        + (h && h.sev !== "info" ? `<span class="cv-badge ${SEV[h.sev]}" title="${esc(h.text)}">${esc(h.short || h.text)}</span>` : "")
        + (vs.length > 1 ? `<span class="cv-badge cv-dim" title="${esc(c.views.map((v) => v.name).join(" + "))}">${vs.length} lenses</span>` : "")
        + (isPtz(c) ? `<span class="cv-badge cv-via" title="Pan, tilt and zoom — steer it on its camera page">PTZ</span>` : "")
        + (c.route === "nvr" ? `<span class="cv-badge cv-via" title="${esc("Through " + ((c.nvr && c.nvr.name) || "its recorder") + (c.nvr ? ", channel " + c.nvr.channel : ""))}">NVR</span>` : "");
      const ph = c.route || c.picture ? "Loading…" : emptyChan(c) ? "No camera on this channel" : esc(c.why_text || "No picture");
      return `<article class="cv-tile ${c.online ? "" : "cv-off"}" data-k="${esc(k)}" tabindex="0" role="button" aria-label="${esc(c.name)}">
        <div class="cv-pic cv-frame">${inner}<div class="cv-ph">${ICON}<span>${ph}</span></div><span class="cv-age" hidden></span></div>
        <div class="cv-cap"><b title="${esc(c.name)}">${esc(c.name)}</b><small>${esc([c.ip, c.model].filter(Boolean).join(" · ") || "—")}</small>${badges ? `<span class="cv-badges">${badges}</span>` : ""}</div>
      </article>`;
    }

    function recHtml(g, r) {
      const bad = (r.health || []).some((h) => h.sev === "crit");
      const s = !r.online ? ["cv-bad", "Offline"] : r.login_refused ? ["cv-warn", "Login refused"] : !r.has_login ? ["cv-warn", "No login saved"]
        : r.channels ? [bad ? "cv-bad" : r.channels_offline ? "cv-warn" : "cv-ok", `${r.channels_online} of ${r.channels} cameras connected`] : ["cv-dim", "Reading its cameras…"];
      const disks = r.disks ? (r.disks.length ? `${r.disks.length} disk${r.disks.length > 1 ? "s" : ""}${r.disk_gb ? " · " + (r.disk_gb >= 1000 ? (r.disk_gb / 1000).toFixed(1) + " TB" : r.disk_gb + " GB") : ""}` : "no disk") : "";
      const foot = r.footage_min_days != null ? `≈${Math.floor(r.footage_min_days)} days footage` : "";
      const h = worst(r.health);
      const line = [foot, disks].filter(Boolean).join(" · ");
      return `<button type="button" class="cv-rec ${st.nvr === r.key ? "cv-on" : ""}" data-nvr="${esc(r.key)}" title="Open this recorder: its health, channels, recording, alarms and set-up">
        <span class="cv-dot ${s[0]}"></span><span class="cv-rtx"><b>${esc(r.name)}</b><small>${esc([r.model && r.model !== r.name ? r.model : "", r.ip].filter(Boolean).join(" · "))}</small><span class="cv-rst ${s[0]}">${esc(s[1])}</span>${line ? `<small>${esc(line)}</small>` : ""}${h && h.sev !== "info" ? `<span class="cv-rst ${SEV[h.sev]}">${esc(h.short || h.text)}${(r.health || []).filter((x) => x.sev !== "info").length > 1 ? " +" + ((r.health || []).filter((x) => x.sev !== "info").length - 1) : ""}</span>` : ""}</span></button>`;
    }

    // The recorder console (2026-11-07): an overview that says what needs a look, each with
    // a button that opens the tab where it is fixed; the tabs themselves are built in
    // render() (they keep their state across the grid's re-renders).
    const REC_FIX = { clock: "time", tz: "time", lost: "channels", rec: "recording", disk: "disks", footage: "disks", pw: "logins" };
    const REC_FIXLABEL = { time: "Set the clock", channels: "Tidy the channels", recording: "Check the recording", disks: "Look at the disks", logins: "Logins" };
    function recPanel(r) {
      const sz = (gb) => (gb >= 1000 ? (gb / 1000).toFixed(1) + " TB" : gb + " GB");
      const diskBad = (r.disks || []).some((d) => !["ok", "idle", "sleep", "sleeping", "standby", "normal"].includes(d.status));
      const kpi = (label, val, sub, cls) => `<div class="cv-kpi ${cls || ""}"><span>${esc(label)}</span><b>${val}</b>${sub ? `<small>${sub}</small>` : ""}</div>`;
      const needs = (r.health || []).filter((h) => h.sev !== "info");
      const status = !r.online ? ["cv-bad", "Offline"] : r.login_refused ? ["cv-warn", "Login refused"] : !r.has_login ? ["cv-warn", "No login saved"]
        : needs.some((h) => h.sev === "crit") ? ["cv-bad", "Needs a look"] : needs.length ? ["cv-warn", "Needs a look"] : ["cv-ok", "All good"];
      return `<section class="cv-rc">
        <div class="cv-rchead"><div><h3>${esc(r.name)}</h3><p class="cv-dimtx">${esc([r.model && r.model !== r.name ? r.model : "", r.firmware, r.ip].filter(Boolean).join(" · "))}${r.health_ts ? ` · checked ${esc(ago(r.health_ts))}` : ""}</p></div>
          <span class="cv-badge ${status[0]}">${status[1]}</span></div>
        <div class="cv-kpis">
          ${kpi("Cameras", r.channels ? `${r.channels_online} <i>of ${r.channels}</i>` : "—", r.channels ? (r.channels_offline ? `${r.channels_offline} not connected` : "all connected") : "reading…", r.channels_offline ? "cv-kwarn" : "")}
          ${kpi("Recording", r.recording_off ? `${r.recording_off} <i>off</i>` : r.channels ? "All" : "—", r.recording_off ? "channels not recording" : "channels recording", r.recording_off ? "cv-kbad" : "")}
          ${kpi("Footage", r.footage_min_days != null ? `≈${Math.floor(r.footage_min_days)} <i>days</i>` : "—", r.footage_min_days != null ? "shortest kept" : "read once a day", "")}
          ${kpi("Disks", r.disks ? (r.disks.length ? `${r.disks.length} · ${sz(r.disk_gb || 0)}` : "None") : "—", r.disks ? (r.disks.length ? (diskBad ? "a disk needs a look" : "all healthy") : "nothing is recorded") : "", diskBad || (r.disks && !r.disks.length) ? "cv-kbad" : "")}
        </div>
        ${needs.length ? `<ul class="cv-needs">${needs.map((h) => { const t = REC_FIX[h.id]; return `<li class="cv-h ${SEV[h.sev] || ""}"><div><b>${esc(h.text)}</b>${h.fix ? `<small>${esc(h.fix)}</small>` : ""}</div>${t && r.has_login && opts.apiBase ? `<button type="button" class="cv-btn" data-rtab="${t}">${esc(REC_FIXLABEL[t])} →</button>` : ""}</li>`; }).join("")}</ul>`
          : `<p class="cv-dimtx">${r.health_ts ? "Nothing needs a look." : "Health not read yet — it is read every 15 minutes."}</p>`}
        ${r.has_login ? "" : `<p class="cv-setmsg cv-warn">Save this recorder's admin login (Logins at the top of the page) to manage it from here.</p>`}
      </section>`;
    }

    function groupHtml(g) {
      const d = g.data;
      const head = opts.fleet ? `<h2 class="cv-gh">${esc(g.title)}${g.href ? ` <a href="${esc(g.href)}">open site</a>` : ""}</h2>` : "";
      if (!d || d.__loading) return `<section class="cv-group">${head}<p class="cv-msg">Loading cameras…</p></section>`;
      if (d.__error) return `<section class="cv-group">${head}<p class="cv-msg cv-warn">${esc(d.__error)}</p></section>`;
      if (d.enabled === false) return `<section class="cv-group">${head}<p class="cv-msg">Camera pictures are turned off on this site (its Settings → Cameras).</p></section>`;
      const shown = camsOf(g).filter((c) => visible(g, c));
      const cams = shown.filter((c) => !emptyChan(c)), empties = shown.filter(emptyChan);
      const byRec = [...new Set(empties.map((c) => (c.nvr && c.nvr.name) || "a recorder"))].join(", ");
      const emptyHtml = empties.length ? `<details class="cv-empty"><summary>${empties.length} empty recorder channel${empties.length > 1 ? "s" : ""} <span class="cv-dimtx">on ${esc(byRec)} — set up on the recorder, but no camera is connected. Not counted as cameras offline.</span></summary>
          <div class="cv-grid cv-${esc(st.size)}">${empties.map((c) => tileHtml(g, c)).join("")}</div></details>` : "";
      const recs = (d.recorders || []);
      // Say when a filter hides cameras, and how many (e.g. a recorder's 19, of which 12 come via it).
      const scope = camsOf(g).filter((c) => !st.nvr || (c.nvr && c.nvr.key === st.nvr));
      const rec = st.nvr && recs.find((r) => r.key === st.nvr);
      const on = [rec ? `recorder ${rec.name}` : "", st.filt !== "all" ? { look: "Need a look", own: "Picture via own login", nvr: "Picture via recorder" }[st.filt] : "", st.q ? `search "${st.q}"` : ""].filter(Boolean);
      const note = on.length ? `<p class="cv-shown">Showing ${cams.length} of ${st.filt !== "all" || st.q ? scope.length : camsOf(g).length} cameras — ${esc(on.join(" · "))} <button type="button" class="cv-linkb" data-show-all>Show all</button></p>` : "";
      return `<section class="cv-group">${head}
        ${opts.stdBase && !opts.fleet && !st.nvr ? `<div data-std="${esc(g.id)}"></div>` : ""}
        ${recs.length ? `<div class="cv-recs">${recs.map((r) => recHtml(g, r)).join("")}</div>` : ""}
        ${rec ? recPanel(rec) : ""}
        ${rec && opts.apiBase && rec.has_login ? `<div data-recset="${esc(rec.key)}" data-g="${esc(g.id)}"></div>` : ""}
        ${note}
        ${cams.length ? `<div class="cv-grid cv-${esc(st.size)}">${cams.map((c) => tileHtml(g, c)).join("")}</div>`
          : `<p class="cv-msg">${camsOf(g).length ? "No camera matches." : "Netwatch has not found any cameras here yet."}</p>`}
        ${emptyHtml}
      </section>`;
    }

    function paint(tile) {
      const k = tile.dataset.k, age = tile.querySelector(".cv-age"), ph = tile.querySelector(".cv-ph");
      let any = null, err = "";
      tile.querySelectorAll(".cv-fit").forEach((fit) => {
        const p = st.pics.get(vk(k, Number(fit.dataset.v))), img = fit.querySelector("img");
        if (p && p.src) {
          if (img.getAttribute("src") !== p.src) img.src = p.src;
          img.hidden = false;
          if (!any || fit.dataset.v === "1") any = p;
        } else if (p && p.err && !err) err = p.err;
      });
      if (any) {
        ph.hidden = true; age.hidden = false;
        age.textContent = (any.stale ? "Saved · " : "") + ago(any.ts);
        tile.classList.toggle("cv-stale", !!any.stale);
      } else if (err) {
        ph.hidden = false; age.hidden = true;
        ph.querySelector("span").textContent = err;
      }
    }

    function render() {
      if (st.dead) return;
      chips();
      body.innerHTML = groups().map(groupHtml).join("");
      body.querySelectorAll(".cv-tile").forEach((t) => { paint(t); st.obs.observe(t); });
      body.querySelectorAll("[data-std]").forEach((ph) => {
        const g = groups().find((x) => String(x.id) === ph.dataset.std); if (!g) return;
        if (!st.std) { const host = document.createElement("section"); host.className = "cv-set cv-stdhost"; st.std = { host, ctl: standardPanel(host, opts.stdBase(g)) }; }
        ph.replaceWith(st.std.host);
      });
      body.querySelectorAll("[data-recset]").forEach((ph) => {
        const id = ph.dataset.g + "|" + ph.dataset.recset;
        let p = st.recSet.get(id);
        if (!p) {
          const g = groups().find((x) => String(x.id) === ph.dataset.g); if (!g) return;
          const rk = ph.dataset.recset, dev = { key: rk, kind: "recorder" }, base = opts.apiBase(g, dev);
          const rec = ((g.data && g.data.recorders) || []).find((x) => x.key === rk) || {};
          const TABS = [["channels", "📹 Channels"], ["recording", "⏺ Recording"], ["alarms", "🔔 Alarms"], ["disks", "💽 Disks"],
            ["time", "🕒 Time"], ["logins", "🔐 Logins"], ["maint", "🛠 Maintenance"]];
          const host = document.createElement("div"); host.className = "cv-rtabs";
          host.innerHTML = `<div class="cv-tabbar" role="tablist" aria-label="Recorder">${TABS.map(([t, l]) => `<button type="button" role="tab" data-rt="${t}" aria-selected="false">${l}</button>`).join("")}</div>
            ${TABS.map(([t]) => `<div class="cv-tabpane" role="tabpanel" data-rp="${t}" hidden></div>`).join("")}`;
          const made = {}, ctls = [];
          const open = (sec) => { const d = sec && sec.querySelector("details"); if (d && !d.open) d.open = true; };
          const pane = (t) => host.querySelector(`[data-rp="${t}"]`);
          const add = (t) => { const sec = document.createElement("section"); sec.className = "cv-set"; pane(t).appendChild(sec); return sec; };
          const build = {
            channels: () => { const a = add("channels"); ctls.push(channelsPanel(a, base)); open(a); },
            recording: () => { ctls.push(recordingPanel(add("recording"), base, opts.stdBase ? opts.stdBase(g) : null, rec)); },
            alarms: () => { ctls.push(alarmsPanel(add("alarms"), base)); },
            disks: () => { const a = add("disks"); ctls.push(disksPanel(a, base + "/disks", true)); open(a); },
            time: () => { const a = add("time"); ctls.push(settingsPanel(a, base, dev, () => {}, { group: "clock", title: "🕒 The recorder's clock", sub: "time zone · time from the internet" })); open(a); },
            logins: () => { const a = add("logins"); ctls.push(settingsPanel(a, base, dev, () => {}, { group: "security", title: "🔐 Logins and security", sub: "users · ports · Hik-Connect · the Netwatch account" })); open(a); },
            maint: () => {
              const a = add("maint"); ctls.push(settingsPanel(a, base, dev, () => {}, { group: "recorder", title: "⚙ Name and disks", sub: "the recorder's name · what it does when its disks are full" })); open(a);
              ctls.push(restartPanel(add("maint"), base, rec.name || "the recorder"));
              if (opts.fwBase) ctls.push(firmwarePanel(add("maint"), opts.fwBase(g, dev), opts.fwLib));
              if (opts.backupBase) ctls.push(backupsPanel(add("maint"), opts.backupBase(g, dev)));
              ctls.push(explorerPanel(add("maint"), base));
            },
          };
          const show = (t) => {
            if (!build[t]) t = "channels";
            if (!made[t]) { made[t] = true; build[t](); }
            host.querySelectorAll("[data-rt]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.rt === t)));
            host.querySelectorAll("[data-rp]").forEach((x) => { x.hidden = x.dataset.rp !== t; });
            mem.set(pre + ".rtab", t);
          };
          host.addEventListener("click", (e) => { const b = e.target.closest("[data-rt]"); if (b) show(b.dataset.rt); });
          host.addEventListener("keydown", (e) => {
            const b = e.target.closest("[data-rt]"); if (!b || !["ArrowLeft", "ArrowRight"].includes(e.key)) return;
            const all = [...host.querySelectorAll("[data-rt]")], i = all.indexOf(b), n = all[(i + (e.key === "ArrowRight" ? 1 : all.length - 1)) % all.length];
            n.focus(); show(n.dataset.rt);
          });
          show(mem.get(pre + ".rtab", "channels"));
          p = { host, show, ctl: { destroy() { ctls.forEach((c) => c && c.destroy && c.destroy()); } } };
          st.recSet.set(id, p);
        }
        ph.replaceWith(p.host);
      });
    }

    function find(k) {
      const i = k.indexOf("|"), gid = k.slice(0, i), key = k.slice(i + 1);
      const g = groups().find((x) => String(x.id) === gid);
      const c = g && camsOf(g).find((x) => x.key === key);
      return c ? { g, c } : null;
    }

    function load(k, fresh) {
      const hit = find(k);
      if (!hit || st.dead) return;
      viewsOf(hit.c).forEach((n) => loadView(hit, k, n, fresh));
    }

    function loadView({ g, c }, tk, n, fresh) {
      const k = vk(tk, n), cur = st.pics.get(k);
      if (cur && cur.loading) return;
      if (!c.route && !c.picture && !(cur && cur.src)) return;
      st.pics.set(k, { ...(cur || {}), loading: true });
      queue(g.id, () => fetchPic(opts.picUrl(g, c, { size: "thumb", fresh, view: n })))
        .then((p) => {
          if (st.dead) { URL.revokeObjectURL(p.src); return; }
          const old = st.pics.get(k);
          if (old && old.src) setTimeout(() => URL.revokeObjectURL(old.src), 1000);
          st.pics.set(k, { ...p, loading: false, at: Date.now() });
          const t = body.querySelector(`.cv-tile[data-k="${CSS.escape(tk)}"]`);
          if (t) {
            const fit = t.querySelector(`.cv-fit[data-v="${n}"]`), box = TILE / viewsOf(c).length;
            if (fit && p.w && p.h && (n > 1 || (!c.aspect && ownLogin(p.route)))) fit.setAttribute("style", fitStyle(p.w / p.h, box));
            paint(t);
          }
        })
        .catch((e) => {
          const old = st.pics.get(k) || {};
          st.pics.set(k, { ...old, loading: false, at: Date.now(), err: old.src ? "" : (WHY[e.why] || e.message || "No picture") });
          const t = body.querySelector(`.cv-tile[data-k="${CSS.escape(tk)}"]`);
          if (t && !old.src) paint(t);
        });
    }

    // a tile on screen gets its picture; one the site has a newer picture for gets that one
    st.obs = new IntersectionObserver((ents) => ents.forEach((en) => {
      if (!en.isIntersecting) return;
      const k = en.target.dataset.k, p = st.pics.get(k), hit = find(k);
      const newer = hit && hit.c.picture && p && p.ts && hit.c.picture.ts > p.ts + 1;
      if (!p || newer || (!p.src && !p.loading && Date.now() - (p.at || 0) > 60000)) load(k, false);
    }), { rootMargin: "200px" });

    function onScreen() {
      const vh = window.innerHeight || 800;
      return [...body.querySelectorAll(".cv-tile")].filter((t) => { const r = t.getBoundingClientRect(); return r.bottom > -100 && r.top < vh + 100; });
    }
    function freshAll() { onScreen().forEach((t) => load(t.dataset.k, true)); }
    function keepBtn() {
      const b = root.querySelector('[data-act="keep"]'), left = st.keepUntil - Date.now();
      b.classList.toggle("cv-on", left > 0);
      b.textContent = left > 0 ? `■ Stop (${Math.ceil(left / 60000)} min)` : "▶ Keep refreshing";
    }

    root.addEventListener("click", (e) => {
      const f = e.target.closest("[data-filt]");
      if (f) { st.filt = f.dataset.filt; render(); return; }
      if (e.target.closest("[data-show-all]")) { st.filt = "all"; st.nvr = ""; st.q = ""; qIn.value = ""; render(); return; }
      if (e.target.closest("[data-clear-nvr]")) { st.nvr = ""; render(); return; }
      const sz = e.target.closest("[data-size]");
      if (sz) { st.size = sz.dataset.size; mem.set(pre + ".size", st.size); render(); return; }
      const rt = e.target.closest("[data-rtab]");
      if (rt) {
        const p = [...st.recSet.entries()].find(([k]) => k.endsWith("|" + st.nvr));
        if (p) { p[1].show(rt.dataset.rtab); p[1].host.scrollIntoView({ behavior: "smooth", block: "start" }); }
        return;
      }
      const nv = e.target.closest("[data-nvr]");
      if (nv) { st.nvr = st.nvr === nv.dataset.nvr ? "" : nv.dataset.nvr; render(); return; }
      const a = e.target.closest("[data-act]");
      if (a && a.dataset.act === "fresh") { freshAll(); return; }
      if (a && a.dataset.act === "keep") {
        st.keepUntil = st.keepUntil > Date.now() ? 0 : Date.now() + 5 * 60000;
        if (st.keepUntil) freshAll();
        keepBtn(); return;
      }
      const t = e.target.closest(".cv-tile");
      if (t) { const hit = find(t.dataset.k); if (hit && opts.onOpen) opts.onOpen(hit.g, hit.c); }
    });
    root.addEventListener("keydown", (e) => {
      const t = e.target.closest(".cv-tile");
      if (t && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); t.click(); }
    });
    let qt = null;
    qIn.addEventListener("input", () => { clearTimeout(qt); qt = setTimeout(() => { st.q = qIn.value.trim().toLowerCase(); render(); }, 150); });

    st.timers.push(setInterval(() => {            // ages, and the 5-minute refresh
      body.querySelectorAll(".cv-tile").forEach((t) => paint(t));
      if (st.keepUntil && Date.now() > st.keepUntil) st.keepUntil = 0;
      keepBtn();
    }, 15000));
    st.timers.push(setInterval(() => { if (st.keepUntil > Date.now()) freshAll(); }, 30000));

    render();
    return {
      update: render,
      destroy() {
        st.dead = true;
        st.timers.forEach(clearInterval);
        st.recSet.forEach((p) => p.ctl.destroy());
        if (st.std) st.std.ctl.destroy();
        if (st.obs) st.obs.disconnect();
        st.pics.forEach((p) => p.src && URL.revokeObjectURL(p.src));
        st.pics.clear();
        el.innerHTML = "";
      },
    };
  }

  // ---- one camera ---------------------------------------------------------------------------
  function camera(el, opts) {
    const g = opts.group, c = opts.cam;
    const vs = viewsOf(c), names = Object.fromEntries((c.views || []).map((v) => [v.n, v.name]));
    const st = { pics: {}, full: false, until: 0, timer: null, dead: false };
    const rec = c.nvr ? `${esc(c.nvr.name || c.nvr.ip)} · channel ${esc(c.nvr.channel)}${vs.length > 1 && c.views ? "" : ""}${c.nvr.chan_online === false ? " · <span class='cv-warn'>not connected</span>" : ""}` : "—";
    const status = c.online ? `<span class="cv-badge cv-ok">Online</span>` : `<span class="cv-badge cv-bad">Offline</span>`;
    const health = (c.health || []).map((h) => `<li class="cv-h ${SEV[h.sev] || ""}"><b>${esc(h.text)}</b>${h.fix ? `<small>${esc(h.fix)}</small>` : ""}</li>`).join("");
    const facts = [
      c.recording === true ? ["Recording", "Yes"] : c.recording === false ? ["Recording", "<span class='cv-warn'>No</span>"] : null,
      c.footage_days != null ? ["Footage kept", `${esc(c.footage_days)} days`] : null,
    ].filter(Boolean).map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
    el.innerHTML = `<div class="cv cv-one">
      <div class="cv-ohead">
        <button type="button" class="cv-btn" data-act="back">← All cameras</button>
        <div class="cv-otitle"><h2>${esc(c.name)}</h2><p>${status}${c.why && c.why !== "offline" && WHY[c.why] ? ` <span class="cv-badge cv-warn">${esc(WHY[c.why])}</span>` : ""}${c.route === "nvr" ? ` <span class="cv-badge cv-via">NVR</span>` : ""}${vs.length > 1 ? ` <span class="cv-badge cv-dim">${vs.length} lenses</span>` : ""}${g.title ? ` <span class="cv-dimtx">${esc(g.title)}</span>` : ""}</p></div>
      </div>
      <div class="cv-ogrid">
        <div class="cv-stage">
          <div class="cv-views cv-n${vs.length}">${vs.map((n) => `<figure class="cv-view" data-v="${n}">
            <div class="cv-pic cv-big" style="aspect-ratio:${viewAspect(c, n, null, 0).toFixed(4)}"><img alt="${esc(c.name)}" hidden><div class="cv-ph">${ICON}<span>${c.route || c.picture ? "Loading…" : esc(c.why_text || "No picture")}</span></div><span class="cv-age" hidden></span></div>
            ${vs.length > 1 ? `<figcaption>${esc(names[n] || "Lens " + n)} · <a href="${esc(opts.picUrl(g, c, { size: "full", view: n }))}" target="_blank" rel="noopener">full size ↗</a></figcaption>` : ""}
          </figure>`).join("")}</div>
          <div class="cv-acts">
            <button type="button" class="cv-btn" data-act="fresh">↻ New picture</button>
            ${opts.apiBase && c.kind === "camera" && c.route && (c.has_login || c.nvr) ? `<button type="button" class="cv-btn" data-act="video" title="Real live video from the camera's sub-stream, for up to 5 minutes">🎥 Live video</button>` : ""}
            <button type="button" class="cv-btn" data-act="live">▶ Keep refreshing (2 min)</button>
            <button type="button" class="cv-btn" data-act="full">🔍 Sharper picture</button>
            ${vs.length > 1 ? "" : `<a class="cv-btn" data-act="open" href="${esc(opts.picUrl(g, c, { size: "full" }))}" target="_blank" rel="noopener">Full size ↗</a>`}
            ${opts.deviceAction ? `<button type="button" class="cv-btn" data-act="device">${esc(opts.deviceAction.label)}</button>` : ""}
          </div>
          <p class="cv-note" aria-live="polite"></p>
        </div>
        <div class="cv-side">
          ${health ? `<section class="cv-health"><h3>Health</h3><ul>${health}</ul></section>` : ""}
          <dl class="cv-facts">
            ${c.health_ts ? `<dt>Health checked</dt><dd>${esc(ago(c.health_ts))}</dd>` : ""}
            <dt>IP address</dt><dd class="cv-mono">${esc(c.ip || "—")}</dd>
            ${c.mac ? `<dt>MAC</dt><dd class="cv-mono">${esc(c.mac)}</dd>` : ""}
            <dt>Model</dt><dd>${esc(c.model || "—")}</dd>
            ${c.firmware ? `<dt>Firmware</dt><dd class="cv-mono">${esc(c.firmware)}</dd>` : ""}
            <dt>Recorder</dt><dd>${rec}</dd>
            ${facts}
            <dt>Picture from</dt><dd data-f="route">${c.route ? esc(ROUTE[c.route] || c.route) : esc(c.why_text || "—")}</dd>
            <dt>Picture</dt><dd data-f="pic">—</dd>
            ${c.last_seen && !c.online ? `<dt>Last seen</dt><dd>${esc(ago(c.last_seen))}</dd>` : ""}
            ${c.why_text && c.why !== "offline" ? `<dt>Note</dt><dd>${esc(c.why_text)}${c.fail ? ` <span class="cv-dimtx">(${esc(c.fail)})</span>` : ""}</dd>` : ""}
          </dl>
        </div>
      </div>
      ${opts.apiBase && c.kind === "camera" ? `<section class="cv-set" data-ptz hidden></section><section class="cv-set" data-set></section><section class="cv-set" data-det></section>${c.nvr ? `<section class="cv-set" data-rec></section>` : ""}${c.has_login ? `<section class="cv-set" data-secu></section><section class="cv-set" data-isapi></section><section class="cv-set" data-disk></section>${opts.backupBase ? `<section class="cv-set" data-bak></section>` : ""}${opts.fwBase ? `<section class="cv-set" data-fw></section>` : ""}` : ""}${opts.stdBase && similarOf(g, c).length ? `<section class="cv-set" data-copy></section>` : ""}` : ""}
      <p class="cv-foot">Pictures are kept in memory only, apart from one saved copy per camera so an offline camera still shows what it looks at.</p>
    </div>`;
    const root = el.querySelector(".cv"), note = root.querySelector(".cv-note");
    const boxOf = (n) => root.querySelector(`.cv-view[data-v="${n}"] .cv-pic`);

    function show(n, p) {
      const old = st.pics[n];
      if (old && old.src && old.src !== p.src) setTimeout(() => URL.revokeObjectURL(old.src), 1000);
      st.pics[n] = p;
      const box = boxOf(n), img = box.querySelector("img");
      img.src = p.src; img.hidden = false; box.querySelector(".cv-ph").hidden = true;
      if (p.w && p.h && (n > 1 || (!c.aspect && (ownLogin(p.route) || st.full)))) box.style.aspectRatio = (p.w / p.h).toFixed(4);
      tick();
    }
    function tick() {
      vs.forEach((n) => {
        const p = st.pics[n]; if (!p) return;
        const age = boxOf(n).querySelector(".cv-age");
        age.hidden = false; age.textContent = (p.stale ? "Saved · " : "") + ago(p.ts);
      });
      const p = st.pics[1];
      if (p) root.querySelector('[data-f="pic"]').textContent = [p.w && p.h ? `${p.w}×${p.h}` : "", p.bytes ? Math.round(p.bytes / 1024) + " KB" : "",
        ago(p.ts), p.stale ? "saved copy" : "", ROUTE[p.route] ? "from " + ROUTE[p.route] : ""].filter(Boolean).join(" · ");
      const lv = root.querySelector('[data-act="live"]'), left = st.until - Date.now();
      lv.classList.toggle("cv-on", left > 0);
      lv.textContent = left > 0 ? `■ Stop (${Math.ceil(left / 1000)} s)` : "▶ Keep refreshing (2 min)";
    }
    async function getView(n, size, fresh) {
      try {
        const p = await queue(g.id, () => fetchPic(opts.picUrl(g, c, { size, fresh, view: n })));
        if (st.dead) { URL.revokeObjectURL(p.src); return; }
        if (n === 1) note.textContent = p.stale ? (c.online ? "The camera did not answer — this is its last saved picture." : "The camera is offline — this is its last saved picture.") : "";
        show(n, p);
      } catch (e) {
        if (st.dead) return;
        const msg = WHY[e.why] ? WHY[e.why] + (e.message ? " — " + e.message : "") : e.message || "No picture";
        if (n === 1) note.textContent = msg;
        if (!st.pics[n]) boxOf(n).querySelector(".cv-ph span").textContent = msg;
      }
    }
    const get = (size, fresh) => Promise.all(vs.map((n) => getView(n, size, fresh)));
    function stopLive() { clearTimeout(st.timer); st.timer = null; st.until = 0; tick(); }
    function loop() {
      if (st.dead || Date.now() > st.until) return stopLive();
      get("thumb", true).finally(() => { if (st.until && !st.dead) st.timer = setTimeout(loop, 2000); });
    }

    // ---- live video (Phase 7: app/camlive.py through the site, or the hub) ----
    const vid = { el: null, timer: null, ends: 0, busy: false };
    function tickVid() {
      const b = root.querySelector('[data-act="video"]'); if (!b) return;
      b.classList.toggle("cv-on", !!vid.el);
      b.textContent = vid.el ? `■ Stop video (${Math.max(0, Math.ceil((vid.ends - Date.now()) / 1000))} s)` : "🎥 Live video";
    }
    function stopVideo(msg) {
      const v = vid.el; vid.el = null;
      if (v) { v.pause(); v.removeAttribute("src"); v.load(); v.remove(); }
      clearInterval(vid.timer); vid.timer = null; tickVid();
      if (msg) note.textContent = msg;
    }
    async function startVideo() {
      if (vid.busy) return;
      vid.busy = true; note.textContent = "Starting live video…";
      const base = opts.apiBase(g, c);
      let info;
      try { info = await jcall(`${base}/live?view=1`); } catch (err) { vid.busy = false; note.textContent = err.message; return; }
      vid.busy = false;
      if (st.dead) return;
      const h265 = /265|hevc/i.test(info.codec || ""), probe = document.createElement("video");
      if (h265 && !probe.canPlayType('video/mp4; codecs="hvc1.1.6.L93.B0"') && !probe.canPlayType('video/mp4; codecs="hev1.1.6.L93.B0"')) {
        note.textContent = "This camera sends its live stream as H.265, which this browser can't play. Open it in Chrome, Edge or Safari, or set the camera's Sub stream to H.264 (Picture and streams).";
        return;
      }
      stopLive();
      const v = document.createElement("video");
      v.className = "cv-live"; v.autoplay = true; v.muted = true; v.playsInline = true; v.controls = true;
      v.setAttribute("playsinline", ""); v.disablePictureInPicture = true;
      v.src = `${base}/live.mp4?view=1`;
      v.addEventListener("playing", () => { if (vid.el === v) note.textContent = `Live · ${[info.size, info.codec].filter(Boolean).join(" · ")} · stops by itself after ${Math.round(info.max_s / 60)} minutes`; });
      v.addEventListener("error", () => { if (vid.el === v) stopVideo(h265 ? "This browser could not play the camera's H.265 video. Try Chrome, Edge or Safari, or set the Sub stream to H.264." : "The live video stopped (the camera or the link to the site)."); });
      v.addEventListener("ended", () => { if (vid.el === v) stopVideo("Live view ended — press Live video to watch again."); });
      boxOf(1).appendChild(v);
      vid.el = v; vid.ends = Date.now() + (info.max_s || 300) * 1000;
      vid.timer = setInterval(() => { tickVid(); if (vid.el && Date.now() > vid.ends + 15000) stopVideo("Live view ended — press Live video to watch again."); }, 1000);
      tickVid();
    }

    root.addEventListener("click", (e) => {
      const a = e.target.closest("[data-act]");
      if (!a) return;
      const act = a.dataset.act;
      if (act === "video") { if (vid.el) stopVideo(""); else startVideo(); }
      if ((act === "live" || act === "full" || act === "back") && vid.el) stopVideo("");
      if (act === "back" && opts.onBack) opts.onBack();
      if (act === "fresh") { st.full = false; get("thumb", true); }
      if (act === "full") { st.full = true; stopLive(); note.textContent = "Reading the sharper picture…"; get("full", true); }
      if (act === "live") {
        if (st.until > Date.now()) return stopLive();
        st.full = false; st.until = Date.now() + 120000; loop();
      }
      if (act === "device" && opts.deviceAction) opts.deviceAction.run();
    });
    const ages = setInterval(tick, 1000);
    if (c.route || c.picture) get("thumb", false);
    const setHost = root.querySelector("[data-set]"), detHost = root.querySelector("[data-det]");
    const refresh = () => { if (!st.dead) setTimeout(() => get("thumb", true), 1000); };
    const panel = setHost ? settingsPanel(setHost, opts.apiBase(g, c), c, refresh) : null;
    const dpanel = detHost ? settingsPanel(detHost, opts.apiBase(g, c), c, refresh, {
      group: "detect", title: "🚨 Detection and alarms",
      sub: "motion · intrusion · line crossing · tampering · privacy masks · strobe and siren",
      pic: (n) => (st.pics[n] || st.pics[1] || {}).src || "" }) : null;
    const recHost = root.querySelector("[data-rec]");
    const rpanel = recHost ? settingsPanel(recHost, opts.apiBase(g, c), c, () => {}, {
      group: "record", title: "⏺ Recording", sub: "when the recorder records it · before and after an event · stream · days kept" }) : null;
    const secHost = root.querySelector("[data-secu]");
    const spanel = secHost ? settingsPanel(secHost, opts.apiBase(g, c), c, () => {}, {
      group: "security", title: "🔐 Logins and security", sub: "users · ports · Hik-Connect · the Netwatch account" }) : null;
    const xHost = root.querySelector("[data-isapi]");
    const xpanel = xHost ? explorerPanel(xHost, opts.apiBase(g, c)) : null;
    const bkHost = root.querySelector("[data-bak]");
    const bkpanel = bkHost ? backupsPanel(bkHost, opts.backupBase(g, c)) : null;
    const dkHost = root.querySelector("[data-disk]");
    const dkpanel = dkHost ? disksPanel(dkHost, opts.apiBase(g, c) + "/disks", false) : null;
    const fwHost = root.querySelector("[data-fw]");
    const fwpanel = fwHost ? firmwarePanel(fwHost, opts.fwBase(g, c), opts.fwLib) : null;
    const cpHost = root.querySelector("[data-copy]");
    const cpanel = cpHost ? copyPanel(cpHost, opts.stdBase(g), c, similarOf(g, c), vs) : null;
    const ptzHost = root.querySelector("[data-ptz]");
    const ppanel = ptzHost ? ptzPanel(ptzHost, opts.apiBase(g, c), vs, names, { root, stage: root.querySelector(".cv-stage"), box: boxOf,
      fig: (n) => root.querySelector(`.cv-view[data-v="${n}"]`), video: root.querySelector('[data-act="video"]'), live: () => !!vid.el,
      refresh: (n) => { if (!st.dead) getView(n, st.full ? "full" : "thumb", true); } }) : null;
    return {
      destroy() {
        st.dead = true; clearInterval(ages); clearTimeout(st.timer); stopVideo("");
        if (panel) panel.destroy();
        if (dpanel) dpanel.destroy();
        if (rpanel) rpanel.destroy();
        if (spanel) spanel.destroy();
        if (xpanel) xpanel.destroy();
        if (cpanel) cpanel.destroy();
        if (bkpanel) bkpanel.destroy();
        if (fwpanel) fwpanel.destroy();
        if (dkpanel) dkpanel.destroy();
        if (ppanel) ppanel.destroy();
        Object.values(st.pics).forEach((p) => p && p.src && URL.revokeObjectURL(p.src));
        el.innerHTML = "";
      },
    };
  }

  // ---- camera settings (Phase 3: app/hiksettings.py) ------------------------------------
  // The camera's own capabilities decide every control; a setting it doesn't describe is
  // shown, not offered. Safe sections apply at once; careful ones show the change list
  // and ask to confirm. Every change is read back on the camera and can be undone.
  async function jcall(url, init) {
    const r = await fetch(url, Object.assign({ credentials: "same-origin", headers: { "Content-Type": "application/json" } }, init || {}));
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.ok === false) throw Object.assign(new Error(j.error || j.message || "HTTP " + r.status), { status: r.status, body: j });
    return j;
  }
  const RISK = { safe: "", careful: "Asks you to confirm", dangerous: "Risky: asks you to type a word to confirm" };

  // Areas, lines, mask boxes and the motion grid are drawn over the camera's picture.
  // The page works in 0..1000 both ways with the origin top-left; the Pi converts to
  // the camera's own screen. A drawing in progress (too few points) stays a draft.
  const DRAW = new Set(["area", "line", "rects", "grid"]);
  const NS = "http://www.w3.org/2000/svg";
  const pts = (v) => String(v || "").split(/\s+/).filter(Boolean).map((p) => p.split(",").map(Number));
  const rects = (v) => String(v || "").split(";").filter((x) => x.trim()).map((r) => r.split(",").map(Number));
  function gridCells(hex, rows, cols) {
    const per = Math.ceil(cols / 8) * 2, out = [];
    for (let r = 0; r < rows; r++) {
      const bits = (hex.slice(r * per, (r + 1) * per).match(/../g) || []).map((b) => parseInt(b, 16).toString(2).padStart(8, "0")).join("");
      for (let c = 0; c < cols; c++) out.push(bits[c] === "1");
    }
    return out;
  }
  function gridHex(cells, rows, cols) {
    const per = Math.ceil(cols / 8) * 8; let hex = "";
    for (let r = 0; r < rows; r++) {
      let bits = ""; for (let c = 0; c < per; c++) bits += c < cols && cells[r * cols + c] ? "1" : "0";
      hex += bits.match(/.{8}/g).map((b) => parseInt(b, 2).toString(16).padStart(2, "0")).join("");
    }
    return hex;
  }
  // The recording week: 7 days x 24 hours, each hour one kind of recording ("-" = none).
  const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const weekRows = (v) => { const r = String(v || "").split("|"); return r.length === 7 && r.every((x) => x.length === 24) ? r : Array(7).fill("-".repeat(24)); };
  const hh = (n) => String(n).padStart(2, "0");
  function weekShown(f, v) {
    const names = Object.fromEntries((f.modes || []).map((m) => [m.v, m.label])), rows = weekRows(v);
    if (rows.every((r) => !/[^-]/.test(r))) return "Never";
    const one = (r) => { const out = []; for (let h = 0; h < 24;) { const k = r[h]; let j = h; while (j < 24 && r[j] === k) j++; if (k !== "-") out.push(`${names[k] || k} ${hh(h)}–${hh(j)}`); h = j; } return out.join(", ") || "not recorded"; };
    const out = [];
    for (let i = 0; i < 7;) { let j = i; while (j + 1 < 7 && rows[j + 1] === rows[i]) j++;
      out.push((i === 0 && j === 6 ? "Every day" : i === j ? WEEKDAYS[i] : WEEKDAYS[i] + "–" + WEEKDAYS[j]) + ": " + one(rows[i])); i = j + 1; }
    return out.join("; ");
  }
  function shownOf(f, v) {
    if (f.kind === "week") return weekShown(f, v);
    if (f.kind === "area") return v ? `${pts(v).length} points drawn` : "not drawn";
    if (f.kind === "line") return v ? "line drawn" : "not drawn";
    if (f.kind === "rects") { const n = rects(v).length; return n ? `${n} box${n > 1 ? "es" : ""}` : "none"; }
    if (f.kind === "grid") return `${gridCells(v, f.rows, f.cols).filter(Boolean).length} of ${f.rows * f.cols} squares`;
    if (f.kind === "set" || f.kind === "links") { const names = Object.fromEntries((f.opts || []).map((o) => [o.v, o.label]));
      return String(v).split(",").filter(Boolean).map((x) => names[x] || x).join(", ") || (f.kind === "set" ? "Anything that moves" : "Nothing"); }
    if (f.kind === "bool") return v === "true" ? "On" : "Off";
    const o = (f.opts || []).find((x) => x.v === v); return o ? o.label : v;
  }

  function settingsPanel(el, base, cam, onPicture, o) {
    o = o || {};
    const group = o.group || "basic";
    const st = { lens: 1, sec: group === "basic" ? "picture" : "", data: null, dead: false, edits: {}, preview: null, busy: false, draft: {}, route: "", brush: {} };
    const onCam = (group === "basic" || group === "detect");
    const devWord = () => (st.data && st.data.recorder) || !(onCam || group === "security") || (onCam && st.data.route === "nvr") ? "recorder" : "camera";
    el.innerHTML = `<details class="cv-setd"><summary><h3>${esc(o.title || "⚙ Camera settings")}</h3><span class="cv-dimtx">${esc(o.sub || "picture · text on video · time · video")}</span></summary><div class="cv-setb" data-b></div></details>`;
    const det = el.querySelector("details"), body = el.querySelector("[data-b]");
    det.addEventListener("toggle", () => { if (det.open && !st.data) load(); });

    async function load() {
      body.innerHTML = `<p class="cv-dimtx">Reading the camera's settings…</p>`;
      try {
        st.data = await jcall(`${base}/settings?lens=${st.lens}${group === "basic" ? "" : "&group=" + group}${st.route ? "&route=" + st.route : ""}`);
        if (st.dead) return;
        if ((st.data.group || "basic") !== group) { body.innerHTML = `<p class="cv-setmsg cv-warn">This site's Netwatch is too old for these settings — update it.</p>`; st.data = null; return; }
        if (group === "security" && st.data.can_change && !st.data.risky) {
          st.data.can_change = false; st.data.why_not = "Risky camera work is turned off for this site (Settings → Cameras)."; }
        if (!st.data.sections.length && !st.data.info) { body.innerHTML = `<p class="cv-dimtx">${group === "record" ? "The recorder has no recording settings for this camera." : group === "recorder" ? "This recorder has none of these settings." : "This camera has none of these settings."}</p>`; return; }
        if (!st.data.sections.some((x) => x.id === st.sec)) st.sec = (st.data.sections[0] || {}).id;
        st.edits = {}; st.draft = {}; st.preview = null; paint();
        loadChanges();
      } catch (e) {
        if (!st.dead) body.innerHTML = `<p class="cv-setmsg cv-warn">${esc(e.message)}</p>`;
      }
    }
    const sec = () => (st.data.sections || []).find((x) => x.id === st.sec);
    const valOf = (f) => (f.id in st.edits ? st.edits[f.id] : f.value);
    function visible(f, s) {
      if (!f.show_if) return true;
      const o = s.fields.find((x) => x.id === f.show_if.field);
      return !o || f.show_if.values.includes(String(valOf(o)));
    }
    function ctl(f) {
      const v = valOf(f), dis = !st.data.can_change || !f.editable ? " disabled" : "";
      if (!f.editable && !DRAW.has(f.kind)) return `<span class="cv-setro">${esc(f.shown ?? "—")}</span>`;
      if (f.kind === "bool") return `<label class="cv-sw"><input type="checkbox" data-f="${esc(f.id)}"${v === "true" ? " checked" : ""}${dis}><span>${v === "true" ? "On" : "Off"}</span></label>`;
      if (f.kind === "enum") return `<select class="cv-q cv-setin" data-f="${esc(f.id)}"${dis}>${(f.opts || []).map((o) => `<option value="${esc(o.v)}"${o.v === v ? " selected" : ""}>${esc(o.label)}</option>`).join("")}</select>`;
      if (DRAW.has(f.kind)) return drawBox(f, v, !dis);
      if (f.kind === "week") return weekBox(f, v, !dis);
      if (f.kind === "set" || f.kind === "links") {
        const on = new Set(String(v).split(",").filter(Boolean));
        return `<span class="cv-chks" data-multi="${esc(f.id)}">${(f.opts || []).map((x) => `<label class="cv-sw"><input type="checkbox" value="${esc(x.v)}"${on.has(x.v) ? " checked" : ""}${dis}><span>${esc(x.label)}</span></label>`).join("")}${(f.fixed || []).length ? `<small class="cv-dimtx">Also set on the camera: ${esc(f.fixed.join(", "))}</small>` : ""}</span>`;
      }
      if (f.kind === "int" && f.max - f.min <= 100 && f.min >= 0) return `<span class="cv-rng"><input type="range" min="${f.min}" max="${f.max}"${f.step ? ` step="${f.step}"` : ""} value="${esc(v)}" data-f="${esc(f.id)}"${dis}><output>${esc(v)}</output></span>`;
      if (f.kind === "int") return `<input class="cv-q cv-setin" type="number" min="${f.min}" max="${f.max}" value="${esc(v)}" data-f="${esc(f.id)}"${dis}>`;
      return `<input class="cv-q cv-setin" type="text" maxlength="${f.maxlen || 32}" value="${esc(v)}" data-f="${esc(f.id)}"${dis}>`;
    }
    function drawBox(f, v, can) {
      const src = o.pic ? o.pic(st.lens) : "", d = st.draft[f.id];
      const hint = f.kind === "area" ? `Tap the picture to place ${f.min === f.max ? f.min : f.min + " to " + f.max} corners${d ? ` (${d.length} placed)` : ""}; drag a corner to move it.`
        : f.kind === "line" ? `Tap where the line starts (A), then where it ends (B)${d ? ` (${d.length} placed)` : ""}; drag an end to move it.`
        : f.kind === "rects" ? `Drag across the picture to add a box (up to ${f.max}); tap a box to remove it.`
        : "Tap or drag over the squares to switch them on or off.";
      return `<div class="cv-draw${can ? " cv-dcan" : ""}" data-draw="${esc(f.id)}">${src ? `<img src="${esc(src)}" alt="">` : ""}<svg aria-label="${esc(f.label)}"></svg></div>`
        + (can ? `<div class="cv-drawbar"><small class="cv-dimtx">${esc(hint)}</small>${f.kind === "grid"
          ? `<button type="button" class="cv-btn" data-gall="1" data-fid="${esc(f.id)}">All</button><button type="button" class="cv-btn" data-gall="0" data-fid="${esc(f.id)}">None</button>`
          : `<button type="button" class="cv-btn" data-dclear="${esc(f.id)}">${f.kind === "rects" ? "Remove all" : "Start again"}</button>`}</div>`
          : `<small class="cv-dimtx">${esc(f.shown || "")}</small>`);
    }
    function weekBox(f, v, can) {
      const rows = weekRows(v), br = st.brush[f.id] || ((f.modes || [])[0] || {}).v || "C";
      const cls = (k) => "cv-wm-" + (k === "-" ? "x" : k);
      const brush = can ? `<div class="cv-wbr" role="group" aria-label="Kind of recording">${(f.modes || []).concat([{ v: "-", label: "Not recorded" }]).map((m) =>
        `<button type="button" class="cv-btn" data-wbrush="${esc(m.v)}" data-fid="${esc(f.id)}" aria-pressed="${m.v === br}"><i class="${cls(m.v)}"></i>${esc(m.label)}</button>`).join("")}</div>` : "";
      const head = `<tr><th></th>${Array.from({ length: 24 }, (_, h) => `<th>${h % 3 ? "" : hh(h)}</th>`).join("")}</tr>`;
      const tb = rows.map((r, d) => `<tr><th>${WEEKDAYS[d]}</th>${[...r].map((k, h) => `<td class="${cls(k)}" data-d="${d}" data-h="${h}" title="${WEEKDAYS[d]} ${hh(h)}:00–${hh(h + 1)}:00"></td>`).join("")}</tr>`).join("");
      return `<div class="cv-week${can ? " cv-dcan" : ""}" data-week="${esc(f.id)}">${brush}<div class="cv-wkwrap"><table class="cv-wk"><thead>${head}</thead><tbody>${tb}</tbody></table></div>`
        + (can ? `<div class="cv-drawbar"><small class="cv-dimtx">Pick a kind of recording, then tap or drag over the hours.</small><button type="button" class="cv-btn" data-wcopy="5" data-fid="${esc(f.id)}">Monday → Tue–Fri</button><button type="button" class="cv-btn" data-wcopy="7" data-fid="${esc(f.id)}">Monday → every day</button><button type="button" class="cv-btn" data-wfill data-fid="${esc(f.id)}">Whole week</button></div>` : "")
        + `<small class="cv-dimtx">${esc(weekShown(f, v))}</small></div>`;
    }
    function mountWeek(box) {
      if (!box.classList.contains("cv-dcan")) return;
      const fid = box.dataset.week, f = sec().fields.find((x) => x.id === fid); if (!f) return;
      const rows = weekRows(valOf(f)).map((r) => [...r]), tbl = box.querySelector("table");
      let on = false;
      const hit = (ev) => { const t = document.elementFromPoint(ev.clientX, ev.clientY); return t && tbl.contains(t) ? t.closest("td[data-d]") : null; };
      const put = (td) => { if (!td) return; const k = st.brush[fid] || ((f.modes || [])[0] || {}).v || "C";
        rows[+td.dataset.d][+td.dataset.h] = k; td.className = "cv-wm-" + (k === "-" ? "x" : k); };
      tbl.addEventListener("pointerdown", (ev) => { if (ev.button > 0) return; const td = hit(ev); if (!td) return;
        ev.preventDefault(); tbl.setPointerCapture(ev.pointerId); on = true; put(td); });
      tbl.addEventListener("pointermove", (ev) => { if (on) put(hit(ev)); });
      const up = () => { if (!on) return; on = false; setEdit(fid, rows.map((r) => r.join("")).join("|")); };
      tbl.addEventListener("pointerup", up);
      tbl.addEventListener("pointercancel", up);
    }
    function mountDraw(box) {
      const fid = box.dataset.draw, f = sec().fields.find((x) => x.id === fid), can = box.classList.contains("cv-dcan");
      const svg = box.querySelector("svg"), img = box.querySelector("img");
      if (!f) return;
      if (img) img.addEventListener("load", () => { if (img.naturalWidth) { box.style.aspectRatio = (img.naturalWidth / img.naturalHeight).toFixed(4); draw(); } });
      let P = null, R = null, G = null, temp = null;
      if (f.kind === "area" || f.kind === "line") P = (st.draft[fid] || pts(valOf(f))).map((p) => p.slice());
      if (f.kind === "rects") R = rects(valOf(f));
      if (f.kind === "grid") G = gridCells(valOf(f), f.rows, f.cols);
      let W = 1, H = 1;
      const px = (u, v) => [u * W / 1000, v * H / 1000];
      function add(tag, a, text) {
        const e = document.createElementNS(NS, tag);
        Object.entries(a).forEach(([k, v]) => e.setAttribute(k, v));
        if (text) e.textContent = text;
        svg.appendChild(e); return e;
      }
      function draw() {
        W = svg.clientWidth || box.clientWidth || 1; H = svg.clientHeight || box.clientHeight || 1;
        svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
        svg.textContent = "";
        if (G) {
          const cw = W / f.cols, rh = H / f.rows;
          G.forEach((on, i) => { if (on) add("rect", { x: (i % f.cols) * cw, y: Math.floor(i / f.cols) * rh, width: cw, height: rh, class: "cv-dcell" }); });
          for (let c = 1; c < f.cols; c++) add("line", { x1: c * cw, y1: 0, x2: c * cw, y2: H, class: "cv-dgrid" });
          for (let r = 1; r < f.rows; r++) add("line", { x1: 0, y1: r * rh, x2: W, y2: r * rh, class: "cv-dgrid" });
        }
        (R || []).concat(temp ? [temp] : []).forEach((r, i) => {
          const [x1, y1] = px(r[0], r[1]), [x2, y2] = px(r[2], r[3]);
          add("rect", { x: Math.min(x1, x2), y: Math.min(y1, y2), width: Math.abs(x2 - x1), height: Math.abs(y2 - y1), class: "cv-dmask", "data-i": r === temp ? "" : i });
        });
        if (P && P.length) {
          const xy = P.map((p) => px(p[0], p[1]));
          if (f.kind === "area") add(P.length >= 3 ? "polygon" : "polyline", { points: xy.map((p) => p.join(",")).join(" "), class: "cv-dpoly" });
          if (f.kind === "line" && P.length === 2) add("line", { x1: xy[0][0], y1: xy[0][1], x2: xy[1][0], y2: xy[1][1], class: "cv-dline" });
          xy.forEach((p, i) => {
            add("circle", { cx: p[0], cy: p[1], r: 9, class: "cv-dpt", "data-i": i });
            if (f.kind === "line") add("text", { x: p[0] + 12, y: p[1] - 10, class: "cv-dlab" }, i ? "B" : "A");
          });
        }
      }
      function commit(v, draft) {
        if (draft) { st.draft[fid] = draft; st.preview = null; st.msg = null; paint(); return; }
        delete st.draft[fid]; setEdit(fid, v);
      }
      const at = (ev) => { const r = svg.getBoundingClientRect();
        return [Math.round(Math.min(1000, Math.max(0, (ev.clientX - r.left) * 1000 / r.width))), Math.round(Math.min(1000, Math.max(0, (ev.clientY - r.top) * 1000 / r.height)))]; };
      const cell = (p) => Math.min(f.rows - 1, Math.floor(p[1] * f.rows / 1000)) * f.cols + Math.min(f.cols - 1, Math.floor(p[0] * f.cols / 1000));
      let drag = null;
      if (can) {
        svg.addEventListener("pointerdown", (ev) => {
          if (ev.button > 0) return;
          ev.preventDefault(); svg.setPointerCapture(ev.pointerId);
          const p = at(ev), hit = ev.target.dataset ? ev.target.dataset.i : undefined;
          if (P) {
            if (hit !== undefined && hit !== "") drag = { i: +hit, moved: false };
            else if (P.length < (f.kind === "line" ? 2 : f.max)) { P.push(p); drag = { i: P.length - 1, moved: true, added: true }; draw(); }
          } else if (R) {
            drag = { start: p, hit: hit !== undefined && hit !== "" ? +hit : null, moved: false };
          } else if (G) {
            const c = cell(p); drag = { mode: !G[c] }; G[c] = drag.mode; draw();
          }
        });
        svg.addEventListener("pointermove", (ev) => {
          if (!drag) return;
          const p = at(ev);
          if (P && drag.i !== undefined) { P[drag.i] = p; drag.moved = true; draw(); }
          else if (R && drag.start) { if (Math.abs(p[0] - drag.start[0]) + Math.abs(p[1] - drag.start[1]) > 8) { drag.moved = true; temp = [drag.start[0], drag.start[1], p[0], p[1]]; draw(); } }
          else if (G) { const c = cell(p); if (G[c] !== drag.mode) { G[c] = drag.mode; draw(); } }
        });
        const up = () => {
          if (!drag) return;
          const d = drag; drag = null;
          if (P) {
            const need = f.kind === "line" ? 2 : f.min, max = f.kind === "line" ? 2 : f.max;
            if (P.length >= need && P.length <= max) commit(P.map((q) => q.join(",")).join(" "));
            else commit(null, P);
          } else if (R) {
            if (!d.moved && d.hit !== null) R.splice(d.hit, 1);
            else if (d.moved && temp) {
              const r = [Math.min(temp[0], temp[2]), Math.min(temp[1], temp[3]), Math.max(temp[0], temp[2]), Math.max(temp[1], temp[3])];
              temp = null;
              if (r[2] - r[0] < 10 || r[3] - r[1] < 10) return draw();
              if (R.length >= f.max) { draw(); return say(`This camera takes up to ${f.max} boxes.`, "cv-warn"); }
              R.push(r);
            } else return draw();
            commit(R.map((r) => r.join(",")).join(";"));
          } else if (G) commit(gridHex(G, f.rows, f.cols));
        };
        svg.addEventListener("pointerup", up);
        svg.addEventListener("pointercancel", () => { drag = null; temp = null; draw(); });
      }
      requestAnimationFrame(draw);
    }
    function paint() {
      const d = st.data, s = sec();
      const lens = d.lenses.length > 1 ? `<div class="cv-seg" role="group" aria-label="Lens">${d.lenses.map((n) => `<button type="button" data-lens="${n}" aria-pressed="${n === st.lens}">Lens ${n}</button>`).join("")}</div>` : "";
      const tabs = `<div class="cv-seg cv-settabs" role="tablist">${d.sections.map((x) => `<button type="button" role="tab" data-sec="${esc(x.id)}" aria-pressed="${x.id === st.sec}">${esc(x.label)}</button>`).join("")}</div>`;
      let html = `<div class="cv-setbar">${tabs}${lens}<button type="button" class="cv-btn" data-a="reload">↻ Read again</button></div>`;
      if (d.route === "nvr" || (d.routes || []).length > 1) {
        const via = d.via ? `${esc(d.via.name || d.via.ip || "the recorder")} · channel ${esc(d.via.channel)}` : "";
        html += `<p class="cv-route">${d.route === "nvr" ? `Through the recorder: <b>${via}</b>.` : "Straight to the camera, with its own login."}${(d.routes || []).length > 1
          ? ` <span class="cv-seg" role="group" aria-label="Change it through"><button type="button" data-route="direct" aria-pressed="${d.route === "direct"}">The camera</button><button type="button" data-route="nvr" aria-pressed="${d.route === "nvr"}">The recorder</button></span>` : ""}</p>`;
      }
      if (!d.can_change) html += `<p class="cv-setmsg">${esc(d.why_not)} You can look, not change.</p>`;
      if (d.info) html += secInfo(d);
      if (s) {
        if (s.note) html += `<p class="cv-dimtx">${esc(s.note)}</p>`;
        if (s.error) html += `<p class="cv-setmsg cv-warn">${esc(s.error)}</p>`;
        if (d.can_change && (s.presets || []).length) html += `<div class="cv-setpre">${s.presets.map((p) => `<button type="button" class="cv-btn" data-pre="${esc(p.id)}">★ ${esc(p.label)}</button>`).join("")}</div>`;
        html += `<div class="cv-setgrid">${s.fields.filter((f) => visible(f, s)).map((f) => `<div class="cv-setrow${f.id in st.edits ? " cv-chg" : ""}${DRAW.has(f.kind) || f.kind === "week" ? " cv-wide" : ""}"><div class="cv-setl"><b>${esc(f.label)}</b>${f.help ? `<small>${esc(f.help)}</small>` : ""}</div><div class="cv-setc">${ctl(f)}${f.id === "name.name" && d.can_change && d.netwatch_name && d.netwatch_name !== f.value ? ` <button type="button" class="cv-btn" data-nwname>Use “${esc(d.netwatch_name)}”</button>` : ""}</div></div>`).join("") || `<p class="cv-dimtx">This camera has none of these settings.</p>`}</div>`;
        const n = Object.keys(st.edits).length;
        if (d.can_change && s.fields.length) html += `<div class="cv-setact"><button type="button" class="cv-btn cv-on" data-a="apply"${n ? "" : " disabled"}>${s.risk === "careful" ? "Review " : "Apply "}${n ? n + " change" + (n > 1 ? "s" : "") : "changes"}</button><button type="button" class="cv-btn" data-a="reset"${n ? "" : " disabled"}>Cancel</button>${RISK[s.risk] ? `<span class="cv-dimtx">${RISK[s.risk]}</span>` : ""}</div>`;
        if (st.preview) html += st.preview;
      }
      html += `<p class="cv-setmsg" data-msg aria-live="polite"></p><div class="cv-chgs" data-chgs></div>`;
      body.innerHTML = html;
      body.querySelectorAll("[data-draw]").forEach(mountDraw);
      body.querySelectorAll("[data-week]").forEach(mountWeek);
      if (st.msg) say(st.msg[0], st.msg[1]);
      paintChanges();
    }
    function secInfo(d) {
      const i = d.info, dev = d.recorder ? "recorder" : "camera";
      const on = (x) => (x ? "On" : "Off");
      let h = `<div class="cv-secinfo">`;
      if ((i.warnings || []).length) h += `<ul class="cv-secwarn">${i.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>`;
      h += `<dl class="cv-secdl"><dt>Users</dt><dd>${(i.users || []).map((u) => `${esc(u.name)} <span class="cv-dimtx">(${esc(u.level)})</span>`).join(", ") || "—"}</dd>`;
      h += `<dt>Ports</dt><dd>${(i.ports || []).map((p) => `<span class="${p.on ? "" : "cv-dimtx"}">${esc(p.protocol)} ${esc(p.port)}${p.on ? "" : " (off)"}</span>`).join(" · ") || "—"}</dd>`;
      if (i.hik_connect) h += `<dt>Hik-Connect</dt><dd>${on(i.hik_connect.on)}${i.hik_connect.on ? (i.hik_connect.registered ? ", connected to the cloud" : ", not connected") : ""}</dd>`;
      if (i.isup) h += `<dt>Platform access (ISUP)</dt><dd>${on(i.isup.on)}</dd>`;
      h += `<dt>Netwatch logs in as</dt><dd><b>${esc(i.login.uses || "—")}</b>${i.login.netwatch ? ` <span class="cv-dimtx">(its own Operator account${i.login.admin_kept ? "; the admin login is kept for risky jobs only" : ""})</span>` : ""}</dd></dl>`;
      if (d.can_change) {
        h += i.login.netwatch
          ? `<div class="cv-setact"><button type="button" class="cv-btn" data-acct="remove">Remove the Netwatch account</button><span class="cv-dimtx">Netwatch goes back to the admin login.</span></div>`
          : `<div class="cv-setact"><button type="button" class="cv-btn cv-on" data-acct="create">Set up the Netwatch account</button><span class="cv-dimtx">A user called netwatch with a long random password nobody sees. Netwatch then logs in with it instead of admin, so it can never lock admin out.</span></div>`;
      }
      if (d.can_change && !d.recorder && !st.pw) h += `<div class="cv-setact"><button type="button" class="cv-btn" data-a="pw-open">Change the admin password…</button><span class="cv-dimtx">Changes it on the camera and on every recorder channel that uses it, then saves it in Netwatch.</span></div>`;
      if (st.pw) {
        const pl = st.pw;
        const recs = (pl.recorders || []).map((r) => `<li>${esc(r.name)}: ${r.channels.length ? "channel " + r.channels.map(esc).join(", ") + " gets the new password" : "no channel uses this login"}${(r.other_user || []).length ? ` · <span class="cv-dimtx">channel ${r.other_user.map((x) => esc(x.channel) + " uses " + esc(x.user)).join(", ")} — not changed</span>` : ""}</li>`).join("");
        h += `<div class="cv-setprev"><p><b>Change the password of <i>${esc(pl.user)}</i> on this camera</b></p>
          <ul class="cv-chglist"><li>The camera's login lock is paused while the recorder catches up, then switched back on.</li>${recs || "<li>No recorder records this camera with that login.</li>"}<li>Netwatch saves the new password the moment the camera takes it. It is never shown or logged.</li></ul>
          <div class="cv-pwrow"><input type="password" class="cv-pwin" data-pw1 autocomplete="new-password" placeholder="New password" aria-label="New password" maxlength="${esc(pl.max)}"><input type="password" class="cv-pwin" data-pw2 autocomplete="new-password" placeholder="Again" aria-label="New password again" maxlength="${esc(pl.max)}"></div>
          <p class="cv-dimtx">${esc(pl.min)}–${esc(pl.max)} characters; at least two of small letters, capitals, digits and the symbols - . _ ~ ! $ ' ( ) * , / : ; @ [ ]</p>${wordBox("pw-go")}</div>`;
      }
      if (st.acct) h += `<div class="cv-setprev"><p><b>${st.acct === "create" ? `Set up the Netwatch account on this ${dev}?` : `Remove the Netwatch account from this ${dev}?`}</b></p>${st.acct === "create"
        ? `<ul class="cv-chglist"><li>Adds an Operator user <b>netwatch</b> with rights for pictures, health, settings, restart and PTZ — not firmware.</li><li>Tests that it logs in, then Netwatch uses it from now on.</li><li>The admin login stays saved, for risky jobs only. If anything fails, the new user is removed again.</li></ul>`
        : `<p>The netwatch user is deleted and Netwatch logs in as admin again.</p>`}${wordBox("acct-go")}</div>`;
      return h + `</div>`;
    }
    function wordBox(go) {
      const w = st.data.confirm_word || "CHANGE";
      return `<p class="cv-dimtx">Type <b>${esc(w)}</b> to go ahead.</p><div class="cv-setact"><input class="cv-word" data-word autocomplete="off" spellcheck="false" aria-label="Type ${esc(w)}"><button type="button" class="cv-btn cv-on" data-a="${go}">Go ahead</button><button type="button" class="cv-btn" data-a="unpreview">Back</button></div>`;
    }
    const word = () => ((body.querySelector("[data-word]") || {}).value || "").trim().toUpperCase();
    async function pwOpen() {
      say("Finding the recorder channels that use this login…", "");
      try { st.pw = await jcall(`${base}/password`); st.acct = null; st.msg = null; paint(); }
      catch (e) { say(e.message, "cv-warn"); }
    }
    async function pwGo() {
      if (st.busy) return;
      const v1 = (body.querySelector("[data-pw1]") || {}).value || "", v2 = (body.querySelector("[data-pw2]") || {}).value || "";
      if (!v1 || v1 !== v2) return say("Type the same new password twice.", "cv-warn");
      st.busy = true; say("Changing the password — the camera first, then the recorder; this can take a minute…", "");
      try {
        const j = await jcall(`${base}/password`, { method: "POST", body: JSON.stringify({ new: v1, again: v2, confirm_text: word() }) });
        st.busy = false; st.pw = null; await load(); say(j.message || "Done.", j.ok ? "cv-okt" : "cv-warn");
      } catch (e) { st.busy = false; say(e.message, "cv-warn"); }
    }
    async function account(action) {
      if (st.busy) return;
      st.busy = true; say(action === "create" ? "Setting up the Netwatch account — this takes a few seconds…" : "Removing the Netwatch account…", "");
      try {
        const j = await jcall(`${base}/account`, { method: "POST", body: JSON.stringify({ action, confirm_text: word() }) });
        st.busy = false; st.acct = null; await load(); say(j.message || "Done.", "cv-okt");
      } catch (e) { st.busy = false; say(e.message, "cv-warn"); }
    }
    function say(text, cls) { st.msg = [text, cls]; const m = body.querySelector("[data-msg]"); if (m) { m.textContent = text; m.className = "cv-setmsg " + (cls || ""); } }
    function setEdit(fid, v) {
      const f = sec().fields.find((x) => x.id === fid);
      if (!f) return;
      if (String(v) === String(f.value)) delete st.edits[fid]; else st.edits[fid] = String(v);
      st.preview = null; st.msg = null; paint();
    }
    function rows(list) {
      return `<ul class="cv-chglist">${list.map((r) => `<li><b>${esc(r.label)}</b> ${esc(r.from)} → ${esc(r.to)}</li>`).join("")}</ul>`;
    }
    async function apply(confirm) {
      const s = sec(); if (st.busy) return;
      const changes = { ...st.edits }, expect = {};
      Object.keys(changes).forEach((k) => { const f = s.fields.find((x) => x.id === k); if (f) expect[k] = f.value; });
      if ((s.risk === "careful" || s.risk === "dangerous") && !confirm) {
        const list = Object.keys(changes).map((k) => { const f = s.fields.find((x) => x.id === k);
          return { label: f.label, from: f.shown ?? f.value, to: shownOf(f, changes[k]) }; });
        const loses = (changes["t.Duration"] && changes["t.Duration"] !== "0") || changes["x.LoopEnable"] === "false" || changes["t.CustomExtensionList/CustomExtension/enableSchedule"] === "false";
        st.preview = `<div class="cv-setprev"><p><b>Change on the ${devWord()}:</b></p>${rows(list)}${loses ? `<p class="cv-warn">${changes["t.Duration"] && changes["t.Duration"] !== "0" ? "The recorder deletes this camera's footage older than that straight away — Undo can't bring it back." : changes["x.LoopEnable"] === "false" ? "When the disks are full the recorder will stop recording every camera." : "The recorder stops recording this camera."}</p>` : ""}${s.id === "time" ? `<p class="cv-dimtx">The camera's clock is also set from this Pi's clock, so it is right straight away.</p>` : ""}${s.id === "main" || s.id === "sub" ? `<p class="cv-dimtx">The picture may drop for a few seconds while the camera switches.</p>` : ""}${s.risk === "dangerous" ? `${changes["ll.enabled"] === "false" ? `<p class="cv-warn">Without the lock anyone on the network can keep guessing passwords.</p>` : ""}${changes["ssh.enabled"] === "true" || changes["upnp.enabled"] === "true" ? `<p class="cv-warn">This opens a door into the device. Turn it off again when you are done.</p>` : ""}${wordBox("confirm")}` : `<div class="cv-setact"><button type="button" class="cv-btn cv-on" data-a="confirm">Yes, change it</button><button type="button" class="cv-btn" data-a="unpreview">Back</button></div>`}</div>`;
        paint(); return;
      }
      if (Object.keys(st.draft).length) return say("Finish the drawing first (or press Start again).", "cv-warn");
      st.busy = true; say(`Changing it on the ${devWord()}…`, "");
      try {
        const j = await jcall(`${base}/settings`, { method: "POST", body: JSON.stringify({ lens: st.lens, section: s.id, changes, expect, confirm: true, route: st.data.route, confirm_text: s.risk === "dangerous" ? word() : "" }) });
        st.busy = false;
        const msg = j.message || "Done.";
        await load(); say(msg, j.ok ? "cv-okt" : "cv-warn");
        if (s.id === "picture" || s.id === "osd" || s.id === "masks") onPicture();
      } catch (e) {
        st.busy = false;
        if (e.status === 409 && e.body && e.body.conflict) { await load(); say(e.message + " — it was read again; make the change once more.", "cv-warn"); }
        else say(e.message, "cv-warn");
      }
    }
    let chg = [];
    async function loadChanges() {
      try { chg = (await jcall(`${base}/changes`)).changes || []; } catch (e) { chg = []; }
      if (!st.dead) paintChanges();
    }
    function paintChanges() {
      const box = body.querySelector("[data-chgs]"); if (!box) return;
      if (!chg.length) { box.innerHTML = ""; return; }
      box.innerHTML = `<h4>Changes made from Netwatch</h4><ul class="cv-chglog">${chg.slice(0, 12).map((x) => `<li>
        <div><b>${esc(x.section_label)}${x.lens > 1 ? " · lens " + x.lens : ""}${x.undo_of ? " (undo)" : ""}</b> <span class="cv-dimtx">${esc(ago(x.ts))}${x.who ? " · " + esc(x.who) : ""}${x.result !== "ok" ? ` · <span class="cv-warn">${x.result === "partial" ? "not every change kept" : "not done"}</span>` : ""}</span></div>
        <small>${(x.fields || []).map((r) => `${esc(r.label)} ${esc(r.shown_before)} → ${esc(r.shown_after)}${r.kept ? "" : " (not kept)"}`).join(" · ")}</small>
        ${x.undone_by ? `<span class="cv-dimtx">undone</span>` : !x.undo_of && !x.no_undo && st.data && st.data.can_change && (x.fields || []).some((r) => r.written) ? `<button type="button" class="cv-btn" data-undo="${esc(x.id)}">↶ Undo</button>` : ""}</li>`).join("")}</ul>`;
    }
    async function undo(id, btn) {
      if (!window.confirm("Put back the values this change replaced? It only goes ahead if the camera still shows this change.")) return;
      btn.disabled = true; say("Undoing on the camera…", "");
      try {
        const j = await jcall(`${base}/changes/${encodeURIComponent(id)}/undo`, { method: "POST", body: JSON.stringify({ confirm: true }) });
        await load(); say(j.message || "Undone.", j.ok ? "cv-okt" : "cv-warn"); onPicture();
      } catch (e) { btn.disabled = false; say(e.message, "cv-warn"); }
    }
    body.addEventListener("input", (e) => {
      const t = e.target;
      if (t.type === "range") { const o = t.parentElement.querySelector("output"); if (o) o.textContent = t.value; }
    });
    body.addEventListener("change", (e) => {
      const m = e.target.closest("[data-multi]");
      if (m) {
        const f = sec().fields.find((x) => x.id === m.dataset.multi); if (!f) return;
        const on = [...m.querySelectorAll("input:checked")].map((x) => x.value).concat(f.fixed || []);
        const order = (f.opts || []).map((x) => x.v).concat(f.fixed || []);
        return setEdit(f.id, order.filter((x) => on.includes(x)).join(","));
      }
      const t = e.target.closest("[data-f]"); if (!t) return;
      setEdit(t.dataset.f, t.type === "checkbox" ? String(t.checked) : t.value);
    });
    body.addEventListener("click", (e) => {
      const b = e.target.closest("button"); if (!b || b.disabled) return;
      if (b.dataset.wbrush !== undefined) { st.brush[b.dataset.fid] = b.dataset.wbrush; paint(); return; }
      if (b.dataset.wcopy || b.hasAttribute("data-wfill")) {
        const f = sec().fields.find((x) => x.id === b.dataset.fid); if (!f) return;
        const r = weekRows(valOf(f)), k = st.brush[f.id] || ((f.modes || [])[0] || {}).v || "C";
        const out = b.dataset.wcopy ? r.map((x, i) => (i > 0 && i < +b.dataset.wcopy ? r[0] : x)) : Array(7).fill(k.repeat(24));
        return setEdit(f.id, out.join("|"));
      }
      if (b.dataset.sec) { st.sec = b.dataset.sec; st.edits = {}; st.draft = {}; st.preview = null; st.msg = null; paint(); }
      else if (b.dataset.dclear) { const f = sec().fields.find((x) => x.id === b.dataset.dclear); if (!f) return;
        if (f.kind === "rects") { delete st.draft[f.id]; setEdit(f.id, ""); } else { st.draft[f.id] = []; st.preview = null; st.msg = null; paint(); } }
      else if (b.dataset.gall) { const f = sec().fields.find((x) => x.id === b.dataset.fid); if (f) setEdit(f.id, gridHex(new Array(f.rows * f.cols).fill(b.dataset.gall === "1"), f.rows, f.cols)); }
      else if (b.dataset.lens) { st.lens = +b.dataset.lens; st.msg = null; load(); }
      else if (b.dataset.route) { if (b.dataset.route !== st.data.route) { st.route = b.dataset.route; st.msg = null; load(); } }
      else if (b.dataset.pre) { const p = (sec().presets || []).find((x) => x.id === b.dataset.pre); if (p) { Object.entries(p.changes).forEach(([k, v]) => { const f = sec().fields.find((x) => x.id === k); if (f && f.editable && String(f.value) !== String(v)) st.edits[k] = String(v); }); st.preview = null; st.msg = Object.keys(st.edits).length ? null : ["The camera already has these values.", "cv-okt"]; paint(); } }
      else if (b.hasAttribute("data-nwname")) setEdit("name.name", st.data.netwatch_name);
      else if (b.dataset.undo) undo(b.dataset.undo, b);
      else if (b.dataset.acct) { st.acct = b.dataset.acct; st.preview = null; st.msg = null; paint(); }
      else if (b.dataset.a === "acct-go") account(st.acct);
      else if (b.dataset.a === "pw-open") pwOpen();
      else if (b.dataset.a === "pw-go") pwGo();
      else if (b.dataset.a === "reload") { st.msg = null; load(); }
      else if (b.dataset.a === "reset") { st.edits = {}; st.draft = {}; st.preview = null; st.msg = null; paint(); }
      else if (b.dataset.a === "apply") apply(false);
      else if (b.dataset.a === "confirm") apply(true);
      else if (b.dataset.a === "unpreview") { st.preview = null; st.acct = null; st.pw = null; paint(); }
    });
    return { destroy() { st.dead = true; } };
  }

  // ---- a recorder's cameras (Phase 4d part 2: app/hikchannels.py) -----------------------
  // Every channel login comes from Netwatch's saved admin login for that camera; nobody
  // types a camera password here. Fix login / move / remove / add are risky: typed word.
  function channelsPanel(el, base) {
    const st = { data: null, dead: false, busy: false, ask: null, msg: null };
    el.innerHTML = `<details class="cv-setd"><summary><h3>📹 Cameras on this recorder</h3><span class="cv-dimtx">channels · their logins · add, move, fix or remove</span></summary><div class="cv-setb" data-b></div></details>`;
    const det = el.querySelector("details"), body = el.querySelector("[data-b]");
    det.addEventListener("toggle", () => { if (det.open && !st.data) load(); });
    async function load() {
      body.innerHTML = `<p class="cv-dimtx">Reading the recorder's channels…</p>`;
      try { st.data = await jcall(`${base}/channels`); st.deadSel = null; if (!st.dead) paint(); }
      catch (e) { if (!st.dead) body.innerHTML = `<p class="cv-setmsg cv-warn">${esc(e.status === 404 || e.status === 501 ? "This site's Netwatch is too old for this — update it." : e.message)}</p>`; }
    }
    // the recorder's own words for a channel (chanDetectResult), said plainly
    const CHSTATE = { networkUnreachable: "nothing answers", passwordError: "wrong password", ipcStreamFail: "no video from the camera",
      notExist: "no camera there", connecting: "connecting…", userLocked: "camera locked the login", ipConflict: "address clash",
      offline: "not connected", ipcStreamNotMatch: "stream mismatch", resolutionNotSupport: "resolution not supported" };
    const stateOf = (r) => r.online ? `<span class="cv-okt">connected</span>` : `<span class="cv-warn" title="${esc(r.state || "")}">${esc(CHSTATE[r.state] || r.state || "not connected")}</span>`;
    function paint() {
      const d = st.data, can = d.can_change;
      let h = can ? "" : `<p class="cv-setmsg">${esc(d.why_not)} You can look, not change.</p>`;
      // Channels nothing answers on: not connected and no device Netwatch knows at that address.
      const dead = d.channels.filter((r) => r.online === false && !r.netwatch);
      if (dead.length) {
        if (!st.deadSel) st.deadSel = new Set(dead.map((r) => r.id));
        h += `<div class="cv-chbox"><p><b>${dead.length} empty channel${dead.length > 1 ? "s" : ""}</b> <span class="cv-dimtx">— nothing answers at ${dead.length > 1 ? "these addresses" : "this address"} and Netwatch has never seen a device there</span></p>
          <div class="cv-chdeadl">${dead.map((r) => `<label class="cv-tog"><input type="checkbox" data-dead="${r.id}"${st.deadSel.has(r.id) ? " checked" : ""}${can ? "" : " disabled"}><span>${esc(r.id)} · ${esc(r.name || "—")} <small class="cv-dimtx">${esc(r.ip || "")}</small></span></label>`).join("")}</div>
          ${can ? `<div class="cv-setact"><button type="button" class="cv-btn" data-ask="remove_dead"${st.deadSel.size ? "" : " disabled"}>Remove ${st.deadSel.size} empty channel${st.deadSel.size === 1 ? "" : "s"}</button><span class="cv-dimtx">A named one (not "IPCamera 05") was a real camera once — check on site whether it hangs somewhere without power first.</span></div>` : ""}</div>`;
      }
      const renamable = d.channels.filter((r) => r.netwatch && r.netwatch.name && r.netwatch.name !== r.name && r.login_known);
      if (renamable.length && can) h += `<div class="cv-chbox"><p><b>${renamable.length} channel name${renamable.length > 1 ? "s differ" : " differs"} from Netwatch</b> <span class="cv-dimtx">— e.g. ${esc(renamable[0].name || "—")} → ${esc(renamable[0].netwatch.name)}</span></p><div class="cv-setact"><button type="button" class="cv-btn" data-ask="names">Use Netwatch's names on the recorder</button></div></div>`;
      h += `<div class="cv-chwrap"><table class="cv-ch"><thead><tr><th>Ch</th><th>Camera</th><th>Address</th><th>Login</th><th>State</th>${can ? "<th></th>" : ""}</tr></thead><tbody>${d.channels.map((r) => `<tr>
        <td>${esc(r.id)}</td><td>${esc(r.name)}${r.netwatch && r.netwatch.name !== r.name ? `<br><small class="cv-dimtx">Netwatch: ${esc(r.netwatch.name)}</small>` : ""}</td>
        <td>${esc(r.ip)}${r.port && r.port !== 8000 ? ":" + esc(r.port) : ""}</td><td>${esc(r.user)}${r.login_known ? "" : ` <small class="cv-dimtx">(no login in Netwatch)</small>`}</td><td>${stateOf(r)}</td>
        ${can ? `<td class="cv-chact">${r.login_known ? `<button type="button" class="cv-btn" data-ask="fix_login" data-ch="${esc(r.id)}">Fix login</button><button type="button" class="cv-btn" data-ask="rename" data-ch="${esc(r.id)}">Rename</button>` : ""}<button type="button" class="cv-btn" data-ask="move" data-ch="${esc(r.id)}">Move</button><button type="button" class="cv-btn" data-ask="remove" data-ch="${esc(r.id)}">Remove</button></td>` : ""}</tr>`).join("")}</tbody></table></div>`;
      if (can && d.addable.length && d.free) h += `<div class="cv-setact"><select class="cv-sel" data-addcam>${d.addable.map((c) => `<option value="${esc(c.key)}">${esc(c.name)} (${esc(c.ip)})</option>`).join("")}</select><button type="button" class="cv-btn" data-ask="add">Add to this recorder</button><span class="cv-dimtx">${esc(d.free)} free channel${d.free === 1 ? "" : "s"}</span></div>`;
      if (st.ask) {
        const a = st.ask, r = d.channels.find((x) => x.id === a.ch) || {};
        const nrows = d.channels.filter((x) => x.netwatch && x.netwatch.name && x.netwatch.name !== x.name && x.login_known);
        const what = a.action === "remove_dead" ? `Remove ${a.chs.length} empty channel${a.chs.length > 1 ? "s" : ""} (${esc(a.chs.join(", "))}) from this recorder?`
          : a.action === "names" ? `Rename ${nrows.length} channel${nrows.length > 1 ? "s" : ""} on the recorder to their Netwatch names?`
          : a.action === "rename" ? `Rename channel ${esc(a.ch)} (${esc(r.name)}) on the recorder`
          : a.action === "fix_login" ? `Give channel ${esc(a.ch)} (${esc(r.name)}) the login Netwatch has for ${esc(r.ip)} (${esc(r.login_user)})?`
          : a.action === "remove" ? `Remove channel ${esc(a.ch)} (${esc(r.name)}, ${esc(r.ip)}) from this recorder?`
          : a.action === "add" ? `Add ${esc(a.label)} to this recorder, with the login Netwatch has for it?`
          : `Move channel ${esc(a.ch)} (${esc(r.name)}) to another camera address`;
        h += `<div class="cv-setprev"><p><b>${what}</b></p>${a.action === "remove" ? `<p class="cv-warn">The recorder stops recording this camera. Its old footage stays until it is overwritten.</p>` : ""}${a.action === "remove_dead" ? `<p class="cv-dimtx">Nothing records on them now. This can't be undone — a channel can be added again later.</p>` : ""}${a.action === "names" ? `<ul class="cv-chglist">${nrows.map((x) => `<li>${esc(x.id)}: ${esc(x.name || "—")} → <b>${esc(x.netwatch.name.slice(0, 32))}</b></li>`).join("")}</ul>` : ""}${a.action === "rename" ? `<div class="cv-pwrow"><input class="cv-pwin" data-newname maxlength="32" value="${esc((r.netwatch && r.netwatch.name) || r.name)}" aria-label="New name"></div><p class="cv-dimtx">Shown on the recorder's screen and in its playback. Netwatch gives the channel the camera's saved login while it writes.</p>` : ""}${a.action === "move" ? `<div class="cv-pwrow"><input class="cv-pwin" data-mip placeholder="New address" value="${esc(r.ip)}" aria-label="New address"><input class="cv-pwin" data-mport placeholder="Port" value="${esc(r.port || 8000)}" aria-label="Port" style="width:6rem"></div><p class="cv-dimtx">Netwatch gives the channel the login it has for the camera at the new address.</p>` : ""}
          <p class="cv-dimtx">Type <b>${esc(d.confirm_word)}</b> to go ahead.</p><div class="cv-setact"><input class="cv-word" data-word autocomplete="off" spellcheck="false" aria-label="Type ${esc(d.confirm_word)}"><button type="button" class="cv-btn cv-on" data-go>Go ahead</button><button type="button" class="cv-btn" data-back>Back</button></div></div>`;
      }
      h += `<p class="cv-setmsg" data-msg aria-live="polite"></p>`;
      body.innerHTML = h;
      if (st.msg) say(st.msg[0], st.msg[1]);
    }
    function say(t, c) { st.msg = [t, c]; const m = body.querySelector("[data-msg]"); if (m) { m.textContent = t; m.className = "cv-setmsg " + (c || ""); } }
    async function go() {
      if (st.busy) return;
      const a = st.ask, w = ((body.querySelector("[data-word]") || {}).value || "").trim().toUpperCase();
      const b = { action: a.action, ch: a.ch, camera: a.camera, confirm_text: w };
      if (a.action === "move") { b.ip = (body.querySelector("[data-mip]") || {}).value; b.port = (body.querySelector("[data-mport]") || {}).value; }
      if (a.action === "remove_dead") b.chs = a.chs;
      if (a.action === "rename") b.name = (body.querySelector("[data-newname]") || {}).value;
      if (a.action === "names") {
        const todo = st.data.channels.filter((x) => x.netwatch && x.netwatch.name && x.netwatch.name !== x.name && x.login_known);
        st.busy = true; let done = 0;
        for (const x of todo) {
          say(`Renaming channel ${x.id} (${done + 1} of ${todo.length})…`, "");
          try { await jcall(`${base}/channels`, { method: "POST", body: JSON.stringify({ action: "rename", ch: x.id, name: x.netwatch.name.slice(0, 32), confirm_text: w }) }); done++; }
          catch (e) { st.busy = false; say(`Stopped at channel ${x.id}: ${e.message}`, "cv-warn"); return; }
        }
        st.busy = false; st.ask = null; await load(); say(`Done — ${done} channel${done === 1 ? "" : "s"} renamed (each is in History with Undo).`, "cv-okt"); return;
      }
      st.busy = true; say("Working on the recorder — it can take up to a minute for the channel to connect…", "");
      try {
        const j = await jcall(`${base}/channels`, { method: "POST", body: JSON.stringify(b) });
        st.busy = false; st.ask = null; await load(); say(j.message || "Done.", j.ok ? "cv-okt" : "cv-warn");
      } catch (e) { st.busy = false; say(e.message, "cv-warn"); }
    }
    body.addEventListener("change", (e) => {
      const c = e.target.closest("[data-dead]"); if (!c) return;
      if (c.checked) st.deadSel.add(+c.dataset.dead); else st.deadSel.delete(+c.dataset.dead);
      paint();
    });
    body.addEventListener("click", (e) => {
      const bt = e.target.closest("button"); if (!bt || bt.disabled) return;
      if (bt.dataset.ask === "remove_dead") { st.ask = { action: "remove_dead", chs: [...st.deadSel].sort((x, y) => x - y) }; st.msg = null; paint(); }
      else if (bt.dataset.ask === "names") { st.ask = { action: "names" }; st.msg = null; paint(); }
      else if (bt.dataset.ask === "add") { const s = body.querySelector("[data-addcam]"); st.ask = { action: "add", camera: s.value, label: s.options[s.selectedIndex].text }; st.msg = null; paint(); }
      else if (bt.dataset.ask) { st.ask = { action: bt.dataset.ask, ch: +bt.dataset.ch }; st.msg = null; paint(); }
      else if (bt.hasAttribute("data-go")) go();
      else if (bt.hasAttribute("data-back")) { st.ask = null; paint(); }
    });
    return { destroy() { st.dead = true; } };
  }

  // ---- the recorder page's own panels (2026-11-07) ----------------------------------------
  // Recording: every channel and how THIS recorder records it (a camera can be recorded by
  // several), then "set the chosen channels to …" as one job — read back, in History, and
  // the whole job can be undone. Only what is chosen is changed.
  const SCHED_CLS = { continuous: "cv-okt", motion: "", event: "", mixed: "", off: "cv-warn", custom: "cv-dimtx" };
  function recordingPanel(el, base, jobBase, rec) {
    const st = { data: null, dead: false, busy: false, job: null, msg: null, sel: new Set(), stopWatch: null };
    el.innerHTML = `<div class="cv-setd cv-pad"><div class="cv-ptzhead"><h3>⏺ How this recorder records each camera</h3><span class="cv-dimtx">its own recording of every channel — a camera other recorders also record is changed here only</span></div><div data-b><p class="cv-dimtx">Reading every channel's recording (one document each)…</p></div></div>`;
    const body = el.querySelector("[data-b]");
    async function load() {
      try { st.data = await jcall(`${base}/recording`); } catch (e) { if (!st.dead) body.innerHTML = `<p class="cv-setmsg cv-warn">${esc(e.status === 404 ? "This site's Netwatch is too old for this — update it." : e.message)}</p>`; return; }
      if (!st.dead) paint();
    }
    load();
    const opt = (v, l, cur) => `<option value="${esc(v)}"${String(cur) === String(v) ? " selected" : ""}>${esc(l)}</option>`;
    function paint() {
      const d = st.data, rows = d.channels || [], can = d.can_change && !!jobBase;
      const off = rows.filter((r) => r.track && !r.track.on).length;
      let h = `<div class="cv-tsum"><span><b>${rows.length}</b> channels</span><span><b>${rows.filter((r) => r.track && r.track.on).length}</b> recording</span>${off ? `<span class="cv-warn"><b>${off}</b> not recording</span>` : ""}<span><b>${rows.filter((r) => r.track && r.track.stream === "sub").length}</b> on the sub stream</span></div>`;
      if (!can) h += `<p class="cv-setmsg">${esc(d.why_not || "")} You can look, not change.</p>`;
      h += `<div class="cv-chwrap"><table class="cv-ch cv-rect"><thead><tr>${can ? `<th><input type="checkbox" data-all aria-label="Choose all"></th>` : ""}<th>Ch</th><th>Camera</th><th>Records</th><th>When</th><th>Stream</th><th>Before / after</th><th>Keeps</th></tr></thead><tbody>${rows.map((r) => {
        const t = r.track;
        const when = !t ? `<span class="cv-dimtx">no recording set up</span>` : `<span class="${SCHED_CLS[t.schedule.kind] || ""}">${esc(t.schedule.text)}</span>`;
        return `<tr class="${r.online === false ? "cv-chdead" : ""}">${can ? `<td><input type="checkbox" data-ch="${r.id}"${st.sel.has(r.id) ? " checked" : ""}${t ? "" : " disabled"} aria-label="Channel ${r.id}"></td>` : ""}
          <td>${esc(r.id)}</td><td>${esc(r.name || "—")}<br><small class="cv-dimtx">${esc(r.ip || "")}${r.online === false ? " · not connected" : ""}</small></td>
          <td>${!t ? "—" : t.on ? `<span class="cv-okt">Yes</span>` : `<span class="cv-warn">No</span>`}</td><td>${when}</td>
          <td>${t ? esc({ main: "Main", sub: "Sub", third: "Third", dual: "Main + sub" }[t.stream] || "—") : "—"}</td>
          <td>${t && t.pre != null ? `${esc(t.pre)} s / ${esc(t.post)} s` : "—"}</td><td>${t && t.days != null ? (t.days === "0" ? "until full" : esc(t.days) + " days") : "—"}</td></tr>`;
      }).join("")}</tbody></table></div>`;
      if (can) {
        h += `<div class="cv-recbox"><p class="cv-setl"><b>Set the chosen channels to</b> <small class="cv-dimtx">${st.sel.size ? `${st.sel.size} chosen` : "choose channels in the table"} · only what you set changes</small></p>
          <div class="cv-recform">
            <label>Record <select class="cv-q cv-setin" data-w="on">${opt("", "leave")}${opt("1", "yes")}${opt("0", "no")}</select></label>
            <label>When <select class="cv-q cv-setin" data-w="mode">${opt("", "leave")}${opt("C", "continuous, all week")}${opt("M", "on motion, all week")}</select></label>
            <label>Stream <select class="cv-q cv-setin" data-w="stream">${opt("", "leave")}${opt("main", "main (sharp)")}${opt("sub", "sub (saves disk)")}</select></label>
            <label>Before <select class="cv-q cv-setin" data-w="pre">${opt("", "leave")}${["0", "5", "10", "15", "20", "25", "30"].map((v) => opt(v, v + " s")).join("")}</select></label>
            <label>After <select class="cv-q cv-setin" data-w="post">${opt("", "leave")}${["5", "10", "30", "60", "120", "300", "600"].map((v) => opt(v, v + " s")).join("")}</select></label>
          </div>
          <p class="cv-dimtx">Motion recording needs motion detection on in each camera's Detection panel. "Before" and "after" only count for motion.</p>
          <div class="cv-setact"><button type="button" class="cv-btn cv-on" data-recgo${st.busy ? " disabled" : ""}>Change ${st.sel.size || ""} channel${st.sel.size === 1 ? "" : "s"}</button><button type="button" class="cv-btn" data-pick="off">Choose the ones not recording</button><button type="button" class="cv-btn" data-pick="none">Choose none</button></div></div>`;
      }
      if (st.job) h += jobHtml(st.job, true);
      h += `<p class="cv-setmsg" data-msg aria-live="polite"></p>`;
      const keep = [...body.querySelectorAll("[data-w]")].reduce((o, x) => (o[x.dataset.w] = x.value, o), {});
      body.innerHTML = h;
      Object.entries(keep).forEach(([k, v]) => { const x = body.querySelector(`[data-w="${k}"]`); if (x) x.value = v; });
      if (st.msg) say(st.msg[0], st.msg[1]);
    }
    function say(t, c) { st.msg = [t, c]; const m = body.querySelector("[data-msg]"); if (m) { m.textContent = t; m.className = "cv-setmsg " + (c || ""); } }
    function follow(id) {
      if (st.stopWatch) st.stopWatch();
      st.stopWatch = watchJob(jobBase, id, (j) => { st.job = j; if (j.state !== "queued" && j.state !== "running") { st.busy = false; load(); } paint(); }, () => st.dead);
    }
    body.addEventListener("change", (e) => {
      const c = e.target.closest("[data-ch]");
      if (c) { if (c.checked) st.sel.add(+c.dataset.ch); else st.sel.delete(+c.dataset.ch); paint(); return; }
      if (e.target.closest("[data-all]")) { const on = e.target.checked; st.sel = new Set(on ? st.data.channels.filter((r) => r.track).map((r) => r.id) : []); paint(); }
    });
    body.addEventListener("click", async (e) => {
      const b = e.target.closest("button"); if (!b || b.disabled) return;
      if (b.dataset.jstop || b.dataset.jundo) { jobAction(jobBase, b, say, (j) => { st.job = j; follow(j.id); }); return; }
      if (b.dataset.pick) { st.sel = new Set(b.dataset.pick === "off" ? st.data.channels.filter((r) => r.track && !r.track.on).map((r) => r.id) : []); paint(); return; }
      if (!b.hasAttribute("data-recgo") || st.busy) return;
      const w = {};
      body.querySelectorAll("[data-w]").forEach((x) => { if (x.value !== "") w[x.dataset.w] = x.dataset.w === "on" ? x.value === "1" : x.value; });
      if (!st.sel.size) return say("Choose the channels first (the boxes in the table).", "cv-warn");
      if (!Object.keys(w).length) return say("Choose what to set — everything says “leave”.", "cv-warn");
      st.busy = true; say("Starting…", "");
      try {
        const j = await jcall(`${jobBase}/camera-jobs`, { method: "POST", body: JSON.stringify({ kind: "record", recorder: rec.key || st.data.key, channels: [...st.sel], want: w }) });
        st.job = j.job; st.sel = new Set(); say("", ""); paint(); follow(j.job.id);
      } catch (err) { st.busy = false; say(err.message, "cv-warn"); }
    });
    return { destroy() { st.dead = true; if (st.stopWatch) st.stopWatch(); } };
  }

  // Alarms: what the recorder itself does when a disk fills or fails, its cable is pulled,
  // an address clashes, wrong passwords are tried or a recording fails. Each switch is one
  // change (read back, in History, with Undo here).
  const ALARM_GOOD = { diskfull: ["beep", "center"], diskerror: ["beep", "center"], nicbroken: ["beep"], recordingfailure: ["beep", "center"], illaccess: ["center"] };
  function alarmsPanel(el, base) {
    const st = { data: null, dead: false, busy: false, msg: null, last: null };
    el.innerHTML = `<div class="cv-setd cv-pad"><div class="cv-ptzhead"><h3>🔔 When something goes wrong with the recorder</h3><span class="cv-dimtx">beep on site · tell the app (Hik-Connect) · email</span></div><div data-b><p class="cv-dimtx">Reading the recorder's alarms…</p></div></div>`;
    const body = el.querySelector("[data-b]");
    async function load() {
      try { st.data = await jcall(`${base}/alarms`); } catch (e) { if (!st.dead) body.innerHTML = `<p class="cv-setmsg cv-warn">${esc(e.status === 404 ? "This site's Netwatch is too old for this — update it." : e.message)}</p>`; return; }
      if (!st.dead) paint();
    }
    load();
    function paint() {
      const d = st.data, names = Object.fromEntries((d.methods || []).map((m) => [m.id, m.label])), dis = d.can_change && !st.busy ? "" : " disabled";
      const silent = d.events.filter((ev) => !ev.methods.some((m) => ev.can.includes(m)));
      let h = d.can_change ? "" : `<p class="cv-setmsg">${esc(d.why_not || "")} You can look, not change.</p>`;
      if (silent.length) h += `<p class="cv-setmsg cv-warn">${silent.length} of these do nothing at all now: ${esc(silent.map((x) => x.label.toLowerCase()).join(", "))}.</p>`;
      h += `<div class="cv-alarms">${d.events.map((ev) => `<div class="cv-alrow"><div class="cv-setl"><b>${esc(ev.label)}</b><small>${esc(ev.why)}</small></div>
        <div class="cv-alsw">${ev.can.map((m) => `<label class="cv-tog"><input type="checkbox" data-ev="${esc(ev.id)}" data-m="${esc(m)}"${ev.methods.includes(m) ? " checked" : ""}${dis}><span>${esc(names[m] || m)}</span></label>`).join("")}</div></div>`).join("")}</div>
        <p class="cv-dimtx">“Tell the app” reaches Hik-Connect and any platform watching the recorder. Email only works once the recorder has a mail server set up.</p>
        ${d.can_change ? `<div class="cv-setact"><button type="button" class="cv-btn" data-good${dis}>Use the FarmCamSA alarms</button><span class="cv-dimtx">disk full, disk error and failed recording beep and tell the app; a pulled cable beeps; wrong passwords tell the app</span></div>` : ""}
        ${st.last ? `<div class="cv-setact"><span class="cv-dimtx">Last change: ${esc(st.last.label)}</span><button type="button" class="cv-btn" data-undo${dis}>Undo</button></div>` : ""}
        <p class="cv-setmsg" data-msg aria-live="polite"></p>`;
      body.innerHTML = h;
      if (st.msg) say(st.msg[0], st.msg[1]);
    }
    function say(t, c) { st.msg = [t, c]; const m = body.querySelector("[data-msg]"); if (m) { m.textContent = t; m.className = "cv-setmsg " + (c || ""); } }
    async function set(ev, methods) {
      const j = await jcall(`${base}/alarms`, { method: "POST", body: JSON.stringify({ event: ev, methods }) });
      if (j.change) st.last = { id: j.change.id, label: j.change.section_label };
      const row = st.data.events.find((x) => x.id === ev); if (row && !j.unchanged) row.methods = methods.slice();
      return j;
    }
    body.addEventListener("change", async (e) => {
      const c = e.target.closest("[data-ev]"); if (!c || st.busy) return;
      const ev = st.data.events.find((x) => x.id === c.dataset.ev);
      const want = [...body.querySelectorAll(`[data-ev="${c.dataset.ev}"]:checked`)].map((x) => x.dataset.m);
      st.busy = true; say(`Changing “${ev.label}”…`, ""); paint();
      try { const j = await set(ev.id, want); st.busy = false; say(j.message || "Done.", j.ok === false ? "cv-warn" : "cv-okt"); }
      catch (err) { st.busy = false; say(err.message, "cv-warn"); }
      paint();
    });
    body.addEventListener("click", async (e) => {
      const b = e.target.closest("button"); if (!b || b.disabled || st.busy) return;
      if (b.hasAttribute("data-good")) {
        st.busy = true; paint();
        let n = 0;
        try {
          for (const ev of st.data.events) {
            const want = (ALARM_GOOD[ev.id] || []).filter((m) => ev.can.includes(m));
            const keep = ev.methods.filter((m) => !["beep", "center"].includes(m));       // email stays as it is
            const all = [...new Set([...want, ...keep])];
            if (ALARM_GOOD[ev.id] && all.sort().join() !== ev.methods.slice().sort().join()) { say(`Changing “${ev.label}”…`, ""); await set(ev.id, all); n++; }
          }
          say(n ? `Done — ${n} alarm${n > 1 ? "s" : ""} changed (each is in History with its own Undo).` : "Already set like that.", "cv-okt");
        } catch (err) { say(err.message, "cv-warn"); }
        st.busy = false; paint(); return;
      }
      if (b.hasAttribute("data-undo") && st.last) {
        st.busy = true; paint();
        try { await jcall(`${base}/changes/${encodeURIComponent(st.last.id)}/undo`, { method: "POST", body: JSON.stringify({ confirm: true }) }); st.last = null; say("Undone.", "cv-okt"); await load(); }
        catch (err) { say(err.message, "cv-warn"); }
        st.busy = false; paint();
      }
    });
    return { destroy() { st.dead = true; } };
  }

  // Restart: the typed word, like every risky job; the recorder records nothing meanwhile.
  function restartPanel(el, base, name) {
    const st = { open: false, dead: false };
    el.innerHTML = `<details class="cv-setd"><summary><h3>🔄 Restart</h3><span class="cv-dimtx">when it misbehaves — it records nothing for a minute or two</span></summary><div class="cv-setb" data-b>
      <p>Restart <b>${esc(name)}</b>? Every camera on it stops recording until it is back (usually 1–3 minutes).</p>
      <p class="cv-dimtx">Needs “Risky camera work” (Settings → Cameras). Type <b>CHANGE</b> to go ahead.</p>
      <div class="cv-setact"><input class="cv-word" data-word autocomplete="off" spellcheck="false" aria-label="Type CHANGE"><button type="button" class="cv-btn cv-ptzwarn" data-go>Restart it</button></div>
      <p class="cv-setmsg" data-msg aria-live="polite"></p></div></details>`;
    const msg = el.querySelector("[data-msg]");
    el.addEventListener("click", async (e) => {
      const b = e.target.closest("[data-go]"); if (!b || b.disabled) return;
      b.disabled = true; msg.textContent = "Asking it to restart…"; msg.className = "cv-setmsg";
      try { const j = await jcall(`${base}/restart`, { method: "POST", body: JSON.stringify({ confirm_text: (el.querySelector("[data-word]").value || "").trim().toUpperCase() }) }); msg.textContent = j.message; msg.className = "cv-setmsg cv-okt"; }
      catch (err) { msg.textContent = err.message; msg.className = "cv-setmsg cv-warn"; b.disabled = false; }
    });
    return { destroy() { st.dead = true; } };
  }

  // ---- the FarmCamSA standard and bulk jobs (Phase 5: app/hikbulk.py) -------------------
  // A check reads every device (writes nothing); a job brings chosen devices to one rule,
  // or copies settings to cameras of the same model — one device at a time on the site
  // Pi, each change read back, in History, and the whole job can be undone at once.
  const similarOf = (g, c) => (c.model ? ((g.data && g.data.cameras) || []).filter((x) => x.key !== c.key && x.kind === "camera"
    && (x.model || "").toUpperCase() === c.model.toUpperCase() && (x.has_login || (x.nvr && x.route))) : []);
  const JSTATE = { queued: "waiting", running: "running", done: "finished", stopped: "stopped" };
  const ISTATE = { pending: "waiting", running: "working…", ok: "done", unchanged: "already right", partial: "partly", failed: "failed", skipped: "not done" };
  const tzShown = (v) => { const m = /^CST([+-])(\d{1,2})(?::(\d\d))?/.exec(v || ""); return m ? `${v} (UTC${m[1] === "-" ? "+" : "−"}${+m[2]}${m[3] && m[3] !== "00" ? ":" + m[3] : ""})` : v; };
  const valShown = (id, v) => v === "" || v == null ? "(empty)" : id === "time.timeZone" ? tzShown(v) : v === "true" ? "on" : v === "false" ? "off" : v;
  function jobHtml(j, withUndo) {
    const n = j.counts || {}, items = j.items || [];
    const sum = ["ok", "unchanged", "partial", "failed", "skipped"].filter((k) => n[k]).map((k) => `${n[k]} ${ISTATE[k]}`).join(" · ");
    const live = j.state === "queued" || j.state === "running";
    return `<div class="cv-job"><p><b>${esc(j.label)}</b> — ${esc(JSTATE[j.state] || j.state)}${live ? ` (${items.filter((i) => !["pending", "running"].includes(i.status)).length} of ${items.length || j.total})` : ""}${sum ? ` · ${esc(sum)}` : ""}${j.note ? ` <span class="cv-dimtx">(${esc(j.note)})</span>` : ""}${j.undone_by ? ` <span class="cv-dimtx">· undone</span>` : ""}</p>
      ${items.length ? `<ul class="cv-jobl">${items.map((i) => `<li class="cv-js-${esc(i.status)}"><b>${esc(i.name)}</b> ${esc(ISTATE[i.status] || i.status)}${i.message && i.status !== "ok" ? ` <span class="cv-dimtx">— ${esc(i.message)}</span>` : ""}</li>`).join("")}</ul>` : ""}
      <div class="cv-setact">${live ? `<button type="button" class="cv-btn" data-jstop="${esc(j.id)}">Stop after this device</button>` : ""}${withUndo && !live && ["rule", "copy", "record"].includes(j.kind) && !j.undone_by && items.some((i) => i.change_id) ? `<button type="button" class="cv-btn" data-jundo="${esc(j.id)}">Undo this whole job</button>` : ""}</div></div>`;
  }
  // Poll one job until it ends; onTick(job) on every read.
  function watchJob(base, id, onTick, dead) {
    let t = null;
    const tick = async () => {
      if (dead()) return;
      try {
        const j = (await jcall(`${base}/camera-jobs/${encodeURIComponent(id)}`)).job;
        onTick(j);
        if (j.state === "queued" || j.state === "running") t = setTimeout(tick, 2000);
      } catch (e) { t = setTimeout(tick, 5000); }
    };
    tick();
    return () => clearTimeout(t);
  }
  async function jobAction(base, b, say, after) {
    try {
      if (b.dataset.jstop) { const j = await jcall(`${base}/camera-jobs/${encodeURIComponent(b.dataset.jstop)}/stop`, { method: "POST", body: "{}" }); say(j.message, ""); }
      else if (b.dataset.jundo) { const j = await jcall(`${base}/camera-jobs/${encodeURIComponent(b.dataset.jundo)}/undo`, { method: "POST", body: "{}" }); after(j.job); }
    } catch (e) { say(e.message, "cv-warn"); }
  }

  function standardPanel(el, base) {
    const st = { dead: false, data: null, open: "", sel: {}, job: null, stopWatch: null, msg: null, busy: false };
    el.innerHTML = `<details class="cv-setd"><summary><h3>📏 FarmCamSA standard</h3><span class="cv-dimtx">check every camera and recorder · fix one rule on many at once · undo a whole job</span></summary><div class="cv-setb" data-b></div></details>`;
    const body = el.querySelector("[data-b]");
    el.querySelector("details").addEventListener("toggle", (e) => { if (e.target.open && !st.data) load(); });
    async function load() {
      if (!st.data) body.innerHTML = `<p class="cv-dimtx">Loading…</p>`;
      try {
        st.data = await jcall(`${base}/camera-standard`);
        if (st.data.running && !st.stopWatch) follow(st.data.running.id);
        if (!st.dead) paint();
      } catch (e) { if (!st.dead) body.innerHTML = `<p class="cv-setmsg cv-warn">${esc(e.status === 404 || e.status === 501 ? "This site's Netwatch is too old for the standard — update it." : e.message)}</p>`; }
    }
    function follow(id) {
      if (st.stopWatch) st.stopWatch();
      st.stopWatch = watchJob(base, id, (j) => {
        st.job = j;
        if (j.state !== "queued" && j.state !== "running") { st.stopWatch = null; load(); } else paint();
      }, () => st.dead);
    }
    const devRows = () => ((st.data.check && st.data.check.devices) || []);
    function counts(r) {
      const n = { ok: 0, off: 0, na: 0, err: 0 };
      devRows().forEach((d) => { if (d.error) { if (r.for.includes(d.kind)) n.err++; return; } const x = d.rules[r.id]; if (x) n[x.state]++; });
      return n;
    }
    function paint() {
      const d = st.data, ck = d.check, live = st.job && (st.job.state === "queued" || st.job.state === "running");
      let h = d.can_change ? "" : `<p class="cv-setmsg">Camera settings are turned off for this site (Settings → Cameras), so the standard can't be checked or applied.</p>`;
      h += `<div class="cv-setact"><button type="button" class="cv-btn${ck ? "" : " cv-on"}" data-check${d.can_change && !live ? "" : " disabled"}>${ck ? "Check again" : "Check every camera and recorder"}</button><span class="cv-dimtx">${ck ? `Last checked ${esc(ago(ck.ts))} · ${devRows().length} devices · reads only, changes nothing` : "Reads each device one at a time and changes nothing."}</span></div>`;
      if (st.job) h += jobHtml(st.job, true);
      if (ck) {
        h += `<div class="cv-chwrap"><table class="cv-ch cv-std"><thead><tr><th>Rule</th><th>Meets</th><th>Off</th><th>Not on it</th><th></th></tr></thead><tbody>`;
        d.rules.forEach((r) => {
          const n = counts(r), open = st.open === r.id;
          h += `<tr><td><b>${esc(r.label)}</b>${r.risky ? ` <span class="cv-badge cv-warn">risky</span>` : ""}<br><small class="cv-dimtx">${esc(r.why)}</small></td><td class="cv-okt">${n.ok}</td><td class="${n.off ? "cv-warn" : ""}">${n.off}</td><td class="cv-dimtx">${n.na + n.err}</td>
            <td>${n.off || n.na + n.err ? `<button type="button" class="cv-btn" data-open="${esc(r.id)}" aria-expanded="${open}">${open ? "Hide" : "Show"}</button>` : ""}</td></tr>`;
          if (open) h += `<tr class="cv-stdx"><td colspan="5">${ruleBox(r)}</td></tr>`;
        });
        h += `</tbody></table></div>`;
      }
      const old = (d.jobs || []).filter((j) => !st.job || j.id !== st.job.id).slice(0, 6);
      if (old.length) h += `<details class="cv-xcaps"><summary>Recent jobs (${old.length})</summary>${old.map((j) => `<div class="cv-job"><p><b>${esc(j.label)}</b> — ${esc(JSTATE[j.state] || j.state)} ${esc(ago(j.finished || j.created))}${j.who ? ` by ${esc(j.who)}` : ""} · ${esc(Object.entries(j.counts || {}).map(([k, v]) => `${v} ${ISTATE[k] || k}`).join(" · "))}${j.undone_by ? " · undone" : ""}</p>${(j.kind === "rule" || j.kind === "copy") && !j.undone_by && j.state !== "running" && ((j.counts || {}).ok || (j.counts || {}).partial) ? `<div class="cv-setact"><button type="button" class="cv-btn" data-jundo="${esc(j.id)}">Undo this whole job</button></div>` : ""}</div>`).join("")}</details>`;
      h += `<p class="cv-setmsg" data-msg aria-live="polite"></p>`;
      body.innerHTML = h;
      if (st.msg) say(st.msg[0], st.msg[1]);
    }
    function ruleBox(r) {
      const rows = devRows().filter((x) => x.error ? r.for.includes(x.kind) : x.rules[r.id] && x.rules[r.id].state !== "ok");
      const sel = st.sel[r.id] || (st.sel[r.id] = new Set(rows.filter((x) => !x.error && x.rules[r.id].state === "off").map((x) => x.key)));
      let h = `<ul class="cv-stdl">${rows.map((x) => {
        const v = x.rules[r.id], off = !x.error && v.state === "off";
        return `<li>${off ? `<label><input type="checkbox" data-pick="${esc(x.key)}"${sel.has(x.key) ? " checked" : ""}> ` : "<span>"}<b>${esc(x.name)}</b>${x.kind === "recorder" ? " <small class='cv-dimtx'>(recorder)</small>" : ""}
          ${off ? v.diffs.map((f) => ` <span class="cv-dimtx">${esc(valShown(f.id, f.now))} → </span><b>${esc(valShown(f.id, f.want))}</b>`).join(";") : ` <span class="cv-dimtx">— ${esc(x.error || v.why)}</span>`}${off ? "</label>" : "</span>"}</li>`;
      }).join("")}</ul>`;
      if (st.data.can_change && rows.some((x) => !x.error && x.rules[r.id].state === "off")) {
        const blocked = r.risky && !st.data.risky;
        h += blocked ? `<p class="cv-setmsg">This rule changes logins and remote access: turn on "Risky camera work" (Settings → Cameras) first.</p>`
          : `<div class="cv-setact cv-stdgo"><label class="cv-dimtx">Pause between devices <input class="cv-pwin cv-num" data-pause type="number" min="0" max="60" value="3"> s</label>
            <label class="cv-dimtx"><input type="checkbox" data-sof> Stop at the first failure</label>
            ${r.risky ? `<span class="cv-dimtx">Type <b>${esc(st.data.confirm_word)}</b></span><input class="cv-word" data-word autocomplete="off" spellcheck="false" aria-label="Type ${esc(st.data.confirm_word)}">` : ""}
            <button type="button" class="cv-btn cv-on" data-go="${esc(r.id)}"${sel.size ? "" : " disabled"}>Change ${sel.size} device${sel.size === 1 ? "" : "s"}</button></div>
            <p class="cv-dimtx">One device at a time. Each is read back and listed in its History; the whole job can be undone afterwards.</p>`;
      }
      return h;
    }
    function say(t, c) { st.msg = [t, c]; const m = body.querySelector("[data-msg]"); if (m) { m.textContent = t; m.className = "cv-setmsg " + (c || ""); } }
    async function post(url, b) {
      if (st.busy) return null;
      st.busy = true;
      try { return await jcall(url, { method: "POST", body: JSON.stringify(b || {}) }); }
      catch (e) { say(e.message, "cv-warn"); return null; }
      finally { st.busy = false; }
    }
    body.addEventListener("click", async (e) => {
      const b = e.target.closest("button"); if (!b || b.disabled) return;
      if (b.hasAttribute("data-check")) { st.msg = null; const j = await post(`${base}/camera-standard`); if (j) { st.job = j.job; paint(); follow(j.job.id); } }
      else if (b.dataset.open) { st.open = st.open === b.dataset.open ? "" : b.dataset.open; st.msg = null; paint(); }
      else if (b.dataset.go) {
        const r = b.dataset.go, w = ((body.querySelector("[data-word]") || {}).value || "").trim().toUpperCase();
        const j = await post(`${base}/camera-jobs`, { kind: "rule", rule: r, keys: [...(st.sel[r] || [])], confirm_text: w,
          pause_s: +((body.querySelector("[data-pause]") || {}).value || 3), stop_on_fail: !!(body.querySelector("[data-sof]") || {}).checked });
        if (j) { st.msg = null; st.job = j.job; st.open = ""; delete st.sel[r]; paint(); follow(j.job.id); }
      } else if (b.dataset.jstop || b.dataset.jundo) jobAction(base, b, say, (job) => { st.job = job; st.msg = null; paint(); follow(job.id); });
    });
    body.addEventListener("change", (e) => {
      const c = e.target.closest("[data-pick]"); if (!c || !st.open) return;
      const s = st.sel[st.open]; if (c.checked) s.add(c.dataset.pick); else s.delete(c.dataset.pick);
      const g = body.querySelector("[data-go]"); if (g) { g.disabled = !s.size; g.textContent = `Change ${s.size} device${s.size === 1 ? "" : "s"}`; }
    });
    return { destroy() { st.dead = true; if (st.stopWatch) st.stopWatch(); } };
  }

  // ---- backups and what changed (Phase 6: hub/app/camconfig.py + app/hikbackup.py) --------
  // The hub keeps the device's own backup file, encrypted, with the file password it was
  // made with; snapshots of the settings Netwatch knows show what changed and when.
  const BWHY = { hand: "by hand", change: "before a risky change", auto: "nightly (settings had changed)" };
  const kb = (n) => n >= 1 << 20 ? `${(n / (1 << 20)).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`;
  function backupsPanel(el, base) {
    const st = { dead: false, data: null, busy: false, msg: null, ask: null, pw: {} };
    el.innerHTML = `<details class="cv-setd"><summary><h3>💾 Backups and changes</h3><span class="cv-dimtx">the device's own backup file · what changed in its settings · put a backup back</span></summary><div class="cv-setb" data-b></div></details>`;
    const body = el.querySelector("[data-b]");
    el.querySelector("details").addEventListener("toggle", (e) => { if (e.target.open && !st.data) load(); });
    async function load() {
      if (!st.data) body.innerHTML = `<p class="cv-dimtx">Loading…</p>`;
      try { st.data = await jcall(base); if (!st.dead) paint(); }
      catch (e) { if (!st.dead) body.innerHTML = `<p class="cv-setmsg cv-warn">${esc(e.message)}</p>`; }
    }
    const when = (ts) => new Date(ts * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
    function paint() {
      const d = st.data;
      let h = `<p class="cv-dimtx">The hub keeps the newest ${esc(d.keep)} backups, encrypted. One is taken before risky work done through the hub, and at night when the settings changed (or a month has passed).</p>
        <div class="cv-setact"><button type="button" class="cv-btn cv-on" data-bk="now"${st.busy ? " disabled" : ""}>Back up now</button>${d.snapshot ? `<span class="cv-dimtx">Settings last read ${esc(ago(d.snapshot.seen || d.snapshot.ts))}</span>` : ""}</div>`;
      h += d.backups.length ? `<div class="cv-chwrap"><table class="cv-ch"><thead><tr><th>Taken</th><th>Why</th><th>Size</th><th>Firmware</th><th></th></tr></thead><tbody>${d.backups.map((b) => `<tr>
          <td>${esc(when(b.ts))}</td><td>${esc(BWHY[b.why] || b.why)}</td><td>${esc(kb(b.bytes))}</td><td>${esc(b.firmware || "—")}</td>
          <td class="cv-chact"><a class="cv-btn" href="${esc(base + "/" + encodeURIComponent(b.name))}" download>Download</a>${b.has_password ? `<button type="button" class="cv-btn" data-pw="${esc(b.name)}">${st.pw[b.name] ? "Hide password" : "File password"}</button>` : ""}<button type="button" class="cv-btn" data-ask="${esc(b.name)}">Put back…</button>
          ${st.pw[b.name] ? `<br><code class="cv-pwshow">${esc(st.pw[b.name])}</code> <small class="cv-dimtx">Hikvision's own tools ask for it when importing the file.</small>` : ""}</td></tr>`).join("")}</tbody></table></div>`
        : `<p class="cv-dimtx">No backup yet.</p>`;
      if (st.ask) {
        const b = d.backups.find((x) => x.name === st.ask) || {};
        h += `<div class="cv-setprev"><p><b>Load the backup of ${esc(when(b.ts))} into the device?</b></p>
          <p class="cv-warn">The device takes every setting from the file — its users, passwords and network address too — and restarts (two or three minutes without pictures or recording). Use it on this device, or on a replacement of the same model (${esc(b.model || "model unknown")}). Changes made since then are lost.</p>
          <p class="cv-dimtx">Type <b>CHANGE</b> to go ahead (needs "Risky camera work").</p><div class="cv-setact"><input class="cv-word" data-word autocomplete="off" spellcheck="false" aria-label="Type CHANGE"><button type="button" class="cv-btn cv-on" data-bk="restore">Put it back</button><button type="button" class="cv-btn" data-bk="back">Back</button></div></div>`;
      }
      if (d.changes.length) {
        h += `<h4 class="cv-bkh">What changed</h4><ul class="cv-bkch">${d.changes.map((c) => `<li><b>Between ${esc(when(c.from))} and ${esc(when(c.ts))}</b><ul>${c.fields.map((f) => `<li>${esc(f.label)}${f.lens !== "1" ? ` (lens ${esc(f.lens)})` : ""}: <span class="cv-dimtx">${esc(f.before == null ? "—" : f.before)} →</span> ${esc(f.after == null ? "—" : f.after)}</li>`).join("")}${c.more ? `<li class="cv-dimtx">and ${esc(c.more)} more</li>` : ""}</ul></li>`).join("")}</ul>`;
      } else if (d.snapshot) h += `<p class="cv-dimtx">No setting has changed since Netwatch first read them.</p>`;
      h += `<p class="cv-setmsg" data-msg aria-live="polite"></p>`;
      body.innerHTML = h;
      if (st.msg) say(st.msg[0], st.msg[1]);
    }
    function say(t, c) { st.msg = [t, c]; const m = body.querySelector("[data-msg]"); if (m) { m.textContent = t; m.className = "cv-setmsg " + (c || ""); } }
    body.addEventListener("click", async (e) => {
      const b = e.target.closest("button"); if (!b || b.disabled || st.busy) return;
      if (b.dataset.bk === "now") {
        st.busy = true; paint(); say("Reading the device's backup file — a few MB, it can take a minute…", "");
        try { const j = await jcall(base, { method: "POST", body: "{}" }); st.busy = false; await load(); say(j.message, j.note ? "cv-warn" : "cv-okt"); }
        catch (err) { st.busy = false; paint(); say(err.message, "cv-warn"); }
      } else if (b.dataset.pw) {
        const n = b.dataset.pw;
        if (st.pw[n]) { delete st.pw[n]; paint(); return; }
        try { st.pw[n] = (await jcall(`${base}/${encodeURIComponent(n)}/password`)).password; paint(); } catch (err) { say(err.message, "cv-warn"); }
      } else if (b.dataset.ask) { st.ask = b.dataset.ask; st.msg = null; paint(); }
      else if (b.dataset.bk === "back") { st.ask = null; paint(); }
      else if (b.dataset.bk === "restore") {
        const w = ((body.querySelector("[data-word]") || {}).value || "").trim().toUpperCase();
        st.busy = true; say("Sending the file to the device…", "");
        try { const j = await jcall(`${base}/${encodeURIComponent(st.ask)}/restore`, { method: "POST", body: JSON.stringify({ confirm_text: w }) }); st.busy = false; st.ask = null; paint(); say(j.message, "cv-okt"); }
        catch (err) { st.busy = false; say(err.message, "cv-warn"); }
      }
    });
    return { destroy() { st.dead = true; } };
  }

  // ---- disks: SD card / hard disks, and format (Phase 6 part 3: app/hikdisks.py) ---------------
  const DISKOK = new Set(["ok", "idle", "full"]);
  function disksPanel(el, base, recorder) {
    const st = { dead: false, data: null, ask: null, busy: false, msg: null, timer: null };
    el.innerHTML = `<details class="cv-setd"><summary><h3>💽 ${recorder ? "Hard disks" : "SD card"}</h3><span class="cv-dimtx">${recorder ? "each disk's state and size · format a disk" : "the card's state and size · format it"}</span></summary><div class="cv-setb" data-b></div></details>`;
    const body = el.querySelector("[data-b]");
    el.querySelector("details").addEventListener("toggle", (e) => { if (e.target.open && !st.data) load(); });
    async function load() {
      if (!st.data) body.innerHTML = `<p class="cv-dimtx">Loading…</p>`;
      try { st.data = await jcall(base); if (!st.dead) paint(); }
      catch (e) { if (!st.dead) body.innerHTML = `<p class="cv-setmsg cv-warn">${esc(e.message)}</p>`; }
      clearTimeout(st.timer);
      if (!st.dead && st.data && st.data.job && st.data.job.state === "running") st.timer = setTimeout(load, 2500);
    }
    function paint() {
      const d = st.data, j = d.job, live = j && j.state === "running";
      let h = d.disks.length ? `<table class="cv-ch"><thead><tr><th>Disk</th><th>State</th><th>Size</th><th>Free</th><th></th></tr></thead><tbody>${d.disks.map((x) => `<tr>
          <td>${esc(x.id)}${x.model ? `<br><span class="cv-dimtx">${esc(x.model)}</span>` : ""}</td>
          <td class="${DISKOK.has(x.status) ? "cv-okt" : "cv-warn"}">${esc(x.plain)}</td>
          <td>${x.capacity_gb != null ? esc(x.capacity_gb >= 1000 ? (x.capacity_gb / 1024).toFixed(1) + " TB" : x.capacity_gb + " GB") : "—"}</td>
          <td>${x.free_gb != null ? esc(x.free_gb >= 1000 ? (x.free_gb / 1024).toFixed(1) + " TB" : x.free_gb + " GB") : "—"}</td>
          <td>${live ? "" : `<button type="button" class="cv-btn" data-dask="${esc(x.id)}">Format…</button>`}</td></tr>`).join("")}</tbody></table>`
        : `<p class="cv-dimtx">${recorder ? "The recorder lists no hard disk." : "No SD card found (the slot is empty, or this camera has none)."}</p>`;
      if (recorder && d.disks.some((x) => x.status === "ok")) h += `<p class="cv-dimtx">A full disk shows 0 free: the recorder overwrites the oldest footage. That is normal.</p>`;
      if (j) {
        h += `<div class="cv-fwjob"><p><b>${live ? "Formatting" : "Formatted"} disk ${esc(j.disk)}</b> <span class="cv-dimtx">started ${esc(ago(j.started))}</span></p>`;
        h += live ? `<p>${j.percent != null ? `${esc(j.percent)}%` : "Working…"} <span class="cv-dimtx">An SD card takes about a minute, a large hard disk a few minutes.</span></p>`
          : `<p class="cv-setmsg ${j.result === "ok" ? "cv-okt" : "cv-warn"}">${esc(j.message)}</p>`;
        h += `</div>`;
      }
      if (st.ask && !live) {
        const x = d.disks.find((y) => y.id === st.ask) || {};
        const used = DISKOK.has(x.status);
        h += `<div class="cv-setprev"><p><b>Format disk ${esc(x.id)} (${esc(x.capacity_gb)} GB, ${esc(x.plain)})?</b></p>
          <ul class="cv-warn">${used ? `<li><b>Every recording on this ${recorder ? "disk" : "card"} is deleted.</b> It can't be undone.</li>` : `<li>Anything on it is deleted (it isn't usable now, so usually nothing is lost).</li>`}
          ${recorder ? `<li>${d.disks.length > 1 ? "The recorder keeps recording on its other disks." : "This is the recorder's only disk: it records nothing until formatting is done."}</li>` : ""}
          <li>A configuration backup is taken first when this is done from the hub.</li></ul>
          <p><label><input type="checkbox" data-dwipe> I understand everything on this ${recorder ? "disk" : "card"} is deleted</label></p>
          <p class="cv-dimtx">Type <b>CHANGE</b> to go ahead (needs "Risky camera work").</p>
          <div class="cv-setact"><input class="cv-word" data-word autocomplete="off" spellcheck="false" aria-label="Type CHANGE"><button type="button" class="cv-btn cv-on" data-dgo>Format</button><button type="button" class="cv-btn" data-dback>Back</button></div></div>`;
      }
      h += `<p class="cv-setmsg" data-msg aria-live="polite"></p>`;
      body.innerHTML = h;
      if (st.msg) say(st.msg[0], st.msg[1]);
    }
    function say(t, c) { st.msg = [t, c]; const m = body.querySelector("[data-msg]"); if (m) { m.textContent = t; m.className = "cv-setmsg " + (c || ""); } }
    body.addEventListener("click", async (e) => {
      const b = e.target.closest("button"); if (!b || b.disabled || st.busy) return;
      if (b.dataset.dask) { st.ask = b.dataset.dask; st.msg = null; paint(); }
      else if (b.hasAttribute("data-dback")) { st.ask = null; paint(); }
      else if (b.hasAttribute("data-dgo")) {
        const w = ((body.querySelector("[data-word]") || {}).value || "").trim().toUpperCase(), wipe = !!(body.querySelector("[data-dwipe]") || {}).checked;
        if (!wipe) return say("Tick that you understand everything on it is deleted.", "cv-warn");
        st.busy = true; say("Taking a backup and starting…", "");
        try { const r = await jcall(`${base}/${encodeURIComponent(st.ask)}/format`, { method: "POST", body: JSON.stringify({ confirm_text: w, wipe_ok: wipe }) }); st.ask = null; st.msg = r.backup_note ? [r.backup_note, "cv-warn"] : null; await load(); }
        catch (err) { say(err.message, "cv-warn"); }
        st.busy = false;
      }
    });
    return { destroy() { st.dead = true; clearTimeout(st.timer); } };
  }

  // ---- firmware (Phase 6 part 2: hub/app/camfw.py + app/hikfw.py) — hub only ----------------
  const FWSTEP = [["backup", "Configuration backup"], ["stage", "File to the site Pi"], ["send", "File to the device"],
    ["install", "Device installs it"], ["restart", "Restart"], ["wait", "Waiting for it to answer"], ["check", "Checks"]];
  const FWCMP = { newer: ["newer than it runs", "cv-okt"], same: ["the version it runs", "cv-dimtx"], older: ["OLDER than it runs", "cv-warn"], unknown: ["can't compare", "cv-dimtx"] };
  const mb = (n) => `${((n || 0) / (1 << 20)).toFixed(n >= 100 << 20 ? 0 : 1)} MB`;
  function fwJobHtml(j) {
    if (!j) return "";
    const at = FWSTEP.findIndex(([k]) => k === j.step), live = j.state === "running";
    let h = `<div class="cv-fwjob"><p><b>${live ? "Updating" : "Update"} ${esc(j.from || "?")} → ${esc(j.to)}</b>${j.test_unit ? ` <span class="cv-warn">test unit</span>` : ""} <span class="cv-dimtx">started ${esc(ago(j.started))}</span></p><ol class="cv-fwsteps">`;
    FWSTEP.forEach(([k, label], i) => {
      const cls = !live && j.result === "ok" ? "done" : i < at ? "done" : i === at ? (live ? "now" : "stop") : "";
      let extra = "";
      if (k === "stage" && j.bytes && (i === at || j.staged)) extra = ` · ${mb(j.staged)} of ${mb(j.bytes)}${j.rate && i === at ? ` · ${mb(j.rate)}/s` : ""}`;
      if (k === "send" && i === at && j.bytes) extra = ` · ${mb(j.sent)} of ${mb(j.bytes)}`;
      if (k === "install" && i === at && j.percent != null) extra = ` · ${esc(j.percent)}%`;
      h += `<li class="${cls}">${esc(label)}${extra}</li>`;
    });
    h += `</ol>`;
    if ((j.checks || []).length) h += `<ul class="cv-fwchecks">${j.checks.map((c) => `<li class="${c.ok ? "cv-okt" : "cv-warn"}">${c.ok ? "✓" : "✗"} ${esc(c.text)}</li>`).join("")}</ul>`;
    if (!live) h += `<p class="cv-setmsg ${j.result === "ok" ? "cv-okt" : "cv-warn"}">${esc(j.message)}</p>`;
    else h += `<p class="cv-dimtx">The device stops recording while it installs and restarts (usually 3–10 minutes). You can leave this page; the hub carries on.</p>`;
    const log = [...(j.log || []), ...(j.site_log || [])].sort((a, b) => a[0] - b[0]);
    if (log.length) h += `<details class="cv-xcaps"><summary>What happened</summary><ul class="cv-fwlog">${log.map(([t, s]) => `<li><span class="cv-dimtx">${esc(new Date(t * 1000).toLocaleTimeString())}</span> ${esc(s)}</li>`).join("")}</ul></details>`;
    return h + `</div>`;
  }
  // Add a file to the library: sent in pieces (a farm-sized file passes any proxy).
  function fwAddHtml(regions, model) {
    return `<details class="cv-xcaps cv-fwadd"><summary>Add a firmware file to the library</summary>
      <p class="cv-dimtx">Download it from Hikvision for this model (a .dav file, or the .zip it comes in). Check the region on the download page: a unit bought in China needs Chinese firmware, and the other way round.</p>
      <div class="cv-fwform"><label>File <input type="file" data-fwf accept=".dav,.zip"></label>
      <label>Version <input class="cv-pwin" data-fwv placeholder="V5.7.15 build 230425" spellcheck="false"></label>
      <label>Models it is for <input class="cv-pwin" data-fwm value="${esc(model || "")}" placeholder="DS-2CD2046G2-IU, DS-2CD2*G2*" spellcheck="false"></label>
      <label>Region <select class="cv-sel" data-fwr><option value="">choose…</option>${(regions || []).map((r) => `<option>${esc(r)}</option>`).join("")}</select></label>
      <label>Notes <input class="cv-pwin" data-fwn placeholder="what the release notes say it fixes"></label></div>
      <div class="cv-setact"><button type="button" class="cv-btn cv-on" data-fwadd>Add to the library</button><span class="cv-dimtx" data-fwprog></span></div></details>`;
  }
  async function fwAdd(root, lib, say) {
    const q = (s) => root.querySelector(s), file = (q("[data-fwf]").files || [])[0];
    if (!file) return say("Choose the file first.", "cv-warn");
    const meta = { version: q("[data-fwv]").value.trim(), models: q("[data-fwm]").value, region: q("[data-fwr]").value, notes: q("[data-fwn]").value };
    if (!meta.version || !meta.models.trim() || !meta.region) return say("Fill in the version, the models and the region.", "cv-warn");
    const prog = q("[data-fwprog]");
    const b = await jcall(`${lib}/uploads`, { method: "POST", body: JSON.stringify({ name: file.name, bytes: file.size }) });
    for (let off = 0; off < file.size; off += b.piece) {
      let tries = 0;
      for (;;) {
        try {
          const r = await fetch(`${lib}/uploads/${b.id}?offset=${off}`, { method: "PUT", credentials: "same-origin", headers: { "Content-Type": "application/octet-stream" }, body: file.slice(off, off + b.piece) });
          const j = await r.json().catch(() => ({}));
          if (!r.ok || j.ok === false) throw new Error(j.error || "HTTP " + r.status);
          break;
        } catch (e) { if (++tries >= 4) throw e; await new Promise((res) => setTimeout(res, 2000 * tries)); }
      }
      prog.textContent = `${mb(Math.min(file.size, off + b.piece))} of ${mb(file.size)}`;
    }
    prog.textContent = "Checking the file…";
    const j = await jcall(`${lib}/uploads/${b.id}/finish`, { method: "POST", body: JSON.stringify(meta) });
    prog.textContent = "";
    return j.file;
  }

  function firmwarePanel(el, base, lib) {
    const st = { dead: false, data: null, ask: null, busy: false, msg: null, timer: null, regions: null };
    el.innerHTML = `<details class="cv-setd"><summary><h3>⬆ Firmware</h3><span class="cv-dimtx">the version it runs · update from the hub's library · backup first · one at a time</span></summary><div class="cv-setb" data-b></div></details>`;
    const body = el.querySelector("[data-b]");
    el.querySelector("details").addEventListener("toggle", (e) => { if (e.target.open && !st.data) load(); });
    async function load() {
      if (!st.data) body.innerHTML = `<p class="cv-dimtx">Loading…</p>`;
      try {
        st.data = await jcall(base);
        if (!st.regions) st.regions = (await jcall(lib).catch(() => ({}))).regions || [];
        if (!st.dead) paint();
      } catch (e) { if (!st.dead) body.innerHTML = `<p class="cv-setmsg cv-warn">${esc(e.message)}</p>`; }
      clearTimeout(st.timer);
      if (!st.dead && st.data && st.data.job && st.data.job.state === "running") st.timer = setTimeout(load, 3000);
    }
    function paint() {
      const d = st.data, dev = d.device, live = d.job && d.job.state === "running";
      let h = dev ? `<p>Runs <b>${esc(dev.firmware || "an unknown version")}</b> <span class="cv-dimtx">(${esc(dev.model || "model unknown")})</span></p>` : `<p class="cv-warn">This device is not in the site's camera list.</p>`;
      if (dev && !dev.own_login) h += `<p class="cv-dimtx">Netwatch updates a device through its own login. This one is reached only through its recorder: save its own login first (an admin login).</p>`;
      if (d.job) h += fwJobHtml(d.job);
      if (d.site_busy) h += `<p class="cv-dimtx">Another device on this site is being updated (${esc(d.site_busy.name)}); one at a time.</p>`;
      if (dev && !live) {
        h += d.files.length ? `<table class="cv-ch"><thead><tr><th>Version</th><th>Region</th><th>File</th><th></th></tr></thead><tbody>${d.files.map((f) => {
          const [ct, cc] = FWCMP[f.compare] || FWCMP.unknown;
          return `<tr><td><b>${esc(f.version)}</b><br><span class="${cc}">${esc(ct)}</span></td><td>${esc(f.region)}</td>
            <td>${esc(f.inner || f.file)} · ${esc(mb(f.bytes))}${f.notes ? `<br><span class="cv-dimtx">${esc(f.notes)}</span>` : ""}<br><span class="cv-dimtx">${f.proven.length ? `used on ${f.proven.length} device${f.proven.length > 1 ? "s" : ""}` : "not used yet — this device would be its test unit"}${f.failed.length ? ` · <span class="cv-warn">failed on ${f.failed.length}</span>` : ""}</span></td>
            <td>${f.compare === "newer" && dev.own_login ? `<button type="button" class="cv-btn" data-fwask="${esc(f.sha256)}">Update…</button>` : ""}</td></tr>`;
        }).join("")}</tbody></table>` : `<p class="cv-dimtx">The library has no file for ${esc(dev.model || "this model")} yet.</p>`;
      }
      if (st.ask && !live) {
        const f = d.files.find((x) => x.sha256 === st.ask) || {};
        h += `<div class="cv-setprev"><p><b>Update ${esc(dev.name)} from ${esc(dev.firmware || "?")} to ${esc(f.version)}?</b></p>
          <ul class="cv-warn"><li>A configuration backup is taken first; without one the update does not start.</li>
          <li>${dev.recorder ? "The recorder stops recording ALL its cameras" : "The camera stops sending pictures and recording"} while it installs and restarts — usually 3–10 minutes.</li>
          ${f.proven.length ? "" : `<li><b>Nobody has used this file yet: this device is its test unit.</b> Other devices wait until this update is checked. Pick a device you can reach if it doesn't come back.</li>`}
          ${dev.recorder ? "<li>Recorders are not updated at night (19:00–06:00).</li>" : ""}
          <li>Firmware can't be undone from Netwatch: going back needs the old file, and some devices refuse older versions.</li></ul>
          <p><label><input type="checkbox" data-fwregion> This device was bought for the file's region: <b>${esc(f.region)}</b></label></p>
          <p class="cv-dimtx">Type <b>CHANGE</b> to go ahead (needs "Risky camera work" on the site).</p>
          <div class="cv-setact"><input class="cv-word" data-word autocomplete="off" spellcheck="false" aria-label="Type CHANGE"><button type="button" class="cv-btn cv-on" data-fwgo>Update</button><button type="button" class="cv-btn" data-fwback>Back</button></div></div>`;
      }
      if (dev && !live) h += fwAddHtml(st.regions, dev.model);
      h += `<p class="cv-setmsg" data-msg aria-live="polite"></p>`;
      const keep = {}; body.querySelectorAll(".cv-fwform input:not([type=file]), .cv-fwform select").forEach((i) => { keep[Object.keys(i.dataset)[0]] = i.value; });
      const addOpen = !!body.querySelector(".cv-fwadd[open]"), file = body.querySelector("[data-fwf]");
      body.innerHTML = h;
      Object.entries(keep).forEach(([k, v]) => { const i = body.querySelector(`[data-${k}]`); if (i) i.value = v; });
      if (addOpen) { const a = body.querySelector(".cv-fwadd"); if (a) a.open = true; const nf = body.querySelector("[data-fwf]"); if (nf && file && file.files.length) nf.replaceWith(file); }
      if (st.msg) say(st.msg[0], st.msg[1]);
    }
    function say(t, c) { st.msg = [t, c]; const m = body.querySelector("[data-msg]"); if (m) { m.textContent = t; m.className = "cv-setmsg " + (c || ""); } }
    body.addEventListener("click", async (e) => {
      const b = e.target.closest("button"); if (!b || b.disabled || st.busy) return;
      if (b.dataset.fwask) { st.ask = b.dataset.fwask; st.msg = null; paint(); }
      else if (b.hasAttribute("data-fwback")) { st.ask = null; paint(); }
      else if (b.hasAttribute("data-fwgo")) {
        const w = ((body.querySelector("[data-word]") || {}).value || "").trim().toUpperCase(), reg = !!(body.querySelector("[data-fwregion]") || {}).checked;
        st.busy = true; say("Checking the device…", "");
        try { await jcall(base, { method: "POST", body: JSON.stringify({ sha256: st.ask, confirm_text: w, region_ok: reg }) }); st.ask = null; st.msg = null; await load(); }
        catch (err) { say(err.message, "cv-warn"); }
        st.busy = false;
      } else if (b.hasAttribute("data-fwadd")) {
        st.busy = true; b.disabled = true; say("", "");
        try { const f = await fwAdd(body, lib, say); if (f) { st.msg = [`Added ${f.version} to the library.`, "cv-okt"]; await load(); } }
        catch (err) { say(err.message, "cv-warn"); }
        st.busy = false; b.disabled = false;
      }
    });
    return { destroy() { st.dead = true; clearTimeout(st.timer); } };
  }

  // The library itself, for the all-sites Cameras page: what each file fits, where it is old.
  function firmwareLibrary(el, lib, opts) {
    const st = { dead: false, data: null, busy: false, msg: null, del: null };
    el.innerHTML = `<details class="cv-setd"><summary><h3>📦 Firmware library</h3><span class="cv-dimtx">files you added from Hikvision · which devices they fit · which run older firmware</span></summary><div class="cv-setb" data-b></div></details>`;
    const body = el.querySelector("[data-b]");
    el.querySelector("details").addEventListener("toggle", (e) => { if (e.target.open && !st.data) load(); });
    async function load() {
      try { st.data = await jcall(lib); if (!st.dead) paint(); }
      catch (e) { if (!st.dead) body.innerHTML = `<p class="cv-setmsg cv-warn">${esc(e.message)}</p>`; }
    }
    function paint() {
      const d = st.data;
      let h = `<p class="cv-dimtx">Update a device from its camera or recorder page (⬆ Firmware). A new file goes on one device first, its test unit.</p>`;
      if (d.running.length) h += `<p><b>Running now:</b> ${d.running.map((j) => `${esc(j.name)} → ${esc(j.to)}`).join(", ")}</p>`;
      h += d.files.length ? d.files.map((f) => {
        const older = f.fits.filter((x) => x.compare === "newer");
        return `<div class="cv-fwfile"><p><b>${esc(f.version)}</b> · ${esc(f.models.join(", "))} · ${esc(f.region)} <span class="cv-dimtx">· ${esc(f.inner || f.file)} · ${esc(mb(f.bytes))} · added ${esc(ago(f.added))}</span>
          <button type="button" class="cv-btn" data-fwdel="${esc(f.sha256)}">${st.del === f.sha256 ? "Really remove?" : "Remove"}</button></p>
          ${f.notes ? `<p class="cv-dimtx">${esc(f.notes)}</p>` : ""}
          <p class="cv-dimtx">Fits ${f.fits.length} device${f.fits.length === 1 ? "" : "s"}; ${older.length} run${older.length === 1 ? "s" : ""} an older version. ${f.proven.length ? `Used on ${f.proven.length}.` : "Not used yet."}${f.failed.length ? ` <span class="cv-warn">Failed on ${f.failed.length}.</span>` : ""}</p>
          ${older.length ? `<ul class="cv-stdl">${older.map((x) => `<li>${opts && opts.devHref ? `<a href="${esc(opts.devHref(x))}">` : ""}<b>${esc(x.name)}</b>${opts && opts.devHref ? "</a>" : ""} <span class="cv-dimtx">${esc(x.site_name)} · ${esc(x.firmware || "?")}${x.own_login ? "" : " · no own login"}${x.online ? "" : " · offline"}</span></li>`).join("")}</ul>` : ""}</div>`;
      }).join("") : `<p class="cv-dimtx">No firmware files yet.</p>`;
      h += fwAddHtml(d.regions, "") + `<p class="cv-setmsg" data-msg aria-live="polite"></p>`;
      body.innerHTML = h;
      if (st.msg) say(st.msg[0], st.msg[1]);
    }
    function say(t, c) { st.msg = [t, c]; const m = body.querySelector("[data-msg]"); if (m) { m.textContent = t; m.className = "cv-setmsg " + (c || ""); } }
    body.addEventListener("click", async (e) => {
      const b = e.target.closest("button"); if (!b || b.disabled || st.busy) return;
      if (b.dataset.fwdel) {
        if (st.del !== b.dataset.fwdel) { st.del = b.dataset.fwdel; paint(); return; }
        try { await jcall(`${lib}/${b.dataset.fwdel}`, { method: "DELETE" }); st.del = null; st.msg = ["Removed.", "cv-okt"]; await load(); } catch (err) { say(err.message, "cv-warn"); }
      } else if (b.hasAttribute("data-fwadd")) {
        st.busy = true; b.disabled = true;
        try { const f = await fwAdd(body, lib, say); if (f) { st.msg = [`Added ${f.version}: it fits ${f.fits.length} device(s), ${f.newer} run older firmware.`, "cv-okt"]; await load(); } }
        catch (err) { say(err.message, "cv-warn"); }
        st.busy = false; b.disabled = false;
      }
    });
    return { destroy() { st.dead = true; } };
  }

  const COPY = { picture: "Picture", osd: "Text on video (not the name)", main: "Main stream", sub: "Sub stream" };
  function copyPanel(el, base, cam, similar, views) {
    const st = { dead: false, job: null, stopWatch: null, msg: null, busy: false };
    el.innerHTML = `<details class="cv-setd"><summary><h3>📋 Copy to similar cameras</h3><span class="cv-dimtx">this camera's settings onto other ${esc(cam.model)} cameras · one at a time · with Undo</span></summary><div class="cv-setb" data-b></div></details>`;
    const body = el.querySelector("[data-b]");
    el.querySelector("details").addEventListener("toggle", (e) => { if (e.target.open && !body.innerHTML) paint(); });
    function paint() {
      const live = st.job && (st.job.state === "queued" || st.job.state === "running");
      let h = `<p class="cv-dimtx">Copies the values this camera has now. A value another camera doesn't take is left out and listed. The camera's own name is never copied.</p>
        <div class="cv-stdl2">${Object.entries(COPY).map(([k, v]) => `<label><input type="checkbox" data-sec="${k}"${k === "picture" ? " checked" : ""}> ${esc(v)}</label>`).join("")}</div>
        ${views.length > 1 ? `<p><label class="cv-dimtx">Lens <select class="cv-sel" data-lens>${views.map((n) => `<option value="${n}">${n}</option>`).join("")}</select></label></p>` : ""}
        <p class="cv-dimtx">To:</p><ul class="cv-stdl">${similar.map((x) => `<li><label><input type="checkbox" data-to="${esc(x.key)}"${x.online ? " checked" : ""}> <b>${esc(x.name)}</b>${x.online ? "" : " <span class='cv-warn'>offline</span>"}</label></li>`).join("")}</ul>
        <div class="cv-setact cv-stdgo"><label class="cv-dimtx">Pause between cameras <input class="cv-pwin cv-num" data-pause type="number" min="0" max="60" value="3"> s</label>
          <label class="cv-dimtx"><input type="checkbox" data-sof> Stop at the first failure</label>
          <button type="button" class="cv-btn cv-on" data-copygo${live ? " disabled" : ""}>Copy</button></div>`;
      if (st.job) h += jobHtml(st.job, true);
      h += `<p class="cv-setmsg" data-msg aria-live="polite"></p>`;
      const keep = [...body.querySelectorAll("input[type=checkbox]")].map((i) => [i.dataset.sec || i.dataset.to || "sof", i.checked]);
      body.innerHTML = h;
      keep.forEach(([k, v]) => { const i = body.querySelector(`[data-sec="${k}"], [data-to="${CSS.escape(k)}"]`) || (k === "sof" ? body.querySelector("[data-sof]") : null); if (i) i.checked = v; });
      if (st.msg) say(st.msg[0], st.msg[1]);
    }
    function say(t, c) { st.msg = [t, c]; const m = body.querySelector("[data-msg]"); if (m) { m.textContent = t; m.className = "cv-setmsg " + (c || ""); } }
    function follow(id) {
      if (st.stopWatch) st.stopWatch();
      st.stopWatch = watchJob(base, id, (j) => { st.job = j; paint(); }, () => st.dead);
    }
    body.addEventListener("click", async (e) => {
      const b = e.target.closest("button"); if (!b || b.disabled || st.busy) return;
      if (b.hasAttribute("data-copygo")) {
        const secs = [...body.querySelectorAll("[data-sec]:checked")].map((i) => i.dataset.sec), keys = [...body.querySelectorAll("[data-to]:checked")].map((i) => i.dataset.to);
        if (!secs.length || !keys.length) return say("Choose what to copy and the cameras to copy it to.", "cv-warn");
        st.busy = true; say("Reading this camera's settings…", "");
        try {
          const j = await jcall(`${base}/camera-jobs`, { method: "POST", body: JSON.stringify({ kind: "copy", source: cam.key, lens: +((body.querySelector("[data-lens]") || {}).value || 1),
            sections: secs, keys, pause_s: +((body.querySelector("[data-pause]") || {}).value || 3), stop_on_fail: !!(body.querySelector("[data-sof]") || {}).checked }) });
          st.msg = null; st.job = j.job; paint(); follow(j.job.id);
        } catch (err) { say(err.message, "cv-warn"); }
        st.busy = false;
      } else if (b.dataset.jstop || b.dataset.jundo) jobAction(base, b, say, (job) => { st.job = job; st.msg = null; paint(); follow(job.id); });
    });
    return { destroy() { st.dead = true; if (st.stopWatch) st.stopWatch(); } };
  }

  // ---- the ISAPI explorer (Phase 4d part 3: app/hikexplorer.py) --------------------------
  // Read any XML document; change VALUES only. Secrets come as •••••• and stay as they are.
  function explorerPanel(el, base) {
    const st = { dead: false, busy: false, data: null, prev: null, msg: null, path: "/ISAPI/System/time" };
    el.innerHTML = `<details class="cv-setd"><summary><h3>🧪 ISAPI explorer</h3><span class="cv-dimtx">any setting the panels don't cover · values only · with Undo</span></summary><div class="cv-setb" data-b></div></details>`;
    const body = el.querySelector("[data-b]");
    el.querySelector("details").addEventListener("toggle", (e) => { if (e.target.open && !body.innerHTML) paint(); });
    function paint() {
      const d = st.data;
      let h = `<p class="cv-dimtx">For experts. Every change is risky work: you preview it, type the word, and it can be undone in the change list. Restart, reset, firmware, disk format, users, ports, the address and recorder channels can't be changed here.</p>
        <div class="cv-setact"><input class="cv-pwin cv-xpath" data-xpath list="cv-xpaths" value="${esc(st.path)}" aria-label="ISAPI path" spellcheck="false"><datalist id="cv-xpaths">${((d && d.common) || []).map((p) => `<option value="${esc(p)}">`).join("")}</datalist><button type="button" class="cv-btn" data-x="read">Read</button></div>`;
      if (d) {
        h += `<textarea class="cv-xdoc" data-xdoc spellcheck="false"${d.writable ? "" : " readonly"} aria-label="Document">${esc(d.doc)}</textarea>`;
        if (d.caps) h += `<details class="cv-xcaps"><summary>What the device allows (capabilities)</summary><pre>${esc(d.caps)}</pre></details>`;
        h += d.writable ? `<div class="cv-setact"><button type="button" class="cv-btn cv-on" data-x="preview">Preview the change</button><span class="cv-dimtx">Change values only; leave ${esc(d.secret)} as it is.</span></div>`
          : `<p class="cv-dimtx">This document can be read here, not changed.</p>`;
      }
      if (st.prev) h += `<div class="cv-setprev"><p><b>Change on the device:</b></p><ul class="cv-chglist">${st.prev.map((c) => `<li><b>${esc(c.path)}</b> ${esc(c.before)} → ${esc(c.after)}</li>`).join("")}</ul>
        <p class="cv-dimtx">Type <b>${esc(st.data.confirm_word)}</b> to go ahead.</p><div class="cv-setact"><input class="cv-word" data-word autocomplete="off" spellcheck="false" aria-label="Type ${esc(st.data.confirm_word)}"><button type="button" class="cv-btn cv-on" data-x="write">Go ahead</button><button type="button" class="cv-btn" data-x="back">Back</button></div></div>`;
      h += `<p class="cv-setmsg" data-msg aria-live="polite"></p>`;
      const keep = st.data ? ((body.querySelector("[data-xdoc]") || {}).value) : null;
      body.innerHTML = h;
      if (keep != null && st.keepEdit) body.querySelector("[data-xdoc]").value = keep;
      st.keepEdit = false;
      if (st.msg) say(st.msg[0], st.msg[1]);
    }
    function say(t, c) { st.msg = [t, c]; const m = body.querySelector("[data-msg]"); if (m) { m.textContent = t; m.className = "cv-setmsg " + (c || ""); } }
    async function read() {
      st.path = ((body.querySelector("[data-xpath]") || {}).value || "").trim(); st.prev = null; st.msg = null;
      say("Reading…", "");
      try { st.data = await jcall(`${base}/isapi?path=${encodeURIComponent(st.path)}`); st.msg = null; paint(); }
      catch (e) { st.data = null; paint(); say(e.status === 404 && !e.body.http ? "This site's Netwatch is too old for the explorer — update it." : e.message, "cv-warn"); }
    }
    async function send(preview) {
      if (st.busy) return;
      const doc = (body.querySelector("[data-xdoc]") || {}).value || "", w = ((body.querySelector("[data-word]") || {}).value || "").trim().toUpperCase();
      st.doc = preview ? doc : st.doc;
      st.busy = true; say(preview ? "Working out the change…" : "Writing it to the device…", "");
      try {
        const j = await jcall(`${base}/isapi`, { method: "POST", body: JSON.stringify({ path: st.data.path, doc: st.doc, preview, confirm_text: w }) });
        st.busy = false;
        if (preview) { st.prev = j.changes.length ? j.changes : null; st.keepEdit = true; paint(); if (!j.changes.length) say(j.message, "cv-okt"); return; }
        st.prev = null; await read(); say(j.message || "Done.", j.ok ? "cv-okt" : "cv-warn");
      } catch (e) { st.busy = false; if (preview) { st.keepEdit = true; paint(); } say(e.message, "cv-warn"); }
    }
    body.addEventListener("click", (e) => {
      const b = e.target.closest("button"); if (!b || b.disabled) return;
      if (b.dataset.x === "read") read();
      else if (b.dataset.x === "preview") send(true);
      else if (b.dataset.x === "write") send(false);
      else if (b.dataset.x === "back") { st.prev = null; st.keepEdit = true; paint(); }
    });
    body.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.matches("[data-xpath]")) { e.preventDefault(); read(); } });
    return { destroy() { st.dead = true; } };
  }

  // ---- PTZ (Phase 4b): each press is one short move the camera stops by itself, so a
  // dropped connection can never leave it turning. Holding a button repeats the move.
  // ---- PTZ console: the lens that moves, steered on its own picture ----------------------
  // A PTZ camera gets its controls right under the pictures. On a 2-lens one (Tri-Lectro's
  // DS-2SE7C425MWG-EB/26: a PTZ lens + a fixed 180° lens) the moving lens is shown big and
  // the wide lens beside it as the overview. Click the moving lens's picture to point it
  // there and drag a box to zoom into it (when the camera says it can), scroll to zoom,
  // arrow keys to nudge. The camera's ~40 "special presets" are sorted into places,
  // patrols and scans, camera functions, and maintenance (reboot, limits, OSD menu), which
  // always asks first — the site refuses those without a confirm too.
  const PTZ_MODEL = /^I?DS-2(DE|DF|DY|SE|SF)/i;
  const isPtz = (c) => PTZ_MODEL.test(c.model || "") || /\bptz\b/i.test(c.name || "");
  const SETUP_NOTE = {
    92: "starts setting the pan and tilt limits by hand",
    93: "saves the pan and tilt limits set by hand",
    94: "restarts the camera — it is offline for a minute or two",
    95: "opens the camera's own menu on its video (for a joystick or a monitor)",
  };
  const clamp01 = (v) => Math.max(0, Math.min(1, v));
  // The same sorting as the site's hiksettings._preset_kind, for a site that is older
  // than this page (the hub shows every farm's cameras, and they update one at a time).
  const PTZ_TOUR = new Set([35, 36, 37, 38, 41, 42, 43, 44, 45, 96, 97, 98, 99, 100, 101, 102, 103, 104, 105]);
  const PTZ_SETUP = new Set([92, 93, 94, 95]);
  const PTZ_RISKY = /\b(reboot|restart|limits?|osd|menu|default|reset|factory|format)\b/i;     // whole words: not "Preset"
  const PTZ_FUNC = /\b(light|demist|defog|wiper|heat(er|ing)?|fan|ir|infrared|day|night|flip|origin|focus|lens|init(ial)?|track(ing)?|wash(er)?|clean(ing)?|alarm|park|home)\b/i;
  function presetKind(p) {
    if (p.kind) return p.kind;
    const id = +p.id, n = p.name || "";
    if (PTZ_SETUP.has(id) || (id >= 33 && id <= 105 && PTZ_RISKY.test(n))) return "setup";
    if (PTZ_TOUR.has(id)) return "tour";
    if (id >= 33 && id <= 46) return "function";
    if (id >= 47 && id <= 91 && PTZ_FUNC.test(n) && !/^\s*preset\s*\d+\s*$/i.test(n)) return "function";
    return "preset";
  }
  function ptzPanel(el, base, views, names, hk) {
    const st = { dead: false, lens: 0, info: null, timer: null, busy: false, speed: 50, rt: [], ask: null, drag: null, aim: null };
    (async () => {
      for (const n of views) {
        try {
          const j = await jcall(`${base}/ptz?lens=${n}`);
          if (st.dead) return;
          if (j.has_ptz) { st.lens = n; st.info = j; break; }
        } catch (e) { return; }
      }
      if (!st.info || st.dead) return;
      setup();
      paint();
    })();
    const kinds = (k) => (st.info.presets || []).filter((p) => presetKind(p) === k);
    const byId = (id) => { const p = (st.info.presets || []).find((x) => x.id === id) || { id, name: "Preset " + id }; return Object.assign({}, p, { kind: presetKind(p) }); };

    function setup() {
      const multi = views.length > 1, j = st.info;
      hk.root.classList.add("cv-hasptz");
      el.classList.add("cv-ptzsec");
      hk.stage.appendChild(el);                          // the controls sit right under the pictures
      views.forEach((n) => {
        const f = hk.fig(n); if (!f) return;
        f.classList.add(n === st.lens ? "cv-ptzlens" : "cv-overview");
        const cap = f.querySelector("figcaption");
        if (cap && multi) cap.insertAdjacentHTML("afterbegin", n === st.lens
          ? `<span class="cv-lenstag cv-lt-ptz">PTZ · this lens moves</span> ` : `<span class="cv-lenstag">Wide view · fixed</span> `);
      });
      const box = hk.box(st.lens);
      if (!box || !j.can_move) return;
      const tip = j.can_point ? "Click to point the camera here · drag a box to zoom in · scroll to zoom" : "Scroll to zoom · arrow keys move it";
      box.insertAdjacentHTML("beforeend", `<div class="cv-aim${j.can_point ? " cv-canpoint" : ""}" tabindex="0" role="application" aria-label="${esc((names[st.lens] || "PTZ lens") + ": " + tip)}">
        <span class="cv-aimtip">${esc(tip)}</span><span class="cv-aimbox" hidden></span><span class="cv-aimdot" hidden></span><span class="cv-aimbusy" hidden>Moving…</span></div>`);
      st.aim = box.querySelector(".cv-aim");
      wireAim(st.aim);
    }

    function paint() {
      const j = st.info, dis = j.can_move ? "" : " disabled", ax = j.axes || {};
      const places = kinds("preset"), tours = kinds("tour"), funcs = kinds("function"), setupL = kinds("setup");
      const stop = tours.find((p) => /\bstop\b/i.test(p.name)), runs = tours.filter((p) => p !== stop);
      const btn = (a, label, aria) => `<button type="button" class="cv-btn cv-ptzb" data-mv="${a}" aria-label="${aria}"${dis}>${label}</button>`;
      const opt = (list) => list.map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
      const live = hk.video && st.lens === 1 && j.can_move;
      el.hidden = false;
      el.innerHTML = `<div class="cv-setd cv-ptz">
        <div class="cv-ptzhead"><h3>🎮 PTZ</h3><span class="cv-dimtx">${views.length > 1 ? `moves ${esc(names[st.lens] || "lens " + st.lens)} · ` : ""}${j.route === "nvr" ? "through the recorder" : "straight to the camera"}${j.guessed ? " · the recorder didn't say what it can do, so try a small move" : ""}</span>
          ${live ? `<button type="button" class="cv-btn cv-ptzlive" data-pv>🎥 Steer with live video</button>` : ""}</div>
        ${j.can_move ? "" : `<p class="cv-setmsg">${esc(j.why_not || "")} You can look, not move.</p>`}
        <div class="cv-ptzrow">
          ${ax.pan || ax.tilt ? `<div class="cv-ptzpad"><span></span>${ax.tilt ? btn("0,1,0", "▲", "Up") : "<span></span>"}<span></span>${ax.pan ? btn("-1,0,0", "◀", "Left") : "<span></span>"}<span class="cv-ptzc">●</span>${ax.pan ? btn("1,0,0", "▶", "Right") : "<span></span>"}<span></span>${ax.tilt ? btn("0,-1,0", "▼", "Down") : "<span></span>"}<span></span></div>` : ""}
          ${ax.zoom ? `<div class="cv-ptzz">${btn("0,0,1", "+", "Zoom in")}${btn("0,0,-1", "−", "Zoom out")}</div>` : ""}
          <div class="cv-ptzside">
            <div class="cv-ptzspd"><span class="cv-dimtx">Speed</span><span class="cv-seg" role="group" aria-label="Speed">${[[20, "Slow"], [50, "Medium"], [90, "Fast"]].map(([v, l]) => `<button type="button" data-spd="${v}" aria-pressed="${v === st.speed}"${dis}>${l}</button>`).join("")}</span></div>
            ${j.can_move ? `<p class="cv-ptztip">${j.can_point ? "Or steer on the picture: <b>click</b> a spot to point the camera there, <b>drag a box</b> to zoom into it (drag up-left to zoom out), <b>scroll</b> to zoom." : "Or on the picture: <b>scroll</b> to zoom, <b>arrow keys</b> to move."} Hold a button to keep moving.</p>` : ""}
          </div>
        </div>
        <div class="cv-ptzgrp"><span class="cv-ptzl">Presets</span><div class="cv-ptzchips">${places.length ? places.map((p) => `<button type="button" class="cv-btn" data-pre="${p.id}"${dis}>${esc(p.name)}</button>`).join("") : `<span class="cv-dimtx">None saved on the camera yet.</span>`}</div></div>
        ${runs.length || stop ? `<div class="cv-ptzgrp"><span class="cv-ptzl">Patrols and scans</span><div class="cv-ptzchips">${runs.length ? `<select class="cv-q cv-setin" data-tour aria-label="Patrol or scan"${dis}>${opt(runs)}</select><button type="button" class="cv-btn" data-run="tour"${dis}>▶ Start</button>` : ""}${stop ? `<button type="button" class="cv-btn" data-pre="${stop.id}"${dis}>■ ${esc(stop.name)}</button>` : ""}</div></div>` : ""}
        ${funcs.length ? `<div class="cv-ptzgrp"><span class="cv-ptzl">Camera</span><div class="cv-ptzchips"><select class="cv-q cv-setin" data-func aria-label="Camera function"${dis}>${opt(funcs)}</select><button type="button" class="cv-btn" data-run="func"${dis}>Do it</button></div></div>` : ""}
        ${setupL.length ? `<details class="cv-ptzmaint"><summary>Maintenance <span class="cv-dimtx">· ${setupL.length} · each one asks first</span></summary><div class="cv-ptzchips">${setupL.map((p) => `<button type="button" class="cv-btn cv-ptzwarn" data-setup="${p.id}"${dis}>${esc(p.name)}</button>`).join("")}</div></details>` : ""}
        <div class="cv-ptzask" data-ask role="alertdialog" aria-live="assertive" hidden></div>
        <p class="cv-setmsg" data-pmsg aria-live="polite"></p></div>`;
    }

    const say = (t, cls) => { const m = el.querySelector("[data-pmsg]"); if (m) { m.textContent = t; m.className = "cv-setmsg " + (cls || ""); } };
    function busy(on) { const b = st.aim && st.aim.querySelector(".cv-aimbusy"); if (b) b.hidden = !on; }
    function later(ms) {                       // the moving lens's picture, once it has got there
      st.rt.forEach(clearTimeout);
      st.rt = [];
      if (hk.live() || st.dead) return;
      st.rt.push(setTimeout(() => hk.refresh(st.lens), ms));
      if (ms > 1500) st.rt.push(setTimeout(() => hk.refresh(st.lens), ms + 3000));
    }
    function ask(p) {
      const a = el.querySelector("[data-ask]"); if (!a) return;
      st.ask = p;
      if (!p) { a.hidden = true; a.innerHTML = ""; return; }
      a.hidden = false;
      a.innerHTML = `<b>⚠ ${esc(p.name)}</b><span>${esc(SETUP_NOTE[p.id] || "changes how the camera itself is set up")}.</span>
        <span class="cv-ptzyn"><button type="button" class="cv-btn cv-ptzwarn" data-yes>Yes, do it</button><button type="button" class="cv-btn" data-no>Cancel</button></span>`;
      a.querySelector("[data-no]").focus();
    }
    async function send(body, msg, settle) {
      if (st.busy) return;
      st.busy = true; busy(true);
      if (msg) say(msg);
      try {
        await jcall(`${base}/ptz`, { method: "POST", body: JSON.stringify(Object.assign({ lens: st.lens, route: st.info.route }, body)) });
        if (msg) say(body.action === "goto" ? `Sent: ${body.name || "preset " + body.preset}.` : "");
        later(settle || 1200);
      } catch (e) {
        stopHold();
        if (e.body && e.body.needs_confirm && body.action === "goto") ask(byId(body.preset));
        else say(e.message, "cv-warn");
      } finally { st.busy = false; busy(false); }
    }
    function move(dir) { const [p, t, z] = dir.split(",").map(Number); send({ action: "move", pan: p * st.speed, tilt: t * st.speed, zoom: z * st.speed, ms: 500 }); }
    function goto(p) { send({ action: "goto", preset: p.id, name: p.name }, `Going to ${p.name}…`, p.kind === "preset" ? 2500 : 1500); }
    function stopHold() { clearInterval(st.timer); st.timer = null; }

    function wireAim(a) {
      const can = st.info.can_point, ax = st.info.axes || {};
      const at = (e) => { const r = a.getBoundingClientRect(); return [clamp01((e.clientX - r.left) / r.width), clamp01((e.clientY - r.top) / r.height)]; };
      const pct = (v) => (v * 100).toFixed(2) + "%";
      function drawBox() {
        const b = a.querySelector(".cv-aimbox"), d = st.drag;
        if (!d || (Math.abs(d.p1[0] - d.p0[0]) < 0.02 && Math.abs(d.p1[1] - d.p0[1]) < 0.02)) { b.hidden = true; return; }
        b.hidden = false;
        b.classList.toggle("cv-out", d.p1[0] < d.p0[0]);
        Object.assign(b.style, { left: pct(Math.min(d.p0[0], d.p1[0])), top: pct(Math.min(d.p0[1], d.p1[1])),
          width: pct(Math.abs(d.p1[0] - d.p0[0])), height: pct(Math.abs(d.p1[1] - d.p0[1])) });
      }
      function dot(p) {
        const m = a.querySelector(".cv-aimdot");
        Object.assign(m.style, { left: pct(p[0]), top: pct(p[1]) });
        m.hidden = false;
        clearTimeout(m._t); m._t = setTimeout(() => { m.hidden = true; }, 1600);
      }
      a.addEventListener("pointerdown", (e) => {
        if (!can || e.button !== 0) return;
        e.preventDefault();
        try { a.setPointerCapture(e.pointerId); } catch (err) { /* synthetic events */ }
        const p = at(e);
        st.drag = { p0: p, p1: p };
      });
      a.addEventListener("pointermove", (e) => { if (st.drag) { st.drag.p1 = at(e); drawBox(); } });
      a.addEventListener("pointerup", () => {
        const d = st.drag; if (!d) return;
        st.drag = null; drawBox();
        const spot = Math.abs(d.p1[0] - d.p0[0]) < 0.02 && Math.abs(d.p1[1] - d.p0[1]) < 0.02;
        if (spot) dot(d.p0);
        send({ action: "point", box: spot ? [d.p0[0], d.p0[1], d.p0[0], d.p0[1]] : [d.p0[0], d.p0[1], d.p1[0], d.p1[1]] },
          spot ? "Pointing the camera there…" : d.p1[0] < d.p0[0] ? "Zooming out…" : "Zooming into the box…", 1600);
      });
      a.addEventListener("pointercancel", () => { st.drag = null; drawBox(); });
      a.addEventListener("wheel", (e) => { if (!ax.zoom) return; e.preventDefault(); move(`0,0,${e.deltaY < 0 ? 1 : -1}`); }, { passive: false });
      a.addEventListener("keydown", (e) => {
        const k = { ArrowUp: "0,1,0", ArrowDown: "0,-1,0", ArrowLeft: "-1,0,0", ArrowRight: "1,0,0", "+": "0,0,1", "=": "0,0,1", "-": "0,0,-1", PageUp: "0,0,1", PageDown: "0,0,-1" }[e.key];
        if (k) { e.preventDefault(); move(k); }
      });
    }

    el.addEventListener("pointerdown", (e) => {
      const b = e.target.closest("[data-mv]"); if (!b || b.disabled) return;
      e.preventDefault(); stopHold(); move(b.dataset.mv);
      st.timer = setInterval(() => move(b.dataset.mv), 450);
    });
    ["pointerup", "pointerleave", "pointercancel"].forEach((ev) => el.addEventListener(ev, stopHold));
    el.addEventListener("keydown", (e) => {
      const b = e.target.closest("[data-mv]"); if (b && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); move(b.dataset.mv); }
      if (e.key === "Escape" && st.ask) ask(null);
    });
    el.addEventListener("click", (e) => {
      const t = e.target;
      const sp = t.closest("[data-spd]");
      if (sp && !sp.disabled) { st.speed = +sp.dataset.spd; el.querySelectorAll("[data-spd]").forEach((x) => x.setAttribute("aria-pressed", String(x === sp))); return; }
      if (t.closest("[data-pv]")) { if (hk.video) hk.video.click(); return; }
      const pre = t.closest("[data-pre]");
      if (pre && !pre.disabled) { goto(byId(+pre.dataset.pre)); return; }
      const run = t.closest("[data-run]");
      if (run && !run.disabled) { const s = el.querySelector(run.dataset.run === "tour" ? "[data-tour]" : "[data-func]"); if (s) goto(byId(+s.value)); return; }
      const su = t.closest("[data-setup]");
      if (su && !su.disabled) { ask(byId(+su.dataset.setup)); return; }
      if (t.closest("[data-no]")) { ask(null); say(""); return; }
      if (t.closest("[data-yes]") && st.ask) { const p = st.ask; ask(null); send({ action: "goto", preset: p.id, name: p.name, confirm: true }, `${p.name}…`, 2500); }
    });
    return { destroy() { st.dead = true; stopHold(); st.rt.forEach(clearTimeout); } };
  }

  window.CamView = { mount, camera, ago, firmwareLibrary, fetchPic: (lid, url) => queue(lid, () => fetchPic(url)) };
})();
