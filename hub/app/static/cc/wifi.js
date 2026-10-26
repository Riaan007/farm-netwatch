/* Netwatch Control Center — wireless links: the fleet Wi-Fi page and a site's Wi-Fi tab.
 *
 * The diagnosis itself is worked out ON THE SITE (app/wifidiag.py) so the farm's
 * own page and the Control Center always say the same thing; this file only
 * shows it. The hub loads a light 7-day summary of every site on a slow timer
 * (core.js → S.wifiLinks) and fetches one link's chart series only when that
 * link is opened, because every byte crosses a farm's radio link.
 */
(function () {
  "use strict";
  const { $, $$, esc, icon, api, state: S } = CC;
  const view = () => $("#view");
  const enc = encodeURIComponent;

  const GRADE = { good: ["Healthy", "ok"], watch: ["Watch", "info"], warn: ["Needs attention", "warn"], crit: ["Poor", "bad"] };
  const RANK = { crit: 0, warn: 1, watch: 2, good: 3 };
  const WHERE = { remote: "From the office", site: "On site" };
  const num = (v, dp = 0) => (v == null || isNaN(v) ? "—" : Number(v).toFixed(dp));
  const sigCls = (v) => (v == null ? "dim" : v <= -82 ? "bad" : v <= -75 ? "warn" : v >= -65 ? "ok" : "cyan");
  const scoreCls = (v) => (v == null ? "dim" : v < 35 ? "bad" : v < 50 ? "warn" : v >= 80 ? "ok" : "cyan");
  const poorBg = (v) => (v == null ? "rgba(148,163,184,.08)" : v >= 50 ? "#e11d48" : v >= 35 ? "#f97316" : v >= 20 ? "#f59e0b" : v >= 8 ? "#eab308aa" : "#10b981");
  const km = (m) => (m == null ? "—" : m >= 1000 ? (m / 1000).toFixed(1) + " km" : Math.round(m) + " m");
  const gradeBadge = (g, short) => `<span class="b ${GRADE[g][1]} nodot" title="${GRADE[g][0]}">${short ? { good: "OK", watch: "Watch", warn: "Attention", crit: "Poor" }[g] : GRADE[g][0]}</span>`;
  const mbps = (kbps) => (kbps ? Math.round(kbps / 1000) + " Mbps" : "—");
  const HOURS_KEY = "cc.wifi.hours";
  let HOURS = 168;
  try { const h = +localStorage.getItem(HOURS_KEY); if ([24, 168, 720].includes(h)) HOURS = h; } catch (e) { /* private mode */ }

  // ---- derived figures (used by core's attention rules and the overview too) -----------
  /** Wireless state of one site: { state: none|legacy|loading|error|ok, links, attn, crit, radioIssues, ... } */
  CC.wifiOf = (id) => {
    const w = S.wifiLinks[id];
    if (!w) return { state: "loading" };
    if (w.legacy) return { state: "legacy", w };
    if (!w.summary) return { state: w.error ? "error" : "loading", w };
    const links = w.links || [];
    const radioIssues = (w.radios || []).flatMap((r) => (r.findings || []).filter((f) => f.level !== "info").map((f) => ({ ...f, radio: r })));
    if (!(w.radios || []).length) return { state: "none", w, links, radioIssues };
    const crit = links.filter((l) => l.grade === "crit");
    const warn = links.filter((l) => l.grade === "warn");
    return { state: "ok", w, links, crit, warn, attn: crit.length + warn.length, radioIssues, summary: w.summary };
  };
  /** Links needing attention across the fleet, worst first: [{ site, link }] */
  CC.wifiAttention = () => S.sites.filter((s) => s.enabled).flatMap((s) => {
    const x = CC.wifiOf(s.id);
    return x.state === "ok" ? x.links.filter((l) => l.grade === "crit" || l.grade === "warn").map((l) => ({ site: s, link: l })) : [];
  }).sort((a, b) => RANK[a.link.grade] - RANK[b.link.grade] || (a.link.quality ?? 100) - (b.link.quality ?? 100));

  const topFinding = (l) => (l.findings || []).find((f) => f.id !== "blind_end") || (l.findings || [])[0];
  const linkHref = (siteId, l) => `#/site/${enc(siteId)}/wifi?link=${enc(l.id)}`;

  // ---- shared renderers ---------------------------------------------------------------
  function kpis(sum, { linksHref, worst, sitesNote } = {}) {
    const attn = (sum.warn || 0) + (sum.crit || 0);
    const unread = (sum.radios || 0) - (sum.radios_read || 0);
    const k = (label, v, s, cls, href) => `<${href ? `a href="${href}"` : "div"} class="kpi ${cls || ""}"><div class="k">${label}</div><div class="v">${v}</div><div class="s">${esc(s)}</div></${href ? "a" : "div"}>`;
    return `<section class="kpis" aria-label="Wireless summary">
      ${k("Wireless links", `${sum.links || 0}`, `${sum.good || 0} healthy${sitesNote ? " · " + sitesNote : ""}`, "", linksHref)}
      ${k("Need attention", attn, sum.crit ? `${sum.crit} poor` : attn ? "none poor" : "nothing to fix", sum.crit ? "is-bad" : attn ? "is-warn" : "is-ok")}
      ${k("Weakest link", worst ? `${worst.link.quality}<small>/100</small>` : "—", worst ? `${worst.label}` : "no link scores yet", worst && worst.link.quality < 50 ? "is-warn" : "", worst ? worst.href : "")}
      ${k("From the office", sum.remote_fixes || 0, "fixes without a trip", sum.remote_fixes ? "is-ok" : "")}
      ${k("Site visits", sum.site_fixes || 0, "need someone on site", "")}
      ${k("Radios read", `${sum.radios_read || 0}<small>/${sum.radios || 0}</small>`, unread ? `${unread} can't be read` : "all readable", unread ? "is-warn" : "")}
    </section>`;
  }

  /** items: [{ ...fix, site }] */
  function fixList(items, { showSite, showInfo, onToggleInfo } = {}) {
    const real = items.filter((f) => f.level !== "info"), info = items.filter((f) => f.level === "info");
    const shown = showInfo ? items : real;
    const href = (f) => (f.link ? linkHref(f.site.id, { id: f.link }) : `#/site/${enc(f.site.id)}/wifi`);
    return (shown.length ? `<ul class="alist">${shown.map((f) => `
      <li><span class="sev ${f.level === "crit" ? "bad" : f.level === "warn" ? "warn" : "info"}">${f.level === "info" ? "i" : "!"}</span>
        <div style="min-width:0"><div class="t">${esc(f.title)} <span class="note">· ${showSite ? `<b class="cyan" style="font-weight:600">${esc(f.site.name)}</b> · ` : ""}${esc(f.target)}</span></div>
          <div class="d">${esc(f.first)}${f.more ? ` <span class="dim">(+${f.more} more finding${f.more === 1 ? "" : "s"})</span>` : ""}</div></div>
        <div class="wf-fixend"><span class="where ${f.where}">${WHERE[f.where] || ""}</span><a class="btn sm" href="${href(f)}">Open</a></div></li>`).join("")}</ul>`
      : `<div class="empty"><b>Nothing to fix</b>No wireless work is waiting${showSite ? " at any site" : ""}.</div>`)
      + (info.length ? `<button class="more" data-wf-info>${showInfo ? "Hide" : "Show"} ${CC.plural(info.length, "suggestion")} to watch</button>` : "");
  }

  function sigBar(label, v) {
    const pct = v == null ? 0 : Math.max(4, Math.min(100, ((v + 92) / 50) * 100));
    const col = v == null ? "#475569" : v <= -82 ? "var(--bad)" : v <= -75 ? "var(--warn)" : v >= -65 ? "var(--ok)" : "var(--cyan)";
    return `<span class="l">${esc(label)}</span><b class="${sigCls(v)}">${v == null ? "—" : num(v)}</b><i><s style="width:${pct}%;background:${col}"></s></i>`;
  }

  function findingHtml(f) {
    return `<div class="wf-find ${f.level}">
      <h4><span class="lv ${f.level}"></span>${esc(f.title)}</h4>
      ${(f.evidence || []).length ? `<ul class="ev">${f.evidence.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>` : ""}
      <div class="fcols">
        <div><h5>Likely causes</h5><ol>${(f.causes || []).map((c) => `<li>${esc(c)}</li>`).join("")}</ol></div>
        <div><h5>What to do</h5><ol class="steps">${(f.steps || []).map((st) => {
          const c = st.check && (st.check.radios || []).length ? st.check : null;
          return `<li><span>${esc(st.text)}</span><span class="wf-chkrow"><span class="where ${st.where}">${WHERE[st.where]}</span>${c
            ? `<button type="button" class="wf-chkbtn" data-wf-check data-kinds="${esc(c.kinds.join(","))}" data-radios="${esc(c.radios.join(","))}" title="Read the answer from the radio now — nothing is changed">Check now</button>` : ""}${st.fix
            ? `<button type="button" class="wf-chkbtn fix" data-wf-fix="${esc(JSON.stringify(st.fix))}" title="Preview the change first — nothing is changed until you apply it">Fix…</button>` : ""}</span>${c ? `<div class="wf-chkres" hidden aria-live="polite"></div>` : ""}${st.fix ? `<div class="wf-chkres wf-fixres" hidden aria-live="polite"></div>` : ""}</li>`;
        }).join("")}</ol></div>
      </div>
      ${f.good ? `<p class="good">✓ Good result: ${esc(f.good)}</p>` : ""}
    </div>`;
  }

  // "Check now" on a From-the-office step: the site reads the radios (read-only).
  async function runCheck(site, btn) {
    const out = btn.closest("li").querySelector(".wf-chkres");
    btn.disabled = true; btn.textContent = "Checking…";
    out.hidden = false; out.innerHTML = `<div class="ts">Reading the radio over SSH…</div>`;
    const fact = (f) => `<div class="f ${esc(f.level || "")}"><span>${esc(f.label)}</span>${String(f.value).includes("\n") ? `<pre>${esc(f.value)}</pre>` : `<b>${esc(f.value)}</b>`}</div>`;
    try {
      const j = await CC.api(`/api/hub/sites/${encodeURIComponent(site.id)}/wifi/check`, { method: "POST", timeout: 70000,
        body: { kinds: btn.dataset.kinds.split(","), radios: btn.dataset.radios.split(",") } });
      const sum = (j.summary || []).map(fact).join("");
      out.innerHTML = (j.results || []).map((r) => `<div><h6>${esc(r.name)} <span class="ip">${esc(r.ip || "")}</span></h6>${r.ok
        ? (r.facts || []).map(fact).join("") : `<div class="f bad"><b>${esc(r.error || "could not read")}</b></div>`}</div>`).join("")
        + (sum ? `<div class="sum">${sum}</div>` : "") + `<div class="ts">Read from the radio at ${new Date().toLocaleTimeString()} · nothing was changed</div>`;
    } catch (e) { out.innerHTML = `<div class="f bad"><b>${esc(e.message)}</b></div>`; }
    btn.disabled = false; btn.textContent = "Check again";
  }

  // "Fix…" on a step: preview (writes nothing) → Apply now → follow the radio until it saves or undoes.
  async function runFix(site, btn) {
    const fix = JSON.parse(btn.dataset.wfFix || "{}"), out = btn.closest("li").querySelector(".wf-fixres");
    const base = `/api/hub/sites/${encodeURIComponent(site.id)}/wifi/fix`;
    const msg = (t, cls) => `<div class="f ${cls || ""}"><b>${esc(t)}</b></div>`;
    out.hidden = false;
    const follow = async () => {
      for (;;) {
        let job = {};
        try { job = (await CC.api(`${base}/${encodeURIComponent(fix.radio)}`)).job || {}; } catch (e) { /* the radio's link may be restarting */ }
        if (job.phase === "done") {
          out.innerHTML = msg(job.msg || job.verdict, job.ok ? "ok" : job.verdict === "reverted" ? "warn" : "bad")
            + ((job.log || []).length ? `<details><summary class="ts">Radio log</summary><pre>${esc(job.log.join("\n"))}</pre></details>` : "");
          return;
        }
        out.innerHTML = `<div class="ts">⏳ ${esc(job.msg || "Working…")}</div>`;
        await new Promise((r) => setTimeout(r, 3000));
      }
    };
    const preview = async (params) => {
      out.innerHTML = `<div class="ts">Reading the radios to build the plan — nothing is changed…</div>`;
      try {
        const j = await CC.api(`${base}/preview`, { method: "POST", timeout: 100000, body: { kind: fix.kind, radio: fix.radio, params } });
        const p = j.plan;
        out.innerHTML = `<h6>${esc(p.label)} — ${esc(p.target.name)} <span class="ip">${esc(p.target.ip)}</span></h6>
          ${p.lines.map((x) => `<div class="ln">• ${esc(x)}</div>`).join("")}
          ${(p.notes || []).map((x) => `<div class="ln note">⚠ ${esc(x)}</div>`).join("")}
          <div class="warnbox">${esc(p.warn)}</div>
          <div class="ts">Changes: ${p.edits.map((e) => `<code>${esc(e.key)} ${esc(e.old)} → ${esc(e.new)}</code>`).join(" · ")}</div>
          <div class="acts"><button type="button" class="btn pri sm" data-go>Apply now</button><button type="button" class="btn sm" data-x>Cancel</button></div>`;
        out.querySelector("[data-x]").onclick = () => { out.hidden = true; out.innerHTML = ""; };
        out.querySelector("[data-go]").onclick = async (e) => {
          e.currentTarget.disabled = true;
          try { await CC.api(`${base}/apply`, { method: "POST", body: { token: j.token, confirm: true } }); }
          catch (err) { out.insertAdjacentHTML("beforeend", msg(err.message, "bad")); return; }
          follow();
        };
      } catch (err) { out.innerHTML = msg(err.message, "bad") + (/Settings/.test(err.message) ? `<div class="ts">Turn it on in the site's own Settings → Advanced → Wi-Fi fixes.</div>` : ""); }
    };
    if (fix.kind === "channel" && !(fix.params && fix.params.freq)) {
      out.innerHTML = `<h6>Move ${esc(fix.name)} to another channel</h6>
        <div class="acts"><label>New centre frequency <input class="inp" style="width:90px" type="number" min="4900" max="6100" step="5" data-f placeholder="5180"> MHz</label>
        <label>Width <select class="sel" data-w><option value="">keep</option><option>20</option><option>40</option><option>80</option></select></label></div>
        <label class="f"><input type="checkbox" data-u> Go ahead even for stations Netwatch can't log in to (if they don't follow, the AP moves back by itself)</label>
        <div class="acts"><button type="button" class="btn pri sm" data-p>Preview</button><button type="button" class="btn sm" data-x>Cancel</button></div>`;
      out.querySelector("[data-x]").onclick = () => { out.hidden = true; out.innerHTML = ""; };
      out.querySelector("[data-p]").onclick = () => preview({ freq: +out.querySelector("[data-f]").value,
        width: +out.querySelector("[data-w]").value || undefined, accept_unverified: out.querySelector("[data-u]").checked });
    } else {
      preview(fix.params || {});
    }
  }

  // ---- Frequency plan per tower (site /api/wifi/plan → freqplan.py) ----
  function spectrum(t, p) {
    const X0 = 5150, X1 = 5850, W = 700, sx = (f) => ((f - X0) / (X1 - X0)) * W;
    const rows = [], placed = [];
    for (const l of t.links.filter((l) => l.lo != null).sort((a, b) => a.lo - b.lo)) {
      let r = 0; while (placed.some((q) => q.r === r && q.hi + 6 > l.lo)) r++;
      placed.push({ r, hi: l.hi }); rows.push({ l, r });
    }
    const nr = Math.max(1, ...rows.map((x) => x.r + 1)), H = 22 + nr * 26;
    const bg = p.ranges.map((r) => [r[0], r[1], "rgba(34,211,238,.07)"]).concat(p.dfs.map((r) => [r[0], r[1], "rgba(245,158,11,.10)"]));
    return `<svg class="wf-spec" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Channels on ${esc(t.name)}">
      <rect x="0" y="0" width="${W}" height="${H - 16}" fill="rgba(148,163,184,.08)"/>
      ${bg.map((b) => `<rect x="${sx(b[0])}" y="0" width="${sx(b[1]) - sx(b[0])}" height="${H - 16}" fill="${b[2]}"/>`).join("")}
      ${[5200, 5300, 5500, 5600, 5700, 5800].map((f) => `<text x="${sx(f)}" y="${H - 3}" class="ax">${f}</text>`).join("")}
      ${rows.map(({ l, r }) => `<g><rect x="${sx(l.lo)}" y="${4 + r * 26}" width="${Math.max(3, sx(l.hi) - sx(l.lo))}" height="20" rx="3" class="${l.clash ? "bad" : "ok"}"/><text x="${sx(l.lo) + 4}" y="${18 + r * 26}" class="lb">${esc(l.name)} · ${l.freq}/${l.width}</text></g>`).join("")}
    </svg>`;
  }
  function planHtml(p) {
    if (!(p.towers || []).length) return `<div class="empty"><b>No towers yet</b>In the Network tab, add a group of type “Tower / mast” for each high point and put its radios in it — then the plan checks their channels.</div>`;
    const sm = p.summary || {}, moves = p.moves || [];
    const head = `<p class="note" style="margin:0 0 6px">${sm.clashes ? `<b class="bad">${CC.plural(sm.clashes, "clash", "clashes")}</b> on ${CC.plural(sm.towers, "tower")} · ${CC.plural(sm.moves, "move")} suggested` : `✓ No channel clashes on ${CC.plural(sm.towers, "tower")}`} · same tower: ${p.guard_mhz} MHz gap · towers within ${p.near_km} km planned together · <span style="color:var(--warn)">amber = DFS (radar check)</span></p>`;
    return head + p.towers.map((t) => {
      const mv = moves.filter((m) => m.towers.includes(t.name));
      return `<div class="wf-tower"><h4>${esc(t.name)}${t.near.length ? ` <span class="note">near ${esc(t.near.join(", "))}</span>` : ""}</h4>
        ${t.links.length ? spectrum(t, p) : `<p class="note">No radio on this tower that the site reads.</p>`}
        ${t.unknown.length ? `<p class="note">Frequency unknown (no login saved): ${esc(t.unknown.join(", "))}</p>` : ""}
        ${t.clashes.length ? `<ul class="clash">${t.clashes.map((c) => `<li class="${c.level}">${esc(c.a_name)} and ${esc(c.b_name)} ${c.gap < 0 ? `overlap by ${Math.round(-c.gap)} MHz` : `are only ${Math.round(c.gap)} MHz apart`}${c.where === "near" ? ` (on ${esc(c.tower)}, nearby)` : ""}</li>`).join("")}</ul>` : ""}
        ${mv.length ? `<ol class="mv">${mv.map((m) => {
          const fix = { kind: "channel", radio: m.ap, name: m.name, params: { freq: m.to, width: m.width } };
          return `<li><span>${m.to ? `Move <b>${esc(m.name)}</b> ${m.from} → ${m.to} MHz (${m.width} MHz wide)${m.dfs ? " — DFS channel" : ""}` : `${esc(m.name)}: ${esc(m.note || "no free channel")}`}</span>${m.to && p.fix_enabled
            ? `<span class="wf-chkrow"><button type="button" class="wf-chkbtn fix" data-wf-fix="${esc(JSON.stringify(fix))}" title="Preview the move first — nothing is changed until you apply it">Move…</button></span><div class="wf-chkres wf-fixres" hidden aria-live="polite"></div>`
            : m.to ? `<span class="note">Turn on the site's Settings → Advanced → Wi-Fi fixes to apply this from here.</span>` : ""}</li>`;
        }).join("")}</ol>` : ""}</div>`;
    }).join("");
  }

  // Inline SVG line charts with a hover readout (no chart library: offline LAN).
  function chart(el, title, ts, lines, opts = {}) {
    if (!el) return;
    const legend = lines.map((ln) => `<span class="lg"><i style="background:${ln.color}"></i>${esc(ln.label)}</span>`).join("");
    const all = [];
    lines.forEach((ln) => (ln.data || []).forEach((v) => { if (v != null) all.push(v); }));
    if ((ts || []).length < 2 || all.length < 2) { el.innerHTML = `<h5>${title} ${legend}</h5><div class="none">${esc(opts.empty || "Not enough readings yet")}</div>`; return; }
    const W = 600, H = opts.height || 110, P = 3, span = opts.span || 10;
    let lo = Math.min(...all), hi = Math.max(...all);
    (opts.guides || []).forEach((g) => { if (g.v >= lo - span && g.v <= hi + span) { lo = Math.min(lo, g.v); hi = Math.max(hi, g.v); } });
    if (hi - lo < span) { const mid = (hi + lo) / 2; lo = mid - span / 2; hi = mid + span / 2; }
    const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
    const x0 = ts[0], x1 = ts[ts.length - 1];
    const X = (t) => P + ((t - x0) * (W - 2 * P)) / (x1 - x0 || 1);
    const Y = (v) => P + ((hi - v) * (H - 2 * P)) / (hi - lo || 1);
    const trace = (data) => { let d = "", pen = false; ts.forEach((t, i) => { const v = data[i]; if (v == null) { pen = false; return; } d += (pen ? "L" : "M") + X(t).toFixed(1) + "," + Y(v).toFixed(1); pen = true; }); return d; };
    const roll = (data, n) => data.map((v, i) => { const w = data.slice(Math.max(0, i - n + 1), i + 1).filter((x) => x != null); return v == null || !w.length ? null : w.reduce((a, b) => a + b, 0) / w.length; });
    const paths = lines.map((ln) => {
      const data = ln.data || [];
      if (!opts.smooth) return `<path d="${trace(data)}" fill="none" stroke="${ln.color}" stroke-width="1.5" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>`;
      return `<path d="${trace(data)}" fill="none" stroke="${ln.color}" stroke-width="1" opacity=".22" vector-effect="non-scaling-stroke"/><path d="${trace(roll(data, opts.smooth))}" fill="none" stroke="${ln.color}" stroke-width="2" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>`;
    }).join("");
    const guides = (opts.guides || []).filter((g) => g.v > lo && g.v < hi).map((g) => `<line x1="0" x2="${W}" y1="${Y(g.v).toFixed(1)}" y2="${Y(g.v).toFixed(1)}" class="${g.bad ? "guide-bad" : "guide"}" vector-effect="non-scaling-stroke"/>`).join("");
    const long = x1 - x0 > 2 * 86400;
    const fmtT = (t) => { const d = new Date(t * 1000); return long ? d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" }) : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); };
    el.innerHTML = `<h5>${title} ${legend}</h5>
      <div class="plot"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="height:${H}px">${guides}${paths}</svg><div class="hov" hidden></div><div class="tip" hidden></div></div>
      <div class="ax"><span>${fmtT(x0)}</span><span>${num(lo + pad)} – ${num(hi - pad)}${opts.unit ? " " + opts.unit : ""}</span><span>${fmtT(x1)}</span></div>`;
    const plot = $(".plot", el), hov = $(".hov", el), tip = $(".tip", el);
    plot.addEventListener("mousemove", (e) => {
      const rect = plot.getBoundingClientRect();
      const t = x0 + ((e.clientX - rect.left) / rect.width) * (x1 - x0);
      let i = 0, best = Infinity;
      ts.forEach((v, k) => { const d = Math.abs(v - t); if (d < best) { best = d; i = k; } });
      const px = ((ts[i] - x0) / (x1 - x0 || 1)) * rect.width;
      hov.hidden = tip.hidden = false;
      hov.style.left = px + "px";
      tip.style.left = Math.max(80, Math.min(rect.width - 80, px)) + "px";
      tip.innerHTML = new Date(ts[i] * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" }) + " · " +
        lines.map((ln) => `<span style="color:${ln.color}">${(ln.data || [])[i] == null ? "—" : num(ln.data[i])}</span>`).join(" / ") + (opts.unit ? " " + opts.unit : "");
    });
    plot.addEventListener("mouseleave", () => { hov.hidden = tip.hidden = true; });
  }

  function drawCharts(root, l, s) {
    if (!root) return;
    const box = (k) => $(`[data-chart="${k}"]`, root);
    if (!s) { $$("[data-chart]", root).forEach((el) => (el.innerHTML = `<div class="skel" style="height:130px"></div>`)); return; }
    const smooth = Math.max(2, Math.round((s.ts || []).length / 40));
    chart(box("sig"), "Signal", s.ts, [{ label: l.ap.name + " hears", data: s.sig_ap, color: "#22d3ee" }, { label: l.sta.name + " hears", data: s.sig_sta, color: "#a78bfa" }],
      { unit: "dBm", span: 12, guides: [{ v: -75 }, { v: -82, bad: true }] });
    chart(box("score"), "Link score", s.ts, [{ label: "↓ download", data: s.score_dl, color: "#34d399" }, { label: "↑ upload", data: s.score_ul, color: "#fbbf24" }],
      { span: 25, smooth, guides: [{ v: 50 }, { v: 35, bad: true }] });
    const cap = (s.cap_dl || []).map((v) => (v == null ? null : Math.round(v / 1000)));
    if (cap.some((v) => v != null)) {
      chart(box("cap"), "Capacity", s.ts, [{ label: l.sta.name + " · Mbps", data: cap, color: "#38bdf8" }], { span: 20, unit: "Mbps" });
      chart(box("air"), "Air time", s.ts, [{ label: l.sta.name + " · % of the channel", data: s.airtime, color: "#f472b6" }], { span: 10, unit: "%", smooth, guides: [{ v: 80 }] });
    } else {
      chart(box("cap"), "Data rate", s.ts, [{ label: "transmit", data: s.tx, color: "#38bdf8" }, { label: "receive", data: s.rx, color: "#f472b6" }], { span: 20, unit: "Mbps" });
      chart(box("air"), "Air time", [], [], { empty: `Save ${l.sta.name}'s SSH login on the site to chart its air time and capacity` });
    }
  }

  function detailHtml(site, l) {
    const m = l.metrics || {}, fz = l.fresnel, ch = m.chains || {};
    const hrs = (label, poor, avg) => `<span class="lab">${label}</span>` + (poor || Array(24).fill(null)).map((v, h) =>
      `<b style="background:${poorBg(v)}" title="${String(h).padStart(2, "0")}:00 · ${v == null ? "no data" : v + "% of readings poor"}${(avg || [])[h] != null ? " · average score " + avg[h] : ""}"></b>`).join("");
    const facts = [
      ["Access point", esc(l.ap.name), [l.ap.model, l.ap.ip].filter(Boolean).map(esc).join(" · ")],
      ["Station", esc(l.sta.name), [l.sta.model, l.sta.ip, l.sta.monitored ? "login saved" : "no login saved"].filter(Boolean).map(esc).join(" · ")],
      ["Channel", `${esc(m.freq_label || "—")}${m.width ? " · " + esc(m.width) + " MHz" : ""}`, m.ap_mode ? "AP mode " + esc(m.ap_mode) : ""],
      ["Distance", km(m.distance_m), m.distance_range && m.distance_range[0] !== m.distance_range[1] ? `radios estimate ${km(m.distance_range[0])} – ${km(m.distance_range[1])}` : ""],
      ["Keep clear", fz ? `${fz.clear_m} m` : "—", fz ? `around the line at mid-path (Fresnel zone ${fz.radius_m} m)` : ""],
      ["Signal now", `<span class="${sigCls((m.signal_ap || {}).now)}">${num((m.signal_ap || {}).now)}</span> / <span class="${sigCls((m.signal_sta || {}).now)}">${num((m.signal_sta || {}).now)}</span> dBm`, `AP / station · normally ${num((m.signal_ap || {}).baseline)} / ${num((m.signal_sta || {}).baseline)}`],
      ["Link score now", `<span class="${scoreCls((m.score_dl || {}).now)}">${num((m.score_dl || {}).now)}</span> ↓ · <span class="${scoreCls((m.score_ul || {}).now)}">${num((m.score_ul || {}).now)}</span> ↑`, `lowest ${num((m.score_dl || {}).min)} ↓ / ${num((m.score_ul || {}).min)} ↑`],
      ["Poor readings", `${num((m.pct_below_50 || {}).score_dl)}% ↓ · ${num((m.pct_below_50 || {}).score_ul)}% ↑`, "share with a link score under 50"],
      ["Data rate", `${num((m.tx || {}).now)} / ${num((m.rx || {}).now)} Mbps`, "transmit / receive now"],
      ["Capacity", mbps((m.capacity || {}).now), (m.capacity || {}).baseline ? "normally " + mbps(m.capacity.baseline) : l.sta.monitored ? "" : "needs the station login"],
      ["Antenna chains", ch.c0 != null ? `${num(ch.c0)} / ${num(ch.c1)} dBm` : "—", ch.gap_avg != null ? `at ${esc(ch.radio)} · average gap ${ch.gap_avg} dB` : ""],
      ["Noise floor", `${num((m.noise_ap || {}).now)} / ${num((m.noise_sta || {}).now)} dBm`, "AP / station"],
      ["Disconnections", String((m.drops || {}).count || 0), (m.drops || {}).count ? "longest " + Math.max(...m.drops.episodes.map((e) => e.minutes)) + " min" : "in this period"],
      ["Last reading", CC.ago(m.last_ts), ""],
    ];
    const found = l.findings || [];
    return `<div class="wf-det">
      ${found.length ? found.map(findingHtml).join("") : `<div class="banner ok-banner">✓ Nothing to fix — this link is healthy in both directions.</div>`}
      <div class="wf-charts">
        <div class="wf-chart" data-chart="sig"></div><div class="wf-chart" data-chart="score"></div>
        <div class="wf-chart" data-chart="cap"></div><div class="wf-chart" data-chart="air"></div>
        <div class="wf-chart" style="grid-column:1/-1"><h5>Poor readings by hour of day${m.worst_hours ? ` <span class="lg warn">worst ${esc(m.worst_hours.label)}</span>` : ""}
          <span class="lg"><i style="background:#10b981"></i>rarely</span><span class="lg"><i style="background:#f59e0b"></i>1 in 5</span><span class="lg"><i style="background:#e11d48"></i>half or more</span></h5>
          <div class="wf-hours">${hrs("↓ download", m.hourly_poor_dl, m.hourly_score_dl)}${hrs("↑ upload", m.hourly_poor_ul, m.hourly_score_ul)}<span></span>${Array.from({ length: 24 }, (_, h) => `<span class="hx">${h % 3 === 0 ? String(h).padStart(2, "0") : ""}</span>`).join("")}</div>
          <p class="note" style="margin:6px 0 0">Share of readings with a link score under 50, in the site's local time. By day points at wind or daytime traffic; at night at busy neighbours or recordings being copied.</p></div>
      </div>
      <dl class="wf-facts">${facts.map(([k, v, sub]) => `<div><dt>${k}</dt><dd>${v}${sub ? `<small>${sub}</small>` : ""}</dd></div>`).join("")}</dl>
      <div class="row">
        <button class="btn sm" data-wf-csv="${esc(l.id)}">${icon("down")} Readings (CSV)</button>
        ${l.sta.ip ? `<button class="btn sm" data-wf-tunnel="${esc(l.sta.ip)}" title="Open ${esc(l.sta.name)}'s airOS page through a tunnel">${icon("plug")} ${esc(l.sta.name)} airOS</button>` : ""}
        ${l.ap.ip ? `<button class="btn sm" data-wf-tunnel="${esc(l.ap.ip)}" title="Open ${esc(l.ap.name)}'s airOS page through a tunnel">${icon("plug")} ${esc(l.ap.name)} airOS</button>` : ""}
      </div>
    </div>`;
  }

  function linkCard(site, l, open) {
    const m = l.metrics || {};
    const top = topFinding(l);
    const cap = (m.capacity || {}).now, drops = (m.drops || {}).count || 0;
    const sub = [l.sta.model, l.sta.ip, m.distance_m != null ? km(m.distance_m) : ""].filter(Boolean).join(" · ");
    return `<div class="wf-link g-${l.grade}${open ? " open" : ""}" data-link="${esc(l.id)}">
      <div class="wf-row" role="button" tabindex="0" aria-expanded="${open}" data-wf-toggle="${esc(l.id)}">
        <div style="min-width:0"><div class="nm">${gradeBadge(l.grade, true)}<b title="${esc(l.name)}">${esc(l.sta.name)}</b></div><span class="sub">${esc(top ? top.title + " · " + sub : sub)}</span></div>
        <div class="wf-sig" title="How loud each radio hears the other (dBm)">${sigBar("AP hears", (m.signal_ap || {}).now)}${sigBar(l.sta.name + " hears", (m.signal_sta || {}).now)}</div>
        <div class="wf-q" title="airMAX link score, average over the period"><span class="v ${scoreCls(l.quality)}">${l.quality == null ? "—" : l.quality}<small> /100</small></span><span class="d">↓ ${num((m.score_dl || {}).avg)} · ↑ ${num((m.score_ul || {}).avg)}</span></div>
        <div class="wf-cap"><b>${mbps(cap)}</b><span class="${drops ? "warn" : ""}">${drops ? CC.plural(drops, "drop") : "no drops"}</span></div>
        <svg class="chev" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>
      </div>
      ${open ? detailHtml(site, l) : ""}
    </div>`;
  }

  function radiosHtml(d) {
    const radios = (d.radios || []).slice().sort((a, b) => a.ok - b.ok || b.is_ap - a.is_ap || String(a.name).localeCompare(String(b.name)));
    if (!radios.length) return `<div class="pbd note">No radios yet.</div>`;
    return `<ul class="alist">${radios.map((r) => {
      const c = r.current || {};
      if (!r.ok) {
        const f = (r.findings || [])[0];
        return `<li><span class="sev unk">?</span><div style="min-width:0"><div class="t">${esc(r.name)} <span class="mono dim">${esc(r.ip || "")}</span></div><div class="d">${esc(f ? f.causes[0] : r.error || "can't be read")}${f && f.steps[0] ? " — " + esc(f.steps[0].text) : ""}</div></div><span></span></li>`;
      }
      const bits = [r.is_ap ? "Access point" : "Station", r.model, r.is_ap ? CC.plural(r.stations, "station") : "", c.airtime != null ? "air " + num(c.airtime) + "%" : "", c.noise != null ? "noise " + num(c.noise) + " dBm" : ""].filter(Boolean);
      const warn = (r.findings || []).some((f) => f.level !== "info");
      return `<li><span class="sev ${warn ? "warn" : "info"}">${icon("wifi")}</span><div style="min-width:0"><div class="t">${esc(r.name)} <span class="mono dim">${esc(r.ip || "")}</span></div><div class="d">${bits.map(esc).join(" · ")} · read ${CC.ago(r.last_ts)}</div></div><span></span></li>`;
    }).join("")}</ul>`;
  }

  const GUIDE = `<dl class="wf-guide">
    <dt>Signal</dt><dd>How loud the other radio arrives. <b class="ok">−65 dBm or better</b> is good, <b class="warn">−75</b> weak, <b class="bad">−82</b> poor. A drop of 6 dB from a link's own normal matters more than the number.</dd>
    <dt>Link score</dt><dd>airMAX's 0–100 rating of the data rate achieved. <b class="ok">80+</b> good, <b class="warn">under 50</b> poor, <b class="bad">under 35</b> bad. Strong signal with a low score = interference or a partly blocked path, not aim.</dd>
    <dt>Download / upload</dt><dd>Download = access point → station. The weak direction points at the end that hears interference.</dd>
    <dt>Air time</dt><dd>How much of the channel is in use. Above <b class="warn">80 %</b> the link is congested — usually camera streams.</dd>
    <dt>Antenna chains</dt><dd>Within 3–4 dB is normal; <b class="warn">8 dB</b> apart means a mis-seated radio, a twisted dish or water in the feed.</dd>
    <dt>Fresnel zone</dt><dd>The oval around the line between two radios that must stay clear. Trees inside it keep the signal strong but ruin the score.</dd>
  </dl>`;

  function csv(site, l, s) {
    if (!s || !(s.ts || []).length) { CC.toast("Open the link first so its readings load", "bad"); return; }
    const cols = ["sig_ap", "sig_sta", "score_dl", "score_ul", "tx", "rx", "latency", "cap_dl", "airtime", "chain_gap"];
    const head = ["time", l.ap.name + " hears (dBm)", l.sta.name + " hears (dBm)", "score download", "score upload", "tx Mbps", "rx Mbps", "latency ms", "capacity kbps", "station air time %", "chain gap dB"];
    const q = (v) => `"${String(v).replace(/"/g, '""')}"`;
    const rows = [head.map(q).join(",")].concat(s.ts.map((t, i) => [new Date(t * 1000).toISOString(), ...cols.map((c) => (s[c] || [])[i] ?? "")].join(",")));
    CC.download(new Blob(["﻿" + rows.join("\r\n")], { type: "text/csv" }), `wifi-${site.id}-${l.name.replace(/[^A-Za-z0-9_-]+/g, "-")}.csv`);
  }

  async function openAirOS(site, ip, btn) {
    await CC.busy(btn, async () => {
      try {
        const j = await api(`/api/hub/sites/${site.id}/tunnel`, { method: "POST", body: { ip, port: 443 }, timeout: 30000 });
        CC.tunnelResult({ ...j, scheme: j.scheme || "https" }, `airOS · ${ip}`, "airOS uses a self-signed certificate — accept the browser warning.");
      } catch (e) { CC.toast(e.message, "bad"); }
    });
  }

  // ---- a site's Wi-Fi tab ----------------------------------------------------------------
  const series = {};                         // `${site}|${hours}|${link}` -> series
  CC.wifiTab = {
    open: new Set(), where: "", showInfo: false, data: {},
    enter(s, el) {
      this.site = s.id;
      const want = CC.params().get("link");
      if (want) this.open.add(want);
      el.innerHTML = `<div class="row wf-bar">
          <div class="seg" role="group" aria-label="Time range" id="wt-range">${[[24, "24 h"], [168, "7 days"], [720, "30 days"]].map(([h, l]) => `<button data-h="${h}" class="${h === HOURS ? "on" : ""}">${l}</button>`).join("")}</div>
          <span class="note" id="wt-at"></span>
          <span style="margin-left:auto" class="row">
            <button class="btn sm" id="wt-poll" title="Ask the site to read every radio now">${icon("refresh")} Read radios now</button>
          </span></div>
        <div id="wt-kpis"></div>
        <div id="wt-legacy"></div>
        <div class="cols"><div>
          <section class="panel"><div class="phd"><h2>What to do</h2><div class="seg" role="group" aria-label="Where" id="wt-where"><button data-w="" class="on">All</button><button data-w="remote">From the office</button><button data-w="site">On site</button></div></div><div id="wt-fix"></div></section>
          <section class="panel"><div class="phd"><h2>Frequency plan per tower</h2><span class="note">Links on one tower must keep their channels apart · towers come from the Network diagram</span></div><div class="pbd" id="wt-plan"><div class="skel" style="height:90px"></div></div></section>
          <section class="panel"><div class="phd"><h2>Links <small>grouped by access point</small></h2><button class="btn sm ghost" id="wt-all">Open all</button></div><div class="pbd" id="wt-links"><div class="skel" style="height:140px"></div></div></section>
        </div><div>
          <section class="panel"><div class="phd"><h2>Radios</h2></div><div id="wt-radios"></div></section>
          <section class="panel"><div class="phd"><h2>Reading the numbers</h2></div><div class="pbd">${GUIDE}</div></section>
        </div></div>`;
      $$("#wt-range button").forEach((b) => (b.onclick = () => {
        HOURS = +b.dataset.h;
        try { localStorage.setItem(HOURS_KEY, HOURS); } catch (e) { /* ignore */ }
        $$("#wt-range button").forEach((x) => x.classList.toggle("on", x === b));
        this.load(CC.site(this.site));
      }));
      $$("#wt-where button").forEach((b) => (b.onclick = () => { this.where = b.dataset.w; $$("#wt-where button").forEach((x) => x.classList.toggle("on", x === b)); this.draw(CC.site(this.site)); }));
      $("#wt-all").onclick = () => { const d = this.current(); if (!d) return; const ids = (d.links || []).map((l) => l.id); if (this.open.size >= ids.length) this.open.clear(); else ids.forEach((i) => this.open.add(i)); this.draw(CC.site(this.site)); };
      $("#wt-poll").onclick = (e) => this.poll(CC.site(this.site), e.currentTarget);
      el.onclick = (e) => {
        const t = e.target.closest("[data-wf-toggle]");
        if (t) return this.toggle(t.dataset.wfToggle);
        if (e.target.closest("[data-wf-info]")) { this.showInfo = !this.showInfo; return this.draw(CC.site(this.site)); }
        const c = e.target.closest("[data-wf-csv]");
        if (c) { const d = this.current(), l = d && d.links.find((x) => x.id === c.dataset.wfCsv); return l && csv(CC.site(this.site), l, series[`${this.site}|${HOURS}|${l.id}`]); }
        const fx = e.target.closest("[data-wf-fix]");
        if (fx) return runFix(CC.site(this.site), fx);
        const ck = e.target.closest("[data-wf-check]");
        if (ck) return runCheck(CC.site(this.site), ck);
        const tn = e.target.closest("[data-wf-tunnel]");
        if (tn) return openAirOS(CC.site(this.site), tn.dataset.wfTunnel, tn);
      };
      el.onkeydown = (e) => { const t = e.target.closest("[data-wf-toggle]"); if (t && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); this.toggle(t.dataset.wfToggle); } };
      this.load(s, want);
    },
    current() { return HOURS === 168 ? S.wifiLinks[this.site] : this.data[`${this.site}|${HOURS}`]; },
    async loadPlan(s) {
      const el = $("#wt-plan");
      if (!el || el.querySelector(".wf-fixres:not([hidden])")) return;
      try {
        const p = await api(`/api/hub/sites/${encodeURIComponent(s.id)}/wifi/plan`, { timeout: 40000 });
        if (this.site === s.id && $("#wt-plan")) $("#wt-plan").innerHTML = planHtml(p);
      } catch (e) { if ($("#wt-plan")) $("#wt-plan").innerHTML = `<div class="empty"><b>No frequency plan</b>${esc(e.message)}</div>`; }
    },
    async load(s, focus, fresh) {
      if (!s) return;
      this.loadPlan(s);
      const id = s.id, h = HOURS;
      const have = h === 168 ? S.wifiLinks[id] : this.data[`${id}|${h}`];
      this.draw(s);
      if (!have || fresh || CC.now() - (have.__at || 0) > 300) {
        try {
          const j = await api(`/api/hub/sites/${id}/wifi-links?hours=${h}${fresh ? "&fresh=1" : ""}`, { timeout: 60000 });
          const val = { ...j, __at: CC.now() };
          if (h === 168) S.wifiLinks[id] = val; else this.data[`${id}|${h}`] = val;
        } catch (e) {
          const val = { ...(have || {}), error: e.message, __at: CC.now() };
          if (h === 168) S.wifiLinks[id] = val; else this.data[`${id}|${h}`] = val;
        }
        if (this.site !== id || HOURS !== h) return;
        this.draw(CC.site(id));
        CC.renderShell();
      }
      if (focus) setTimeout(() => { const el = $(`.wf-link[data-link="${CSS.escape(focus)}"]`); if (el) el.scrollIntoView({ behavior: "smooth", block: "start" }); }, 80);
    },
    async poll(s, btn) {
      await CC.busy(btn, async () => {
        try { await api(`/api/hub/sites/${s.id}/radio-poll`, { method: "POST" }); }
        catch (e) { CC.toast(e.message, "bad"); return; }
        CC.toast("The site is reading its radios — about a minute…");
        await new Promise((r) => setTimeout(r, 45000));
        await this.load(CC.site(s.id), null, true);
        Object.keys(series).filter((k) => k.startsWith(s.id + "|")).forEach((k) => delete series[k]);
        this.draw(CC.site(s.id));
        CC.toast("Radios read", "ok");
      });
    },
    toggle(lid) {
      if (this.open.has(lid)) this.open.delete(lid); else this.open.add(lid);
      const url = `#/site/${enc(this.site)}/wifi${this.open.has(lid) ? "?link=" + enc(lid) : ""}`;
      if (location.hash !== url) history.replaceState(null, "", url);
      this.draw(CC.site(this.site));
    },
    update(s) {
      // The fleet loader emits every 20 s; only redraw when this site's link data changed,
      // otherwise open links, charts and hover would reset under the reader.
      if (!s || s.id !== this.site || !$("#wt-links")) return;
      if ($(".wf-fixres:not([hidden])")) return;          // never redraw under a fix that may be running
      const d = this.current();
      const key = d ? `${d.__at}|${d.cached_at}|${!!d.error}` : "";
      if (key === this.drawnKey) { if (d) $("#wt-at").textContent = `${d.stale ? "last known · " : ""}read ${CC.ago(d.cached_at || d.__at)}${d.poll_min ? ` · radios polled every ${d.poll_min} min` : ""}`; return; }
      this.draw(s);
    },
    leave() { this.site = null; },
    draw(s) {
      if (!s || !$("#wt-links")) return;
      const d = this.current();
      this.drawnKey = d ? `${d.__at}|${d.cached_at}|${!!d.error}` : "";
      const legacy = d && d.legacy;
      $("#wt-at").textContent = !d ? "Loading…" : d.error && !d.summary ? "" : `${d.stale ? "last known · " : ""}read ${CC.ago(d.cached_at || d.__at)}${d.poll_min ? ` · radios polled every ${d.poll_min} min` : ""}`;
      $("#wt-legacy").innerHTML = !d ? "" : legacy ? `<div class="banner">This site runs an older Netwatch without link diagnosis${d.radios_n ? ` (${CC.plural(d.radios_n, "radio")} being read)` : ""}. Update its Netwatch to see the diagnosis here.${(d.problems || []).length ? ` Its radio monitor reports: ${d.problems.slice(0, 3).map((p) => esc(p.what)).join(" · ")}.` : ""}</div>`
        : d.error && !d.summary ? `<div class="banner bad">${esc(d.error)}</div>` : "";
      if (!d || !d.summary) {
        $("#wt-kpis").innerHTML = ""; $("#wt-fix").innerHTML = d ? `<div class="empty"><b>No diagnosis</b>${legacy ? "Update the site's Netwatch." : "Waiting for the site."}</div>` : `<div class="pbd"><div class="skel" style="height:80px"></div></div>`;
        $("#wt-links").innerHTML = d ? "" : `<div class="skel" style="height:140px"></div>`; $("#wt-radios").innerHTML = "";
        return;
      }
      const links = d.links || [];
      const worstL = links.filter((l) => l.quality != null).sort((a, b) => a.quality - b.quality)[0];
      $("#wt-kpis").innerHTML = kpis(d.summary, { worst: worstL && { link: worstL, label: worstL.name, href: linkHref(s.id, worstL) } });
      const fixes = (d.fixes || []).filter((f) => !this.where || f.where === this.where).map((f) => ({ ...f, site: s }));
      $("#wt-fix").innerHTML = (d.radios || []).length ? fixList(fixes, { showInfo: this.showInfo }) : `<div class="empty"><b>No radios are read at this site</b>The site reads Ubiquiti airOS radios that have a saved SSH login — add them on the site's Netwatch page (Logins).</div>`;
      $("#wt-all").hidden = !links.length;
      $("#wt-all").textContent = this.open.size >= links.length && links.length ? "Close all" : "Open all";
      const byAp = new Map();
      links.forEach((l) => { if (!byAp.has(l.ap.mac)) byAp.set(l.ap.mac, []); byAp.get(l.ap.mac).push(l); });
      const radioBy = Object.fromEntries((d.radios || []).map((r) => [String(r.key).toUpperCase(), r]));
      const groups = [...byAp.entries()].sort((a, b) => Math.min(...a[1].map((l) => RANK[l.grade])) - Math.min(...b[1].map((l) => RANK[l.grade])) || a[1][0].ap.name.localeCompare(b[1][0].ap.name));
      $("#wt-links").innerHTML = !links.length ? `<div class="empty"><b>No links in this period</b>${(d.radios || []).length ? "The radios answered, but none had a connected station." : "No radios are read yet."}</div>` : groups.map(([mac, ls]) => {
        const ap = ls[0].ap, r = radioBy[mac], m = ls[0].metrics || {};
        const st = (r && r.stats) || {}, air = st.airtime || {};
        const meta = [ap.model, ap.ip, m.freq_label, m.width ? m.width + " MHz" : "", r && r.ssid ? "SSID " + r.ssid : ""].filter(Boolean).map(esc).join(" · ");
        const notes = (r ? r.findings || [] : []).map(findingHtml).join("");
        return `<div class="wf-ap"><div class="wf-aph"><span class="ico">${icon("wifi")}</span><b>${esc(ap.name)}</b><span class="note">${meta}</span>
            ${m.ap_mixed ? `<span class="b warn nodot">Mixed mode</span>` : ""}${air.now != null ? `<span class="tag">Air ${num(air.now)}%${air.max != null ? " · peak " + num(air.max) + "%" : ""}</span>` : ""}${(st.noise || {}).now != null ? `<span class="tag">Noise ${num(st.noise.now)} dBm</span>` : ""}<span class="tag">${CC.plural(ls.length, "link")}</span></div>
          ${notes}${ls.map((l) => linkCard(s, l, this.open.has(l.id))).join("")}</div>`;
      }).join("");
      $("#wt-radios").innerHTML = radiosHtml(d);
      // charts for open links: series come per link, on demand
      links.filter((l) => this.open.has(l.id)).forEach((l) => {
        const root = $(`.wf-link[data-link="${CSS.escape(l.id)}"]`);
        const k = `${s.id}|${HOURS}|${l.id}`;
        if (series[k]) return drawCharts(root, l, series[k]);
        drawCharts(root, l, null);
        if (series[k] === undefined) {
          series[k] = false;
          api(`/api/hub/sites/${s.id}/wifi-links?hours=${HOURS}&link=${enc(l.id)}`, { timeout: 60000 })
            .then((j) => { const x = (j.links || [])[0]; series[k] = (x && x.series) || { ts: [] }; })
            .catch(() => { series[k] = { ts: [] }; })
            .then(() => { const el = $(`.wf-link[data-link="${CSS.escape(l.id)}"]`); if (el && this.site === s.id) drawCharts(el, l, series[k]); });
        }
      });
    },
  };

  // ---- fleet Wi-Fi page ------------------------------------------------------------------
  CC.route("/wifi", {
    where: "", siteF: "", showInfo: false,
    enter() {
      CC.setCrumbs([{ label: "Overview", href: "#/" }, { label: "Wi-Fi" }]);
      view().innerHTML = `<div class="ph"><div><div class="eyebrow">Fleet</div><h1>Wireless links</h1><p id="wf-sub">Every radio link at every site: how healthy it is, why, and what to do. Last 7 days.</p></div>
          <div class="actions"><button class="btn" id="wf-refresh" title="Ask every site for its latest link diagnosis">${icon("refresh")} Refresh</button></div></div>
        <div id="wf-kpis"></div>
        <div class="cols"><div>
          <section class="panel"><div class="phd"><h2>What to do across the fleet</h2><div class="row"><select class="sel" id="wf-site" aria-label="Site"><option value="">All sites</option></select><div class="seg" role="group" aria-label="Where" id="wf-where"><button data-w="" class="on">All</button><button data-w="remote">From the office</button><button data-w="site">On site</button></div></div></div><div id="wf-fix"></div></section>
          <section class="panel"><div class="phd"><h2>Links <small id="wf-n"></small></h2></div><div class="twrap"><table class="t"><thead><tr><th>Link</th><th class="hide-m">Signal AP / station</th><th>Score</th><th class="num hide-m">Capacity</th><th class="num hide-m">Drops</th></tr></thead><tbody id="wf-body"></tbody></table></div></section>
        </div><div>
          <section class="panel"><div class="phd"><h2>Sites</h2></div><div id="wf-sites"></div></section>
          <section class="panel"><div class="phd"><h2>Reading the numbers</h2></div><div class="pbd">${GUIDE}</div></section>
        </div></div>`;
      $("#wf-site").onchange = () => { this.siteF = $("#wf-site").value; this.update(); };
      $$("#wf-where button").forEach((b) => (b.onclick = () => { this.where = b.dataset.w; $$("#wf-where button").forEach((x) => x.classList.toggle("on", x === b)); this.update(); }));
      $("#wf-refresh").onclick = (e) => CC.busy(e.currentTarget, async () => {
        await CC.pool(S.sites.filter((s) => s.enabled && s.reachable), 3, async (s) => {
          try { S.wifiLinks[s.id] = { ...(await api(`/api/hub/sites/${s.id}/wifi-links?hours=168&fresh=1`, { timeout: 60000 })), __at: CC.now() }; }
          catch (err) { S.wifiLinks[s.id] = { ...(S.wifiLinks[s.id] || {}), error: err.message, __at: CC.now() }; }
          this.update();
        });
        CC.emit();
      });
      $("#wf-fix").onclick = (e) => { if (e.target.closest("[data-wf-info]")) { this.showInfo = !this.showInfo; this.update(); } };
      $("#wf-body").onclick = (e) => { const tr = e.target.closest("tr[data-href]"); if (tr) location.hash = tr.dataset.href; };
      $("#wf-body").onkeydown = (e) => { const tr = e.target.closest("tr[data-href]"); if (tr && e.key === "Enter") location.hash = tr.dataset.href; };
      this.update();
    },
    update() {
      if (!$("#wf-body")) return;
      const sites = S.sites.filter((s) => s.enabled);
      const sel = $("#wf-site"), cur = this.siteF;
      sel.innerHTML = `<option value="">All sites</option>` + sites.map((s) => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join("");
      sel.value = cur;
      const per = sites.map((s) => ({ s, x: CC.wifiOf(s.id) }));
      const withData = per.filter((p) => p.x.state === "ok");
      const sum = { links: 0, good: 0, warn: 0, crit: 0, radios: 0, radios_read: 0, remote_fixes: 0, site_fixes: 0 };
      withData.forEach(({ x }) => Object.keys(sum).forEach((k) => (sum[k] += x.summary[k] || 0)));
      const all = withData.flatMap(({ s, x }) => x.links.map((l) => ({ s, l })));
      const worst = all.filter((r) => r.l.quality != null).sort((a, b) => a.l.quality - b.l.quality)[0];
      const loading = per.filter((p) => p.x.state === "loading").length;
      $("#wf-sub").textContent = `${CC.plural(sum.links, "link")} across ${CC.plural(withData.length, "site")} with radios · last 7 days${loading ? ` · ${loading} still loading` : ""}`;
      $("#wf-kpis").innerHTML = !S.loaded ? "" : kpis(sum, { worst: worst && { link: worst.l, label: `${worst.s.name} · ${worst.l.name}`, href: linkHref(worst.s.id, worst.l) }, sitesNote: CC.plural(withData.length, "site") });
      // fix list
      const fixes = withData.filter(({ s }) => !cur || s.id === cur).flatMap(({ s, x }) => (x.w.fixes || []).map((f) => ({ ...f, site: s })))
        .filter((f) => !this.where || f.where === this.where)
        .sort((a, b) => ({ crit: 0, warn: 1, info: 2 })[a.level] - ({ crit: 0, warn: 1, info: 2 })[b.level] || (a.where !== "remote") - (b.where !== "remote"));
      $("#wf-fix").innerHTML = !withData.length ? `<div class="pbd"><div class="${loading ? "skel" : "note"}" style="height:${loading ? "70px" : "auto"}">${loading ? "" : "No site has a link diagnosis yet."}</div></div>` : fixList(fixes, { showSite: !cur, showInfo: this.showInfo });
      // links table
      const rows = all.filter((r) => !cur || r.s.id === cur).sort((a, b) => RANK[a.l.grade] - RANK[b.l.grade] || (a.l.quality ?? 100) - (b.l.quality ?? 100));
      $("#wf-n").textContent = rows.length ? CC.plural(rows.length, "link") : "";
      $("#wf-body").innerHTML = rows.map(({ s, l }) => {
        const m = l.metrics || {}, top = topFinding(l), drops = (m.drops || {}).count || 0;
        return `<tr data-href="${esc(linkHref(s.id, l))}" tabindex="0">
          <td><div class="nm">${gradeBadge(l.grade)} ${esc(l.name)}</div><div class="sub"><b class="cyan" style="font-weight:600">${esc(s.name)}</b>${top ? " · " + esc(top.title) : ""}</div></td>
          <td class="hide-m mono"><span class="${sigCls((m.signal_ap || {}).now)}">${num((m.signal_ap || {}).now)}</span> / <span class="${sigCls((m.signal_sta || {}).now)}">${num((m.signal_sta || {}).now)}</span> <span class="dim">dBm</span></td>
          <td><b class="${scoreCls(l.quality)}">${l.quality == null ? "—" : l.quality}</b> <span class="note">↓${num((m.score_dl || {}).avg)} ↑${num((m.score_ul || {}).avg)}</span></td>
          <td class="num hide-m">${mbps((m.capacity || {}).now)}</td>
          <td class="num hide-m ${drops ? "warn" : "dim"}">${drops}</td></tr>`;
      }).join("") || `<tr><td colspan="5"><div class="empty"><b>${loading ? "Loading links…" : "No links"}</b>${loading ? "" : "No site reports a wireless link yet."}</div></td></tr>`;
      // sites
      $("#wf-sites").innerHTML = `<ul class="alist">${per.map(({ s, x }) => {
        const href = `#/site/${enc(s.id)}/wifi`;
        const [sev, mark, line] = !s.reachable ? ["bad", "!", "Site offline — last known links"] :
          x.state === "loading" ? ["unk", "…", "Loading"] :
          x.state === "error" ? ["unk", "?", x.w.error] :
          x.state === "legacy" ? ["unk", "↑", "Older Netwatch — update the site to see the link diagnosis"] :
          x.state === "none" ? ["unk", "–", "No radios read (no saved radio logins)"] :
          [x.crit.length ? "bad" : x.attn || x.radioIssues.length ? "warn" : "info", x.attn ? "!" : "✓",
            `${x.summary.good}/${x.summary.links} links healthy · ${x.summary.radios_read}/${x.summary.radios} radios read${x.radioIssues.length ? " · " + x.radioIssues.map((f) => f.title).slice(0, 1).join("") : ""}`];
        return `<li><span class="sev ${sev}">${mark}</span><div style="min-width:0"><div class="t">${esc(s.name)}</div><div class="d">${esc(line || "")}</div></div><a class="btn sm" href="${href}">Open</a></li>`;
      }).join("")}</ul>`;
    },
  });
})();
