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
    chan_offline: "Recorder lost it", not_supported: "Not Hikvision", failed: "Last read failed", off: "Pictures off",
  };
  const ROUTE = { direct: "its own login", nvr: "its recorder", saved: "the saved copy", "hub-saved": "the hub's saved copy" };
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

  const aspectOf = (c, p, learned) => c.aspect || learned || (p && p.route === "direct" && p.w && p.h ? p.w / p.h : 0) || 16 / 9;
  // Every tile has the same 16:9 frame; the picture sits inside it in the camera's own shape.
  const TILE = 16 / 9;
  const fitStyle = (a) => (a >= TILE ? `width:100%;height:${(100 * TILE / a).toFixed(2)}%` : `height:100%;width:${(100 * a / TILE).toFixed(2)}%`);

  function needsLook(c) { return !c.route || !c.online || c.why === "failed"; }

  // ---- the grid --------------------------------------------------------------------------
  function mount(el, opts) {
    const pre = opts.storeKey || "cv";
    const st = {
      // The filter always starts at All: a remembered one hid cameras without saying so.
      q: "", filt: "all", size: mem.get(pre + ".size", "m"),
      pics: new Map(), learned: new Map(), keepUntil: 0, dead: false, obs: null, timers: [], nvr: opts.nvr || "",
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
      if (st.filt === "own" && c.route !== "direct") return false;
      if (!st.q) return true;
      const hay = [c.name, c.ip, c.model, c.mac, c.nvr && c.nvr.name, g.title].join(" ").toLowerCase();
      return st.q.split(/\s+/).every((w) => hay.includes(w));
    };

    function chips() {
      const all = groups().flatMap((g) => camsOf(g).filter((c) => !st.nvr || (c.nvr && c.nvr.key === st.nvr)));
      const n = { all: all.length, look: all.filter(needsLook).length, nvr: all.filter((c) => c.route === "nvr").length, own: all.filter((c) => c.route === "direct").length };
      const lbl = { all: "All", look: "Need a look", own: "Picture via own login", nvr: "Picture via recorder" };
      root.querySelector(".cv-chips").innerHTML = Object.keys(lbl).map((k) =>
        `<button type="button" class="cv-chip ${st.filt === k ? "cv-on" : ""}" data-filt="${k}" aria-pressed="${st.filt === k}">${lbl[k]} <span>${n[k]}</span></button>`).join("")
        + (st.nvr ? `<button type="button" class="cv-chip cv-on" data-clear-nvr title="Show every recorder">Recorder only ✕</button>` : "");
      root.querySelectorAll(".cv-seg button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.size === st.size)));
    }

    function tileHtml(g, c) {
      const k = g.id + "|" + c.key, p = st.pics.get(k);
      const asp = aspectOf(c, p, st.learned.get(k));
      const badges = (c.why && WHY[c.why] ? `<span class="cv-badge ${c.why === "offline" ? "cv-bad" : "cv-warn"}">${esc(WHY[c.why])}</span>` : "")
        + (c.route === "nvr" ? `<span class="cv-badge cv-via" title="${esc("Through " + ((c.nvr && c.nvr.name) || "its recorder") + (c.nvr ? ", channel " + c.nvr.channel : ""))}">NVR</span>` : "");
      const ph = c.route || c.picture ? "Loading…" : esc(c.why_text || "No picture");
      return `<article class="cv-tile ${c.online ? "" : "cv-off"}" data-k="${esc(k)}" tabindex="0" role="button" aria-label="${esc(c.name)}">
        <div class="cv-pic cv-frame"><div class="cv-fit" style="${fitStyle(asp)}"><img alt="" hidden></div><div class="cv-ph">${ICON}<span>${ph}</span></div><span class="cv-age" hidden></span></div>
        <div class="cv-cap"><b title="${esc(c.name)}">${esc(c.name)}</b><small>${esc([c.ip, c.model].filter(Boolean).join(" · ") || "—")}</small>${badges ? `<span class="cv-badges">${badges}</span>` : ""}</div>
      </article>`;
    }

    function recHtml(g, r) {
      const s = !r.online ? ["cv-bad", "Offline"] : r.login_refused ? ["cv-warn", "Login refused"] : !r.has_login ? ["cv-warn", "No login saved"]
        : r.channels ? [r.channels_offline ? "cv-warn" : "cv-ok", `${r.channels_online} of ${r.channels} cameras connected`] : ["cv-dim", "Reading its cameras…"];
      return `<button type="button" class="cv-rec ${st.nvr === r.key ? "cv-on" : ""}" data-nvr="${esc(r.key)}" title="Show only this recorder's cameras">
        <span class="cv-dot ${s[0]}"></span><span class="cv-rtx"><b>${esc(r.name)}</b><small>${esc([r.model, r.ip].filter(Boolean).join(" · "))}</small><span class="cv-rst ${s[0]}">${esc(s[1])}</span></span></button>`;
    }

    function groupHtml(g) {
      const d = g.data;
      const head = opts.fleet ? `<h2 class="cv-gh">${esc(g.title)}${g.href ? ` <a href="${esc(g.href)}">open site</a>` : ""}</h2>` : "";
      if (!d || d.__loading) return `<section class="cv-group">${head}<p class="cv-msg">Loading cameras…</p></section>`;
      if (d.__error) return `<section class="cv-group">${head}<p class="cv-msg cv-warn">${esc(d.__error)}</p></section>`;
      if (d.enabled === false) return `<section class="cv-group">${head}<p class="cv-msg">Camera pictures are turned off on this site (its Settings → Cameras).</p></section>`;
      const cams = camsOf(g).filter((c) => visible(g, c));
      const recs = (d.recorders || []);
      // Say when a filter hides cameras, and how many (e.g. a recorder's 19, of which 12 come via it).
      const scope = camsOf(g).filter((c) => !st.nvr || (c.nvr && c.nvr.key === st.nvr));
      const rec = st.nvr && recs.find((r) => r.key === st.nvr);
      const on = [rec ? `recorder ${rec.name}` : "", st.filt !== "all" ? { look: "Need a look", own: "Picture via own login", nvr: "Picture via recorder" }[st.filt] : "", st.q ? `search "${st.q}"` : ""].filter(Boolean);
      const note = on.length ? `<p class="cv-shown">Showing ${cams.length} of ${st.filt !== "all" || st.q ? scope.length : camsOf(g).length} cameras — ${esc(on.join(" · "))} <button type="button" class="cv-linkb" data-show-all>Show all</button></p>` : "";
      return `<section class="cv-group">${head}
        ${recs.length ? `<div class="cv-recs">${recs.map((r) => recHtml(g, r)).join("")}</div>` : ""}
        ${note}
        ${cams.length ? `<div class="cv-grid cv-${esc(st.size)}">${cams.map((c) => tileHtml(g, c)).join("")}</div>`
          : `<p class="cv-msg">${camsOf(g).length ? "No camera matches." : "Netwatch has not found any cameras here yet."}</p>`}
      </section>`;
    }

    function paint(tile, p) {
      const img = tile.querySelector("img"), age = tile.querySelector(".cv-age"), ph = tile.querySelector(".cv-ph");
      if (p && p.src) {
        if (img.getAttribute("src") !== p.src) img.src = p.src;
        img.hidden = false; ph.hidden = true;
        age.hidden = false;
        age.textContent = (p.stale ? "Saved · " : "") + ago(p.ts);
        tile.classList.toggle("cv-stale", !!p.stale);
      } else if (p && p.err) {
        ph.hidden = false; img.hidden = true; age.hidden = true;
        ph.querySelector("span").textContent = p.err;
      }
    }

    function render() {
      if (st.dead) return;
      chips();
      body.innerHTML = groups().map(groupHtml).join("");
      body.querySelectorAll(".cv-tile").forEach((t) => {
        const p = st.pics.get(t.dataset.k);
        if (p) paint(t, p);
        st.obs.observe(t);
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
      const { g, c } = hit, cur = st.pics.get(k);
      if (cur && cur.loading) return;
      if (!c.route && !c.picture && !(cur && cur.src)) return;
      st.pics.set(k, { ...(cur || {}), loading: true });
      queue(g.id, () => fetchPic(opts.picUrl(g, c, { size: "thumb", fresh })))
        .then((p) => {
          if (st.dead) { URL.revokeObjectURL(p.src); return; }
          const old = st.pics.get(k);
          if (old && old.src) setTimeout(() => URL.revokeObjectURL(old.src), 1000);
          st.pics.set(k, { ...p, loading: false, at: Date.now() });
          const t = body.querySelector(`.cv-tile[data-k="${CSS.escape(k)}"]`);
          if (t) {
            if (!c.aspect && p.route === "direct" && p.w && p.h) t.querySelector(".cv-fit").setAttribute("style", fitStyle(p.w / p.h));
            paint(t, st.pics.get(k));
          }
        })
        .catch((e) => {
          const old = st.pics.get(k) || {};
          st.pics.set(k, { ...old, loading: false, at: Date.now(), err: old.src ? "" : (WHY[e.why] || e.message || "No picture") });
          const t = body.querySelector(`.cv-tile[data-k="${CSS.escape(k)}"]`);
          if (t && !old.src) paint(t, st.pics.get(k));
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
      body.querySelectorAll(".cv-tile").forEach((t) => { const p = st.pics.get(t.dataset.k); if (p && p.src) paint(t, p); });
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
    const st = { pic: null, full: false, until: 0, timer: null, dead: false, aspect: aspectOf(c, null, 0) };
    const rec = c.nvr ? `${esc(c.nvr.name || c.nvr.ip)} · channel ${esc(c.nvr.channel)}${c.nvr.chan_online === false ? " · <span class='cv-warn'>not connected</span>" : ""}` : "—";
    const status = c.online ? `<span class="cv-badge cv-ok">Online</span>` : `<span class="cv-badge cv-bad">Offline</span>`;
    el.innerHTML = `<div class="cv cv-one">
      <div class="cv-ohead">
        <button type="button" class="cv-btn" data-act="back">← All cameras</button>
        <div class="cv-otitle"><h2>${esc(c.name)}</h2><p>${status}${c.why && c.why !== "offline" && WHY[c.why] ? ` <span class="cv-badge cv-warn">${esc(WHY[c.why])}</span>` : ""}${c.route === "nvr" ? ` <span class="cv-badge cv-via">NVR</span>` : ""}${g.title ? ` <span class="cv-dimtx">${esc(g.title)}</span>` : ""}</p></div>
      </div>
      <div class="cv-ogrid">
        <div class="cv-stage">
          <div class="cv-pic cv-big" style="aspect-ratio:${st.aspect.toFixed(4)}"><img alt="${esc(c.name)}" hidden><div class="cv-ph">${ICON}<span>${c.route || c.picture ? "Loading…" : esc(c.why_text || "No picture")}</span></div><span class="cv-age" hidden></span></div>
          <div class="cv-acts">
            <button type="button" class="cv-btn" data-act="fresh">↻ New picture</button>
            <button type="button" class="cv-btn" data-act="live">▶ Keep refreshing (2 min)</button>
            <button type="button" class="cv-btn" data-act="full">🔍 Sharper picture</button>
            <a class="cv-btn" data-act="open" href="${esc(opts.picUrl(g, c, { size: "full" }))}" target="_blank" rel="noopener">Full size ↗</a>
            ${opts.deviceAction ? `<button type="button" class="cv-btn" data-act="device">${esc(opts.deviceAction.label)}</button>` : ""}
          </div>
          <p class="cv-note" aria-live="polite"></p>
        </div>
        <dl class="cv-facts">
          <dt>IP address</dt><dd class="cv-mono">${esc(c.ip || "—")}</dd>
          ${c.mac ? `<dt>MAC</dt><dd class="cv-mono">${esc(c.mac)}</dd>` : ""}
          <dt>Model</dt><dd>${esc(c.model || "—")}</dd>
          ${c.firmware ? `<dt>Firmware</dt><dd class="cv-mono">${esc(c.firmware)}</dd>` : ""}
          <dt>Recorder</dt><dd>${rec}</dd>
          <dt>Picture from</dt><dd data-f="route">${c.route ? esc(ROUTE[c.route] || c.route) : esc(c.why_text || "—")}</dd>
          <dt>Picture</dt><dd data-f="pic">—</dd>
          ${c.last_seen && !c.online ? `<dt>Last seen</dt><dd>${esc(ago(c.last_seen))}</dd>` : ""}
          ${c.why_text && c.why !== "offline" ? `<dt>Note</dt><dd>${esc(c.why_text)}${c.fail ? ` <span class="cv-dimtx">(${esc(c.fail)})</span>` : ""}</dd>` : ""}
        </dl>
      </div>
      <p class="cv-foot">Camera settings (picture, video, detection, PTZ…) come in the next phases. Pictures are kept in memory only, apart from one saved copy per camera so an offline camera still shows what it looks at.</p>
    </div>`;
    const root = el.querySelector(".cv"), box = root.querySelector(".cv-pic"), img = box.querySelector("img");
    const note = root.querySelector(".cv-note");

    function show(p) {
      if (st.pic && st.pic.src && st.pic.src !== p.src) { const old = st.pic.src; setTimeout(() => URL.revokeObjectURL(old), 1000); }
      st.pic = p;
      img.src = p.src; img.hidden = false; box.querySelector(".cv-ph").hidden = true;
      if (!c.aspect && p.w && p.h && (p.route === "direct" || st.full)) { st.aspect = p.w / p.h; box.style.aspectRatio = st.aspect.toFixed(4); }
      tick();
    }
    function tick() {
      const p = st.pic; if (!p) return;
      const age = box.querySelector(".cv-age");
      age.hidden = false; age.textContent = (p.stale ? "Saved · " : "") + ago(p.ts);
      root.querySelector('[data-f="pic"]').textContent = [p.w && p.h ? `${p.w}×${p.h}` : "", p.bytes ? Math.round(p.bytes / 1024) + " KB" : "",
        ago(p.ts), p.stale ? "saved copy" : "", ROUTE[p.route] ? "from " + ROUTE[p.route] : ""].filter(Boolean).join(" · ");
      const lv = root.querySelector('[data-act="live"]'), left = st.until - Date.now();
      lv.classList.toggle("cv-on", left > 0);
      lv.textContent = left > 0 ? `■ Stop (${Math.ceil(left / 1000)} s)` : "▶ Keep refreshing (2 min)";
    }
    async function get(size, fresh) {
      try {
        const p = await queue(g.id, () => fetchPic(opts.picUrl(g, c, { size, fresh })));
        if (st.dead) { URL.revokeObjectURL(p.src); return; }
        note.textContent = p.stale ? (c.online ? "The camera did not answer — this is its last saved picture." : "The camera is offline — this is its last saved picture.") : "";
        show(p);
      } catch (e) {
        if (st.dead) return;
        note.textContent = WHY[e.why] ? WHY[e.why] + (e.message ? " — " + e.message : "") : e.message || "No picture";
        if (!st.pic) box.querySelector(".cv-ph span").textContent = note.textContent;
      }
    }
    function stopLive() { clearTimeout(st.timer); st.timer = null; st.until = 0; tick(); }
    function loop() {
      if (st.dead || Date.now() > st.until) return stopLive();
      get("thumb", true).finally(() => { if (st.until && !st.dead) st.timer = setTimeout(loop, 2000); });
    }

    root.addEventListener("click", (e) => {
      const a = e.target.closest("[data-act]");
      if (!a) return;
      const act = a.dataset.act;
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
    return {
      destroy() {
        st.dead = true; clearInterval(ages); clearTimeout(st.timer);
        if (st.pic && st.pic.src) URL.revokeObjectURL(st.pic.src);
        el.innerHTML = "";
      },
    };
  }

  window.CamView = { mount, camera, ago, fetchPic: (lid, url) => queue(lid, () => fetchPic(url)) };
})();
