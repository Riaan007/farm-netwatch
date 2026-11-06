/* Netwatch switch view — ONE renderer for a Ubiquiti EdgeSwitch / UISP switch,
 * used by the site's own page (#/switches) and the hub's Control Center (site tab
 * "Switches"). The hub image copies this file to /static/cc/switchview.js at build
 * time, so both always draw the same thing. Vanilla JS, no dependencies, its own
 * scoped CSS (.swv-*) on the dark NOC palette both apps share.
 *
 *   const ctl = SwitchView.mount(el, {
 *     load(hours, port)  -> Promise<detail>   GET  …/devices/<key>/switch?hours=&port=
 *     poll()             -> Promise<view>     POST …/switch/poll
 *     action(body)       -> Promise<result>   POST …/switch/action (throws {body} on HTTP errors)
 *     backups: { list(), take(), href(name) }  (optional)
 *     advanced: { load(refresh), set(body) }  (optional) GET / POST …/switch/advanced —
 *                                             shown for switches whose caps.advanced is true
 *     deviceHref(dev)    -> href | null       (optional) link to a connected device
 *     onLogin()                               (optional) where to fix the login
 *     settingsHref                            (optional) where "Manage switches" is turned on
 *     port                                    (optional) port id to open first, e.g. "0/5"
 *     confirm(title, text, {danger}) -> Promise<bool>   (optional, default window.confirm)
 *     toast(msg, kind)                        (optional)
 *   });
 *   ctl.refresh(); ctl.destroy();
 */
(function () {
  "use strict";
  if (window.SwitchView) return;

  const CSS = `
.swv{--swv-line:rgba(148,163,184,.16);--swv-panel:rgba(148,163,184,.06);--swv-ink:#e2e8f0;--swv-muted:#94a3b8;--swv-dim:#64748b;
  --swv-ok:#34d399;--swv-warn:#fbbf24;--swv-bad:#fb7185;--swv-cyan:#22d3ee;--swv-vio:#a5b4fc;color:var(--swv-ink);font-size:14px;line-height:1.45;display:grid;gap:14px;min-width:0}
.swv *{box-sizing:border-box}
.swv button{font:inherit;color:inherit;cursor:pointer}
.swv .swv-box{background:var(--swv-panel);border:1px solid var(--swv-line);border-radius:14px;padding:14px 16px;min-width:0}
.swv h3{margin:0 0 8px;font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--swv-muted);font-weight:600}
.swv .swv-muted{color:var(--swv-muted)} .swv .swv-dim{color:var(--swv-dim)}
.swv .swv-mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.92em}
.swv .swv-tnum{font-variant-numeric:tabular-nums}
.swv .swv-row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.swv .swv-btn{border:1px solid rgba(148,163,184,.28);background:rgba(148,163,184,.08);border-radius:9px;padding:6px 11px;font-size:13px;font-weight:600;white-space:nowrap}
.swv .swv-btn:hover{background:rgba(148,163,184,.16)} .swv .swv-btn:disabled{opacity:.5;cursor:progress}
.swv .swv-btn.pri{background:#0e7490;border-color:#0891b2;color:#ecfeff} .swv .swv-btn.pri:hover{background:#0891b2}
.swv .swv-btn.danger{border-color:rgba(251,113,133,.45);color:#fecdd3} .swv .swv-btn.danger:hover{background:rgba(244,63,94,.2)}
.swv .swv-btn.sm{padding:4px 8px;font-size:12px}
.swv .swv-b{display:inline-flex;align-items:center;gap:5px;border-radius:999px;padding:2px 9px;font-size:12px;font-weight:600;background:rgba(148,163,184,.13);color:#cbd5e1;white-space:nowrap}
.swv .swv-b.ok{background:rgba(16,185,129,.14);color:var(--swv-ok)} .swv .swv-b.warn{background:rgba(245,158,11,.14);color:var(--swv-warn)}
.swv .swv-b.bad{background:rgba(244,63,94,.15);color:var(--swv-bad)} .swv .swv-b.info{background:rgba(34,211,238,.12);color:#67e8f9}
.swv .swv-head{display:flex;flex-wrap:wrap;gap:10px 16px;align-items:flex-start;justify-content:space-between}
.swv .swv-head h2{margin:0;font-size:20px;line-height:1.2;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.swv .swv-head p{margin:4px 0 0}
.swv .swv-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.swv .swv-kpi{background:var(--swv-panel);border:1px solid var(--swv-line);border-radius:12px;padding:10px 12px;min-width:0}
.swv .swv-kpi .l{font-size:12px;color:var(--swv-muted)} .swv .swv-kpi .v{font-size:20px;font-weight:700;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.swv .swv-kpi .s{font-size:12px;color:var(--swv-dim);margin-top:2px}
.swv .swv-bar{height:6px;border-radius:9px;background:rgba(148,163,184,.15);overflow:hidden;margin-top:6px}
.swv .swv-bar i{display:block;height:100%;background:var(--swv-ok)} .swv .swv-bar i.warn{background:var(--swv-warn)} .swv .swv-bar i.bad{background:var(--swv-bad)}
.swv .swv-probs{display:grid;gap:8px;margin:0;padding:0;list-style:none}
.swv .swv-prob{display:grid;grid-template-columns:auto 1fr auto;gap:10px;align-items:start;border-radius:10px;padding:9px 11px;border:1px solid var(--swv-line)}
.swv .swv-prob.crit{border-color:rgba(251,113,133,.4);background:rgba(127,29,29,.16)} .swv .swv-prob.warn{border-color:rgba(251,191,36,.35);background:rgba(120,53,15,.13)}
.swv .swv-prob .t{font-weight:600} .swv .swv-prob .h{font-size:13px;color:var(--swv-muted);margin-top:2px}
.swv .swv-face{display:flex;gap:6px;overflow-x:auto;padding:10px;border-radius:12px;background:linear-gradient(#0b1220,#070c16);border:1px solid rgba(148,163,184,.22);scrollbar-width:thin}
.swv .swv-port{flex:0 0 auto;width:74px;border-radius:9px;border:1px solid rgba(148,163,184,.22);background:#0d1627;padding:6px 6px 5px;text-align:left;position:relative;display:grid;gap:3px}
.swv .swv-port:hover{border-color:rgba(34,211,238,.6)} .swv .swv-port.sel{border-color:var(--swv-cyan);box-shadow:0 0 0 2px rgba(34,211,238,.25)}
.swv .swv-port .n{display:flex;justify-content:space-between;align-items:center;font-size:12px;font-weight:700}
.swv .swv-led{width:9px;height:9px;border-radius:50%;background:#334155;box-shadow:inset 0 0 0 1px rgba(0,0,0,.4)}
.swv .swv-led.g{background:var(--swv-ok);box-shadow:0 0 6px rgba(52,211,153,.7)} .swv .swv-led.a{background:var(--swv-warn);box-shadow:0 0 6px rgba(251,191,36,.6)}
.swv .swv-led.r{background:var(--swv-bad);box-shadow:0 0 6px rgba(251,113,133,.7)} .swv .swv-led.x{background:#1e293b;outline:1px dashed #475569}
.swv .swv-port .jack{height:22px;border-radius:4px;background:#020617;border:1px solid #1e293b;display:flex;align-items:center;justify-content:center;font-size:11px;color:#64748b}
.swv .swv-port.sfp .jack{border-style:dashed}
.swv .swv-port .d{font-size:11px;color:var(--swv-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-height:15px}
.swv .swv-port .p{font-size:11px;color:#fde68a;min-height:15px;white-space:nowrap}
.swv .swv-port .flag{position:absolute;top:-7px;right:-5px;font-size:11px;background:#0f172a;border:1px solid rgba(148,163,184,.3);border-radius:6px;padding:0 3px}
.swv .swv-legend{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:12px;color:var(--swv-muted);margin-top:8px}
.swv .swv-legend span{display:inline-flex;align-items:center;gap:5px}
.swv .swv-tablewrap{overflow-x:auto}
.swv table.swv-t{width:100%;border-collapse:collapse;font-size:13px}
.swv table.swv-t th{text-align:left;font-weight:600;color:var(--swv-muted);font-size:12px;padding:6px 8px;border-bottom:1px solid var(--swv-line);white-space:nowrap}
.swv table.swv-t td{padding:8px;border-bottom:1px solid rgba(148,163,184,.08);vertical-align:top}
.swv table.swv-t tr.port-row{cursor:pointer} .swv table.swv-t tr.port-row:hover td{background:rgba(148,163,184,.05)}
.swv table.swv-t tr.sel td{background:rgba(34,211,238,.07)}
.swv .swv-devs{display:flex;flex-wrap:wrap;gap:4px}
.swv table.swv-t td.swv-now{white-space:nowrap}
.swv button.swv-dev{border:0;cursor:pointer;color:#67e8f9}
.swv .swv-dev{display:inline-flex;align-items:center;gap:5px;max-width:220px;border-radius:7px;padding:1px 7px;background:rgba(148,163,184,.1);font-size:12px;color:#e2e8f0;text-decoration:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.swv a.swv-dev:hover{background:rgba(34,211,238,.16)}
.swv .swv-dot{width:7px;height:7px;border-radius:50%;background:var(--swv-ok);flex:none} .swv .swv-dot.off{background:var(--swv-bad)}
.swv .swv-spark{width:110px;height:26px;display:block}
.swv .swv-detail{display:grid;gap:12px}
.swv .swv-grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
.swv dl.swv-kv{display:grid;grid-template-columns:max-content 1fr;gap:4px 14px;margin:0;font-size:13px}
.swv dl.swv-kv dt{color:var(--swv-muted)} .swv dl.swv-kv dd{margin:0;min-width:0;overflow-wrap:anywhere}
.swv .swv-chart{width:100%;height:120px;display:block}
.swv .swv-seg{display:inline-flex;border:1px solid rgba(148,163,184,.28);border-radius:9px;overflow:hidden}
.swv .swv-seg button{border:0;background:transparent;padding:4px 10px;font-size:12px;font-weight:600}
.swv .swv-seg button.on{background:rgba(34,211,238,.18);color:#a5f3fc}
.swv .swv-ev{list-style:none;margin:0;padding:0;display:grid;gap:2px;max-height:360px;overflow:auto}
.swv .swv-ev li{display:grid;grid-template-columns:22px 1fr auto;gap:8px;padding:6px 4px;border-bottom:1px solid rgba(148,163,184,.07);font-size:13px}
.swv .swv-note{font-size:12px;color:var(--swv-dim)}
.swv .swv-banner{border-radius:12px;padding:12px 14px;border:1px solid rgba(251,191,36,.35);background:rgba(120,53,15,.14)}
.swv .swv-banner.bad{border-color:rgba(251,113,133,.4);background:rgba(127,29,29,.16)}
.swv .swv-lock{border-radius:10px;padding:9px 11px;background:rgba(99,102,241,.12);border:1px solid rgba(165,180,252,.3);font-size:13px}
.swv select.swv-sel,.swv input.swv-in{background:#050b16;border:1px solid rgba(148,163,184,.28);border-radius:8px;padding:5px 8px;color:#e2e8f0;font:inherit;font-size:13px}
.swv .swv-adv-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:10px}
.swv .swv-adv-sec{margin-top:16px} .swv .swv-adv-sec>h3{margin-bottom:8px}
.swv .swv-feat{border:1px solid var(--swv-line);border-radius:12px;padding:10px 12px;display:grid;gap:5px;align-content:start;min-width:0}
.swv .swv-feat-h{display:flex;justify-content:space-between;gap:8px;align-items:center}
.swv .swv-feat-h b{font-size:14px}
.swv .swv-chips{display:flex;flex-wrap:wrap;gap:4px}
.swv .swv-chip{min-width:34px;border:1px solid rgba(148,163,184,.28);background:#0d1627;border-radius:7px;padding:3px 6px;font-size:12px;font-weight:700;text-align:center;line-height:1.3}
.swv .swv-chip.on{background:rgba(16,185,129,.18);border-color:rgba(52,211,153,.6);color:#a7f3d0}
.swv .swv-chip.chg{outline:2px dashed #22d3ee;outline-offset:1px}
.swv .swv-chip.lock{opacity:.5;cursor:not-allowed} .swv .swv-chip.no{opacity:.22;cursor:not-allowed}
.swv .swv-chip:disabled{cursor:default}
.swv .swv-tips{display:grid;gap:6px;margin:0;padding:0;list-style:none}
.swv .swv-tip{border-radius:10px;padding:8px 10px;border:1px solid var(--swv-line);display:grid;grid-template-columns:auto 1fr auto;gap:8px;align-items:start}
.swv .swv-tip.do{border-color:rgba(34,211,238,.4);background:rgba(8,51,68,.28)}
.swv .swv-tip .t{font-weight:600} .swv .swv-tip .h{font-size:12.5px;color:var(--swv-muted);margin-top:2px}
.swv .swv-res{margin-top:6px;border-radius:9px;padding:7px 9px;font-size:13px;background:#050b16;border:1px solid rgba(148,163,184,.2)}
.swv .swv-res.ok{border-color:rgba(52,211,153,.45)} .swv .swv-res.warn{border-color:rgba(251,191,36,.45)}
.swv .swv-skel{height:90px;border-radius:12px;background:linear-gradient(90deg,rgba(148,163,184,.06),rgba(148,163,184,.13),rgba(148,163,184,.06));background-size:200% 100%;animation:swvsk 1.4s infinite}
@keyframes swvsk{to{background-position:-200% 0}}
@media (max-width:720px){
  .swv table.swv-t thead{display:none}
  .swv table.swv-t,.swv table.swv-t tbody,.swv table.swv-t tr,.swv table.swv-t td{display:block;width:100%}
  .swv table.swv-t tr{border-bottom:1px solid var(--swv-line);padding:6px 0}
  .swv table.swv-t td{border:0;padding:3px 4px;display:flex;gap:10px}
  .swv table.swv-t td::before{content:attr(data-l);flex:0 0 78px;color:var(--swv-dim);font-size:12px}
  .swv .swv-head h2{font-size:18px}
}`;

  function injectCss() {
    if (document.getElementById("swv-css")) return;
    const s = document.createElement("style");
    s.id = "swv-css";
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  // ---- formatting ---------------------------------------------------------------------
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const now = () => Math.floor(Date.now() / 1000);
  function ago(ts) {
    if (!ts) return "never";
    const s = Math.max(0, now() - ts);
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + " min ago";
    if (s < 86400) return Math.floor(s / 3600) + " h ago";
    return Math.floor(s / 86400) + " d ago";
  }
  function when(ts) { return ts ? new Date(ts * 1000).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : ""; }
  function bps(v) {
    if (v == null || isNaN(v)) return "—";
    const u = ["bit/s", "kbit/s", "Mbit/s", "Gbit/s"];
    let i = 0; let n = +v;
    while (n >= 1000 && i < u.length - 1) { n /= 1000; i++; }
    return (n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)) + " " + u[i];
  }
  function bytes(v) {
    if (v == null || isNaN(v)) return "—";
    const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; let n = +v;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i ? n.toFixed(1) : n) + " " + u[i];
  }
  function uptime(s) {
    if (s == null) return "—";
    s = Math.floor(s);
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return d ? `${d} d ${h} h` : h ? `${h} h ${m} min` : `${m} min`;
  }
  const speedLabel = (p) => (!p.up ? "" : p.speed >= 1000 ? (p.speed / 1000) + " G" : p.speed ? p.speed + " M" : "up");
  const portNo = (p) => String(p.id).split("/").pop();
  const isDefaultName = (n) => !n || /^port\s*\d+$/i.test(n) || /^sfp\s*\d*$/i.test(n);
  const devName = (d) => d.name || d.ip || d.mac || "device";
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
  const POE_LABEL = { off: "Off", auto: "Auto (powers what asks for it)", on: "Forced on", active: "802.3af/at", "active-4pair": "802.3bt", "24v": "24 V passive", "24v-4pair": "24 V 4-pair", "27v": "27 V passive", "27v-4pair": "27 V 4-pair", "48v": "48 V passive", "54v": "54 V passive", "54v-4pair": "54 V 4-pair", pthru: "Pass-through", compat: "Auto + non-standard devices", force: "Forced on (no detection)" };
  const poeLabel = (m) => POE_LABEL[m] || m || "—";

  function portState(p, probs) {
    const pp = probs.filter((x) => x.port === p.id);
    if (pp.some((x) => x.level === "crit")) return { led: "r", label: "Problem", cls: "bad" };
    if (!p.enabled) return { led: "x", label: "Switched off", cls: "" };
    if (!p.up) return { led: "", label: "No link", cls: "" };
    if (pp.some((x) => x.level === "warn")) return { led: "a", label: "Needs a look", cls: "warn" };
    if (p.speed && p.speed < 1000) return { led: "a", label: `${p.speed} Mbit/s`, cls: "warn" };
    return { led: "g", label: "Up", cls: "ok" };
  }

  // ---- charts -----------------------------------------------------------------------------
  function spark(rows) {
    if (!rows || rows.length < 2) return `<svg class="swv-spark" aria-hidden="true"></svg>`;
    const W = 110, H = 26;
    const vals = rows.map((r) => Math.max(r.rx_bps || 0, r.tx_bps || 0));
    const max = Math.max(1, ...vals);
    const t0 = rows[0].ts, t1 = rows[rows.length - 1].ts || t0 + 1;
    const pts = rows.map((r, i) => `${(((r.ts - t0) / Math.max(1, t1 - t0)) * W).toFixed(1)},${(H - 2 - (vals[i] / max) * (H - 4)).toFixed(1)}`).join(" ");
    const downs = rows.filter((r) => !r.up).map((r) => `<rect x="${(((r.ts - t0) / Math.max(1, t1 - t0)) * W).toFixed(1)}" y="${H - 3}" width="2" height="3" fill="#fb7185"/>`).join("");
    return `<svg class="swv-spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true"><polyline points="${pts}" fill="none" stroke="#22d3ee" stroke-width="1.3" vector-effect="non-scaling-stroke"/>${downs}</svg>`;
  }

  function lineChart(rows, series, opts) {
    opts = opts || {};
    if (!rows || rows.length < 2) return `<p class="swv-note">Not enough readings yet — one arrives every few minutes.</p>`;
    // Lines stretch with the box (preserveAspectRatio none); the labels are HTML so they never squash.
    const W = 600, H = 110;
    const t0 = rows[0].ts, t1 = rows[rows.length - 1].ts;
    const x = (ts) => ((ts - t0) / Math.max(1, t1 - t0)) * W;
    let max = 0;
    series.forEach((sr) => rows.forEach((r) => { if (r[sr.k] != null) max = Math.max(max, r[sr.k]); }));
    if (!max && opts.emptyNote) return `<p class="swv-note">${esc(opts.emptyNote)}</p>`;
    max = max || 1;
    const y = (v) => 3 + (1 - v / max) * (H - 6);
    const paths = series.map((sr) => {
      let d = "", pen = false;
      rows.forEach((r) => { const v = r[sr.k]; if (v == null) { pen = false; return; } d += `${pen ? "L" : "M"}${x(r.ts).toFixed(1)},${y(v).toFixed(1)}`; pen = true; });
      return `<path d="${d}" fill="none" stroke="${sr.c}" stroke-width="1.6" vector-effect="non-scaling-stroke"/>`;
    }).join("");
    const downs = opts.showDown ? rows.filter((r) => r.up === 0).map((r) => `<rect x="${x(r.ts).toFixed(1)}" y="0" width="3" height="${H}" fill="rgba(251,113,133,.2)"/>`).join("") : "";
    const grid = [0, 0.5, 1].map((f) => `<line x1="0" x2="${W}" y1="${y(max * f).toFixed(1)}" y2="${y(max * f).toFixed(1)}" stroke="rgba(148,163,184,.13)" vector-effect="non-scaling-stroke"/>`).join("");
    const fmt = opts.fmt || ((v) => String(Math.round(v)));
    const span = t1 - t0;
    const tfmt = (t) => new Date(t * 1000).toLocaleString([], span > 172800 ? { day: "numeric", month: "short" } : { hour: "2-digit", minute: "2-digit" });
    const legend = `<div class="swv-legend">${series.map((sr) => `<span><i style="width:10px;height:3px;background:${sr.c};display:inline-block;border-radius:2px"></i>${esc(sr.label)}</span>`).join("")}${opts.showDown && rows.some((r) => r.up === 0) ? `<span><i style="width:10px;height:10px;background:rgba(251,113,133,.35);display:inline-block"></i>no link</span>` : ""}</div>`;
    return `<div style="display:grid;grid-template-columns:auto 1fr;gap:0 8px;align-items:stretch">
        <div class="swv-note swv-tnum" style="display:flex;flex-direction:column;justify-content:space-between;text-align:right;font-size:11px;line-height:1">${[1, 0.5, 0].map((f) => `<span>${esc(fmt(max * f))}</span>`).join("")}</div>
        <svg class="swv-chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${esc(opts.aria || "chart")}" style="height:110px">${downs}${grid}${paths}</svg>
        <span></span><div class="swv-note swv-tnum" style="display:flex;justify-content:space-between;font-size:11px;margin-top:2px"><span>${esc(tfmt(t0))}</span><span>${esc(tfmt((t0 + t1) / 2))}</span><span>${esc(tfmt(t1))}</span></div>
      </div>${legend}`;
  }

  // ---- events -------------------------------------------------------------------------------
  const EV = {
    link_down: ["🔴", "Link lost"], link_up: ["🟢", "Link up"], speed_change: ["🟠", "Speed changed"],
    poe_lost: ["⚡", "PoE power lost"], poe_mode: ["⚡", "PoE setting changed"], port_disabled: ["⛔", "Port switched off"],
    port_enabled: ["✅", "Port switched on"], port_renamed: ["✏️", "Port renamed"], device_moved: ["🔀", "Device moved port"],
    rebooted: ["🔄", "Switch restarted"], action: ["🛠️", "Action from Netwatch"],
  };
  const ACTION_LABEL = { "port-off": "turn port off", "port-on": "turn port on", "poe-off": "PoE off", "poe-mode": "PoE mode", "poe-cycle": "PoE power cycle", name: "rename port", "cable-test": "cable test", locate: "find-me LEDs", reboot: "reboot", backup: "config backup",
    "set-rstp": "RSTP", "set-loop_guard": "loop protection", "set-storm": "storm control", "set-rate_limit": "bandwidth limit", "set-qos": "QoS", "set-poe_watchdog": "PoE watchdog", "set-long_range": "long-range ports", "set-eee": "EEE", "set-lldp": "LLDP", "set-dhcp_snooping": "DHCP snooping", "set-isolation": "port isolation", "set-telnet": "Telnet", "set-onvif": "ONVIF discovery", "set-sadp_agent": "SADP agent", "set-ntp": "NTP clock", "set-counters": "clear counters" };
  const advVal = (d) => [d.enabled === true ? "on" : d.enabled === false ? "off" : "", d.isolated === true ? "isolated" : d.isolated === false ? "not isolated" : "", d.high === true ? "high priority" : d.high === false ? "normal priority" : "",
    (d.ports || []).length ? "ports " + d.ports.map((p) => String(p).split("/").pop()).join(", ") : "", (d.trusted || []).length ? "trusted " + d.trusted.map((p) => String(p).split("/").pop()).join(", ") : "", d.server ? "server " + d.server : "",
    d.rx_mbps || d.tx_mbps ? `↓${d.rx_mbps || "–"}/↑${d.tx_mbps || "–"} Mbit/s` : "", d.priority != null ? "priority " + d.priority : ""].filter(Boolean).join(", ");
  function evText(e) {
    const d = e.detail || {};
    const port = d.port ? `Port ${esc(String(d.port).split("/").pop())}${d.port_name && !isDefaultName(d.port_name) ? ` (${esc(d.port_name)})` : ""}` : "";
    const who = (d.devices || []).length ? ` — ${d.devices.map(esc).join(", ")}` : "";
    switch (d.switch_event) {
      case "speed_change": return `${port}: ${esc(d.was)} → ${esc(d.speed)} Mbit/s${who}`;
      case "poe_mode": return `${port}: ${esc(poeLabel(d.was))} → ${esc(poeLabel(d.mode))}${who}`;
      case "poe_lost": return `${port} stopped drawing power (was ${esc(d.was_w)} W)${who}`;
      case "port_renamed": return `${port}: “${esc(d.was || "")}” → “${esc(d.port_name || "")}”`;
      case "device_moved": return `${who.slice(3)} now on ${port} (was port ${esc(String(d.was_port || "").split("/").pop())})`;
      case "rebooted": return `The switch restarted (power cut or reboot)`;
      case "action": return `${esc(ACTION_LABEL[d.action] || d.action)}${String(d.action || "").startsWith("set-") && advVal(d) ? " → " + esc(advVal(d)) : `${d.port ? " on " + port : ""}${d.mode ? " → " + esc(poeLabel(d.mode)) : ""}${d.name ? ` “${esc(d.name)}”` : ""}`} — ${d.result === "ok" ? "done" : "<span style='color:#fb7185'>failed" + (d.error ? ": " + esc(d.error) : "") + "</span>"}`;
      default: return `${port}${who}`;
    }
  }

  // ---- mount ----------------------------------------------------------------------------------
  function mount(root, opts) {
    injectCss();
    const o = Object.assign({ confirm: (t, x) => Promise.resolve(window.confirm(t + (x ? "\n\n" + x : ""))), toast: () => {} }, opts);
    const st = { data: null, hours: 24, sel: null, portSeries: {}, busy: false, backups: null, timer: null, dead: false, err: "",
      adv: null, advOpen: false, advErr: "", advPick: {}, advOut: {} };
    root.classList.add("swv");
    root.innerHTML = `<div class="swv-skel"></div><div class="swv-skel" style="height:140px"></div>`;

    let first = true;
    async function refresh(quiet) {
      try {
        const d = await o.load(st.hours);
        if (st.dead) return;
        st.data = d; st.err = "";
        if (st.sel && !(d.ports || []).some((p) => p.id === st.sel)) st.sel = null;
        draw();
        if (first && o.port && (d.ports || []).some((p) => p.id === o.port)) select(o.port);
        first = false;
      } catch (e) {
        if (st.dead) return;
        st.err = e.message || String(e);
        if (!st.data || !quiet) draw();
      }
    }

    async function pollNow(btn) {
      if (btn) btn.disabled = true;
      try { await o.poll(); await refresh(); o.toast("Switch read", "ok"); }
      catch (e) { o.toast(e.message || String(e), "bad"); }
      finally { if (btn) btn.disabled = false; }
    }

    async function act(body, btn, okMsg) {
      if (btn) btn.disabled = true;
      try {
        let r;
        try { r = await o.action(body); }
        catch (e) {
          const b = e.body || {};
          if (b.needs_confirm) {
            if (!(await o.confirm("Are you sure?", b.warning, { danger: true }))) return null;
            r = await o.action(Object.assign({}, body, { confirm: true }));
          } else throw e;
        }
        if (r && r.needs_confirm) {
          if (!(await o.confirm("Are you sure?", r.warning, { danger: true }))) return null;
          r = await o.action(Object.assign({}, body, { confirm: true }));
        }
        if (r && r.ok === false) throw new Error(r.error || "failed");
        o.toast(okMsg || "Done", "ok");
        setTimeout(() => refresh(true), 2500);
        setTimeout(() => refresh(true), 9000);
        return r;
      } catch (e) {
        o.toast((e.body && e.body.error) || e.message || String(e), "bad");
        return null;
      } finally { if (btn) btn.disabled = false; }
    }

    function stateBadge(d) {
      if (!d.online) return `<span class="swv-b bad">Offline</span>`;
      if (d.kind === "no_login") return `<span class="swv-b warn">Login needed</span>`;
      if (d.kind === "auth_failed") return `<span class="swv-b bad">Login rejected</span>`;
      if (d.kind === "pending") return `<span class="swv-b">Waiting for first read</span>`;
      if (!d.ok) return `<span class="swv-b warn">Can't read</span>`;
      const crit = (d.problems || []).filter((p) => p.level === "crit").length, warn = (d.problems || []).filter((p) => p.level === "warn").length;
      if (crit) return `<span class="swv-b bad">${plural(crit, "fault")}</span>`;
      if (warn) return `<span class="swv-b warn">${warn} to check</span>`;
      return `<span class="swv-b ok">Healthy</span>`;
    }

    function draw() {
      const d = st.data;
      if (!d) { root.innerHTML = `<div class="swv-banner bad">${esc(st.err || "Couldn't load the switch.")}</div>`; return; }
      const probs = d.problems || [];
      const ports = d.ports || [];
      const dev = d.device || {}, h = d.health || {}, poe = d.poe || {}, sum = d.summary || {};
      const readable = d.ok || ports.length;
      const title = `<div class="swv-head"><div style="min-width:0"><h2>🔀 ${esc(d.name)} ${stateBadge(d)}</h2>
          <p class="swv-muted">${[d.model || dev.model, dev.firmware ? "firmware " + esc(dev.firmware) : "", d.ip ? `<span class="swv-mono">${esc(d.ip)}</span>` : "", dev.uptime != null ? "up " + uptime(dev.uptime) : ""].filter(Boolean).join(" · ")}</p>
          <p class="swv-note">${d.read_ts ? `Read ${ago(d.read_ts)} · every ${esc(d.poll_min)} min` : "Not read yet"}${d.stale ? " · <b style='color:#fbbf24'>out of date</b>" : ""}${st.err ? ` · <span style="color:#fb7185">${esc(st.err)}</span>` : ""}</p></div>
        <div class="swv-row"><button class="swv-btn" data-a="poll" title="Read the switch now">↻ Read now</button></div></div>`;

      let banner = "";
      if (d.kind === "no_login" || d.kind === "auth_failed") {
        banner = `<div class="swv-banner ${d.kind === "auth_failed" ? "bad" : ""}"><b>${d.kind === "auth_failed" ? "The switch rejected the saved login." : "No login saved for this switch."}</b>
          <div class="swv-muted" style="margin-top:4px">Netwatch reads ports, PoE and traffic with the <b>switch's own web login</b> (the username and password of its web page). ${d.kind === "auth_failed" ? "It does not retry a rejected login on its own (so the switch doesn't lock the account) — it tries again as soon as a new login is saved." : ""}</div>
          ${o.onLogin ? `<div class="swv-row" style="margin-top:8px"><button class="swv-btn pri" data-a="login">🔑 ${d.kind === "auth_failed" ? "Fix the login" : "Add the login"}</button></div>` : ""}</div>`;
      } else if (!d.ok && d.kind && d.kind !== "pending") {
        banner = `<div class="swv-banner">Couldn't read the switch: ${esc(d.error || d.kind)}${readable ? " — showing the last reading." : ""}</div>`;
      }

      if (!readable) {
        root.innerHTML = title + banner + (d.kind === "pending" ? `<div class="swv-box swv-muted">The first reading arrives within a few minutes of the switch having a login.</div>` : "");
        bindTop();
        return;
      }

      const poePct = poe.pct;
      const kpis = `<div class="swv-kpis">
        <div class="swv-kpi"><div class="l">Ports with a link</div><div class="v swv-tnum">${sum.up ?? "—"} <span class="swv-dim" style="font-size:14px">/ ${sum.ports ?? "—"}</span></div><div class="s">${sum.disabled ? plural(sum.disabled, "port") + " switched off" : "none switched off"}</div></div>
        <div class="swv-kpi"><div class="l">PoE power</div><div class="v swv-tnum">${poe.unmeasured && !poe.used_w ? `${poe.powered || 0} <span class="swv-dim" style="font-size:14px">port${poe.powered === 1 ? "" : "s"} on</span>` : poe.used_w != null ? poe.used_w + " W" : "—"}</div><div class="s">${poe.unmeasured ? `${plural(poe.powered || 0, "port")} powered · ${poe.unmeasured} on passive PoE (not measured)` : poe.budget_w ? `of ${poe.budget_w} W · ${plural(poe.powered || 0, "device")} powered` : plural(poe.powered || 0, "device") + " powered"}</div>${poePct != null ? `<div class="swv-bar"><i class="${poePct >= 92 ? "bad" : poePct >= 80 ? "warn" : ""}" style="width:${Math.min(100, poePct)}%"></i></div>` : ""}</div>
        <div class="swv-kpi"><div class="l">Traffic now</div><div class="v swv-tnum" style="font-size:16px">↓ ${bps(sum.rx_bps)}</div><div class="s swv-tnum">↑ ${bps(sum.tx_bps)}</div></div>
        <div class="swv-kpi"><div class="l">Switch health</div><div class="v swv-tnum" style="font-size:16px">${h.temp != null ? `<span style="color:${h.temp >= 80 ? "#fb7185" : h.temp >= 70 ? "#fbbf24" : "inherit"}">${Math.round(h.temp)}°C</span>` : "—"}</div><div class="s">${[h.cpu != null ? "CPU " + Math.round(h.cpu) + "%" : "", h.ram != null ? "RAM " + Math.round(h.ram) + "%" : "", (h.fans || []).length ? "fan " + h.fans.map(Math.round).join("/") + " rpm" : ""].filter(Boolean).join(" · ") || "not reported"}</div></div>
        <div class="swv-kpi"><div class="l">Problems</div><div class="v">${probs.filter((p) => p.level !== "info").length || `<span style="color:#34d399">None</span>`}</div><div class="s">${probs.filter((p) => p.level === "info").length ? plural(probs.filter((p) => p.level === "info").length, "note") : "port rules watch every poll"}</div></div>
      </div>`;

      const probList = probs.length ? `<div class="swv-box"><h3>What needs attention</h3><ul class="swv-probs">${probs.slice().sort((a, b) => ({ crit: 0, warn: 1, info: 2 }[a.level] - { crit: 0, warn: 1, info: 2 }[b.level])).map((p) => `<li class="swv-prob ${p.level}"><span>${p.level === "crit" ? "🔴" : p.level === "warn" ? "🟠" : "ℹ️"}</span><div style="min-width:0"><div class="t">${esc(p.what)}</div><div class="h">${esc(p.hint)}</div></div>${p.port ? `<button class="swv-btn sm" data-port="${esc(p.port)}">Port ${esc(String(p.port).split("/").pop())}</button>` : ""}</li>`).join("")}</ul></div>` : "";

      const face = `<div class="swv-box"><h3>Front panel</h3><div class="swv-face" role="list">${ports.map((p) => {
        const s = portState(p, probs);
        const who = (p.devices || []).length ? devName(p.devices[0]) + (p.devices.length > 1 ? ` +${p.devices.length - 1}` : "") : p.up && p.unknown_macs ? plural(p.unknown_macs, "device") : !p.up && (p.last_devices || []).length ? "was: " + devName(p.last_devices[0]) : !isDefaultName(p.name) ? p.name : "";
        const sfp = /sfp/i.test(p.type) || (p.sfp && p.sfp.present);
        return `<button role="listitem" class="swv-port ${sfp ? "sfp" : ""} ${st.sel === p.id ? "sel" : ""}" data-port="${esc(p.id)}" title="Port ${esc(portNo(p))}${!isDefaultName(p.name) ? " · " + esc(p.name) : ""} · ${esc(s.label)}${p.protected ? " · " + esc(p.protected) : ""}">
          ${p.protected ? `<span class="flag" title="${esc(p.protected)}">🔒</span>` : p.uplink ? `<span class="flag" title="${p.uplink} devices behind this port">⇅</span>` : ""}
          <span class="n"><span>${sfp ? "SFP " : ""}${esc(portNo(p))}</span><i class="swv-led ${s.led}"></i></span>
          <span class="jack">${p.up ? esc(speedLabel(p)) : p.enabled ? "" : "off"}</span>
          <span class="p">${(p.poe_w || 0) >= 0.5 ? "⚡ " + (+p.poe_w).toFixed(1) + " W" : (p.poe_mode && p.poe_mode !== "off" ? "<span class='swv-dim'>⚡ on</span>" : "")}</span>
          <span class="d">${esc(who)}</span></button>`;
      }).join("")}</div>
        <div class="swv-legend"><span><i class="swv-led g"></i>1 Gbit link</span><span><i class="swv-led a"></i>slow link / to check</span><span><i class="swv-led r"></i>fault</span><span><i class="swv-led"></i>no link</span><span><i class="swv-led x"></i>switched off</span><span>🔒 Pi or router — can't be switched off</span><span>⇅ uplink</span></div></div>`;

      const table = `<div class="swv-box"><h3>Ports</h3><div class="swv-tablewrap"><table class="swv-t"><thead><tr><th>Port</th><th>Connected</th><th>Link</th><th>PoE</th><th>Traffic ${esc(st.hours === 24 ? "24 h" : st.hours / 24 + " d")}</th><th>Now ↓ / ↑</th><th>Errors</th></tr></thead><tbody>
        ${ports.map((p) => {
          const s = portState(p, probs);
          // An uplink carries dozens of devices: show a few, the port detail lists them all.
          const all = p.devices || [], SHOW = 6;
          const devs = all.slice(0, SHOW).map((x) => devChip(x)).join("")
            + (all.length > SHOW ? `<button class="swv-dev" data-port="${esc(p.id)}" title="Open the port to see all of them">+${all.length - SHOW} more</button>` : "")
            + (p.unknown_macs ? `<span class="swv-dev swv-dim">+${plural(p.unknown_macs, "unknown device")}</span>` : "");
          const last = !p.up && (p.last_devices || []).length ? `<span class="swv-note">last: ${p.last_devices.map((x) => esc(devName(x))).join(", ")}${p.last_seen_ts ? " · " + ago(p.last_seen_ts) : ""}</span>` : "";
          const errs = (p.errors || 0) + (p.dropped || 0);
          return `<tr class="port-row ${st.sel === p.id ? "sel" : ""}" data-port="${esc(p.id)}" tabindex="0">
            <td data-l="Port"><b>${esc(portNo(p))}</b>${!isDefaultName(p.name) ? ` <span class="swv-muted">${esc(p.name)}</span>` : ""}${p.protected ? " 🔒" : ""}${p.uplink ? ` <span class="swv-b" title="${p.uplink} devices behind this port">uplink</span>` : ""}</td>
            <td data-l="Connected"><div class="swv-devs">${devs || last || `<span class="swv-dim">—</span>`}</div></td>
            <td data-l="Link"><span class="swv-b ${s.cls}">${esc(p.up ? (p.speed ? (p.speed >= 1000 ? p.speed / 1000 + " Gbit" : p.speed + " Mbit") + (p.duplex === "half" ? " half" : "") : "up") : s.label)}</span></td>
            <td data-l="PoE">${p.poe_supported ? `${(p.poe_w || 0) >= 0.5 ? `<b style="color:#fde68a">${(+p.poe_w).toFixed(1)} W</b> ` : ""}<span class="swv-muted">${esc(poeLabel(p.poe_mode))}</span>` : `<span class="swv-dim">—</span>`}</td>
            <td data-l="Traffic">${spark((d.port_series || {})[p.id])}</td>
            <td data-l="Now" class="swv-tnum swv-now">${p.up ? `↓ ${bps(p.rx_bps)}<br><span class="swv-muted">↑ ${bps(p.tx_bps)}</span>` : `<span class="swv-dim">—</span>`}</td>
            <td data-l="Errors" class="swv-tnum">${errs ? `<span style="color:#fbbf24">${errs}</span>` : `<span class="swv-dim">0</span>`}</td></tr>`;
        }).join("")}</tbody></table></div><p class="swv-note" style="margin:8px 0 0">Click a port for its charts, what's plugged in${d.manage ? " and its controls" : ""}. Errors are the switch's counters since it last restarted.</p></div>`;

      const advBox = o.advanced && (d.caps || {}).advanced ? `<div class="swv-box" id="swv-adv"></div>` : "";
      root.innerHTML = title + banner + kpis + probList + face + `<div id="swv-detail"></div>` + table + advBox + `<div class="swv-grid2"><div class="swv-box" id="swv-events"></div><div class="swv-box" id="swv-info"></div></div>`;
      bindTop();
      drawDetail();
      drawEvents();
      drawInfo();
      drawAdv();
    }

    function devChip(x) {
      const href = o.deviceHref ? o.deviceHref(x) : null;
      const inner = `<i class="swv-dot ${x.online ? "" : "off"}"></i>${esc(devName(x))}${x.watch ? " 🔔" : ""}`;
      return href ? `<a class="swv-dev" href="${esc(href)}" title="${esc([x.ip, x.mac, x.vendor].filter(Boolean).join(" · "))}">${inner}</a>` : `<span class="swv-dev" title="${esc([x.ip, x.mac, x.vendor].filter(Boolean).join(" · "))}">${inner}</span>`;
    }

    function bindTop() {
      root.querySelectorAll("[data-a=poll]").forEach((b) => (b.onclick = () => pollNow(b)));
      root.querySelectorAll("[data-a=login]").forEach((b) => (b.onclick = () => o.onLogin && o.onLogin()));
      root.querySelectorAll("[data-port]").forEach((el) => {
        // stopPropagation: the "+N more" button sits inside its port's row, and both would toggle.
        const pick = (e) => { if (e.target.closest("a")) return; e.stopPropagation(); select(el.dataset.port); };
        el.onclick = pick;
        if (el.tagName === "TR") el.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(el.dataset.port); } };
      });
    }

    async function select(id) {
      st.sel = st.sel === id ? null : id;
      root.querySelectorAll(".swv-port,.port-row").forEach((el) => el.classList.toggle("sel", el.dataset.port === st.sel));
      drawDetail();
      const el = root.querySelector("#swv-detail");
      if (st.sel && el) el.scrollIntoView({ behavior: "smooth", block: "nearest" });
      if (st.sel) {
        const k = `${st.sel}|${st.hours}`;
        if (!st.portSeries[k]) {
          try {
            const j = await o.load(st.hours, st.sel);
            st.portSeries[k] = (j.port_series || {})[st.sel] || [];
          } catch (e) { st.portSeries[k] = []; }
          if (st.sel && `${st.sel}|${st.hours}` === k) drawDetail();
        }
      }
    }

    function drawDetail() {
      const el = root.querySelector("#swv-detail");
      if (!el) return;
      const d = st.data;
      const p = st.sel && (d.ports || []).find((x) => x.id === st.sel);
      if (!p) { el.innerHTML = ""; return; }
      const probs = (d.problems || []).filter((x) => x.port === p.id);
      const rows = st.portSeries[`${p.id}|${st.hours}`];
      const s = portState(p, d.problems || []);
      const vl = p.vlans || {};
      const devs = (p.devices || []).map(devChip).join("");
      const manage = d.manage;
      const modes = (p.poe_modes || []).length ? p.poe_modes : p.poe_supported ? ["off", "active", "24v"] : [];
      let controls;
      if (!manage) {
        controls = `<div class="swv-lock">🔒 Switching ports and PoE from here is turned off for this site.${o.settingsHref ? ` <a href="${esc(o.settingsHref)}" style="color:#a5b4fc">Turn on “Manage switches”</a>` : " Turn on “Manage switches” in the site's Settings."}</div>`;
      } else if (p.protected) {
        controls = `<div class="swv-lock">🔒 ${esc(p.protected.charAt(0).toUpperCase() + p.protected.slice(1))} — Netwatch won't switch this port off, change its PoE or run a cable test on it.</div>
          <div class="swv-row" style="margin-top:8px"><button class="swv-btn sm" data-x="rename">✏️ Rename</button></div>`;
      } else {
        controls = `<div class="swv-row">
          ${p.enabled ? `<button class="swv-btn danger" data-x="off">⛔ Turn port off</button>` : `<button class="swv-btn pri" data-x="on">✅ Turn port on</button>`}
          ${p.poe_supported && p.poe_mode && p.poe_mode !== "off" ? `<button class="swv-btn" data-x="cycle" title="PoE off for 8 seconds, then back on — restarts the powered device">⚡ Power cycle</button>` : ""}
          ${modes.length ? `<label class="swv-row" style="gap:6px"><span class="swv-muted" style="font-size:13px">PoE</span><select class="swv-sel" data-x="poe">${modes.map((m) => `<option value="${esc(m)}" ${m === p.poe_mode ? "selected" : ""}>${esc(poeLabel(m))}</option>`).join("")}</select></label>` : ""}
          <button class="swv-btn sm" data-x="rename">✏️ Rename</button>
          ${(d.caps || {}).cable_test === false ? "" : `<button class="swv-btn sm" data-x="cable" title="Measures the cable pairs — the link drops for a few seconds">📏 Cable test</button>`}
        </div>
        ${p.uplink ? `<p class="swv-note" style="margin:6px 0 0">⇅ ${p.uplink} devices are behind this port — switching it off takes all of them down.</p>` : ""}
        ${modes.some((m) => /\d+v/i.test(m)) ? `<p class="swv-note" style="margin:6px 0 0">⚠ Passive 24 V / 48 V PoE can damage devices that don't expect it — only choose it for gear that needs passive power.</p>` : ""}
        ${modes.includes("on") && modes.includes("auto") ? `<p class="swv-note" style="margin:6px 0 0">⚠ This switch's PoE is passive, at its own input voltage. “Forced on” powers the port even when the device doesn't ask for it — keep it on Auto unless the device needs it.</p>` : ""}
        <div id="swv-cable"></div>`;
      }
      el.innerHTML = `<div class="swv-box swv-detail" style="border-color:rgba(34,211,238,.35)">
        <div class="swv-head"><div><h2 style="font-size:17px">Port ${esc(portNo(p))}${!isDefaultName(p.name) ? ` · ${esc(p.name)}` : ""} <span class="swv-b ${s.cls}">${esc(s.label)}</span></h2>
          <p class="swv-muted">${p.up ? `${p.speed ? p.speed + " Mbit/s " + (p.duplex || "") : "link up"} · ` : ""}${p.poe_supported ? "PoE " + esc(poeLabel(p.poe_mode)) + ((p.poe_w || 0) >= 0.5 ? ` drawing ${(+p.poe_w).toFixed(1)} W` : p.poe_w == null && p.poe_mode && p.poe_mode !== "off" ? " (power not measured)" : "") + (p.poe_status ? " · " + esc(p.poe_status) : "") : "no PoE"}</p></div>
          <div class="swv-row"><div class="swv-seg" role="group" aria-label="Time range">${[[24, "24 h"], [168, "7 d"], [720, "30 d"]].map(([hh, l]) => `<button data-h="${hh}" class="${st.hours === hh ? "on" : ""}">${l}</button>`).join("")}</div><button class="swv-btn sm" data-x="close" aria-label="Close port details">✕</button></div></div>
        ${probs.length ? `<ul class="swv-probs">${probs.map((x) => `<li class="swv-prob ${x.level}"><span>${x.level === "crit" ? "🔴" : x.level === "warn" ? "🟠" : "ℹ️"}</span><div><div class="t">${esc(x.what)}</div><div class="h">${esc(x.hint)}</div></div><span></span></li>`).join("")}</ul>` : ""}
        <div class="swv-grid2">
          <div><div class="swv-note">Traffic</div>${rows ? lineChart(rows, [{ k: "rx_bps", c: "#22d3ee", label: "download (into switch)" }, { k: "tx_bps", c: "#a78bfa", label: "upload (out of port)" }], { fmt: bps, showDown: true, aria: "Port traffic" }) : `<div class="swv-skel" style="height:120px"></div>`}</div>
          <div><div class="swv-note">PoE power (W)</div>${!p.poe_supported ? `<p class="swv-note">This port has no PoE.</p>` : rows ? lineChart(rows, [{ k: "poe_w", c: "#fde68a", label: "watts" }], { fmt: (v) => v.toFixed(1) + " W", aria: "PoE draw", emptyNote: p.poe_mode === "off" ? "PoE is switched off on this port." : /v/.test(p.poe_mode || "") ? "The switch doesn't measure power on passive PoE ports." : "Nothing drew PoE power on this port in this period." }) : `<div class="swv-skel" style="height:120px"></div>`}</div>
        </div>
        <div class="swv-grid2">
          <div><h3>Plugged in</h3>${devs ? `<div class="swv-devs">${devs}</div>` : ""}${p.unknown_macs ? `<p class="swv-note" style="margin:6px 0 0">${plural(p.unknown_macs, "device")} Netwatch doesn't know (not on the scanned network, or behind another switch).</p>` : ""}${!devs && !p.unknown_macs ? (p.last_devices || []).length ? `<p class="swv-muted" style="margin:0">Nothing now. Last seen here ${p.last_seen_ts ? ago(p.last_seen_ts) : ""}: ${p.last_devices.map(devChip).join(" ")}</p>` : `<p class="swv-muted" style="margin:0">Nothing seen on this port.</p>` : ""}</div>
          <div><h3>Settings</h3><dl class="swv-kv">
            <dt>Port id</dt><dd class="swv-mono">${esc(p.id)}</dd>
            <dt>VLAN</dt><dd>${(vl.untagged || []).length ? "untagged " + vl.untagged.map(esc).join(", ") : "—"}${(vl.tagged || []).length ? " · tagged " + vl.tagged.map(esc).join(", ") : ""}</dd>
            ${p.speed_cfg ? `<dt>Speed setting</dt><dd>${esc(p.speed_cfg)}</dd>` : ""}
            ${p.stp_state ? `<dt>Spanning tree</dt><dd>${esc(p.stp_state)}</dd>` : ""}
            ${p.isolated ? `<dt>Isolated</dt><dd>yes</dd>` : ""}
            ${p.ping_watchdog ? `<dt>Ping watchdog</dt><dd>on (the switch power-cycles PoE itself when the device stops answering)</dd>` : ""}
            ${p.sfp && p.sfp.present ? `<dt>SFP module</dt><dd>${esc([p.sfp.vendor, p.sfp.part].filter(Boolean).join(" ") || "present")}${p.sfp.temp != null ? " · " + p.sfp.temp + "°C" : ""}${p.sfp.rx_power != null ? " · rx " + p.sfp.rx_power + " dBm" : ""}</dd>` : ""}
            <dt>Totals</dt><dd class="swv-tnum">↓ ${bytes(p.rx_bytes)} · ↑ ${bytes(p.tx_bytes)}</dd>
            <dt>Errors / drops</dt><dd class="swv-tnum">${p.errors ?? "—"} / ${p.dropped ?? "—"}</dd>
          </dl></div>
        </div>
        <div><h3>Control</h3>${controls}</div></div>`;
      el.querySelectorAll("[data-h]").forEach((b) => (b.onclick = async () => {
        st.hours = +b.dataset.h;
        drawDetail();
        const k = `${p.id}|${st.hours}`;
        if (!st.portSeries[k]) {
          try { st.portSeries[k] = ((await o.load(st.hours, p.id)).port_series || {})[p.id] || []; } catch (e) { st.portSeries[k] = []; }
        }
        if (st.sel === p.id) drawDetail();
      }));
      const x = (n) => el.querySelector(`[data-x="${n}"]`);
      if (x("close")) x("close").onclick = () => select(p.id);
      if (x("off")) x("off").onclick = async (e) => { if (await o.confirm(`Turn port ${portNo(p)} off?`, `${(p.devices || []).length ? p.devices.map(devName).join(", ") + " will lose the network" : "Anything plugged in loses the network"}${(p.poe_w || 0) >= 0.5 ? " and its PoE power" : ""} until the port is turned back on.`, { danger: true })) act({ action: "port", port: p.id, enabled: false }, e.currentTarget, `Port ${portNo(p)} turned off`); };
      if (x("on")) x("on").onclick = (e) => act({ action: "port", port: p.id, enabled: true }, e.currentTarget, `Port ${portNo(p)} turned on`);
      if (x("cycle")) x("cycle").onclick = async (e) => { if (await o.confirm(`Power cycle port ${portNo(p)}?`, `PoE goes off for 8 seconds and comes back on. ${(p.devices || []).length ? p.devices.map(devName).join(", ") + " restarts" : "The powered device restarts"} — cameras and radios take 1–2 minutes to come back.`)) act({ action: "poe-cycle", port: p.id, off_s: 8 }, e.currentTarget, `Port ${portNo(p)} power cycled`); };
      if (x("poe")) x("poe").onchange = async (e) => {
        const m = e.target.value;
        const ok = await o.confirm(`Set PoE on port ${portNo(p)} to “${poeLabel(m)}”?`, m === "off" ? "The powered device switches off." : /v/.test(m) ? "Passive PoE sends power whether or not the device asks for it — wrong voltage can destroy a device." : "", { danger: m === "off" || /v/.test(m) });
        if (!ok) { e.target.value = p.poe_mode; return; }
        const r = await act({ action: "poe", port: p.id, mode: m }, e.target, `PoE set to ${poeLabel(m)}`);
        if (!r) e.target.value = p.poe_mode;
      };
      if (x("rename")) x("rename").onclick = async (e) => {
        const n = window.prompt(`Name for port ${portNo(p)} (shown on the switch too)`, isDefaultName(p.name) ? ((p.devices || [])[0] ? devName(p.devices[0]) : "") : p.name);
        if (n == null) return;
        act({ action: "name", port: p.id, name: n.trim() }, e.currentTarget, "Port renamed");
      };
      if (x("cable")) x("cable").onclick = async (e) => {
        if (!(await o.confirm(`Cable test on port ${portNo(p)}?`, "The link drops for a few seconds while the switch measures the pairs.", { danger: true }))) return;
        const r = await act({ action: "cable-test", port: p.id }, e.currentTarget, "Cable test finished");
        const out = el.querySelector("#swv-cable");
        if (r && out) out.innerHTML = cableHtml(r.result);
      };
    }

    function cableHtml(res) {
      res = res || {};
      if (res.text == null) return `<pre class="swv-mono" style="white-space:pre-wrap;margin:8px 0 0;font-size:12px;background:#050b16;border:1px solid rgba(148,163,184,.2);border-radius:8px;padding:8px">${esc(JSON.stringify(res, null, 2))}</pre>`;
      return `<div class="swv-res ${res.good ? "ok" : "warn"}">${res.good ? "✅" : "⚠️"} <b>${esc(res.text.charAt(0).toUpperCase() + res.text.slice(1))}</b>${res.length_m != null ? ` · cable about ${esc(Math.round(res.length_m))} m` : ""}${res.status ? ` <span class="swv-dim">(${esc(res.status)})</span>` : ""}
        ${res.good ? "" : `<div class="swv-note" style="margin-top:3px">Open = a core is broken or not punched down; short = two cores touch; high impedance = a bad crimp or a corroded joint. Re-terminate both ends first.</div>`}</div>`;
    }

    // ---- advanced settings (Hikvision smart switches: hikswitch_adv on the site) -----------------
    async function advLoad(refresh) {
      st.advOpen = true; st.advErr = "";
      if (refresh) st.adv = null;
      drawAdv();
      try { st.adv = await o.advanced.load(!!refresh); st.advPick = {}; }
      catch (e) { st.advErr = (e.body && e.body.error) || e.message || String(e); }
      if (!st.dead) drawAdv();
    }

    async function advSet(body, btn, okMsg) {
      if (btn) btn.disabled = true;
      const send = async (b) => {
        try { return await o.advanced.set(b); }
        catch (e) { if (e.body && e.body.needs_confirm) return e.body; throw e; }
      };
      try {
        let r = await send(body);
        if (r && r.needs_confirm) {
          if (!(await o.confirm("Are you sure?", r.warning, { danger: true }))) return null;
          r = await send(Object.assign({}, body, { confirm: true }));
        }
        if (!r || r.ok === false) throw new Error((r && r.error) || "failed");
        if (r.advanced) st.adv = r.advanced;
        else if (st.adv && r.shows && r.after) st.adv.features[r.shows] = r.after;
        delete st.advPick[body.feature];
        o.toast(okMsg || "Saved on the switch", "ok");
        drawAdv();
        setTimeout(() => refresh(true), 4000);
        return r;
      } catch (e) {
        o.toast((e.body && e.body.error) || e.message || String(e), "bad");
        return null;
      } finally { if (btn) btn.disabled = false; }
    }

    function drawAdv() {
      const el = root.querySelector("#swv-adv");
      if (!el) return;
      const d = st.data || {};
      if (!st.advOpen) {
        el.innerHTML = `<div class="swv-head"><div style="min-width:0"><h3 style="margin:0">Advanced settings</h3>
          <p class="swv-note" style="margin:4px 0 0">Loop protection (RSTP and the switch's own loop guard), storm control, PoE watchdog, long-range ports, DHCP snooping, LLDP, QoS, bandwidth limits, cable test, SFP diagnostics and the clock — read from the switch with its saved login.</p></div>
          <button class="swv-btn" data-adv="open">⚙️ Show advanced settings</button></div>`;
        el.querySelector("[data-adv=open]").onclick = () => advLoad(false);
        return;
      }
      const a = st.adv;
      if (!a) {
        el.innerHTML = `<h3>Advanced settings</h3>` + (st.advErr
          ? `<div class="swv-banner bad">${esc(st.advErr)}</div><div class="swv-row" style="margin-top:8px"><button class="swv-btn" data-adv="reload">↻ Try again</button></div>`
          : `<div class="swv-skel"></div><p class="swv-note">Reading the switch's settings (one login)…</p>`);
        const b = el.querySelector("[data-adv=reload]"); if (b) b.onclick = () => advLoad(true);
        return;
      }
      const f = a.features || {}, manage = !!a.manage, prot = a.protected || {}, ups = a.uplinks || {};
      const vp = (id) => (d.ports || []).find((p) => p.id === id) || {};
      const pn = (id) => String(id).split("/").pop();
      const pTitle = (p) => { const v = vp(p.id); return `Port ${pn(p.id)}${v.name && !isDefaultName(v.name) ? " · " + v.name : ""}${(v.devices || []).length ? " · " + v.devices.map(devName).join(", ") : ""}${prot[p.id] ? " · 🔒 " + prot[p.id] : ups[p.id] ? ` · ${ups[p.id]} devices behind it` : ""}`; };
      const portsTxt = (ids) => (ids || []).length ? ids.map((x) => "port " + pn(x)).join(", ") : "none";
      const badge = (v, onTxt, offTxt) => v == null ? `<span class="swv-b">—</span>` : v ? `<span class="swv-b ok">${onTxt || "On"}</span>` : `<span class="swv-b">${offTxt || "Off"}</span>`;
      const J = (o2) => esc(JSON.stringify(o2));
      const setBtn = (body, label, cls, ask) => manage ? `<button class="swv-btn sm ${cls || ""}" data-set="${J(body)}"${ask ? ` data-ask="${esc(ask)}"` : ""}>${label}</button>` : "";
      const toggle = (feature, cur, what, ask) => cur == null ? "" : setBtn({ feature, enabled: !cur }, cur ? `Turn ${what} off` : `Turn ${what} on`, cur ? "" : "pri", ask === false ? "" : ask || `Turn ${what} ${cur ? "off" : "on"} on this switch?`);
      const card = (title, state, desc, body) => `<div class="swv-feat"><div class="swv-feat-h"><b>${title}</b>${state || ""}</div><div class="swv-note">${desc}</div>${body || ""}</div>`;
      const na = (x) => !x || x.supported === false;
      const err = (x) => x && x.error ? `<div class="swv-note" style="color:#fbbf24">Couldn't read: ${esc(x.error)}</div>` : "";
      const edge = (a.ports || []).filter((p) => p.up && !prot[p.id] && !ups[p.id]).map((p) => p.id);

      // per-port chips: the switch's current state, with clicks collected until Save
      function chips(feature, opt) {
        opt = opt || {};
        const on = new Set(advCur(feature)), pend = st.advPick[feature] || new Set(), rng = opt.range;
        return `<div class="swv-chips">${(a.ports || []).map((p) => {
          const n = +pn(p.id);
          const no = (rng && (n < rng[0] || n > rng[1])) || (opt.copper && p.type === "sfp");
          const lock = opt.lock && prot[p.id] && !on.has(p.id);
          const isOn = on.has(p.id) !== pend.has(p.id);
          return `<button class="swv-chip ${isOn ? "on" : ""} ${pend.has(p.id) ? "chg" : ""} ${no ? "no" : lock ? "lock" : ""}" data-chip="${esc(feature)}" data-p="${esc(p.id)}" ${!manage || no || lock ? "disabled" : ""} title="${esc(pTitle(p) + (no ? " — this port doesn't have it" : lock ? " — Netwatch won't change this port" : ""))}">${esc(pn(p.id))}</button>`;
        }).join("")}</div>`;
      }
      const saveRow = (feature, label) => {
        const pend = st.advPick[feature];
        if (!manage || !pend || !pend.size) return "";
        return `<div class="swv-row" style="margin-top:4px"><button class="swv-btn sm pri" data-save="${esc(feature)}">${label || `Save ${plural(pend.size, "change")}`}</button><button class="swv-btn sm" data-undo="${esc(feature)}">Undo</button></div>`;
      };
      const pick = (feature, ids) => manage && ids.length ? `<button class="swv-btn sm" data-pick="${esc(feature)}" data-ids="${J(ids)}">Pick all device ports</button>` : "";

      // ---- what Netwatch suggests
      const tips = (a.advice || []);
      const tipsHtml = tips.length ? `<ul class="swv-tips">${tips.map((t, i) => {
        let btn = "";
        if (manage && t.change && t.feature === "cable_test") btn = `<button class="swv-btn sm pri" data-cable="${esc(t.change.port)}">Test port ${esc(pn(t.change.port))}</button>`;
        else if (manage && t.change) btn = `<button class="swv-btn sm ${t.level === "do" ? "pri" : ""}" data-set="${J(Object.assign({ feature: t.feature }, t.change))}"${["rstp", "poe_watchdog", "dhcp_snooping"].includes(t.feature) ? "" : ` data-ask="${esc(t.title + "?")}"`}>${t.level === "info" ? "OK" : "Do it"}</button>`;
        return `<li class="swv-tip ${esc(t.level)}"><span>${t.level === "do" ? "👉" : t.level === "consider" ? "💡" : "ℹ️"}</span><div style="min-width:0"><div class="t">${esc(t.title)}</div><div class="h">${esc(t.why)}</div>${t.feature === "cable_test" && st.advOut["cable:" + (t.change || {}).port] ? st.advOut["cable:" + t.change.port] : ""}</div>${btn}</li>`;
      }).join("")}</ul>` : `<p class="swv-muted" style="margin:0">Nothing to suggest — this switch's protections are set the way Netwatch would set them.</p>`;

      // ---- loop protection
      const r = f.rstp || {}, lg = f.loop_guard || {};
      const rstpBody = !na(r) ? `${r.enabled ? `<div class="swv-note">${r.is_root ? "This switch is the <b>root bridge</b> (the centre of the tree)." : r.root_id ? `Root bridge: <span class="swv-mono">${esc(r.root_id)}</span>` : ""}${r.priority != null ? ` · bridge priority ${esc(r.priority)}` : ""}</div>
          ${(r.blocked || []).length ? `<div class="swv-note" style="color:#fbbf24">Blocking ${esc(portsTxt(r.blocked))} — a second path, kept as a standby.</div>` : `<div class="swv-note">No port blocked — there is no second path.</div>`}
          <details><summary class="swv-note" style="cursor:pointer">Port roles</summary><div class="swv-chips" style="margin-top:4px">${(a.ports || []).filter((p) => p.up).map((p) => { const x = (r.ports || {})[p.id] || {}; return `<span class="swv-b ${x.state === "forwarding" ? "" : "warn"}" title="${esc(pTitle(p))}">${esc(pn(p.id))}: ${esc(x.role || "?")}${x.state && x.state !== "forwarding" ? " · " + esc(x.state) : ""}</span>`; }).join("")}</div></details>` : ""}
        ${manage ? `<div class="swv-row" style="margin-top:4px">${setBtn({ feature: "rstp", enabled: !r.enabled }, r.enabled ? "Turn RSTP off" : "Turn RSTP on", r.enabled ? "" : "pri")}
          ${r.enabled ? `<label class="swv-row" style="gap:6px"><span class="swv-note">Priority</span><select class="swv-sel" data-prio>${Array.from({ length: 16 }, (_, i) => i * 4096).map((v) => `<option value="${v}" ${v === r.priority ? "selected" : ""}>${v}${v === 4096 ? " (make root)" : v === 32768 ? " (default)" : ""}</option>`).join("")}</select></label>` : ""}</div>` : ""}` : err(r);
      const loopSec = `<div class="swv-adv-sec"><h3>Loop protection</h3><div class="swv-adv-grid">
        ${na(r) && !r.error ? "" : card("RSTP (spanning tree)", badge(r.enabled), "Switches agree on one path through the network, so a second cable between two switches waits as a standby instead of flooding the network. Standard: works with MikroTik and other managed switches.", rstpBody)}
        ${na(lg) && !lg.error ? "" : card("Loop protection", badge(lg.enabled), "Hikvision's own loop detection: when a loop forms behind a port — even through a cheap unmanaged switch — the switch blocks just that port.",
          (lg.enabled && (lg.blocked || []).length ? `<div class="swv-note" style="color:#fb7185">Blocked now: ${esc(portsTxt(lg.blocked))}</div>` : "") + err(lg) + (manage && lg.supported ? `<div class="swv-row" style="margin-top:4px">${setBtn({ feature: "loop_guard", enabled: !lg.enabled }, lg.enabled ? "Turn off" : "Turn on", lg.enabled ? "" : "pri", lg.enabled ? "" : "Turn loop protection on?")}</div>` : ""))}
        ${na(f.erps) ? "" : card("ERPS ring protection", badge(f.erps.enabled), "For switches cabled in a ring: one link waits as a standby and takes over in under a second. Set it up on the switch's own page — every switch in the ring needs it.", f.erps.enabled ? `<div class="swv-note">Ring ports: ${esc((f.erps.ports || []).join(", "))}</div>` : "")}
        ${na(r) && na(lg) ? `<p class="swv-muted">This switch has no loop protection settings.</p>` : ""}
      </div></div>`;

      // ---- traffic
      const sm = f.storm || {}, rl = f.rate_limit || {}, q = f.qos || {};
      const MODE = { broadCast: "broadcast", multiCast: "multicast", unknownUnicast: "unknown-unicast", broadcast: "broadcast", multicast: "multicast", unicast: "unknown-unicast" };
      const g = sm.global;
      const stormBody = na(sm) ? err(sm) : `${g ? `<div class="swv-note">Limits <b>${esc(MODE[g.mode] || g.mode)}</b> traffic to <b>${esc(g.ratio_pct)}%</b> of the port's speed on the ports marked green.</div>
          ${manage ? `<div class="swv-row"><select class="swv-sel" data-storm-mode>${(g.modes || []).map((m) => `<option value="${esc(m)}" ${m === g.mode ? "selected" : ""}>${esc(MODE[m] || m)}</option>`).join("")}</select><input class="swv-in" data-storm-ratio type="number" min="${esc((g.ratio_range || [1])[0])}" max="${esc((g.ratio_range || [1, 100])[1])}" value="${esc(g.ratio_pct)}" style="width:70px"> %<button class="swv-btn sm" data-storm-global>Save limit</button></div>` : ""}`
          : `<div class="swv-note">New ports get <b>broadcast</b> limited to <input class="swv-in" data-storm-lim type="number" value="10" min="1" max="1000" style="width:64px"> Mbit/s.</div>`}
        ${chips("storm")}${saveRow("storm")}<div class="swv-row">${pick("storm", edge)}</div>`;
      const limited = Object.entries(rl.ports || {}).filter(([, v]) => v.rx_mbps != null || v.tx_mbps != null);
      const rateBody = na(rl) ? err(rl) : `${limited.length ? `<div class="swv-chips">${limited.map(([k, v]) => `<span class="swv-b info">port ${esc(pn(k))}: ↓${esc(v.rx_mbps ?? "–")} ↑${esc(v.tx_mbps ?? "–")} Mbit/s ${manage ? `<button class="swv-btn sm" style="padding:0 5px" data-set="${J({ feature: "rate_limit", ports: [k], rx_mbps: null, tx_mbps: null })}" data-ask="Remove the limit on port ${esc(pn(k))}?" title="Remove the limit">✕</button>` : ""}</span>`).join("")}</div>` : `<div class="swv-note">No port is limited.</div>`}
        ${manage ? `<div class="swv-row"><select class="swv-sel" data-rl-port>${(a.ports || []).map((p) => `<option value="${esc(p.id)}" ${prot[p.id] ? "disabled" : ""}>Port ${esc(pn(p.id))}${prot[p.id] ? " 🔒" : ""}</option>`).join("")}</select>↓<input class="swv-in" data-rl-rx type="number" min="1" placeholder="in" style="width:62px">↑<input class="swv-in" data-rl-tx type="number" min="1" placeholder="out" style="width:62px">Mbit/s<button class="swv-btn sm" data-rl-save>Set limit</button></div>` : ""}`;
      const qosBody = na(q) ? err(q) : `${q.enabled ? `<div class="swv-note">${q.type === "redPort" ? "High-priority port mode" : `QoS ${esc(q.algorithm || "")}${q.algorithm === "WRR" ? ` · high:low weight ${esc(q.high_weight)}:${esc(q.low_weight)}` : ""}`}. High-priority ports are green.</div>` : ""}
        ${manage && q.can_set ? `<div class="swv-row">${setBtn(q.enabled ? { feature: "qos", enabled: false } : { feature: "qos", enabled: true, algorithm: "WRR", high_weight: 8, low_weight: 1 }, q.enabled ? "Turn QoS off" : "Turn QoS on (WRR 8:1)", "", q.enabled ? "Turn QoS off?" : "Turn QoS on? Ports you mark high-priority then go first when a link is full.")}</div>` : ""}
        ${q.enabled ? chips("qos", { range: q.port_range }) + saveRow("qos") : ""}`;
      const trafficSec = na(sm) && na(rl) && na(q) ? "" : `<div class="swv-adv-sec"><h3>Traffic</h3><div class="swv-adv-grid">
        ${na(sm) && !sm.error ? "" : card("Storm control", `<span class="swv-b ${advCur("storm").length ? "ok" : ""}">${advCur("storm").length ? plural(advCur("storm").length, "port") : "Off"}</span>`, "Caps broadcast floods per port, so one faulty device or a loop can't swamp the rest of the network.", stormBody)}
        ${na(rl) && !rl.error ? "" : card("Bandwidth limits", limited.length ? `<span class="swv-b info">${plural(limited.length, "port")}</span>` : `<span class="swv-b">None</span>`, "Caps how fast one port may send or receive — e.g. so one camera can't fill a slow radio link.", rateBody)}
        ${na(q) && !q.error ? "" : card("QoS / priority", badge(q.enabled), "When a link is full, traffic from high-priority ports goes first (e.g. the recorder, or a gate controller behind a radio link).", qosBody)}
      </div></div>`;

      // ---- PoE and cabling
      const wd = f.poe_watchdog || {}, lr = f.long_range || {}, eee = f.eee || {}, ct = f.cable_test || {}, sfp = f.sfp || {};
      const lrBody = na(lr) ? err(lr) : `${lr.auto != null ? `<div class="swv-row"><span class="swv-note">Automatic long-range: ${lr.auto ? "<b>on</b> — the switch picks the ports itself" : "off"}</span>${setBtn({ feature: "long_range", auto: !lr.auto }, lr.auto ? "Choose ports myself" : "Let the switch decide", "", lr.auto ? "Turn automatic long-range mode off?" : "Turn automatic long-range mode on?")}</div>` : ""}
        ${lr.auto ? "" : chips("long_range", { range: lr.port_range, lock: true }) + saveRow("long_range")}`;
      const copper = (a.ports || []).filter((p) => p.type !== "sfp" && !(ct.port_range && (+pn(p.id) < ct.port_range[0] || +pn(p.id) > ct.port_range[1])));
      const ctBody = manage ? `<div class="swv-row"><select class="swv-sel" data-ct-port>${copper.map((p) => `<option value="${esc(p.id)}" ${prot[p.id] ? "disabled" : ""}>Port ${esc(pn(p.id))}${prot[p.id] ? " 🔒" : ""}${vp(p.id).name && !isDefaultName(vp(p.id).name) ? " · " + esc(vp(p.id).name) : ""}</option>`).join("")}</select><button class="swv-btn sm" data-ct-run>📏 Test cable</button></div>${st.advOut.cable || ""}` : "";
      const mods = Object.entries(sfp.modules || {});
      const sfpBody = mods.length ? mods.map(([k, m]) => m.error ? `<div class="swv-note">Port ${esc(pn(k))}: ${esc(m.error)}</div>` : `<div class="swv-note"><b>Port ${esc(pn(k))}</b> ${esc([m.vendor, m.type, m.wavelength_nm ? m.wavelength_nm + " nm" : ""].filter(Boolean).join(" · "))}<br>rx ${m.rx_dbm != null ? esc(m.rx_dbm.toFixed(1)) + " dBm" : "—"} · tx ${m.tx_dbm != null ? esc(m.tx_dbm.toFixed(1)) + " dBm" : "—"} · ${m.temp_c != null ? esc(Math.round(m.temp_c)) + "°C" : ""}</div>`).join("") : `<div class="swv-note">No SFP module with a link.</div>`;
      const poeSec = `<div class="swv-adv-sec"><h3>PoE and cabling</h3><div class="swv-adv-grid">
        ${na(wd) && !wd.error ? "" : card("PoE watchdog", badge(wd.enabled), "A camera that hangs keeps drawing power but stops sending data. The watchdog notices and power-cycles that port by itself.", err(wd) + (manage && wd.supported ? `<div class="swv-row" style="margin-top:4px">${setBtn({ feature: "poe_watchdog", enabled: !wd.enabled }, wd.enabled ? "Turn off" : "Turn on", wd.enabled ? "" : "pri", wd.enabled ? "Turn the PoE watchdog off? Hung cameras then stay down until someone restarts them." : "")}</div>` : ""))}
        ${na(lr) && !lr.error ? "" : card("Long-range ports", `<span class="swv-b ${(lr.ports_on || []).length ? "info" : ""}">${(lr.ports_on || []).length ? plural(lr.ports_on.length, "port") : lr.auto ? "Auto" : "None"}</span>`, "A long-range port runs at 10 Mbit/s so its cable can reach about 250–300 m — for a camera far beyond the normal 100 m.", lrBody)}
        ${na(eee) && !eee.error ? "" : card("EEE (energy saving)", badge(eee.enabled), "Energy-efficient Ethernet lets quiet links nap. Some cameras and radios drop or count errors with it; keep it off if ports misbehave.", err(eee) + (manage && eee.supported ? `<div class="swv-row" style="margin-top:4px">${toggle("eee", eee.enabled, "EEE")}</div>` : ""))}
        ${ct.supported === false ? "" : card("Cable test", "", "Measures each pair of a copper cable: broken, short, badly crimped, and roughly how long. The link drops for a few seconds.", ctBody)}
        ${na(sfp) ? "" : card("SFP modules", "", "Fibre/SFP module health: light received and sent, temperature.", sfpBody)}
        ${card("Port counters", "", "Clear the switch's traffic and error counters — after fixing a cable, to see whether errors come back.", manage ? `<div class="swv-row" style="margin-top:4px">${setBtn({ feature: "counters" }, "Clear counters", "", "Clear every port's traffic and error counters on this switch?")}</div>` : "")}
      </div></div>`;

      // ---- discovery and security
      const ld = f.lldp || {}, dh = f.dhcp_snooping || {}, iso = f.isolation || {}, acc = f.access || {}, ps = f.port_security || {}, ot = f.other || {};
      const lldpBody = err(ld) + (ld.enabled && (ld.neighbors || []).length ? `<div class="swv-tablewrap"><table class="swv-t"><thead><tr><th>Port</th><th>Neighbour</th><th>Its port</th></tr></thead><tbody>${ld.neighbors.map((n) => `<tr><td data-l="Port">${esc(n.port ? pn(n.port) : n.port_label)}</td><td data-l="Neighbour">${esc(n.name || n.ip || n.mac)}${n.ip && n.name ? ` <span class="swv-dim swv-mono">${esc(n.ip)}</span>` : ""}</td><td data-l="Its port">${esc(n.peer_port)}</td></tr>`).join("")}</tbody></table></div>` : ld.enabled ? `<div class="swv-note">No LLDP neighbours yet.</div>` : "")
        + (manage && ld.supported ? `<div class="swv-row" style="margin-top:4px">${toggle("lldp", ld.enabled, "LLDP")}</div>` : "");
      const dhBody = na(dh) ? err(dh) : `<div class="swv-note">${dh.enabled ? `Trusted (may hand out addresses): <b>${esc(portsTxt(dh.trusted))}</b>.${(dh.entries || []).length ? ` ${plural(dh.entries.length, "lease")} seen.` : ""}` : "Pick the port(s) the DHCP server (usually the router) is behind — Netwatch pre-picks the router's port:"}</div>
        ${manage ? chips("dhcp_snooping") + (dh.enabled ? saveRow("dhcp_snooping", "Save trusted ports") : "") : ""}
        ${manage ? `<div class="swv-row" style="margin-top:4px">${dh.enabled ? setBtn({ feature: "dhcp_snooping", enabled: false }, "Turn off", "", "Turn DHCP snooping off?") : `<button class="swv-btn sm pri" data-dh-on>Turn on, trusting the green ports</button>`}</div>` : ""}`;
      const isoBody = na(iso) ? err(iso) : `<div class="swv-note">${(iso.ports || []).length ? `Isolated: <b>${esc(portsTxt(iso.ports))}</b> — they can't reach each other, only the other ports.` : "No port isolated."}</div>${chips("isolation", { range: iso.port_range, lock: true })}${saveRow("isolation")}`;
      const accRows = [
        ["Telnet", acc.telnet, "telnet", "Old remote console — sends the password in plain text. Keep it off."],
        ["ONVIF discovery", acc.onvif, "onvif", "Answers ONVIF searches from recorders and VMS software. A switch doesn't need it."],
        ["SADP agent", acc.sadp_agent, "sadp_agent", "Broadcasts a discovery packet every minute to draw Hikvision's topology. Netwatch doesn't need it."],
      ];
      const accBody = `<dl class="swv-kv">${accRows.filter((x) => x[1] != null).map(([l, v, k, h]) => `<dt>${esc(l)}</dt><dd>${badge(v)} ${manage ? setBtn({ feature: k, enabled: !v }, v ? "Turn off" : "Turn on", "", `Turn ${l} ${v ? "off" : "on"}?`) : ""}<div class="swv-note">${esc(h)}</div></dd>`).join("")}
        ${acc.sadp_server != null ? `<dt>SADP server</dt><dd>${badge(acc.sadp_server)}<div class="swv-note">Lets SADP tools (and Netwatch) find the switch — keep it on.</div></dd>` : ""}
        ${acc.http_port != null ? `<dt>Web page</dt><dd>HTTP port ${esc(acc.http_port)}${acc.https != null ? ` · HTTPS ${acc.https ? "on" : "off"}${acc.https_port ? " (" + esc(acc.https_port) + ")" : ""}${acc.https_redirect ? " · redirects to HTTPS" : ""}` : ""}<div class="swv-note">Netwatch talks to the switch over HTTP — don't force HTTPS-only from the switch's own page.</div></dd>` : ""}
        ${acc.ssh != null ? `<dt>SSH</dt><dd>${badge(acc.ssh)}</dd>` : ""}
        ${acc.cloud != null ? `<dt>Hik-Connect cloud</dt><dd>${badge(acc.cloud)}</dd>` : ""}
        ${ps.supported ? `<dt>Port security</dt><dd>${ps.enabled ? "on (" + esc(ps.mode) + ")" : "off"}</dd>` : ""}</dl>
        ${ot.supported && [ot.acl, ot.ip_source_guard, ot.arp_protection].some(Boolean) ? `<div class="swv-note" style="margin-top:6px">Also on this switch, set on its own page: ${esc([ot.acl && "access lists (ACL)", ot.ip_source_guard && "IP source guard", ot.arp_protection && "ARP gateway protection", ot.dscp && "DSCP priority"].filter(Boolean).join(", "))}.</div>` : ""}`;
      const secSec = `<div class="swv-adv-sec"><h3>Discovery and security</h3><div class="swv-adv-grid">
        ${na(ld) && !ld.error ? "" : card("LLDP", badge(ld.enabled), "Switches, routers and many cameras announce themselves to their neighbour, so you can see what is on each port.", lldpBody)}
        ${na(dh) && !dh.error ? "" : card("DHCP snooping", badge(dh.enabled), "Only trusted ports may hand out IP addresses: a home router or phone hotspot plugged in by mistake can't knock cameras off the network.", dhBody)}
        ${na(iso) && !iso.error ? "" : card("Port isolation", `<span class="swv-b ${(iso.ports || []).length ? "info" : ""}">${(iso.ports || []).length ? plural(iso.ports.length, "port") : "None"}</span>`, "Isolated ports can't talk to each other — a hacked camera can't reach the others. Never isolate the recorder's port or an uplink.", isoBody)}
        ${card("Remote access", "", "How the switch can be reached and found.", accBody)}
      </div></div>`;

      // ---- network layout (read only)
      const vl = f.vlan || {}, lag = f.lag || {}, mir = f.mirror || {};
      const vports = Object.entries(vl.ports || {});
      const plain = vports.length && vports.every(([, v]) => v.type === "access" && v.pvid === 1);
      const layoutSec = na(vl) && na(lag) && na(mir) ? "" : `<div class="swv-adv-sec"><h3>Network layout <span class="swv-dim" style="text-transform:none;letter-spacing:0">(read only — change on the switch's own page)</span></h3><div class="swv-adv-grid">
        ${na(vl) ? "" : card("VLANs", plain ? `<span class="swv-b">VLAN 1 only</span>` : `<span class="swv-b info">${plural((vl.ids || []).length, "VLAN")}</span>`, plain ? "Every port is in VLAN 1 — one flat network." : "Which VLANs each port carries.", plain ? "" : `<div class="swv-chips">${vports.map(([k, v]) => `<span class="swv-b" title="${esc(v.type)}">${esc(pn(k))}: ${esc(v.type === "access" ? "VLAN " + v.pvid : v.type + " pvid " + v.pvid + " · " + (v.vlans || []).join(","))}</span>`).join("")}</div>`)}
        ${na(lag) ? "" : card("Link aggregation", `<span class="swv-b">${(lag.groups || []).length ? plural(lag.groups.length, "group") : "None"}</span>`, "Several cables bundled into one faster, redundant link.", (lag.groups || []).map((x) => `<div class="swv-note">Group ${esc(x.id)}: ${esc(portsTxt(x.ports))}</div>`).join(""))}
        ${na(mir) ? "" : card("Port mirroring", badge(mir.enabled), "Copies one port's traffic to another, for a packet capture.", mir.enabled ? `<div class="swv-note">${esc(portsTxt(mir.from))} → port ${esc(pn(mir.to))}</div>` : "")}
      </div></div>`;

      // ---- clock
      const tm = f.time || {};
      const drift = tm.drift_s;
      const timeSec = na(tm) ? "" : `<div class="swv-adv-sec"><h3>Clock</h3><div class="swv-adv-grid">${card("Time", tm.mode ? `<span class="swv-b ${tm.mode === "NTP" ? "ok" : "warn"}">${esc(tm.mode === "manually" ? "set by hand" : tm.mode)}</span>` : "",
        "With the right time, the switch's log lines up with the cameras and the recorder when you look into a fault.",
        `<div class="swv-note">Switch clock: <span class="swv-mono">${esc((tm.time || "").replace("T", " "))}</span>${drift != null ? ` · ${Math.abs(drift) < 60 ? "on time" : `<b style="color:#fbbf24">${drift > 0 ? "ahead" : "behind"} by ${esc(uptime(Math.abs(drift)))}</b>`}` : ""}${tm.mode === "NTP" && tm.ntp_server ? ` · NTP ${esc(tm.ntp_server)} every ${esc(tm.interval_min)} min` : ""}</div>
         ${manage && tm.can_ntp ? `<div class="swv-row"><input class="swv-in" data-ntp-server placeholder="pool.ntp.org" value="${esc(tm.ntp_server || "pool.ntp.org")}" style="width:170px"><button class="swv-btn sm" data-ntp-save>Use this NTP server</button></div><div class="swv-note">The switch needs a gateway and DNS for a name like pool.ntp.org; the router's IP works when it runs an NTP server.</div>` : ""}`)}</div></div>`;

      el.innerHTML = `<div class="swv-head"><div style="min-width:0"><h3 style="margin:0">Advanced settings</h3>
          <p class="swv-note" style="margin:4px 0 0">Read ${ago(a.ts)}${a.read_s != null ? ` in ${esc(a.read_s)} s` : ""}${a.model_known ? "" : " · the switch didn't list its features, so Netwatch tried each one"}</p></div>
          <div class="swv-row"><button class="swv-btn sm" data-adv="reload">↻ Read again</button><button class="swv-btn sm" data-adv="hide">Hide</button></div></div>
        ${manage ? "" : `<div class="swv-lock" style="margin-top:8px">🔒 Changing settings from here is turned off for this site.${o.settingsHref ? ` <a href="${esc(o.settingsHref)}" style="color:#a5b4fc">Turn on “Manage switches”</a>` : ""}</div>`}
        <div class="swv-adv-sec"><h3>Netwatch suggests</h3>${tipsHtml}</div>
        ${loopSec}${trafficSec}${poeSec}${secSec}${layoutSec}${timeSec}
        <p class="swv-note" style="margin:12px 0 0">Every change is logged under “What happened”. 🔒 ports carry the Pi or the router — Netwatch won't make changes there that could cut the site off.</p>`;
      bindAdv(el);
    }

    function bindAdv(el) {
      const q = (s) => el.querySelector(s);
      el.querySelectorAll("[data-adv=reload]").forEach((b) => (b.onclick = () => advLoad(true)));
      el.querySelectorAll("[data-adv=hide]").forEach((b) => (b.onclick = () => { st.advOpen = false; drawAdv(); }));
      el.querySelectorAll("[data-set]").forEach((b) => (b.onclick = async () => {
        const body = JSON.parse(b.dataset.set);
        if (b.dataset.ask && !(await o.confirm(b.dataset.ask, ""))) return;
        advSet(body, b);
      }));
      el.querySelectorAll("[data-chip]").forEach((b) => (b.onclick = () => {
        const k = b.dataset.chip;
        const s = st.advPick[k] || (st.advPick[k] = new Set());
        if (s.has(b.dataset.p)) s.delete(b.dataset.p); else s.add(b.dataset.p);
        drawAdv();
      }));
      el.querySelectorAll("[data-undo]").forEach((b) => (b.onclick = () => { delete st.advPick[b.dataset.undo]; drawAdv(); }));
      el.querySelectorAll("[data-pick]").forEach((b) => (b.onclick = () => {
        const k = b.dataset.pick, ids = JSON.parse(b.dataset.ids);
        const cur = new Set(advCur(k));
        st.advPick[k] = new Set(ids.filter((id) => !cur.has(id)));
        drawAdv();
      }));
      el.querySelectorAll("[data-save]").forEach((b) => (b.onclick = () => advSavePorts(b.dataset.save, b)));
      const prio = q("[data-prio]");
      if (prio) prio.onchange = async () => {
        const v = +prio.value;
        if (!(await o.confirm(`Set this switch's bridge priority to ${v}?`, "The switch with the LOWEST priority becomes the root (the centre of the tree). Ports may re-learn for a few seconds."))) { drawAdv(); return; }
        advSet({ feature: "rstp", enabled: true, priority: v, confirm: true }, prio);
      };
      const sg = q("[data-storm-global]");
      if (sg) sg.onclick = () => advSet({ feature: "storm", mode: q("[data-storm-mode]").value, ratio_pct: +q("[data-storm-ratio]").value }, sg);
      const rls = q("[data-rl-save]");
      if (rls) rls.onclick = () => {
        const rx = +q("[data-rl-rx]").value || null, tx = +q("[data-rl-tx]").value || null;
        if (!rx && !tx) { o.toast("Type a limit in Mbit/s (download, upload or both)", "bad"); return; }
        advSet({ feature: "rate_limit", ports: [q("[data-rl-port]").value], rx_mbps: rx, tx_mbps: tx }, rls);
      };
      const ctr = q("[data-ct-run]");
      if (ctr) ctr.onclick = () => runCable(q("[data-ct-port]").value, ctr, "cable");
      el.querySelectorAll("[data-cable]").forEach((b) => (b.onclick = () => runCable(b.dataset.cable, b, "cable:" + b.dataset.cable)));
      const dho = q("[data-dh-on]");
      if (dho) dho.onclick = () => {
        const cur = new Set(advCur("dhcp_snooping")), pend = st.advPick.dhcp_snooping || new Set();
        const trusted = [...new Set([...cur].filter((p) => !pend.has(p)).concat([...pend].filter((p) => !cur.has(p))))];
        if (!trusted.length) { o.toast("Pick the port the DHCP server (the router) is behind first", "bad"); return; }
        advSet({ feature: "dhcp_snooping", enabled: true, trusted }, dho, "DHCP snooping is on");
      };
      const ntp = q("[data-ntp-save]");
      if (ntp) ntp.onclick = () => advSet({ feature: "ntp", server: q("[data-ntp-server]").value.trim() }, ntp, "NTP server set");
    }

    function advCur(feature) {
      const f = (st.adv || {}).features || {};
      if (feature === "storm") return Object.entries((f.storm || {}).ports || {}).filter(([, v]) => v.enabled).map(([k]) => k);
      if (feature === "long_range") return (f.long_range || {}).ports_on || [];
      if (feature === "isolation") return (f.isolation || {}).ports || [];
      if (feature === "qos") return (f.qos || {}).high_ports || [];
      if (feature === "dhcp_snooping") {
        if ((f.dhcp_snooping || {}).enabled) return f.dhcp_snooping.trusted || [];
        const tip = ((st.adv || {}).advice || []).find((x) => x.feature === "dhcp_snooping");
        const prot = (st.adv || {}).protected || {};
        return (tip && tip.change && tip.change.trusted) || Object.keys(prot).filter((k) => /router/.test(prot[k]));
      }
      return [];
    }

    async function advSavePorts(feature, btn) {
      const cur = new Set(advCur(feature)), pend = st.advPick[feature] || new Set();
      const on = [...pend].filter((p) => !cur.has(p)), off = [...pend].filter((p) => cur.has(p));
      const flag = { storm: "enabled", long_range: "enabled", isolation: "isolated", qos: "high" }[feature];
      const bodies = [];
      if (feature === "dhcp_snooping") {
        bodies.push({ feature, enabled: true, trusted: [...cur].filter((p) => !pend.has(p)).concat(on) });
      } else {
        const extra = feature === "storm" && !((st.adv.features.storm || {}).global) ? { type: "broadcast", limit_mbps: +((root.querySelector("[data-storm-lim]") || {}).value || 10) } : {};
        if (on.length) bodies.push(Object.assign({ feature, ports: on, [flag]: true }, extra));
        if (off.length) bodies.push({ feature, ports: off, [flag]: false });
      }
      for (const b of bodies) {
        if (!(await advSet(b, btn))) return;
      }
    }

    async function runCable(port, btn, slot) {
      if (!port) return;
      if (!(await o.confirm(`Cable test on port ${String(port).split("/").pop()}?`, "The link drops for a few seconds while the switch measures the pairs.", { danger: true }))) return;
      const r = await act({ action: "cable-test", port }, btn, "Cable test finished");
      if (r) { st.advOut[slot] = cableHtml(r.result); drawAdv(); }
    }

    function drawEvents() {
      const el = root.querySelector("#swv-events");
      if (!el) return;
      const evs = (st.data.events || []);
      el.innerHTML = `<h3>What happened</h3>${evs.length ? `<ul class="swv-ev">${evs.map((e) => {
        const m = EV[(e.detail || {}).switch_event] || ["•", (e.detail || {}).switch_event || e.type];
        return `<li><span>${m[0]}</span><div style="min-width:0"><b>${esc(m[1])}</b><div class="swv-muted" style="font-size:12.5px">${evText(e)}</div></div><span class="swv-note" title="${esc(when(e.ts))}">${esc(ago(e.ts))}</span></li>`;
      }).join("")}</ul>` : `<p class="swv-muted" style="margin:0">Nothing yet — links going down or up, speed changes, PoE loss, devices moving port and every action taken here are listed as they happen.</p>`}`;
    }

    function drawInfo() {
      const el = root.querySelector("#swv-info");
      if (!el) return;
      const d = st.data, dev = d.device || {}, svc = d.services || {}, h = d.health || {};
      const on = (v) => v == null ? "—" : v ? "on" : "off";
      const bk = o.backups && (d.caps || {}).backup !== false ? `<h3 style="margin-top:14px">Config backups</h3><div id="swv-bk" class="swv-muted" style="font-size:13px">The switch's own configuration file, kept on the site Pi (last 10).</div>
        <div class="swv-row" style="margin-top:6px"><button class="swv-btn sm" data-i="backup">💾 Take a backup now</button><button class="swv-btn sm" data-i="bklist">Show kept backups</button></div>` : "";
      el.innerHTML = `<h3>Switch</h3><dl class="swv-kv">
          <dt>Model</dt><dd>${esc([d.model || dev.model, dev.product && dev.product !== dev.model ? "(" + dev.product + ")" : ""].filter(Boolean).join(" ") || "—")}</dd>
          <dt>Firmware</dt><dd class="swv-mono">${esc(dev.firmware || "—")}</dd>
          ${dev.serial ? `<dt>Serial</dt><dd class="swv-mono">${esc(dev.serial)}</dd>` : ""}
          <dt>MAC</dt><dd class="swv-mono">${esc(dev.mac || d.mac || "—")}</dd>
          <dt>Uptime</dt><dd>${esc(uptime(dev.uptime))}</dd>
          ${(h.temps || []).length ? `<dt>Temperatures</dt><dd>${h.temps.map((t) => esc((t.name ? t.name + " " : "") + Math.round(t.value) + "°C")).join(" · ")}</dd>` : ""}
          ${(h.psu || []).length ? `<dt>Power supply</dt><dd>${h.psu.map((p) => esc([p.type, p.voltage != null ? p.voltage + " V" : "", p.power != null ? p.power + " W" : ""].filter(Boolean).join(" "))).join(" · ")}</dd>` : ""}
          <dt>VLANs</dt><dd>${(d.vlans || []).length ? d.vlans.map((v) => esc(v.id + (v.name ? " " + v.name : ""))).join(", ") : "—"}</dd>
          ${Object.keys(svc).length ? `<dt>Services</dt><dd>SSH ${on(svc.ssh)} · Telnet ${svc.telnet ? "<b style='color:#fbbf24'>on</b>" : on(svc.telnet)} · HTTP ${on(svc.http)} · SNMP ${on(svc.snmp)}${"ntp" in svc ? " · NTP " + on(svc.ntp) : ""}${"unms" in svc ? " · UISP " + on(svc.unms) : ""}</dd>` : ""}
        </dl>
        ${d.manage ? `<div class="swv-row" style="margin-top:12px">${(d.caps || {}).locate === false ? "" : `<button class="swv-btn sm" data-i="locate" title="Blink the LEDs so someone on site finds this switch">💡 Blink LEDs</button>`}<button class="swv-btn sm danger" data-i="reboot">↻ Restart switch</button></div>` : ""}
        ${bk}`;
      const b = (n) => el.querySelector(`[data-i="${n}"]`);
      if (b("locate")) b("locate").onclick = (e) => act({ action: "locate", on: true }, e.currentTarget, "The switch LEDs are blinking");
      if (b("reboot")) b("reboot").onclick = async (e) => { if (await o.confirm(`Restart ${d.name}?`, "Every device on this switch loses the network and PoE power for about 2 minutes.", { danger: true })) act({ action: "reboot", confirm: true }, e.currentTarget, "The switch is restarting"); };
      if (o.backups && (d.caps || {}).backup !== false) {
        const list = async () => {
          const box = el.querySelector("#swv-bk");
          try {
            const j = await o.backups.list();
            const bs = j.backups || [];
            if (box) box.innerHTML = bs.length ? bs.slice(0, 5).map((x) => `<div class="swv-row" style="justify-content:space-between;font-size:13px"><span>${esc(when(x.ts))} <span class="swv-dim">${bytes(x.size)}</span></span><a class="swv-btn sm" href="${esc(o.backups.href(x.name))}">Download</a></div>`).join("") : "None kept yet.";
          } catch (err) { if (box) box.textContent = err.message || String(err); }
        };
        // Listing needs the site login, so it waits for a click instead of popping a login on page load.
        b("bklist").onclick = (e) => { e.currentTarget.hidden = true; list(); };
        b("backup").onclick = async (e) => {
          e.currentTarget.disabled = true;
          try { await o.backups.take(); o.toast("Backup saved on the site Pi", "ok"); list(); }
          catch (err) { o.toast((err.body && err.body.error) || err.message, "bad"); }
          finally { e.currentTarget.disabled = false; }
        };
      }
    }

    refresh();
    st.timer = setInterval(() => { if (!document.hidden) refresh(true); }, 60000);
    return {
      refresh: () => refresh(),
      destroy() { st.dead = true; clearInterval(st.timer); },
    };
  }

  /** Small summary for lists of switches: [{…view}] -> html. */
  function summaryCard(sw, href) {
    injectCss();
    const probs = sw.problems || [];
    const crit = probs.filter((p) => p.level === "crit").length, warn = probs.filter((p) => p.level === "warn").length;
    const st = !sw.online ? ["bad", "Offline"] : sw.kind === "no_login" ? ["warn", "Login needed"] : sw.kind === "auth_failed" ? ["bad", "Login rejected"] : !sw.ok && !(sw.ports || []).length ? ["warn", "Not read"] : crit ? ["bad", plural(crit, "fault")] : warn ? ["warn", `${warn} to check`] : ["ok", "Healthy"];
    const s = sw.summary || {}, poe = sw.poe || {};
    const leds = (sw.ports || []).map((p) => `<i class="swv-led ${portState(p, probs).led}" title="Port ${esc(portNo(p))}"></i>`).join("");
    return `<a class="swv swv-box" href="${esc(href)}" style="display:grid;gap:6px;text-decoration:none;color:inherit">
      <div class="swv-row" style="justify-content:space-between"><b>🔀 ${esc(sw.name)}</b><span class="swv-b ${st[0]}">${st[1]}</span></div>
      <div class="swv-muted" style="font-size:13px">${esc([sw.model, sw.ip].filter(Boolean).join(" · "))}</div>
      ${leds ? `<div class="swv-row" style="gap:5px">${leds}</div>` : ""}
      <div class="swv-note">${(sw.ports || []).length ? `${s.up}/${s.ports} ports up · PoE ${poe.used_w ?? "—"}${poe.budget_w ? "/" + poe.budget_w : ""} W · ↓ ${bps(s.rx_bps)}` : "no reading yet"}${sw.read_ts ? " · " + ago(sw.read_ts) : ""}</div></a>`;
  }

  // injectCss is exported so RouterView draws in the very same stylesheet — a
  // router and a switch are both managed units and must read identically.
  window.SwitchView = { mount, summaryCard, injectCss, fmt: { bps, bytes, uptime, ago } };
})();
