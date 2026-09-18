/* Netwatch network view — ONE renderer for the site's Network page (#/network)
 * and the hub's Control Center (site tab "Network"). The diagram (SVG: pan,
 * zoom, drag, towers/sites, links) and the map (Leaflet) both draw the SAME
 * /api/topology graph, so a change in one view is in the other. The hub image
 * copies this file to /static/cc/topoview.js at build time. Vanilla JS; Leaflet
 * is only needed for the map view.
 *
 *   const ctl = TopoView.mount(el, {
 *     load(opts)               -> Promise<graph>  GET  …/topology[?scope=all]
 *     call(method, path, body) -> Promise<json>   POST|DELETE …/topology<path> (throws Error(message))
 *     iconUrl(id)              -> url of an uploaded icon
 *     openDevice(id)                              the app's own device window
 *     placeDevicesHref                            where devices get a GPS pin (optional)
 *     mapTiles(map, L)                            base layers for the map (optional)
 *     login()                  -> Promise<bool>   unlock editing (optional)
 *     toast(msg, kind), confirm(title, text, {danger, ok}) -> Promise<bool>
 *     storageKey, view: "diagram"|"map", focus: node/group id, onRoute(view, focus)
 *   });
 *   ctl.refresh(); ctl.setView("map"); ctl.focus(id); ctl.destroy();
 */
(function () {
  "use strict";
  if (window.TopoView) return;

  const CSS = `
.tv{--tv-line:rgba(148,163,184,.16);--tv-line2:rgba(148,163,184,.3);--tv-panel:rgba(148,163,184,.06);--tv-ink:#e2e8f0;--tv-muted:#94a3b8;--tv-dim:#64748b;
  --tv-cyan:#22d3ee;--tv-ok:#34d399;--tv-warn:#fbbf24;--tv-bad:#fb7185;--tv-vio:#a78bfa;--tv-h:var(--tv-height,calc(100vh - 230px));
  color:var(--tv-ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;display:grid;gap:10px;min-width:0}
.tv *{box-sizing:border-box}
.tv button,.tv input,.tv select,.tv textarea{font:inherit;color:inherit}
.tv button{cursor:pointer}
.tv [hidden]{display:none!important}
.tv .tv-item .t svg,.tv .tv-sug .t svg,.tv h4 svg,.tv .tv-note svg,.tv-dlg .checks svg{display:inline-block;vertical-align:-3px}
.tv .tv-btn svg,.tv .tv-seg svg{display:inline-block}
.tv .tv-bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.tv .tv-acts2{display:flex;flex-wrap:wrap;gap:6px;margin-left:auto;justify-content:flex-end}
.tv .tv-bar select.tv-inp{width:auto;min-height:34px;padding:4px 8px;font-size:13px}
.tv .tv-seg{display:inline-flex;border:1px solid var(--tv-line2);border-radius:10px;overflow:hidden;flex:none}
.tv .tv-seg button{border:0;background:transparent;padding:6px 12px;font-size:13px;font-weight:650;color:var(--tv-muted);display:inline-flex;align-items:center;gap:6px}
.tv .tv-seg button+button{border-left:1px solid var(--tv-line)}
.tv .tv-seg button.on{background:rgba(34,211,238,.16);color:#cffafe}
.tv .tv-seg svg{width:16px;height:16px}
.tv .tv-btn{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--tv-line2);background:rgba(148,163,184,.08);border-radius:10px;padding:6px 11px;font-size:13px;font-weight:600;white-space:nowrap;min-height:34px}
.tv .tv-btn:hover:not(:disabled){background:rgba(148,163,184,.16);border-color:rgba(34,211,238,.5)}
.tv .tv-btn:disabled{opacity:.5;cursor:not-allowed}
.tv .tv-btn.pri{background:#0e7490;border-color:#0891b2;color:#ecfeff}
.tv .tv-btn.pri:hover:not(:disabled){background:#0891b2}
.tv .tv-btn.ok{background:rgba(16,185,129,.15);border-color:rgba(52,211,153,.45);color:#a7f3d0}
.tv .tv-btn.danger{border-color:rgba(251,113,133,.45);color:#fecdd3}
.tv .tv-btn.danger:hover:not(:disabled){background:rgba(244,63,94,.18)}
.tv .tv-btn.on{background:rgba(34,211,238,.16);border-color:rgba(34,211,238,.55);color:#cffafe}
.tv .tv-btn.sm{min-height:28px;padding:3px 9px;font-size:12px;border-radius:8px}
.tv .tv-btn svg{width:16px;height:16px;flex:none}
.tv .tv-btn .n{background:#fbbf24;color:#1c1917;border-radius:99px;padding:0 6px;font-size:11px;font-weight:800}
.tv .tv-btn .n.q{background:rgba(148,163,184,.25);color:#e2e8f0}
.tv .tv-search{position:relative;flex:0 1 240px;min-width:150px}
.tv .tv-search input{width:100%;height:34px;border-radius:10px;border:1px solid var(--tv-line2);background:rgba(2,6,23,.55);padding:0 10px 0 30px;font-size:13px}
.tv .tv-search svg{position:absolute;left:9px;top:9px;width:16px;height:16px;color:var(--tv-dim)}
.tv .tv-main{display:grid;grid-template-columns:minmax(0,1fr) 360px;gap:10px;align-items:start}
.tv.side-off .tv-main{grid-template-columns:minmax(0,1fr)}
/* Details under the diagram: floating over its lower edge, so the diagram keeps
   its full height and the panel never needs scrolling to reach. */
.tv.side-bottom .tv-main{grid-template-columns:minmax(0,1fr);position:relative}
.tv.side-bottom .tv-side{position:absolute;left:10px;right:10px;bottom:10px;top:auto;z-index:6;
  max-height:min(32vh,300px);min-height:0;background:rgba(10,17,32,.97);box-shadow:0 -12px 40px rgba(0,0,0,.55);
  display:flex;flex-wrap:wrap;align-content:flex-start}
.tv.side-bottom .tv-side>.hd{flex:1 1 100%;position:sticky;top:0}
.tv.side-bottom .tv-side>.sec{flex:1 1 300px;min-width:0;border-bottom:0;border-right:1px solid rgba(148,163,184,.08)}
.tv .tv-stage{position:relative;height:var(--tv-h);min-height:460px;border-radius:14px;border:1px solid var(--tv-line);overflow:hidden;background:radial-gradient(900px 500px at 20% 0%,rgba(34,211,238,.06),transparent 60%),#060b15;touch-action:none;user-select:none;-webkit-user-select:none}
.tv .tv-svg{width:100%;height:100%;display:block;outline:none;cursor:crosshair}
.tv.pan-mode .tv-svg{cursor:grab}
.tv .tv-svg.panning{cursor:grabbing}
.tv .tv-marq{fill:rgba(34,211,238,.12);stroke:#22d3ee;stroke-width:1.2;stroke-dasharray:5 4;pointer-events:none}
.tv .tv-g-grip{cursor:nwse-resize}
.tv .tv-g-grip rect{fill:rgba(148,163,184,.16)}
.tv .tv-g-grip:hover rect{fill:rgba(34,211,238,.28)}
.tv .tv-svg.connecting,.tv .tv-svg.connecting .tv-n{cursor:crosshair}
.tv .tv-map{position:absolute;inset:0;z-index:0;isolation:isolate;background:#0b1322}
.tv .tv-banner{position:absolute;z-index:900;left:50%;top:12px;transform:translateX(-50%);display:flex;gap:10px;align-items:center;padding:6px 8px 6px 14px;border-radius:99px;background:rgba(8,145,178,.96);color:#fff;font-weight:600;font-size:13px;box-shadow:0 10px 30px rgba(0,0,0,.5);max-width:calc(100% - 24px)}
.tv .tv-banner span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tv .tv-banner button{border:0;background:rgba(255,255,255,.2);border-radius:99px;padding:2px 10px;font-size:12px;color:#fff}
.tv .tv-zoom{position:absolute;left:10px;bottom:calc(10px + var(--tv-dock-h,0px));z-index:5;display:flex;gap:4px;align-items:center;background:rgba(6,11,21,.88);border:1px solid var(--tv-line2);border-radius:10px;padding:3px}
.tv .tv-zoom button{border:0;background:transparent;border-radius:7px;min-width:30px;height:28px;font-weight:700;color:#cbd5e1;padding:0 6px}
.tv .tv-zoom button:hover{background:rgba(148,163,184,.16)}
.tv .tv-zoom span{font-size:11.5px;color:var(--tv-muted);min-width:40px;text-align:center;font-variant-numeric:tabular-nums}
.tv .tv-legend{position:absolute;right:10px;bottom:calc(10px + var(--tv-dock-h,0px));z-index:5;max-width:min(330px,calc(100% - 20px));background:rgba(6,11,21,.94);border:1px solid var(--tv-line2);border-radius:12px;font-size:12px}
.tv .tv-legend>button{border:0;background:transparent;padding:6px 10px;font-weight:700;color:#cbd5e1;width:100%;text-align:left;display:flex;gap:6px;align-items:center}
.tv .tv-legend .body{padding:2px 12px 10px;display:grid;gap:10px;max-height:52vh;overflow:auto}
.tv .tv-legend h4{margin:0 0 4px;font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--tv-dim)}
.tv .tv-legend .row{display:flex;align-items:center;gap:8px;margin:3px 0;color:#cbd5e1}
.tv .tv-legend .grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:2px 10px}
.tv .tv-legend svg{flex:none}
.tv .tv-empty{position:absolute;inset:0;display:grid;place-items:center;text-align:center;color:var(--tv-muted);padding:20px;pointer-events:none}
.tv .tv-empty b{display:block;color:#e2e8f0;font-size:16px;margin-bottom:4px}
.tv .tv-side{border:1px solid var(--tv-line);border-radius:14px;background:linear-gradient(180deg,rgba(148,163,184,.07),rgba(148,163,184,.02));max-height:var(--tv-h);min-height:460px;overflow:auto;position:sticky;top:var(--tv-sticky-top,70px)}
.tv .tv-side .hd{display:flex;align-items:flex-start;gap:10px;padding:14px 14px 10px;border-bottom:1px solid var(--tv-line);position:sticky;top:0;background:#0a1120;z-index:2}
.tv .tv-side .hd h3{margin:0;font-size:16px;line-height:1.25;word-break:break-word}
.tv .tv-side .hd p{margin:3px 0 0;color:var(--tv-muted);font-size:12.5px}
.tv .tv-side .hd .x{margin-left:auto;border:0;background:transparent;color:var(--tv-muted);font-size:20px;line-height:1;padding:0 4px}
.tv .tv-side .sec{padding:12px 14px;border-bottom:1px solid rgba(148,163,184,.08)}
.tv .tv-side .sec:last-child{border-bottom:0}
.tv .tv-side h4{margin:0 0 8px;font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--tv-dim);display:flex;align-items:center;gap:6px}
.tv .tv-side h4 .c{color:var(--tv-muted);letter-spacing:0}
.tv .tv-kv{display:grid;grid-template-columns:auto minmax(0,1fr);gap:4px 12px;margin:0;font-size:13px}
.tv .tv-kv dt{color:var(--tv-dim)}
.tv .tv-kv dd{margin:0;word-break:break-word}
.tv .mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.92em}
.tv .muted{color:var(--tv-muted)} .tv .dim{color:var(--tv-dim)}
.tv .tv-note{font-size:12.5px;color:var(--tv-muted);margin:0}
.tv .tv-acts{display:flex;flex-wrap:wrap;gap:6px}
.tv .tv-b{display:inline-flex;align-items:center;gap:5px;border-radius:99px;padding:1px 9px;font-size:12px;font-weight:650;white-space:nowrap;background:rgba(148,163,184,.14);color:#cbd5e1}
.tv .tv-b.ok{background:rgba(16,185,129,.15);color:var(--tv-ok)} .tv .tv-b.bad{background:rgba(244,63,94,.16);color:var(--tv-bad)}
.tv .tv-b.warn{background:rgba(245,158,11,.15);color:var(--tv-warn)} .tv .tv-b.info{background:rgba(34,211,238,.13);color:#67e8f9}
.tv .tv-b.unm{background:transparent;border:1px dashed rgba(148,163,184,.5);color:#cbd5e1}
.tv .tv-list{display:grid;gap:2px;margin:0 -6px}
.tv .tv-item{display:grid;grid-template-columns:30px minmax(0,1fr) auto;gap:2px 8px;align-items:center;padding:6px;border-radius:9px;border:0;background:transparent;text-align:left;width:100%}
.tv .tv-item:hover{background:rgba(34,211,238,.06)}
.tv .tv-item .tv-ic{grid-row:span 2;width:30px;height:26px;flex:none}
.tv .tv-item .t{font-weight:650;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tv .tv-item .s{font-size:11.5px;color:var(--tv-dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;grid-column:2}
.tv .tv-item .r{grid-row:span 2;display:flex;gap:4px;align-items:center}
.tv .tv-sug{border:1px solid var(--tv-line);border-radius:10px;padding:8px 10px;margin:0 0 8px;display:grid;gap:6px;background:rgba(2,6,23,.35)}
.tv .tv-sug .t{font-weight:650;font-size:13px}
.tv .tv-sug p{margin:0;font-size:12px;color:var(--tv-muted)}
.tv .tv-field{display:grid;gap:4px;font-size:12.5px;color:var(--tv-muted)}
.tv .tv-inp{width:100%;min-height:34px;padding:6px 9px;border-radius:9px;border:1px solid var(--tv-line2);background:rgba(2,6,23,.6);color:#e2e8f0;font-size:13.5px}
.tv select.tv-inp{background:#0b1322}
.tv .tv-inp:focus{outline:none;border-color:rgba(34,211,238,.6);box-shadow:0 0 0 3px rgba(34,211,238,.15)}
.tv .tv-metrics{display:grid;grid-template-columns:repeat(auto-fill,minmax(98px,1fr));gap:6px}
.tv .tv-metric{border:1px solid var(--tv-line);border-radius:9px;padding:6px 8px;background:rgba(2,6,23,.35)}
.tv .tv-metric .l{font-size:10.5px;color:var(--tv-dim);text-transform:uppercase;letter-spacing:.06em}
.tv .tv-metric .v{font-size:15px;font-weight:700;font-variant-numeric:tabular-nums}
.tv .tv-metric .v.ok{color:var(--tv-ok)} .tv .tv-metric .v.warn{color:var(--tv-warn)} .tv .tv-metric .v.bad{color:var(--tv-bad)}
.tv .tv-mini{height:240px;border:1px solid var(--tv-line);border-radius:10px;background:#060b15;overflow:hidden}
.tv .tv-mini svg{width:100%;height:100%;display:block}
.tv .tv-grpmk{display:flex}
@media (max-width:1100px){.tv .tv-main{grid-template-columns:minmax(0,1fr)}
  .tv .tv-side{position:fixed;left:8px;right:8px;bottom:8px;top:auto;max-height:58vh;min-height:0;z-index:45;background:#0a1120;box-shadow:0 -10px 40px rgba(0,0,0,.6)}
  .tv.side-off .tv-side{display:none}}
@media (max-width:640px){.tv{--tv-h:calc(100vh - 190px)} .tv .tv-bar .lbl{display:none} .tv .tv-bar .lbl.keep{display:inline} .tv .tv-search{flex:1 1 100%}
  .tv .tv-legend{max-width:calc(100% - 20px)}}
/* the SVG */
.tv-svg text{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.tv-svg .tv-g-box{fill:rgba(148,163,184,.045);stroke:rgba(148,163,184,.28);stroke-width:1.2}
.tv-svg .tv-g.st-down .tv-g-box{stroke:rgba(251,113,133,.7)}
.tv-svg .tv-g.st-warn .tv-g-box{stroke:rgba(251,191,36,.6)}
.tv-svg .tv-g.is-sel .tv-g-box{stroke:#22d3ee;stroke-width:2}
.tv-svg .tv-g.drop .tv-g-box{stroke:#22d3ee;stroke-width:2.5;fill:rgba(34,211,238,.08)}
.tv-svg .tv-g-hd{fill:rgba(148,163,184,.09)}
.tv-svg .tv-g-name{fill:#f1f5f9;font-size:14px;font-weight:700}
.tv-svg .tv-g-sub{fill:#94a3b8;font-size:11.5px}
.tv-svg .tv-g-hit{fill:transparent;cursor:move}
.tv-svg .tv-ua .tv-g-box{fill:rgba(245,158,11,.035);stroke:rgba(251,191,36,.55);stroke-dasharray:7 5}
.tv-svg .tv-ua .tv-g-name{fill:#fde68a}
.tv-svg .tv-tog{cursor:pointer}
.tv-svg .tv-tog rect{fill:rgba(148,163,184,.12);stroke:rgba(148,163,184,.3)}
.tv-svg .tv-tog:hover rect,.tv-svg .tv-tog:focus-visible rect{fill:rgba(34,211,238,.2)}
.tv-svg .tv-tog:focus-visible rect{stroke:#22d3ee}
.tv-svg .tv-tog{outline:none}
.tv-svg .tv-tog path{stroke:#e2e8f0;stroke-width:1.8;fill:none;stroke-linecap:round}
.tv-svg .tv-n{cursor:pointer;outline:none}
.tv-svg .tv-n .plate{fill:transparent;stroke:transparent;stroke-width:1.5}
.tv-svg .tv-n:hover .plate,.tv-svg .tv-n:focus-visible .plate{fill:rgba(148,163,184,.07);stroke:rgba(148,163,184,.25)}
.tv-svg .tv-n.is-sel .plate{fill:rgba(34,211,238,.1);stroke:#22d3ee}
.tv-svg .tv-n.hit .plate{stroke:#fbbf24;stroke-width:2}
.tv-svg .tv-n.dim{opacity:.22}
.tv-svg .tv-n.off .ico{filter:grayscale(.75) brightness(.8)}
.tv-svg .tv-n.quiet .ico{filter:grayscale(1) brightness(.6);opacity:.7}
.tv-svg .tv-n.unm .ico{opacity:.92}
.tv-svg .tv-n-name,.tv-svg .tv-n-sub,.tv-svg .tv-n-tag,.tv-svg .tv-g-sub{paint-order:stroke;stroke:#070c17;stroke-width:3.2px;stroke-linejoin:round}
.tv-svg .tv-n-name{fill:#f1f5f9;font-size:12px;font-weight:650}
.tv-svg .tv-n-sub{fill:#94a3b8;font-size:10.5px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.tv-svg .tv-n-tag{font-size:10px;font-weight:650}
.tv-svg .tv-n.off .tv-n-tag{fill:#fb7185} .tv-svg .tv-n.quiet .tv-n-tag{fill:#94a3b8}
.tv-svg .tv-n.unm .tv-n-tag{fill:#cbd5e1} .tv-svg .tv-n.on .tv-n-tag{fill:#6ee7b7}
.tv-svg .tv-handle{opacity:0;cursor:crosshair}
.tv-svg.edit .tv-n:hover .tv-handle,.tv-svg.edit .tv-n.is-sel .tv-handle{opacity:1}
.tv-svg .tv-handle circle{fill:#0e7490;stroke:#67e8f9;stroke-width:1.5}
.tv-svg .tv-handle path{stroke:#fff;stroke-width:1.8}
.tv-svg .tv-e{fill:none;stroke-linecap:round}
.tv-svg .tv-e-hit{fill:none;stroke:transparent;stroke-width:14;cursor:pointer}
.tv-svg .tv-edge.dim{opacity:.12}
.tv-svg .tv-edge.is-sel .tv-e{filter:drop-shadow(0 0 4px rgba(34,211,238,.9))}
.tv-svg .tv-e.eth{stroke:#94a3b8;stroke-width:2.2}
.tv-svg .tv-e.fib{stroke:#a78bfa;stroke-width:5}
.tv-svg .tv-e.fib2{stroke:#0b1020;stroke-width:1.6}
.tv-svg .tv-e.wl{stroke:#22d3ee;stroke-width:2.2;stroke-dasharray:7 5}
.tv-svg .tv-e.down{stroke:#fb7185}
.tv-svg .tv-e.fib.down{stroke:#e11d48}
.tv-svg .tv-e.deg{stroke:#fbbf24}
.tv-svg .tv-e.sug{stroke-dasharray:2 6;stroke-width:2.4;opacity:.8}
.tv-svg .tv-e.fib.sug{stroke-width:3}
.tv-svg .tv-e.agg{stroke-width:3.6}
.tv-svg .tv-e.fib.agg{stroke-width:6}
.tv-svg .tv-pill rect{fill:#0b1324;stroke:rgba(148,163,184,.35)}
.tv-svg .tv-pill text{fill:#e2e8f0;font-size:10.5px;font-weight:600}
.tv-svg .tv-pill.sug rect{stroke:#fbbf24;stroke-dasharray:3 2}
.tv-svg .tv-pill.sug text{fill:#fde68a}
.tv-svg .tv-pill.down rect{stroke:#fb7185} .tv-svg .tv-pill.down text{fill:#fecdd3}
.tv-svg .tv-port{fill:#cbd5e1;font-size:9.5px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;paint-order:stroke;stroke:#060b15;stroke-width:3px}
.tv-svg .tv-ghost{stroke:#22d3ee;stroke-width:2;stroke-dasharray:6 4;fill:none}
.tv-svg .tv-core{fill:none;stroke:rgba(52,211,153,.5);stroke-dasharray:3 3}
@media (prefers-reduced-motion:no-preference){.tv-svg .tv-n.off .ring{animation:tvpulse 1.8s ease-in-out infinite}}
@keyframes tvpulse{0%,100%{opacity:.9}50%{opacity:.25}}
/* map */
.tv-mk{position:absolute;left:0;top:0;transform:translate(-50%,calc(-100% - 8px));display:inline-flex;align-items:center;gap:6px;padding:4px 10px 4px 5px;border-radius:99px;background:rgba(8,14,28,.94);border:2px solid #e2e8f0;color:#f8fafc;font:600 12.5px system-ui,sans-serif;white-space:nowrap;box-shadow:0 6px 18px rgba(0,0,0,.55);cursor:pointer}
.tv-mk::after{content:"";position:absolute;left:50%;bottom:-9px;transform:translateX(-50%);border:7px solid transparent;border-top-color:inherit;border-bottom:0}
.tv-mk.below{transform:translate(-50%,10px)}
.tv-mk.below::after{bottom:auto;top:-9px;border:7px solid transparent;border-bottom-color:inherit;border-top:0}
.tv-mk.approx{border-style:dashed}
.tv-mk.st-ok{border-color:#34d399} .tv-mk.st-warn{border-color:#fbbf24} .tv-mk.st-down{border-color:#fb7185} .tv-mk.st-unknown{border-color:#94a3b8}
.tv-mk.is-sel{box-shadow:0 0 0 3px #22d3ee,0 6px 18px rgba(0,0,0,.55)}
.tv-mk .gi{width:24px;height:24px;border-radius:50%;display:grid;place-items:center;background:#0f172a}
.tv-mk .gi svg{width:18px;height:18px}
.tv-mk .cnt{font-weight:700;font-size:11.5px;padding:0 6px;border-radius:99px;background:rgba(148,163,184,.18)}
.tv-mk .cnt.bad{background:rgba(244,63,94,.25);color:#fecdd3} .tv-mk .cnt.ok{background:rgba(16,185,129,.2);color:#a7f3d0}
.tv-mkwrap{width:0!important;height:0!important;background:none;border:0}
.tv-pin{width:22px;height:22px;border-radius:50%;display:grid;place-items:center;background:#0f172a;border:2px solid #34d399;box-shadow:0 3px 10px rgba(0,0,0,.6);transform:translate(-50%,-50%);position:absolute}
.tv-pin svg{width:18px;height:16px}
.tv-pin.off{border-color:#fb7185} .tv-pin.quiet{border-color:#64748b} .tv-pin.is-sel{box-shadow:0 0 0 3px #22d3ee}
.leaflet-tooltip.tv-tip{background:rgba(6,10,19,.92);color:#f1f5f9;border:1px solid rgba(148,163,184,.35);border-radius:6px;font-size:11.5px;font-weight:600;padding:2px 7px;box-shadow:none}
.leaflet-tooltip.tv-tip::before{display:none}
/* dialogs (an overlay, not <dialog>: the site's login prompt must stay on top) */
.tv-dlg{position:fixed;inset:0;z-index:80;background:rgba(2,6,23,.72);backdrop-filter:blur(3px);display:flex;align-items:center;justify-content:center;padding:14px}
.tv-dlg .card{width:min(560px,100%);max-height:calc(100vh - 28px);overflow:auto;background:#0b1324;border:1px solid rgba(148,163,184,.3);border-radius:16px;box-shadow:0 24px 70px rgba(0,0,0,.7);color:#e2e8f0;font:14px/1.45 system-ui,sans-serif}
.tv-dlg .card.wide{width:min(760px,100%)}
.tv-dlg .dh{display:flex;align-items:flex-start;gap:10px;padding:16px 18px 10px;border-bottom:1px solid rgba(148,163,184,.14)}
.tv-dlg .dh h2{margin:0;font-size:17px}
.tv-dlg .dh p{margin:3px 0 0;color:#94a3b8;font-size:12.5px}
.tv-dlg .db{padding:14px 18px;display:grid;gap:12px}
.tv-dlg .df{display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap;padding:12px 18px 16px;border-top:1px solid rgba(148,163,184,.14)}
.tv-dlg .two{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.tv-dlg .err{color:#fda4af;font-size:13px;min-height:18px}
.tv-dlg .hint{color:#94a3b8;font-size:12px}
.tv-dlg .checks{display:grid;gap:4px;max-height:220px;overflow:auto;border:1px solid rgba(148,163,184,.2);border-radius:9px;padding:6px 8px}
.tv-dlg .checks label{display:flex;gap:8px;align-items:center;font-size:13px}
.tv-dlg .icons{display:grid;grid-template-columns:repeat(auto-fill,minmax(92px,1fr));gap:8px}
.tv-dlg .iconbtn{border:1px solid rgba(148,163,184,.22);background:rgba(2,6,23,.45);border-radius:10px;padding:6px;display:grid;gap:3px;justify-items:center;font-size:11px;color:#cbd5e1;text-align:center}
.tv-dlg .iconbtn.on{border-color:#22d3ee;background:rgba(34,211,238,.1)}
.tv-dlg .iconbtn img,.tv-dlg .iconbtn svg{width:56px;height:48px;object-fit:contain}
@media (max-width:560px){.tv-dlg .two{grid-template-columns:1fr}}
`;

  function injectCss() {
    if (document.getElementById("tv-css")) return;
    const s = document.createElement("style");
    s.id = "tv-css";
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  // ---- helpers -----------------------------------------------------------------------------
  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ESC[c]);
  const now = () => Math.floor(Date.now() / 1000);
  const ago = (ts) => {
    if (!ts) return "never";
    const s = Math.max(0, now() - ts);
    if (s < 90) return "just now";
    if (s < 3600) return Math.round(s / 60) + " min ago";
    if (s < 86400) return Math.round(s / 3600) + " h ago";
    return Math.round(s / 86400) + " days ago";
  };
  const km = (m) => (m == null || m === "" ? "" : m >= 1000 ? (m / 1000).toFixed(1) + " km" : Math.round(m) + " m");
  const dbm = (v) => (v == null ? "—" : Math.round(v) + " dBm");
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const trunc = (s, n) => { s = String(s || ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; };
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
  };
  function parseLatLon(raw) {
    let t = String(raw || "").trim();
    if (!t) return null;
    const ok = (a, b) => (isFinite(a) && isFinite(b) && Math.abs(a) <= 90 && Math.abs(b) <= 180 && !(a === 0 && b === 0)) ? { lat: +(+a).toFixed(6), lon: +(+b).toFixed(6) } : null;
    let m = t.match(/!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)/) || t.match(/@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)/) || t.match(/[?&](?:q|query|ll|destination)=(-?\d+(?:\.\d+)?)(?:,|%2C)\s*(-?\d+(?:\.\d+)?)/i);
    if (m) return ok(+m[1], +m[2]);
    const dms = [...t.matchAll(/(\d+(?:\.\d+)?)\s*°\s*(?:(\d+(?:\.\d+)?)\s*['′]\s*)?(?:(\d+(?:\.\d+)?)\s*["″]\s*)?([NSEW])/gi)];
    if (dms.length === 2) {
      const val = (x) => { const v = +x[1] + (+x[2] || 0) / 60 + (+x[3] || 0) / 3600; return /[SW]/i.test(x[4]) ? -v : v; };
      const a = dms.find((x) => /[NS]/i.test(x[4])), b = dms.find((x) => /[EW]/i.test(x[4]));
      return a && b ? ok(val(a), val(b)) : null;
    }
    t = t.replace(/[−–]/g, "-");
    m = t.match(/^(-?\d+(?:[.,]\d+)?)\s*[,; ]\s*(-?\d+(?:[.,]\d+)?)$/) || t.match(/^(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)$/);
    if (m) return ok(+m[1].replace(",", "."), +m[2].replace(",", "."));
    return null;
  }

  // ---- a site's reply is data: numbers are numbers, states are known words --------------------
  const NUM = (v, d = 0) => (v === null || v === undefined || v === "" || !isFinite(+v) ? d : +v);
  const NUMN = (v) => (v === null || v === undefined || v === "" || !isFinite(+v) ? null : +v);
  const POS = (p) => (p && isFinite(+p.x) && isFinite(+p.y) ? { x: +p.x, y: +p.y } : null);
  const GEO = (p) => (p && isFinite(+p.lat) && isFinite(+p.lon) ? { ...p, lat: +p.lat, lon: +p.lon, ts: NUMN(p.ts) } : null);
  const STR = (v, d = "") => (v === null || v === undefined ? d : String(v));
  const pick = (v, allowed, d) => (allowed.includes(v) ? v : d);
  const SIZE = (v) => (v && typeof v === "object" && NUM(v.w) > 0 && NUM(v.h) > 0
    ? { w: Math.min(6000, NUM(v.w)), h: Math.min(4000, NUM(v.h)) } : null);
  function sanitize(g) {
    if (!g || typeof g !== "object") return g;
    const numObj = (o, keys) => { if (o) keys.forEach((k) => { if (k in o) o[k] = NUMN(o[k]); }); return o; };
    g.nodes = (Array.isArray(g.nodes) ? g.nodes : []).filter((n) => n && typeof n.id === "string");
    g.nodes.forEach((n) => {
      n.name = STR(n.name, n.id); n.kind = STR(n.kind, "other"); n.group = STR(n.group, "~");
      ["ip", "mac", "vendor", "model", "firmware", "hostname", "notes", "port_notes", "category", "icon"].forEach((k) => (n[k] = STR(n[k])));
      n.state = pick(n.state, ["online", "offline", "quiet", "unmonitored"], "quiet");
      n.pos = POS(n.pos); n.geo = GEO(n.geo); n.last_seen = NUM(n.last_seen); n.rtt = NUMN(n.rtt);
      n.ports = NUMN(n.ports); n.virtual = !!n.virtual; n.locked = !!n.locked;
      n.problems = (Array.isArray(n.problems) ? n.problems : []).map((x) => ({ detail: STR(x && x.detail), fix: STR(x && x.fix), type: STR(x && x.type) }));
      if (n.inferred) n.inferred = { state: pick(n.inferred.state, ["suspect", "passing"], "passing"), online: NUM(n.inferred.online), total: NUM(n.inferred.total) };
      if (n.radio) numObj(n.radio, ["ts", "signal", "noise", "airtime", "cap_dl", "cap_ul", "tx_rate", "rx_rate", "stations"]);
      if (n.switch_port) n.switch_port = { ...n.switch_port, uplink: !!n.switch_port.uplink };
      if (n.suggested_group) n.suggested_group = { group: STR(n.suggested_group.group), via: STR(n.suggested_group.via), confirmed: !!n.suggested_group.confirmed };
    });
    g.groups = (Array.isArray(g.groups) ? g.groups : []).filter((x) => x && typeof x.id === "string");
    g.groups.forEach((x) => {
      x.name = STR(x.name, x.id); x.kind = STR(x.kind, "site"); x.description = STR(x.description);
      x.status = pick(x.status, ["ok", "warn", "down", "unknown"], "unknown");
      x.pos = POS(x.pos); x.geo = GEO(x.geo); x.lat = NUMN(x.lat); x.lon = NUMN(x.lon);
      const c = x.counts || {};
      x.counts = {};
      ["total", "online", "offline", "quiet", "unmonitored", "problems"].forEach((k) => (x.counts[k] = NUM(c[k])));
      x.size = { w: NUM(x.size && x.size.w, 260), h: NUM(x.size && x.size.h, 124) };
      x.fixed_size = SIZE(x.fixed_size);
    });
    g.links = (Array.isArray(g.links) ? g.links : []).filter((L) => L && typeof L.id === "string" && typeof L.a === "string" && typeof L.b === "string");
    g.links.forEach((L) => {
      L.medium = pick(L.medium, ["ethernet", "fibre", "wireless"], "ethernet");
      L.status = pick(L.status, ["up", "down", "degraded", "unknown"], "unknown");
      L.confirmed = !!L.confirmed;
      ["a_port", "b_port", "label", "notes", "evidence", "source"].forEach((k) => (L[k] = STR(L[k])));
      if (L.metrics) numObj(L.metrics, ["ts", "signal", "remote_signal", "score_dl", "score_ul", "distance", "latency", "tx", "rx"]);
    });
    g.suggestions = (Array.isArray(g.suggestions) ? g.suggestions : []).filter((x) => x && typeof x.id === "string");
    g.suggestions.forEach((x) => {
      x.medium = pick(x.medium, ["ethernet", "fibre", "wireless"], "ethernet");
      x.members = Array.isArray(x.members) ? x.members.map(String) : [];
      x.unknown = NUM(x.unknown);
      ["a", "b", "a_port", "b_port", "evidence", "type"].forEach((k) => (x[k] = STR(x[k])));
    });
    const ua = g.unassigned || {};
    g.unassigned = { pos: POS(ua.pos) || { x: 0, y: 0 }, size: { w: NUM(ua.size && ua.size.w, 520), h: NUM(ua.size && ua.size.h, 124) },
      fixed_size: SIZE(ua.fixed_size), count: NUM(ua.count) };
    const gr = g.grid || {};
    g.grid = {};
    [["cell_w", 150], ["cell_h", 122], ["pad_x", 18], ["pad_top", 54], ["pad_bottom", 14], ["row_max", 6],
      ["empty_w", 260], ["empty_h", 124], ["collapsed_w", 200], ["collapsed_h", 124]].forEach(([k, d]) => (g.grid[k] = NUM(gr[k], d)));
    g.kinds = (Array.isArray(g.kinds) ? g.kinds : []).filter((k) => k && typeof k.id === "string").map((k) => ({ ...k, label: STR(k.label, k.id), tier: NUM(k.tier, 4), wireless: !!k.wireless }));
    const brief = (list) => (Array.isArray(list) ? list : []).filter((o) => o && typeof o.id === "string")
      .map((o) => ({ id: o.id, name: STR(o.name, o.id), ip: STR(o.ip), kind: STR(o.kind, "other"), state: STR(o.state) }));
    g.others = brief(g.others);
    g.hidden = brief(g.hidden);
    const gk = g.group_kinds && typeof g.group_kinds === "object" ? g.group_kinds : {};
    g.group_kinds = {};
    Object.keys(gk).forEach((k) => (g.group_kinds[k] = STR(gk[k], k)));
    const icons = g.icons && typeof g.icons === "object" ? g.icons : {};
    g.icons = {};
    Object.keys(icons).forEach((k) => { if (/^i-[0-9a-f]{8}$/.test(k)) g.icons[k] = { id: k, name: STR(icons[k] && icons[k].name), ts: NUM(icons[k] && icons[k].ts) }; });
    const ti = g.type_icons && typeof g.type_icons === "object" ? g.type_icons : {};
    g.type_icons = {};
    Object.keys(ti).forEach((k) => { if (typeof ti[k] === "string" && g.icons[ti[k]]) g.type_icons[k] = ti[k]; });
    g.view = { lock_all: !!(g.view && g.view.lock_all) };
    g.store_error = STR(g.store_error);
    g.can_edit = !!g.can_edit;
    g.dismissed = NUM(g.dismissed); g.orphan_links = NUM(g.orphan_links); g.ts = NUM(g.ts); g.rev = NUM(g.rev);
    if (g.site) { g.site = { name: STR(g.site.name), location: STR(g.site.location), lat: NUMN(g.site.lat), lon: NUMN(g.site.lon) }; }
    return g;
  }

  // ---- the equipment icons (isometric, 64×56) ------------------------------------------------
  const C30 = 0.866, S30 = 0.5;
  function shade(hex, amt) {
    const n = parseInt(hex.slice(1), 16);
    const f = (c) => Math.round(amt < 0 ? c * (1 + amt) : c + (255 - c) * amt);
    return "#" + [n >> 16, (n >> 8) & 255, n & 255].map((c) => f(c).toString(16).padStart(2, "0")).join("");
  }
  const pts = (list) => list.map((p) => p[0].toFixed(1) + "," + p[1].toFixed(1)).join(" ");
  const poly = (list, fill, extra = "") => `<polygon points="${pts(list)}" fill="${fill}" ${extra}/>`;
  /** An isometric box; (cx, cy) = screen point under the middle of its footprint. */
  function box(cx, cy, w, d, h, color, o = {}) {
    const ox = cx - (w / 2 - d / 2) * C30, oy = cy - (w / 2 + d / 2) * S30;
    const P = (x, y, z) => [ox + (x - y) * C30, oy + (x + y) * S30 - z];
    const sx = `stroke="${o.stroke || "rgba(2,6,23,.6)"}" stroke-width="${o.sw || 0.8}" stroke-linejoin="round"${o.dash ? ` stroke-dasharray="${o.dash}"` : ""}`;
    const svg = poly([P(0, d, 0), P(w, d, 0), P(w, d, h), P(0, d, h)], shade(color, -0.16), sx)
      + poly([P(w, 0, 0), P(w, d, 0), P(w, d, h), P(w, 0, h)], shade(color, -0.4), sx)
      + poly([P(0, 0, h), P(w, 0, h), P(w, d, h), P(0, d, h)], shade(color, 0.14), sx);
    const L = (x0, x1, z0, z1, fill, extra = "") => poly([P(x0, d, z0), P(x1, d, z0), P(x1, d, z1), P(x0, d, z1)], fill, extra);
    const R = (y0, y1, z0, z1, fill, extra = "") => poly([P(w, y0, z0), P(w, y1, z0), P(w, y1, z1), P(w, y0, z1)], fill, extra);
    const T = (x0, x1, y0, y1, fill, extra = "") => poly([P(x0, y0, h), P(x1, y0, h), P(x1, y1, h), P(x0, y1, h)], fill, extra);
    return { svg, P, L, R, T };
  }
  const shadow = (cx, cy, rx) => `<ellipse cx="${cx}" cy="${cy}" rx="${rx}" ry="${(rx * 0.32).toFixed(1)}" fill="rgba(0,0,0,.45)"/>`;
  const arcs = (x, y, dir = 1, color = "#22d3ee") => [5, 9, 13].map((r, i) =>
    `<path d="M${x},${y - r * 0.8} A${r},${r} 0 0 ${dir > 0 ? 1 : 0} ${x},${y + r * 0.8}" fill="none" stroke="${color}" stroke-width="1.6" stroke-linecap="round" opacity="${1 - i * 0.25}" transform="translate(${dir * 2},0)"/>`).join("");
  const bolt = (x, y, s = 1) => `<path d="M${x + 1 * s},${y - 6 * s} L${x - 3 * s},${y + 1 * s} L${x},${y + 1 * s} L${x - 1 * s},${y + 6 * s} L${x + 3 * s},${y - 1 * s} L${x},${y - 1 * s} Z" fill="#fbbf24"/>`;

  function camBullet(shellColor, lens) {
    const b = box(35, 38, 26, 11, 11, shellColor);
    const L = b.P(26, 5.5, 5.5);
    const hood = box(34, 30, 30, 13, 1.6, shade(shellColor, -0.1));
    return shadow(34, 47, 18) + `<path d="M22,46 L22,38 L28,34" stroke="#94a3b8" stroke-width="3" fill="none" stroke-linecap="round"/>`
      + poly([[16, 44], [24, 48], [24, 40], [16, 36]], "#64748b") + b.svg + hood.svg
      + `<ellipse cx="${L[0].toFixed(1)}" cy="${L[1].toFixed(1)}" rx="4.2" ry="4.8" fill="#0f172a" stroke="#475569"/>`
      + `<circle cx="${(L[0] - 1).toFixed(1)}" cy="${(L[1] - 1.4).toFixed(1)}" r="1.3" fill="${lens || "#67e8f9"}"/>`
      + `<circle cx="${b.P(3, 11, 8)[0].toFixed(1)}" cy="${b.P(3, 11, 8)[1].toFixed(1)}" r="1" fill="#f43f5e"/>`;
  }

  const ICONS = {
    camera() {
      const b = box(35, 38, 26, 11, 11, "#e2e8f0");
      const lens = b.P(26, 5.5, 5.5);
      const hood = box(34, 30, 30, 13, 1.6, "#cbd5e1");
      return shadow(34, 47, 18) + `<path d="M22,46 L22,38 L28,34" stroke="#94a3b8" stroke-width="3" fill="none" stroke-linecap="round"/>`
        + poly([[16, 44], [24, 48], [24, 40], [16, 36]], "#64748b") + b.svg + hood.svg
        + `<ellipse cx="${lens[0].toFixed(1)}" cy="${lens[1].toFixed(1)}" rx="4.2" ry="4.8" fill="#0f172a" stroke="#475569"/>`
        + `<circle cx="${(lens[0] - 1).toFixed(1)}" cy="${(lens[1] - 1.4).toFixed(1)}" r="1.3" fill="#67e8f9"/>`
        + `<circle cx="${b.P(3, 11, 8)[0].toFixed(1)}" cy="${b.P(3, 11, 8)[1].toFixed(1)}" r="1" fill="#f43f5e"/>`;
    },
    "camera-bullet"() { return camBullet("#e2e8f0"); },
    "camera-thermal"() {
      return camBullet("#cbd5e1", "#fb923c")
        + `<path d="M46,20 q3,-3 0,-6 M50,21 q4,-4 0,-8" fill="none" stroke="#fb923c" stroke-width="1.5" stroke-linecap="round" opacity=".9"/>`;
    },
    "camera-anpr"() {
      return camBullet("#cbd5e1", "#fde68a")
        + `<rect x="36" y="40" width="20" height="9" rx="1.6" fill="#f8fafc" stroke="#475569" stroke-width=".8"/>`
        + `<path d="M39,45.5 h3 M44,45.5 h3 M49,45.5 h4" stroke="#0f172a" stroke-width="1.6" stroke-linecap="round"/>`;
    },
    "camera-ptz"() {
      const plate = box(30, 15, 20, 12, 3, "#cbd5e1");
      return shadow(31, 49, 17) + plate.svg
        + `<path d="M30,17 L30,22" stroke="#94a3b8" stroke-width="3.4" stroke-linecap="round"/>`
        + `<path d="M17,30 a13,13 0 0 1 26,0 z" fill="#94a3b8" stroke="rgba(2,6,23,.6)" stroke-width=".8"/>`
        + `<circle cx="30" cy="31" r="13" fill="#f1f5f9" stroke="rgba(2,6,23,.55)" stroke-width=".8"/>`
        + `<path d="M17,31 a13,13 0 0 0 26,0 z" fill="#e2e8f0"/>`
        + `<circle cx="34" cy="33" r="6.4" fill="#0f172a" stroke="#475569" stroke-width=".9"/>`
        + `<circle cx="32.2" cy="31.2" r="1.9" fill="#67e8f9"/>`
        + `<path d="M46,26 a9,9 0 0 1 0,11" fill="none" stroke="#22d3ee" stroke-width="1.7" stroke-linecap="round"/>`
        + `<path d="M46,37 l-2.6,-1 l.6,2.9 z" fill="#22d3ee"/>`;
    },
    "camera-dome"() {
      const plate = box(31, 44, 30, 16, 3, "#cbd5e1");
      return shadow(31, 49, 19) + plate.svg
        + `<path d="M15,40 a16,16 0 0 1 32,0 z" fill="#f1f5f9" stroke="rgba(2,6,23,.55)" stroke-width=".9"/>`
        + `<path d="M20,40 a11,11 0 0 1 22,0 z" fill="#1e293b" opacity=".85"/>`
        + `<circle cx="33" cy="34" r="4.6" fill="#0f172a" stroke="#475569" stroke-width=".8"/>`
        + `<circle cx="31.4" cy="32.6" r="1.5" fill="#67e8f9"/>`;
    },
    "camera-turret"() {
      const base = box(31, 45, 24, 14, 5, "#cbd5e1");
      return shadow(31, 49, 17) + base.svg
        + `<path d="M19,38 a13,9 0 0 1 24,0 z" fill="#94a3b8"/>`
        + `<circle cx="31" cy="30" r="12" fill="#f1f5f9" stroke="rgba(2,6,23,.55)" stroke-width=".9"/>`
        + `<circle cx="33" cy="31" r="7.4" fill="#0f172a" stroke="#475569" stroke-width=".9"/>`
        + `<circle cx="31" cy="29" r="2.2" fill="#67e8f9"/>`
        + `<path d="M24,24 a10,10 0 0 1 14,0" fill="none" stroke="#e2e8f0" stroke-width="1.4" opacity=".7"/>`;
    },
    "camera-dual"() {
      const arm = `<path d="M18,47 L18,36 L24,32" stroke="#94a3b8" stroke-width="3" fill="none" stroke-linecap="round"/>`;
      const b = box(36, 36, 30, 12, 13, "#e2e8f0");
      return shadow(33, 48, 18) + arm + poly([[12, 45], [20, 49], [20, 41], [12, 37]], "#64748b") + b.svg
        + `<ellipse cx="44" cy="30" rx="4.4" ry="5" fill="#0f172a" stroke="#475569"/><circle cx="43" cy="28.6" r="1.3" fill="#67e8f9"/>`
        + `<ellipse cx="44" cy="38.5" rx="4.4" ry="5" fill="#0f172a" stroke="#475569"/><circle cx="43" cy="37.1" r="1.3" fill="#a78bfa"/>`;
    },
    "camera-pano"() {
      const arm = `<path d="M31,49 L31,40" stroke="#94a3b8" stroke-width="3.4" stroke-linecap="round"/>`;
      return shadow(31, 50, 18) + arm
        + `<path d="M12,34 a19,13 0 0 1 38,0 a19,9 0 0 1 -38,0 z" fill="#e2e8f0" stroke="rgba(2,6,23,.55)" stroke-width=".9"/>`
        + `<path d="M12,34 a19,13 0 0 1 38,0 z" fill="#f1f5f9"/>`
        + [16.5, 25.5, 34.5, 43.5].map((x, i) => `<circle cx="${x}" cy="${33 - [0, 3, 3, 0][i]}" r="3.4" fill="#0f172a" stroke="#475569" stroke-width=".8"/>`).join("")
        + `<circle cx="16.5" cy="32" r="1.1" fill="#67e8f9"/><circle cx="43.5" cy="32" r="1.1" fill="#67e8f9"/>`;
    },
    intercom() {
      const b = box(31, 44, 22, 8, 26, "#cbd5e1");
      return shadow(31, 48, 15) + b.svg
        + b.L(4, 18, 17, 23, "#0f172a")
        + b.L(6, 16, 9.5, 11.5, "#94a3b8") + b.L(6, 16, 7, 9, "#94a3b8") + b.L(6, 16, 4.5, 6.5, "#94a3b8")
        + `<circle cx="${b.P(11, 8, 14)[0].toFixed(1)}" cy="${b.P(11, 8, 14)[1].toFixed(1)}" r="2.4" fill="#22d3ee"/>`;
    },
    nvr() {
      const b = box(32, 44, 30, 26, 11, "#475569");
      let s = shadow(32, 47, 26) + b.svg;
      for (let i = 0; i < 3; i++) s += b.L(4 + i * 7.5, 9.5 + i * 7.5, 3, 8, "#1e293b");
      s += b.R(4, 7, 5, 7, "#22d3ee") + b.T(4, 26, 4, 5.2, "rgba(15,23,42,.45)") + b.T(4, 26, 8, 9.2, "rgba(15,23,42,.45)");
      return s;
    },
    switch() {
      const b = box(32, 40, 42, 16, 7, "#4f46e5");
      let s = shadow(32, 44, 28) + b.svg;
      for (let i = 0; i < 8; i++) {
        s += b.L(3 + i * 4.6, 6.3 + i * 4.6, 1.2, 4.6, "#0f172a");
        if (i % 3 !== 2) s += b.L(3.8 + i * 4.6, 5.3 + i * 4.6, 5.2, 6.3, "#34d399");
      }
      return s + b.T(3, 14, 3, 5, "#a5b4fc");
    },
    "unmanaged-switch"() {
      const b = box(32, 40, 42, 16, 7, "#64748b", { dash: "2 1.5", stroke: "#cbd5e1", sw: 0.9 });
      let s = shadow(32, 44, 28) + b.svg;
      for (let i = 0; i < 8; i++) s += b.L(3 + i * 4.6, 6.3 + i * 4.6, 1.2, 4.6, "#1e293b");
      return s;
    },
    router() {
      const cx = 32, cy = 42, rx = 18, ry = 9, h = 10;
      const c = "#6366f1";
      return shadow(32, 45, 22)
        + `<path d="M${cx - rx},${cy - h} L${cx - rx},${cy} A${rx},${ry} 0 0 0 ${cx + rx},${cy} L${cx + rx},${cy - h} Z" fill="${shade(c, -0.3)}" stroke="rgba(2,6,23,.6)" stroke-width=".8"/>`
        + `<ellipse cx="${cx}" cy="${cy - h}" rx="${rx}" ry="${ry}" fill="${shade(c, 0.12)}" stroke="rgba(2,6,23,.6)" stroke-width=".8"/>`
        + `<g stroke="#eef2ff" stroke-width="1.6" fill="none" stroke-linecap="round" stroke-linejoin="round" transform="translate(${cx},${cy - h}) scale(1,.5)">`
        + `<path d="M-3,-3 L-12,-12 M-12,-12 L-12,-7 M-12,-12 L-7,-12"/><path d="M3,3 L12,12 M12,12 L12,7 M12,12 L7,12"/>`
        + `<path d="M3,-3 L12,-12 M3,-3 L3,-8 M3,-3 L8,-3"/><path d="M-3,3 L-12,12 M-3,3 L-3,8 M-3,3 L-8,3"/></g>`
        + `<circle cx="${cx - 9}" cy="${cy + 3}" r="1.1" fill="#34d399"/><circle cx="${cx - 5}" cy="${cy + 4.5}" r="1.1" fill="#34d399"/>`;
    },
    "wifi-router"() {
      return ICONS.router().replace(/^/, "")
        + `<path d="M22,30 L19,12 M42,30 L45,12" stroke="#cbd5e1" stroke-width="2.4" stroke-linecap="round"/>`
        + arcs(47, 14, 1) ;
    },
    radio() {
      const pole = `<path d="M26,50 L26,8" stroke="#94a3b8" stroke-width="3" stroke-linecap="round"/><path d="M26,24 L31,26" stroke="#64748b" stroke-width="2"/>`;
      const b = box(35, 40, 6, 4, 30, "#e2e8f0");
      return shadow(28, 50, 12) + pole + b.svg + b.L(1, 5, 23, 27, "#94a3b8") + arcs(44, 24, 1);
    },
    ptp() {
      return shadow(28, 50, 12)
        + `<path d="M26,50 L26,12" stroke="#94a3b8" stroke-width="3" stroke-linecap="round"/>`
        + `<path d="M26,26 L33,26" stroke="#64748b" stroke-width="2.4"/>`
        + `<ellipse cx="38" cy="25" rx="7.5" ry="15" fill="#cbd5e1" stroke="rgba(2,6,23,.6)" stroke-width=".8"/>`
        + `<ellipse cx="39.5" cy="25" rx="5" ry="11" fill="#f1f5f9"/>`
        + `<path d="M40,25 L49,25" stroke="#94a3b8" stroke-width="1.6"/><rect x="47" y="22.5" width="4" height="5" rx="1" fill="#e2e8f0" stroke="#64748b" stroke-width=".6"/>`
        + arcs(55, 25, 1);
    },
    internet() {
      return `<ellipse cx="32" cy="47" rx="20" ry="5" fill="rgba(0,0,0,.45)"/>`
        + `<path d="M16,40 a8,8 0 0 1 1-15 a11,11 0 0 1 20-6 a9,9 0 0 1 15,8 a7,7 0 0 1-1,13 Z" fill="#0ea5e9" stroke="#bae6fd" stroke-width="1.2"/>`
        + `<circle cx="33" cy="30" r="7" fill="none" stroke="#e0f2fe" stroke-width="1.3"/><path d="M26,30 H40 M33,23 C29,27 29,33 33,37 C37,33 37,27 33,23" fill="none" stroke="#e0f2fe" stroke-width="1.1"/>`;
    },
    server() {
      const b = box(32, 47, 14, 24, 30, "#334155");
      let s = shadow(32, 49, 18) + b.svg;
      for (let i = 0; i < 4; i++) s += b.L(2, 12, 22 - i * 5, 23.4 - i * 5, "#0f172a");
      return s + b.L(2, 4, 26, 28, "#34d399");
    },
    nas() {
      const b = box(32, 46, 16, 22, 22, "#1f2937");
      return shadow(32, 48, 18) + b.svg + b.L(2, 14, 12, 19, "#374151", `stroke="#0b1020" stroke-width=".6"`)
        + b.L(2, 14, 3, 10, "#374151", `stroke="#0b1020" stroke-width=".6"`) + b.L(11, 13, 16.5, 17.5, "#38bdf8") + b.L(11, 13, 7.5, 8.5, "#38bdf8");
    },
    pc() {
      const st = box(32, 48, 10, 8, 3, "#475569");
      const neck = `<path d="M32,45 L32,38" stroke="#475569" stroke-width="3"/>`;
      const m = box(32, 40, 30, 3, 21, "#1e293b");
      return shadow(32, 49, 14) + st.svg + neck + m.svg + m.L(2, 28, 2.5, 19, "#0e7490") + m.L(4, 16, 11, 17, "rgba(165,243,252,.35)");
    },
    printer() {
      const b = box(32, 46, 28, 22, 12, "#94a3b8");
      const paper = box(33, 36, 16, 3, 9, "#f8fafc", { sw: 0.5 });
      return shadow(32, 48, 22) + b.svg + b.L(5, 23, 4, 7, "#1e293b") + paper.svg + b.R(3, 6, 8, 10, "#34d399");
    },
    phone() {
      const b = box(33, 46, 22, 18, 7, "#334155");
      let s = shadow(32, 48, 18) + b.svg;
      for (let i = 0; i < 3; i++) for (let j = 0; j < 3; j++) {
        const p = b.P(11 + i * 3.4, 4 + j * 3.4, 7);
        s += `<circle cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r=".9" fill="#cbd5e1"/>`;
      }
      return s + `<path d="M16,33 C15,27 20,24 24,27 L31,23 C35,20 40,23 38,28" fill="none" stroke="#0f172a" stroke-width="4.5" stroke-linecap="round"/>`;
    },
    alarm() {
      const b = box(32, 46, 22, 4, 28, "#e2e8f0");
      let s = shadow(32, 48, 16) + b.svg + b.L(3, 19, 16, 25, "#0f172a");
      for (let i = 0; i < 3; i++) for (let j = 0; j < 2; j++) s += b.L(5 + i * 5, 8 + i * 5, 5 + j * 4.5, 7.6 + j * 4.5, "#94a3b8");
      return s + `<circle cx="44" cy="17" r="6" fill="#f43f5e" stroke="#fecdd3" stroke-width="1"/><path d="M41.5,18.5 h5 M44,13.5 v1" stroke="#fff" stroke-width="1.4" stroke-linecap="round"/>`;
    },
    solar() {
      return shadow(32, 49, 20)
        + `<path d="M22,48 L26,34 M42,48 L40,32" stroke="#94a3b8" stroke-width="2.2"/>`
        + `<polygon points="8,34 34,20 58,30 32,44" fill="#1e3a8a" stroke="#cbd5e1" stroke-width="1.4"/>`
        + `<path d="M16.7,29.5 L40.7,40.7 M25.3,24.8 L49.3,36.1 M16,38.4 L42,24.4 M24,41.9 L50,27.8" stroke="#93c5fd" stroke-width=".7" opacity=".8"/>`
        + `<circle cx="52" cy="10" r="4" fill="#fbbf24"/>`;
    },
    ups() {
      const b = box(32, 47, 16, 24, 24, "#374151");
      const p = b.P(8, 24, 13);
      return shadow(32, 49, 18) + b.svg + bolt(p[0], p[1], 1.2) + b.L(3, 13, 20, 21.5, "#0f172a");
    },
    poe() {
      const b = box(32, 42, 18, 12, 7, "#1f2937");
      const p = b.P(9, 6, 7);
      return shadow(32, 45, 16) + `<path d="M40,42 C48,46 52,40 58,44" stroke="#94a3b8" stroke-width="2" fill="none"/>` + b.svg + bolt(p[0], p[1] - 0.5, 0.9);
    },
    "media-converter"() {
      const b = box(30, 42, 18, 14, 7, "#0f766e");
      return shadow(30, 45, 16) + `<path d="M38,40 C50,44 56,34 48,30 C42,27 42,36 52,38" stroke="#fb923c" stroke-width="2.4" fill="none" stroke-linecap="round"/>` + b.svg + b.L(3, 7, 2, 5, "#0f172a") + b.L(10, 15, 2, 5, "#fb923c");
    },
    "patch-panel"() {
      const b = box(32, 40, 46, 6, 9, "#475569");
      let s = shadow(32, 43, 28) + b.svg;
      for (let i = 0; i < 10; i++) s += b.L(3 + i * 4.1, 5.8 + i * 4.1, 2, 5, "#0f172a");
      return s;
    },
    media() {
      const st = box(32, 48, 14, 6, 2, "#374151");
      const m = box(32, 42, 36, 3, 22, "#111827");
      return shadow(32, 49, 18) + st.svg + `<path d="M32,46 L32,42" stroke="#374151" stroke-width="3"/>` + m.svg + m.L(2, 34, 2, 20, "#312e81") + m.L(5, 15, 12, 18, "rgba(196,181,253,.35)");
    },
    iot() {
      return shadow(32, 49, 10)
        + `<path d="M26,40 h12 v5 a2,2 0 0 1 -2,2 h-8 a2,2 0 0 1 -2,-2 Z" fill="#94a3b8"/>`
        + `<path d="M24,36 C16,30 18,12 32,12 C46,12 48,30 40,36 Z" fill="#fde68a" stroke="#f59e0b" stroke-width="1.2"/>`
        + `<path d="M29,36 L30,26 L34,26 L35,36" stroke="#b45309" stroke-width="1.2" fill="none"/>`;
    },
    other() {
      const b = box(32, 46, 24, 20, 14, "#64748b");
      return shadow(32, 48, 20) + b.svg + b.T(6, 18, 6, 14, "rgba(226,232,240,.35)");
    },
    unknown() {
      const b = box(32, 46, 22, 22, 20, "#475569");
      const p = b.P(11, 22, 10);
      return shadow(32, 48, 20) + b.svg + `<text x="${(p[0] - 3.5).toFixed(1)}" y="${(p[1] + 5).toFixed(1)}" font-size="14" font-weight="800" fill="#fde68a" font-family="system-ui,sans-serif">?</text>`;
    },
  };

  // Group kinds: line icons (24×24) for headers, map markers and collapsed towers.

  // Pictures an operator can pick by hand (BUILTIN below). Same 64×56 frame.
  const EXTRA_ICONS = {
    "ubnt-dish"() {
      return shadow(28, 50, 12)
        + `<path d="M26,50 L26,12" stroke="#94a3b8" stroke-width="3" stroke-linecap="round"/>`
        + `<path d="M26,26 L33,26" stroke="#64748b" stroke-width="2.4"/>`
        + `<circle cx="42" cy="26" r="15" fill="#e2e8f0" stroke="rgba(2,6,23,.6)" stroke-width=".9"/>`
        + `<circle cx="42" cy="26" r="11" fill="#f8fafc"/>`
        + `<circle cx="42" cy="26" r="11" fill="none" stroke="#0ea5e9" stroke-width="1.4" opacity=".8"/>`
        + `<circle cx="42" cy="26" r="3.6" fill="#cbd5e1" stroke="#64748b" stroke-width=".8"/>`;
    },
    "ubnt-sector"() {
      const b = box(38, 30, 10, 5, 34, "#f1f5f9");
      return shadow(28, 50, 12) + `<path d="M26,50 L26,10" stroke="#94a3b8" stroke-width="3" stroke-linecap="round"/>`
        + `<path d="M26,26 L33,26" stroke="#64748b" stroke-width="2.4"/>` + b.svg
        + b.L(1.5, 8.5, 6, 28, "#cbd5e1") + arcs(50, 26, 1);
    },
    "ubnt-ap"() {
      const plate = box(31, 20, 12, 8, 3, "#cbd5e1");
      return shadow(31, 46, 20) + plate.svg
        + `<path d="M31,23 L31,29" stroke="#94a3b8" stroke-width="2.6" stroke-linecap="round"/>`
        + `<ellipse cx="31" cy="36" rx="20" ry="9" fill="#f8fafc" stroke="rgba(2,6,23,.5)" stroke-width=".9"/>`
        + `<ellipse cx="31" cy="34.5" rx="20" ry="9" fill="#ffffff"/>`
        + `<ellipse cx="31" cy="34.5" rx="11" ry="5" fill="none" stroke="#0ea5e9" stroke-width="1.6"/>`;
    },
    "ubnt-nano"() {
      const b = box(36, 32, 9, 6, 22, "#f1f5f9");
      return shadow(28, 50, 12) + `<path d="M26,50 L26,14" stroke="#94a3b8" stroke-width="3" stroke-linecap="round"/>`
        + `<path d="M26,28 L32,28" stroke="#64748b" stroke-width="2.2"/>` + b.svg
        + b.L(1.5, 7.5, 4, 18, "#cbd5e1") + arcs(48, 28, 1);
    },
    "mikrotik-router"() {
      const b = box(31, 42, 34, 20, 7, "#1e40af");
      let ant = "";
      [-13, 0, 13].forEach((dx) => { ant += `<path d="M${31 + dx},${28 - Math.abs(dx) * 0.18} L${31 + dx},${14 - Math.abs(dx) * 0.2}" stroke="#94a3b8" stroke-width="2.6" stroke-linecap="round"/>`; });
      return shadow(31, 46, 24) + ant + b.svg + b.L(4, 30, 2.5, 4.5, "#60a5fa") + b.T(5, 29, 5, 15, "rgba(255,255,255,.07)");
    },
    "mikrotik-switch"() {
      const b = box(32, 41, 42, 16, 9, "#1d4ed8");
      let s2 = shadow(32, 45, 28) + b.svg;
      for (let i = 0; i < 8; i++) s2 += b.L(3 + i * 4.6, 6.3 + i * 4.6, 1.5, 5, "#0f172a") + b.L(3.8 + i * 4.6, 5.5 + i * 4.6, 5.6, 6.6, "#93c5fd");
      return s2 + b.T(3, 14, 3, 5, "#bfdbfe");
    },
    "cudy-router"() {
      const b = box(31, 42, 30, 18, 6, "#f8fafc");
      let ant = "";
      [-14, -5, 5, 14].forEach((dx) => { ant += `<path d="M${31 + dx},${29} L${31 + dx + dx * 0.25},${15}" stroke="#cbd5e1" stroke-width="2.4" stroke-linecap="round"/>`; });
      return shadow(31, 46, 22) + ant + b.svg + b.L(5, 25, 2, 3.6, "#22d3ee") + b.T(6, 24, 4, 13, "rgba(148,163,184,.12)");
    },
    "cudy-outdoor"() {
      const b = box(35, 34, 14, 9, 24, "#f8fafc");
      return shadow(28, 50, 12) + `<path d="M26,50 L26,16" stroke="#94a3b8" stroke-width="3" stroke-linecap="round"/>`
        + `<path d="M26,30 L31,30" stroke="#64748b" stroke-width="2.2"/>` + b.svg
        + b.L(2.5, 11.5, 15, 21, "#e2e8f0") + b.L(4, 10, 4, 6, "#22d3ee") + arcs(50, 30, 1);
    },
  };
  // What the icon picker offers: every equipment type, then these extras.
  const EXTRA_META = {
    "ubnt-dish": ["Dish radio (PowerBeam)", "Ubiquiti"], "ubnt-sector": ["Sector antenna", "Ubiquiti"],
    "ubnt-ap": ["Ceiling AP (UniFi)", "Ubiquiti"], "ubnt-nano": ["NanoStation / LiteAP", "Ubiquiti"],
    "mikrotik-router": ["Router (hAP)", "MikroTik"], "mikrotik-switch": ["Switch (CRS)", "MikroTik"],
    "cudy-router": ["Indoor router", "Cudy"], "cudy-outdoor": ["Outdoor AP", "Cudy"],
  };

  const GICON = {
    tower: `<path d="M12 3 7 21M12 3l5 18M8.3 15h7.4M9.5 10h5M7 21h10" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/><path d="M5 5a9 9 0 0 1 0 6M19 5a9 9 0 0 0 0 6" fill="none" stroke="#22d3ee" stroke-width="1.5" stroke-linecap="round"/>`,
    site: `<path d="M3 11l9-7 9 7M5 10v10h14V10M10 20v-5h4v5" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>`,
    building: `<path d="M5 21V4h10v17M15 9h4v12M3 21h18M8 7h1M11 7h1M8 11h1M11 11h1M8 15h1M11 15h1" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>`,
    pole: `<path d="M12 21V6M9 21h6M8 7h8" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M16.5 3.5a5 5 0 0 1 0 6M7.5 3.5a5 5 0 0 0 0 6" fill="none" stroke="#22d3ee" stroke-width="1.5" stroke-linecap="round"/>`,
    cabinet: `<path d="M6 3h12v18H6zM9 7h6M9 11h6M9 15h6" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/>`,
    area: `<path d="M12 21s-6-5.4-6-10a6 6 0 0 1 12 0c0 4.6-6 10-6 10z" fill="none" stroke="currentColor" stroke-width="1.7"/><circle cx="12" cy="11" r="2.2" fill="currentColor"/>`,
  };
  const gicon = (kind, size = 20, color = "#e2e8f0") => `<svg viewBox="0 0 24 24" width="${size}" height="${size}" style="color:${color}" aria-hidden="true">${GICON[kind] || GICON.site}</svg>`;

  // One hidden <svg> of <symbol>s shared by every view on the page.
  function ensureSymbols() {
    if (document.getElementById("tv-symbols")) return;
    const defs = Object.entries({ ...ICONS, ...EXTRA_ICONS }).map(([k, fn]) => `<symbol id="tvk-${k}" viewBox="0 0 64 56">${fn()}</symbol>`).join("")
      + Object.entries(GICON).map(([k, s]) => `<symbol id="tvg-${k}" viewBox="0 0 24 24">${s}</symbol>`).join("");
    const div = document.createElement("div");
    div.innerHTML = `<svg id="tv-symbols" width="0" height="0" style="position:absolute;width:0;height:0" aria-hidden="true"><defs>${defs}</defs></svg>`;
    document.body.appendChild(div.firstChild);
  }
  const ALL_ICONS = { ...ICONS, ...EXTRA_ICONS };
  const kindSvg = (kind, w = 30, h = 26) => `<svg class="tv-ic" viewBox="0 0 64 56" width="${w}" height="${h}" aria-hidden="true"><use href="#tvk-${ALL_ICONS[kind] ? kind : "other"}"/></svg>`;
  /** A picture reference: "b:<name>" = one Netwatch draws, anything else = an uploaded file. */
  const builtinOf = (ref) => (typeof ref === "string" && ref.slice(0, 2) === "b:" && ALL_ICONS[ref.slice(2)] ? ref.slice(2) : null);

  // Status glyphs drawn on a node (colour is never the only signal).
  const STATE = {
    online: { label: "Online", cls: "on", b: "ok", fill: "#059669", glyph: `<path d="M-3.2,0.2 L-1,2.4 L3.4,-2.2" stroke="#fff" stroke-width="1.8" fill="none" stroke-linecap="round" stroke-linejoin="round"/>` },
    offline: { label: "Offline", cls: "off", b: "bad", fill: "#e11d48", glyph: `<path d="M-2.6,-2.6 L2.6,2.6 M2.6,-2.6 L-2.6,2.6" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>` },
    quiet: { label: "Not seen for 7+ days", cls: "quiet", b: "", fill: "#475569", glyph: `<path d="M-3,0 H3" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>` },
    unmonitored: { label: "Not monitored", cls: "unm", b: "unm", fill: "#1e293b", glyph: `<circle r="3" fill="none" stroke="#cbd5e1" stroke-width="1.3"/><path d="M-2.1,2.1 L2.1,-2.1" stroke="#cbd5e1" stroke-width="1.3"/>`, dash: true },
  };
  const MEDIUM = {
    ethernet: { label: "Ethernet", cls: "eth", icon: `<path d="M4 10h16v8H4zM8 18v3M12 18v3M16 18v3M8 6h8v4H8z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/>` },
    fibre: { label: "Fibre", cls: "fib", icon: `<path d="M3 12c4-6 7 6 11 0s5-3 7-1" fill="none" stroke="#a78bfa" stroke-width="2.4" stroke-linecap="round"/><circle cx="3.5" cy="12" r="1.6" fill="#fb923c"/>` },
    wireless: { label: "Wireless", cls: "wl", icon: `<path d="M2 9a15 15 0 0 1 20 0M5 12.5a10 10 0 0 1 14 0M8.5 16a5 5 0 0 1 7 0M12 20h.01" fill="none" stroke="#22d3ee" stroke-width="1.8" stroke-linecap="round"/>` },
  };
  const micon = (m, size = 16) => `<svg viewBox="0 0 24 24" width="${size}" height="${size}" style="color:#cbd5e1;flex:none" aria-hidden="true">${(MEDIUM[m] || MEDIUM.ethernet).icon}</svg>`;
  const I = {
    diagram: `<path d="M4 4h6v5H4zM14 4h6v5h-6zM9 15h6v5H9zM7 9v3h10V9M12 12v3" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>`,
    map: `<path d="M9 20l-6-3V4l6 3 6-3 6 3v13l-6-3-6 3zM9 7v13M15 4v13" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>`,
    search: `<path d="M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14zm9 3-4.3-4.3" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>`,
    plus: `<path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>`,
    link: `<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>`,
    wand: `<path d="M4 20 15 9M14 4v3M18 8h3M17 5l2-2M9 5l1 1M19 13l1 1" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>`,
    lock: `<path d="M6 11h12v10H6zM9 11V8a3 3 0 0 1 6 0v3" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>`,
    unlock: `<path d="M6 11h12v10H6zM9 11V8a3 3 0 0 1 5.8-1" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>`,
    review: `<path d="M9 11l3 3 8-8M20 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>`,
    filter: `<path d="M4 5h16l-6 8v6l-4-2v-4z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>`,
    fit: `<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>`,
    hand: `<path d="M8 13V6.5a1.5 1.5 0 0 1 3 0V12V4.5a1.5 1.5 0 0 1 3 0V12V6.5a1.5 1.5 0 0 1 3 0V15a6 6 0 0 1-6 6h-1a5 5 0 0 1-4.3-2.5L4 15a1.6 1.6 0 0 1 2.6-1.8z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/>`,
    dock: `<path d="M4 5h16v14H4z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M4 15h16" stroke="currentColor" stroke-width="1.8"/>`,
    image: `<path d="M4 5h16v14H4zM4 16l5-5 4 4 3-3 4 4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><circle cx="15.5" cy="9" r="1.5" fill="currentColor"/>`,
    refresh: `<path d="M4 4v5h5M20 20v-5h-5M5 9a7.5 7.5 0 0 1 13.5-2.5M19 15a7.5 7.5 0 0 1-13.5 2.5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>`,
    info: `<path d="M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 11v5M12 8h.01" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>`,
    tag: `<path d="M3 12V4h8l10 10-8 8L3 12zM7.5 7.5h.01" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>`,
  };
  const ico = (n, size = 16) => `<svg viewBox="0 0 24 24" width="${size}" height="${size}" aria-hidden="true">${I[n] || ""}</svg>`;

  // =============================================================================================
  function mount(root, opts) {
    injectCss();
    ensureSymbols();
    const PK = opts.storageKey || "nw.topo";
    const pref = store.get(PK, {});
    const S = {
      g: null, idx: null, boxes: {}, edges: [],
      view: opts.view || pref.view || "diagram",
      sel: null, multi: new Set(),
      k: pref.k || 1, tx: pref.tx != null ? pref.tx : 40, ty: pref.ty != null ? pref.ty : 40, fitted: pref.k != null,
      q: "", status: pref.status || "all", kinds: pref.kinds || "all",
      media: pref.media || { ethernet: true, fibre: true, wireless: true },
      sugg: pref.sugg !== false, labels: pref.labels !== false, review: pref.review !== false,
      legend: !!pref.legend, scope: pref.scope === "all" ? "all" : "infra",
      panel: null, connect: null, drag: null, over: { nodes: {}, groups: {}, ua: null, size: {} },
      dropTarget: null, placing: null, destroyed: false, loading: false, err: "", timer: null,
      dock: pref.dock === "right" ? "right" : "bottom", panMode: !!pref.panMode, space: false, marquee: null,
      focusWanted: opts.focus || null,
    };
    const savePref = () => store.set(PK, { view: S.view, k: S.k, tx: S.tx, ty: S.ty, status: S.status, kinds: S.kinds,
      media: S.media, sugg: S.sugg, labels: S.labels, review: S.review, legend: S.legend, scope: S.scope,
      dock: S.dock, panMode: S.panMode });
    const toast = (m, kind) => (opts.toast ? opts.toast(m, kind) : console.log(m));
    const confirmBox = (t, x, o) => (opts.confirm ? opts.confirm(t, x, o || {}) : Promise.resolve(window.confirm(t + "\n\n" + x)));
    const kindOf = (id) => (S.g && S.g.kinds.find((k) => k.id === id)) || { id, label: id, tier: 4, wireless: false };
    const canEdit = () => !!(S.g && S.g.can_edit) || !!opts.assumeEdit;
    const locked = () => !!(S.g && S.g.view && S.g.view.lock_all);

    root.innerHTML = `<div class="tv">
      <div class="tv-bar">
        <div class="tv-seg" role="group" aria-label="View"><button type="button" data-view="diagram">${ico("diagram")}<span class="lbl">Diagram</span></button><button type="button" data-view="map">${ico("map")}<span class="lbl">Map</span></button></div>
        <label class="tv-search">${ico("search")}<input type="search" data-q placeholder="Find equipment, IP or MAC…" aria-label="Find equipment" autocomplete="off"></label>
        <select class="tv-inp" data-f="status" aria-label="Which equipment to highlight">
          <option value="all">All equipment</option><option value="bad">Problems &amp; offline</option><option value="online">Online</option><option value="unm">Not monitored</option></select>
        <select class="tv-inp" data-f="kinds" aria-label="Equipment type">
          <option value="all">All types</option><option value="net">Network &amp; radios</option><option value="cctv">Cameras &amp; recorders</option><option value="power">Power &amp; other</option></select>
        <details class="tv-pop" style="position:relative"><summary class="tv-btn" style="list-style:none">${ico("filter")}<span class="lbl">Show</span></summary>
          <div data-pop style="position:absolute;z-index:30;top:40px;left:0;min-width:250px;background:#0b1324;border:1px solid var(--tv-line2);border-radius:12px;padding:10px 12px;display:grid;gap:6px;box-shadow:0 18px 40px rgba(0,0,0,.6)">
            <b style="font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--tv-dim)">Connections</b>
            <label><input type="checkbox" data-m="ethernet"> Ethernet</label>
            <label><input type="checkbox" data-m="fibre"> Fibre</label>
            <label><input type="checkbox" data-m="wireless"> Wireless</label>
            <label><input type="checkbox" data-o="sugg"> Suggested (not confirmed yet)</label>
            <label><input type="checkbox" data-o="labels"> Port numbers and labels</label>
            <b style="font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--tv-dim);margin-top:4px">Equipment</b>
            <label><input type="checkbox" data-o="review"> "Not in a group yet" area</label>
            <label><input type="checkbox" data-o="scope"> Every device on the network (phones, PCs…)</label>
          </div></details>
        <div class="tv-acts2">
        <button type="button" class="tv-btn" data-a="add-group" title="Add a tower, site or building">${ico("plus")}<span class="lbl keep">Group</span></button>
        <button type="button" class="tv-btn" data-a="add-equip" title="Add equipment Netwatch cannot find, e.g. an unmanaged switch">${ico("plus")}<span class="lbl keep">Equipment</span></button>
        <button type="button" class="tv-btn" data-a="add-link" title="Connect two pieces of equipment">${ico("link")}<span class="lbl">Connect</span></button>
        <button type="button" class="tv-btn" data-a="arrange" title="Tidy everything that is not locked">${ico("wand")}<span class="lbl">Auto-arrange</span></button>
        <button type="button" class="tv-btn" data-a="lock" title="Lock the layout so nothing can be dragged by accident">${ico("unlock")}<span class="lbl">Lock layout</span></button>
        <button type="button" class="tv-btn" data-a="review" title="Suggested connections and equipment not in a group yet">${ico("review")}<span class="lbl">Review</span><span class="n" data-rn hidden></span></button>
        <button type="button" class="tv-btn" data-a="icons" title="Equipment pictures">${ico("image")}<span class="lbl">Icons</span></button>
        <button type="button" class="tv-btn" data-a="dock" title="Show the details panel under the diagram or beside it">${ico("dock")}<span class="lbl">Panel</span></button>
        </div>
      </div>
      <div class="tv-main">
        <div class="tv-stage">
          <svg class="tv-svg" tabindex="0" role="application" aria-label="Network diagram — drag to select, hold the space bar to move, scroll to zoom">
            <defs>
              <pattern id="tv-dots" width="30" height="30" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r="1" fill="rgba(148,163,184,.13)"/></pattern>
              <pattern id="tv-iso" width="34.64" height="20" patternUnits="userSpaceOnUse"><path d="M0,10 L17.32,0 L34.64,10 L17.32,20 Z" fill="none" stroke="rgba(148,163,184,.055)" stroke-width="1"/></pattern>
            </defs>
            <g data-world><rect x="-60000" y="-60000" width="120000" height="120000" fill="url(#tv-dots)" data-bg/><g data-lg></g><g data-le></g><g data-ln></g><g data-lo></g></g>
          </svg>
          <div class="tv-map" hidden></div>
          <div class="tv-banner" hidden><span data-bt></span><button type="button" data-bc>Cancel</button></div>
          <div class="tv-zoom" data-zoombar><button type="button" data-z="-" aria-label="Zoom out">−</button><span data-zl>100%</span><button type="button" data-z="+" aria-label="Zoom in">+</button><button type="button" data-z="fit" title="Fit to screen" aria-label="Fit to screen">${ico("fit")}</button>
            <button type="button" data-z="hand" title="Drag to move the diagram instead of selecting (or hold the space bar)" aria-label="Move the diagram" aria-pressed="false">${ico("hand")}</button></div>
          <div class="tv-legend" data-legend></div>
          <div class="tv-empty" data-empty hidden></div>
        </div>
        <aside class="tv-side" data-side aria-label="Details" aria-live="polite"></aside>
      </div>
    </div>`;
    const $ = (sel, el = root) => el.querySelector(sel);
    const $$ = (sel, el = root) => [...el.querySelectorAll(sel)];
    const tv = $(".tv"), svg = $(".tv-svg"), world = $("[data-world]");
    const LG = $("[data-lg]"), LE = $("[data-le]"), LN = $("[data-ln]"), LO = $("[data-lo]");
    const side = $("[data-side]"), stage = $(".tv-stage"), mapEl = $(".tv-map");

    // ---- the graph ----------------------------------------------------------------------------
    function setGraph(g) {
      if (S.destroyed || !g || !g.nodes) return;
      S.g = sanitize(g);
      const idx = { nodes: {}, groups: {}, links: {}, byGroup: {}, adj: {} };
      g.nodes.forEach((n) => { idx.nodes[n.id] = n; (idx.byGroup[n.group] = idx.byGroup[n.group] || []).push(n); });
      g.groups.forEach((x) => (idx.groups[x.id] = x));
      g.links.forEach((L) => {
        idx.links[L.id] = L;
        (idx.adj[L.a] = idx.adj[L.a] || []).push(L);
        (idx.adj[L.b] = idx.adj[L.b] || []).push(L);
      });
      S.idx = idx;
      if (!g.can_edit) applyLocalCollapse();
      if (S.sel && !selExists(S.sel)) S.sel = null;
      S.multi.forEach((id) => { if (!idx.nodes[id]) S.multi.delete(id); });
      render();
      if (S.focusWanted) { const f = S.focusWanted; S.focusWanted = null; focus(f); }
    }
    function selExists(sel) {
      if (sel.t === "node") return !!S.idx.nodes[sel.id];
      if (sel.t === "link") return !!S.idx.links[sel.id];
      if (sel.t === "group") return !!S.idx.groups[sel.id];
      return sel.t === "review" || sel.t === "ua";
    }
    async function refresh(quiet) {
      if (S.loading || S.destroyed) return;
      S.loading = true;
      try {
        const g = await opts.load({ scope: S.scope });
        if (S.destroyed) return;
        S.err = "";
        if (!S.drag) setGraph(g);
        else S.pending = g;
      } catch (e) {
        if (S.destroyed) return;
        S.err = e.message || "Could not load the network";
        if (!quiet) toast(S.err, "bad");
        if (!S.g) render();
      } finally {
        S.loading = false;
      }
    }
    const graphQuery = (path) => path + (path.includes("?") ? "&" : "?") + "graph=1" + (S.scope === "all" ? "&scope=all" : "");
    async function act(method, path, body, { graph = true, okMsg } = {}) {
      try {
        const res = await opts.call(method, graph ? graphQuery(path) : path, body);
        if (S.destroyed) return res || {};
        if (res && res.graph) setGraph(res.graph);
        if (okMsg) toast(okMsg, "ok");
        return res || {};
      } catch (e) {
        if (!S.destroyed) toast(e.message || "That did not work", "bad");
        throw e;
      }
    }

    // ---- geometry -------------------------------------------------------------------------------
    const G = () => S.g.grid;
    const CW = () => G().cell_w, CH = () => G().cell_h;
    function fitSize(members) {
      const g = G();
      let w = g.empty_w, h = g.empty_h;
      members.forEach((n) => {
        const p = n.pos || { x: g.pad_x, y: g.pad_top };
        w = Math.max(w, p.x + g.cell_w + g.pad_x);
        h = Math.max(h, p.y + g.cell_h + g.pad_bottom);
      });
      return { w: Math.round(w), h: Math.round(h) };
    }
    /** A box never shrinks below what is inside it, however small it was dragged. */
    const grown = (min, fixed) => (fixed ? { w: Math.max(min.w, fixed.w || 0), h: Math.max(min.h, fixed.h || 0) } : min);
    function computeBoxes() {
      const out = {};
      S.g.groups.forEach((gr) => {
        const p = S.over.groups[gr.id] || gr.pos || { x: 0, y: 0 };
        const min = fitSize(S.idx.byGroup[gr.id] || []);
        const sz = gr.collapsed ? { w: G().collapsed_w, h: G().collapsed_h } : grown(min, S.over.size[gr.id] || gr.fixed_size);
        out[gr.id] = { id: gr.id, x: p.x, y: p.y, w: sz.w, h: sz.h, min, collapsed: !!gr.collapsed, g: gr };
      });
      const ua = S.idx.byGroup["~"] || [];
      if (ua.length && S.review) {
        const p = S.over.ua || (S.g.unassigned && S.g.unassigned.pos) || { x: 0, y: 0 };
        const min = { w: Math.max(fitSize(ua).w, 520), h: fitSize(ua).h };
        const sz = grown(min, S.over.size["~"] || (S.g.unassigned && S.g.unassigned.fixed_size));
        out["~"] = { id: "~", x: p.x, y: p.y, w: sz.w, h: sz.h, min, ua: true };
      }
      S.boxes = out;
    }
    function absPos(n) {
      if (S.over.nodes[n.id]) return S.over.nodes[n.id];
      const b = S.boxes[n.group];
      if (!b || b.collapsed) return null;
      const p = n.pos || { x: G().pad_x, y: G().pad_top };
      return { x: b.x + p.x, y: b.y + p.y };
    }
    /** Where an end of a link is drawn: a node, or its collapsed group. */
    function endOf(id) {
      const n = S.idx.nodes[id];
      if (!n) return null;
      const b = S.boxes[n.group];
      if (!b) return null;
      if (b.collapsed && !S.over.nodes[id]) return { key: "g:" + b.id, x: b.x, y: b.y, w: b.w, h: b.h, box: true, grp: "g:" + b.id };
      const a = absPos(n);
      return a ? { key: id, x: a.x, y: a.y, w: CW(), h: CH(), node: true, grp: n.group } : null;
    }
    function anchors(e) {
      if (e.box) return { cx: e.x + e.w / 2, cy: e.y + e.h / 2, top: [e.x + e.w / 2, e.y], bottom: [e.x + e.w / 2, e.y + e.h], left: [e.x, e.y + e.h / 2], right: [e.x + e.w, e.y + e.h / 2] };
      const cx = e.x + CW() / 2, cy = e.y + 34;
      return { cx, cy, top: [cx, e.y + 5], bottom: [cx, e.y + CH() - 6], left: [e.x + 16, cy], right: [e.x + CW() - 16, cy] };
    }
    function curve(A, B, bend) {
      const a = anchors(A), b = anchors(B);
      const dx = b.cx - a.cx, dy = b.cy - a.cy;
      const sameGroup = A.grp && A.grp === B.grp;
      let p, q, c1, c2;
      if (sameGroup ? Math.abs(dy) > 20 : Math.abs(dy) > Math.abs(dx) * 0.7) {
        [p, q] = dy > 0 ? [a.bottom, b.top] : [a.top, b.bottom];
        const c = Math.max(28, Math.abs(q[1] - p[1]) * 0.45) * Math.sign(q[1] - p[1] || 1);
        c1 = [p[0] + bend, p[1] + c]; c2 = [q[0] + bend, q[1] - c];
      } else {
        [p, q] = dx > 0 ? [a.right, b.left] : [a.left, b.right];
        const c = Math.max(28, Math.abs(q[0] - p[0]) * 0.45) * Math.sign(q[0] - p[0] || 1);
        c1 = [p[0] + c, p[1] + bend]; c2 = [q[0] - c, q[1] + bend];
      }
      const at = (t) => {
        const u = 1 - t;
        return [u * u * u * p[0] + 3 * u * u * t * c1[0] + 3 * u * t * t * c2[0] + t * t * t * q[0],
          u * u * u * p[1] + 3 * u * u * t * c1[1] + 3 * u * t * t * c2[1] + t * t * t * q[1]];
      };
      const f = (v) => v.toFixed(1);
      return { d: `M${f(p[0])},${f(p[1])} C${f(c1[0])},${f(c1[1])} ${f(c2[0])},${f(c2[1])} ${f(q[0])},${f(q[1])}`, at, p, q,
        len: Math.hypot(q[0] - p[0], q[1] - p[1]) };
    }
    function computeEdges() {
      const out = [], agg = {};
      for (const L of S.g.links) {
        if (!S.media[L.medium]) continue;
        if (!L.confirmed && !S.sugg) continue;
        const A = endOf(L.a), B = endOf(L.b);
        if (!A || !B || A.key === B.key) continue;
        if (A.box || B.box) {
          const key = [A.key, B.key].sort().join("|");
          const x = (agg[key] = agg[key] || { id: "agg:" + key, A, B, links: [] });
          x.links.push(L);
        } else out.push({ id: L.id, A, B, links: [L] });
      }
      Object.values(agg).forEach((x) => out.push(x));
      const par = {};
      out.forEach((e) => {
        const k = [e.A.key, e.B.key].sort().join("|");
        e.pi = par[k] = par[k] == null ? 0 : par[k] + 1;
      });
      S.edges = out;
    }

    // ---- filters --------------------------------------------------------------------------------
    const KGROUP = { net: ["internet", "ptp", "radio", "router", "wifi-router", "switch", "unmanaged-switch", "media-converter", "patch-panel"],
      cctv: ["camera", "camera-ptz", "camera-dome", "camera-turret", "camera-bullet", "camera-dual", "camera-pano",
        "camera-thermal", "camera-anpr", "intercom", "nvr"], power: ["poe", "ups", "solar", "alarm", "server", "nas", "pc", "printer", "phone", "media", "iot", "other", "unknown"] };
    function ipMatch(q) {
      if (!/^[\d.*]+$/.test(q) || !/\d/.test(q)) return null;
      if (/^\d{1,3}(\.\d{1,3}){3}$/.test(q)) return (ip) => ip === q;
      if (q.startsWith(".")) return (ip) => (ip || "").endsWith(q);
      if (q.endsWith("*")) return (ip) => (ip || "").startsWith(q.slice(0, -1));
      return (ip) => (ip || "").includes(q);
    }
    function matchesQuery(n) {
      const q = S.q.trim().toLowerCase();
      if (!q) return true;
      const im = ipMatch(q);
      if (im) return im(n.ip);
      return [n.name, n.ip, n.mac, n.model, n.vendor, n.hostname, kindOf(n.kind).label, n.notes].join(" ").toLowerCase().includes(q);
    }
    function passesFilter(n) {
      if (S.kinds !== "all" && !(KGROUP[S.kinds] || []).includes(n.kind)) return false;
      if (S.status === "bad") return n.state === "offline" || n.state === "quiet" || (n.problems || []).length > 0 || (n.inferred && n.inferred.state === "suspect");
      if (S.status === "online") return n.state === "online";
      if (S.status === "unm") return n.state === "unmonitored";
      return true;
    }

    // ---- drawing --------------------------------------------------------------------------------
    /** Where the details panel sits, and what dragging the canvas does. */
    function applyModes() {
      tv.classList.toggle("side-bottom", S.dock === "bottom");
      // how much of the canvas the docked panel covers, so the zoom bar and legend clear it
      const h = S.dock === "bottom" && side.offsetParent && side.offsetHeight ? side.offsetHeight + 12 : 0;
      tv.style.setProperty("--tv-dock-h", h + "px");
      tv.classList.toggle("pan-mode", S.panMode || S.space);
      const hb = $('[data-z="hand"]');
      if (hb) { hb.setAttribute("aria-pressed", String(S.panMode)); hb.style.color = S.panMode ? "#67e8f9" : ""; }
    }
    function render() {
      if (S.destroyed) return;
      applyModes();
      if (!S.g) {
        const e = $("[data-empty]");
        e.hidden = false;
        e.innerHTML = S.err ? `<div><b>Could not load the network</b>${esc(S.err)}</div>` : `<div><b>Loading the network…</b></div>`;
        return;
      }
      tv.classList.toggle("side-off", !S.sel && S.panel !== "review" && window.innerWidth <= 1100);
      syncBar();
      if (S.view === "map") {
        svg.style.display = "none";
        $("[data-zoombar]").hidden = true;
        $("[data-empty]").hidden = true;
        mapEl.hidden = false;
        drawMap();
      } else {
        svg.style.display = "";
        $("[data-zoombar]").hidden = false;
        mapEl.hidden = true;
        drawDiagram();
      }
      drawLegend();
      drawSide();
    }
    function drawDiagram() {
      computeBoxes();
      computeEdges();
      const e = $("[data-empty]");
      const nothing = !S.g.nodes.length && !S.g.groups.length;
      e.hidden = !nothing;
      if (nothing) e.innerHTML = `<div><b>Nothing to draw yet</b>Add a tower or site with <b>+ Group</b>, or wait for the next scan to find equipment.</div>`;
      svg.classList.toggle("edit", canEdit() && !locked());
      if (!S.fitted && (S.g.nodes.length || S.g.groups.length)) { fitView(false); S.fitted = true; }
      else if (!S.checkedView && Object.keys(S.boxes).length) {
        S.checkedView = true;
        const r = svg.getBoundingClientRect();
        const a = toWorld(r.left, r.top), z = toWorld(r.right, r.bottom);
        const seen = Object.values(S.boxes).some((b) => b.x < z.x && b.x + b.w > a.x && b.y < z.y && b.y + b.h > a.y);
        if (!seen && r.width) fitView(false);
      }
      applyTransform();
      LG.innerHTML = Object.values(S.boxes).map(groupSvg).join("");
      LE.innerHTML = S.edges.map(edgeSvg).join("");
      const hits = S.q.trim() ? new Set(S.g.nodes.filter(matchesQuery).map((n) => n.id)) : null;
      const related = relatedSet();
      LN.innerHTML = S.g.nodes.map((n) => {
        const a = absPos(n);
        if (!a || S.over.nodes[n.id]) return "";
        return nodeSvg(n, a, hits, related);
      }).join("");
      LO.innerHTML = Object.keys(S.over.nodes).map((id) => nodeSvg(S.idx.nodes[id], S.over.nodes[id], null, null, true)).join("") + (S.ghost || "")
        + (S.marquee ? `<rect class="tv-marq" x="${S.marquee.x.toFixed(1)}" y="${S.marquee.y.toFixed(1)}" width="${S.marquee.w.toFixed(1)}" height="${S.marquee.h.toFixed(1)}" rx="4"/>` : "");
    }
    function applyTransform() {
      world.setAttribute("transform", `translate(${S.tx.toFixed(1)},${S.ty.toFixed(1)}) scale(${S.k.toFixed(4)})`);
      $("[data-zl]").textContent = Math.round(S.k * 100) + "%";
      svg.classList.toggle("lod", S.k < 0.55);
    }
    /** Nodes and links to keep bright while something is selected. */
    function relatedSet() {
      if (!S.sel) return null;
      const set = new Set();
      if (S.sel.t === "node") {
        set.add(S.sel.id);
        (S.idx.adj[S.sel.id] || []).forEach((L) => { set.add(L.id); set.add(L.a); set.add(L.b); });
      } else if (S.sel.t === "link") {
        const L = S.idx.links[S.sel.id];
        if (L) { set.add(L.id); set.add(L.a); set.add(L.b); }
      } else return null;
      return set;
    }
    function groupSvg(b) {
      const f = (v) => Math.round(v);
      if (b.ua) {
        const n = (S.idx.byGroup["~"] || []).length;
        return `<g class="tv-g tv-ua ${S.dropHighlight === "~" ? "drop" : ""} ${S.sel && S.sel.t === "ua" ? "is-sel" : ""}" data-box="~">
          <rect class="tv-g-box" x="${f(b.x)}" y="${f(b.y)}" width="${f(b.w)}" height="${f(b.h)}" rx="16"/>
          <rect class="tv-g-hit" data-drag-box="~" x="${f(b.x)}" y="${f(b.y)}" width="${f(b.w)}" height="44" rx="16"/>
          <text class="tv-g-name" x="${f(b.x + 18)}" y="${f(b.y + 22)}">Not in a group yet · ${n}</text>
          <text class="tv-g-sub" x="${f(b.x + 18)}" y="${f(b.y + 38)}">Found by the scanner or added by hand — drag each into its tower or site, or use Review</text>
          ${canEdit() && !locked() ? gripSvg("~", b) : ""}</g>`;
      }
      const gr = b.g, c = gr.counts || {};
      const mon = (c.total || 0) - (c.unmonitored || 0);
      const bits = [];
      if (mon) bits.push(`${c.online}/${mon} online`);
      if (c.offline) bits.push(`${c.offline} offline`);
      if (c.quiet) bits.push(`${c.quiet} not seen 7+ d`);
      if (c.unmonitored) bits.push(`${c.unmonitored} not monitored`);
      if (c.problems) bits.push(`${c.problems} problem${c.problems > 1 ? "s" : ""}`);
      const stTxt = !c.total ? "Empty" : { ok: "All online", warn: "Needs a look", down: "Down", unknown: "Not monitored" }[gr.status] || "";
      const stCol = { ok: "#34d399", warn: "#fbbf24", down: "#fb7185", unknown: "#94a3b8" }[gr.status] || "#94a3b8";
      const sel = S.sel && S.sel.t === "group" && S.sel.id === gr.id;
      const cls = `tv-g st-${gr.status} ${sel ? "is-sel" : ""} ${S.dropHighlight === gr.id ? "drop" : ""}`;
      const kindLabel = (S.g.group_kinds || {})[gr.kind] || "Group";
      const maxName = Math.max(8, Math.floor((b.w - 110) / 8.2));
      const chevron = b.collapsed ? "M-4,-2 L0,2 L4,-2" : "M-4,2 L0,-2 L4,2";
      let s = `<g class="${cls}" data-box="${esc(gr.id)}" aria-label="${esc(kindLabel + " " + gr.name + ": " + stTxt)}">
        <rect x="${f(b.x + 3)}" y="${f(b.y + 4)}" width="${f(b.w)}" height="${f(b.h)}" rx="16" fill="rgba(0,0,0,.35)"/>
        <rect class="tv-g-box" x="${f(b.x)}" y="${f(b.y)}" width="${f(b.w)}" height="${f(b.h)}" rx="16"/>
        <rect x="${f(b.x)}" y="${f(b.y + 44)}" width="${f(b.w)}" height="${f(Math.max(0, b.h - 50))}" fill="url(#tv-iso)"/>
        <path class="tv-g-hd" d="M${f(b.x)},${f(b.y + 44)} V${f(b.y + 16)} a16,16 0 0 1 16,-16 H${f(b.x + b.w - 16)} a16,16 0 0 1 16,16 V${f(b.y + 44)} Z"/>
        <rect x="${f(b.x)}" y="${f(b.y + 12)}" width="4" height="22" rx="2" fill="${stCol}"/>
        <rect class="tv-g-hit" data-drag-box="${esc(gr.id)}" x="${f(b.x)}" y="${f(b.y)}" width="${f(b.w - 70)}" height="44"/>
        <use href="#tvg-${esc(GICON[gr.kind] ? gr.kind : "site")}" x="${f(b.x + 12)}" y="${f(b.y + 11)}" width="22" height="22" style="color:#e2e8f0" pointer-events="none"/>
        <text class="tv-g-name" x="${f(b.x + 42)}" y="${f(b.y + 21)}" pointer-events="none">${esc(trunc(gr.name, maxName))}</text>
        <text class="tv-g-sub" x="${f(b.x + 42)}" y="${f(b.y + 37)}" pointer-events="none"><tspan fill="${stCol}" font-weight="700">${esc(stTxt)}</tspan>${bits.length && !b.collapsed ? esc(" · " + bits.join(" · ")) : ""}</text>
        <g class="tv-tog" data-toggle="${esc(gr.id)}" transform="translate(${f(b.x + b.w - 24)},${f(b.y + 22)})" role="button" tabindex="0" aria-expanded="${b.collapsed ? "false" : "true"}" aria-label="${b.collapsed ? "Expand" : "Collapse"} ${esc(gr.name)}"><rect x="-13" y="-13" width="26" height="26" rx="8"/><path d="${chevron}"/></g>
        ${canEdit() && !locked() && !b.collapsed && !gr.locked ? gripSvg(gr.id, b) : ""}
        ${gr.locked ? `<g transform="translate(${f(b.x + b.w - 50)},${f(b.y + 22)})" pointer-events="none"><rect x="-9" y="-9" width="18" height="18" rx="5" fill="rgba(148,163,184,.14)"/><svg x="-7" y="-7" width="14" height="14" viewBox="0 0 24 24" style="color:#fbbf24">${I.lock}</svg></g>` : ""}`;
      if (b.collapsed) {
        const lines = [];
        if (mon) lines.push([`${c.online} of ${mon} online`, c.online === mon ? "#6ee7b7" : "#e2e8f0"]);
        if (c.offline || c.quiet) lines.push([`${(c.offline || 0) + (c.quiet || 0)} offline`, "#fb7185"]);
        if (c.unmonitored) lines.push([`${c.unmonitored} not monitored`, "#cbd5e1"]);
        if (!lines.length) lines.push(["Empty", "#94a3b8"]);
        s += `<rect class="tv-g-hit" data-drag-box="${esc(gr.id)}" x="${f(b.x)}" y="${f(b.y + 44)}" width="${f(b.w)}" height="${f(b.h - 44)}"/>
          <use href="#tvg-${esc(GICON[gr.kind] ? gr.kind : "site")}" x="${f(b.x + 14)}" y="${f(b.y + 56)}" width="54" height="54" style="color:#cbd5e1" pointer-events="none"/>`
          + lines.map((l, i) => `<text x="${f(b.x + 78)}" y="${f(b.y + 70 + i * 16)}" font-size="12" font-weight="600" fill="${l[1]}" pointer-events="none">${esc(l[0])}</text>`).join("");
      }
      return s + "</g>";
    }
    /** Drag corner: makes a group or the review area bigger than its contents. */
    function gripSvg(id, b) {
      const f = (v) => Math.round(v);
      return `<g class="tv-g-grip" data-resize="${esc(id)}" transform="translate(${f(b.x + b.w)},${f(b.y + b.h)})" role="button" tabindex="-1" aria-label="Resize">
        <rect x="-20" y="-20" width="20" height="20" rx="6"/>
        <path d="M-14,-3 L-3,-14 M-8,-3 L-3,-8" stroke="#cbd5e1" stroke-width="1.6" stroke-linecap="round" fill="none"/></g>`;
    }
    function nodeTag(n) {
      if (n.state === "offline") return "Offline · " + ago(n.last_seen);
      if (n.state === "quiet") return "Not seen 7+ days";
      if (n.state === "unmonitored") {
        if (n.inferred && n.inferred.state === "suspect") return "? all neighbours down";
        if (n.inferred && n.inferred.state === "passing") return "Not monitored · passing";
        return "Not monitored";
      }
      if (n.radio && n.radio.ok) {
        const r = n.radio;
        if (r.signal != null) return `${Math.round(r.signal)} dBm${r.stations ? ` · ${r.stations} linked` : ""}`;
        if (r.stations) return `${r.stations} station${r.stations > 1 ? "s" : ""}`;
      }
      return kindOf(n.kind).label;
    }
    function iconRef(n) {
      const id = n.icon || (S.g.type_icons || {})[n.kind] || "";
      const b = builtinOf(id);
      if (b) return { builtin: b };
      if (id && S.g.icons && S.g.icons[id] && opts.iconUrl) return { href: opts.iconUrl(id) };
      return { builtin: ALL_ICONS[n.kind] ? n.kind : "other" };
    }
    /** The same picture the diagram uses, for the lists in the side panel. */
    const itemIcon = (n, w = 30, h = 26) => {
      const r = iconRef(n);
      return r.href ? `<img class="tv-ic" src="${esc(r.href)}" alt="" width="${w}" height="${h}" style="object-fit:contain">`
        : `<svg class="tv-ic" viewBox="0 0 64 56" width="${w}" height="${h}" aria-hidden="true"><use href="#tvk-${esc(r.builtin)}"/></svg>`;
    };
    function nodeSvg(n, a, hits, related, dragging) {
      const st = STATE[n.state] || STATE.online;
      const f = (v) => Math.round(v);
      const x = a.x, y = a.y;
      const sel = (S.sel && S.sel.t === "node" && S.sel.id === n.id) || S.multi.has(n.id);
      const dim = !dragging && ((hits && !hits.has(n.id)) || (!hits && !passesFilter(n)) || (related && !related.has(n.id)));
      const hit = hits && hits.has(n.id);
      const ref = iconRef(n);
      const icon = ref.href
        ? `<image class="ico" href="${esc(ref.href)}" x="${f(x + 43)}" y="${f(y + 6)}" width="64" height="56" preserveAspectRatio="xMidYMid meet"/>`
        : `<use class="ico" href="#tvk-${esc(ref.builtin)}" x="${f(x + 43)}" y="${f(y + 6)}" width="64" height="56"/>`;
      const sub = n.ip || (n.virtual ? kindOf(n.kind).label : n.mac) || "";
      const suspect = n.inferred && n.inferred.state === "suspect";
      const badge = suspect
        ? `<circle r="7.5" fill="#b45309" stroke="#fde68a" stroke-width="1"/><text y="3.8" text-anchor="middle" font-size="11" font-weight="800" fill="#fff">?</text>`
        : `<circle r="7.5" fill="${st.fill}" stroke="${st.dash ? "#94a3b8" : "#0b1020"}" stroke-width="1.2" ${st.dash ? `stroke-dasharray="2 1.5"` : ""}/>${st.glyph}`;
      const probs = (n.problems || []).length;
      const label = [n.name, kindOf(n.kind).label, st.label, n.ip, probs ? probs + " problem(s)" : ""].filter(Boolean).join(", ");
      const title = [n.name, kindOf(n.kind).label + (n.model ? " · " + n.model : ""), n.ip, n.mac, st.label + (n.state === "offline" ? " · last seen " + ago(n.last_seen) : ""),
        ...(n.problems || []).map((p) => "⚠ " + p.detail), n.locked ? "Position locked" : ""].filter(Boolean).join("\n");
      return `<g class="tv-n ${st.cls} ${sel ? "is-sel" : ""} ${dim ? "dim" : ""} ${hit ? "hit" : ""}" data-node="${esc(n.id)}" tabindex="0" role="button" aria-label="${esc(label)}">
        <title>${esc(title)}</title>
        <rect class="plate" x="${f(x + 4)}" y="${f(y + 2)}" width="${CW() - 8}" height="${CH() - 6}" rx="12"/>
        ${n.state === "offline" ? `<circle class="ring" cx="${f(x + 75)}" cy="${f(y + 36)}" r="31" fill="none" stroke="#fb7185" stroke-width="1.6" stroke-dasharray="4 3"/>` : ""}
        ${n.state === "unmonitored" ? `<ellipse cx="${f(x + 75)}" cy="${f(y + 38)}" rx="34" ry="27" fill="none" stroke="rgba(203,213,225,.35)" stroke-dasharray="3 4"/>` : ""}
        ${icon}
        <g transform="translate(${f(x + 107)},${f(y + 13)})">${badge}</g>
        ${probs ? `<g transform="translate(${f(x + 43)},${f(y + 13)})"><path d="M0,-8 L8,6 L-8,6 Z" fill="#f59e0b" stroke="#fde68a" stroke-width="1"/><text y="4.5" text-anchor="middle" font-size="9.5" font-weight="900" fill="#1c1917">!</text></g>` : ""}
        ${n.locked ? `<g transform="translate(${f(x + 18)},${f(y + 13)})" aria-hidden="true"><rect x="-9" y="-9" width="18" height="18" rx="5" fill="#1f2937" stroke="#fbbf24" stroke-width="1"/><svg x="-6.5" y="-6.5" width="13" height="13" viewBox="0 0 24 24" style="color:#fbbf24">${I.lock}</svg></g>` : ""}
        <text class="tv-n-name" x="${f(x + 75)}" y="${f(y + 80)}" text-anchor="middle">${esc(trunc(n.name, 21))}</text>
        <text class="tv-n-sub" x="${f(x + 75)}" y="${f(y + 95)}" text-anchor="middle">${esc(trunc(sub, 24))}</text>
        <text class="tv-n-tag" x="${f(x + 75)}" y="${f(y + 109)}" text-anchor="middle" fill="${suspect ? "#fbbf24" : ""}">${esc(trunc(nodeTag(n), 26))}</text>
        ${dragging ? "" : `<g class="tv-handle" data-handle="${esc(n.id)}" transform="translate(${f(x + CW() - 16)},${f(y + 36)})" aria-hidden="true"><circle r="8"/><path d="M-4,0 H4 M0,-4 V4"/></g>`}
      </g>`;
    }
    function edgeLook(e) {
      const Ls = e.links;
      const media = [...new Set(Ls.map((L) => L.medium))];
      const medium = media.length === 1 ? media[0] : media.includes("fibre") ? "fibre" : "ethernet";
      const rank = { down: 3, degraded: 2, unknown: 1, up: 0 };
      const status = Ls.map((L) => L.status).sort((a, b) => rank[b] - rank[a])[0];
      const confirmed = Ls.every((L) => L.confirmed);
      return { medium, status, confirmed, agg: Ls.length > 1 || e.A.box || e.B.box };
    }
    function edgeLabel(e, look) {
      if (look.agg) {
        const n = e.links.length;
        const wl = e.links.find((L) => L.metrics && L.metrics.signal != null);
        return (n > 1 ? `×${n}` : "") + (wl ? `${n > 1 ? " · " : ""}${Math.round(wl.metrics.signal)} dBm` : n > 1 ? "" : (e.links[0].label || ""));
      }
      const L = e.links[0];
      const bits = [];
      if (L.label) bits.push(L.label);
      if (L.medium === "wireless" && L.metrics) {
        if (L.metrics.signal != null) bits.push(Math.round(L.metrics.signal) + " dBm");
        if (L.metrics.distance) bits.push(km(L.metrics.distance));
      }
      if (!L.confirmed) bits.unshift("?");
      return bits.join(" · ");
    }
    function edgeSvg(e) {
      const look = edgeLook(e);
      const bend = e.pi ? (e.pi % 2 ? 1 : -1) * Math.ceil(e.pi / 2) * 22 : 0;
      const c = curve(e.A, e.B, bend);
      const m = MEDIUM[look.medium] || MEDIUM.ethernet;
      const st = look.status === "down" ? "down" : look.status === "degraded" ? "deg" : "";
      const cls = `tv-e ${m.cls} ${st} ${look.confirmed ? "" : "sug"} ${look.agg ? "agg" : ""}`;
      const related = relatedSet();
      const selected = S.sel && S.sel.t === "link" && e.links.some((L) => L.id === S.sel.id);
      const dim = related && !e.links.some((L) => related.has(L.id));
      const roomy = c.len > 90;
      const label = (S.labels && roomy) || look.agg || (!look.confirmed && roomy) ? edgeLabel(e, look) : "";
      const mid = c.at(0.5);
      let s = `<g class="tv-edge ${dim ? "dim" : ""} ${selected ? "is-sel" : ""}" data-edge="${esc(e.id)}">
        <path class="${cls}" d="${c.d}"/>${look.medium === "fibre" ? `<path class="tv-e fib2" d="${c.d}"/>` : ""}
        <path class="tv-e-hit" d="${c.d}"><title>${esc(e.links.map((L) => `${MEDIUM[L.medium].label}${L.confirmed ? "" : " (suggested)"}: ${nodeName(L.a)}${L.a_port ? " [" + L.a_port + "]" : ""} ↔ ${nodeName(L.b)}${L.b_port ? " [" + L.b_port + "]" : ""}`).join("\n"))}</title></path>`;
      if (label) {
        const w = Math.max(22, label.length * 6.1 + 14);
        s += `<g class="tv-pill ${look.confirmed ? "" : "sug"} ${look.status === "down" ? "down" : ""}" transform="translate(${mid[0].toFixed(1)},${mid[1].toFixed(1)})" pointer-events="none">
          <rect x="${(-w / 2).toFixed(1)}" y="-9" width="${w.toFixed(1)}" height="18" rx="9"/><text text-anchor="middle" y="3.8">${esc(label)}</text></g>`;
      }
      if (look.status === "down" && !label) {
        s += `<g transform="translate(${mid[0].toFixed(1)},${mid[1].toFixed(1)})" pointer-events="none"><circle r="8" fill="#0b1324" stroke="#fb7185"/><path d="M-3,-3 L3,3 M3,-3 L-3,3" stroke="#fb7185" stroke-width="1.8"/></g>`;
      }
      if (!look.agg && S.labels && S.k >= 0.6 && c.len > 130) {
        const L = e.links[0];
        const endA = e.A.key === L.a ? L.a_port : L.b_port;
        const endB = e.A.key === L.a ? L.b_port : L.a_port;
        const pa = c.at(0.14), pb = c.at(0.86);
        if (endA) s += `<text class="tv-port" x="${pa[0].toFixed(1)}" y="${(pa[1] + 3).toFixed(1)}" text-anchor="middle" pointer-events="none">${esc(trunc(endA, 12))}</text>`;
        if (endB) s += `<text class="tv-port" x="${pb[0].toFixed(1)}" y="${(pb[1] + 3).toFixed(1)}" text-anchor="middle" pointer-events="none">${esc(trunc(endB, 12))}</text>`;
      }
      return s + "</g>";
    }
    const nodeName = (id) => (S.idx.nodes[id] ? S.idx.nodes[id].name : id);

    function drawLegend() {
      const el = $("[data-legend]");
      if (!S.legend) {
        el.innerHTML = `<button type="button" data-legend-toggle aria-expanded="false">${ico("info", 14)} Legend</button>`;
        return;
      }
      const row = (sym, txt) => `<div class="row">${sym}<span>${txt}</span></div>`;
      const badge = (k) => { const s = STATE[k]; return `<svg width="18" height="18" viewBox="-9 -9 18 18"><circle r="7.5" fill="${s.fill}" stroke="${s.dash ? "#94a3b8" : "#0b1020"}" ${s.dash ? `stroke-dasharray="2 1.5"` : ""}/>${s.glyph}</svg>`; };
      const line = (cls, extra = "") => `<svg width="46" height="12"><path class="tv-e ${cls}" d="M3,6 H43" style="${extra}"/>${cls.includes("fib") ? `<path class="tv-e fib2" d="M3,6 H43"/>` : ""}</svg>`;
      const kinds = ["router", "switch", "unmanaged-switch", "radio", "ptp", "camera", "nvr", "poe", "internet", "ups"];
      el.innerHTML = `<button type="button" data-legend-toggle aria-expanded="true">${ico("info", 14)} Legend <span style="margin-left:auto">✕</span></button>
        <div class="body">
          <div><h4>Status</h4>${row(badge("online"), "Online")}${row(badge("offline"), "Offline (red dashed ring)")}${row(badge("quiet"), "Not seen for 7+ days")}${row(badge("unmonitored"), "Added by hand — not monitored, never “offline”")}
            ${row(`<svg width="18" height="18" viewBox="-9 -9 18 18"><circle r="7.5" fill="#b45309"/><text y="3.8" text-anchor="middle" font-size="11" font-weight="800" fill="#fff">?</text></svg>`, "Not monitored, and everything connected to it is down")}
            ${row(`<svg width="18" height="18" viewBox="-9 -9 18 18"><path d="M0,-8 L8,6 L-8,6 Z" fill="#f59e0b"/><text y="4.5" text-anchor="middle" font-size="9.5" font-weight="900" fill="#1c1917">!</text></svg>`, "Has a problem (IP conflict, weak link, switch port)")}</div>
          <div class="tv-svg" style="display:block;height:auto;cursor:default"><h4>Connections</h4>${row(line("eth"), "Ethernet")}${row(line("fib"), "Fibre")}${row(line("wl"), "Wireless (radio to radio)")}${row(line("eth sug"), "Suggested by the readings — confirm or dismiss")}${row(line("eth down"), "Down (an end is offline)")}${row(line("eth agg"), "Several links to a collapsed group (×N)")}</div>
          <div><h4>Equipment</h4><div class="grid">${kinds.map((k) => row(kindSvg(k, 26, 22), esc(kindOf(k).label))).join("")}</div></div>
        </div>`;
    }

    // ---- toolbar ---------------------------------------------------------------------------------
    function syncBar() {
      $$("[data-view]").forEach((b) => { b.classList.toggle("on", b.dataset.view === S.view); b.setAttribute("aria-pressed", b.dataset.view === S.view); });
      $("[data-f=status]").value = S.status;
      $("[data-f=kinds]").value = S.kinds;
      $$("[data-m]").forEach((c) => (c.checked = !!S.media[c.dataset.m]));
      $$("[data-o]").forEach((c) => (c.checked = c.dataset.o === "scope" ? S.scope === "all" : !!S[c.dataset.o]));
      const edit = canEdit();
      const lk = $("[data-a=lock]");
      lk.innerHTML = `${ico(locked() ? "lock" : "unlock")}<span class="lbl">${locked() ? "Layout locked" : "Lock layout"}</span>`;
      lk.classList.toggle("on", locked());
      $("[data-a=arrange]").disabled = locked();
      $$("[data-a=add-group],[data-a=add-equip],[data-a=add-link],[data-a=arrange],[data-a=lock],[data-a=icons]").forEach((b) => (b.title = edit ? b.title.replace(/ \(log in first\)$/, "") : b.title.replace(/ \(log in first\)$/, "") + " (log in first)"));
      const sugg = S.g.suggestions.length;
      const ua = (S.idx.byGroup["~"] || []).length;
      const rn = $("[data-rn]");
      rn.hidden = !(sugg || ua);
      rn.textContent = sugg || ua;
      rn.classList.toggle("q", !sugg);
      $("[data-a=review]").classList.toggle("on", S.panel === "review" && !S.sel);
      const bt = $("[data-bt]");
      const banner = S.connect ? (S.connect.from ? `Click the other end for ${nodeName(S.connect.from)}` : "Click the first piece of equipment to connect")
        : S.placing ? `Click the map where ${(S.idx.groups[S.placing] || {}).name || "the group"} is` : "";
      bt.textContent = banner;
      bt.parentElement.hidden = !banner;
      svg.classList.toggle("connecting", !!S.connect);
    }
    let prefTimer = null;
    const savePrefSoon = () => { clearTimeout(prefTimer); prefTimer = setTimeout(savePref, 400); };
    let qTimer = null;
    $("[data-q]").addEventListener("input", (e) => {
      S.q = e.target.value;
      clearTimeout(qTimer);
      qTimer = setTimeout(() => { if (S.view === "diagram") drawDiagram(); else drawMap(); }, 120);
    });
    $("[data-q]").addEventListener("keydown", (e) => {
      if (e.key !== "Enter" || !S.g) return;
      const hit = S.g.nodes.find(matchesQuery) || S.g.groups.find((g) => g.name.toLowerCase().includes(S.q.trim().toLowerCase()));
      if (hit) focus(hit.id); else toast("Nothing matches “" + S.q + "”");
    });
    $("[data-f=status]").addEventListener("change", (e) => { S.status = e.target.value; savePref(); render(); });
    $("[data-f=kinds]").addEventListener("change", (e) => { S.kinds = e.target.value; savePref(); render(); });
    $$("[data-m]").forEach((c) => c.addEventListener("change", () => { S.media[c.dataset.m] = c.checked; savePref(); render(); }));
    $$("[data-o]").forEach((c) => c.addEventListener("change", () => {
      if (c.dataset.o === "scope") { S.scope = c.checked ? "all" : "infra"; savePref(); refresh(); return; }
      S[c.dataset.o] = c.checked; savePref(); render();
    }));
    $$("[data-view]").forEach((b) => (b.onclick = () => setView(b.dataset.view)));
    $("[data-bc]").onclick = () => { S.connect = null; S.placing = null; render(); };
    $$("[data-z]").forEach((b) => (b.onclick = () => {
      const r = svg.getBoundingClientRect();
      if (b.dataset.z === "hand") { S.panMode = !S.panMode; savePref(); applyModes(); return; }
      if (b.dataset.z === "fit") { fitView(); drawDiagram(); return; }
      zoomAt(r.width / 2, r.height / 2, clamp(S.k * (b.dataset.z === "+" ? 1.25 : 0.8), 0.15, 2.5));
    }));
    root.addEventListener("click", (e) => {
      const lt = e.target.closest("[data-legend-toggle]");
      if (lt) { S.legend = !S.legend; savePref(); drawLegend(); return; }
      if (!e.target.closest(".tv-pop")) $$(".tv-pop[open]").forEach((d) => d.removeAttribute("open"));
      const a = e.target.closest("[data-a]");
      if (!a || !S.g) return;
      const what = a.dataset.a;
      if (what === "add-group") return needEdit(() => groupDialog());
      if (what === "add-equip") return needEdit(() => equipmentDialog());
      if (what === "add-link") return needEdit(() => linkDialog({}));
      if (what === "arrange") return needEdit(arrangeAll);
      if (what === "lock") return needEdit(() => act("POST", "/layout", { lock_all: !locked() }, { okMsg: locked() ? "Layout unlocked" : "Layout locked — nothing can be dragged until you unlock it" }));
      if (what === "review") { S.sel = null; S.panel = S.panel === "review" ? null : "review"; render(); return; }
      if (what === "icons") return iconLibrary();
      if (what === "dock") { S.dock = S.dock === "bottom" ? "right" : "bottom"; savePref(); applyModes(); drawDiagram(); return; }
    });
    async function needEdit(fn) {
      if (!canEdit() && opts.login) {
        const ok = await opts.login();
        if (!ok) return;
        await refresh(true);
      }
      return fn();
    }
    async function arrangeAll() {
      if (!(await confirmBox("Auto-arrange the diagram?", "Every tower, site and piece of equipment that is not locked is placed afresh, following the connections. Locked items stay where they are.", { ok: "Auto-arrange" }))) return;
      await act("POST", S.scope === "all" ? "/arrange?scope=all" : "/arrange", {}, { graph: false }).then((res) => { if (res.graph) setGraph(res.graph); S.fitted = false; render(); toast("Arranged", "ok"); }).catch(() => {});
    }
    function setView(v) {
      if (v !== "diagram" && v !== "map") return;
      S.view = v;
      S.connect = null;
      if (v !== "map") S.placing = null;
      savePref();
      render();
      if (opts.onRoute) opts.onRoute(v, S.sel && S.sel.id);
    }

    // ---- pan / zoom / drag ------------------------------------------------------------------------
    const pointers = new Map();
    let raf = 0;
    const schedule = () => { if (!raf) raf = requestAnimationFrame(() => { raf = 0; drawDiagram(); }); };
    function toWorld(cx, cy) {
      const r = svg.getBoundingClientRect();
      return { x: (cx - r.left - S.tx) / S.k, y: (cy - r.top - S.ty) / S.k };
    }
    function zoomAt(mx, my, k2) {
      S.tx = mx - (mx - S.tx) * (k2 / S.k);
      S.ty = my - (my - S.ty) * (k2 / S.k);
      const crossed = (S.k < 0.6) !== (k2 < 0.6);
      S.k = k2;
      applyTransform();
      if (crossed) schedule();
      savePrefSoon();
    }
    function fitView(save = true, only) {
      const r = svg.getBoundingClientRect();
      const W = r.width || 900, H = r.height || 520;
      const bs = only ? [only] : Object.values(S.boxes);
      if (!bs.length) { S.k = 1; S.tx = 40; S.ty = 40; applyTransform(); return; }
      const x0 = Math.min(...bs.map((b) => b.x)), y0 = Math.min(...bs.map((b) => b.y));
      const x1 = Math.max(...bs.map((b) => b.x + b.w)), y1 = Math.max(...bs.map((b) => b.y + b.h));
      const k = clamp(Math.min((W - 60) / Math.max(1, x1 - x0), (H - 60) / Math.max(1, y1 - y0)), 0.15, only ? 1.3 : 1.1);
      S.k = k;
      S.tx = (W - (x1 - x0) * k) / 2 - x0 * k;
      S.ty = (H - (y1 - y0) * k) / 2 - y0 * k;
      applyTransform();
      if (save) savePrefSoon();
    }
    function centerOn(x, y, k) {
      const r = svg.getBoundingClientRect();
      if (k) S.k = k;
      S.tx = (r.width || 900) / 2 - x * S.k;
      S.ty = (r.height || 520) / 2 - y * S.k;
      applyTransform();
      savePrefSoon();
    }
    function boxAt(w, exclude) {
      let best = null;
      for (const b of Object.values(S.boxes)) {
        if (w.x < b.x || w.x > b.x + b.w || w.y < b.y || w.y > b.y + b.h) continue;
        if (!best || b.w * b.h < best.w * best.h) best = b;
      }
      return best ? best.id : null;
    }
    const groupLocked = (n) => !!(S.idx.groups[n.group] && S.idx.groups[n.group].locked);
    const cellKey = (p) => `${Math.round((p.y - G().pad_top) / G().cell_h)}:${Math.round((p.x - G().pad_x) / G().cell_w)}`;
    function snapCell(rel, cid, moving, claimed) {
      const g = G();
      const c0 = Math.max(0, Math.round((rel.x - g.pad_x) / g.cell_w));
      const r0 = Math.max(0, Math.round((rel.y - g.pad_top) / g.cell_h));
      const taken = new Set((S.idx.byGroup[cid] || []).filter((m) => !moving.has(m.id) && m.pos).map((m) => cellKey(m.pos)));
      claimed.forEach((k) => taken.add(k));
      for (let dist = 0; dist < 50; dist++) {
        for (let dr = -dist; dr <= dist; dr++) {
          for (let dc = -dist; dc <= dist; dc++) {
            if (Math.max(Math.abs(dr), Math.abs(dc)) !== dist) continue;
            const r = r0 + dr, c = c0 + dc;
            if (r < 0 || c < 0 || taken.has(`${r}:${c}`)) continue;
            claimed.add(`${r}:${c}`);
            return { x: g.pad_x + c * g.cell_w, y: g.pad_top + r * g.cell_h };
          }
        }
      }
      return { x: g.pad_x + c0 * g.cell_w, y: g.pad_top + r0 * g.cell_h };
    }
    function nodeUnder(cx, cy) {
      const el = document.elementFromPoint(cx, cy);
      const n = el && el.closest && el.closest("[data-node]");
      return n && root.contains(n) ? n.dataset.node : null;
    }

    svg.addEventListener("pointerdown", (ev) => {
      if (!S.g || (ev.pointerType === "mouse" && ev.button !== 0)) return;
      pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
      if (pointers.size === 2) {
        const [p, q] = [...pointers.values()];
        S.drag = { type: "pinch", d0: Math.hypot(p.x - q.x, p.y - q.y), k0: S.k };
        S.over.nodes = {}; S.over.groups = {}; S.over.ua = null; S.over.size = {}; S.ghost = ""; S.marquee = null;
        return;
      }
      const t = ev.target;
      const base = { x0: ev.clientX, y0: ev.clientY, w0: toWorld(ev.clientX, ev.clientY), moved: false };
      const editable = canEdit() && !locked();
      const tog = t.closest("[data-toggle]");
      const handle = t.closest("[data-handle]");
      const nodeEl = t.closest("[data-node]");
      const edgeEl = t.closest("[data-edge]");
      const boxEl = t.closest("[data-drag-box]");
      const gripEl = t.closest("[data-resize]");
      if (tog) S.drag = { ...base, type: "toggle", id: tog.dataset.toggle };
      else if (gripEl && editable) {
        const gb = S.boxes[gripEl.dataset.resize];
        S.drag = { ...base, type: "resize", id: gripEl.dataset.resize, start: { w: gb.w, h: gb.h }, min: gb.min || { w: gb.w, h: gb.h } };
      }
      else if (S.connect) S.drag = { ...base, type: "pick", node: nodeEl ? nodeEl.dataset.node : null };
      else if (handle && editable) S.drag = { ...base, type: "connect", from: handle.dataset.handle };
      else if (nodeEl) {
        const n = S.idx.nodes[nodeEl.dataset.node];
        S.drag = { ...base, type: "node", id: n.id, shift: ev.shiftKey || ev.metaKey || ev.ctrlKey, can: editable && !n.locked && !groupLocked(n) };
      } else if (edgeEl) S.drag = { ...base, type: "edge", id: edgeEl.dataset.edge };
      else if (boxEl) {
        const id = boxEl.dataset.dragBox;
        const b = S.boxes[id];
        S.drag = { ...base, type: "box", id, start: { x: b.x, y: b.y }, can: editable && (id === "~" || !S.idx.groups[id].locked) };
      } else if (S.panMode || S.space || ev.button === 1 || ev.pointerType === "touch") {
        S.drag = { ...base, type: "pan", tx0: S.tx, ty0: S.ty };
        svg.classList.add("panning");
      } else {
        // empty canvas + mouse = draw a box around equipment to select it
        S.drag = { ...base, type: "marquee", add: ev.shiftKey || ev.metaKey || ev.ctrlKey };
      }
      try { svg.setPointerCapture(ev.pointerId); } catch (e) { /* synthetic events */ }
    });
    svg.addEventListener("pointermove", (ev) => {
      if (pointers.has(ev.pointerId)) pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
      const D = S.drag;
      if (!D) return;
      if (D.type === "pinch") {
        if (pointers.size < 2) return;
        const [p, q] = [...pointers.values()];
        const r = svg.getBoundingClientRect();
        zoomAt((p.x + q.x) / 2 - r.left, (p.y + q.y) / 2 - r.top, clamp(D.k0 * Math.hypot(p.x - q.x, p.y - q.y) / Math.max(1, D.d0), 0.15, 2.5));
        return;
      }
      const dx = ev.clientX - D.x0, dy = ev.clientY - D.y0;
      if (!D.moved && Math.abs(dx) + Math.abs(dy) < 5) return;
      const first = !D.moved;
      D.moved = true;
      const w = toWorld(ev.clientX, ev.clientY);
      if (D.type === "pan") {
        S.tx = D.tx0 + dx; S.ty = D.ty0 + dy;
        applyTransform();
      } else if (D.type === "node" && D.can) {
        if (first) {
          const ids = S.multi.has(D.id) || (S.sel && S.sel.t === "node" && S.sel.id === D.id && S.multi.size)
            ? [...new Set([D.id, ...S.multi, ...(S.sel && S.sel.t === "node" ? [S.sel.id] : [])])] : [D.id];
          D.ids = ids.filter((id) => { const n = S.idx.nodes[id]; return n && !n.locked && !groupLocked(n) && absPos(n); });
          D.starts = {};
          D.ids.forEach((id) => (D.starts[id] = absPos(S.idx.nodes[id])));
        }
        D.ids.forEach((id) => (S.over.nodes[id] = { x: D.starts[id].x + w.x - D.w0.x, y: D.starts[id].y + w.y - D.w0.y }));
        const tgt = boxAt(w);
        S.dropTarget = tgt || null;
        S.dropHighlight = tgt && D.ids.some((id) => S.idx.nodes[id].group !== tgt) ? tgt : null;
        schedule();
      } else if (D.type === "box" && D.can) {
        const p = { x: Math.round((D.start.x + w.x - D.w0.x) / 10) * 10, y: Math.round((D.start.y + w.y - D.w0.y) / 10) * 10 };
        if (D.id === "~") S.over.ua = p; else S.over.groups[D.id] = p;
        schedule();
      } else if (D.type === "resize") {
        S.over.size[D.id] = { w: Math.max(D.min.w, Math.round((D.start.w + w.x - D.w0.x) / 10) * 10),
                              h: Math.max(D.min.h, Math.round((D.start.h + w.y - D.w0.y) / 10) * 10) };
        schedule();
      } else if (D.type === "marquee") {
        S.marquee = { x: Math.min(D.w0.x, w.x), y: Math.min(D.w0.y, w.y), w: Math.abs(w.x - D.w0.x), h: Math.abs(w.y - D.w0.y) };
        schedule();
      } else if (D.type === "connect") {
        const n = S.idx.nodes[D.from], a = absPos(n);
        const tgt = nodeUnder(ev.clientX, ev.clientY);
        S.ghost = `<path class="tv-ghost" pointer-events="none" d="M${(a.x + CW() / 2).toFixed(1)},${(a.y + 34).toFixed(1)} L${w.x.toFixed(1)},${w.y.toFixed(1)}"/>`
          + (tgt && tgt !== D.from ? `<circle pointer-events="none" cx="${w.x.toFixed(1)}" cy="${w.y.toFixed(1)}" r="9" fill="#0e7490" stroke="#67e8f9"/>` : "");
        schedule();
      }
    });
    function endDrag(ev, cancelled) {
      pointers.delete(ev.pointerId);
      const D = S.drag;
      if (!D || (D.type === "pinch" && pointers.size)) return;
      S.drag = null;
      svg.classList.remove("panning");
      if (cancelled) { S.over = { nodes: {}, groups: {}, ua: null, size: {} }; S.marquee = null; S.ghost = ""; S.dropTarget = null; S.dropHighlight = null; drawDiagram(); return; }
      if (D.type === "pinch") { drawDiagram(); return; }
      if (D.type === "pan") {
        if (!D.moved) { S.sel = null; S.multi.clear(); render(); }
        savePrefSoon();
      } else if (D.type === "resize") {
        if (D.moved && S.over.size[D.id]) saveBoxSize(D.id, S.over.size[D.id]);
        else { S.over.size = {}; drawDiagram(); }
      } else if (D.type === "marquee") {
        const m = S.marquee;
        S.marquee = null;
        if (D.moved && m && (m.w > 4 || m.h > 4)) selectInBox(m, D.add);
        else if (!D.moved) { S.sel = null; S.multi.clear(); }
        render();
      } else if (D.type === "toggle") toggleGroup(D.id);
      else if (D.type === "pick") { if (D.node) pickConnect(D.node); }
      else if (D.type === "node") {
        if (!D.moved || !D.can) {
          if (D.moved && !D.can) {
            if (!canEdit() && opts.login) needEdit(() => toast("Logged in — drag it again"));
            else toast(locked() ? "The layout is locked — unlock it to move things" : "That position is locked", "");
          }
          if (D.shift) {
            if (S.multi.has(D.id)) S.multi.delete(D.id); else S.multi.add(D.id);
            if (S.sel && S.sel.t === "node" && S.sel.id !== D.id) S.multi.add(S.sel.id);
          } else S.multi.clear();
          S.sel = { t: "node", id: D.id };
          S.panel = null;
          render();
        } else dropNodes(D);
      } else if (D.type === "edge") selectEdge(D.id);
      else if (D.type === "box") {
        if (!D.moved || !D.can) {
          if (D.moved && !D.can) {
            if (!canEdit() && opts.login) needEdit(() => toast("Logged in — drag it again"));
            else toast(locked() ? "The layout is locked — unlock it to move things" : "That group is locked");
          }
          S.sel = D.id === "~" ? { t: "ua", id: "~" } : { t: "group", id: D.id };
          S.over.groups = {}; S.over.ua = null;
          S.panel = D.id === "~" ? "review" : null;
          render();
        } else saveBoxPos(D);
      } else if (D.type === "connect") {
        S.ghost = "";
        const tgt = nodeUnder(ev.clientX, ev.clientY);
        if (tgt && tgt !== D.from) linkDialog({ a: D.from, b: tgt });
        else if (!D.moved) { S.sel = { t: "node", id: D.from }; }
        render();
      }
      if (S.pending && !S.drag) {
        const g = S.pending;
        S.pending = null;
        // a poll that finished during a drag predates the drop the drag just saved
        if (!(D.moved && D.can && (D.type === "node" || D.type === "box"))) setGraph(g);
      }
    }
    svg.addEventListener("pointerup", (ev) => endDrag(ev, false));
    svg.addEventListener("pointercancel", (ev) => endDrag(ev, true));
    svg.addEventListener("wheel", (ev) => {
      if (!S.g) return;
      ev.preventDefault();
      const r = svg.getBoundingClientRect();
      zoomAt(ev.clientX - r.left, ev.clientY - r.top, clamp(S.k * Math.exp(-ev.deltaY * (ev.ctrlKey ? 0.01 : 0.0015)), 0.15, 2.5));
    }, { passive: false });
    svg.addEventListener("dblclick", (ev) => {
      if (!S.g) return;
      const box = ev.target.closest("[data-drag-box]");
      const node = ev.target.closest("[data-node]");
      if (node) {
        const n = S.idx.nodes[node.dataset.node];
        if (n.virtual) needEdit(() => equipmentDialog(n)); else if (opts.openDevice) opts.openDevice(n.id);
      } else if (box && box.dataset.dragBox !== "~") toggleGroup(box.dataset.dragBox);
      else if (!box) { const r = svg.getBoundingClientRect(); zoomAt(ev.clientX - r.left, ev.clientY - r.top, clamp(S.k * 1.5, 0.15, 2.5)); }
    });
    svg.addEventListener("keyup", (ev) => { if (ev.key === " " && S.space) { S.space = false; applyModes(); } });
    svg.addEventListener("blur", () => { if (S.space) { S.space = false; applyModes(); } });
    svg.addEventListener("keydown", (ev) => {
      if (!S.g) return;
      const nodeEl = ev.target.closest && ev.target.closest("[data-node]");
      const togEl = ev.target.closest && ev.target.closest("[data-toggle]");
      if (ev.key === " " && !ev.target.closest("[data-node]") && !S.space) { S.space = true; applyModes(); ev.preventDefault(); return; }
      if (ev.key === "Escape") { S.connect = null; S.sel = null; S.multi.clear(); render(); return; }
      if ((ev.key === "Enter" || ev.key === " ") && togEl) {
        ev.preventDefault();
        const id = togEl.dataset.toggle;
        toggleGroup(id);
        const again = LG.querySelector(`[data-toggle="${window.CSS && window.CSS.escape ? window.CSS.escape(id) : id}"]`);
        if (again) again.focus({ preventScroll: true });
        return;
      }
      if ((ev.key === "Enter" || ev.key === " ") && nodeEl) {
        ev.preventDefault();
        if (S.connect) pickConnect(nodeEl.dataset.node);
        else { S.sel = { t: "node", id: nodeEl.dataset.node }; S.panel = null; render(); focusNodeEl(nodeEl.dataset.node); }
        return;
      }
      if (ev.key === "f" || ev.key === "F") { fitView(); drawDiagram(); return; }
      if (ev.key === "+" || ev.key === "=") { const r = svg.getBoundingClientRect(); zoomAt(r.width / 2, r.height / 2, clamp(S.k * 1.25, 0.15, 2.5)); return; }
      if (ev.key === "-") { const r = svg.getBoundingClientRect(); zoomAt(r.width / 2, r.height / 2, clamp(S.k * 0.8, 0.15, 2.5)); return; }
      if ((ev.key === "Delete" || ev.key === "Backspace") && S.sel) { ev.preventDefault(); deleteSelected(); return; }
      const arrows = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
      if (arrows[ev.key] && S.sel && S.sel.t === "node" && canEdit() && !locked()) {
        const n = S.idx.nodes[S.sel.id];
        if (!n || n.locked || groupLocked(n) || !n.pos) return;
        ev.preventDefault();
        const [dc, dr] = arrows[ev.key];
        const p = { x: Math.max(G().pad_x, n.pos.x + dc * CW()), y: Math.max(G().pad_top, n.pos.y + dr * CH()) };
        if ((S.idx.byGroup[n.group] || []).some((m) => m.id !== n.id && m.pos && cellKey(m.pos) === cellKey(p))) return;
        n.pos = p;
        drawDiagram();
        focusNodeEl(n.id);
        opts.call("POST", "/layout", { nodes: { [n.id]: { pos: p } } }).catch((e) => toast(e.message, "bad"));
      }
    });
    function focusNodeEl(id) {
      const el = LN.querySelector(`[data-node="${window.CSS && window.CSS.escape ? window.CSS.escape(id) : id}"]`);
      if (el) el.focus({ preventScroll: true });
    }
    function dropNodes(D) {
      const target = S.dropTarget;
      S.dropTarget = null;
      S.dropHighlight = null;
      const moving = new Set(D.ids);
      const claimed = {};
      const body = { nodes: {} };
      let regroup = false;
      for (const id of D.ids) {
        const n = S.idx.nodes[id];
        const abs = S.over.nodes[id];
        const cid = target || n.group;
        const b = S.boxes[cid];
        claimed[cid] = claimed[cid] || new Set();
        const entry = {};
        if (cid !== n.group) { entry.group = cid === "~" ? null : cid; regroup = true; }
        if (b && !b.collapsed) entry.pos = snapCell({ x: abs.x - b.x, y: abs.y - b.y }, cid, moving, claimed[cid]);
        body.nodes[id] = entry;
        if (!regroup && entry.pos) n.pos = entry.pos;
      }
      S.over.nodes = {};
      if (regroup) {
        const names = D.ids.map(nodeName);
        const dest = target === "~" ? "“Not in a group yet”" : (S.idx.groups[target] || {}).name;
        D.ids.forEach((id) => { const n = S.idx.nodes[id]; n.group = target; if (body.nodes[id].pos) n.pos = body.nodes[id].pos; else n.pos = null; });
        setGraph(S.g);
        act("POST", "/layout", body, { okMsg: `${names.length > 1 ? names.length + " items" : names[0]} moved to ${dest}` }).catch(() => refresh(true));
      } else {
        drawDiagram();
        send("POST", "/layout", body).catch((e) => { toast(e.message, "bad"); refresh(true); });
      }
    }
    /** A box that was dragged bigger keeps that size until it is dragged back. */
    function saveBoxSize(id, sz) {
      const b = S.boxes[id], min = (b && b.min) || sz;
      const fixed = sz.w <= min.w && sz.h <= min.h ? "" : sz;     // back to hugging the contents
      S.over.size = {};
      if (id === "~") { if (S.g.unassigned) S.g.unassigned.fixed_size = fixed || null; }
      else if (S.idx.groups[id]) S.idx.groups[id].fixed_size = fixed || null;
      drawDiagram();
      send("POST", "/layout", id === "~" ? { unassigned: { size: fixed } } : { groups: { [id]: { size: fixed } } })
        .catch((e) => { toast(e.message, "bad"); refresh(true); });
    }
    /** Everything whose middle is inside the box the operator drew. */
    function selectInBox(m, add) {
      if (!add) S.multi.clear();
      (S.g.nodes || []).forEach((n) => {
        const a = absPos(n);
        if (!a || !passesFilter(n)) return;
        const cx = a.x + CW() / 2, cy = a.y + CH() / 2;
        if (cx >= m.x && cx <= m.x + m.w && cy >= m.y && cy <= m.y + m.h) S.multi.add(n.id);
      });
      const picked = [...S.multi];
      S.sel = picked.length === 1 ? { t: "node", id: picked[0] } : null;
      S.panel = null;
      if (!picked.length) toast("Nothing inside the box");
    }
    function saveBoxPos(D) {
      const p = D.id === "~" ? S.over.ua : S.over.groups[D.id];
      S.over.groups = {}; S.over.ua = null;
      if (!p) return drawDiagram();
      if (D.id === "~") S.g.unassigned.pos = p; else S.idx.groups[D.id].pos = p;
      drawDiagram();
      send("POST", "/layout", D.id === "~" ? { unassigned: { pos: p } } : { groups: { [D.id]: { pos: p } } })
        .catch((e) => { toast(e.message, "bad"); refresh(true); });
    }
    const localCollapse = {};
    function applyLocalCollapse() {
      Object.entries(localCollapse).forEach(([id, v]) => { if (S.idx.groups[id]) S.idx.groups[id].collapsed = v; });
    }
    function toggleGroup(id) {
      const gr = S.idx.groups[id];
      if (!gr) return;
      gr.collapsed = !gr.collapsed;
      if (canEdit()) send("POST", `/groups/${encodeURIComponent(id)}`, { collapsed: gr.collapsed }).catch(() => refresh(true));
      else localCollapse[id] = gr.collapsed;
      render();
    }
    function selectEdge(eid) {
      const e = S.edges.find((x) => x.id === eid);
      if (!e) return;
      if (e.links.length === 1) { S.sel = { t: "link", id: e.links[0].id }; S.panel = null; }
      else {
        const gid = (e.A.box ? e.A.key : e.B.key).slice(2);
        S.sel = { t: "group", id: gid };
        S.panel = null;
      }
      render();
    }
    function pickConnect(id) {
      if (!S.connect) return;
      if (!S.connect.from) { S.connect.from = id; S.sel = { t: "node", id }; render(); return; }
      if (id === S.connect.from) return;
      const a = S.connect.from;
      S.connect = null;
      render();
      linkDialog({ a, b: id });
    }
    async function deleteSelected() {
      if (!canEdit()) return;
      if (S.sel.t === "link") {
        const L = S.idx.links[S.sel.id];
        if (!L) return;
        if (!L.confirmed) return act("POST", `/suggestions/${encodeURIComponent(L.id)}`, { action: "dismiss" }, { okMsg: "Suggestion dismissed" }).catch(() => {});
        if (await confirmBox("Delete this connection?", `${nodeName(L.a)} ↔ ${nodeName(L.b)} (${MEDIUM[L.medium].label})`, { danger: true, ok: "Delete" }))
          act("DELETE", `/links/${encodeURIComponent(L.id)}`, undefined, { okMsg: "Connection deleted" }).then(() => { S.sel = null; render(); }).catch(() => {});
      } else if (S.sel.t === "node") {
        const n = S.idx.nodes[S.sel.id];
        if (n && n.virtual) deleteEquipment(n);
      }
    }
    async function deleteEquipment(n) {
      const links = (S.idx.adj[n.id] || []).filter((L) => L.confirmed).length;
      if (!(await confirmBox(`Delete ${n.name}?`, `This hand-added equipment and its ${links} connection${links === 1 ? "" : "s"} will be removed from the diagram and the map.`, { danger: true, ok: "Delete" }))) return;
      act("DELETE", `/equipment/${encodeURIComponent(n.id)}`, undefined, { okMsg: `${n.name} deleted` }).then(() => { S.sel = null; render(); }).catch(() => {});
    }

    // ---- side panel -----------------------------------------------------------------------------
    const stateText = (n) => {
      if (n.state === "offline") return `Offline — last seen ${ago(n.last_seen)}`;
      if (n.state === "quiet") return `Not seen for 7+ days (last ${ago(n.last_seen)})`;
      if (n.state === "unmonitored") return "Not monitored — added by hand";
      return "Online";
    };
    const stateBadge = (n) => {
      const st = STATE[n.state] || STATE.online;
      return `<span class="tv-b ${st.b}">${n.state === "online" ? "✓" : n.state === "offline" ? "✕" : n.state === "quiet" ? "–" : "⊘"} ${esc(st.label)}</span>`;
    };
    const groupName = (gid) => (gid === "~" ? "Not in a group yet" : (S.idx.groups[gid] || {}).name || "—");
    const LINK_STATUS = { up: ["ok", "✓ Up"], down: ["bad", "✕ Down"], degraded: ["warn", "! Weak"], unknown: ["", "? Not monitored"] };
    const SOURCE = { manual: "Added by hand", discovered: "Confirmed from the readings", switch: "Switch address table", router: "Router bridge table", radio: "Radio station table" };
    function groupOptions(cur, withUa = true) {
      return (withUa ? `<option value="~" ${cur === "~" ? "selected" : ""}>Not in a group yet</option>` : "")
        + S.g.groups.slice().sort((a, b) => a.name.localeCompare(b.name))
          .map((g) => `<option value="${esc(g.id)}" ${cur === g.id ? "selected" : ""}>${esc(g.name)} (${esc((S.g.group_kinds || {})[g.kind] || g.kind)})</option>`).join("");
    }
    const KIND_SECTIONS = [["Wireless", ["ptp", "radio", "wifi-router"]], ["Network", ["internet", "router", "switch", "unmanaged-switch", "media-converter", "patch-panel"]],
      ["Cameras", ["camera", "camera-ptz", "camera-dome", "camera-turret", "camera-bullet", "camera-dual", "camera-pano", "camera-thermal", "camera-anpr", "intercom", "nvr"]],
      ["Power", ["poe", "ups", "solar", "alarm"]], ["Other", ["server", "nas", "pc", "printer", "phone", "media", "iot", "other"]]];
    /** <option>s for every type, grouped — 30+ in one flat list is unreadable. */
    function kindOptionList(cur) {
      const seen = new Set();
      let out = "";
      KIND_SECTIONS.forEach(([label, ids]) => {
        const opts2 = ids.filter((id) => kindOf(id).label !== id).map((id) => { seen.add(id); return `<option value="${esc(id)}" ${cur === id ? "selected" : ""}>${esc(kindOf(id).label)}</option>`; }).join("");
        if (opts2) out += `<optgroup label="${esc(label)}">${opts2}</optgroup>`;
      });
      const rest = S.g.kinds.filter((k) => k.id !== "unknown" && !seen.has(k.id));
      if (rest.length) out += `<optgroup label="Other">${rest.map((k) => `<option value="${esc(k.id)}" ${cur === k.id ? "selected" : ""}>${esc(k.label)}</option>`).join("")}</optgroup>`;
      return out;
    }
    function kindOptions(cur, auto) {
      return (auto != null ? `<option value="" ${!cur ? "selected" : ""}>Detected: ${esc(kindOf(auto).label)}</option>` : "") + kindOptionList(cur);
    }
    function linkItem(L, from) {
      const other = L.a === from ? L.b : L.a;
      const o = S.idx.nodes[other] || { name: other, kind: "other" };
      const myPort = L.a === from ? L.a_port : L.b_port, otherPort = L.a === from ? L.b_port : L.a_port;
      const st = LINK_STATUS[L.status] || LINK_STATUS.unknown;
      const bits = [MEDIUM[L.medium].label];
      if (myPort) bits.push("port " + myPort);
      if (otherPort) bits.push("→ " + otherPort);
      if (L.metrics && L.metrics.signal != null) bits.push(Math.round(L.metrics.signal) + " dBm");
      if (o.group !== (S.idx.nodes[from] || {}).group) bits.push(groupName(o.group));
      return `<button type="button" class="tv-item" data-sel-link="${esc(L.id)}">${kindSvg(o.kind)}<span class="t">${micon(L.medium, 13)} ${esc(o.name)}</span>
        <span class="r">${L.confirmed ? `<span class="tv-b ${st[0]}">${esc(st[1])}</span>` : `<span class="tv-b warn">Suggested</span>`}</span><span class="s">${esc(bits.join(" · "))}</span></button>`;
    }
    function nodeItem(n, extra = "") {
      const st = STATE[n.state] || STATE.online;
      const bits = [n.ip || kindOf(n.kind).label, st.label];
      if (n.radio && n.radio.signal != null) bits.push(Math.round(n.radio.signal) + " dBm");
      if ((n.problems || []).length) bits.push(`⚠ ${n.problems.length}`);
      return `<div class="tv-item" role="button" tabindex="0" data-sel-node="${esc(n.id)}">${kindSvg(n.kind)}<span class="t">${esc(n.name)}</span>
        <span class="r"><span class="tv-b ${st.b}" title="${esc(st.label)}">${n.state === "online" ? "✓" : n.state === "offline" ? "✕" : n.state === "quiet" ? "–" : "⊘"}</span>${extra}</span><span class="s">${esc(bits.join(" · "))}</span></div>`;
    }
    const metric = (l, v, cls = "") => `<div class="tv-metric"><div class="l">${esc(l)}</div><div class="v ${cls}">${v}</div></div>`;
    const sigCls = (v) => (v == null ? "" : v >= -65 ? "ok" : v >= -75 ? "warn" : "bad");
    function radioSection(r) {
      if (!r) return "";
      if (!r.ok) return `<div class="sec"><h4>Radio</h4><p class="tv-note">Netwatch could not read this radio${r.error ? ` (${esc(r.error)})` : ""} — the device itself may still be up.</p></div>`;
      const m = [];
      if (r.signal != null) m.push(metric("Signal", dbm(r.signal), sigCls(r.signal)));
      if (r.noise != null) m.push(metric("Noise floor", dbm(r.noise)));
      if (r.stations != null) m.push(metric("Linked radios", r.stations));
      if (r.airtime != null) m.push(metric("Air time", Math.round(r.airtime) + " %", r.airtime >= 80 ? "warn" : ""));
      if (r.cap_dl != null) m.push(metric("Capacity ↓", Math.round(r.cap_dl) + " %"));
      if (r.freq != null) m.push(metric("Channel", `${esc(r.freq)}${r.chanbw ? " / " + esc(r.chanbw) : ""}`));
      return `<div class="sec"><h4>Radio <span class="c">${esc(r.mode || "")}${r.ssid ? " · " + esc(r.ssid) : ""} · read ${esc(ago(r.ts))}</span></h4><div class="tv-metrics">${m.join("")}</div></div>`;
    }
    function nodePanel(n) {
      if (!n) return "";
      const edit = canEdit();
      const links = (S.idx.adj[n.id] || []).slice().sort((a, b) => (b.confirmed - a.confirmed));
      const kv = [];
      const row = (k, v) => v ? kv.push(`<dt>${esc(k)}</dt><dd>${v}</dd>`) : null;
      if (n.virtual) {
        row("Model", esc(n.model));
        row("IP", n.ip ? `<span class="mono">${esc(n.ip)}</span>` : `<span class="dim">none (not needed)</span>`);
        row("MAC", n.mac ? `<span class="mono">${esc(n.mac)}</span>` : "");
        row("Ports", n.ports != null ? esc(n.ports) : "");
        row("Port notes", esc(n.port_notes));
        row("Notes", n.notes ? esc(n.notes).replace(/\n/g, "<br>") : "");
      } else {
        row("IP", `<span class="mono">${esc(n.ip)}</span>`);
        row("MAC", `<span class="mono">${esc(n.mac)}</span>`);
        row("Vendor", esc(n.vendor));
        row("Model", esc(n.model));
        row("Firmware", n.firmware ? `<span class="mono">${esc(n.firmware)}</span>` : "");
        row("Last seen", n.state === "online" ? "now" + (n.rtt != null ? ` · ${esc(Math.round(n.rtt))} ms` : "") : esc(ago(n.last_seen)));
        if (n.switch_port) row("Plugged into", `${esc(nodeName(n.switch_port.switch_key))} · port ${esc(String(n.switch_port.port).split("/").pop())}${n.switch_port.uplink ? " (via an uplink)" : ""}`);
      }
      const probs = (n.problems || []).map((p) => `<div class="tv-sug"><div class="t">⚠ ${esc(p.detail)}</div>${p.fix ? `<p>${esc(p.fix)}</p>` : ""}</div>`).join("");
      let inferred = "";
      if (n.virtual) {
        const inf = n.inferred;
        inferred = `<div class="sec"><h4>Status</h4><p class="tv-note">Netwatch cannot check this equipment itself, so it is never shown as offline.${inf ? inf.state === "suspect"
          ? ` <b style="color:#fbbf24">All ${inf.total} monitored devices connected to it are down</b> — it may have lost power or failed.`
          : ` ${inf.online} of the ${inf.total} monitored devices connected to it answer, so it is passing traffic.` : ""}</p></div>`;
      }
      const matches = n.virtual ? S.g.nodes.filter((d) => !d.virtual && ((n.ip && d.ip === n.ip) || (n.mac && d.mac && d.mac.toLowerCase() === n.mac))) : [];
      const geoOk = !!(n.geo || (S.idx.groups[n.group] && S.idx.groups[n.group].geo));
      const acts = [];
      if (!n.virtual && opts.openDevice) acts.push(`<button type="button" class="tv-btn sm pri" data-p="open">Open device</button>`);
      if (n.virtual) acts.push(`<button type="button" class="tv-btn sm pri" data-p="edit">Edit</button>`);
      acts.push(`<button type="button" class="tv-btn sm" data-p="connect">${ico("link", 14)} Connect to…</button>`);
      if (S.view === "diagram" && geoOk) acts.push(`<button type="button" class="tv-btn sm" data-p="show-map">${ico("map", 14)} Show on map</button>`);
      if (S.view === "map") acts.push(`<button type="button" class="tv-btn sm" data-p="show-diagram">${ico("diagram", 14)} Show in diagram</button>`);
      if (!n.virtual && n.group === "~" && !links.some((L) => L.confirmed)) acts.push(`<button type="button" class="tv-btn sm" data-p="hide" title="Leave it off the diagram (it stays in the device list)">Hide from diagram</button>`);
      matches.forEach((d) => acts.push(`<button type="button" class="tv-btn sm ok" data-p="replace" data-dev="${esc(d.id)}" title="The scanner found this equipment — let the discovered device take its place">Use discovered ${esc(d.name)}</button>`));
      if (n.virtual) acts.push(`<button type="button" class="tv-btn sm danger" data-p="delete">Delete</button>`);
      const placement = `<div class="sec"><h4>On the diagram</h4>
        <div style="display:grid;gap:8px">
          <label class="tv-field">Group<select class="tv-inp" data-set="group" ${edit ? "" : "disabled"}>${groupOptions(n.group)}</select></label>
          ${n.virtual ? "" : `<label class="tv-field">Equipment type<select class="tv-inp" data-set="kind" ${edit ? "" : "disabled"}>${kindOptions(n.kind_set ? n.kind : "", n.kind_set ? "" : n.kind)}</select></label>`}
          ${n.suggested_group && n.group === "~" ? `<button type="button" class="tv-btn sm ok" data-p="to-group" data-g="${esc(n.suggested_group.group)}">Move to ${esc(groupName(n.suggested_group.group))} <span class="dim">(wired to ${esc(n.suggested_group.via)})</span></button>` : ""}
          <div class="tv-acts"><label class="tv-btn sm" style="cursor:pointer"><input type="checkbox" data-set="locked" ${n.locked ? "checked" : ""} ${edit ? "" : "disabled"}> Lock position</label>
            <button type="button" class="tv-btn sm" data-p="icon" ${edit ? "" : "disabled"}>${ico("image", 14)} Picture…</button></div>
          ${edit ? "" : `<p class="tv-note">${opts.login ? `<button type="button" class="tv-btn sm" data-p="login">${ico("lock", 14)} Log in to change the diagram</button>` : "Read only."}</p>`}
        </div></div>`;
      return `<div class="hd">${itemIcon(n, 48, 42)}<div style="min-width:0"><h3>${esc(n.name)}</h3><p>${esc(kindOf(n.kind).label)} · ${esc(groupName(n.group))}</p>
          <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">${stateBadge(n)}${(n.problems || []).length ? `<span class="tv-b warn">⚠ ${n.problems.length} problem${n.problems.length > 1 ? "s" : ""}</span>` : ""}${n.locked ? `<span class="tv-b">🔒 Locked</span>` : ""}</div></div>
          <button type="button" class="x" data-close aria-label="Close">×</button></div>
        <div class="sec"><p class="tv-note" style="margin-bottom:8px">${esc(stateText(n))}</p><dl class="tv-kv">${kv.join("")}</dl></div>
        ${probs ? `<div class="sec"><h4>Problems</h4>${probs}</div>` : ""}${inferred}${radioSection(n.radio)}
        <div class="sec"><h4>Connections <span class="c">${links.length}</span></h4>${links.length ? `<div class="tv-list">${links.map((L) => linkItem(L, n.id)).join("")}</div>` : `<p class="tv-note">Not connected to anything yet. Use <b>Connect to…</b>${canEdit() ? " or drag the + handle on the diagram" : ""}.</p>`}</div>
        ${placement}
        <div class="sec"><div class="tv-acts">${acts.join("")}</div></div>`;
    }
    function linkPanel(L) {
      if (!L) return "";
      const st = LINK_STATUS[L.status] || LINK_STATUS.unknown;
      const end = (id, port) => {
        const n = S.idx.nodes[id] || { name: id, kind: "other", state: "online" };
        return `<div class="tv-item" role="button" tabindex="0" data-sel-node="${esc(id)}">${kindSvg(n.kind)}<span class="t">${esc(n.name)}</span><span class="r">${port ? `<span class="tv-b mono">${esc(port)}</span>` : ""}</span><span class="s">${esc([n.ip, groupName(n.group), (STATE[n.state] || {}).label].filter(Boolean).join(" · "))}</span></div>`;
      };
      const m = L.metrics || {};
      const mt = [];
      if (m.signal != null) mt.push(metric("Signal", dbm(m.signal), sigCls(m.signal)));
      if (m.remote_signal != null) mt.push(metric("Other end hears", dbm(m.remote_signal), sigCls(m.remote_signal)));
      if (m.score_dl != null) mt.push(metric("Link score ↓", Math.round(m.score_dl), m.score_dl < 50 ? "bad" : m.score_dl < 65 ? "warn" : "ok"));
      if (m.score_ul != null) mt.push(metric("Link score ↑", Math.round(m.score_ul), m.score_ul < 50 ? "bad" : m.score_ul < 65 ? "warn" : "ok"));
      if (m.distance) mt.push(metric("Distance", km(m.distance)));
      if (m.latency != null) mt.push(metric("Latency", Math.round(m.latency) + " ms"));
      if (m.tx != null) mt.push(metric("Rate ↑", Math.round(m.tx) + " Mb/s"));
      if (m.rx != null) mt.push(metric("Rate ↓", Math.round(m.rx) + " Mb/s"));
      const edit = canEdit();
      const acts = L.confirmed
        ? `<button type="button" class="tv-btn sm pri" data-p="edit-link" ${edit ? "" : "disabled"}>Edit</button><button type="button" class="tv-btn sm danger" data-p="delete-link" ${edit ? "" : "disabled"}>Delete</button>`
        : `<button type="button" class="tv-btn sm ok" data-p="accept" ${edit ? "" : "disabled"}>✓ Confirm connection</button><button type="button" class="tv-btn sm" data-p="dismiss" ${edit ? "" : "disabled"}>Dismiss</button>`;
      return `<div class="hd"><svg viewBox="0 0 24 24" width="40" height="40" aria-hidden="true">${MEDIUM[L.medium].icon}</svg><div style="min-width:0"><h3>${esc(MEDIUM[L.medium].label)} ${L.confirmed ? "connection" : "— suggested"}</h3>
          <p>${esc(L.label || (L.confirmed ? SOURCE[L.source] || "" : "Proven by the site's own readings — not on the diagram until you confirm it"))}</p>
          <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap"><span class="tv-b ${st[0]}">${esc(st[1])}</span>${L.confirmed ? "" : `<span class="tv-b warn">Suggested</span>`}</div></div>
          <button type="button" class="x" data-close aria-label="Close">×</button></div>
        <div class="sec"><h4>Between</h4><div class="tv-list">${end(L.a, L.a_port)}${end(L.b, L.b_port)}</div></div>
        ${mt.length ? `<div class="sec"><h4>What the radios report <span class="c">${esc(ago(m.ts))}</span></h4><div class="tv-metrics">${mt.join("")}</div></div>` : ""}
        ${L.evidence ? `<div class="sec"><h4>Why Netwatch knows <span class="c">${esc(SOURCE[L.source] || "")}</span></h4><p class="tv-note">${esc(L.evidence)}</p></div>` : ""}
        ${L.notes ? `<div class="sec"><h4>Notes</h4><p class="tv-note">${esc(L.notes).replace(/\n/g, "<br>")}</p></div>` : ""}
        ${L.status === "down" ? `<div class="sec"><p class="tv-note">One end of this connection is not answering — check the device marked offline first.</p></div>` : ""}
        <div class="sec"><div class="tv-acts">${acts}</div>${edit ? "" : opts.login ? `<p class="tv-note" style="margin-top:8px"><button type="button" class="tv-btn sm" data-p="login">${ico("lock", 14)} Log in to change it</button></p>` : ""}</div>`;
    }
    function groupLinks(gid) {
      const inside = [], outside = [];
      S.g.links.forEach((L) => {
        const ga = (S.idx.nodes[L.a] || {}).group, gb = (S.idx.nodes[L.b] || {}).group;
        if (ga === gid && gb === gid) inside.push(L);
        else if (ga === gid || gb === gid) outside.push(L);
      });
      return { inside, outside };
    }
    function groupPanel(gr) {
      if (!gr) return "";
      const members = (S.idx.byGroup[gr.id] || []).slice().sort((a, b) => kindOf(a.kind).tier - kindOf(b.kind).tier || a.name.localeCompare(b.name));
      const { inside, outside } = groupLinks(gr.id);
      const c = gr.counts || {};
      const edit = canEdit();
      const stTxt = !members.length ? ["", "Empty"] : { ok: ["ok", "✓ All online"], warn: ["warn", "! Needs a look"], down: ["bad", "✕ Down"], unknown: ["", "Not monitored"] }[gr.status] || ["", ""];
      const loc = gr.geo
        ? `<span class="mono">${gr.geo.lat.toFixed(6)}, ${gr.geo.lon.toFixed(6)}</span>${gr.geo_approx ? ` <span class="tv-b">approximate — middle of its devices' pins</span>` : ""}`
        : `<span class="dim">No location yet</span>`;
      const extItem = (L) => {
        const mine = (S.idx.nodes[L.a] || {}).group === gr.id ? L.a : L.b;
        const other = mine === L.a ? L.b : L.a;
        const o = S.idx.nodes[other] || { name: other, kind: "other", group: "~" };
        const st = LINK_STATUS[L.status] || LINK_STATUS.unknown;
        const bits = [nodeName(mine) + " → " + groupName(o.group)];
        if (L.metrics && L.metrics.signal != null) bits.push(Math.round(L.metrics.signal) + " dBm");
        if (L.metrics && L.metrics.distance) bits.push(km(L.metrics.distance));
        return `<button type="button" class="tv-item" data-sel-link="${esc(L.id)}">${kindSvg(o.kind)}<span class="t">${micon(L.medium, 13)} ${esc(o.name)}</span><span class="r">${L.confirmed ? `<span class="tv-b ${st[0]}">${esc(st[1])}</span>` : `<span class="tv-b warn">Suggested</span>`}</span><span class="s">${esc(bits.join(" · "))}</span></button>`;
      };
      const inItem = (L) => `<button type="button" class="tv-item" data-sel-link="${esc(L.id)}">${micon(L.medium, 22)}<span class="t">${esc(nodeName(L.a))} ↔ ${esc(nodeName(L.b))}</span><span class="r">${L.confirmed ? "" : `<span class="tv-b warn">Suggested</span>`}</span><span class="s">${esc([MEDIUM[L.medium].label, L.a_port && "port " + L.a_port, L.b_port && "→ " + L.b_port, L.label].filter(Boolean).join(" · "))}</span></button>`;
      const acts = [
        `<button type="button" class="tv-btn sm pri" data-p="edit-group" ${edit ? "" : "disabled"}>Edit</button>`,
        S.view === "map" ? `<button type="button" class="tv-btn sm" data-p="show-diagram">${ico("diagram", 14)} Show in diagram</button>` : `<button type="button" class="tv-btn sm" data-p="show-map">${ico("map", 14)} ${gr.geo ? "Show on map" : "Place on map"}</button>`,
        `<button type="button" class="tv-btn sm" data-p="toggle">${gr.collapsed ? "Expand" : "Collapse"}</button>`,
        `<button type="button" class="tv-btn sm" data-p="lock-group" ${edit ? "" : "disabled"}>${gr.locked ? "Unlock" : "Lock"} layout</button>`,
        `<button type="button" class="tv-btn sm" data-p="arrange-group" ${edit && !gr.locked && !locked() ? "" : "disabled"}>${ico("wand", 14)} Tidy inside</button>`,
        `<button type="button" class="tv-btn sm" data-p="add-here" ${edit ? "" : "disabled"}>${ico("plus", 14)} Equipment here</button>`,
        gr.geo && !gr.geo_approx ? `<a class="tv-btn sm" href="https://www.google.com/maps/dir/?api=1&destination=${(+gr.geo.lat).toFixed(6)},${(+gr.geo.lon).toFixed(6)}" target="_blank" rel="noopener">Directions ↗</a>` : "",
        `<button type="button" class="tv-btn sm danger" data-p="delete-group" ${edit ? "" : "disabled"}>Delete</button>`,
      ];
      return `<div class="hd"><span style="width:44px;height:44px;border-radius:12px;background:#0f172a;display:grid;place-items:center;flex:none">${gicon(gr.kind, 30)}</span><div style="min-width:0"><h3>${esc(gr.name)}</h3>
          <p>${esc((S.g.group_kinds || {})[gr.kind] || "Group")} · ${members.length} item${members.length === 1 ? "" : "s"}</p>
          <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap"><span class="tv-b ${stTxt[0]}">${esc(stTxt[1])}</span>${gr.locked ? `<span class="tv-b">🔒 Layout locked</span>` : ""}</div></div>
          <button type="button" class="x" data-close aria-label="Close">×</button></div>
        <div class="sec">${gr.description ? `<p class="tv-note" style="margin-bottom:8px;color:#cbd5e1">${esc(gr.description).replace(/\n/g, "<br>")}</p>` : ""}
          <dl class="tv-kv"><dt>Location</dt><dd>${loc}</dd></dl></div>
        <div class="sec"><h4>Monitoring</h4><div class="tv-metrics">
          ${metric("Online", `${c.online || 0}<span class="dim" style="font-size:12px"> / ${(c.total || 0) - (c.unmonitored || 0)}</span>`, c.online && c.online === (c.total - c.unmonitored) ? "ok" : "")}
          ${metric("Offline", (c.offline || 0) + (c.quiet || 0), (c.offline || c.quiet) ? "bad" : "")}
          ${metric("Not monitored", c.unmonitored || 0)}
          ${metric("Problems", c.problems || 0, c.problems ? "warn" : "")}</div></div>
        ${members.length ? `<div class="sec"><details ${S.miniOpen === false ? "" : "open"} data-mini-toggle><summary class="tv-note" style="cursor:pointer;margin-bottom:8px">Layout inside ${esc(gr.name)}</summary><div class="tv-mini" data-mini="${esc(gr.id)}"></div></details></div>` : ""}
        <div class="sec"><h4>Equipment <span class="c">${members.length}</span></h4>${members.length ? `<div class="tv-list">${members.map((n) => nodeItem(n, !n.virtual && opts.openDevice ? `<button type="button" class="tv-btn sm" data-p="open" data-id="${esc(n.id)}" title="Open the device window">Open</button>` : "")).join("")}</div>` : `<p class="tv-note">Nothing here yet — drag equipment onto this ${esc(((S.g.group_kinds || {})[gr.kind] || "group").toLowerCase())} in the diagram, or pick it as the group in a device's panel.</p>`}</div>
        <div class="sec"><h4>Connections inside <span class="c">${inside.length}</span></h4>${inside.length ? `<div class="tv-list">${inside.map(inItem).join("")}</div>` : `<p class="tv-note">None yet.</p>`}</div>
        <div class="sec"><h4>Links to other places <span class="c">${outside.length}</span></h4>${outside.length ? `<div class="tv-list">${outside.map(extItem).join("")}</div>` : `<p class="tv-note">No links out of this group yet.</p>`}</div>
        <div class="sec"><div class="tv-acts">${acts.join("")}</div></div>`;
    }
    function reviewPanel() {
      const sugg = S.g.suggestions;
      const links = sugg.filter((s) => s.type === "link");
      const shared = sugg.filter((s) => s.type === "shared_port");
      const ua = (S.idx.byGroup["~"] || []).slice().sort((a, b) => a.name.localeCompare(b.name));
      const edit = canEdit();
      const dis = edit ? "" : "disabled";
      const sl = links.map((s) => `<div class="tv-sug"><div class="t">${micon(s.medium, 14)} ${esc(nodeName(s.a))}${s.a_port ? ` <span class="mono dim">[${esc(s.a_port)}]</span>` : ""} ↔ ${esc(nodeName(s.b))}</div>
          <p>${esc(s.evidence)}</p>
          <div class="tv-acts"><button type="button" class="tv-btn sm ok" data-r="accept" data-id="${esc(s.id)}" ${dis}>✓ Confirm</button><button type="button" class="tv-btn sm" data-r="dismiss" data-id="${esc(s.id)}" ${dis}>Dismiss</button><button type="button" class="tv-btn sm" data-sel-link="${esc(s.id)}">Show</button></div></div>`).join("");
      const sp = shared.map((s) => `<div class="tv-sug"><div class="t">${ico("tag", 14)} ${esc(nodeName(s.a))} port ${esc(String(s.a_port).split("/").pop())}: ${s.members.length + (s.unknown || 0)} devices</div>
          <p>${esc(s.evidence)}</p><p>${s.members.map((k) => esc(nodeName(k))).join(", ")}${s.unknown ? ` + ${s.unknown} not on the diagram` : ""}</p>
          <div class="tv-acts"><button type="button" class="tv-btn sm ok" data-r="insert" data-id="${esc(s.id)}" ${dis}>Add the switch between…</button><button type="button" class="tv-btn sm" data-r="dismiss" data-id="${esc(s.id)}" ${dis}>Dismiss</button></div></div>`).join("");
      const uaItems = ua.map((n) => nodeItem(n, n.suggested_group
        ? `<button type="button" class="tv-btn sm ok" data-r="to-group" data-id="${esc(n.id)}" data-g="${esc(n.suggested_group.group)}" title="Wired to ${esc(n.suggested_group.via)}" ${dis}>→ ${esc(trunc(groupName(n.suggested_group.group), 14))}</button>` : "")).join("");
      const q = (S.otherQ || "").toLowerCase();
      const others = S.g.others.filter((o) => !q || [o.name, o.ip].join(" ").toLowerCase().includes(q));
      const oItems = others.slice(0, 60).map((o) => `<div class="tv-item">${kindSvg(o.kind)}<span class="t">${esc(o.name)}</span><span class="r"><button type="button" class="tv-btn sm" data-r="show" data-id="${esc(o.id)}" ${dis}>Add</button></span><span class="s">${esc([o.ip, kindOf(o.kind).label, (STATE[o.state] || {}).label].filter(Boolean).join(" · "))}</span></div>`).join("");
      const hItems = S.g.hidden.map((o) => `<div class="tv-item">${kindSvg(o.kind)}<span class="t">${esc(o.name)}</span><span class="r"><button type="button" class="tv-btn sm" data-r="unhide" data-id="${esc(o.id)}" ${dis}>Show again</button></span><span class="s">${esc(o.ip)}</span></div>`).join("");
      const suggested = ua.filter((n) => n.suggested_group).length;
      return `<div class="hd">${ico("review", 30)}<div><h3>Review</h3><p>What the site's readings prove, and equipment that still needs a place.</p></div><button type="button" class="x" data-close aria-label="Close">×</button></div>
        ${edit ? "" : `<div class="sec"><p class="tv-note">${opts.login ? `<button type="button" class="tv-btn sm" data-p="login">${ico("lock", 14)} Log in to make changes</button>` : "Read only."}</p></div>`}
        <div class="sec"><h4>Suggested connections <span class="c">${links.length}</span></h4>
          ${links.length ? `${links.length > 1 ? `<div class="tv-acts" style="margin-bottom:8px"><button type="button" class="tv-btn sm ok" data-r="accept-all" ${dis}>✓ Confirm all ${links.length}</button></div>` : ""}${sl}`
          : `<p class="tv-note">Nothing new. Connections are only suggested from a switch or router port with one device on it, or a radio registered on an access point — never from a shared subnet or location.</p>`}</div>
        ${shared.length ? `<div class="sec"><h4>Ports with several devices <span class="c">${shared.length}</span></h4>${sp}</div>` : ""}
        <div class="sec"><h4>Not in a group yet <span class="c">${ua.length}</span></h4>
          ${ua.length ? `${suggested > 1 ? `<div class="tv-acts" style="margin-bottom:6px"><button type="button" class="tv-btn sm ok" data-r="to-group-all" ${dis}>Move ${suggested} to their wired group</button></div>` : ""}<div class="tv-list">${uaItems}</div>` : `<p class="tv-note">Everything on the diagram belongs to a tower or site.</p>`}</div>
        <div class="sec"><h4>Other devices on this network <span class="c">${S.g.others.length}</span></h4>
          <p class="tv-note" style="margin-bottom:6px">Phones, computers and unidentified devices stay off the diagram until you add them.</p>
          ${S.g.others.length > 8 ? `<input class="tv-inp" data-other-q placeholder="Filter…" value="${esc(S.otherQ || "")}" style="margin-bottom:6px">` : ""}
          <div class="tv-list">${oItems}</div>${others.length > 60 ? `<p class="tv-note">${others.length - 60} more — filter to find them.</p>` : ""}</div>
        ${S.g.hidden.length ? `<div class="sec"><h4>Hidden from the diagram <span class="c">${S.g.hidden.length}</span></h4><div class="tv-list">${hItems}</div></div>` : ""}
        ${S.g.dismissed ? `<div class="sec"><p class="tv-note">${S.g.dismissed} dismissed suggestion${S.g.dismissed > 1 ? "s" : ""}. <button type="button" class="tv-btn sm" data-r="undismiss" ${dis}>Bring them back</button></p></div>` : ""}
        ${S.g.orphan_links ? `<div class="sec"><p class="tv-note">${S.g.orphan_links} saved connection${S.g.orphan_links > 1 ? "s point" : " points"} at devices that are not on the diagram right now (forgotten, pruned or hidden). They come back if the device does. <button type="button" class="tv-btn sm danger" data-r="orphans" ${dis}>Remove the ones whose device is gone</button></p></div>` : ""}`;
    }
    function overviewPanel() {
      const n = S.g.nodes, bad = n.filter((x) => x.state === "offline" || (x.problems || []).length || (x.inferred && x.inferred.state === "suspect"));
      const confirmed = S.g.links.filter((L) => L.confirmed).length;
      return `${S.g.store_error ? `<div class="sec"><div class="tv-sug" style="border-color:rgba(251,113,133,.5)"><div class="t">⚠ The diagram file could not be read</div><p>${esc(S.g.store_error)}. Nothing is saved until it is fixed on the Pi.</p></div></div>` : ""}
        <div class="hd">${ico("info", 28)}<div><h3>${esc((S.g.site && S.g.site.name) || "This site")}</h3><p>${S.g.groups.length} group${S.g.groups.length === 1 ? "" : "s"} · ${n.length} items · ${confirmed} connection${confirmed === 1 ? "" : "s"}</p></div></div>
        ${bad.length ? `<div class="sec"><h4>Needs a look <span class="c">${bad.length}</span></h4><div class="tv-list">${bad.slice(0, 12).map((x) => nodeItem(x)).join("")}</div></div>` : `<div class="sec"><p class="tv-note">✓ Everything that is monitored answers.</p></div>`}
        ${reviewCta()}
        <div class="sec"><h4>How it works</h4><p class="tv-note">
          • <b>Groups</b> are towers, sites or buildings. Drag equipment onto one to put it there; the ▾ button collapses it to a summary, and the corner grip makes the box bigger.<br>
          • <b>Select several</b>: drag a box around them on empty space, or shift-click. Hold the <b>space bar</b> (or use the hand button) to move the diagram instead.<br>
          • <b>Connect</b>: drag the <b>+</b> handle of one item onto another, or use Connect. Wireless links join two radios.<br>
          • <b>Equipment</b> adds things Netwatch cannot see, like an unmanaged switch — no IP needed.<br>
          • Dotted lines are <b>suggested</b> from switch, router and radio readings; confirm or dismiss them.<br>
          • Each item can get its own picture — open it and choose <b>Picture</b>; <b>Icons</b> sets one for a whole type.<br>
          • Positions are saved as you drag. <b>Lock layout</b> stops accidental moves; <b>Auto-arrange</b> tidies what is not locked.<br>
          • Moving things here never changes their GPS position on the map.</p></div>`;
    }
    function reviewCta() {
      const sug = S.g.suggestions.length, ua = (S.idx.byGroup["~"] || []).length;
      if (!sug && !ua) return "";
      const bits = [];
      if (sug) bits.push(`${sug} suggested connection${sug === 1 ? "" : "s"}`);
      if (ua) bits.push(`${ua} not in a group yet`);
      return `<div class="sec"><button type="button" class="tv-btn ok" data-a="review" style="white-space:normal;text-align:left;height:auto;padding:8px 12px">${ico("review")}<span>Review: ${esc(bits.join(" · "))}</span></button>
        ${ua && !S.g.groups.length ? `<p class="tv-note" style="margin-top:8px">Start with <b>+ Group</b> for each tower or site, then drag the equipment into it.</p>` : ""}</div>`;
    }
    function drawMini(el, gid) {
      const members = S.idx.byGroup[gid] || [];
      if (!members.length) { el.innerHTML = `<div class="tv-empty" style="position:static;height:100%"><div>Empty</div></div>`; return; }
      const g = G();
      const pos = (n) => n.pos || { x: g.pad_x, y: g.pad_top };
      const ids = new Set(members.map((n) => n.id));
      const xs = members.map((n) => pos(n).x), ys = members.map((n) => pos(n).y);
      const x0 = Math.min(...xs) - 10, y0 = Math.min(...ys) - 10;
      const x1 = Math.max(...xs) + CW() + 10, y1 = Math.max(...ys) + CH() + 10;
      const ends = {};
      members.forEach((n) => { const p = pos(n); ends[n.id] = { key: n.id, x: p.x, y: p.y, w: CW(), h: CH(), node: true }; });
      const edges = S.g.links.filter((L) => ids.has(L.a) && ids.has(L.b)).map((L) => {
        const c = curve(ends[L.a], ends[L.b], 0);
        const cls = `tv-e ${MEDIUM[L.medium].cls} ${L.status === "down" ? "down" : ""} ${L.confirmed ? "" : "sug"}`;
        return `<path class="${cls}" d="${c.d}"/>${L.medium === "fibre" ? `<path class="tv-e fib2" d="${c.d}"/>` : ""}`;
      }).join("");
      el.innerHTML = `<svg class="tv-svg" viewBox="${x0} ${y0} ${x1 - x0} ${y1 - y0}" preserveAspectRatio="xMidYMid meet" style="cursor:default" role="img" aria-label="Layout of the group">
        ${edges}${members.map((n) => nodeSvg(n, pos(n), null, null, true)).join("")}</svg>`;
    }

    // ---- side panel wiring ---------------------------------------------------------------------
    const enc = encodeURIComponent;
    let sideKey = "";
    function drawSide() {
      if (!S.g) return;
      const key = S.sel ? S.sel.t + ":" + S.sel.id : S.panel || "overview";
      const keep = key === sideKey ? side.scrollTop : 0;
      const active = document.activeElement;
      const typing = active && side.contains(active) && active.matches("[data-other-q]");
      let html = "";
      if (S.sel && S.sel.t === "node") html = nodePanel(S.idx.nodes[S.sel.id]);
      else if (S.sel && S.sel.t === "link") html = linkPanel(S.idx.links[S.sel.id]);
      else if (S.sel && S.sel.t === "group") html = groupPanel(S.idx.groups[S.sel.id]);
      else if (S.panel === "review" || (S.sel && S.sel.t === "ua")) html = reviewPanel();
      else html = overviewPanel();
      side.innerHTML = html;
      sideKey = key;
      side.scrollTop = keep;
      const mini = side.querySelector("[data-mini]");
      if (mini) drawMini(mini, mini.dataset.mini);
      if (typing) {
        const inp = side.querySelector("[data-other-q]");
        if (inp) { inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); }
      }
      tv.classList.toggle("side-off", !S.sel && S.panel !== "review" && window.innerWidth <= 1100);
    }
    side.addEventListener("click", (e) => {
      const t = e.target;
      if (t.closest("[data-close]")) { S.sel = null; S.panel = null; S.multi.clear(); render(); return; }
      const pb = t.closest("[data-p]");
      if (pb) { panelAction(pb); return; }
      const rb = t.closest("[data-r]");
      if (rb) { reviewAction(rb); return; }
      const ab = t.closest("[data-a]");
      if (ab) return;                       // handled by the toolbar listener on root
      const mn = t.closest("[data-mini] [data-node]");
      if (mn) { focus(mn.dataset.node); return; }
      const sl = t.closest("[data-sel-link]");
      if (sl) { selectLink(sl.dataset.selLink); return; }
      const sn = t.closest("[data-sel-node]");
      if (sn) focus(sn.dataset.selNode);
    });
    side.addEventListener("keydown", (e) => {
      if ((e.key === "Enter" || e.key === " ") && e.target.matches("[data-sel-node]")) { e.preventDefault(); focus(e.target.dataset.selNode); }
    });
    side.addEventListener("change", (e) => { const s = e.target.closest("[data-set]"); if (s) nodeSetting(s); });
    side.addEventListener("input", (e) => { if (e.target.matches("[data-other-q]")) { S.otherQ = e.target.value; drawSide(); } });
    side.addEventListener("toggle", (e) => { if (e.target.matches && e.target.matches("[data-mini-toggle]")) S.miniOpen = e.target.open; }, true);

    function selectLink(id) {
      const L = S.idx.links[id];
      if (!L) return;
      S.sel = { t: "link", id };
      S.panel = null;
      render();
      if (S.view === "diagram") {
        const A = endOf(L.a), B = endOf(L.b);
        if (A && B) { const a = anchors(A), b = anchors(B); centerOn((a.cx + b.cx) / 2, (a.cy + b.cy) / 2); }
      } else mapFocusLink(L);
    }
    async function send(method, path, body) {
      const res = await opts.call(method, graphQuery(path), body);
      if (res && res.graph) setGraph(res.graph);
      return res || {};
    }
    async function panelAction(b) {
      const p = b.dataset.p, sel = S.sel || {};
      const n = sel.t === "node" ? S.idx.nodes[sel.id] : null;
      const L = sel.t === "link" ? S.idx.links[sel.id] : null;
      const gr = sel.t === "group" ? S.idx.groups[sel.id] : null;
      const quiet = () => {};
      switch (p) {
        case "login":
          if (opts.login && await opts.login()) refresh(true);
          return;
        case "open": return opts.openDevice && opts.openDevice(b.dataset.id || (n && n.id));
        case "edit": return needEdit(() => equipmentDialog(n));
        case "connect":
          return needEdit(() => {
            if (S.view === "diagram") { S.connect = { from: n.id }; render(); toast("Now click the other end — Esc cancels"); }
            else linkDialog({ a: n.id });
          });
        case "show-map": { const id = (n || gr).id; setView("map"); return focus(id); }
        case "show-diagram": { const id = (n || gr).id; setView("diagram"); return focus(id); }
        case "hide": return needEdit(() => act("POST", `/nodes/${enc(n.id)}`, { hidden: true }, { okMsg: `${n.name} hidden — bring it back from Review` }).then(() => { S.sel = null; render(); }, quiet));
        case "replace": {
          const d = S.idx.nodes[b.dataset.dev];
          if (!(await confirmBox(`Use ${d.name} instead?`, `The scanner found this equipment. ${d.name} takes over the place, group and connections of “${n.name}”, and the hand-added entry is removed.`, { ok: "Use discovered device" }))) return;
          return needEdit(() => act("POST", `/equipment/${enc(n.id)}/replace`, { device: d.id }, { okMsg: `${d.name} now stands in for ${n.name}` }).then(() => { S.sel = { t: "node", id: d.id }; render(); }, quiet));
        }
        case "delete": return needEdit(() => deleteEquipment(n));
        case "to-group": return needEdit(() => act("POST", `/nodes/${enc(n.id)}`, { group: b.dataset.g }, { okMsg: `${n.name} moved to ${groupName(b.dataset.g)}` }).catch(quiet));
        case "icon": return needEdit(() => iconDialog(n));
        case "edit-link": return needEdit(() => linkDialog(L));
        case "delete-link": return needEdit(deleteSelected);
        case "accept":
          return needEdit(() => act("POST", `/suggestions/${enc(L.id)}`, { action: "accept" }, { okMsg: "Connection confirmed" })
            .then((res) => { if (res.result && res.result.id) { S.sel = { t: "link", id: res.result.id }; render(); } }, quiet));
        case "dismiss": return needEdit(() => act("POST", `/suggestions/${enc(L.id)}`, { action: "dismiss" }, { okMsg: "Suggestion dismissed" }).then(() => { S.sel = null; render(); }, quiet));
        case "edit-group": return needEdit(() => groupDialog(gr));
        case "toggle": return toggleGroup(gr.id);
        case "lock-group": return needEdit(() => act("POST", `/groups/${enc(gr.id)}`, { locked: !gr.locked }, { okMsg: gr.locked ? `${gr.name} unlocked` : `${gr.name} locked — Auto-arrange leaves it alone` }).catch(quiet));
        case "arrange-group": return needEdit(() => act("POST", S.scope === "all" ? "/arrange?scope=all" : "/arrange", { scope: gr.id }, { graph: false, okMsg: `${gr.name} tidied` }).catch(quiet));
        case "add-here": return needEdit(() => equipmentDialog(null, gr.id));
        case "delete-group": {
          const cnt = (S.idx.byGroup[gr.id] || []).length;
          if (!(await confirmBox(`Delete ${gr.name}?`, cnt ? `Its ${cnt} item${cnt > 1 ? "s are" : " is"} not deleted — they move to “Not in a group yet”.` : "The group is empty.", { danger: true, ok: "Delete group" }))) return;
          return needEdit(() => act("DELETE", `/groups/${enc(gr.id)}`, undefined, { okMsg: `${gr.name} deleted` }).then(() => { S.sel = null; render(); }, quiet));
        }
      }
    }
    async function nodeSetting(el) {
      const n = S.sel && S.idx.nodes[S.sel.id];
      if (!n) return;
      const what = el.dataset.set;
      const body = what === "group" ? { group: el.value } : what === "kind" ? { kind: el.value } : { locked: el.checked };
      const msg = what === "group" ? `${n.name} moved to ${groupName(el.value)}` : what === "kind" ? "Equipment type saved" : el.checked ? "Position locked" : "Position unlocked";
      await needEdit(() => act("POST", `/nodes/${enc(n.id)}`, body, { okMsg: msg })).catch(() => refresh(true));
    }
    async function reviewAction(b) {
      const r = b.dataset.r, id = b.dataset.id;
      const quiet = () => {};
      if (r === "accept") return needEdit(() => act("POST", `/suggestions/${enc(id)}`, { action: "accept" }, { okMsg: "Connection confirmed" }).catch(quiet));
      if (r === "dismiss") return needEdit(() => act("POST", `/suggestions/${enc(id)}`, { action: "dismiss" }, { okMsg: "Dismissed — Review can bring it back" }).catch(quiet));
      if (r === "accept-all") {
        const n = S.g.suggestions.filter((s) => s.type === "link").length;
        if (!(await confirmBox(`Confirm all ${n} suggested connections?`, "Each one is proven by a switch or router port table or a radio's station table. You can still delete any of them afterwards.", { ok: `Confirm ${n}` }))) return;
        return needEdit(() => act("POST", "/suggestions/accept-all", {}).then((res) => {
          const x = res.result || {};
          toast(`${x.accepted || 0} connection${x.accepted === 1 ? "" : "s"} confirmed` + ((x.failed || []).length ? ` · ${x.failed.length} could not be: ${x.failed[0].error}` : ""), (x.failed || []).length ? "" : "ok");
        }, quiet));
      }
      if (r === "insert") { const s = S.g.suggestions.find((x) => x.id === id); return s && needEdit(() => insertDialog(s)); }
      if (r === "to-group") return needEdit(() => act("POST", `/nodes/${enc(id)}`, { group: b.dataset.g }, { okMsg: `${nodeName(id)} moved to ${groupName(b.dataset.g)}` }).catch(quiet));
      if (r === "to-group-all") {
        const body = { nodes: {} };
        (S.idx.byGroup["~"] || []).filter((n) => n.suggested_group).forEach((n) => (body.nodes[n.id] = { group: n.suggested_group.group }));
        return needEdit(() => act("POST", "/layout", body, { okMsg: `${Object.keys(body.nodes).length} items moved into their groups` }).catch(quiet));
      }
      if (r === "show") return needEdit(() => act("POST", `/nodes/${enc(id)}`, { show: true }, { okMsg: `${(S.g.others.find((o) => o.id === id) || {}).name || "Device"} added — it waits under “Not in a group yet”` }).catch(quiet));
      if (r === "unhide") return needEdit(() => act("POST", `/nodes/${enc(id)}`, { hidden: false }, { okMsg: "Back on the diagram" }).catch(quiet));
      if (r === "undismiss") return needEdit(() => act("POST", "/dismissed/clear", {}, { okMsg: "Dismissed suggestions are back" }).catch(quiet));
      if (r === "orphans") {
        if (!(await confirmBox("Remove connections to devices that are gone?", "Saved connections and diagram places of devices that no longer exist on this site are deleted. Hidden devices keep theirs.", { danger: true, ok: "Remove" }))) return;
        return needEdit(() => act("POST", "/orphans/clear", {}).then((res) => {
          const x = res.result || {};
          toast(`${x.links_removed || 0} connection${x.links_removed === 1 ? "" : "s"} removed`, "ok");
        }, quiet));
      }
    }

    // ---- dialogs ---------------------------------------------------------------------------------
    const dialogs = new Set();
    function dialog({ title, sub = "", body, ok = "Save", wide = false, onOk, extra = "", danger = false }) {
      const wrap = document.createElement("div");
      wrap.className = "tv-dlg tv";
      wrap.innerHTML = `<form class="card ${wide ? "wide" : ""}" role="dialog" aria-modal="true" aria-label="${esc(title)}" novalidate>
        <div class="dh"><div><h2>${esc(title)}</h2>${sub ? `<p>${sub}</p>` : ""}</div></div>
        <div class="db">${body}<div class="err" role="alert"></div></div>
        <div class="df">${extra}<span style="flex:1"></span><button type="button" class="tv-btn" data-x>${onOk ? "Cancel" : "Close"}</button>${onOk ? `<button type="submit" class="tv-btn ${danger ? "danger" : "pri"}">${esc(ok)}</button>` : ""}</div></form>`;
      document.body.appendChild(wrap);
      const form = wrap.querySelector("form"), err = wrap.querySelector(".err");
      const prev = document.activeElement;
      const onKey = (e) => { if (e.key === "Escape" && !document.getElementById("nw-auth")) { e.stopPropagation(); close(); } };
      function close() {
        wrap.remove();
        dialogs.delete(close);
        document.removeEventListener("keydown", onKey, true);
        if (prev && prev.focus && document.contains(prev)) prev.focus({ preventScroll: true });
      }
      dialogs.add(close);
      document.addEventListener("keydown", onKey, true);
      wrap.addEventListener("mousedown", (e) => { if (e.target === wrap) close(); });
      wrap.querySelector("[data-x]").onclick = close;
      form.onsubmit = async (e) => {
        e.preventDefault();
        if (!onOk) return;
        const btn = form.querySelector("[type=submit]");
        btn.disabled = true;
        err.textContent = "";
        try {
          if ((await onOk(form, err)) !== false) close();
        } catch (x) {
          err.textContent = x.message || "That did not work";
        } finally {
          btn.disabled = false;
        }
      };
      setTimeout(() => { const f = form.querySelector("input:not([type=hidden]):not([disabled]),select,textarea"); if (f) f.focus(); }, 30);
      return { wrap, form, close, err };
    }
    /** A free spot near the middle of the view: rings outward, above/below before sideways. */
    function freeSpot(w, h) {
      const r = svg.getBoundingClientRect();
      const c = toWorld(r.left + r.width / 2, r.top + r.height / 2);
      const x0 = Math.round((c.x - w / 2) / 10) * 10, y0 = Math.round((c.y - h / 2) / 10) * 10;
      const hit = (x, y) => Object.values(S.boxes).some((b) => !(x + w + 30 <= b.x || b.x + b.w + 30 <= x || y + h + 30 <= b.y || b.y + b.h + 30 <= y));
      if (!hit(x0, y0)) return { x: x0, y: y0 };
      for (let ring = 1; ring < 60; ring++) {
        for (const [dx, dy] of [[0, -1], [0, 1], [-1, 0], [1, 0], [-1, -1], [1, -1], [-1, 1], [1, 1]]) {
          const x = x0 + dx * ring * (w + 40), y = y0 + dy * ring * (h + 40);
          if (!hit(x, y)) return { x, y };
        }
      }
      return { x: x0, y: y0 };
    }
    function groupDialog(gr) {
      const kinds = Object.entries(S.g.group_kinds || {}).map(([k, l]) => `<option value="${esc(k)}" ${(gr ? gr.kind : "tower") === k ? "selected" : ""}>${esc(l)}</option>`).join("");
      const loc = gr && gr.lat != null ? `${gr.lat}, ${gr.lon}` : "";
      let pickAfter = false;
      const d = dialog({
        title: gr ? `Edit ${gr.name}` : "Add a group",
        sub: "A tower, site or building. Its equipment is drawn together on the diagram and shown as one marker on the map.",
        body: `<div class="two"><label class="tv-field">Name *<input class="tv-inp" name="name" required maxlength="80" value="${esc(gr ? gr.name : "")}" placeholder="e.g. Tower A, Farm office"></label>
            <label class="tv-field">Kind<select class="tv-inp" name="kind">${kinds}</select></label></div>
          <label class="tv-field">Description<textarea class="tv-inp" name="description" rows="3" maxlength="600" placeholder="What is here, how to get there, who has the key…">${esc(gr ? gr.description || "" : "")}</textarea></label>
          <label class="tv-field">Location (optional)<input class="tv-inp mono" name="loc" value="${esc(loc)}" placeholder="-33.924868, 18.424055 or a Google Maps link"></label>
          <p class="hint">Leave it empty to place it on the map later${gr && gr.geo_approx ? " — until then the map uses the middle of its devices' pins" : ""}.</p>`,
        ok: gr ? "Save" : "Add group",
        extra: `<button type="button" class="tv-btn sm" data-pick>${ico("map", 14)} ${gr ? "Pick on the map" : "Add, then pick on the map"}</button>`,
        async onOk(f) {
          const name = f.name.value.trim();
          if (!name) throw new Error("Give the group a name");
          const body = { name, kind: f.kind.value, description: f.description.value };
          const raw = f.loc.value.trim();
          if (raw) {
            const g = parseLatLon(raw);
            if (!g) throw new Error("That is not a position — use e.g. -33.924868, 18.424055 or a Google Maps link");
            body.lat = g.lat; body.lon = g.lon;
          } else if (gr && gr.lat != null) body.clear_location = true;
          if (gr) {
            await send("POST", `/groups/${enc(gr.id)}`, body);
            toast("Saved", "ok");
            return gr.id;
          }
          if (S.view === "diagram") { computeBoxes(); body.pos = freeSpot(260, 124); }
          const res = await send("POST", "/groups", body);
          S.sel = { t: "group", id: res.result.id };
          S.panel = null;
          render();
          if (S.view === "diagram" && S.boxes[res.result.id]) {
            const b = S.boxes[res.result.id];
            centerOn(b.x + b.w / 2, b.y + b.h / 2);
            drawDiagram();
          }
          toast(`${name} added — drag equipment into it`, "ok");
          if (pickAfter) setTimeout(() => startPlacing(res.result.id), 60);
          return res.result.id;
        },
      });
      d.wrap.querySelector("[data-pick]").onclick = () => {
        if (gr) { d.close(); startPlacing(gr.id); return; }
        pickAfter = true;
        d.form.requestSubmit();
      };
    }
    function equipmentDialog(n, gid) {
      const kinds = kindOptionList(n ? n.kind : "unmanaged-switch");
      const cur = n ? n.group : gid || (S.sel && S.sel.t === "group" ? S.sel.id : "~");
      dialog({
        title: n ? `Edit ${n.name}` : "Add equipment",
        sub: "For equipment Netwatch cannot find by itself — an unmanaged switch, a PoE injector, a fibre converter, a radio without a login. It is shown as <b>Not monitored</b>, never as offline.",
        body: `<div class="two"><label class="tv-field">Name *<input class="tv-inp" name="name" required maxlength="80" value="${esc(n ? n.name : "")}" placeholder="e.g. Gate PoE switch"></label>
            <label class="tv-field">Type<select class="tv-inp" name="kind">${kinds}</select></label></div>
          <div class="two"><label class="tv-field">Group<select class="tv-inp" name="group">${groupOptions(cur)}</select></label>
            <label class="tv-field">Model<input class="tv-inp" name="model" maxlength="80" value="${esc(n ? n.model : "")}" placeholder="optional"></label></div>
          <div class="two"><label class="tv-field">Ports<input class="tv-inp" name="ports" type="number" min="0" max="512" inputmode="numeric" value="${n && n.ports != null ? esc(n.ports) : ""}" placeholder="optional"></label>
            <label class="tv-field">Port notes<input class="tv-inp" name="port_notes" maxlength="300" value="${esc(n ? n.port_notes : "")}" placeholder="e.g. 1 uplink, 2–5 cameras"></label></div>
          <div class="two"><label class="tv-field">IP address<input class="tv-inp mono" name="ip" value="${esc(n ? n.ip : "")}" placeholder="optional — not needed"></label>
            <label class="tv-field">MAC address<input class="tv-inp mono" name="mac" value="${esc(n ? n.mac : "")}" placeholder="optional"></label></div>
          <label class="tv-field">Notes<textarea class="tv-inp" name="notes" rows="3" maxlength="1000" placeholder="Where it is mounted, what powers it…">${esc(n ? n.notes : "")}</textarea></label>`,
        ok: n ? "Save" : "Add equipment",
        async onOk(f) {
          const body = {};
          ["name", "kind", "group", "model", "ports", "port_notes", "ip", "mac", "notes"].forEach((k) => (body[k] = f[k].value));
          if (!body.name.trim()) throw new Error("Give the equipment a name");
          if (n) {
            await send("POST", `/equipment/${enc(n.id)}`, body);
            toast("Saved", "ok");
          } else {
            const res = await send("POST", "/equipment", body);
            S.sel = { t: "node", id: res.result.id };
            S.panel = null;
            render();
            toast(`${body.name.trim()} added — connect it with Connect to…`, "ok");
          }
        },
      });
    }
    function nodeSelect(name, cur, disabled) {
      const byG = {};
      S.g.nodes.forEach((n) => (byG[n.group] = byG[n.group] || []).push(n));
      const order = S.g.groups.slice().sort((a, b) => a.name.localeCompare(b.name)).map((g) => g.id).concat(["~"]);
      return `<select class="tv-inp" name="${name}" ${disabled ? "disabled" : ""} required><option value="">Choose…</option>${order.filter((g) => byG[g]).map((g) =>
        `<optgroup label="${esc(groupName(g))}">${byG[g].sort((a, b) => a.name.localeCompare(b.name)).map((n) =>
          `<option value="${esc(n.id)}" ${cur === n.id ? "selected" : ""}>${esc(n.name)}${n.ip ? " · " + esc(n.ip) : ""} (${esc(kindOf(n.kind).label)})</option>`).join("")}</optgroup>`).join("")}</select>`;
    }
    function linkDialog(L) {
      const editing = !!(L && L.id);
      const med = (L && L.medium) || "ethernet";
      const d = dialog({
        title: editing ? "Edit connection" : "Connect equipment",
        sub: editing ? "" : "Pick the two ends. A wireless link joins the two radios themselves — connect a camera to the radio or switch it is wired to.",
        body: `<div class="two"><label class="tv-field">From *${nodeSelect("a", L && L.a, editing)}</label>
            <label class="tv-field">Port <input class="tv-inp mono" name="a_port" maxlength="24" value="${esc((L && L.a_port) || "")}" placeholder="optional, e.g. 5, eth0, SFP1"></label></div>
          <div class="two"><label class="tv-field">To *${nodeSelect("b", L && L.b, editing)}</label>
            <label class="tv-field">Port <input class="tv-inp mono" name="b_port" maxlength="24" value="${esc((L && L.b_port) || "")}" placeholder="optional"></label></div>
          <div class="two"><label class="tv-field">Type<select class="tv-inp" name="medium">${Object.entries(MEDIUM).map(([k, m]) => `<option value="${k}" ${med === k ? "selected" : ""}>${esc(m.label)}</option>`).join("")}</select></label>
            <label class="tv-field">Label<input class="tv-inp" name="label" maxlength="60" value="${esc((L && L.label) || "")}" placeholder="e.g. Cat6 40 m · 5 GHz 2.3 km"></label></div>
          <p class="hint" data-wl></p>
          <label class="tv-field">Notes<textarea class="tv-inp" name="notes" rows="2" maxlength="500">${esc((L && L.notes) || "")}</textarea></label>
          ${editing ? `<label style="display:flex;gap:8px;align-items:center;font-size:13px"><input type="checkbox" name="swap"> Swap the two ends (and their ports)</label>` : ""}`,
        ok: editing ? "Save" : "Connect",
        async onOk(f) {
          const body = { a_port: f.a_port.value, b_port: f.b_port.value, medium: f.medium.value, label: f.label.value, notes: f.notes.value };
          if (editing) {
            if (f.swap.checked) body.swap = true;
            await send("POST", `/links/${enc(L.id)}`, body);
            toast("Saved", "ok");
            return;
          }
          body.a = f.a.value; body.b = f.b.value;
          if (!body.a || !body.b) throw new Error("Pick both ends");
          if (body.a === body.b) throw new Error("Pick two different pieces of equipment");
          const res = await send("POST", "/links", body);
          S.sel = { t: "link", id: res.result.id };
          S.panel = null;
          render();
          toast("Connected", "ok");
        },
      });
      const f = d.form;
      const hint = () => {
        const bad = f.medium.value === "wireless" ? [f.a.value, f.b.value].filter(Boolean).map((id) => S.idx.nodes[id]).filter((n) => n && !kindOf(n.kind).wireless) : [];
        f.querySelector("[data-wl]").innerHTML = bad.length
          ? `<span style="color:#fbbf24">⚠ ${bad.map((n) => `${esc(n.name)} is “${esc(kindOf(n.kind).label)}”`).join(", ")}. A wireless link joins two radios — pick the radio it is wired to, or change its equipment type first.</span>`
          : f.medium.value === "wireless" ? "Wireless: radio to radio. Signal and distance appear when Netwatch can read the radios." : "";
      };
      ["a", "b", "medium"].forEach((k) => f[k].addEventListener("change", hint));
      hint();
    }
    function insertDialog(s) {
      const unit = S.idx.nodes[s.a] || { name: s.a, group: "~" };
      const types = ["unmanaged-switch", "poe", "media-converter", "switch", "radio", "other"];
      dialog({
        title: "Add what sits between",
        sub: esc(s.evidence),
        body: `<div class="two"><label class="tv-field">Name<input class="tv-inp" name="name" maxlength="80" placeholder="${esc(`Unmanaged switch on ${unit.name} port ${String(s.a_port).split("/").pop()}`)}"></label>
            <label class="tv-field">Type<select class="tv-inp" name="kind">${types.map((k) => `<option value="${k}">${esc(kindOf(k).label)}</option>`).join("")}</select></label></div>
          <div class="two"><label class="tv-field">Group<select class="tv-inp" name="group">${groupOptions(unit.group)}</select></label>
            <label class="tv-field">Ports<input class="tv-inp" name="ports" type="number" min="0" max="512" placeholder="optional"></label></div>
          <div class="tv-field">Connected to it<div class="checks">${s.members.map((k) => `<label><input type="checkbox" name="m" value="${esc(k)}" checked> ${kindSvg((S.idx.nodes[k] || {}).kind || "other", 22, 18)} ${esc(nodeName(k))}</label>`).join("")}</div></div>
          <p class="hint">Netwatch adds it as <b>not monitored</b> equipment, wires it to ${esc(unit.name)} port ${esc(String(s.a_port).split("/").pop())}, and wires the ticked devices to it.</p>`,
        ok: "Add it",
        async onOk(f) {
          const members = [...f.querySelectorAll("[name=m]:checked")].map((c) => c.value);
          if (!members.length) throw new Error("Tick at least one device");
          const res = await send("POST", `/suggestions/${enc(s.id)}`, { action: "insert", name: f.name.value, kind: f.kind.value,
            group: f.group.value, ports: f.ports.value, members });
          S.sel = { t: "node", id: res.result.id };
          S.panel = null;
          render();
          toast("Added and wired up", "ok");
        },
      });
    }
    function shrinkImage(file, max = 160) {
      return new Promise((resolve, reject) => {
        if (!/^image\/(png|jpeg|webp|gif)$/.test(file.type)) return reject(new Error("Use a PNG, JPEG, WebP or GIF picture"));
        const img = new Image();
        const url = URL.createObjectURL(file);
        img.onload = () => {
          const s = Math.min(1, max / Math.max(img.width, img.height));
          const c = document.createElement("canvas");
          c.width = Math.max(1, Math.round(img.width * s));
          c.height = Math.max(1, Math.round(img.height * s));
          c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
          URL.revokeObjectURL(url);
          resolve(c.toDataURL("image/png"));
        };
        img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("That picture could not be read")); };
        img.src = url;
      });
    }
    const iconPreview = (iid) => (iid && opts.iconUrl ? `<img src="${esc(opts.iconUrl(iid))}" alt="">` : "");
    // ---- pictures ----------------------------------------------------------------------------
    /** Every picture Netwatch draws itself, in the order the picker shows them. */
    function builtinSections() {
      const out = KIND_SECTIONS.map(([label, ids]) => [label, ids.filter((id) => ALL_ICONS[id])]);
      const brands = {};
      Object.entries(EXTRA_META).forEach(([id, [, brand]]) => (brands[brand] = brands[brand] || []).push(id));
      Object.entries(brands).forEach(([brand, ids]) => out.push([brand, ids]));
      return out.filter(([, ids]) => ids.length);
    }
    const builtinLabel = (id) => (EXTRA_META[id] ? EXTRA_META[id][0] : kindOf(id).label);
    function builtinGrid(current) {
      return builtinSections().map(([label, ids]) => `<div class="tv-field">${esc(label)}<div class="icons">${ids.map((id) =>
        `<button type="button" class="iconbtn ${current === "b:" + id ? "on" : ""}" data-icon="b:${esc(id)}">${kindSvg(id, 56, 48)}<span>${esc(builtinLabel(id))}</span></button>`).join("")}</div></div>`).join("");
    }
    function iconDialog(n) {
      const typeIcon = (S.g.type_icons || {})[n.kind];
      const current = n.icon || "";
      const uploads = Object.values(S.g.icons || {}).sort((a, b) => b.ts - a.ts);
      const d = dialog({
        title: `Picture for ${n.name}`,
        sub: `Pick one below, or upload your own. “Default” follows the equipment type (${esc(kindOf(n.kind).label)}).`,
        wide: true,
        body: `<div class="icons">
            <button type="button" class="iconbtn ${!current ? "on" : ""}" data-icon="">${typeIcon ? (builtinOf(typeIcon) ? kindSvg(builtinOf(typeIcon), 56, 48) : iconPreview(typeIcon)) : kindSvg(n.kind, 56, 48)}<span>Default${typeIcon ? " (set for the type)" : ""}</span></button>
          </div>
          ${uploads.length ? `<div class="tv-field">Your pictures<div class="icons">${uploads.map((c) => `<button type="button" class="iconbtn ${current === c.id ? "on" : ""}" data-icon="${esc(c.id)}">${iconPreview(c.id)}<span>${esc(trunc(c.name || "Uploaded", 16))}</span></button>`).join("")}</div></div>` : ""}
          ${builtinGrid(current)}
          <label style="display:flex;gap:8px;align-items:center;font-size:13px"><input type="checkbox" name="all"> Use the picture I click for every “${esc(kindOf(n.kind).label)}”, not just this one</label>
          <label class="tv-field">Upload your own (PNG, JPEG, WebP or GIF — shrunk to 160 px)<input type="file" name="file" accept="image/png,image/jpeg,image/webp,image/gif" class="tv-inp"></label>`,
        ok: "Upload",
        async onOk(f) {
          const file = f.file.files[0];
          if (!file) throw new Error("Choose a picture above, or pick a file to upload");
          const data = await shrinkImage(file);
          const body = { data, name: file.name };
          if (f.all.checked) body.kind = n.kind; else body.node = n.id;
          await send("POST", "/icons", body);
          toast("Picture saved", "ok");
        },
      });
      d.wrap.querySelectorAll("[data-icon]").forEach((b) => (b.onclick = async () => {
        const forType = d.wrap.querySelector("[name=all]").checked;
        try {
          if (forType) await send("POST", "/type-icons", { kind: n.kind, icon: b.dataset.icon });
          else await send("POST", `/nodes/${enc(n.id)}`, { icon: b.dataset.icon });
          toast(forType ? `Picture set for every ${kindOf(n.kind).label}` : "Picture changed", "ok");
          d.close();
        } catch (e) { d.err.textContent = e.message; }
      }));
    }
    /** Pick one of Netwatch's own pictures for a whole equipment type. */
    function typeIconDialog(kind) {
      const cur = (S.g.type_icons || {})[kind] || "";
      const d = dialog({
        title: `Picture for every ${kindOf(kind).label}`,
        sub: "Every item of this type is drawn with it, unless it has its own picture.",
        wide: true,
        body: `<div class="icons"><button type="button" class="iconbtn ${!cur ? "on" : ""}" data-icon="">${kindSvg(kind, 56, 48)}<span>Default</span></button></div>${builtinGrid(cur)}`,
      });
      d.wrap.querySelectorAll("[data-icon]").forEach((b) => (b.onclick = async () => {
        try { await send("POST", "/type-icons", { kind, icon: b.dataset.icon }); d.close(); iconLibrary(); }
        catch (e) { d.err.textContent = e.message; }
      }));
    }
    function iconLibrary() {
      const ti = S.g.type_icons || {};
      const used = {};
      S.g.nodes.forEach((n) => { if (n.icon) used[n.icon] = (used[n.icon] || 0) + 1; });
      Object.values(ti).forEach((id) => (used[id] = (used[id] || 0) + 1));
      const kinds = S.g.kinds.filter((k) => k.id !== "unknown");
      const edit = canEdit();
      const d = dialog({
        title: "Equipment pictures",
        sub: "Every type is drawn with its own picture. Choose a different one from Netwatch's set, or upload your own. A single item can also be given its own picture from its panel.",
        wide: true,
        body: `<div class="icons">${kinds.map((k) => `<div class="iconbtn">${ti[k.id] ? (builtinOf(ti[k.id]) ? kindSvg(builtinOf(ti[k.id]), 56, 48) : iconPreview(ti[k.id])) : kindSvg(k.id, 56, 48)}<span>${esc(k.label)}</span>
            <span style="display:flex;gap:4px;flex-wrap:wrap;justify-content:center"><button type="button" class="tv-btn sm" data-type-pick="${esc(k.id)}" ${edit ? "" : "disabled"}>Choose</button>
            <label class="tv-btn sm" style="cursor:pointer">Upload<input type="file" hidden accept="image/png,image/jpeg,image/webp,image/gif" data-type-up="${esc(k.id)}" ${edit ? "" : "disabled"}></label>
            ${ti[k.id] ? `<button type="button" class="tv-btn sm" data-type-reset="${esc(k.id)}" ${edit ? "" : "disabled"}>Default</button>` : ""}</span></div>`).join("")}</div>
          ${Object.keys(S.g.icons || {}).length ? `<div class="tv-field">Uploaded pictures<div class="icons">${Object.values(S.g.icons).map((c) => `<div class="iconbtn">${iconPreview(c.id)}<span>${esc(trunc(c.name || "Uploaded", 16))} · used ${used[c.id] || 0}×</span><button type="button" class="tv-btn sm danger" data-icon-del="${esc(c.id)}" ${edit ? "" : "disabled"}>Delete</button></div>`).join("")}</div></div>` : ""}`,
      });
      d.wrap.querySelectorAll("[data-type-pick]").forEach((b) => (b.onclick = () => { d.close(); typeIconDialog(b.dataset.typePick); }));
      d.wrap.querySelectorAll("[data-type-up]").forEach((inp) => inp.addEventListener("change", async () => {
        const file = inp.files[0];
        if (!file) return;
        try {
          const data = await shrinkImage(file);
          await send("POST", "/icons", { data, name: file.name, kind: inp.dataset.typeUp });
          toast("Picture saved", "ok");
          d.close();
          iconLibrary();
        } catch (e) { d.err.textContent = e.message; }
      }));
      d.wrap.querySelectorAll("[data-type-reset]").forEach((b) => (b.onclick = async () => {
        try { await send("POST", "/type-icons", { kind: b.dataset.typeReset, icon: "" }); d.close(); iconLibrary(); }
        catch (e) { d.err.textContent = e.message; }
      }));
      d.wrap.querySelectorAll("[data-icon-del]").forEach((b) => (b.onclick = async () => {
        try { await send("DELETE", `/icons/${enc(b.dataset.iconDel)}`); d.close(); iconLibrary(); }
        catch (e) { d.err.textContent = e.message; }
      }));
    }

    // ---- map ---------------------------------------------------------------------------------------
    let MAP = null, ML = null, mapFitted = false;
    S.mapDevices = pref.mapDevices !== false;
    S.mapEdit = false;
    function initMap() {
      const LF = window.L;
      mapEl.innerHTML = `<div data-mapmap style="position:absolute;inset:0"></div>
        <div data-mapbar style="position:absolute;z-index:700;left:56px;top:10px;display:flex;gap:6px;flex-wrap:wrap;align-items:center;padding:5px 7px;border-radius:12px;background:rgba(8,14,28,.9);border:1px solid var(--tv-line2)">
          <label class="tv-btn sm" style="cursor:pointer"><input type="checkbox" data-mdev> Device pins</label>
          <button type="button" class="tv-btn sm" data-medit>${ico("unlock", 14)} Move markers</button>
          <button type="button" class="tv-btn sm" data-mfit>${ico("fit", 14)} Fit</button>
          ${opts.placeDevicesHref ? `<a class="tv-btn sm" href="${esc(opts.placeDevicesHref)}" title="Give devices their own GPS pins">Place devices ↗</a>` : ""}
        </div>
        <div data-nogeo style="position:absolute;z-index:700;left:10px;bottom:24px;max-width:min(320px,calc(100% - 20px));max-height:40%;overflow:auto;padding:8px 10px;border-radius:12px;background:rgba(8,14,28,.92);border:1px solid var(--tv-line2);font-size:12.5px" hidden></div>`;
      MAP = LF.map(mapEl.querySelector("[data-mapmap]"), { zoomControl: true, worldCopyJump: true });
      if (opts.mapTiles) opts.mapTiles(MAP, LF);
      else {
        const sat = LF.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", { maxZoom: 21, maxNativeZoom: 19, attribution: "Imagery © Esri, Maxar, Earthstar Geographics" });
        const street = LF.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 21, maxNativeZoom: 19, attribution: "© OpenStreetMap contributors" });
        sat.addTo(MAP);
        LF.control.layers({ Satellite: sat, "Street map": street }, {}, { position: "topright" }).addTo(MAP);
      }
      LF.control.scale({ imperial: false }).addTo(MAP);
      ML = { links: LF.layerGroup().addTo(MAP), pins: LF.layerGroup().addTo(MAP), groups: LF.layerGroup().addTo(MAP) };
      MAP.setView([-29.0, 24.5], 5);
      MAP.on("click", (e) => {
        if (!S.placing) return;
        const gid = S.placing;
        S.placing = null;
        placeGroup(gid, e.latlng.lat, e.latlng.lng);
      });
      const bar = mapEl.querySelector("[data-mapbar]");
      LF.DomEvent.disableClickPropagation(bar);
      LF.DomEvent.disableClickPropagation(mapEl.querySelector("[data-nogeo]"));
      const dev = bar.querySelector("[data-mdev]");
      dev.checked = S.mapDevices;
      dev.onchange = () => { S.mapDevices = dev.checked; store.set(PK, { ...store.get(PK, {}), mapDevices: dev.checked }); drawMap(); };
      bar.querySelector("[data-medit]").onclick = () => needEdit(() => { S.mapEdit = !S.mapEdit; drawMap(); });
      bar.querySelector("[data-mfit]").onclick = () => mapFit(true);
    }
    async function placeGroup(gid, lat, lon) {
      const gr = S.idx.groups[gid];
      try {
        await send("POST", `/groups/${enc(gid)}`, { lat: +lat.toFixed(6), lon: +lon.toFixed(6) });
        toast(`${gr ? gr.name : "Group"} placed on the map`, "ok");
        S.sel = { t: "group", id: gid };
        render();
      } catch (e) { toast(e.message, "bad"); render(); }
    }
    function startPlacing(gid) {
      if (!canEdit()) return needEdit(() => startPlacing(gid));
      S.placing = gid;
      S.sel = { t: "group", id: gid };
      setView("map");
    }
    const gposOf = () => {
      const out = {};
      S.g.groups.forEach((g) => { if (g.geo) out[g.id] = [g.geo.lat, g.geo.lon]; });
      return out;
    };
    function mapBounds() {
      const pts = [];
      S.g.groups.forEach((g) => g.geo && pts.push([g.geo.lat, g.geo.lon]));
      if (S.mapDevices) S.g.nodes.forEach((n) => n.geo && pts.push([n.geo.lat, n.geo.lon]));
      return pts;
    }
    function mapFit(animate) {
      if (!MAP) return;
      const pts = mapBounds();
      const site = S.g.site || {};
      if (pts.length > 1) MAP.fitBounds(pts, { padding: [60, 60], maxZoom: 18, animate: !!animate });
      else if (pts.length === 1) MAP.setView(pts[0], 17, { animate: !!animate });
      else if (site.lat != null && site.lon != null) MAP.setView([site.lat, site.lon], 15, { animate: !!animate });
    }
    function drawMap() {
      const LF = window.L;
      if (!LF) { mapEl.innerHTML = `<div class="tv-empty"><div><b>The map library did not load</b>Reload the page.</div></div>`; return; }
      if (!MAP) initMap();
      setTimeout(() => MAP && MAP.invalidateSize(), 40);
      ML.links.clearLayers(); ML.pins.clearLayers(); ML.groups.clearLayers();
      const edit = S.mapEdit && canEdit() && !locked();
      const eb = mapEl.querySelector("[data-medit]");
      eb.classList.toggle("on", edit);
      eb.innerHTML = `${ico(edit ? "lock" : "unlock", 14)} ${edit ? "Markers unlocked" : "Move markers"}`;
      const gpos = gposOf();
      const npos = (n) => (n.geo ? [n.geo.lat, n.geo.lon] : gpos[n.group] || null);
      const hits = S.q.trim() ? new Set(S.g.nodes.filter(matchesQuery).map((n) => n.id)) : null;
      // connections between places
      const segs = {};
      S.g.links.forEach((lk) => {
        if (!S.media[lk.medium] || (!lk.confirmed && !S.sugg)) return;
        const a = S.idx.nodes[lk.a], b = S.idx.nodes[lk.b];
        if (!a || !b) return;
        if (!S.mapDevices && (a.geo || b.geo) && a.group === b.group) return;
        const pa = S.mapDevices ? npos(a) : gpos[a.group], pb = S.mapDevices ? npos(b) : gpos[b.group];
        if (!pa || !pb || (Math.abs(pa[0] - pb[0]) < 1e-7 && Math.abs(pa[1] - pb[1]) < 1e-7)) return;
        const key = [pa.join(","), pb.join(",")].sort().join("|");
        (segs[key] = segs[key] || { pa, pb, links: [] }).links.push(lk);
      });
      Object.values(segs).forEach((sg) => {
        const look = edgeLook({ links: sg.links, A: {}, B: {} });
        const color = look.status === "down" ? "#fb7185" : look.status === "degraded" ? "#fbbf24" : look.medium === "wireless" ? "#22d3ee" : look.medium === "fibre" ? "#c4b5fd" : "#e2e8f0";
        const weight = look.medium === "fibre" ? 5 : 3;
        const sel = S.sel && S.sel.t === "link" && sg.links.some((x) => x.id === S.sel.id);
        LF.polyline([sg.pa, sg.pb], { color: "#020617", weight: weight + 4, opacity: 0.55, interactive: false }).addTo(ML.links);
        const line = LF.polyline([sg.pa, sg.pb], { color, weight: sel ? weight + 2 : weight, opacity: 0.95,
          dashArray: !look.confirmed ? "2 9" : look.medium === "wireless" ? "12 9" : null });
        const first = sg.links[0];
        const m = first.metrics || {};
        const dist = m.distance || LF.latLng(sg.pa).distanceTo(sg.pb);
        const tip = `<b>${esc(sg.links.length > 1 ? sg.links.length + " connections" : MEDIUM[first.medium].label + (first.confirmed ? "" : " (suggested)"))}</b><br>`
          + sg.links.slice(0, 4).map((x) => `${esc(nodeName(x.a))} ↔ ${esc(nodeName(x.b))}${x.metrics && x.metrics.signal != null ? ` · ${Math.round(x.metrics.signal)} dBm` : ""}`).join("<br>")
          + `<br><span style="color:#94a3b8">${esc(km(dist))}${look.status === "down" ? " · an end is offline" : ""}</span>`;
        line.bindTooltip(tip, { className: "tv-tip", sticky: true });
        line.on("click", (e) => { LF.DomEvent.stopPropagation(e); selectLink(first.id); });
        line.addTo(ML.links);
      });
      // device pins
      if (S.mapDevices) {
        S.g.nodes.forEach((n) => {
          if (!n.geo || n.virtual) return;
          const st = STATE[n.state] || STATE.online;
          const sel = S.sel && S.sel.t === "node" && S.sel.id === n.id;
          const dim = hits && !hits.has(n.id);
          const mk = LF.marker([n.geo.lat, n.geo.lon], {
            icon: LF.divIcon({ className: "tv-mkwrap", iconSize: null, html: `<div class="tv-pin ${st.cls === "on" ? "" : st.cls} ${sel ? "is-sel" : ""}" style="${dim ? "opacity:.3" : ""}"><svg viewBox="0 0 64 56"><use href="#tvk-${esc(ICONS[n.kind] ? n.kind : "other")}"/></svg></div>` }),
            keyboard: true, title: `${n.name} — ${st.label}`, zIndexOffset: sel ? 900 : 0,
          });
          mk.bindTooltip(`${esc(n.name)} · ${esc(st.label)}`, { className: "tv-tip", direction: "top", offset: [0, -12] });
          mk.on("click", () => { S.sel = { t: "node", id: n.id }; S.panel = null; render(); });
          mk.addTo(ML.pins);
        });
      }
      // towers and sites
      S.g.groups.forEach((g) => {
        if (!g.geo) return;
        const c = g.counts || {};
        const mon = (c.total || 0) - (c.unmonitored || 0);
        const sel = S.sel && S.sel.t === "group" && S.sel.id === g.id;
        const dim = hits && !(S.idx.byGroup[g.id] || []).some((n) => hits.has(n.id)) && !g.name.toLowerCase().includes(S.q.trim().toLowerCase());
        const cnt = mon ? `<span class="cnt ${(c.offline || c.quiet) ? "bad" : "ok"}">${c.online}/${mon}</span>` : `<span class="cnt">${c.total || 0}</span>`;
        const flag = g.status === "down" ? " ✕" : g.status === "warn" ? " !" : "";
        const html = `<div class="tv-mk st-${g.status} ${g.geo_approx ? "approx" : ""} ${sel ? "is-sel" : ""}" style="${dim ? "opacity:.35" : ""}"><span class="gi">${gicon(g.kind, 18)}</span><b>${esc(g.name)}</b>${cnt}${flag}</div>`;
        const mk = LF.marker([g.geo.lat, g.geo.lon], { icon: LF.divIcon({ className: "tv-mkwrap", iconSize: null, html }),
          draggable: edit && !g.geo_approx, keyboard: true, zIndexOffset: 1000, title: `${g.name} — ${{ ok: "all online", warn: "needs a look", down: "down", unknown: "not monitored" }[g.status] || ""}${g.geo_approx ? " (approximate position)" : ""}` });
        mk.bindTooltip(`${esc((S.g.group_kinds || {})[g.kind] || "Group")}: ${esc(g.name)}<br>${mon ? `${c.online} of ${mon} online` : "Nothing monitored"}${c.unmonitored ? ` · ${c.unmonitored} not monitored` : ""}${g.geo_approx ? "<br><i>Approximate — set its location for the real spot</i>" : ""}`, { className: "tv-tip", direction: "top", offset: [0, -34] });
        mk.on("click", () => { S.sel = { t: "group", id: g.id }; S.panel = null; render(); });
        mk.on("dragend", (e) => { const ll = e.target.getLatLng(); placeGroup(g.id, ll.lat, ll.lng); });
        mk.addTo(ML.groups);
      });
      // groups that have no position yet
      const nogeo = S.g.groups.filter((g) => !g.geo);
      const box = mapEl.querySelector("[data-nogeo]");
      box.hidden = !nogeo.length;
      box.innerHTML = nogeo.length ? `<b style="display:block;margin-bottom:4px">Not on the map yet</b>` + nogeo.map((g) =>
        `<div style="display:flex;align-items:center;gap:6px;margin:3px 0">${gicon(g.kind, 16)}<span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(g.name)}</span><button type="button" class="tv-btn sm" data-place="${esc(g.id)}">${S.placing === g.id ? "Click the map…" : "Place"}</button></div>`).join("") : "";
      box.querySelectorAll("[data-place]").forEach((b) => (b.onclick = () => startPlacing(b.dataset.place)));
      mapEl.querySelector("[data-mapmap]").style.cursor = S.placing ? "crosshair" : "";
      if (!mapFitted) { mapFit(false); mapFitted = true; }
    }
    function mapFocusGroup(gid) {
      const g = S.idx.groups[gid];
      if (!MAP || !g) return;
      if (g.geo) MAP.setView([g.geo.lat, g.geo.lon], Math.max(MAP.getZoom(), 16));
      else toast(`${g.name} has no location yet — use Place`);
    }
    function mapFocusNode(id) {
      const n = S.idx.nodes[id];
      if (!MAP || !n) return;
      const g = S.idx.groups[n.group];
      const p = n.geo || (g && g.geo);
      if (p) MAP.setView([p.lat, p.lon], Math.max(MAP.getZoom(), n.geo ? 18 : 16));
      else toast(`${n.name} has no position on the map`);
    }
    function mapFocusLink(lk) {
      if (!MAP) return;
      const pos = (id) => { const n = S.idx.nodes[id]; const g = n && S.idx.groups[n.group]; const p = (n && n.geo) || (g && g.geo); return p ? [p.lat, p.lon] : null; };
      const pts = [pos(lk.a), pos(lk.b)].filter(Boolean);
      if (pts.length === 2) MAP.fitBounds(pts, { padding: [80, 80], maxZoom: 18 });
      else if (pts.length === 1) MAP.setView(pts[0], Math.max(MAP.getZoom(), 16));
    }

    // ---- lifecycle ---------------------------------------------------------------------------------
    function focus(id) {
      if (!S.g) { S.focusWanted = id; return; }
      if (S.idx.groups[id]) {
        S.sel = { t: "group", id };
        S.panel = null;
        render();
        if (S.view === "diagram") { computeBoxes(); if (S.boxes[id]) { fitView(true, S.boxes[id]); drawDiagram(); } }
        else mapFocusGroup(id);
        return;
      }
      const n = S.idx.nodes[id];
      if (!n) { toast("That device is not on the diagram — add it from Review"); return; }
      const gr = S.idx.groups[n.group];
      if (S.view === "diagram" && gr && gr.collapsed) {
        gr.collapsed = false;
        if (canEdit()) opts.call("POST", `/groups/${enc(gr.id)}`, { collapsed: false }).catch(() => {});
        else localCollapse[gr.id] = false;
      }
      if (n.group === "~" && !S.review) { S.review = true; savePref(); }
      S.sel = { t: "node", id };
      S.panel = null;
      render();
      if (S.view === "diagram") {
        const a = absPos(n);
        if (a) centerOn(a.x + CW() / 2, a.y + CH() / 2, Math.max(S.k, 0.9));
        drawDiagram();
        focusNodeEl(id);
      } else mapFocusNode(id);
    }
    const onResize = () => {
      if (!S.g) return;
      tv.classList.toggle("side-off", !S.sel && S.panel !== "review" && window.innerWidth <= 1100);
      if (S.view === "map" && MAP) MAP.invalidateSize();
    };
    const onVis = () => { if (showing() && S.g && now() - (S.g.ts || 0) > 20) refresh(true); };
    window.addEventListener("resize", onResize);
    document.addEventListener("visibilitychange", onVis);
    const showing = () => !document.hidden && root.isConnected && root.offsetParent !== null;
    S.timer = setInterval(() => { if (showing() && !S.drag && !dialogs.size && !S.connect) refresh(true); }, opts.refreshMs || 30000);
    render();
    refresh();
    return {
      refresh: () => refresh(false),
      setView,
      focus,
      select: (id) => focus(id),
      destroy() {
        S.destroyed = true;
        clearInterval(S.timer);
        window.removeEventListener("resize", onResize);
        document.removeEventListener("visibilitychange", onVis);
        [...dialogs].forEach((close) => close());
        if (MAP) { MAP.remove(); MAP = null; }
        root.innerHTML = "";
      },
      get state() { return S; },
    };
  }

  // =============================================================================================
  /** Towers and sites (and the links between them) on an existing Leaflet map —
   *  the Map pages keep their own device pins and placing tools.
   *   const ov = TopoView.overlay(map, { load, networkHref(groupId), onChange });  ov.refresh(); ov.remove(); */
  function overlay(map, o) {
    injectCss();
    ensureSymbols();
    const LF = window.L;
    const layer = LF.layerGroup().addTo(map);
    let graph = null, dead = false;
    function draw() {
      layer.clearLayers();
      if (!graph || dead) return;
      const byId = {};
      graph.nodes.forEach((n) => (byId[n.id] = n));
      const gpos = {};
      graph.groups.forEach((g) => { if (g.geo) gpos[g.id] = [g.geo.lat, g.geo.lon]; });
      const segs = {};
      graph.links.forEach((lk) => {
        if (!lk.confirmed) return;
        const a = byId[lk.a], b = byId[lk.b];
        const pa = a && gpos[a.group], pb = b && gpos[b.group];
        if (!pa || !pb || a.group === b.group) return;
        const key = [a.group, b.group].sort().join("|");
        (segs[key] = segs[key] || { pa, pb, links: [] }).links.push(lk);
      });
      Object.values(segs).forEach((sg) => {
        const wl = sg.links.every((x) => x.medium === "wireless");
        const fib = sg.links.some((x) => x.medium === "fibre");
        const down = sg.links.some((x) => x.status === "down");
        LF.polyline([sg.pa, sg.pb], { color: "#020617", weight: 7, opacity: 0.5, interactive: false }).addTo(layer);
        LF.polyline([sg.pa, sg.pb], { color: down ? "#fb7185" : wl ? "#22d3ee" : fib ? "#c4b5fd" : "#e2e8f0", weight: fib ? 5 : 3, dashArray: wl ? "12 9" : null, opacity: 0.95 })
          .bindTooltip(`${sg.links.length} link${sg.links.length > 1 ? "s" : ""}${down ? " · an end is offline" : ""}<br>${sg.links.slice(0, 3).map((x) => `${esc((byId[x.a] || {}).name)} ↔ ${esc((byId[x.b] || {}).name)}`).join("<br>")}`, { className: "tv-tip", sticky: true })
          .addTo(layer);
      });
      graph.groups.forEach((g) => {
        if (!g.geo) return;
        const c = g.counts || {};
        const mon = (c.total || 0) - (c.unmonitored || 0);
        const html = `<div class="tv-mk below st-${g.status} ${g.geo_approx ? "approx" : ""}"><span class="gi">${gicon(g.kind, 18)}</span><b>${esc(g.name)}</b><span class="cnt ${(c.offline || c.quiet) ? "bad" : mon ? "ok" : ""}">${mon ? `${c.online}/${mon}` : c.total || 0}</span></div>`;
        const members = graph.nodes.filter((n) => n.group === g.id);
        const pop = `<div style="min-width:220px"><b style="font-size:14px">${esc(g.name)}</b><br><span style="color:#94a3b8">${esc((graph.group_kinds || {})[g.kind] || "")}${g.geo_approx ? " · approximate position" : ""}</span>
          <div style="margin:6px 0;max-height:180px;overflow:auto">${members.map((n) => `<div style="display:flex;gap:6px;align-items:center">${kindSvg(n.kind, 22, 18)}<span style="flex:1">${esc(n.name)}</span><span style="color:${n.state === "online" ? "#34d399" : n.state === "unmonitored" ? "#cbd5e1" : "#fb7185"}">${esc((STATE[n.state] || {}).label || "")}</span></div>`).join("") || "<i>Empty</i>"}</div>
          ${o.networkHref ? `<a href="${esc(o.networkHref(g.id))}">Open in the network view →</a>` : ""}</div>`;
        LF.marker([g.geo.lat, g.geo.lon], { icon: LF.divIcon({ className: "tv-mkwrap", iconSize: null, html }), zIndexOffset: -500, keyboard: true, title: g.name })
          .bindPopup(pop, { maxWidth: 320 }).addTo(layer);
      });
      if (o.onChange) o.onChange(graph);
    }
    async function refresh() {
      try { graph = sanitize(await o.load()); draw(); } catch (e) { /* the map works without the overlay */ }
    }
    refresh();
    return { refresh, remove() { dead = true; layer.remove(); }, get graph() { return graph; },
      bounds() { return graph ? graph.groups.filter((g) => g.geo).map((g) => [g.geo.lat, g.geo.lon]) : []; } };
  }

  window.TopoView = { mount, overlay, injectCss, ensureSymbols, kindSvg, gicon, parseLatLon, sanitize, esc, icons: Object.keys(ICONS) };
})();
