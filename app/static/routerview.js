/* Netwatch — one MikroTik router, drawn exactly the way a switch is.
 *
 * A router and a switch are both managed units, so this deliberately reuses
 * switchview.js's stylesheet and card shape: same front panel, same port table,
 * same badges. ONE renderer serves BOTH the site's managed-units page and the
 * hub Control Center (the hub image copies this file next to switchview.js).
 *
 * The host supplies the transport, so the same code works site-direct or
 * hub-proxied:
 *   load()              -> GET  …/mikrotik/report   (normalised ports/summary/poe)
 *   action(body)        -> POST …/mikrotik/action   (rename/reboot/poe/iface/export)
 *   runCmd(cmd, conf)   -> POST …/mikrotik/console  (the terminal)
 *   confirm/toast/onLogin/manage/port
 */
(function () {
  "use strict";
  if (window.RouterView) return;

  // Only used when switchview.js isn't on the page — normally we share its sheet.
  const FALLBACK = `
.swv{--swv-line:rgba(148,163,184,.16);--swv-panel:rgba(148,163,184,.06);--swv-ink:#e2e8f0;--swv-muted:#94a3b8;--swv-dim:#64748b;
  --swv-ok:#34d399;--swv-warn:#fbbf24;--swv-bad:#fb7185;--swv-cyan:#22d3ee;color:var(--swv-ink);font-size:14px;line-height:1.45;display:grid;gap:14px;min-width:0}
.swv *{box-sizing:border-box}.swv button{font:inherit;color:inherit;cursor:pointer}
.swv .swv-box{background:var(--swv-panel);border:1px solid var(--swv-line);border-radius:14px;padding:14px 16px;min-width:0}
.swv h3{margin:0 0 8px;font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--swv-muted);font-weight:600}
.swv .swv-muted{color:var(--swv-muted)}.swv .swv-dim{color:var(--swv-dim)}
.swv .swv-mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.92em}
.swv .swv-row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.swv .swv-btn{border:1px solid rgba(148,163,184,.28);background:rgba(148,163,184,.08);border-radius:9px;padding:6px 11px;font-size:13px;font-weight:600}
.swv .swv-btn.pri{background:#0e7490;border-color:#0891b2;color:#ecfeff}.swv .swv-btn.danger{border-color:rgba(251,113,133,.45);color:#fecdd3}
.swv .swv-btn.sm{padding:4px 8px;font-size:12px}.swv .swv-btn:disabled{opacity:.5}
.swv .swv-b{display:inline-flex;align-items:center;gap:5px;border-radius:999px;padding:2px 9px;font-size:12px;font-weight:600;background:rgba(148,163,184,.13);color:#cbd5e1}
.swv .swv-b.ok{background:rgba(16,185,129,.14);color:var(--swv-ok)}.swv .swv-b.warn{background:rgba(245,158,11,.14);color:var(--swv-warn)}
.swv .swv-b.bad{background:rgba(244,63,94,.15);color:var(--swv-bad)}
.swv .swv-head{display:flex;flex-wrap:wrap;gap:10px 16px;align-items:flex-start;justify-content:space-between}
.swv .swv-head h2{margin:0;font-size:20px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.swv .swv-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.swv .swv-kpi{background:var(--swv-panel);border:1px solid var(--swv-line);border-radius:12px;padding:10px 12px}
.swv .swv-kpi .l{font-size:12px;color:var(--swv-muted)}.swv .swv-kpi .v{font-size:20px;font-weight:700;margin-top:2px}
.swv .swv-kpi .s{font-size:12px;color:var(--swv-dim);margin-top:2px}
.swv .swv-face{display:flex;gap:6px;overflow-x:auto;padding:10px;border-radius:12px;background:linear-gradient(#0b1220,#070c16);border:1px solid rgba(148,163,184,.22)}
.swv .swv-port{flex:0 0 auto;width:74px;border-radius:9px;border:1px solid rgba(148,163,184,.22);background:#0d1627;padding:6px 6px 5px;text-align:left;position:relative;display:grid;gap:3px}
.swv .swv-port.sel{border-color:var(--swv-cyan);box-shadow:0 0 0 2px rgba(34,211,238,.25)}
.swv .swv-port .n{display:flex;justify-content:space-between;align-items:center;font-size:12px;font-weight:700}
.swv .swv-led{width:9px;height:9px;border-radius:50%;background:#334155}
.swv .swv-led.g{background:var(--swv-ok);box-shadow:0 0 6px rgba(52,211,153,.7)}.swv .swv-led.a{background:var(--swv-warn)}
.swv .swv-led.r{background:var(--swv-bad)}.swv .swv-led.x{background:#1e293b;outline:1px dashed #475569}
.swv .swv-port .jack{height:22px;border-radius:4px;background:#020617;border:1px solid #1e293b;display:flex;align-items:center;justify-content:center;font-size:11px;color:#64748b}
.swv .swv-port .d{font-size:11px;color:var(--swv-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-height:15px}
.swv .swv-port .p{font-size:11px;color:#fde68a;min-height:15px}
.swv .swv-tablewrap{overflow-x:auto}
.swv table.swv-t{width:100%;border-collapse:collapse;font-size:13px}
.swv table.swv-t th{text-align:left;color:var(--swv-muted);font-size:12px;padding:6px 8px;border-bottom:1px solid var(--swv-line)}
.swv table.swv-t td{padding:8px;border-bottom:1px solid rgba(148,163,184,.08)}
.swv .swv-note{font-size:12px;color:var(--swv-dim)}
.swv .swv-banner{border-radius:12px;padding:12px 14px;border:1px solid rgba(251,191,36,.35);background:rgba(120,53,15,.14)}
.swv .swv-banner.bad{border-color:rgba(251,113,133,.4);background:rgba(127,29,29,.16)}
.swv .swv-seg{display:inline-flex;border:1px solid rgba(148,163,184,.28);border-radius:9px;overflow:hidden}
.swv .swv-seg button{border:0;background:transparent;padding:4px 10px;font-size:12px;font-weight:600}
.swv .swv-seg button.on{background:rgba(34,211,238,.18);color:#a5f3fc}
.swv input.swv-in{background:#050b16;border:1px solid rgba(148,163,184,.28);border-radius:8px;padding:5px 8px;color:#e2e8f0;font:inherit;font-size:13px}
.swv .swv-skel{height:90px;border-radius:12px;background:rgba(148,163,184,.08)}
.swv dl.swv-kv{display:grid;grid-template-columns:max-content 1fr;gap:4px 14px;margin:0;font-size:13px}
.swv dl.swv-kv dt{color:var(--swv-muted)}.swv dl.swv-kv dd{margin:0;overflow-wrap:anywhere}`;

  function injectCss() {
    if (window.SwitchView && window.SwitchView.injectCss) return window.SwitchView.injectCss();
    if (document.getElementById("swv-css")) return;
    const s = document.createElement("style");
    s.id = "swv-css";
    s.textContent = FALLBACK;
    document.head.appendChild(s);
  }

  // ---- formatting -------------------------------------------------------------------------
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  function bps(v) {
    const n = +v || 0;
    if (n >= 1e9) return (n / 1e9).toFixed(2) + " Gb/s";
    if (n >= 1e6) return (n / 1e6).toFixed(1) + " Mb/s";
    if (n >= 1e3) return Math.round(n / 1e3) + " Kb/s";
    return n ? n + " b/s" : "—";
  }
  function bytes(v) {
    let n = +v || 0, i = 0;
    const u = ["B", "KB", "MB", "GB", "TB"];
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i ? n.toFixed(1) : n) + " " + u[i];
  }
  function ago(ts) {
    if (!ts) return "";
    const s = Math.max(0, Math.floor(Date.now() / 1000) - ts);
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + " min ago";
    if (s < 86400) return Math.floor(s / 3600) + " h ago";
    return Math.floor(s / 86400) + " d ago";
  }
  const portNo = (p) => String(p.name || p.id || "").replace(/^ether/i, "");
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

  function portState(p) {
    if (!p.enabled) return { led: "x", label: "Switched off", cls: "" };
    if (!p.up) return { led: "", label: "No link", cls: "" };
    if (p.speed && p.speed < 100) return { led: "a", label: p.speed + " Mbit/s", cls: "warn" };
    return { led: "g", label: p.speed ? p.speed + " Mbit/s" : "Up", cls: "ok" };
  }

  // Why a router won't read, and what to do about it.
  const KIND = {
    no_login: ["warn", "Login needed", "No admin login is saved for this router. Save its username and password, then it can be managed here (RouterOS ships admin with a blank password)."],
    auth_failed: ["bad", "Login rejected", "The router refused the saved login. Save the correct admin username and password."],
    api_off: ["warn", "API turned off", "The RouterOS API service (port 8728) is off on this router. Open the Terminal below and run  /ip service enable api  — that works over MAC-Telnet without the API."],
    unreachable: ["bad", "Can't reach it", "The router didn't answer. Check it's powered and reachable from the site Pi."],
    no_ip: ["warn", "No IP yet", "Netwatch hasn't learned this router's IP. Its MAC is discovered — run a scan, or adopt it from the managed-units page."],
    pending: ["", "Not read yet", "Open it to read the router."],
  };

  function stateBadge(r) {
    if (r.online === false && !r.ok) return `<span class="swv-b bad">Offline</span>`;
    if (!r.ok) { const k = KIND[r.kind] || ["warn", "Can't read"]; return `<span class="swv-b ${k[0]}">${esc(k[1])}</span>`; }
    const crit = (r.problems || []).filter((p) => p.level === "crit").length;
    const warn = (r.problems || []).filter((p) => p.level === "warn").length;
    if (crit) return `<span class="swv-b bad">${plural(crit, "fault")}</span>`;
    if (warn) return `<span class="swv-b warn">${warn} to check</span>`;
    return `<span class="swv-b ok">Healthy</span>`;
  }

  /** Small summary for the managed-unit list — same shape as a switch card. */
  function summaryCard(r, href) {
    injectCss();
    const s = r.summary || {}, poe = r.poe || {};
    const leds = (r.ports || []).filter((p) => p.type === "ether")
      .map((p) => `<i class="swv-led ${portState(p).led}" title="${esc(p.name)}"></i>`).join("");
    // A router we can't log into still announces itself over MNDP, so show what it
    // says about itself rather than an empty card.
    const known = [r.version && "RouterOS " + r.version, r.uptime && "up " + r.uptime,
                   r.mndp && "answers by MAC"].filter(Boolean).join(" · ");
    const line = (r.ports || []).length
      ? `${s.up}/${s.ports} ports up${poe.ports ? ` · ${poe.ports} PoE` : ""} · ↓ ${bps(s.rx_bps)}`
      : (known || (KIND[r.kind] ? KIND[r.kind][1] : "no reading yet"));
    return `<a class="swv swv-box" href="${esc(href)}" style="display:grid;gap:6px;text-decoration:none;color:inherit">
      <div class="swv-row" style="justify-content:space-between"><b>🧭 ${esc(r.name)}</b>${stateBadge(r)}</div>
      <div class="swv-muted" style="font-size:13px">${esc([r.model, r.ip].filter(Boolean).join(" · "))}</div>
      ${leds ? `<div class="swv-row" style="gap:5px">${leds}</div>` : ""}
      <div class="swv-note">${esc(line)}${r.read_ts ? " · " + ago(r.read_ts) : ""}</div></a>`;
  }

  // ---- mount ------------------------------------------------------------------------------
  function mount(root, opts) {
    injectCss();
    const o = Object.assign({
      confirm: (t, x) => Promise.resolve(window.confirm(t + (x ? "\n\n" + x : ""))),
      toast: () => {},
    }, opts);
    const st = { data: null, sel: null, tab: "devices", dead: false, err: "", hist: [], timer: null, q: "" };
    root.classList.add("swv");
    root.innerHTML = `<div class="swv-skel"></div><div class="swv-skel" style="height:150px"></div>`;

    async function refresh(quiet) {
      try {
        const d = await o.load();
        if (st.dead) return;
        st.data = d; st.err = "";
        if (st.sel && !(d.ports || []).some((p) => p.name === st.sel)) st.sel = null;
        draw();
      } catch (e) {
        if (st.dead) return;
        st.err = (e.body && (e.body.hint || e.body.error)) || e.message || String(e);
        st.data = st.data && quiet ? st.data : null;
        draw();
      }
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
        o.toast((r && r.msg) || okMsg || "Done", "ok");
        setTimeout(() => refresh(true), 2000);
        return r;
      } catch (e) {
        o.toast((e.body && e.body.error) || e.message || String(e), "bad");
        return null;
      } finally { if (btn) btn.disabled = false; }
    }

    function draw() {
      const d = st.data;
      if (!d) {
        root.innerHTML = `<div class="swv-banner bad">${esc(st.err || "Couldn't read this router.")}</div>
          ${o.onLogin ? `<div class="swv-row"><button class="swv-btn pri" data-x="login">Save the router's login</button></div>` : ""}`;
        bind();
        return;
      }
      const y = d.system || {};
      const s = d.summary || {}, poe = d.poe || {};
      const memPct = (y.free_memory && y.total_memory) ? Math.round(100 * (1 - y.free_memory / y.total_memory)) : null;
      const eth = (d.ports || []).filter((p) => p.type === "ether");
      const other = (d.ports || []).filter((p) => p.type !== "ether");
      const locked = d.manage_enabled === false;

      root.innerHTML = `
        <div class="swv-head">
          <div style="min-width:0">
            <h2>🧭 ${esc(y.identity || d.name || "MikroTik")} ${stateBadge({ ok: true, problems: d.problems || [] })}
              <span class="swv-b">${esc(d.source === "mactelnet" ? "by MAC" : "via IP")}</span></h2>
            <p class="swv-muted">${esc([y.model || y.board, y.version && ("RouterOS " + y.version), d.ip].filter(Boolean).join(" · "))}</p>
          </div>
          <div class="swv-row">
            <button class="swv-btn sm" data-x="refresh">↻ Read now</button>
            ${o.onLogin ? `<button class="swv-btn sm" data-x="login">Login</button>` : ""}
          </div>
        </div>

        ${locked ? `<div class="swv-banner">🔒 Read-only — switch on <b>Manage MikroTik routers</b> in this site's Settings to unlock the actions.</div>` : ""}

        <div class="swv-kpis">
          <div class="swv-kpi"><div class="l">CPU</div><div class="v">${esc(String(y.cpu ?? "—"))}%</div><div class="s">${esc(y.cpu_count ? y.cpu_count + " core" : "")}</div></div>
          <div class="swv-kpi"><div class="l">Memory</div><div class="v">${memPct == null ? "—" : memPct + "%"}</div><div class="s">${bytes((y.total_memory || 0) - (y.free_memory || 0))} of ${bytes(y.total_memory)}</div></div>
          <div class="swv-kpi"><div class="l">Uptime</div><div class="v" style="font-size:16px">${esc(y.uptime || "—")}</div><div class="s">${esc(y.version || "")}</div></div>
          <div class="swv-kpi"><div class="l">Ports up</div><div class="v">${s.up ?? "—"}/${s.ports ?? "—"}</div><div class="s">↓ ${bps(s.rx_bps)} ↑ ${bps(s.tx_bps)}</div></div>
          <div class="swv-kpi"><div class="l">Devices seen</div><div class="v">${(d.connected || []).length}</div><div class="s">${poe.ports ? poe.ports + " PoE ports" : ""}</div></div>
          ${(y.health && (y.health.temperature || y.health.voltage)) ? `<div class="swv-kpi"><div class="l">Health</div><div class="v" style="font-size:16px">${esc([y.health.temperature && y.health.temperature + " °C", y.health.voltage && y.health.voltage + " V"].filter(Boolean).join(" · "))}</div></div>` : ""}
        </div>

        <div>
          <h3>Ports</h3>
          <div class="swv-face">${eth.map(facePort).join("") || `<span class="swv-note">no ethernet ports</span>`}</div>
          ${other.length ? `<div class="swv-note" style="margin-top:6px">Also: ${other.map((p) => esc(p.name) + (p.up ? "" : " (down)")).join(", ")}</div>` : ""}
          <div id="rv-detail"></div>
        </div>

        <div class="swv-box"><div class="swv-row" style="justify-content:space-between;margin-bottom:8px">
            <div class="swv-seg" id="rv-tabs">
              ${["devices", "network", "logs", "terminal"].map((t) => `<button class="${st.tab === t ? "on" : ""}" data-tab="${t}">${t === "devices" ? "Devices" : t === "network" ? "Network" : t === "logs" ? "Log" : "Terminal"}</button>`).join("")}
            </div>
            <div class="swv-row">
              <button class="swv-btn sm" data-x="rename" ${locked ? "disabled" : ""}>Rename</button>
              <button class="swv-btn sm" data-x="export">Export config</button>
              <button class="swv-btn sm danger" data-x="reboot" ${locked ? "disabled" : ""}>Reboot</button>
            </div></div>
          <div id="rv-tabbody"></div></div>`;
      drawDetail();
      drawTab();
      bind();
    }

    function facePort(p) {
      const ps = portState(p);
      const dev = (p.comment || "").trim();
      const poeTxt = p.poe_mode && p.poe_mode !== "off"
        ? (p.poe_w >= 0.5 ? p.poe_w.toFixed(1) + " W" : (p.poe && p.poe.status === "waiting-for-load" ? "PoE idle" : "PoE"))
        : "";
      return `<button class="swv-port ${st.sel === p.name ? "sel" : ""}" data-port="${esc(p.name)}" title="${esc(p.name)} — ${esc(ps.label)}">
        <div class="n">${esc(portNo(p))}<i class="swv-led ${ps.led}"></i></div>
        <div class="jack">${p.up ? esc(p.speed ? p.speed + "M" : "up") : ""}</div>
        <div class="d">${esc(dev)}</div>
        <div class="p">${esc(poeTxt)}</div></button>`;
    }

    function drawDetail() {
      const host = root.querySelector("#rv-detail");
      if (!host) return;
      const p = (st.data.ports || []).find((x) => x.name === st.sel);
      if (!p) { host.innerHTML = `<p class="swv-note" style="margin-top:8px">Pick a port to see it and act on it.</p>`; return; }
      const locked = st.data.manage_enabled === false;
      const ps = portState(p);
      const onPoe = p.poe_mode && p.poe_mode !== "off";
      host.innerHTML = `<div class="swv-box" style="margin-top:10px">
        <div class="swv-row" style="justify-content:space-between"><b>${esc(p.name)}</b><span class="swv-b ${ps.cls}">${esc(ps.label)}</span></div>
        <dl class="swv-kv" style="margin-top:8px">
          <dt>Traffic</dt><dd class="swv-mono">↓ ${bps(p.rx_rate)} ↑ ${bps(p.tx_rate)}</dd>
          <dt>Total</dt><dd class="swv-mono">↓ ${bytes(p.rx)} ↑ ${bytes(p.tx)}</dd>
          ${p.mac ? `<dt>MAC</dt><dd class="swv-mono">${esc(p.mac)}</dd>` : ""}
          ${p.comment ? `<dt>Comment</dt><dd>${esc(p.comment)}</dd>` : ""}
          ${p.poe ? `<dt>PoE</dt><dd>${esc(p.poe_mode || "off")}${p.poe.status ? " · " + esc(p.poe.status) : ""}${p.poe_w >= 0.5 ? " · " + p.poe_w.toFixed(1) + " W" : ""}</dd>` : ""}
        </dl>
        <div class="swv-row" style="margin-top:10px">
          <button class="swv-btn sm" data-x="iface" data-port="${esc(p.name)}" data-enable="${p.enabled ? "0" : "1"}" ${locked ? "disabled" : ""}>${p.enabled ? "Switch port off" : "Switch port on"}</button>
          ${p.poe ? `<button class="swv-btn sm pri" data-x="cycle" data-port="${esc(p.name)}" ${locked || !onPoe ? "disabled" : ""}>⟳ Power cycle</button>
            <button class="swv-btn sm" data-x="poe" data-port="${esc(p.name)}" data-mode="${onPoe ? "off" : "auto-on"}" ${locked ? "disabled" : ""}>${onPoe ? "PoE off" : "PoE on"}</button>` : ""}
        </div>
        ${p.poe ? `<p class="swv-note" style="margin:8px 0 0">Power cycle drops PoE on this port for a few seconds — whatever is plugged in (camera, AP) restarts.</p>` : ""}
      </div>`;
    }

    function drawTab() {
      const host = root.querySelector("#rv-tabbody");
      if (!host) return;
      const d = st.data;
      if (st.tab === "devices") {
        const rows = (d.connected || []).filter((c) => !st.q || (`${c.ip} ${c.mac} ${c.hostname || ""} ${c.vendor || ""} ${c.port || c.iface || ""}`).toLowerCase().includes(st.q));
        host.innerHTML = `<div class="swv-row" style="justify-content:space-between;margin-bottom:8px">
            <span class="swv-muted">${plural((d.connected || []).length, "device")} the router can see</span>
            <input class="swv-in" id="rv-q" placeholder="filter ip / mac / name / port" value="${esc(st.q)}" style="max-width:240px"></div>
          <div class="swv-tablewrap"><table class="swv-t"><thead><tr><th>IP</th><th>MAC</th><th>Vendor</th><th>Name</th><th>Port</th><th>Seen by</th></tr></thead><tbody>
          ${rows.map((c) => `<tr><td data-l="IP" class="swv-mono">${esc(c.ip || "—")}</td><td data-l="MAC" class="swv-mono">${esc(c.mac)}</td>
            <td data-l="Vendor" class="swv-muted">${esc(c.vendor || "")}</td><td data-l="Name">${esc(c.hostname || "")}</td>
            <td data-l="Port" class="swv-mono">${esc(c.port || c.iface || "—")}</td>
            <td data-l="Seen by" class="swv-dim">${esc((c.src || []).filter((x, i, a) => a.indexOf(x) === i).join(" + "))}</td></tr>`).join("")
          || `<tr><td colspan="6" class="swv-note">nothing seen</td></tr>`}</tbody></table></div>
          <p class="swv-note" style="margin-top:8px">From the router's own DHCP leases, ARP table and bridge host table — nothing is scanned.</p>`;
      } else if (st.tab === "network") {
        const t = (title, head, rows) => rows ? `<h3 style="margin-top:12px">${title}</h3><div class="swv-tablewrap"><table class="swv-t"><thead><tr>${head.map((h) => `<th>${h}</th>`).join("")}</tr></thead><tbody>${rows}</tbody></table></div>` : "";
        host.innerHTML =
          t("Addresses", ["Address", "Interface"], (d.addresses || []).map((a) => `<tr><td data-l="Address" class="swv-mono">${esc(a.address)}</td><td data-l="Iface" class="swv-mono swv-muted">${esc(a.interface)}</td></tr>`).join(""))
          + t("Routes", ["Destination", "Gateway", "Dist", ""], (d.routes || []).map((r) => `<tr><td data-l="Dst" class="swv-mono">${esc(r.dst)}</td><td data-l="Gateway" class="swv-mono">${esc(r.gateway || "")}</td><td data-l="Dist" class="swv-dim">${esc(r.distance)}</td><td data-l="State">${r.active ? `<span class="swv-b ok">active</span>` : `<span class="swv-b">inactive</span>`}</td></tr>`).join(""))
          + `<h3 style="margin-top:12px">DNS</h3><p class="swv-mono swv-muted" style="margin:0">${esc((d.dns || {}).servers || "—")}</p>`
          + t("Firewall", ["Chain", "Action", "Match", "Comment"], ((d.firewall || {}).filter || []).map((f) => `<tr><td data-l="Chain" class="swv-mono">${esc(f.chain)}</td><td data-l="Action"><span class="swv-b ${f.action === "drop" || f.action === "reject" ? "bad" : "ok"}">${esc(f.action)}</span></td><td data-l="Match" class="swv-dim">${esc([f.protocol, f.dst_port].filter(Boolean).join("/"))}</td><td data-l="Comment" class="swv-muted">${esc(f.comment || "")}</td></tr>`).join(""))
          + t("Wireless clients", ["MAC", "Interface", "Signal", "Tx/Rx"], (((d.wireless || {}).registrations) || []).map((w) => `<tr><td data-l="MAC" class="swv-mono">${esc(w.mac)}</td><td data-l="Iface" class="swv-mono swv-muted">${esc(w.interface)}</td><td data-l="Signal">${esc(w.signal)}</td><td data-l="Rate" class="swv-dim">${esc(w.tx_rate)}/${esc(w.rx_rate)}</td></tr>`).join(""));
      } else if (st.tab === "logs") {
        host.innerHTML = `<pre class="swv-mono" style="margin:0;max-height:52vh;overflow:auto;white-space:pre-wrap;font-size:12px">${(d.logs || []).map((l) => esc(l.time) + "  " + esc(l.topics) + "  " + esc(l.message)).join("\n") || "no log entries"}</pre>`;
      } else {
        const locked = d.manage_enabled === false;
        host.innerHTML = `<pre id="rv-term" class="swv-mono" style="margin:0 0 8px;height:44vh;overflow:auto;white-space:pre-wrap;background:#050b16;border:1px solid rgba(148,163,184,.18);border-radius:10px;padding:10px;font-size:12px;color:#a7f3d0">${st.hist.map(esc).join("")}</pre>
          <div class="swv-row" style="flex-wrap:nowrap"><span class="swv-mono" style="color:#a5b4fc">&gt;</span>
            <input class="swv-in swv-mono" id="rv-cmd" style="flex:1" placeholder="RouterOS command…" ${locked ? "disabled" : ""}>
            <button class="swv-btn pri" data-x="run" ${locked ? "disabled" : ""}>Run</button></div>
          <div class="swv-row" style="margin-top:6px">${["/system resource print", "/ip dhcp-server lease print", "/interface print stats", "/ip service print", "/log print"].map((q) => `<button class="swv-btn sm" data-q="${esc(q)}" ${locked ? "disabled" : ""}>${esc(q.replace(/^\//, "").split(" ").slice(0, 2).join(" "))}</button>`).join("")}</div>
          <p class="swv-note" style="margin:6px 0 0">Runs as <b>admin</b> on the live router by MAC (MAC-Telnet) and is written to this device's history. Commands that could strand the router ask first.</p>`;
      }
    }

    async function runCmd(cmd, confirmDanger) {
      if (!o.runCmd) return;
      st.hist.push("> " + cmd + "\n");
      try {
        const r = await o.runCmd(cmd, !!confirmDanger);
        if (r && r.needs_confirm) {
          st.hist.pop();
          if (await o.confirm("Run this anyway?", r.warning, { danger: true })) return runCmd(cmd, true);
          return;
        }
        st.hist.push((r && r.output ? r.output : "(no output)") + "\n\n");
      } catch (e) {
        const b = e.body || {};
        if (b.needs_confirm) {
          st.hist.pop();
          if (await o.confirm("Run this anyway?", b.warning, { danger: true })) return runCmd(cmd, true);
          return;
        }
        st.hist.push("error: " + ((b && b.error) || e.message || e) + "\n\n");
      }
      if (st.hist.length > 240) st.hist.splice(0, 120);
      const t = root.querySelector("#rv-term");
      if (t) { t.textContent = st.hist.join(""); t.scrollTop = t.scrollHeight; }
    }

    function bind() {
      root.querySelectorAll("[data-port]").forEach((b) => {
        if (b.tagName !== "BUTTON" || b.dataset.x) return;
        b.onclick = () => { st.sel = st.sel === b.dataset.port ? null : b.dataset.port; draw(); };
      });
      const tabs = root.querySelector("#rv-tabs");
      if (tabs) tabs.onclick = (e) => { const b = e.target.closest("[data-tab]"); if (!b) return; st.tab = b.dataset.tab; draw(); };
      const q = root.querySelector("#rv-q");
      if (q) q.oninput = () => { st.q = q.value.toLowerCase(); drawTab(); const n = root.querySelector("#rv-q"); if (n) { n.focus(); n.setSelectionRange(n.value.length, n.value.length); } };
      root.querySelectorAll("[data-q]").forEach((b) => b.onclick = () => { const c = root.querySelector("#rv-cmd"); if (c) { c.value = b.dataset.q; c.focus(); } });
      const cmd = root.querySelector("#rv-cmd");
      const go = () => { const v = (cmd.value || "").trim(); if (!v) return; cmd.value = ""; runCmd(v); };
      if (cmd) cmd.onkeydown = (e) => { if (e.key === "Enter") go(); };
      root.querySelectorAll("[data-x]").forEach((b) => b.onclick = async () => {
        const x = b.dataset.x;
        if (x === "refresh") return refresh();
        if (x === "login") return o.onLogin && o.onLogin();
        if (x === "run") return go();
        if (x === "rename") {
          const n = window.prompt("New name for this router (its RouterOS identity):", (st.data.system || {}).identity || "");
          if (n) act({ action: "set-identity", name: n }, b, "Renamed");
          return;
        }
        if (x === "reboot") {
          if (await o.confirm("Reboot this router?", "It goes offline for about a minute and everything behind it loses connection.", { danger: true })) act({ action: "reboot" }, b, "Rebooting");
          return;
        }
        if (x === "export") {
          b.disabled = true;
          try {
            const r = await o.action({ action: "export" });
            const w = root.querySelector("#rv-tabbody");
            st.tab = "logs";
            draw();
            const host = root.querySelector("#rv-tabbody");
            if (host) host.innerHTML = `<pre class="swv-mono" style="margin:0;max-height:52vh;overflow:auto;white-space:pre-wrap;font-size:12px">${esc((r && r.config) || "(empty)")}</pre>`;
          } catch (e) { o.toast((e.body && e.body.error) || e.message, "bad"); }
          finally { b.disabled = false; }
          return;
        }
        if (x === "cycle") {
          if (await o.confirm(`Power cycle ${b.dataset.port}?`, "PoE on that port goes off for a few seconds and comes back — the camera or AP plugged into it restarts.", { danger: true }))
            act({ action: "poe-cycle", port: b.dataset.port, duration: 5 }, b);
          return;
        }
        if (x === "poe") return act({ action: "poe", id: b.dataset.port, mode: b.dataset.mode }, b);
        if (x === "iface") return act({ action: "interface", id: b.dataset.port, enable: b.dataset.enable === "1" }, b);
      });
    }

    refresh();
    st.timer = setInterval(() => { if (!document.hidden && st.tab !== "terminal") refresh(true); }, 60000);
    return { refresh: () => refresh(), destroy() { st.dead = true; clearInterval(st.timer); } };
  }

  window.RouterView = { mount, summaryCard, injectCss, fmt: { bps, bytes, ago } };
})();
