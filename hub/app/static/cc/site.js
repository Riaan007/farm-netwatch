/* Netwatch Control Center — one site's workspace + the device drawer.
 * Tabs: overview · devices · problems · health · history · backups · access. */
(function () {
  "use strict";
  const { $, $$, esc, icon, api, state: S } = CC;
  const view = () => $("#view");
  const enc = encodeURIComponent;
  const TABS = [
    ["overview", "Overview"], ["devices", "Devices"], ["map", "Map"], ["wifi", "Wi-Fi"], ["switches", "Managed"], ["problems", "Problems"], ["health", "Pi health"],
    ["history", "History"], ["backups", "Backups"], ["access", "Remote access"],
  ];

  // per-site data fetched on demand (the fleet loader keeps devices, internet, wifi, backups)
  const L = { sysinfo: {}, reach: {}, conflicts: {}, kuma: {}, tunnels: {} };
  const fresh = (m, id, ttl) => m[id] && CC.now() - m[id].__at < ttl;
  async function get(m, id, path, ttl, opt) {
    if (fresh(m, id, ttl)) return m[id];
    const j = await api(path, opt);
    m[id] = { ...j, __at: CC.now() };
    return m[id];
  }

  // ---- header actions ----------------------------------------------------------------
  async function sshPi(s, btn) {
    await CC.busy(btn, async () => {
      try { tunnelResult(await api(`/api/hub/sites/${s.id}/pi-ssh`, { method: "POST" }), `SSH to ${s.name}'s Pi`, "Log in with the Pi's own user and password."); }
      catch (e) { CC.toast(e.message, "bad"); }
    });
  }
  function tunnelResult(j, title, hint) {
    const host = j.host || location.hostname;
    const hp = `${host}:${j.port}`;
    const link = j.scheme ? `${j.scheme}://${hp}` : j.device_port === 22 ? `ssh://${hp}` : "";
    const d = CC.dialog(`<div class="dhd"><div><h2>${esc(title)}</h2><p>${j.tunneled === false ? "Same LAN — connect directly" : "Relay through the hub · only this computer can use it"}</p></div><button class="btn icon ghost" data-close aria-label="Close">${icon("x")}</button></div>
      <div class="dbd"><div class="panel pbd" style="display:grid;gap:8px;text-align:center"><div class="mono" style="font-size:20px">${esc(hp)}</div>
        ${link ? `<a class="btn pri" href="${esc(link)}" target="_blank" rel="noopener">${j.scheme ? "Open in browser" : "Open in PuTTY / SSH"} ${icon("ext")}</a>` : ""}</div>
        <p class="muted" style="margin:0">${esc(hint || "")} ${j.device_port === 22 ? `Command line: <span class="mono">ssh -p ${esc(j.port)} user@${esc(host)}</span>` : ""}</p>
        <p class="note" style="margin:0">Tunnels close by themselves after 10 minutes idle (8 hours at most).</p></div>
      <div class="dft"><button class="btn" id="tr-copy">Copy host:port</button><button class="btn pri" data-close>Done</button></div>`);
    $("#tr-copy", d).onclick = (e) => CC.copy(hp, e.currentTarget);
  }
  CC.tunnelResult = tunnelResult;

  function piPassword(s) {
    const d = CC.dialog(`<div class="dhd"><div><h2>Pi password · ${esc(s.name)}</h2><p>For the site's own Netwatch page</p></div><button class="btn icon ghost" data-close aria-label="Close">${icon("x")}</button></div>
      <form class="dbd" id="pp"><p class="muted" style="margin:0">People need it to see saved device logins or change the Pi's settings. Everyone logged in there now is signed out.</p>
        <label class="fld">New password<input class="inp" type="password" name="a" minlength="8" autocomplete="new-password" required></label>
        <label class="fld">Type it again<input class="inp" type="password" name="b" minlength="8" autocomplete="new-password" required></label>
        <div class="bad note" id="pp-err" aria-live="polite"></div>
        <div class="row" style="justify-content:flex-end"><button type="button" class="btn" data-close>Cancel</button><button class="btn pri">Set password</button></div></form>`);
    $("#pp", d).onsubmit = async (e) => {
      e.preventDefault();
      const f = e.target;
      if (f.a.value !== f.b.value) { $("#pp-err", d).textContent = "The two entries differ."; return; }
      await CC.busy(e.submitter, async () => {
        try { await api(`/api/hub/sites/${s.id}/pi-password`, { method: "POST", body: { password: f.a.value } }); d.close(); CC.toast("Pi password set", "ok"); }
        catch (err) { $("#pp-err", d).textContent = err.message; }
      });
    };
  }

  async function aiReport(s, btn) {
    await CC.busy(btn, async () => {
      CC.toast("Gemini is writing the report — usually 10–30 s…");
      try {
        const r = await api(`/api/hub/sites/${s.id}/report`, { method: "POST", raw: true, timeout: 120000 });
        const type = r.headers.get("Content-Type") || "";
        if (!r.ok || type.includes("json")) {
          const j = await r.json().catch(() => ({}));
          throw new Error((j.error || `Report failed (${r.status})`) + (r.status === 409 ? "" : " — check the Gemini key under Settings → Alerts & AI"));
        }
        const cd = r.headers.get("Content-Disposition") || "";
        const m = cd.match(/filename="?([^";]+)"?/);
        CC.download(await r.blob(), m ? m[1] : `netwatch-${s.id}-report.pdf`);
        CC.toast("Report downloaded", "ok");
      } catch (e) { CC.toast(e.message, "bad"); }
    });
  }

  async function wifiDoctor(s) {
    const d = CC.dialog(`<div class="dhd"><div><h2>Wi-Fi Doctor · ${esc(s.name)}</h2><p>Gemini reads the wireless trends and writes a work list</p></div><button class="btn icon ghost" data-close aria-label="Close">${icon("x")}</button></div>
      <div class="dbd" id="wd"><div class="skel" style="height:24px"></div><div class="skel" style="height:120px"></div><p class="note" style="margin:0">Analysing — usually 10–30 seconds.</p></div>`, { cls: "modal wide" });
    try {
      const j = await api(`/api/hub/sites/${s.id}/wifi-doctor`, { method: "POST", timeout: 120000 });
      const a = j.analysis || {};
      const tone = { healthy: "ok", watch: "warn", degraded: "warn", critical: "bad" }[a.overall] || "unk";
      $("#wd", d).innerHTML = `<div class="row"><span class="b ${tone}">${esc(a.overall || "unknown")}</span><span class="note">${CC.plural(j.radio_count || 0, "radio")} · ${CC.plural(j.finding_count || 0, "monitor finding")} · ${esc(a.model || "")}</span></div>
        <p style="margin:0">${esc(a.summary || "")}</p>
        ${(a.site_actions || []).length ? `<div class="sect"><h3>Do these first</h3><ol style="margin:0;padding-left:20px">${a.site_actions.map((x) => `<li>${esc(x)}</li>`).join("")}</ol></div>` : ""}
        ${(a.links || []).map((l) => `<div class="panel pbd"><div class="row" style="justify-content:space-between"><b>${esc(l.name)}</b><span class="b ${l.severity === "high" ? "bad" : l.severity === "medium" ? "warn" : "unk"}">${esc(l.severity || "")}</span></div>
          <p class="muted" style="margin:6px 0">${esc(l.verdict || "")}</p>
          ${(l.likely_causes || []).length ? `<div class="note">Likely: ${l.likely_causes.map(esc).join(" · ")}</div>` : ""}
          ${(l.actions || []).length ? `<ul style="margin:6px 0 0;padding-left:18px">${l.actions.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}</div>`).join("")}
        ${(a.watch || []).length ? `<div class="sect"><h3>Keep an eye on</h3><ul style="margin:0;padding-left:18px">${a.watch.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>` : ""}`;
    } catch (e) {
      $("#wd", d).innerHTML = `<div class="banner bad">${esc(e.message)}</div><p class="note" style="margin:0">${e.status === 409 ? "This site has no wireless telemetry yet — save SSH logins on its Ubiquiti radios in the site's Netwatch." : "Wi-Fi Doctor needs a Gemini key under Settings → Alerts & AI."}</p>`;
    }
  }

  CC.wifiDoctor = wifiDoctor;

  // ---- site route --------------------------------------------------------------------------
  const site = {
    id: null, tab: "overview",
    enter(a) {
      const [id, tab] = [a.id, a.tab];
      const changed = this.id !== id;
      this.id = id; this.tab = TABS.some((t) => t[0] === tab) ? tab : "overview";
      const s = CC.site(id);
      if (!s) {
        if (!S.loaded) { view().innerHTML = `<div class="skel" style="height:200px"></div>`; this.pending = true; return; }
        view().innerHTML = `<div class="empty"><b>Unknown site</b><a href="#/">Back to overview</a></div>`; return;
      }
      this.pending = false;
      CC.setCrumbs([{ label: "Overview", href: "#/" }, { label: s.name, href: `#/site/${enc(id)}` }, ...(this.tab === "overview" ? [] : [{ label: TABS.find((t) => t[0] === this.tab)[1] }])]);
      if (changed || !$("#st-head")) {
        view().innerHTML = `<div id="st-head"></div><nav class="tabs" id="st-tabs" aria-label="Site sections"></nav><div id="st-body"></div>`;
      }
      this.head(); this.tabs();
      $("#st-body").innerHTML = "";
      this.body = TABS_IMPL[this.tab];
      this.body.enter(s, $("#st-body"));
    },
    update() {
      if (this.pending && CC.site(this.id)) return this.enter({ id: this.id, tab: this.tab });
      const s = CC.site(this.id);
      if (!s || !$("#st-head")) return;
      this.head(); this.tabs();
      if (this.body && this.body.update) this.body.update(s, $("#st-body"));
    },
    head() {
      const s = CC.site(this.id);
      const net = S.internet[s.id];
      const netB = !net || !net.checked_ts ? "" : net.ok ? `<span class="b ok">Internet OK</span>` : net.has_gateway && !net.gateway ? `<span class="b bad">Router down</span>` : !net.dns && net.external ? `<span class="b warn">DNS broken</span>` : `<span class="b warn">No upstream</span>`;
      const c = CC.siteCounts(s);
      $("#st-head").innerHTML = `<div class="ph"><div style="min-width:0"><div class="eyebrow">Site</div>
          <h1 style="display:flex;align-items:center;gap:10px"><i class="dot ${CC.STATE[CC.siteState(s)].dot}"></i>${esc(s.name)}</h1>
          <p class="row" style="gap:8px">${CC.stateBadge(s)} ${netB} <span class="muted">${esc(s.location || "")}</span> <span class="mono dim">${esc(s.vpn_ip)}</span>${s.latency_ms != null ? `<span class="mono dim">${Math.round(s.latency_ms)} ms</span>` : ""}<span class="dim">· ${c.total != null ? `${c.online}/${c.total} online · ` : ""}scan ${CC.ago(s.last_scan_ts)}${s.is_scanning ? " · scanning…" : ""}</span></p></div>
        <div class="actions">
          <button class="btn" data-h="poll" title="Ask the site for fresh data now">${icon("refresh")} Refresh</button>
          <button class="btn" data-h="ssh" title="SSH to the site's Pi over the VPN">${icon("term")} SSH Pi</button>
          <button class="btn" data-h="report" title="PDF site report written by Gemini">${icon("file")} AI report</button>
          <button class="btn" data-h="wifi" title="Gemini analysis of the wireless links">${icon("wifi")} Wi-Fi Doctor</button>
          <button class="btn" data-h="pw" title="Password for the site's own Netwatch page">${icon("lock")} Pi password</button>
          ${s.links && s.links.netwatch ? `<a class="btn" href="${esc(s.links.netwatch)}" target="_blank" rel="noopener" title="Edit names, logins and settings on the site's own page">Site Netwatch ${icon("ext")}</a>` : ""}
          ${s.links && s.links.kuma ? `<a class="btn" href="${esc(s.links.kuma)}" target="_blank" rel="noopener">Kuma ${icon("ext")}</a>` : ""}
        </div></div>
        ${!s.reachable && s.enabled ? `<div class="banner bad">The hub can't reach this site right now${s.error ? ` (${esc(s.error)})` : ""}. Everything below is the last data received${s.fetched_at ? `, ${CC.ago(s.fetched_at)}` : ""}.</div>` : s.stale ? `<div class="banner">Device list is out of date (last refresh ${CC.ago(s.fetched_at)}).</div>` : ""}`;
      $$("[data-h]", $("#st-head")).forEach((b) => (b.onclick = async () => {
        const h = b.dataset.h;
        if (h === "poll") CC.busy(b, async () => { await api(`/api/hub/sites/${s.id}/poll`, { method: "POST" }).catch((e) => CC.toast(e.message, "bad")); await new Promise((r) => setTimeout(r, 2500)); S.devicesAt[s.id] = 0; Object.values(L).forEach((m) => delete m[s.id]); delete S.internet[s.id]; await CC.load(); CC.toast("Refreshed", "ok"); });
        if (h === "ssh") sshPi(s, b);
        if (h === "report") aiReport(s, b);
        if (h === "wifi") wifiDoctor(s);
        if (h === "pw") piPassword(s);
      }));
    },
    tabs() {
      const s = CC.site(this.id);
      const issues = CC.siteIssues(s);
      const probs = (s.conflicts || 0) + (s.rotated || 0);
      const wl = CC.wifiOf(s.id);
      const wn = wl.state === "ok" ? wl.attn + wl.radioIssues.length : 0;
      const swp = CC.switchProblems(s.id).filter((p) => p.level !== "info");
      const counts = { switches: swp.length ? [swp.length, swp.some((p) => p.level === "crit")] : null, overview: issues.length ? [issues.length, issues.some((i) => i.sev === "bad")] : null, problems: probs ? [probs, !!s.conflicts] : null, devices: S.devices[s.id] ? [S.devices[s.id].length, false] : null, wifi: wn ? [wn, wl.crit.length > 0] : null };
      $("#st-tabs").innerHTML = TABS.map(([k, l]) => {
        const c = counts[k];
        return `<a href="#/site/${enc(s.id)}${k === "overview" ? "" : "/" + k}" class="${this.tab === k ? "on" : ""}">${l}${c ? ` <span class="count ${c[1] ? "" : "quiet"}">${c[0]}</span>` : ""}</a>`;
      }).join("");
    },
    leave() { if (this.body && this.body.leave) this.body.leave(); },
  };
  CC.route("/site/:id/:tab", site);
  CC.route("/site/:id", site);

  // ---- tabs -----------------------------------------------------------------------------
  const TABS_IMPL = {};

  TABS_IMPL.overview = {
    enter(s, el) {
      el.innerHTML = `<div class="cols"><div>
          <section class="panel"><div class="phd"><h2>Needs attention</h2></div><div id="so-att"></div></section>
          <section class="panel"><div class="phd"><h2>Devices by type</h2><a class="btn sm ghost" href="#/site/${enc(s.id)}/devices">All devices</a></div><div class="pbd" id="so-types"></div></section>
          <section class="panel"><div class="phd"><h2>Uptime Kuma</h2>${s.links && s.links.kuma ? `<a class="btn sm ghost" href="${esc(s.links.kuma)}" target="_blank" rel="noopener">Open ${icon("ext")}</a>` : ""}</div><div id="so-kuma"><div class="pbd note">Loading…</div></div></section>
        </div><div>
          <section class="panel"><div class="phd"><h2>Site reachability</h2></div><div class="pbd" id="so-reach"><div class="skel" style="height:70px"></div></div></section>
          <section class="panel"><div class="phd"><h2>Pi health</h2><a class="btn sm ghost" href="#/site/${enc(s.id)}/health">Details</a></div><div class="pbd" id="so-pi"><div class="skel" style="height:70px"></div></div></section>
          <section class="panel"><div class="phd"><h2>Recent changes</h2><a class="btn sm ghost" href="#/site/${enc(s.id)}/history">History</a></div><div id="so-ev"><div class="pbd note">Loading…</div></div></section>
        </div></div>`;
      this.update(s, el);
      this.fetch(s);
    },
    async fetch(s) {
      const id = s.id;
      get(L.reach, id, `/api/hub/sites/${id}/reachability`, 60).then(() => this.update(CC.site(id))).catch(() => {});
      get(L.sysinfo, id, `/api/hub/sites/${id}/sysinfo`, 60).then(() => this.update(CC.site(id))).catch(() => {});
      if (s.kuma_state && s.kuma_state !== "off" && s.kuma_state !== "not-configured") get(L.kuma, id, `/api/hub/sites/${id}/kuma`, 60).then(() => this.update(CC.site(id))).catch((e) => { L.kuma[id] = { ok: false, reason: (e.body && e.body.reason) || e.message, __at: CC.now() }; this.update(CC.site(id)); });
      else { L.kuma[id] = { ok: false, reason: "not-configured", __at: CC.now() }; }
      api(`/api/hub/sites/${id}/events?limit=60`).then((j) => { this.events = j.events || []; this.update(CC.site(id)); }).catch(() => { this.events = []; this.update(CC.site(id)); });
    },
    update(s) {
      if (!s || !$("#so-att")) return;
      $("#so-att").innerHTML = CC.issueList(CC.siteIssues(s), { showSite: false });
      const devs = S.devices[s.id];
      $("#so-types").innerHTML = !devs ? `<div class="skel" style="height:60px"></div>` : `<div class="stats">${CC.GROUPS.map(([g, label]) => {
        const ds = devs.filter((d) => CC.cat(d).group === g && CC.devState(d) !== "quiet");
        if (!ds.length) return "";
        const up = ds.filter((d) => d.online).length;
        return `<a class="kpi ${up < ds.length && g !== "unknown" && g !== "other" ? "is-warn" : ""}" href="#/site/${enc(s.id)}/devices?cat=${g}" style="padding:10px 12px"><div class="k">${label}</div><div class="v" style="font-size:22px">${up}<small>/${ds.length}</small></div><div class="s">${up < ds.length ? `${ds.length - up} offline` : "all online"}</div></a>`;
      }).join("")}</div>${devs.some((d) => CC.devState(d) === "quiet") ? `<p class="note" style="margin:10px 0 0">${devs.filter((d) => CC.devState(d) === "quiet").length} devices not seen for 7+ days are left out — <a href="#/site/${enc(s.id)}/devices?state=quiet">see them</a>.</p>` : ""}`;
      const r = L.reach[s.id];
      if (r) $("#so-reach").innerHTML = `${CC.spark(r.series, "spark")}<div class="row" style="justify-content:space-between;margin-top:8px"><span class="note">24 h <b class="${(r.summary.reach_24h || 0) >= 99 ? "ok" : "warn"}">${CC.pct(r.summary.reach_24h, 1)}</b></span><span class="note">7 d <b>${CC.pct(r.summary.reach_7d, 1)}</b></span><span class="note">30 d <b>${CC.pct(r.summary.reach_30d, 1)}</b></span></div><p class="note" style="margin:6px 0 0">Share of hub polls this site answered. Last answered ${CC.ago(r.summary.last_reachable)}.</p>`;
      const p = L.sysinfo[s.id];
      if (p) $("#so-pi").innerHTML = p.sysinfo && p.sysinfo.ts ? piSummary(p.sysinfo, p.level) : `<p class="note" style="margin:0">No health data yet.</p>`;
      const k = L.kuma[s.id];
      if (k) $("#so-kuma").innerHTML = !k.ok ? `<div class="pbd note">${k.reason === "no-status-page" ? "No status page yet — create one with slug “farm” in this site's Kuma to see monitors here." : k.reason === "not-configured" ? "Kuma isn't set up for this site." : k.reason === "unreachable" ? "The hub can't reach this site's Kuma." : "Kuma unavailable (" + esc(k.reason || "unknown") + ")."}</div>` :
        `<ul class="alist">${(k.monitors || []).map((m) => `<li><span class="sev ${m.last_status === 1 ? "info" : m.last_status === 0 ? "bad" : "unk"}">${m.last_status === 1 ? "↑" : m.last_status === 0 ? "↓" : "?"}</span><div style="min-width:0"><div class="t">${esc(m.name)}</div><div class="d">${esc(m.group || "")}${m.uptime_24h != null ? ` · ${CC.pct(m.uptime_24h, 1)} 24 h` : ""}${m.ping != null ? ` · ${Math.round(m.ping)} ms` : ""}</div></div><div class="beats" style="width:120px">${(m.beats || []).slice(-24).map((b) => `<i class="${b.status === 1 || b === 1 ? "" : b.status === 0 || b === 0 ? "x" : "n"}"></i>`).join("")}</div></li>`).join("") || `<li><div class="d">No monitors on the status page.</div></li>`}</ul>`;
      if (this.events) $("#so-ev").innerHTML = eventsList(this.events, { compact: true });
    },
  };

  const EV = { new: ["New device", "info"], offline: ["Went offline", "bad"], online: ["Came online", "ok"], ip_change: ["IP changed", "warn"], replaced: ["Address taken over", "warn"], switch: ["Switch", "info"] };
  function eventsList(evs, { compact = false } = {}) {
    if (!evs.length) return `<div class="empty"><b>No events</b>Nothing recorded for this filter.</div>`;
    // The same device flipping the same way every scan (phones with private
    // MACs, a Pi on two interfaces) would bury everything else: fold repeats
    // into their newest occurrence with a count.
    const merged = [], seen = new Map();
    for (const e of evs) {
      const k = [e.type, e.ip, e.key, (e.detail || {}).switch_event, (e.detail || {}).action, (e.detail || {}).port].join("|");
      const first = seen.get(k);
      if (first) { first.__n = (first.__n || 1) + 1; first.__first = e.ts; continue; }
      const row = { ...e }; seen.set(k, row); merged.push(row);
    }
    const rows = compact ? merged.slice(0, 8) : merged;
    return `<ul class="alist">${rows.map((e) => {
      const [label, tone] = EV[e.type] || [e.type, "unk"];
      const who = e.name || (e.detail && e.detail.type_label) || e.vendor || e.hostname || e.mac || "Unknown device";
      const det = e.detail || {};
      const extra = e.type === "switch" && CC.switchEventText ? esc(CC.switchEventText(e)) : compact ? "" : [e.mac, e.hostname, det.model, det.ports, det.prev_name || det.prev_vendor || det.prev_mac ? "replaced " + (det.prev_name || det.prev_vendor || det.prev_mac) : ""].filter(Boolean).map(esc).join(" · ");
      return `<li><span class="b ${tone === "info" ? "info" : tone} nodot" style="min-width:${compact ? 0 : 128}px;justify-content:center">${label}</span><div style="min-width:0"><div class="t" style="font-weight:600">${esc(who)} <span class="mono dim">${esc(e.ip || "")}</span></div>${extra ? `<div class="d">${extra}</div>` : ""}</div><span class="note" title="${esc(CC.when(e.ts))}" style="text-align:right;white-space:nowrap">${CC.ago(e.ts)}${e.__n ? `<br><span class="b warn nodot" title="Repeated since ${esc(CC.when(e.__first))}">×${e.__n}</span>` : ""}</span></li>`;
    }).join("")}</ul>`;
  }

  function meter(label, val, unit, warn, crit, sub) {
    const tone = val == null ? "" : val >= crit ? "bad" : val >= warn ? "warn" : "ok";
    const w = val == null ? 0 : Math.min(100, unit === "%" ? val : (val / crit) * 100);
    return `<div class="meter"><div class="top"><span>${label}</span><b class="${tone}">${val == null ? "—" : (Math.round(val * 10) / 10) + (unit === "°C" ? " °C" : unit)}</b></div><div class="bar"><i style="width:${w}%;background:${tone === "bad" ? "var(--bad)" : tone === "warn" ? "var(--warn)" : "linear-gradient(90deg,var(--ok),var(--cyan))"}"></i></div>${sub ? `<div class="note">${sub}</div>` : ""}</div>`;
  }
  function worstDisk(si) { return (si.disks || []).slice().sort((a, b) => (b.used_pct || 0) - (a.used_pct || 0))[0]; }
  function piSummary(si, level) {
    const dk = worstDisk(si) || {};
    return `<div class="row" style="justify-content:space-between;margin-bottom:10px"><span class="muted">${esc((si.model || "").replace("Raspberry Pi", "Pi"))}</span><span class="b ${level === "crit" ? "bad" : level === "warn" ? "warn" : "ok"}">${level === "crit" ? "Critical" : level === "warn" ? "Warning" : "Healthy"}</span></div>
      <div style="display:grid;gap:10px">${meter("Temperature", si.temp_c, "°C", 75, 82)}${meter("CPU", si.cpu_pct, "%", 80, 95)}${meter("Memory", si.mem && si.mem.used_pct, "%", 90, 97)}${meter("Disk", dk.used_pct, "%", 85, 95, dk.free_gb != null ? `${dk.free_gb} GB free` : "")}</div>
      <p class="note" style="margin:10px 0 0">Up ${CC.dur(si.uptime_s)} · checked ${CC.ago(si.ts)}</p>`;
  }

  TABS_IMPL.devices = {
    enter(s, el) {
      el.innerHTML = `<section class="panel" id="sd-panel"></section><p class="note">Names, categories, watch flags and saved logins are edited on the <a href="${esc((s.links && s.links.netwatch) || "#")}" target="_blank" rel="noopener">site's own Netwatch page</a>.</p>`;
      CC.deviceTable.mount($("#sd-panel"), { siteId: s.id });
    },
    update(s) { const el = $("#sd-panel"); if (el && el.__st) CC.deviceTable.draw(el, el.__st, s.id); },
  };

  TABS_IMPL.map = {
    enter(s, el) { el.innerHTML = `<div id="sm-host"></div>`; CC.mapView.mount($("#sm-host"), { siteId: s.id }); },
    update() { CC.mapView.softDraw(); },
    leave() { CC.mapView.destroy(); },
  };

  TABS_IMPL.wifi = {
    enter: (s, el) => CC.wifiTab.enter(s, el),
    update: (s) => CC.wifiTab.update(s),
    leave: () => CC.wifiTab.leave(),
  };

  TABS_IMPL.switches = {
    enter: (s, el) => CC.switchTab.enter(s, el),
    update: (s) => CC.switchTab.update(s),
    leave: () => CC.switchTab.leave(),
  };

  TABS_IMPL.problems = {
    enter(s, el) {
      el.innerHTML = `<section class="panel"><div class="phd"><h2>IP address conflicts</h2><small class="note" id="sp-src"></small></div><div id="sp-conf"><div class="pbd"><div class="skel" style="height:60px"></div></div></div></section>
        <section class="panel"><div class="phd"><h2>Wireless links</h2><small class="note">from the site's radio monitor</small></div><div id="sp-wifi"></div></section>`;
      $("#sp-conf").onclick = (e) => { const b = e.target.closest("button[data-ip]"); if (b) this.clear(s, b.dataset.ip, b.dataset.soft === "1", b); };
      this.load(s);
    },
    async load(s) {
      try { L.conflicts[s.id] = { ...(await api(`/api/hub/sites/${s.id}/conflicts`)), __at: CC.now() }; } catch (e) { L.conflicts[s.id] = { conflicts: [], error: e.message, __at: CC.now() }; }
      this.update(s);
    },
    async clear(s, ip, soft, btn) {
      if (!(await CC.confirm(`Clear ${ip}?`, soft ? "Forget the earlier owners of this address and re-test it. It comes back only if two devices use it again." : "Mark this conflict as dealt with and re-test the address now. If both devices still answer, it returns on the next scan.", { ok: "Clear & re-test" }))) return;
      await CC.busy(btn, async () => {
        try { await api(`/api/hub/sites/${s.id}/conflicts/clear`, { method: "POST", body: { ip } }); CC.toast(`Re-testing ${ip}…`, "ok"); setTimeout(() => this.load(s), 1500); }
        catch (e) { CC.toast(e.message, "bad"); }
      });
    },
    update(s) {
      if (!$("#sp-conf")) return;
      const c = L.conflicts[s.id];
      if (c) {
        $("#sp-src").textContent = c.error ? "" : c.source === "site" ? `live from the site · ${CC.ago(c.fetched_at)}` : `from cached devices · ${CC.ago(c.fetched_at)}`;
        const live = (c.conflicts || []).filter((x) => x.kind !== "identity_rotated");
        const soft = (c.conflicts || []).filter((x) => x.kind === "identity_rotated");
        const row = (x, isSoft) => `<li><span class="sev ${isSoft ? "unk" : "warn"}">${isSoft ? "↻" : "!"}</span><div style="min-width:0"><div class="t mono">${esc(x.ip)}</div><div class="chips" style="margin-top:4px">${(x.devices || []).map((d) => `<span class="b ${d.online ? "ok" : "unk"} nodot">${esc(d.name || d.vendor || "Unknown")} · ${esc(d.mac || "")}${d.online ? "" : " · " + CC.ago(d.last_seen)}</span>`).join("")}</div></div><button class="btn sm ${isSoft ? "" : "good"}" data-ip="${esc(x.ip)}" data-soft="${isSoft ? 1 : 0}">Clear & re-test</button></li>`;
        $("#sp-conf").innerHTML = c.error ? `<div class="pbd bad">${esc(c.error)}</div>` : !live.length && !soft.length ? `<div class="empty"><b>No conflicts</b>Every address belongs to one device.</div>` :
          `${live.length ? `<div class="pbd note" style="padding-bottom:0">Live: two devices answer on the same address at the same time.</div><ul class="alist">${live.map((x) => row(x, false)).join("")}</ul>` : ""}
           ${soft.length ? `<div class="pbd note" style="padding-bottom:0;border-top:1px solid var(--line)">Address reuse: different devices held it in turn (DHCP, phones with private MACs) — usually harmless.</div><ul class="alist">${soft.map((x) => row(x, true)).join("")}</ul>` : ""}`;
      }
      const w = S.wifi[s.id];
      const wl = CC.wifiOf(s.id);
      if (wl.state === "ok") {
        const bad = [...wl.crit, ...wl.warn];
        $("#sp-wifi").innerHTML = `<div class="pbd note" style="padding-bottom:0">${wl.summary.good}/${wl.summary.links} links healthy · ${wl.summary.radios_read}/${wl.summary.radios} radios read · <a href="#/site/${enc(s.id)}/wifi">diagnosis, charts and what to do</a></div>` +
          (bad.length || wl.radioIssues.length ? `<ul class="alist">${wl.radioIssues.map((f) => `<li><span class="sev warn">!</span><div style="min-width:0"><div class="t">${esc(f.title)}</div><div class="d">${esc(f.radio.name)} · ${esc((f.steps[0] || {}).text || "")}</div></div><a class="btn sm" href="#/site/${enc(s.id)}/wifi">Open</a></li>`).join("")}${bad.map((l) => `<li><span class="sev ${l.grade === "crit" ? "bad" : "warn"}">!</span><div style="min-width:0"><div class="t">${esc(l.name)}</div><div class="d">${esc(((l.findings || [])[0] || {}).title || "")}</div></div><a class="btn sm" href="#/site/${enc(s.id)}/wifi?link=${enc(l.id)}">Open</a></li>`).join("")}</ul>`
            : `<div class="empty"><b>Links look normal</b>Nothing in the site's link diagnosis needs attention.</div>`);
        return;
      }
      $("#sp-wifi").innerHTML = !w ? `<div class="pbd note">Loading…</div>` : !w.ok ? `<div class="pbd note">No wireless telemetry — the site reads Ubiquiti radios that have a saved SSH login.</div>` :
        `<div class="pbd note" style="padding-bottom:0">${Object.keys(w.radios || {}).length} radios read · <a href="#/site/${enc(s.id)}/wifi">Wi-Fi tab</a></div>` +
        ((w.problems || []).length ? `<ul class="alist">${w.problems.map((p) => `<li><span class="sev ${p.level === "crit" ? "bad" : "warn"}">!</span><div style="min-width:0"><div class="t">${esc(p.what)}</div><div class="d">${esc(p.hint || "")} · ${esc(p.ip || "")} · ${CC.ago(p.ts)}</div></div></li>`).join("")}</ul>` : `<div class="empty"><b>Links look normal</b>Every radio is within its own usual range.</div>`);
    },
  };

  TABS_IMPL.health = {
    enter(s, el) {
      el.innerHTML = `<div class="cols"><div><section class="panel"><div class="phd"><h2>Site Pi</h2><small class="note" id="sh-at"></small></div><div class="pbd" id="sh-main"><div class="skel" style="height:140px"></div></div></section></div>
        <div><section class="panel"><div class="phd"><h2>Internet from the site</h2></div><div class="pbd" id="sh-net"></div></section>
        <section class="panel"><div class="phd"><h2>Storage & system</h2></div><div class="pbd" id="sh-sys"></div></section></div></div>`;
      delete L.sysinfo[s.id];
      get(L.sysinfo, s.id, `/api/hub/sites/${s.id}/sysinfo`, 30).then(() => this.update(CC.site(s.id))).catch((e) => { $("#sh-main").innerHTML = `<div class="bad">${esc(e.message)}</div>`; });
      this.update(s);
    },
    update(s) {
      if (!$("#sh-main")) return;
      const n = S.internet[s.id];
      $("#sh-net").innerHTML = !n ? `<span class="note">Not checked yet.</span>` : `<dl class="kv"><dt>Farm router</dt><dd>${n.has_gateway ? (n.gateway ? `<span class="b ok">Answers</span>` : `<span class="b bad">No answer</span>`) : "—"}</dd><dt>Internet</dt><dd>${n.external ? `<span class="b ok">Reachable</span>` : `<span class="b bad">Unreachable</span>`}</dd><dt>DNS</dt><dd>${n.dns ? `<span class="b ok">Working</span>` : `<span class="b bad">Failing</span>`}</dd><dt>Checked</dt><dd>${CC.ago(n.checked_ts)}</dd></dl>`;
      const p = L.sysinfo[s.id];
      if (!p) return;
      const si = p.sysinfo;
      if (!si || !si.ts) { $("#sh-main").innerHTML = `<p class="note" style="margin:0">This site hasn't reported health data yet (older Netwatch image, or the Pi is unreachable).</p>`; return; }
      $("#sh-at").textContent = "checked " + CC.ago(si.ts);
      const th = si.throttled || {};
      const dk = worstDisk(si) || {};
      const days = Math.min(...(si.disks || []).map((d) => (d.trend && d.trend.days_to_full) || Infinity));
      $("#sh-main").innerHTML = piSummary(si, p.level) + `<div class="stats" style="margin-top:14px">
        <div><div class="note">Load</div><b class="mono">${(si.load || []).map((x) => (+x).toFixed(1)).join(" · ") || "—"}</b></div>
        <div><div class="note">Power</div><b class="${th.undervoltage_now ? "bad" : th.throttled_now ? "warn" : "ok"}">${th.undervoltage_now ? "Undervoltage now" : th.throttled_now ? "Throttled" : th.undervoltage_ever ? "OK (dipped since boot)" : "OK"}</b></div>
        <div><div class="note">Clock</div><b class="${Math.abs((si.clock || {}).offset_s || 0) >= 600 ? "bad" : Math.abs((si.clock || {}).offset_s || 0) >= 120 ? "warn" : "ok"}">${si.clock && si.clock.offset_s != null ? `${Math.round(si.clock.offset_s)} s off` : "—"}</b></div>
        <div><div class="note">Disk full in</div><b class="${days <= 5 ? "bad" : days <= 14 ? "warn" : ""}">${isFinite(days) ? `~${Math.round(days)} days` : "not filling"}</b></div></div>`;
      const sto = si.storage || {};
      const fsBad = (sto.filesystems || []).some((f) => f.ro || (f.write_test && f.write_test.ok === false));
      const ext4 = (sto.ext4_errors || []).reduce((a, e) => a + (e.count || 0), 0);
      const slow = (sto.filesystems || []).some((f) => f.write_test && f.write_test.fsync_ms > 5000);
      const card = (sto.cards || [])[0];
      const ct = si.containers || {};
      const smart = si.smart || {};
      $("#sh-sys").innerHTML = `<dl class="kv">
        <dt>Storage</dt><dd>${fsBad ? `<span class="b bad">Failing (read-only)</span>` : ext4 ? `<span class="b bad">${ext4} filesystem errors</span>` : sto.io_errors_new ? `<span class="b warn">I/O errors</span>` : slow ? `<span class="b warn">Slow writes</span>` : `<span class="b ok">OK</span>`}</dd>
        <dt>Disk</dt><dd>${dk.used_pct != null ? `${dk.used_pct}% used · ${dk.free_gb} GB free` : "—"}</dd>
        ${card ? `<dt>SD card</dt><dd>${card.age_years != null ? `${card.age_years} years old` : "present"}${card.age_years > 5 ? ` <span class="b warn">old</span>` : ""}</dd>` : ""}
        ${smart.available ? `<dt>SMART</dt><dd>${(smart.devices || []).every((d) => d.healthy) ? `<span class="b ok">Healthy</span>` : `<span class="b bad">Failing</span>`}</dd>` : ""}
        <dt>Containers</dt><dd>${ct.available ? `${(ct.containers || []).length} running${ct.flapping && ct.flapping.length ? ` · <span class="b warn">restarting: ${esc(ct.flapping.join(", "))}</span>` : ""}` : "—"}</dd>
        <dt>Watchdog</dt><dd>${(si.watchdog || {}).active ? "Armed" : (si.watchdog || {}).enabled ? "Host-owned" : "Off"}</dd>
        <dt>Uptime</dt><dd>${CC.dur(si.uptime_s)}</dd></dl>`;
    },
  };

  TABS_IMPL.history = {
    enter(s, el) {
      el.innerHTML = `<section class="panel"><div class="tbar">
          <label class="search">${icon("search")}<span class="sr">Filter</span><input class="inp" id="sh-q" type="search" placeholder="IP, name, vendor, MAC" title="${esc(CC.IP_SEARCH_HELP)}"></label>
          <select class="sel" id="sh-type" aria-label="Event type"><option value="">All events</option><option value="new">New devices</option><option value="offline">Went offline</option><option value="online">Came online</option><option value="ip_change">IP changes</option></select>
          <select class="sel" id="sh-mode" aria-label="View"><option value="events">Timeline</option><option value="ips">By IP address</option></select>
          <span class="note" id="sh-n" style="margin-left:auto"></span></div><div id="sh-list"><div class="pbd"><div class="skel" style="height:120px"></div></div></div></section>`;
      const p = CC.params();
      if (p.get("q")) $("#sh-q").value = p.get("q");
      // a full address is asked of the site (its whole history, not just the newest 500)
      let lastIp = CC.isFullIp($("#sh-q").value) ? $("#sh-q").value.trim() : "";
      $("#sh-q").oninput = CC.debounce(() => {
        const v = $("#sh-q").value.trim(), ip = CC.isFullIp(v) ? v : "";
        if (ip !== lastIp && $("#sh-mode").value === "events") { lastIp = ip; this.load(s); } else this.draw();
      }, 250);
      $("#sh-type").onchange = () => this.load(s);
      $("#sh-mode").onchange = () => this.load(s);
      $("#sh-list").onclick = (e) => { const r = e.target.closest("[data-ip]"); if (r) { $("#sh-mode").value = "events"; $("#sh-q").value = r.dataset.ip; this.load(s); } };
      this.load(s);
    },
    async load(s) {
      const mode = $("#sh-mode").value;
      const seq = (this.seq = (this.seq || 0) + 1);       // a slower older request must not overwrite a newer one
      $("#sh-n").textContent = "Loading…";
      try {
        if (mode === "ips") this.ips = (await api(`/api/hub/sites/${s.id}/ip-history`)).ips || [];
        else {
          const v = $("#sh-q").value.trim();
          const ip = CC.isFullIp(v) ? "&ip=" + encodeURIComponent(v) : "";
          const ev = (await api(`/api/hub/sites/${s.id}/events?limit=500${$("#sh-type").value ? "&type=" + $("#sh-type").value : ""}${ip}`)).events || [];
          if (seq !== this.seq) return;
          this.events = ev;
        }
      } catch (e) { $("#sh-list").innerHTML = `<div class="pbd bad">${esc(e.message)}</div>`; return; }
      this.draw();
    },
    draw() {
      if (!$("#sh-list")) return;
      const q = $("#sh-q").value.trim().toLowerCase();
      const ipMatch = CC.ipMatcher(q);
      const hit = (x) => !q || (ipMatch ? ipMatch(x.ip) : [x.ip, x.name, x.vendor, x.hostname, x.mac].join(" ").toLowerCase().includes(q));
      if ($("#sh-mode").value === "ips") {
        const rows = (this.ips || []).filter(hit).sort((a, b) => (ipMatch ? CC.ipNum(a.ip) - CC.ipNum(b.ip) : 0));
        $("#sh-n").textContent = CC.plural(rows.length, "address", "addresses");
        $("#sh-list").innerHTML = `<div class="twrap"><table class="t"><thead><tr><th>IP</th><th>Last device</th><th class="num">Devices</th><th class="num hide-m">Events</th><th>Last change</th></tr></thead><tbody>${rows.map((r) => `<tr data-ip="${esc(r.ip)}" tabindex="0"><td class="mono">${esc(r.ip)}</td><td>${esc(r.name || r.vendor || r.hostname || r.mac || "—")}</td><td class="num">${r.device_count}${r.device_count > 1 ? ` <span class="b warn nodot">shared</span>` : ""}</td><td class="num hide-m">${r.event_count}</td><td>${esc((EV[r.last_type] || [r.last_type])[0])} · ${CC.ago(r.last_ts)}</td></tr>`).join("")}</tbody></table></div>`;
      } else {
        const rows = (this.events || []).filter(hit);
        $("#sh-n").textContent = CC.plural(rows.length, "event");
        $("#sh-list").innerHTML = (!rows.length && q
          ? `<div class="empty"><b>No events for ${esc($("#sh-q").value.trim())}</b>${ipMatch ? "Nothing has joined, left or changed on this address in the site's history — it has been stable." : "Nothing matches this search."}</div>`
          : eventsList(rows.slice(0, 300))) + (rows.length > 300 ? `<div class="pbd note">Showing the newest 300 — filter to narrow down.</div>` : "");
      }
    },
  };

  TABS_IMPL.backups = {
    enter(s, el) {
      el.innerHTML = `<section class="panel"><div class="phd"><h2>Config backups <small>encrypted · newest 14 kept</small></h2><div class="row"><button class="btn sm" id="sb-key">${icon("key")} Backup key</button><button class="btn sm pri" id="sb-now" ${s.reachable ? "" : "disabled"}>${icon("save")} Back up now</button></div></div><div id="sb-list"></div>
        <div class="pbd note" style="border-top:1px solid var(--line)">Rebuilding on a new Pi: enrol it with the site's setup command (Settings → Sites) so the VPN comes up, then Restore here — settings, device names and saved logins come back. Downloaded files open only with the backup key.</div></section>`;
      $("#sb-key").onclick = CC.backupKeyDialog;
      $("#sb-now").onclick = (e) => CC.backupNow(s.id, e.currentTarget);
      $("#sb-list").onclick = async (e) => {
        const b = e.target.closest("button[data-act]"); if (!b) return;
        if (b.dataset.act === "restore") CC.restoreBackup(s.id, b.dataset.name);
        if (b.dataset.act === "del") {
          if (!(await CC.confirm("Delete this backup?", `${b.dataset.name} is removed from the hub for good.`, { ok: "Delete", danger: true }))) return;
          try { await api(`/api/hub/sites/${s.id}/backups/${b.dataset.name}`, { method: "DELETE" }); delete S.backups[s.id]; CC.toast("Deleted", "ok"); await CC.load(); } catch (err) { CC.toast(err.message, "bad"); }
        }
      };
      this.update(s);
    },
    update(s) {
      if (!$("#sb-list")) return;
      const b = S.backups[s.id];
      const list = (b && b.list) || [];
      $("#sb-list").innerHTML = !b ? `<div class="pbd"><div class="skel" style="height:60px"></div></div>` : !list.length ? `<div class="empty"><b>No backups yet</b>The hub takes one daily, or use Back up now.</div>` :
        `<div class="twrap"><table class="t"><thead><tr><th>Taken</th><th class="hide-m">Size</th><th class="hide-m">Encryption</th><th></th></tr></thead><tbody>${list.map((x, i) => `<tr style="cursor:default"><td>${CC.when(x.ts)} <span class="note">${CC.ago(x.ts)}${i === 0 ? " · newest" : ""}</span></td><td class="hide-m">${CC.kb(x.bytes)}</td><td class="hide-m">${x.encrypted ? `<span class="b ok nodot">🔒 Encrypted</span>` : `<span class="b bad nodot">Not encrypted</span>`}</td>
          <td class="num"><div class="row" style="justify-content:flex-end;flex-wrap:nowrap"><a class="btn sm ghost" href="/api/hub/sites/${enc(s.id)}/backups/${enc(x.name)}">${icon("down")} Download</a><button class="btn sm good" data-act="restore" data-name="${esc(x.name)}" ${s.reachable ? "" : "disabled"}>Restore</button><button class="btn sm danger" data-act="del" data-name="${esc(x.name)}" aria-label="Delete backup ${esc(x.name)}">${icon("x")}</button></div></td></tr>`).join("")}</tbody></table></div>`;
    },
  };

  TABS_IMPL.access = {
    enter(s, el) {
      el.innerHTML = `<div class="cols"><div><section class="panel"><div class="phd"><h2>Open a tunnel</h2></div><form class="pbd" id="sa-form" style="display:grid;gap:12px">
          <p class="muted" style="margin:0">Reach any device on the farm LAN — its web page, SSH, RDP or camera stream — through the site Pi and the hub. Only the computer that opens a tunnel can use it.</p>
          <div class="grid2"><label class="fld">Device<select class="sel" name="ip" style="width:100%"></select></label><label class="fld">Port<input class="inp mono" name="port" type="number" min="1" max="65535" placeholder="80" required></label></div>
          <div class="chips" id="sa-ports"></div>
          <div class="row" style="justify-content:flex-end"><button type="button" class="btn" id="sa-ssh">${icon("term")} SSH to the Pi itself</button><button class="btn pri">Open tunnel</button></div></form></section></div>
        <div><section class="panel"><div class="phd"><h2>Active tunnels</h2><button class="btn sm ghost" id="sa-reload">${icon("refresh")}</button></div><div id="sa-list"></div></section></div></div>`;
      const f = $("#sa-form");
      const devs = (S.devices[s.id] || []).filter((d) => d.ip && CC.devState(d) !== "quiet").sort((a, b) => (b.online - a.online) || CC.ipNum(a.ip) - CC.ipNum(b.ip));
      const pre = CC.params().get("ip");
      f.ip.innerHTML = devs.map((d) => `<option value="${esc(d.ip)}" ${d.ip === pre ? "selected" : ""}>${esc(d.ip)} · ${esc(CC.devName(d))}${d.online ? "" : " (offline)"}</option>`).join("");
      const ports = () => {
        const d = devs.find((x) => x.ip === f.ip.value) || {};
        const common = [[80, "HTTP"], [443, "HTTPS"], [22, "SSH"], [554, "RTSP"], [8080, "HTTP-alt"], [3389, "RDP"]];
        const scanned = (d.ports || []).map((p) => [p, ((d.services || {})[p] || "").split(" ")[0] || "open"]);
        const all = scanned.concat(common.filter(([p]) => !scanned.some(([q]) => q === p)));
        $("#sa-ports").innerHTML = all.map(([p, l]) => `<button type="button" class="btn sm ${scanned.some(([q]) => q === p) ? "" : "ghost"}" data-port="${p}">${p} <span class="dim">${esc(l)}</span></button>`).join("");
      };
      f.ip.onchange = ports; ports();
      $("#sa-ports").onclick = (e) => { const b = e.target.closest("[data-port]"); if (b) { f.port.value = b.dataset.port; f.requestSubmit(); } };
      f.onsubmit = async (e) => {
        e.preventDefault();
        await CC.busy(e.submitter || f.querySelector(".pri"), async () => {
          try {
            const j = await api(`/api/hub/sites/${s.id}/tunnel`, { method: "POST", body: { ip: f.ip.value, port: +f.port.value }, timeout: 30000 });
            const d = devs.find((x) => x.ip === f.ip.value);
            tunnelResult(j, `${d ? CC.devName(d) : f.ip.value} · port ${j.device_port}`, "");
            this.load(s);
          } catch (err) { CC.toast(err.message, "bad"); }
        });
      };
      $("#sa-ssh").onclick = (e) => sshPi(s, e.currentTarget).then(() => this.load(s));
      $("#sa-reload").onclick = () => this.load(s);
      $("#sa-list").onclick = async (e) => {
        const b = e.target.closest("button[data-tid]"); if (!b) return;
        try { await api(`/api/hub/sites/${s.id}/tunnel/${b.dataset.tid}`, { method: "DELETE" }); this.load(s); } catch (err) { CC.toast(err.message, "bad"); }
      };
      this.load(s);
    },
    async load(s) {
      try { this.list = (await api(`/api/hub/sites/${s.id}/tunnels`)).tunnels || []; } catch (e) { this.list = []; }
      if (!$("#sa-list")) return;
      $("#sa-list").innerHTML = !this.list.length ? `<div class="empty"><b>No open tunnels</b>They close themselves when idle.</div>` :
        `<ul class="alist">${this.list.map((t) => { const hp = `${location.hostname}:${t.hub_port}`; return `<li><span class="sev info">${icon("plug")}</span><div style="min-width:0"><div class="t mono">${esc(t.ip)}:${t.port}</div><div class="d">${t.scheme ? `<a href="${t.scheme}://${esc(hp)}" target="_blank" rel="noopener">${esc(hp)}</a>` : `<span class="mono">${esc(hp)}</span>`} · ${CC.plural(t.conns, "connection")} · opened ${CC.ago(t.created_at)}</div></div><button class="btn sm danger" data-tid="${esc(t.id)}">Close</button></li>`; }).join("")}</ul>`;
    },
  };

  // ---- device drawer ---------------------------------------------------------------------------
  CC.openDevice = (siteId, key) => {
    const s = CC.site(siteId);
    const d = (S.devices[siteId] || []).find((x) => x.key === key);
    if (!s || !d) return;
    const c = CC.cat(d);
    const dlg = CC.dialog(`<div class="dhd"><div style="min-width:0"><div class="eyebrow">${c.icon} ${esc(c.label)} · ${esc(s.name)}</div><h2>${esc(CC.devName(d))}</h2><p class="row" style="gap:6px;margin-top:6px">${CC.devStateBadge(d)}${d.watch ? `<span class="b info nodot">🔔 Watched</span>` : ""}${d.ip_conflict ? `<span class="b warn nodot">IP conflict</span>` : ""}${d.has_credentials ? `<span class="b unk nodot">🔑 Login saved</span>` : ""}${CC.isMikrotik(d) ? `<span class="b vio nodot">MikroTik</span>` : ""}</p></div><button class="btn icon ghost" data-close aria-label="Close">${icon("x")}</button></div>
      <div class="dbd">
        <div class="sect"><h3>Identity</h3><dl class="kv">
          <dt>IP</dt><dd class="mono">${esc(d.ip || "—")}</dd><dt>MAC</dt><dd class="mono">${esc(d.mac || "—")}</dd>
          ${d.device_name ? `<dt>Own name</dt><dd>${esc(d.device_name)} <span class="note">(${d.device_name_src === "nvr" ? "from the NVR" : "set on the device"})</span></dd>` : ""}<dt>Vendor</dt><dd>${esc(d.vendor || "—")}</dd>${d.model ? `<dt>Model</dt><dd>${esc(d.model)}</dd>` : ""}${d.firmware ? `<dt>Firmware</dt><dd class="mono">${esc(d.firmware)}</dd>` : ""}${d.serial ? `<dt>Serial</dt><dd class="mono">${esc(d.serial)}</dd>` : ""}
          ${d.hostname ? `<dt>Hostname</dt><dd class="mono">${esc(d.hostname)}</dd>` : ""}${d.type ? `<dt>Detected as</dt><dd>${esc(d.type)} <span class="note">(${esc(d.confidence || "")} confidence)</span></dd>` : ""}
          <dt>Last seen</dt><dd>${d.online ? "now" : CC.ago(d.last_seen)} <span class="note">${esc(CC.when(d.last_seen))}</span></dd><dt>First seen</dt><dd>${esc(CC.when(d.first_seen))}</dd>
          ${(d.ports || []).length ? `<dt>Open ports</dt><dd class="mono">${d.ports.map((p) => esc(p) + ((d.services || {})[p] ? ` <span class="dim">${esc(d.services[p].split(" ")[0])}</span>` : "")).join(", ")}</dd>` : ""}
          ${d.ip_conflict_with && d.ip_conflict_with.length ? `<dt>Shares IP with</dt><dd>${d.ip_conflict_with.map((x) => esc(x.name || x.vendor || x.mac)).join(", ")}</dd>` : ""}
        </dl></div>
        <div class="sect"><h3>Login</h3><div id="dd-login"></div></div>
        ${CC.isMikrotik(d) ? `<div class="sect"><h3>MikroTik router</h3>
          <div id="dd-mt" class="note">Reading the router…</div>
          <div class="row" style="margin-top:10px"><button class="btn pri" id="dd-mt-console">🖥️ Open MikroTik console</button><button class="btn sm" id="dd-mt-reload">↻ Refresh</button></div>
        </div>` : ""}
        <div class="sect"><h3>Availability</h3><div id="dd-up" class="note">Loading…</div>
          <div class="row" style="margin:10px 0 6px;gap:4px" id="dd-range">${["30m", "1h", "12h", "24h"].map((r) => `<button class="btn sm ${r === "1h" ? "pri" : ""}" data-r="${r}">${r}</button>`).join("")}</div><div id="dd-chart"></div></div>
        <div class="sect"><h3>Location</h3><div id="dd-loc"></div></div>
        ${d.ip ? `<div class="sect"><h3>Diagnostics <span class="dim" style="text-transform:none;letter-spacing:0;font-weight:400">run from the site Pi</span></h3><div class="row"><button class="btn sm" data-dx="ping">Ping</button><button class="btn sm" data-dx="quality">Connection test</button><button class="btn sm" data-dx="tracert">Traceroute</button><button class="btn sm" data-dx="deep">Deep scan</button></div><div id="dd-out" style="margin-top:10px"></div></div>
        <div class="sect"><h3>Remote access</h3><div class="chips" id="dd-ports"></div><p class="note" style="margin:8px 0 0">Opens a tunnel through the site Pi — only this computer can use it.</p></div>` : ""}
      </div>
      <div class="dft">${s.links && s.links.netwatch ? `<a class="btn" href="${esc(s.links.netwatch)}" target="_blank" rel="noopener">Edit on site Netwatch ${icon("ext")}</a>` : ""}<a class="btn" href="#/site/${enc(s.id)}/history?q=${enc(d.ip || d.mac || "")}" data-close>${icon("history")} History</a><button class="btn pri" data-close>Close</button></div>`, { cls: "drawer" });

    if (CC.switchDrawer) CC.switchDrawer(dlg, siteId, d);

    const drawLoc = () => {
      const g = d.geo;
      $("#dd-loc", dlg).innerHTML = `${g ? `<p style="margin:0 0 8px"><span class="b ok nodot">📍 On the map</span> <span class="mono">${CC.fmtLatLon(g)}</span>${g.note ? ` · ${esc(g.note)}` : ""} <span class="note">set ${CC.ago(g.ts)}</span></p>` : `<p class="note" style="margin:0 0 8px">Not on the map yet.</p>`}
        <div class="row" style="flex-wrap:nowrap"><input class="inp mono" id="dd-loc-in" placeholder="-33.924868, 18.424055 or a Google Maps link" value="${g ? CC.fmtLatLon(g) : ""}"><button class="btn" id="dd-loc-save">Save</button></div>
        <input class="inp" id="dd-loc-note" style="margin-top:6px" maxlength="120" placeholder="Where exactly (optional) — e.g. 6 m pole at the pump house" value="${esc((g && g.note) || "")}">
        <div class="row" style="margin-top:8px"><a class="btn sm" href="#/site/${enc(siteId)}/map?place=${enc(key)}" data-close>🗺️ Pick on the map</a>
          ${g ? `<a class="btn sm" href="#/site/${enc(siteId)}/map?focus=${enc(key)}" data-close>Show on map</a><a class="btn sm" href="${CC.directions(g)}" target="_blank" rel="noopener">Directions ${icon("ext")}</a><button class="btn sm danger" id="dd-loc-clear">Remove</button>` : ""}
          <span class="note" id="dd-loc-msg"></span></div>`;
      const msg = (t, bad) => { const m = $("#dd-loc-msg", dlg); m.textContent = t; m.className = "note " + (bad ? "bad" : "ok"); };
      $("#dd-loc-save", dlg).onclick = (e) => CC.busy(e.currentTarget, async () => {
        const pos = CC.parseLatLon($("#dd-loc-in", dlg).value);
        if (!pos) return msg("Not a position — use e.g. -33.924868, 18.424055 or a Google Maps link.", true);
        try { await CC.setLocation(siteId, key, { ...pos, note: $("#dd-loc-note", dlg).value }); drawLoc(); msg("✓ Saved"); } catch (err) { msg(err.message, true); }
      });
      const clr = $("#dd-loc-clear", dlg);
      if (clr) clr.onclick = async () => { if (!(await CC.confirm("Remove from the map?", `${CC.devName(d)} will no longer show on the map.`, { ok: "Remove", danger: true }))) return; try { await CC.setLocation(siteId, key, { clear: true }); drawLoc(); msg("✓ Removed"); } catch (err) { msg(err.message, true); } };
    };
    drawLoc();

    // Login test: runs ON the site (credtest.py) — one real login attempt, nothing saved from here.
    const LOGIN_RES = { ok: ["ok", "✓ Login works"], auth_failed: ["bad", "✗ Login rejected"], unreachable: ["warn", "⚠ Couldn't reach the device"], untestable: ["unk", "ℹ Can't test this login automatically"] };
    const drawLogin = () => {
      const t = d.cred_test && d.cred_test.current ? d.cred_test : null;
      $("#dd-login", dlg).innerHTML = `
        <p style="margin:0 0 8px">${d.has_credentials ? `<span class="b unk nodot">🔑 Login saved on the site</span>` : `<span class="note">No login saved on the site yet.</span>`}
          ${t ? ` <span class="b ${t.result === "ok" ? "ok" : "bad"} nodot" title="${esc(t.detail || "")}">${t.result === "ok" ? "✓ last test worked" : "✗ last test rejected"}</span> <span class="note">${esc(t.method || "")} · ${esc(t.username || "")} · ${CC.ago(t.ts)}</span>` : ""}</p>
        ${d.has_credentials ? `<div class="row"><button class="btn sm good" id="dd-login-saved">🧪 Test saved login</button></div>` : ""}
        <details id="dd-login-other" style="margin-top:8px" ${d.has_credentials ? "" : "open"}><summary class="note" style="cursor:pointer">${d.has_credentials ? "Test a different login" : "Test a login"}</summary>
          <div class="grid2" style="margin-top:8px"><input class="inp" id="dd-login-user" placeholder="Username" autocomplete="off" value="${esc((t && t.username) || "")}"><input class="inp" id="dd-login-pass" type="password" placeholder="Password" autocomplete="new-password"></div>
          <div class="row" style="margin-top:8px"><button class="btn sm good" id="dd-login-typed">🧪 Test this login</button><span class="note">Not saved — save logins on the site's Netwatch page.</span></div></details>
        <div id="dd-login-res" style="margin-top:8px"></div>
        <p class="note" style="margin:6px 0 0">Each test is one real login attempt on the device (Hikvision locks the account after several wrong ones).</p>`;
      const run = (btn, body) => CC.busy(btn, async () => {
        const res = $("#dd-login-res", dlg);
        res.innerHTML = `<div class="note">Testing — checking which ports answer, then one login attempt (up to 20 s)…</div>`;
        try {
          const r = await api(`/api/hub/sites/${siteId}/devices/${enc(key)}/credentials/test`, { method: "POST", body, timeout: 70000 });
          const [cls, label] = LOGIN_RES[r.result] || ["unk", r.result];
          const info = r.info || {};
          const extra = [info.name, info.model].filter(Boolean).map(esc).join(" · ");
          const tried = (r.tried || []).length > 1 ? `<div class="note">Tried: ${r.tried.map((x) => `${esc(x.method)} — ${esc(String(x.result).replace("_", " "))}`).join(" · ")}</div>` : "";
          res.innerHTML = `<div class="panel pbd" style="display:grid;gap:4px"><div><span class="b ${cls} nodot">${label}</span>${r.method ? ` <span class="note">${esc(r.method)}</span>` : ""}</div>
            <div class="muted" style="font-size:13px">${esc(r.detail || "")}${extra ? " · " + extra : ""}</div>${tried}
            ${r.result === "ok" && !r.current ? `<div class="note">This login is not the one saved on the site${s.links && s.links.netwatch ? ` — <a href="${esc(s.links.netwatch)}" target="_blank" rel="noopener">save it there ${icon("ext")}</a>` : ""}.</div>` : ""}
            ${r.result === "auth_failed" && r.current ? `<div class="note warn">The saved login no longer works — radio monitoring and camera names use it.</div>` : ""}</div>`;
          if (r.current && (r.result === "ok" || r.result === "auth_failed")) {
            d.cred_test = { ts: r.ts, result: r.result, method: r.method, detail: r.detail, username: r.username, current: true };
            const keep = res.innerHTML; drawLogin(); $("#dd-login-res", dlg).innerHTML = keep;
          }
        } catch (err) { res.innerHTML = `<div class="banner bad" style="margin:0">${esc(err.message)}</div>`; }
      });
      const saved = $("#dd-login-saved", dlg);
      if (saved) saved.onclick = (e) => run(e.currentTarget, {});
      $("#dd-login-typed", dlg).onclick = (e) => {
        const user = $("#dd-login-user", dlg).value.trim(), pass = $("#dd-login-pass", dlg).value;
        if (!user && !pass) { $("#dd-login-res", dlg).innerHTML = `<div class="note warn">Type a username and password first.</div>`; return; }
        run(e.currentTarget, { username: user, password: pass });
      };
    };
    drawLogin();

    const retry = (fn) => fn().catch(() => new Promise((r) => setTimeout(r, 2500)).then(fn));
    retry(() => api(`/api/hub/sites/${siteId}/history/${enc(key)}`)).then((h) => {
      const u = h.summary || {};
      $("#dd-up", dlg).innerHTML = `<div class="stats"><div><div class="note">24 hours</div><b>${CC.pct(u.uptime_24h, 1)}</b></div><div><div class="note">7 days</div><b>${CC.pct(u.uptime_7d, 1)}</b></div><div><div class="note">30 days</div><b>${CC.pct(u.uptime_30d, 1)}</b></div></div>`;
    }).catch(() => { $("#dd-up", dlg).textContent = "The site didn't answer (it may be mid-scan) — close and reopen to retry."; });
    const chart = (range) => {
      $$("#dd-range button", dlg).forEach((b) => b.classList.toggle("pri", b.dataset.r === range));
      $("#dd-chart", dlg).innerHTML = `<div class="skel" style="height:150px"></div>`;
      retry(() => api(`/api/hub/sites/${siteId}/history/${enc(key)}/beats?range=${range}`)).then((j) => { $("#dd-chart", dlg).innerHTML = latencyChart(j.points || []); })
        .catch(() => { $("#dd-chart", dlg).innerHTML = `<p class="note">No samples.</p>`; });
    };
    $("#dd-range", dlg).onclick = (e) => { const b = e.target.closest("[data-r]"); if (b) chart(b.dataset.r); };
    chart("1h");
    if (CC.isMikrotik(d)) {
      const quick = () => {
        $("#dd-mt", dlg).innerHTML = `<div class="skel" style="height:60px"></div>`;
        api(`/api/hub/sites/${siteId}/devices/${enc(key)}/mikrotik`, { timeout: 45000 }).then((r) => {
          const src = r.source === "mactelnet" ? "by MAC" : "via IP";
          const bits = [r.model || r.board, r.version && ("RouterOS " + r.version), r.uptime && ("up " + r.uptime),
            (r.cpu !== "" && r.cpu != null) && ("CPU " + r.cpu + "%"),
            r.health && (r.health.voltage || r.health.temperature) && [r.health.voltage && r.health.voltage + "V", r.health.temperature && r.health.temperature + "°C"].filter(Boolean).join(" ")].filter(Boolean);
          $("#dd-mt", dlg).innerHTML = `<div class="row" style="gap:6px;flex-wrap:wrap"><b>🧭 ${esc(r.identity || "MikroTik")}</b><span class="b vio nodot">${src}</span></div><div class="note mono" style="margin-top:4px">${esc(bits.join(" · "))}</div>`;
        }).catch((err) => { $("#dd-mt", dlg).innerHTML = `<p class="note bad">${esc(err.message)}</p>`; });
      };
      quick();
      $("#dd-mt-reload", dlg).onclick = quick;
      $("#dd-mt-console", dlg).onclick = () => CC.openMikrotikConsole(siteId, key);
    }
    if (d.ip) {
      const common = [[80, "HTTP"], [443, "HTTPS"], [22, "SSH"], [554, "RTSP"]];
      const scanned = (d.ports || []).map((p) => [p, ((d.services || {})[p] || "").split(" ")[0] || "open"]);
      $("#dd-ports", dlg).innerHTML = scanned.concat(common.filter(([p]) => !scanned.some(([q]) => q === p))).map(([p, l]) => `<button class="btn sm ${scanned.some(([q]) => q === p) ? "" : "ghost"}" data-port="${p}">${p} <span class="dim">${esc(l)}</span></button>`).join("");
      $("#dd-ports", dlg).onclick = async (e) => {
        const b = e.target.closest("[data-port]"); if (!b) return;
        await CC.busy(b, async () => {
          try { const j = await api(`/api/hub/sites/${siteId}/tunnel`, { method: "POST", body: { ip: d.ip, port: +b.dataset.port }, timeout: 30000 }); tunnelResult(j, `${CC.devName(d)} · port ${j.device_port}`, ""); }
          catch (err) { CC.toast(err.message, "bad"); }
        });
      };
      $$("[data-dx]", dlg).forEach((b) => (b.onclick = () => CC.busy(b, async () => {
        const out = $("#dd-out", dlg);
        const act = b.dataset.dx;
        try {
          if (act === "deep") {
            await api(`/api/hub/sites/${siteId}/trigger`, { method: "POST", body: { mode: "deep", hosts: [d.ip] } });
            out.innerHTML = `<div class="note">Deep scan started — ports and services update on the next device refresh (a few minutes).</div>`;
          } else if (act === "quality") {
            out.innerHTML = `<div class="note">Sending 20 pings…</div>`;
            const q = (await api(`/api/hub/sites/${siteId}/command`, { method: "POST", body: { action: "quality", ip: d.ip }, timeout: 60000 })).quality || {};
            const tone = { excellent: "ok", good: "ok", fair: "warn", poor: "bad", down: "unk" }[q.rating] || "unk";
            out.innerHTML = q.error ? `<div class="bad">${esc(q.error)}</div>` : `<div class="row" style="margin-bottom:8px"><span class="b ${tone}">${esc(q.rating || "")}</span><span class="note">${q.recv}/${q.sent} replies${q.note ? " · " + esc(q.note) : ""}</span></div><div class="stats"><div><div class="note">Loss</div><b>${CC.pct(q.loss)}</b></div><div><div class="note">Average</div><b>${q.avg ?? "—"} ms</b></div><div><div class="note">Jitter</div><b>${q.jitter ?? "—"} ms</b></div><div><div class="note">Worst</div><b>${q.max ?? "—"} ms</b></div></div>`;
          } else {
            out.innerHTML = `<div class="note">Running…</div>`;
            const j = await api(`/api/hub/sites/${siteId}/command`, { method: "POST", body: { action: act, ip: d.ip }, timeout: 90000 });
            out.innerHTML = `<pre class="out">${esc(j.result || "")}</pre>`;
          }
        } catch (err) { out.innerHTML = `<div class="bad">${esc(err.message)}</div>`; }
      })));
    }
  };

  function latencyChart(points) {
    if (!points.length) return `<p class="note" style="margin:0">No samples yet — watched and named devices are pinged every minute.</p>`;
    const W = 560, H = 130, pad = 4;
    const t0 = points[0].ts, t1 = points[points.length - 1].ts || t0 + 1;
    const rtts = points.map((p) => p.rtt).filter((v) => v != null);
    const max = Math.max(10, ...rtts);
    const x = (ts) => pad + ((ts - t0) / Math.max(1, t1 - t0)) * (W - pad * 2);
    const y = (v) => H - pad - (v / max) * (H - pad * 2);
    let path = "", pen = false;
    points.forEach((p) => { if (p.rtt == null) { pen = false; return; } path += `${pen ? "L" : "M"}${x(p.ts).toFixed(1)},${y(p.rtt).toFixed(1)}`; pen = true; });
    const ups = points.map((p) => p.up).filter((v) => v != null);
    const avg = rtts.length ? rtts.reduce((a, b) => a + b, 0) / rtts.length : null;
    return `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Ping time">
        ${[0.25, 0.5, 0.75].map((f) => `<line class="grid" x1="0" x2="${W}" y1="${(H * f).toFixed(0)}" y2="${(H * f).toFixed(0)}"/>`).join("")}
        <path d="${path}" fill="none" stroke="#22d3ee" stroke-width="1.6" vector-effect="non-scaling-stroke"/></svg>
      <div class="beats" style="margin-top:6px">${points.slice(-80).map((p) => `<i class="${p.up == null ? "n" : p.up >= 0.99 ? "" : "x"}"></i>`).join("")}</div>
      <div class="row" style="justify-content:space-between;margin-top:6px"><span class="note">Ping min ${rtts.length ? Math.min(...rtts).toFixed(1) : "—"} · avg ${avg != null ? avg.toFixed(1) : "—"} · max ${rtts.length ? max.toFixed(1) : "—"} ms</span><span class="note">Up ${ups.length ? CC.pct((100 * ups.reduce((a, b) => a + b, 0)) / ups.length, 1) : "—"}</span></div>`;
  }

  // ===== MikroTik management console =========================================
  CC.openMikrotikConsole = (siteId, key) => {
    const s = CC.site(siteId);
    const d = (S.devices[siteId] || []).find((x) => x.key === key);
    if (!s || !d) return;
    let report = null, tab = "overview", timer = null, hist = [], histI = 0;

    const B = (v) => { let n = parseInt(v, 10); if (isNaN(n)) return esc(String(v || "0")); const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; } return (i ? n.toFixed(1) : n) + " " + u[i]; };
    const R = (v) => { let n = parseInt(v, 10); if (isNaN(n) || n <= 0) return "—"; if (n >= 1e9) return (n / 1e9).toFixed(2) + " Gb/s"; if (n >= 1e6) return (n / 1e6).toFixed(1) + " Mb/s"; if (n >= 1e3) return (n / 1e3).toFixed(0) + " Kb/s"; return n + " b/s"; };
    const mgmtOff = () => report && !report.manage_enabled;
    const offBanner = () => mgmtOff() ? `<div class="panel pbd" style="border-color:rgba(245,158,11,.4);background:rgba(245,158,11,.08);margin-bottom:10px"><span class="note" style="color:#fbbf24">🔒 Management is read-only on this site. Turn on <b>Manage MikroTik routers</b> in the site's Netwatch Settings to unlock the actions below.</span></div>` : "";

    const dlg = CC.dialog(`<div class="dhd"><div style="min-width:0"><div class="eyebrow">🖥️ MikroTik console · ${esc(s.name)}</div><h2 id="mtc-title" style="min-width:0;overflow:hidden;text-overflow:ellipsis">${esc(CC.devName(d))}</h2></div>
      <div class="row" style="gap:6px;flex:0 0 auto"><label class="note" style="display:flex;align-items:center;gap:4px" title="Live refresh every 4s"><input type="checkbox" id="mtc-auto"> live</label><button class="btn sm" id="mtc-refresh">↻</button><button class="btn icon ghost" data-close aria-label="Close">${icon("x")}</button></div></div>
      <div class="dbd" style="padding-top:0">
        <div class="row" id="mtc-tabs" style="gap:2px;position:sticky;top:0;background:#0b1220;z-index:2;padding:8px 0;flex-wrap:wrap;border-bottom:1px solid rgba(255,255,255,.08)"></div>
        <div id="mtc-body" style="margin-top:10px"><div class="skel" style="height:220px"></div></div>
      </div>`, { cls: "modal wide", onClose: () => { if (timer) clearInterval(timer); } });

    const TABS = () => {
      const t = [["overview", "Overview"], ["ports", "Ports & PoE"], ["devices", "Devices"], ["network", "Network"], ["logs", "Logs"], ["terminal", "Terminal"]];
      if (report && report.wireless && (report.wireless.interfaces.length || report.wireless.registrations.length)) t.splice(4, 0, ["wireless", "Wireless"]);
      return t;
    };
    const drawTabs = () => { $("#mtc-tabs", dlg).innerHTML = TABS().map(([k, l]) => `<button class="btn sm ${k === tab ? "pri" : "ghost"}" data-tab="${k}">${l}</button>`).join(""); };
    $("#mtc-tabs", dlg).onclick = (e) => { const b = e.target.closest("[data-tab]"); if (!b) return; tab = b.dataset.tab; drawTabs(); render(); };

    const act = async (body, okMsg) => {
      try { const r = await api(`/api/hub/sites/${siteId}/devices/${enc(key)}/mikrotik/action`, { method: "POST", body, timeout: 30000 }); CC.toast(r.msg || okMsg || "Done"); setTimeout(load, 800); }
      catch (err) { CC.toast(err.message, "bad"); }
    };

    // ---- tab renderers ------------------------------------------------------
    const rOverview = () => {
      const y = report.system;
      const memPct = (y.free_memory && y.total_memory) ? Math.round(100 * (1 - y.free_memory / y.total_memory)) : null;
      const kv = [["Model", y.model || y.board], ["RouterOS", y.version], ["Firmware", y.firmware], ["Serial", y.serial],
      ["Uptime", y.uptime], ["Arch", y.arch], ["Board time", [y.date, y.time].filter(Boolean).join(" ")]];
      const eth = report.ports.filter((p) => p.type === "ether");
      const face = eth.map((p) => {
        const col = p.disabled ? "#475569" : p.running ? "#34d399" : "#ef4444";
        const poe = p.poe && p.poe.mode && p.poe.mode !== "off" ? (p.poe.status === "powered-on" ? "⚡" : "·") : "";
        return `<div title="${esc(p.name)} ${p.running ? "up " + esc(p.rate) : "down"}${p.poe ? " PoE " + esc(p.poe.mode) : ""}" style="flex:1;min-width:54px;text-align:center;border:1px solid rgba(255,255,255,.12);border-radius:6px;padding:6px 2px;background:${col}22"><div style="font-size:10px" class="mono">${esc(p.name.replace("ether", "e"))}</div><div style="height:6px;width:6px;border-radius:50%;background:${col};margin:4px auto 0"></div><div style="font-size:10px;color:#fbbf24;height:12px">${poe}</div></div>`;
      }).join("");
      return offBanner() + `<div class="panel pbd"><div class="row" style="justify-content:space-between"><b>🧭 ${esc(y.identity || "MikroTik")}</b>
          <div class="row" style="gap:6px"><span class="b ${y.cpu > 80 ? "warn" : "ok"} nodot">CPU ${esc(String(y.cpu))}%</span>${memPct != null ? `<span class="b ${memPct > 85 ? "warn" : "unk"} nodot">RAM ${memPct}%</span>` : ""}${y.health && y.health.temperature ? `<span class="b unk nodot">${esc(y.health.temperature)}°C</span>` : ""}${y.health && y.health.voltage ? `<span class="b unk nodot">${esc(y.health.voltage)}V</span>` : ""}</div></div>
        <dl class="kv" style="margin-top:8px">${kv.map(([k, v]) => v ? `<dt>${k}</dt><dd class="mono">${esc(String(v))}</dd>` : "").join("")}
          <dt>Memory</dt><dd class="mono">${B((y.total_memory || 0) - (y.free_memory || 0))} / ${B(y.total_memory)}</dd></dl>
        <div class="note" style="margin-top:10px">Ports</div><div class="row" style="gap:4px;margin-top:4px">${face || '<span class="note">no ethernet ports</span>'}</div>
        <div class="row" style="gap:6px;margin-top:12px"><button class="btn sm" data-act="rename" ${mgmtOff() ? "disabled" : ""}>✏️ Rename</button><button class="btn sm" data-act="export">📄 Export config</button><button class="btn sm danger" data-act="reboot" ${mgmtOff() ? "disabled" : ""}>↻ Reboot router</button></div>
        <div id="mtc-export" style="margin-top:8px"></div></div>`;
    };
    const rPorts = () => {
      const rows = report.ports.map((p) => {
        const st = p.disabled ? `<span class="b unk nodot">disabled</span>` : p.running ? `<span class="b ok nodot">up ${esc(p.rate || "")}${p.full_duplex ? " FD" : ""}</span>` : `<span class="b bad nodot">down</span>`;
        let poe = "—", poeAct = "";
        if (p.poe) {
          const on = p.poe.mode && p.poe.mode !== "off";
          const pw = p.poe.power ? ` · ${esc(p.poe.power)}W` : "";
          poe = `<span class="b ${p.poe.status === "powered-on" ? "ok" : on ? "unk" : "dim"} nodot">${esc(p.poe.mode || "off")}${p.poe.status ? " · " + esc(p.poe.status) : ""}${pw}</span>`;
          poeAct = `<button class="btn sm" data-act="poe-cycle" data-port="${esc(p.name)}" ${mgmtOff() ? "disabled" : ""} title="Power-cycle — reboots the device on this port">⟳ cycle</button>
            <button class="btn sm ghost" data-act="poe" data-id="${esc(p.name)}" data-mode="${on ? "off" : "auto-on"}" ${mgmtOff() ? "disabled" : ""}>${on ? "PoE off" : "PoE on"}</button>`;
        }
        const tgl = p.type === "ether" || p.type === "" ? "" : "";
        return `<tr><td class="mono">${esc(p.name)}</td><td>${st}</td><td class="mono dim">↓${R(p.rx_rate)} ↑${R(p.tx_rate)}</td><td class="mono dim">${B(p.rx)}/${B(p.tx)}</td><td>${poe}</td>
          <td class="row" style="gap:4px;justify-content:flex-end"><button class="btn sm ghost" data-act="iface" data-id="${esc(p.name)}" data-enable="${p.disabled ? "1" : "0"}" ${mgmtOff() ? "disabled" : ""}>${p.disabled ? "enable" : "disable"}</button>${poeAct}</td></tr>`;
      }).join("");
      return offBanner() + `<div class="panel pbd" style="overflow-x:auto"><table class="tbl" style="width:100%;font-size:12px"><thead><tr><th>Port</th><th>Link</th><th>Live</th><th>Total ↓/↑</th><th>PoE</th><th></th></tr></thead><tbody>${rows}</tbody></table>
        <p class="note" style="margin:8px 0 0">⟳ cycle power-cycles PoE on that port to reboot the camera/AP plugged into it. Tick <b>live</b> above for auto-refreshing rates.</p></div>`;
    };
    const rDevices = () => {
      const list = report.connected;
      return `<div class="panel pbd"><div class="row" style="justify-content:space-between;margin-bottom:8px"><b>${list.length} device${list.length === 1 ? "" : "s"} on the switch</b><input class="inp" id="mtc-dsearch" placeholder="filter ip / mac / name / port" style="max-width:240px"></div>
        <div style="overflow-x:auto"><table class="tbl" id="mtc-dtable" style="width:100%;font-size:12px"><thead><tr><th>IP</th><th>MAC</th><th>Vendor</th><th>Hostname</th><th>Port</th><th>Seen</th></tr></thead><tbody>${list.map(mtcDevRow).join("")}</tbody></table></div>
        <p class="note" style="margin:8px 0 0">Collected from the router's DHCP leases, ARP table and bridge host table — no scanning.</p></div>`;
    };
    const mtcDevRow = (c) => `<tr data-f="${esc((c.ip + " " + c.mac + " " + (c.hostname || "") + " " + (c.vendor || "") + " " + (c.port || c.iface || "")).toLowerCase())}">
        <td class="mono">${esc(c.ip || "—")}</td><td class="mono">${esc(c.mac)}</td><td class="dim">${esc(c.vendor || "")}</td><td>${esc(c.hostname || "")}</td>
        <td class="mono">${esc(c.port || c.iface || "—")}</td><td class="dim" style="font-size:10px">${esc((c.src || []).filter((x, i, a) => a.indexOf(x) === i).join("+"))}</td></tr>`;
    const rNetwork = () => {
      const addr = report.addresses.map((a) => `<tr><td class="mono">${esc(a.address)}</td><td class="mono dim">${esc(a.interface)}</td></tr>`).join("");
      const rt = report.routes.map((r) => `<tr><td class="mono">${esc(r.dst)}</td><td class="mono">${esc(r.gateway || "")}</td><td class="dim">${esc(r.distance)}</td><td>${r.active ? '<span class="b ok nodot">active</span>' : '<span class="b dim nodot">inactive</span>'}</td></tr>`).join("");
      const fw = report.firewall.filter.map((f) => `<tr><td class="mono">${esc(f.chain)}</td><td><span class="b ${f.action === "drop" || f.action === "reject" ? "bad" : "ok"} nodot">${esc(f.action)}</span></td><td class="dim">${esc([f.protocol, f.dst_port].filter(Boolean).join("/"))}</td><td class="dim mono">${B(f.bytes)}</td><td class="dim">${esc(f.comment || "")}</td></tr>`).join("");
      const nat = report.firewall.nat.map((f) => `<tr><td class="mono">${esc(f.chain)}</td><td>${esc(f.action)}</td><td class="dim">${esc(f.to_addresses || f.dst_port || "")}</td><td class="dim">${esc(f.comment || "")}</td></tr>`).join("");
      const blk = (title, head, body) => body ? `<div class="panel pbd" style="margin-bottom:10px;overflow-x:auto"><b>${title}</b><table class="tbl" style="width:100%;font-size:12px;margin-top:6px"><thead><tr>${head.map((h) => `<th>${h}</th>`).join("")}</tr></thead><tbody>${body}</tbody></table></div>` : "";
      return blk("IP addresses", ["Address", "Interface"], addr) + blk("Routes", ["Dst", "Gateway", "Dist", ""], rt)
        + `<div class="panel pbd" style="margin-bottom:10px"><b>DNS</b> <span class="mono note">${esc(report.dns.servers || "—")}${report.dns.dynamic_servers ? " (+dyn " + esc(report.dns.dynamic_servers) + ")" : ""}</span></div>`
        + blk("Firewall filter", ["Chain", "Action", "Match", "Bytes", "Comment"], fw) + blk("NAT", ["Chain", "Action", "To", "Comment"], nat);
    };
    const rWireless = () => {
      const ifs = report.wireless.interfaces.map((w) => `<tr><td class="mono">${esc(w.name)}</td><td>${esc(w.ssid)}</td><td class="dim">${esc(w.band)}</td><td class="dim">${esc(w.frequency)}</td><td>${w.running ? '<span class="b ok nodot">up</span>' : '<span class="b bad nodot">down</span>'}</td></tr>`).join("");
      const regs = report.wireless.registrations.map((r) => `<tr><td class="mono">${esc(r.mac)}</td><td class="mono dim">${esc(r.interface)}</td><td>${esc(r.signal)}</td><td class="dim">${esc(r.tx_rate)}/${esc(r.rx_rate)}</td><td class="dim">${esc(r.uptime)}</td></tr>`).join("");
      return (ifs ? `<div class="panel pbd" style="margin-bottom:10px;overflow-x:auto"><b>Wireless interfaces</b><table class="tbl" style="width:100%;font-size:12px;margin-top:6px"><thead><tr><th>Iface</th><th>SSID</th><th>Band</th><th>Freq</th><th></th></tr></thead><tbody>${ifs}</tbody></table></div>` : "")
        + `<div class="panel pbd" style="overflow-x:auto"><b>Connected clients (${report.wireless.registrations.length})</b><table class="tbl" style="width:100%;font-size:12px;margin-top:6px"><thead><tr><th>MAC</th><th>Iface</th><th>Signal</th><th>Tx/Rx</th><th>Uptime</th></tr></thead><tbody>${regs || '<tr><td colspan=5 class="note">none</td></tr>'}</tbody></table></div>`;
    };
    const rLogs = () => `<div class="panel pbd"><pre class="mono" style="margin:0;max-height:60vh;overflow:auto;white-space:pre-wrap;font-size:11px">${report.logs.map((l) => `${esc(l.time)}  ${esc(l.topics)}  ${esc(l.message)}`).join("\n") || "no log entries"}</pre></div>`;
    const rTerminal = () => {
      const quick = ["/system resource print", "/ip dhcp-server lease print", "/interface print stats", "/ip firewall filter print", "/log print", "/interface ethernet poe monitor [find] once"];
      return offBanner() + `<div class="panel pbd"><pre id="mtc-term" class="mono" style="margin:0 0 8px;height:46vh;overflow:auto;white-space:pre-wrap;background:#050b16;border:1px solid rgba(255,255,255,.1);border-radius:8px;padding:10px;font-size:12px;color:#a7f3d0">${hist.map((h) => esc(h)).join("")}</pre>
        <div class="row" style="flex-wrap:nowrap;gap:6px"><span class="mono" style="color:#a78bfa;align-self:center">&gt;</span><input class="inp mono" id="mtc-cmd" placeholder="type a RouterOS command…" ${mgmtOff() ? "disabled" : ""} style="flex:1"><button class="btn pri" id="mtc-run" ${mgmtOff() ? "disabled" : ""}>Run</button></div>
        <div class="row" style="gap:4px;margin-top:6px;flex-wrap:wrap">${quick.map((q) => `<button class="btn sm ghost" data-q="${esc(q)}" ${mgmtOff() ? "disabled" : ""}>${esc(q.replace(/^\//, "").split(" ").slice(0, 2).join(" "))}</button>`).join("")}</div>
        <p class="note" style="margin:6px 0 0">Runs as <b>admin</b> on the live router by MAC (MAC-Telnet), logged. Up-arrow recalls history. Dangerous commands ask first.</p></div>`;
    };

    const render = () => {
      const body = $("#mtc-body", dlg);
      if (!report) return;
      body.innerHTML = ({ overview: rOverview, ports: rPorts, devices: rDevices, network: rNetwork, wireless: rWireless, logs: rLogs, terminal: rTerminal }[tab] || rOverview)();
      wire();
    };

    const runTerm = async (cmd, confirmDanger) => {
      const term = $("#mtc-term", dlg); if (!term) return;
      hist.push("> " + cmd + "\n");
      try {
        const r = await api(`/api/hub/sites/${siteId}/devices/${enc(key)}/mikrotik/console`, { method: "POST", body: { command: cmd, confirm: !!confirmDanger }, timeout: 45000 });
        hist.push((r.output || "(no output)") + "\n\n");
      } catch (err) {
        if (err.body && err.body.needs_confirm) { hist.pop(); if (confirm("⚠ " + err.body.warning + "\n\nRun this command anyway?")) runTerm(cmd, true); return; }
        hist.push("error: " + err.message + "\n\n");
      }
      term.textContent = hist.join(""); term.scrollTop = term.scrollHeight;
    };

    const wire = () => {
      $("#mtc-body", dlg).querySelectorAll("[data-act]").forEach((b) => b.onclick = () => {
        const a = b.dataset.act;
        if (a === "reboot") { CC.confirm("Reboot this router?", `${CC.devName(d)} will be offline ~30–60s.`, { ok: "Reboot", danger: true }).then((ok) => ok && act({ action: "reboot" }, "Rebooting")); }
        else if (a === "rename") { const n = prompt("New router identity:", report.system.identity || ""); if (n) act({ action: "set-identity", name: n }, "Renamed"); }
        else if (a === "poe-cycle") { CC.confirm("Power-cycle " + b.dataset.port + "?", "The powered device on that port reboots.", { ok: "Power-cycle" }).then((ok) => ok && act({ action: "poe-cycle", port: b.dataset.port, duration: 5 })); }
        else if (a === "poe") { act({ action: "poe", id: b.dataset.id, mode: b.dataset.mode }); }
        else if (a === "iface") { CC.busy(b, () => act({ action: "interface", id: b.dataset.id, enable: b.dataset.enable === "1" })); }
        else if (a === "export") { const box = $("#mtc-export", dlg); box.innerHTML = `<div class="skel" style="height:60px"></div>`; api(`/api/hub/sites/${siteId}/devices/${enc(key)}/mikrotik/action`, { method: "POST", body: { action: "export" }, timeout: 30000 }).then((r) => { box.innerHTML = `<pre class="mono" style="max-height:40vh;overflow:auto;white-space:pre-wrap;background:#050b16;border:1px solid rgba(255,255,255,.1);border-radius:8px;padding:8px;font-size:11px">${esc(r.config || "(empty)")}</pre>`; }).catch((e) => { box.innerHTML = `<p class="note bad">${esc(e.message)}</p>`; }); }
      });
      const ds = $("#mtc-dsearch", dlg);
      if (ds) ds.oninput = () => { const q = ds.value.toLowerCase(); $("#mtc-dtable tbody", dlg).querySelectorAll("tr").forEach((tr) => { tr.style.display = tr.dataset.f.includes(q) ? "" : "none"; }); };
      const run = $("#mtc-run", dlg), cmd = $("#mtc-cmd", dlg);
      if (run && cmd) {
        const go = () => { const c = cmd.value.trim(); if (!c) return; hist.length > 200 && hist.splice(0, 100); runTerm(c); cmd.value = ""; histI = 0; };
        run.onclick = go;
        cmd.onkeydown = (e) => { if (e.key === "Enter") go(); else if (e.key === "ArrowUp") { const cmds = hist.filter((h) => h.startsWith("> ")).map((h) => h.slice(2).trim()); if (cmds.length) { histI = Math.min(histI + 1, cmds.length); cmd.value = cmds[cmds.length - histI] || ""; } } };
        cmd.focus();
      }
      $("#mtc-body", dlg).querySelectorAll("[data-q]").forEach((b) => b.onclick = () => { const c = $("#mtc-cmd", dlg); if (c) { c.value = b.dataset.q; c.focus(); } });
    };

    const load = async () => {
      try {
        report = await api(`/api/hub/sites/${siteId}/devices/${enc(key)}/mikrotik/report`, { timeout: 45000 });
        $("#mtc-title", dlg).textContent = report.system.identity ? report.system.identity + " · " + (report.system.model || "MikroTik") : CC.devName(d);
        drawTabs(); render();
      } catch (err) {
        const hint = err.body && err.body.hint;
        $("#mtc-body", dlg).innerHTML = `<div class="panel pbd"><p class="note bad">Couldn't read the router: ${esc(err.message)}</p>${hint ? `<p class="note" style="color:#fbbf24">💡 ${esc(hint)}</p>` : `<p class="note">The console reads over the RouterOS API on the router's IP (learned from its MAC). If this persists, the router may be unreachable over the VPN or the saved admin login is wrong.</p>`}</div>`;
      }
    };
    drawTabs();
    $("#mtc-refresh", dlg).onclick = load;
    $("#mtc-auto", dlg).onchange = (e) => { if (timer) { clearInterval(timer); timer = null; } if (e.target.checked) timer = setInterval(() => { if (tab === "ports" || tab === "overview") load(); }, 4000); };
    load();
  };
})();
