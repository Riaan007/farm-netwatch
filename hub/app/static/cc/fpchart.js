/* Frequency plan chart — shared by the site page (app/static/fpchart.js) and the hub's
   Control Center (hub/app/static/cc/fpchart.js). The two files must stay IDENTICAL
   (tests/test_fpchart_same.py); each page passes its own button classes in `ui`.

   One chart for the whole site: a lane per tower, the 5 GHz band across, every link a bar
   at its real channel (centre ± width/2), other Wi-Fi heard by a site survey as grey bars.
   Drag a bar (or focus it and press ←/→) to plan a move: it snaps to 5 MHz, stays inside
   the allowed band and recolours every bar as clashes appear or clear. A dropped bar is a
   DRAFT kept in this browser; nothing reaches a radio until "Preview move…" → Apply.
   Clash rules mirror app/freqplan.py: same tower < guard MHz apart = clash (overlap =
   critical), near links overlapping = clash, near links < guard apart = close call. "Near" is
   the plan's near_links (closest radios within near_km, from the Network-map GPS); a site on
   an older build sends none, so its near towers are used instead. */
(function () {
  "use strict";
  const X0 = 5150, X1 = 5850, ROW = 30, PX = 760, STEP = 5;
  const LV = { crit: 3, warn: 2, info: 1 };
  const pct = (f) => ((f - X0) / (X1 - X0)) * 100;
  const pair = (a, b) => [a, b].sort().join("|");
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many || one + "s"}`;
  const gapOf = (x, y) => Math.max(x[0], y[0]) - Math.min(x[1], y[1]);
  const band = (c, w) => [c - w / 2, c + w / 2];
  const ago = (ts) => { if (!ts) return "never"; const s = Date.now() / 1000 - ts; return s < 90 ? "just now" : s < 5400 ? Math.round(s / 60) + " min ago" : s < 172800 ? Math.round(s / 3600) + " h ago" : Math.round(s / 86400) + " days ago"; };
  const loud = (sig) => (sig == null ? 1 : sig >= -60 ? 4 : sig >= -70 ? 2 : sig >= -80 ? 1 : 0.3);

  function candidates(p, w) {
    const out = [];
    (p.ranges || []).forEach(([lo, hi]) => { for (let c = lo + w / 2; c + w / 2 <= hi; c += STEP) out.push(c); });
    return out;
  }
  const isDfs = (p, b) => (p.dfs || []).some(([a, z]) => b[0] < z && b[1] > a);

  // ---- the model: links, the planned channel of each, clashes now and after ----------
  function model(p, drafts, nameOf) {
    const L = {};                                                       // ap -> link (first seen)
    const towersOf = {};                                                // ap -> [tower id]
    (p.towers || []).forEach((t) => t.links.forEach((l) => {
      if (!L[l.ap]) L[l.ap] = { ...l, name: nameOf(l.ap) || l.name };
      (towersOf[l.ap] = towersOf[l.ap] || []).push(t.id);
    }));
    const auto = {};
    (p.moves || []).forEach((m) => { if (m.to && L[m.ap]) auto[m.ap] = m; });
    const to = {};                                                      // ap -> planned centre (only when it changes)
    Object.keys(L).forEach((ap) => {
      const d = drafts[ap];
      const c = d != null ? d : auto[ap] ? auto[ap].to : null;
      if (c != null && L[ap].freq != null && c !== L[ap].freq) to[ap] = c;
    });
    const now = {}, after = {};
    Object.values(L).forEach((l) => { if (l.freq != null) { now[l.ap] = band(l.freq, l.width); after[l.ap] = to[l.ap] != null ? band(to[l.ap], l.width) : now[l.ap]; } });
    return { L, towersOf, auto, to, now, after, byId: Object.fromEntries((p.towers || []).map((t) => [t.id, t])) };
  }

  // Two links that may not share a channel: on one tower, or near by GPS (old sites: on near towers).
  function close(p, M, a, b) {
    const ta = M.towersOf[a] || [], tb = M.towersOf[b] || [];
    if (ta.some((t) => tb.includes(t))) return true;
    if (p.near_links) return (p.near_links[a] || []).includes(b);
    return true;
  }

  function clashes(p, M, bands) {
    const out = new Map(), lvl = {};
    const add = (t, a, b, g, level, where, other) => {
      const k = pair(a, b), cur = out.get(k);
      const place = where === "same" ? t.name : [t.name, other.name].sort().join(" ↔ ");
      if (!cur) out.set(k, { a, b, gap: g, level, same: where === "same", places: [place] });
      else { if (!cur.places.includes(place)) cur.places.push(place); if (LV[level] > LV[cur.level]) cur.level = level; cur.same = cur.same || where === "same"; cur.gap = Math.min(cur.gap, g); }
      [a, b].forEach((ap) => { const kk = t.id + "|" + ap; if (!lvl[kk] || LV[level] > LV[lvl[kk]]) lvl[kk] = level; });
    };
    const on = (t) => t.links.map((l) => l.ap).filter((ap) => bands[ap]);
    (p.towers || []).forEach((t) => {
      const here = on(t);
      here.forEach((a, i) => here.slice(i + 1).forEach((b) => {
        const g = gapOf(bands[a], bands[b]);
        if (g < p.guard_mhz) add(t, a, b, g, g < 0 ? "crit" : "warn", "same");
      }));
      if (p.near_links) {
        here.forEach((a) => (p.near_links[a] || []).forEach((b) => {
          if (!bands[b] || here.includes(b)) return;
          const o = M.byId[(M.towersOf[b] || [])[0]]; if (!o) return;
          const g = gapOf(bands[a], bands[b]);
          if (g < p.guard_mhz) add(t, a, b, g, g < 0 ? "warn" : "info", "near", o);
        }));
        return;
      }
      (t.near_ids || (p.towers || []).filter((o) => (t.near || []).includes(o.name)).map((o) => o.id)).forEach((oid) => {
        const o = M.byId[oid]; if (!o) return;
        here.forEach((a) => on(o).forEach((b) => {
          if (a === b || here.includes(b)) return;
          if ((p.towers || []).some((x) => x.links.some((l) => l.ap === a) && x.links.some((l) => l.ap === b))) return;
          const g = gapOf(bands[a], bands[b]);
          if (g < p.guard_mhz) add(t, a, b, g, g < 0 ? "warn" : "info", "near", o);
        }));
      });
    });
    return { list: [...out.values()], lvl };
  }

  function heardOn(p, M, ap, b) {
    const hit = [];
    (M.towersOf[ap] || []).forEach((tid) => ((M.byId[tid] || {}).others || []).forEach((n) => { if (gapOf(b, [n.lo, n.hi]) < 5 && !hit.includes(n)) hit.push(n); }));
    return { n: hit.length, cost: Math.round(hit.reduce((s, n) => s + loud(n.signal), 0) * 10) / 10, nets: hit };
  }

  // Best channels for one link: clear of every link on its towers and every near link as planned (a
  // far link's channel may be shared), then quiet (in whole steps: a faint network under about -80 dBm
  // alone does not justify a move), then non-DFS, then clear of EVERY link before sharing, then a small move.
  function ranked(p, M, ap, k) {
    const l = M.L[ap]; if (!l || l.freq == null) return [];
    const out = [];
    candidates(p, l.width).forEach((c) => {
      const b = band(c, l.width);
      const hit = Object.keys(M.after).filter((x) => x !== ap && gapOf(b, M.after[x]) < p.guard_mhz);
      if (hit.some((x) => close(p, M, ap, x))) return;
      const h = heardOn(p, M, ap, b);
      out.push({ c, dfs: isDfs(p, b), heard: h.n, cost: h.cost, shares: hit.map((x) => (M.L[x] || {}).name || x) });
    });
    out.sort((x, y) => Math.floor(x.cost) - Math.floor(y.cost) || x.dfs - y.dfs || (x.shares.length > 0) - (y.shares.length > 0) || Math.abs(x.c - l.freq) - Math.abs(y.c - l.freq));
    return out.slice(0, k || 3);
  }

  // ---- drawing ----------------------------------------------------------------------
  function lane(p, M, t, mode, state, ui) {
    const bands = mode === "after" ? M.after : M.now;
    const items = t.links.filter((l) => l.lo != null).map((l) => {
      const ml = M.L[l.ap], planned = M.to[l.ap];
      const useNew = mode === "after" && planned != null;
      const b = bands[l.ap] || [l.lo, l.hi], f = useNew ? planned : l.freq;
      const st = (useNew && !state.lvl[t.id + "|" + l.ap]) ? "moved" : state.lvl[t.id + "|" + l.ap] || "ok";
      const label = `${ml.name} · ${f}`;
      const lw = (label.length * 6.6 + 14) * ((X1 - X0) / PX);
      const left = b[1] + lw > X1;
      return { l, ml, b, f, st, planned, useNew, left, ext: left ? [b[0] - lw, b[1]] : [b[0], b[1] + lw] };
    }).sort((a, b) => a.ext[0] - b.ext[0]);
    const rows = [];
    items.forEach((it) => { let r = 0; while ((rows[r] || []).some((x) => x.ext[1] + 4 > it.ext[0] && x.ext[0] < it.ext[1] + 4)) r++; (rows[r] = rows[r] || []).push(it); it.r = r; });
    const others = t.others || [];
    const nrow = Math.max(1, rows.length), oy = nrow * ROW + 4;
    const h = oy + (others.length ? 14 : 4);
    const tip = (it) => `${it.ml.name}\n${it.f} MHz centre · ${it.l.width} MHz wide (${Math.round(it.b[0])}–${Math.round(it.b[1])})${it.l.dfs && !it.useNew ? "\nDFS channel (radar check)" : ""}${it.planned != null ? `\nPlanned: ${it.l.freq} → ${it.planned} MHz` : ""}${(it.l.here || []).length ? "\nOn this tower: " + it.l.here.join(", ") : ""}\n${it.l.readable ? "Drag (or ←/→) to plan a move" : "Netwatch cannot log in to this AP — it can't be moved from here"}`;
    const bars = items.map((it) => `${it.useNew ? `<i class="fp-ghost" style="left:${pct(it.l.lo)}%;width:${pct(it.l.hi) - pct(it.l.lo)}%;top:${it.r * ROW + 6}px"></i>` : ""}
      <div class="fp-it s-${it.st}${it.left ? " lft" : ""}${it.l.readable ? " drag" : ""}" data-ap="${esc(it.l.ap)}" data-w="${it.l.width}" tabindex="0" role="slider" aria-label="${esc(it.ml.name)} channel" aria-valuenow="${it.f}" title="${esc(tip(it))}" style="--l:${pct(it.b[0])}%;--w:${pct(it.b[1]) - pct(it.b[0])}%;top:${it.r * ROW + 6}px">
        <i class="fp-bar"></i><span class="fp-lb"><b>${esc(it.ml.name)}</b> <span class="fp-f">${it.f}</span>${it.planned != null && mode !== "after" ? ` <em title="A move is planned">→ ${it.planned}</em>` : ""}</span></div>`).join("");
    const oth = others.map((n) => `<i class="fp-oth" style="left:${pct(n.lo)}%;width:${Math.max(0.3, pct(n.hi) - pct(n.lo))}%;top:${oy}px;opacity:${n.signal == null ? 0.5 : n.signal >= -60 ? 0.95 : n.signal >= -70 ? 0.7 : n.signal >= -80 ? 0.45 : 0.25}" title="${esc(`${n.ssid || "(hidden network)"} · ${n.freq} MHz${n.width_known ? " · " + n.width + " MHz wide" : ""} · ${n.signal == null ? "?" : n.signal} dBm\nheard ${ago(n.seen)}`)}"></i>`).join("");
    const sc = (t.scanned || [])[0];
    const scanLine = others.length ? `${plural(others.length, "other network")} · ${ago(sc && sc.ts)}` : sc ? `scanned ${ago(sc.ts)} · none heard` : "";
    const scanBtn = t.scan_from && t.scan_from.length && p.fix_enabled ? `<button type="button" class="fp-scanb" data-fp-scan="${esc(t.id)}" title="Scan for other Wi-Fi from a station on this tower">${sc ? "Scan again" : "Scan"}</button>` : "";
    const unk = (t.unknown || []).length ? `<small class="fp-unk" title="${esc(t.unknown.join(", "))}">+${t.unknown.length} unread</small>` : "";
    return `<div class="fp-lane" data-tower="${esc(t.id)}"><div class="fp-tw"><b title="${esc(t.name)}">${esc(t.name)}</b><small>${t.near.length ? "near " + esc(t.near.join(", ")) : "on its own"}</small>${scanLine ? `<small class="fp-scl">${esc(scanLine)}</small>` : ""}${unk}${scanBtn}</div>
      <div class="fp-track" style="height:${h}px">${items.length ? bars : `<span class="fp-none">No radio here that Netwatch can read</span>`}${oth}</div></div>`;
  }

  function chart(p, M, mode, state, ui) {
    const zones = [].concat(
      (p.ranges || []).map((r) => `<i class="fp-z ok" style="left:${pct(r[0])}%;width:${pct(r[1]) - pct(r[0])}%"></i>`),
      (p.dfs || []).map((r) => `<i class="fp-z dfs" style="left:${pct(r[0])}%;width:${pct(r[1]) - pct(r[0])}%"></i>`),
      [`<i class="fp-z no" style="left:${pct(5350)}%;width:${pct(5470) - pct(5350)}%" title="5350–5470 MHz may not be used"></i>`]).join("");
    const ticks = []; for (let f = 5200; f <= 5800; f += 50) ticks.push(`<span class="${f % 100 ? "mn" : ""}" style="left:${pct(f)}%">${f % 100 ? "" : f}</span>`);
    const bands = [[5150, 5250, "U-NII-1"], [5250, 5350, "U-NII-2 · DFS"], [5350, 5470, "not allowed"], [5470, 5725, "U-NII-2C · DFS"], [5725, 5850, "U-NII-3"]]
      .map(([a, b, l]) => `<span style="left:${pct(a)}%;width:${pct(b) - pct(a)}%">${l}</span>`).join("");
    return `<div class="fp-scroll"><div class="fp">
      <div class="fp-head"><div class="fp-corner">Tower</div><div class="fp-axis"><div class="fp-bands">${bands}</div><div class="fp-ticks">${ticks.join("")}</div></div></div>
      <div class="fp-body"><div class="fp-zones">${zones}</div>${p.towers.map((t) => lane(p, M, t, mode, state, ui)).join("")}</div>
      <div class="fp-foot"><div></div><div class="fp-ticks">${ticks.join("")}</div></div>
    </div></div>`;
  }

  function html(p, M, mode, drafts, ui, scanUi) {
    if (!(p.towers || []).length) return `<div class="${ui.empty}"><b>No towers yet</b>On the Network page, add a group of type “Tower / mast” for each high point and put its radios in it — the plan then keeps their channels apart.</div>`;
    const nowS = clashes(p, M, M.now), aftS = clashes(p, M, M.after);
    const state = mode === "after" ? aftS : nowS;
    const real = (s) => s.list.filter((c) => c.level !== "info");
    const nowReal = real(nowS), aftReal = real(aftS);
    const moves = Object.keys(M.to).map((ap) => ({ ap, l: M.L[ap], to: M.to[ap], draft: drafts[ap] != null, auto: M.auto[ap] }))
      .sort((a, b) => (a.draft - b.draft) || a.l.name.localeCompare(b.l.name));
    moves.forEach((m, i) => { m.n = i + 1; });
    const nOf = Object.fromEntries(moves.map((m) => [m.ap, m.n]));
    const kept = Object.keys(drafts).filter((ap) => M.L[ap] && drafts[ap] === M.L[ap].freq && M.auto[ap]);
    const others = new Set((p.towers || []).flatMap((t) => (t.others || []).map((n) => n.bssid))).size;
    const verdict = !nowReal.length ? `<div class="fp-verdict ok"><span class="dot"></span><div><b>No channel clashes</b><span>Every link keeps ${p.guard_mhz} MHz clear of the links on its towers${p.near_links ? ` and within ${p.near_km} km` : " and nearby"}.</span></div></div>`
      : `<div class="fp-verdict ${nowReal.some((c) => c.level === "crit") ? "bad" : "warn"}"><span class="dot"></span><div><b>${plural(nowReal.length, "channel clash", "channel clashes")}</b><span>${moves.length ? (aftReal.length ? `the plan below leaves ${plural(aftReal.length, "clash", "clashes")}` : `the ${plural(moves.length, "move")} below clear all of them`) : "drag a bar to plan a move"}</span></div></div>`;
    const stats = `<div class="fp-stats"><span><b>${p.towers.length}</b> towers</span><span><b>${Object.keys(M.L).length}</b> links</span><span><b>${p.guard_mhz}</b> MHz gap wanted</span><span><b>${others}</b> other networks heard</span><span>${p.near_links ? `links within <b>${p.near_km}</b> km of each other (Network-map GPS) keep apart · farther apart they may share` : `towers within <b>${p.near_km}</b> km or linked are planned together`}</span></div>`;
    const legend = `<div class="fp-legend"><span><i class="k ok"></i>clear</span><span><i class="k crit"></i>overlap on one tower</span><span><i class="k warn"></i>clash with a nearby tower</span><span><i class="k info"></i>close call</span>${mode === "after" ? `<span><i class="k moved"></i>moved</span>` : `<span><em>→ 5735</em> move planned</span>`}<span><i class="k oth"></i>other Wi-Fi (scan)</span><span><i class="k dfsz"></i>DFS band</span></div>`;
    const name = (ap) => esc((M.L[ap] || {}).name || ap);
    // clash cards: every pair that clashes now or after the plan
    const pairs = new Map();
    nowS.list.forEach((c) => pairs.set(pair(c.a, c.b), { now: c }));
    aftS.list.forEach((c) => { const k = pair(c.a, c.b); pairs.set(k, { ...(pairs.get(k) || {}), after: c }); });
    const clashCards = [...pairs.values()].map((x) => {
      const c = x.now || x.after, by = [c.a, c.b].filter((ap) => nOf[ap]).map((ap) => nOf[ap]);
      const status = x.now && !x.after ? `<div class="d ok">✓ Cleared by move ${by.join(" + ")}</div>`
        : !x.now ? `<div class="d bad">New — move ${by.join(" + ")} would cause this</div>`
        : by.length ? `<div class="d">Still ${x.after.gap < 0 ? "overlapping" : "close"} after move ${by.join(" + ")}</div>` : `<div class="d dim">No move planned for this pair</div>`;
      const lev = (x.after || x.now).level, cc = x.after || x.now;
      return { lev: !x.after ? 0 : LV[lev], h: `<li class="fp-cl ${x.after ? lev : "cleared"}" data-pair="${esc(c.a)}|${esc(c.b)}"><span class="sevdot"></span>
        <div><b>${name(c.a)}</b> <span class="dim">and</span> <b>${name(c.b)}</b>
          <div class="d">${cc.gap < 0 ? `overlap by <b>${Math.round(-cc.gap)} MHz</b>` : `only <b>${Math.round(cc.gap)} MHz</b> apart`} · ${cc.same ? "same tower" : "nearby towers"}: ${esc(cc.places.join(" · "))}</div>${status}</div></li>` };
    }).sort((a, b) => b.lev - a.lev).map((x) => x.h);
    const moveCard = (m) => {
      const l = m.l, b = band(m.to, l.width), dfs = isDfs(p, b);
      const hf = heardOn(p, M, m.ap, M.now[m.ap]), ht = heardOn(p, M, m.ap, b);
      const bad = aftS.list.filter((c) => c.level !== "info" && (c.a === m.ap || c.b === m.ap));
      const fix = { kind: "channel", radio: m.ap, name: l.name, params: { freq: m.to, width: l.width } };
      const why = m.draft ? "Your move" : m.auto && m.auto.why === "busy" ? "Suggested: quieter channel" : "Suggested";
      return `<li class="fp-mv${m.draft ? " draft" : ""}" data-ap="${esc(m.ap)}" data-fixhost>
        <span class="n">${m.n}</span>
        <div class="body"><div class="mvtop"><b>${esc(l.name)}</b><span class="fq"><s>${l.freq}</s><i>→</i><b>${m.to}</b> MHz</span><span class="${ui.tag}">${l.width} MHz wide</span>${dfs ? ui.badge("warn", "DFS", "Radar-checked channel: the AP listens about a minute before it transmits") : ""}<span class="fp-why${m.draft ? " me" : ""}">${why}</span></div>
          <div class="d">${esc((M.towersOf[m.ap] || []).map((id) => M.byId[id].name).join(" · "))}${(hf.n || ht.n) ? ` · other networks heard: ${hf.n} now → <b class="${ht.n > hf.n ? "bad" : ht.n < hf.n ? "ok" : ""}">${ht.n}</b>` : ""}</div>
          ${bad.length ? `<div class="d bad">⚠ Still clashes with ${bad.map((c) => name(c.a === m.ap ? c.b : c.a)).join(", ")}</div>` : `<div class="d ok">✓ Clear of every link on every tower</div>`}
          <div class="acts">${l.readable && p.fix_enabled ? `<button type="button" class="${ui.btn} ${ui.pri}" ${ui.fixAttr(fix)} title="Reads the radios and shows exactly what would change — nothing is changed until you press Apply">Preview move…</button>` : ""}
            ${m.draft ? `<button type="button" class="${ui.btn}" data-fp-reset="${esc(m.ap)}">${m.auto ? "Back to the suggestion" : "Undo"}</button>` : `<button type="button" class="${ui.btn}" data-fp-keep="${esc(m.ap)}" title="Leave this link where it is">Not this one</button>`}
            ${!p.fix_enabled ? `<span class="${ui.note}">To apply moves, turn on <b>Settings → Advanced → Wi-Fi fixes</b>${ui.onHub ? " on the site's own Netwatch" : ""}.</span>` : l.readable ? `<span class="${ui.note}">Undoes itself if the link does not come back</span>` : `<span class="${ui.note}">Netwatch can't log in to this AP</span>`}</div>
          ${l.readable && p.fix_enabled ? ui.fixRes : ""}</div></li>`;
    };
    const bestRows = Object.values(M.L).filter((l) => l.readable && l.freq != null).map((l) => {
      const cur = heardOn(p, M, l.ap, M.after[l.ap]), best = ranked(p, M, l.ap, 3);
      const f = M.to[l.ap] != null ? M.to[l.ap] : l.freq;
      return `<li data-ap="${esc(l.ap)}"><div class="nm"><b>${esc(l.name)}</b><small>${f} MHz · ${l.width} wide · ${cur.n ? plural(cur.n, "other network") + " heard" : "no other network heard"}</small></div>
        <div class="ch">${best.map((x) => `<button type="button" class="fp-best${x.c === f ? " on" : ""}" data-fp-set="${esc(l.ap)}" data-c="${x.c}" title="${x.heard ? plural(x.heard, "other network") + " heard here" : "No other network heard here"}${x.dfs ? " · DFS" : ""} · ${x.shares.length ? `clear of every link nearby · shares with ${esc(x.shares.join(", "))} (more than ${p.near_km} km away)` : "clear of every link"}">${x.c}${x.dfs ? "<small>DFS</small>" : ""}${x.shares.length ? "<small>shared</small>" : ""}${x.heard ? `<small>${x.heard} heard</small>` : x.shares.length ? "" : `<small>quiet</small>`}</button>`).join("") || `<span class="dim">no clear channel at this width</span>`}</div></li>`;
    }).join("");
    const anyScan = (p.towers || []).some((t) => (t.scanned || []).length);
    return `<div class="fp-top">${verdict}${stats}</div>
      <div class="fp-bar-row"><div class="${ui.seg}" role="group" aria-label="Show channels" data-fp-mode><button data-m="now" class="${mode !== "after" ? "on" : ""}">Now</button><button data-m="after" class="${mode === "after" ? "on" : ""}"${moves.length ? "" : " disabled"}>With the plan${moves.length ? ` (${moves.length})` : ""}</button></div>${legend}</div>
      <p class="fp-hint">Drag a link's bar along the band to plan a move — it snaps to 5 MHz and every bar recolours as clashes appear or clear. Nothing changes on a radio until you press <b>Preview move…</b> and then Apply.${Object.keys(drafts).length ? ` <button type="button" class="fp-linkb" data-fp-clear>Clear my moves</button>` : ""}</p>
      ${p.summary && p.summary.centre_unknown ? `<div class="fp-banner">${plural(p.summary.centre_unknown, "wide link")} not re-read since the update — placed from its control channel until the next radio read.</div>` : ""}
      ${scanUi}
      ${chart(p, M, mode, state, ui)}
      <div class="fp-cols">
        <section><h3>Planned moves <small>${moves.length ? "one link at a time · each one previewed first" : ""}</small></h3>${moves.length ? `<ol class="fp-mvs">${moves.map(moveCard).join("")}</ol>` : `<p class="${ui.note}">Nothing planned. Drag a bar, or pick one of the best channels below.</p>`}
          ${kept.length ? `<p class="${ui.note}">Left where they are: ${kept.map((ap) => `${name(ap)} <button type="button" class="fp-linkb" data-fp-reset="${esc(ap)}">undo</button>`).join(" · ")}</p>` : ""}</section>
        <section><h3>Clashes <small>${nowReal.length ? `${nowReal.length} now` : "none now"}${moves.length ? ` · ${aftReal.length} with the plan` : ""}</small></h3>${clashCards.length ? `<ul class="fp-cls">${clashCards.join("")}</ul>` : `<p class="${ui.note}">✓ Nothing overlaps.</p>`}</section>
      </div>
      <section class="fp-bestsec"><h3>Best channels per link <small>${anyScan ? "clear of every nearby link as planned, then the fewest and quietest other networks, then non-DFS, then not shared" : "clear of every nearby link as planned, non-DFS first · scan a tower to also avoid other Wi-Fi"}</small></h3><ul class="fp-bests">${bestRows}</ul></section>`;
  }

  // ---- mount: state, drag, scan -------------------------------------------------------
  function mount(el, opts) {
    const ui = opts.ui;
    let plan = null, mode = "now", drafts = {}, scanBox = null;
    const load = () => { try { drafts = JSON.parse(localStorage.getItem(opts.storeKey) || "{}") || {}; } catch (e) { drafts = {}; } };
    const save = () => { try { localStorage.setItem(opts.storeKey, JSON.stringify(drafts)); } catch (e) { /* private mode */ } };
    load();
    const busy = () => !!el.querySelector(ui.fixResSel + ":not([hidden])");
    const nameOf = (k) => (opts.nameOf && opts.nameOf(k)) || "";

    function scanHtml() {
      if (!scanBox) return "";
      const t = (plan.towers || []).find((x) => x.id === scanBox.tower); if (!t) return "";
      if (scanBox.job) return `<div class="fp-scanbox"><b>Scan from ${esc(scanBox.name)}</b><span>${esc(scanBox.job.msg || "Working…")}</span>${scanBox.job.phase === "done" ? `<button type="button" class="${ui.btn}" data-fp-scanx>Close</button>` : ""}</div>`;
      return `<div class="fp-scanbox"><b>Scan for other Wi-Fi at ${esc(t.name)}</b>
        <label>from <select data-fp-scanfrom>${t.scan_from.map((s) => `<option value="${esc(s.key)}">${esc(nameOf(s.key) || s.name)}</option>`).join("")}</select></label>
        <span>That station's link drops for about 10–30 seconds while it listens, then reconnects. Its access point is never touched.</span>
        <span class="acts"><button type="button" class="${ui.btn} ${ui.pri}" data-fp-scango>Scan now</button><button type="button" class="${ui.btn}" data-fp-scanx>Cancel</button></span></div>`;
    }

    function render() {
      if (!plan || busy()) return;
      const M = model(plan, drafts, nameOf);
      Object.keys(drafts).forEach((ap) => { if (!M.L[ap]) delete drafts[ap]; });
      if (mode === "after" && !Object.keys(M.to).length) mode = "now";
      el.innerHTML = html(plan, M, mode, drafts, ui, scanHtml());
      const real = clashes(plan, M, M.now).list.filter((c) => c.level !== "info").length;
      if (opts.onCount) opts.onCount((plan.towers || []).length ? real : null);
    }
    function setDraft(ap, c) {
      const l = model(plan, {}, nameOf).L[ap]; if (!l) return;
      const auto = (plan.moves || []).find((m) => m.ap === ap && m.to);
      if (c === l.freq && !auto) delete drafts[ap];
      else if (auto && c === auto.to) delete drafts[ap];
      else drafts[ap] = c;
      save(); mode = "after"; render();
    }

    // drag: live recolour without a full redraw, commit on release
    let drag = null;
    const nearest = (w, c) => candidates(plan, w).reduce((b, x) => (Math.abs(x - c) < Math.abs(b - c) ? x : b), candidates(plan, w)[0]);
    function live(ap, c) {
      const M = model(plan, { ...drafts, [ap]: c }, nameOf);
      const S = clashes(plan, M, M.after);
      el.querySelectorAll(".fp-it[data-ap]").forEach((it) => {
        const a = it.dataset.ap, tid = it.closest(".fp-lane").dataset.tower, b = M.after[a]; if (!b) return;
        it.style.setProperty("--l", pct(b[0]) + "%");
        const st = S.lvl[tid + "|" + a] || (M.to[a] != null ? "moved" : "ok");
        it.className = it.className.replace(/\bs-\w+/, "s-" + st);
        if (a === ap) { const f = it.querySelector(".fp-f"); if (f) f.textContent = c; const em = it.querySelector("em"); if (em) em.remove(); }
      });
      const mine = S.list.filter((x) => x.level !== "info" && (x.a === ap || x.b === ap));
      const h = heardOn(plan, M, ap, M.after[ap]);
      const tipEl = el.querySelector(".fp-drag") || el.appendChild(Object.assign(document.createElement("div"), { className: "fp-drag" }));
      tipEl.innerHTML = `<b>${c} MHz</b>${isDfs(plan, M.after[ap]) ? " · DFS" : ""}<br>${mine.length ? `<span class="bad">clashes with ${mine.map((x) => esc(M.L[x.a === ap ? x.b : x.a].name)).join(", ")}</span>` : (() => { const far = Object.keys(M.after).filter((x) => x !== ap && gapOf(M.after[ap], M.after[x]) < plan.guard_mhz && !close(plan, M, ap, x)); return far.length ? `<span class="ok">clear of every link nearby</span> · shares with ${esc(far.map((x) => M.L[x].name).join(", "))} (far away)` : `<span class="ok">clear of every link</span>`; })()}${h.n ? ` · ${plural(h.n, "other network")}` : (plan.towers || []).some((t) => (t.scanned || []).length) ? " · quiet" : ""}`;
      return tipEl;
    }
    el.addEventListener("pointerdown", (e) => {
      const it = e.target.closest(".fp-it.drag"); if (!it || e.button > 0 || busy()) return;
      const track = it.closest(".fp-track"), M = model(plan, drafts, nameOf), ap = it.dataset.ap, l = M.L[ap];
      const start = M.to[ap] != null ? M.to[ap] : l.freq;
      drag = { ap, w: l.width, x0: e.clientX, start, c: start, per: (X1 - X0) / track.getBoundingClientRect().width, moved: false, id: e.pointerId, it };
      it.setPointerCapture(e.pointerId);
    });
    el.addEventListener("pointermove", (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      const dx = e.clientX - drag.x0;
      if (!drag.moved && Math.abs(dx) < 4) return;
      drag.moved = true; el.classList.add("fp-dragging");
      const c = nearest(drag.w, drag.start + Math.round((dx * drag.per) / STEP) * STEP);
      if (c === drag.c && drag.tip) return;
      drag.c = c;
      drag.tip = live(drag.ap, c);
      const r = el.getBoundingClientRect();
      drag.tip.style.left = Math.min(r.width - 200, Math.max(0, e.clientX - r.left + 14)) + "px";
      drag.tip.style.top = (e.clientY - r.top + 14) + "px";
    });
    const end = (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      const d = drag; drag = null; el.classList.remove("fp-dragging");
      const tip = el.querySelector(".fp-drag"); if (tip) tip.remove();
      if (d.moved) { d.it.dataset.dragged = "1"; setDraft(d.ap, d.c); }
    };
    el.addEventListener("pointerup", end);
    el.addEventListener("pointercancel", end);
    el.addEventListener("keydown", (e) => {
      const it = e.target.closest(".fp-it.drag"); if (!it || !["ArrowLeft", "ArrowRight"].includes(e.key)) return;
      e.preventDefault();
      const M = model(plan, drafts, nameOf), ap = it.dataset.ap, l = M.L[ap], cur = M.to[ap] != null ? M.to[ap] : l.freq;
      const list = candidates(plan, l.width), i = list.indexOf(nearest(l.width, cur));
      const c = list[Math.max(0, Math.min(list.length - 1, i + (e.key === "ArrowRight" ? 1 : -1)))];
      setDraft(ap, c);
      const again = el.querySelector(`.fp-it[data-ap="${CSS.escape(ap)}"]`); if (again) again.focus();
    });

    el.addEventListener("click", async (e) => {
      const q = (s) => e.target.closest(s);
      let b;
      if ((b = q("[data-fp-mode] button")) && !b.disabled) { mode = b.dataset.m; return render(); }
      if ((b = q("[data-fp-reset]"))) { delete drafts[b.dataset.fpReset]; save(); return render(); }
      if ((b = q("[data-fp-keep]"))) { const l = model(plan, {}, nameOf).L[b.dataset.fpKeep]; if (l) { drafts[l.ap] = l.freq; save(); } return render(); }
      if (q("[data-fp-clear]")) { drafts = {}; save(); mode = "now"; return render(); }
      if ((b = q("[data-fp-set]"))) return setDraft(b.dataset.fpSet, +b.dataset.c);
      if ((b = q("[data-fp-scan]"))) { scanBox = { tower: b.dataset.fpScan }; render(); const sb = el.querySelector(".fp-scanbox"); if (sb) sb.scrollIntoView({ behavior: "smooth", block: "nearest" }); return; }
      if (q("[data-fp-scanx]")) { scanBox = null; return render(); }
      if (q("[data-fp-scango]")) {
        const key = el.querySelector("[data-fp-scanfrom]").value;
        scanBox.name = el.querySelector("[data-fp-scanfrom]").selectedOptions[0].textContent;
        scanBox.job = { msg: "Starting…" }; render();
        try {
          scanBox.job = await opts.scan(key);
          for (let i = 0; i < 40 && scanBox && scanBox.job.phase !== "done"; i++) {
            await new Promise((r) => setTimeout(r, 3000));
            try { scanBox.job = (await opts.scanState(key)) || scanBox.job; } catch (err) { /* the link may be down while it scans */ }
            if (scanBox) render();
          }
          if (scanBox && scanBox.job.ok && opts.reload) await opts.reload();
        } catch (err) { if (scanBox) scanBox.job = { phase: "done", msg: err.message }; }
        if (scanBox) render();
        return;
      }
      const it = q(".fp-it[data-ap]");
      if (it) {
        if (it.dataset.dragged) { delete it.dataset.dragged; return; }
        const m = el.querySelector(`.fp-mv[data-ap="${CSS.escape(it.dataset.ap)}"]`) || el.querySelector(`.fp-bests li[data-ap="${CSS.escape(it.dataset.ap)}"]`);
        if (m) { m.scrollIntoView({ behavior: "smooth", block: "center" }); m.classList.add("flash"); setTimeout(() => m.classList.remove("flash"), 1200); }
      }
    });
    // pointing at a link lights it up on every tower, with its move, best channels and clashes
    const hl = (aps) => {
      if (drag) return;
      el.querySelectorAll(".hl").forEach((x) => x.classList.remove("hl"));
      aps.forEach((ap) => el.querySelectorAll(`.fp-it[data-ap="${CSS.escape(ap)}"], .fp-mv[data-ap="${CSS.escape(ap)}"], .fp-bests li[data-ap="${CSS.escape(ap)}"]`).forEach((x) => x.classList.add("hl")));
      if (aps.length) el.querySelectorAll(".fp-cl").forEach((x) => { if (aps.some((ap) => x.dataset.pair.split("|").includes(ap))) x.classList.add("hl"); });
      el.classList.toggle("fp-hl", aps.length > 0);
    };
    const over = (e) => { const x = e.target.closest(".fp-it[data-ap], .fp-mv[data-ap], .fp-bests li[data-ap], .fp-cl[data-pair]"); hl(!x ? [] : x.dataset.ap ? [x.dataset.ap] : x.dataset.pair.split("|")); };
    el.addEventListener("mouseover", over); el.addEventListener("focusin", over);
    el.addEventListener("mouseleave", () => hl([]));

    return {
      set(p) { plan = p; render(); },
      render,
      busy,
      plan: () => plan,
      planned(ap) { const M = plan && model(plan, drafts, nameOf); return M && M.to[ap] != null ? { to: M.to[ap], draft: drafts[ap] != null } : null; },
      goto(ap) { const m = el.querySelector(`.fp-mv[data-ap="${CSS.escape(ap)}"]`); if (m) { m.scrollIntoView({ behavior: "smooth", block: "center" }); m.classList.add("flash"); setTimeout(() => m.classList.remove("flash"), 1400); } },
    };
  }

  window.FreqChart = { mount, model, clashes, ranked, candidates };
})();
