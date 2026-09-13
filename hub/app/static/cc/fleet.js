/* Netwatch Control Center — fleet views: overview, devices, attention, backups,
 * VPN & remote access, settings (sites, alerts & AI). */
(function () {
  "use strict";
  const { $, $$, esc, icon, api, state: S } = CC;
  const view = () => $("#view");

  // ---- shared renderers ----------------------------------------------------------
  CC.spark = (series, cls = "spark") =>
    `<div class="${cls}" role="img" aria-label="Reachability, last 24 hours">${(series || []).map((v) => `<i class="${v == null ? "n" : v >= 0.99 ? "" : v > 0 ? "w" : "x"}" style="height:${v == null ? 30 : Math.max(18, Math.round(v * 100))}%"></i>`).join("")}</div>`;

  CC.issueList = (issues, { showSite = true, limit = 0 } = {}) => {
    if (!issues.length) return `<div class="empty"><b>All clear</b>Nothing needs your attention right now.</div>`;
    const list = limit ? issues.slice(0, limit) : issues;
    const mark = { bad: "!", warn: "!", unk: "?", info: "i" };
    return `<ul class="alist">${list.map((i) => `
      <li><span class="sev ${i.sev}" aria-label="${i.sev === "bad" ? "Fault" : i.sev === "warn" ? "Warning" : "Info"}">${mark[i.sev]}</span>
        <div style="min-width:0"><div class="t">${esc(i.title)}</div><div class="d">${showSite ? `<b class="cyan" style="font-weight:600">${esc(i.site.name)}</b> · ` : ""}${esc(i.detail)}</div></div>
        ${i.act ? `<a class="btn sm" href="${i.act}">Open</a>` : ""}</li>`).join("")}</ul>
      ${limit && issues.length > limit ? `<a class="more" href="#/attention" style="text-align:center;text-decoration:none">Show all ${issues.length}</a>` : ""}`;
  };

  function siteCard(s) {
    const st = CC.STATE[CC.siteState(s)];
    const c = CC.siteCounts(s);
    const issues = CC.siteIssues(s);
    const pct = c.total ? Math.round((100 * c.online) / c.total) : 0;
    const top = issues.filter((i) => i.sev === "bad" || i.sev === "warn").slice(0, 2);
    const href = `#/site/${encodeURIComponent(s.id)}`;
    return `<article class="scard ${st.card}">
      <div class="in">
        <div class="row" style="justify-content:space-between;align-items:flex-start;flex-wrap:nowrap">
          <div style="min-width:0"><h3><i class="dot ${st.dot}"></i><a href="${href}">${esc(s.name)}</a></h3><div class="loc">${esc(s.location || s.vpn_ip)}</div></div>
          <div style="text-align:right;flex:none">${CC.stateBadge(s)}<div class="note" style="margin-top:4px">${s.latency_ms != null ? `<span class="mono">${Math.round(s.latency_ms)} ms</span> · ` : ""}scan ${CC.ago(s.last_scan_ts)}</div></div>
        </div>
        ${c.total == null ? `<div class="muted">No device data yet</div>` : `
        <div>
          <div class="nums"><b class="${c.online === c.total ? "ok" : ""}">${c.online}</b><span class="muted">of ${c.total} online</span>
            ${c.quiet ? `<span class="note" title="Not seen for 7+ days — old discoveries, visitors' phones">· ${c.quiet} gone quiet</span>` : ""}</div>
          <div class="bar"><i style="width:${pct}%"></i></div>
          ${c.infra != null ? `<div class="note" style="margin-top:6px">Infrastructure <b class="${c.infraUp === c.infra ? "ok" : "warn"}">${c.infraUp}/${c.infra}</b> up · cameras, recorders, network, power</div>` : ""}
        </div>`}
        ${top.length ? `<div style="display:grid;gap:4px">${top.map((i) => `<div class="note" style="color:${i.sev === "bad" ? "var(--bad)" : "var(--warn)"}">● ${esc(i.title)}</div>`).join("")}${issues.length > 2 ? `<div class="note">+ ${issues.length - 2} more</div>` : ""}</div>` : ""}
        <div>${CC.spark(s.spark)}<div class="note" style="margin-top:3px">Reachability 24 h · ${s.reach_24h != null ? s.reach_24h + "%" : "—"}</div></div>
      </div>
      <div class="foot">
        ${s.kuma ? `<span class="note">Kuma <b class="ok">↑${s.kuma.up}</b>${s.kuma.down ? ` <b class="bad">↓${s.kuma.down}</b>` : ""}</span>` : s.kuma_state === "no-status-page" ? `<span class="note" title="Create a status page with slug 'farm' in this site's Kuma">Kuma: no status page</span>` : s.kuma_state === "unreachable" ? `<span class="note bad">Kuma unreachable</span>` : ""}
        <span class="btns">
        ${s.links && s.links.netwatch ? `<a class="btn sm ghost" href="${esc(s.links.netwatch)}" target="_blank" rel="noopener" title="This site's own Netwatch page">Netwatch ${icon("ext")}</a>` : ""}
        ${s.links && s.links.kuma ? `<a class="btn sm ghost" href="${esc(s.links.kuma)}" target="_blank" rel="noopener">Kuma ${icon("ext")}</a>` : ""}
        <a class="btn sm pri" href="${href}">Open</a></span>
      </div>
    </article>`;
  }

  function fleetKpis() {
    const sites = S.sites.filter((s) => s.enabled);
    const reach = sites.filter((s) => s.reachable).length;
    let total = 0, online = 0, offline = 0, quiet = 0, infra = 0, infraUp = 0, have = 0;
    sites.forEach((s) => {
      const c = CC.siteCounts(s);
      if (c.total == null) return;
      have++; total += c.total; online += c.online;
      offline += c.offline || 0; quiet += c.quiet || 0; infra += c.infra || 0; infraUp += c.infraUp || 0;
    });
    const watched = sites.reduce((a, s) => a + ((S.devices[s.id] || []).filter((d) => d.watch && !d.online).length || (S.devices[s.id] ? 0 : s.watched_down || 0)), 0);
    const conflicts = sites.reduce((a, s) => a + (s.conflicts || 0), 0);
    const kumaDown = sites.reduce((a, s) => a + ((s.kuma && s.kuma.down) || 0), 0);
    const issues = CC.allIssues();
    const bad = issues.filter((i) => i.sev === "bad").length;
    const kpi = (k, v, s, cls, href) => `<${href ? `a href="${href}"` : "div"} class="kpi ${cls}"><div class="k">${k}</div><div class="v">${v}</div><div class="s">${s}</div></${href ? "a" : "div"}>`;
    return `<section class="kpis" aria-label="Fleet summary">
      ${kpi("Sites online", `${reach}<small>/${sites.length}</small>`, reach === sites.length ? "All sites reachable over the VPN" : `${sites.length - reach} unreachable`, reach === sites.length ? "is-ok" : "is-bad", "#/attention")}
      ${kpi("Needs attention", issues.length, issues.length ? `${bad} fault${bad === 1 ? "" : "s"} · ${issues.length - bad} warning${issues.length - bad === 1 ? "" : "s"}` : "Nothing waiting on you", bad ? "is-bad" : issues.length ? "is-warn" : "is-ok", "#/attention")}
      ${kpi("Key equipment up", have ? `${infraUp}<small>/${infra}</small>` : "—", "Cameras, recorders, network & power seen this week", infra && infraUp < infra ? "is-warn" : "is-ok", "#/devices?scope=infra&state=offline")}
      ${kpi("Devices online", have ? `${online}<small>/${total}</small>` : "—", `${offline} offline this week${quiet ? ` · ${quiet} gone quiet` : ""}`, "", "#/devices")}
      ${kpi("Watched down", watched, watched ? "Devices you flagged to watch" : "Every watched device is up", watched ? "is-bad" : "is-ok", "#/devices?watch=1&state=offline")}
      ${kpi("Conflicts · Kuma", `${conflicts}<small> · ${kumaDown}</small>`, `${conflicts ? CC.plural(conflicts, "IP clash", "IP clashes") : "No IP clashes"} · ${kumaDown ? kumaDown + " Kuma down" : "Kuma all up"}`, conflicts || kumaDown ? (kumaDown ? "is-bad" : "is-warn") : "is-ok", "#/attention")}
    </section>`;
  }

  // ---- overview ----------------------------------------------------------------------
  CC.route("/", {
    enter() {
      CC.setCrumbs([{ label: "Overview" }]);
      view().innerHTML = `
        <div class="ph"><div><div class="eyebrow">Control Center</div><h1 id="ov-title">Every site at a glance</h1><p id="ov-sub">Loading the fleet…</p></div>
          <div class="actions"><a class="btn" href="#/settings/sites">${icon("plus")} Add site</a></div></div>
        <div id="ov-banner"></div>
        <div id="ov-kpis"></div>
        <div class="cols">
          <div><section class="panel"><div class="phd"><h2>Sites <small id="ov-sites-n"></small></h2><div class="row"><select class="sel" id="ov-sort" aria-label="Sort sites"><option value="state">Worst first</option><option value="name">By name</option></select></div></div><div class="pbd"><div class="sites" id="ov-sites"></div></div></section></div>
          <div><section class="panel"><div class="phd"><h2>Needs attention <small id="ov-att-n"></small></h2><a class="btn sm ghost" href="#/attention">All</a></div><div id="ov-att"></div></section>
          <section class="panel"><div class="phd"><h2>Monitoring path</h2></div><div class="pbd" id="ov-path"></div></section></div>
        </div>`;
      $("#ov-sort").onchange = () => this.update();
      this.update();
    },
    update() {
      if (!$("#ov-sites")) return;
      $("#ov-banner").innerHTML = S.error ? `<div class="banner bad">${esc(S.error)} — showing the last data received ${CC.ago(S.updated)}.</div>` : "";
      if (!S.loaded) { $("#ov-kpis").innerHTML = `<div class="kpis">${'<div class="kpi"><div class="skel" style="height:60px"></div></div>'.repeat(4)}</div>`; return; }
      const sites = S.sites.slice();
      const rank = { offline: 0, fault: 1, warn: 2, ok: 3, paused: 4 };
      if ($("#ov-sort").value === "state") sites.sort((a, b) => rank[CC.siteState(a)] - rank[CC.siteState(b)] || a.name.localeCompare(b.name));
      else sites.sort((a, b) => a.name.localeCompare(b.name));
      const issues = CC.allIssues();
      const bad = issues.filter((i) => i.sev === "bad").length;
      $("#ov-title").textContent = !S.sites.length ? "Add your first site" : bad ? `${CC.plural(bad, "fault")} across the fleet` : issues.length ? "Running, with a few things to look at" : "All sites healthy";
      $("#ov-sub").textContent = `${S.hubName} · ${CC.plural(S.sites.length, "site")} · refreshed ${CC.ago(S.updated)}`;
      $("#ov-kpis").innerHTML = fleetKpis();
      $("#ov-sites-n").textContent = S.sites.length;
      $("#ov-sites").innerHTML = sites.map(siteCard).join("") || `<div class="empty"><b>No sites yet</b>Add a farm with the new-site wizard — it creates the VPN client and gives you one command to paste on the Pi.<div style="margin-top:12px"><a class="btn pri" href="#/settings/sites">${icon("plus")} Add site</a></div></div>`;
      $("#ov-att-n").textContent = issues.length || "";
      $("#ov-att").innerHTML = CC.issueList(issues, { limit: 8 });
      const iso = S.vpn;
      const enabled = S.sites.filter((s) => s.enabled);
      const backedUp = enabled.filter((s) => { const a = CC.backupAge(s.id); return a != null && a <= 26; }).length;
      const piOk = enabled.filter((s) => s.pi_health === "ok").length;
      const keyOk = enabled.filter((s) => ["ok", "claimed"].includes(s.api_auth)).length;
      const vpnSites = enabled.filter((s) => /^10\.8\./.test(s.vpn_ip || ""));
      $("#ov-path").innerHTML = `<dl class="kv">
        <dt>Hub data</dt><dd>${S.error ? `<span class="b bad">Not updating</span>` : `<span class="b ok">Live</span> <span class="note">every 20 s</span>`}</dd>
        <dt>VPN sites reachable</dt><dd>${vpnSites.filter((s) => s.reachable).length}/${vpnSites.length}</dd>
        <dt>Client isolation</dt><dd>${iso ? (iso.enabled && iso.applied ? `<span class="b ok">On</span>` : `<span class="b bad">Off</span>`) : `<span class="note">checking…</span>`}</dd>
        <dt>Pi health OK</dt><dd>${piOk}/${enabled.length}</dd>
        <dt>Pi login + hub key</dt><dd>${keyOk}/${enabled.length}</dd>
        <dt>Backup in last 26 h</dt><dd>${backedUp}/${enabled.length} <span class="note">· encrypted</span></dd>
      </dl><p class="note" style="margin:12px 0 0">A site that goes offline means the hub lost sight of it — not proof its cameras failed. Its devices keep their last known state.</p>`;
      if (!S.vpn && !this._vpnAsked) { this._vpnAsked = true; api("/api/hub/vpn-isolation").then((j) => { S.vpn = j; CC.emit(); }).catch(() => {}); }
    },
  });

  // ---- attention ------------------------------------------------------------------------
  CC.route("/attention", {
    enter() {
      CC.setCrumbs([{ label: "Overview", href: "#/" }, { label: "Needs attention" }]);
      view().innerHTML = `<div class="ph"><div><div class="eyebrow">Fleet</div><h1>Needs attention</h1><p>Faults first, then warnings. Everything here is worked out from live hub data — nothing is stored or acknowledged yet.</p></div></div>
        <section class="panel"><div class="tbar"><select class="sel" id="at-site" aria-label="Site"><option value="">All sites</option></select><select class="sel" id="at-sev" aria-label="Severity"><option value="">Faults and warnings</option><option value="bad">Faults only</option><option value="warn">Warnings only</option></select><span class="note" id="at-n"></span></div><div id="at-list"></div></section>`;
      $("#at-site").onchange = $("#at-sev").onchange = () => this.update();
      this.update();
    },
    update() {
      if (!$("#at-list")) return;
      const sel = $("#at-site"), cur = sel.value;
      sel.innerHTML = `<option value="">All sites</option>` + S.sites.map((s) => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join("");
      sel.value = cur;
      let list = CC.allIssues();
      if (cur) list = list.filter((i) => i.site.id === cur);
      if ($("#at-sev").value) list = list.filter((i) => i.sev === $("#at-sev").value);
      $("#at-n").textContent = CC.plural(list.length, "item");
      $("#at-list").innerHTML = S.loaded ? CC.issueList(list) : `<div class="pbd"><div class="skel" style="height:80px"></div></div>`;
    },
  });

  // ---- devices (fleet-wide) --------------------------------------------------------------
  CC.deviceFilters = (p) => ({
    q: p.get("q") || "", site: p.get("site") || "", scope: p.get("scope") || "",
    state: p.get("state") || "", cat: p.get("cat") || "", watch: p.get("watch") === "1",
  });
  CC.filterDevices = (rows, f) => {
    const q = f.q.trim().toLowerCase();
    const exactIp = /^\d{1,3}(\.\d{1,3}){3}$/.test(q);    // a full address means that address, not .10–.199
    return rows.filter((d) => {
      if (f.site && d.__site.id !== f.site) return false;
      if (f.scope === "infra" && !CC.isInfra(d)) return false;
      if (f.scope === "mikrotik" && !CC.isMikrotik(d)) return false;
      const st = CC.devState(d);
      if (f.state === "all") { /* no state filter */ }
      else if (f.state === "offline" && st !== "offline") return false;
      if (f.state === "online" && st !== "online") return false;
      if (f.state === "quiet" && st !== "quiet") return false;
      if (f.state === "" && st === "quiet" && !q) return false;          // gone-quiet hidden unless asked
      if (f.cat && CC.cat(d).group !== f.cat) return false;
      if (f.watch && !d.watch) return false;
      if (exactIp) return d.ip === q;
      if (q && ![CC.devName(d), d.ip, d.mac, d.vendor, d.hostname, d.model, d.category, d.type, d.__site.name].join(" ").toLowerCase().includes(q)) return false;
      return true;
    });
  };
  CC.devStateBadge = (d) => {
    const st = CC.devState(d);
    return st === "online" ? `<span class="b ok">Online</span>` : st === "offline" ? `<span class="b ${d.watch ? "bad" : "warn"}">Offline</span>` : `<span class="b unk" title="Not seen for 7+ days">Gone quiet</span>`;
  };
  CC.deviceCsv = (rows, name) => {
    const cols = ["site", "name", "category", "type", "ip", "mac", "vendor", "model", "serial", "firmware", "hostname", "state", "last_seen", "rtt_ms", "watched", "open_ports"];
    const q = (v) => { v = v == null ? "" : String(v); return /[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v; };
    const lines = [cols.join(",")].concat(rows.map((d) => [d.__site.name, CC.devName(d), CC.cat(d).label, d.type, d.ip, d.mac, d.vendor, d.model, d.serial, d.firmware, d.hostname, CC.devState(d), d.last_seen ? new Date(d.last_seen * 1000).toISOString() : "", d.rtt, d.watch ? "yes" : "", (d.ports || []).join(" ")].map(q).join(",")));
    const stamp = new Date().toISOString().slice(0, 16).replace(/[-:T]/g, "");
    CC.download(new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv" }), `${name}-${stamp}.csv`);
  };

  /** Device table shared by the fleet page and a site's Devices tab. */
  CC.deviceTable = {
    mount(el, { siteId = null } = {}) {
      const p = CC.params();
      const f = CC.deviceFilters(p);
      if (siteId) f.site = siteId;
      const st = { f, sort: p.get("sort") || "state", dir: 1, limit: 150, group: siteId ? "cat" : "site" };
      el.innerHTML = `
        <div class="tbar">
          <label class="search">${icon("search")}<span class="sr">Search devices</span><input class="inp" id="dv-q" type="search" placeholder="Name, IP, MAC, vendor, model…" value="${esc(f.q)}"></label>
          ${siteId ? "" : `<select class="sel" id="dv-site" aria-label="Site"><option value="">All sites</option>${S.sites.map((s) => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join("")}</select>`}
          <select class="sel" id="dv-state" aria-label="State"><option value="">Seen this week</option><option value="online">Online</option><option value="offline">Offline (this week)</option><option value="quiet">Gone quiet (7+ days)</option><option value="all">Everything</option></select>
          <select class="sel" id="dv-cat" aria-label="Type"><option value="">All types</option>${CC.GROUPS.map(([k, l]) => `<option value="${k}">${l}</option>`).join("")}</select>
          <select class="sel" id="dv-scope" aria-label="Scope"><option value="">Any device</option><option value="infra">Infrastructure only</option><option value="mikrotik">MikroTik</option></select>
          <label class="chk"><input type="checkbox" id="dv-watch" ${f.watch ? "checked" : ""}> Watched</label>
          <span class="note" id="dv-n" style="margin-left:auto"></span>
          <button class="btn sm" id="dv-csv">${icon("down")} CSV</button>
        </div>
        <div class="twrap"><table class="t"><thead><tr>
          <th><button data-sort="name">Device</button></th>
          ${siteId ? "" : `<th class="hide-m"><button data-sort="site">Site</button></th>`}
          <th class="hide-m"><button data-sort="cat">Type</button></th>
          <th><button data-sort="ip">IP</button></th>
          <th><button data-sort="state">State</button></th>
          <th class="num hide-m"><button data-sort="rtt">Ping</button></th>
          <th class="hide-m"><button data-sort="seen">Last seen</button></th>
        </tr></thead><tbody id="dv-body"></tbody></table></div>
        <button class="more" id="dv-more" hidden>Show more</button>`;
      $("#dv-state", el).value = f.state === "" ? "" : f.state;
      $("#dv-cat", el).value = f.cat;
      $("#dv-scope", el).value = f.scope;
      if (!siteId) $("#dv-site", el).value = f.site;
      const sync = () => {
        f.q = $("#dv-q", el).value; f.state = $("#dv-state", el).value; f.cat = $("#dv-cat", el).value; f.scope = $("#dv-scope", el).value; f.watch = $("#dv-watch", el).checked;
        if (!siteId) f.site = $("#dv-site", el).value;
        st.limit = 150; this.draw(el, st, siteId);
      };
      $("#dv-q", el).oninput = CC.debounce(sync, 150);
      $$("select, input[type=checkbox]", el).forEach((x) => { if (x.id !== "dv-q") x.onchange = sync; });
      $$("th button", el).forEach((b) => (b.onclick = () => { st.dir = st.sort === b.dataset.sort ? -st.dir : 1; st.sort = b.dataset.sort; this.draw(el, st, siteId); }));
      $("#dv-more", el).onclick = () => { st.limit += 300; this.draw(el, st, siteId); };
      $("#dv-csv", el).onclick = () => CC.deviceCsv(this.rows(st, siteId), siteId ? `netwatch-${siteId}-devices` : "netwatch-devices");
      $("#dv-body", el).onclick = (e) => { const tr = e.target.closest("tr[data-key]"); if (tr) CC.openDevice(tr.dataset.site, tr.dataset.key); };
      $("#dv-body", el).onkeydown = (e) => { if (e.key === "Enter") { const tr = e.target.closest("tr[data-key]"); if (tr) CC.openDevice(tr.dataset.site, tr.dataset.key); } };
      el.__st = st;
      this.draw(el, st, siteId);
    },
    rows(st, siteId) {
      const sites = siteId ? [CC.site(siteId)].filter(Boolean) : S.sites;
      const all = sites.flatMap((s) => (S.devices[s.id] || []).map((d) => Object.assign(d, { __site: s })));
      const rows = CC.filterDevices(all, st.f);
      const rank = { offline: 0, online: 1, quiet: 2 };
      const key = {
        name: (d) => CC.devName(d).toLowerCase(), site: (d) => d.__site.name, ip: (d) => CC.ipNum(d.ip),
        cat: (d) => CC.cat(d).label, rtt: (d) => (d.rtt == null ? 1e9 : d.rtt), seen: (d) => (d.online ? 9e12 : -(d.last_seen || 0)),
        state: (d) => rank[CC.devState(d)] * 10 - (d.watch ? 1 : 0),
      }[st.sort];
      return rows.sort((a, b) => { const x = key(a), y = key(b); return (x < y ? -1 : x > y ? 1 : CC.ipNum(a.ip) - CC.ipNum(b.ip)) * st.dir; });
    },
    draw(el, st, siteId) {
      const body = $("#dv-body", el);
      if (!body) return;
      const sites = siteId ? [CC.site(siteId)].filter(Boolean) : S.sites;
      const loadedAll = sites.every((s) => S.devices[s.id]);
      if (!loadedAll && !sites.some((s) => S.devices[s.id])) { body.innerHTML = `<tr><td colspan="7"><div class="skel" style="height:120px"></div></td></tr>`; return; }
      const rows = this.rows(st, siteId);
      const total = sites.reduce((a, s) => a + (S.devices[s.id] || []).length, 0);
      const quiet = sites.reduce((a, s) => a + (S.devices[s.id] || []).filter((d) => CC.devState(d) === "quiet").length, 0);
      $("#dv-n", el).textContent = `${rows.length} of ${total}${quiet && st.f.state === "" ? ` · ${quiet} gone quiet hidden` : ""}`;
      const shown = rows.slice(0, st.limit);
      const cols = siteId ? 6 : 7;
      let last = null, html = "";
      const groupKey = st.sort === "state" || st.sort === "name" ? (siteId ? (d) => CC.cat(d).group : (d) => d.__site.id) : null;
      const groupLabel = siteId ? (g) => (CC.GROUPS.find((x) => x[0] === g) || [0, g])[1] : (g) => CC.site(g).name;
      const rowsByGroup = groupKey ? shown.slice().sort((a, b) => {
        const ga = groupKey(a), gb = groupKey(b);
        const order = siteId ? (g) => CC.GROUPS.findIndex((x) => x[0] === g) : (g) => S.sites.findIndex((s) => s.id === g);
        return order(ga) - order(gb);
      }) : shown;
      for (const d of rowsByGroup) {
        if (groupKey) {
          const g = groupKey(d);
          if (g !== last) {
            const inGroup = rows.filter((x) => groupKey(x) === g);
            const up = inGroup.filter((x) => x.online).length;
            html += `<tr class="grp"><td colspan="${cols}">${esc(groupLabel(g))} <span class="dim">· ${up}/${inGroup.length} online</span></td></tr>`;
            last = g;
          }
        }
        const c = CC.cat(d);
        html += `<tr data-key="${esc(d.key)}" data-site="${esc(d.__site.id)}" tabindex="0">
          <td><div class="nm">${esc(CC.devName(d))}${d.watch ? ` <span title="Watched" aria-label="Watched">🔔</span>` : ""}${d.ip_conflict ? ` <span class="b warn nodot" title="IP conflict">conflict</span>` : ""}${CC.isMikrotik(d) ? ` <span class="tag">MikroTik</span>` : ""}</div><div class="sub">${esc([d.vendor && d.vendor !== CC.devName(d) ? d.vendor.replace(/,?\s*(Co\.,?\s*Ltd\.?|Ltd\.?|Inc\.?)$/i, "") : "", d.model].filter(Boolean).join(" · ") || d.mac || "")}</div></td>
          ${siteId ? "" : `<td class="hide-m">${esc(d.__site.name)}</td>`}
          <td class="hide-m">${c.icon} ${esc(c.label)}</td>
          <td class="mono">${esc(d.ip || "—")}</td>
          <td>${CC.devStateBadge(d)}</td>
          <td class="num mono hide-m">${d.online && d.rtt != null ? d.rtt + " ms" : ""}</td>
          <td class="hide-m">${d.online ? `<span class="ok">now</span>` : CC.ago(d.last_seen)}</td></tr>`;
      }
      body.innerHTML = html || `<tr><td colspan="${cols}"><div class="empty"><b>No devices match</b>Try “Everything” or clear the search.</div></td></tr>`;
      const more = $("#dv-more", el);
      more.hidden = rows.length <= st.limit;
      more.textContent = `Show more (${rows.length - st.limit} left)`;
    },
  };

  CC.route("/devices", {
    enter() {
      CC.setCrumbs([{ label: "Overview", href: "#/" }, { label: "Devices" }]);
      view().innerHTML = `<div class="ph"><div><div class="eyebrow">Fleet inventory</div><h1>All devices</h1><p>Every device the sites have discovered. Click one for details, history, diagnostics and remote access.</p></div></div>
        <section class="panel" id="dv-panel"></section>`;
      CC.deviceTable.mount($("#dv-panel"));
    },
    update() { const el = $("#dv-panel"); if (el && el.__st) CC.deviceTable.draw(el, el.__st, null); },
  });

  // ---- backups (fleet) ----------------------------------------------------------------------
  CC.backupKeyDialog = () => {
    const d = CC.dialog(`<div class="dhd"><div><h2>Backup key</h2><p>Decrypts every site's backups on this hub</p></div><button class="btn icon ghost" data-close aria-label="Close">${icon("x")}</button></div>
      <form class="dbd" id="bk-form"><p style="margin:0" class="muted">This key plus any backup file opens every saved device login. Enter the hub password to show it.</p>
        <label class="fld">Hub password<input class="inp" type="password" name="pw" autocomplete="current-password" required></label>
        <div class="bad note" id="bk-err" aria-live="polite"></div>
        <div class="row" style="justify-content:flex-end"><button type="button" class="btn" data-close>Cancel</button><button class="btn pri">Show key</button></div></form>`);
    $("#bk-form", d).onsubmit = async (e) => {
      e.preventDefault();
      const btn = e.submitter;
      await CC.busy(btn, async () => {
        try {
          const j = await api("/api/hub/backup-key", { method: "POST", body: { password: e.target.pw.value } });
          e.target.outerHTML = `<div class="dbd"><p style="margin:0" class="muted">Store it in your password manager. You need it to restore these backups on a replacement hub, or to open a downloaded file with <span class="mono">backups.py decrypt FILE KEY</span>.</p>
            <textarea class="inp mono" readonly rows="2" id="bk-key">${esc(j.key)}</textarea><div class="note">Key id <span class="mono">${esc(j.key_id)}</span></div>
            <div class="row" style="justify-content:flex-end"><button class="btn" id="bk-copy">Copy</button><button class="btn pri" data-close>Done</button></div></div>`;
          $("#bk-copy", d).onclick = (ev) => CC.copy(j.key, ev.currentTarget);
        } catch (err) { $("#bk-err", d).textContent = err.message; }
      });
    };
  };
  CC.restoreBackup = async (siteId, name) => {
    const s = CC.site(siteId);
    if (!(await CC.confirm(`Restore ${s.name}?`, `Backup ${name}\n\nThis REPLACES the site's settings, device names and saved logins with the backup. Use it after rebuilding a Pi (enrol it first so the VPN is up).`, { ok: "Restore", danger: true }))) return;
    try {
      const j = await api(`/api/hub/sites/${siteId}/restore`, { method: "POST", body: { name }, timeout: 45000 });
      CC.toast(`Restored ${s.name}: ${(j.applied || []).join(", ") || "done"}`, "ok");
    } catch (e) { CC.toast(`Restore failed: ${e.message}`, "bad"); }
  };
  CC.backupNow = async (siteId, btn) => CC.busy(btn, async () => {
    try {
      await api(`/api/hub/sites/${siteId}/backup`, { method: "POST", timeout: 45000 });
      const j = await api(`/api/hub/sites/${siteId}/backups`);
      S.backups[siteId] = { list: j.backups || [], __at: CC.now() };
      CC.toast(`Backed up ${CC.site(siteId).name}`, "ok"); CC.emit();
    } catch (e) { CC.toast(`Backup failed: ${e.message}`, "bad"); }
  });

  CC.route("/backups", {
    enter() {
      CC.setCrumbs([{ label: "Overview", href: "#/" }, { label: "Backups" }]);
      view().innerHTML = `<div class="ph"><div><div class="eyebrow">Recovery</div><h1>Config backups</h1><p>The hub pulls an encrypted backup of every site's settings, device names and saved logins each day and keeps the newest 14.</p></div>
        <div class="actions"><button class="btn" id="bk-key">${icon("key")} Backup key</button><button class="btn pri" id="bk-all">${icon("save")} Back up all now</button></div></div>
        <div class="banner" style="border-color:rgba(34,211,238,.3);background:var(--info-bg);color:var(--ink2)">Backups are AES-256 encrypted with the hub's backup key. Keep a copy of that key in your password manager — without it these backups can't be restored on a replacement hub.</div>
        <section class="panel"><div class="twrap"><table class="t"><thead><tr><th>Site</th><th>Newest backup</th><th class="hide-m">Kept</th><th class="hide-m">Encryption</th><th></th></tr></thead><tbody id="bk-body"></tbody></table></div></section>`;
      $("#bk-key").onclick = CC.backupKeyDialog;
      $("#bk-all").onclick = (e) => CC.busy(e.currentTarget, async () => { for (const s of S.sites.filter((x) => x.enabled && x.reachable)) await CC.backupNow(s.id); });
      $("#bk-body").onclick = (e) => {
        const b = e.target.closest("button[data-act]"); if (!b) return;
        if (b.dataset.act === "now") CC.backupNow(b.dataset.site, b);
      };
      this.update();
    },
    update() {
      if (!$("#bk-body")) return;
      $("#bk-body").innerHTML = S.sites.map((s) => {
        const b = S.backups[s.id];
        const list = (b && b.list) || [];
        const age = CC.backupAge(s.id);
        const enc = list.length ? list.every((x) => x.encrypted) : null;
        return `<tr style="cursor:default"><td><a class="nm" href="#/site/${encodeURIComponent(s.id)}/backups" style="text-decoration:none;color:var(--ink)">${esc(s.name)}</a></td>
          <td>${!b ? `<span class="note">loading…</span>` : !list.length ? `<span class="b warn">None yet</span>` : `<span class="b ${age > 26 ? "warn" : "ok"}">${CC.ago(list[0].ts)}</span> <span class="note">${CC.kb(list[0].bytes)}</span>`}</td>
          <td class="hide-m">${list.length || "—"}</td>
          <td class="hide-m">${enc == null ? "—" : enc ? `<span class="b ok nodot">🔒 Encrypted</span>` : `<span class="b bad nodot">Not encrypted</span>`}</td>
          <td class="num"><div class="row" style="justify-content:flex-end"><a class="btn sm ghost" href="#/site/${encodeURIComponent(s.id)}/backups">History</a><button class="btn sm" data-act="now" data-site="${esc(s.id)}" ${s.reachable ? "" : "disabled"}>Back up now</button></div></td></tr>`;
      }).join("");
    },
  });

  // ---- VPN & remote access -----------------------------------------------------------------
  CC.route("/vpn", {
    enter() {
      CC.setCrumbs([{ label: "Overview", href: "#/" }, { label: "VPN & remote access" }]);
      view().innerHTML = `<div class="ph"><div><div class="eyebrow">Connectivity</div><h1>VPN & remote access</h1><p>Farm sites dial out to the hub over WireGuard. Your own phone or laptop can join too, to reach the office and every site from anywhere.</p></div>
        <div class="actions"><button class="btn pri" id="rm-add">${icon("plus")} Add phone or laptop</button></div></div>
        <div class="cols"><div>
          <section class="panel"><div class="phd"><h2>Your devices <small>operator access</small></h2></div><div id="rm-list"></div>
            <div class="pbd note" style="border-top:1px solid var(--line)">On the router, forward <b>UDP 51820</b> to this hub (${esc(location.hostname)}). These devices can reach every site, the hub and the office LAN.</div></section>
          <section class="panel"><div class="phd"><h2>Site tunnels</h2></div><div class="twrap"><table class="t"><thead><tr><th>Site</th><th>VPN address</th><th>State</th><th class="num">Latency</th><th class="hide-m">Pi login</th></tr></thead><tbody id="vpn-sites"></tbody></table></div></section>
        </div><div>
          <section class="panel"><div class="phd"><h2>${icon("shield")} Client isolation</h2></div><div class="pbd" id="vpn-iso"><div class="skel" style="height:90px"></div></div></section>
        </div></div>`;
      $("#rm-add").onclick = () => this.add();
      $("#rm-list").onclick = async (e) => {
        const b = e.target.closest("button[data-del]"); if (!b) return;
        if (!(await CC.confirm("Remove this device?", `${b.dataset.name} loses VPN access immediately.`, { ok: "Remove", danger: true }))) return;
        try { await api(`/api/hub/remote/${b.dataset.del}`, { method: "DELETE" }); CC.toast("Removed", "ok"); this.load(); } catch (err) { CC.toast(err.message, "bad"); }
      };
      this.load();
    },
    async load() {
      const [rm, iso] = await Promise.all([api("/api/hub/remote").catch(() => null), api("/api/hub/vpn-isolation").catch(() => null)]);
      S.remote = rm; S.vpn = iso; this.update();
    },
    update() {
      if (!$("#rm-list")) return;
      const rm = S.remote;
      $("#rm-list").innerHTML = !rm ? `<div class="pbd"><div class="skel" style="height:60px"></div></div>` : !rm.clients.length ? `<div class="empty"><b>No devices yet</b>Add your phone or laptop to reach the hub from anywhere.</div>` :
        `<ul class="alist">${rm.clients.map((c) => `<li><span class="sev info">${icon("phone", "")}</span><div><div class="t">${esc(c.name)}</div><div class="d mono">${esc(c.address)} · added ${CC.when(c.created)}</div></div><button class="btn sm danger" data-del="${esc(c.id)}" data-name="${esc(c.name)}">Remove</button></li>`).join("")}</ul>`;
      $("#vpn-sites").innerHTML = S.sites.map((s) => `<tr style="cursor:default"><td class="nm">${esc(s.name)}</td><td class="mono">${esc(s.vpn_ip)}${/^10\.8\./.test(s.vpn_ip) ? "" : ` <span class="note">(LAN)</span>`}</td><td>${s.reachable ? `<span class="b ok">Connected</span>` : `<span class="b bad">Unreachable</span>`}</td><td class="num mono">${s.latency_ms != null ? Math.round(s.latency_ms) + " ms" : "—"}</td>
        <td class="hide-m">${{ ok: `<span class="b ok">Protected</span>`, claimed: `<span class="b ok">Protected</span>`, mismatch: `<span class="b bad">Key mismatch</span>`, refused: `<span class="b warn">Not claimed</span>`, legacy: `<span class="b warn">Old image</span>` }[s.api_auth] || `<span class="note">—</span>`}</td></tr>`).join("");
      const iso = S.vpn;
      if (iso) {
        const on = iso.enabled && iso.applied;
        $("#vpn-iso").innerHTML = `<div class="row" style="margin-bottom:10px">${on ? `<span class="b ok">On</span>` : `<span class="b bad">${iso.enabled ? "Not applied" : "Switched off"}</span>`}<span class="note">checked ${CC.ago(iso.checked)}</span></div>
          <p style="margin:0 0 10px" class="muted">Farm sites can only answer the hub. They can't reach each other, your devices, the office LAN or the hub's own pages.</p>
          <dl class="kv"><dt>Allowed devices</dt><dd class="mono">${(iso.operators || []).join(", ") || "none"}</dd><dt>Firewall</dt><dd class="mono">${esc(iso.backend || "")}</dd>${iso.error ? `<dt>Error</dt><dd class="bad">${esc(iso.error)}</dd>` : ""}</dl>
          <p class="note" style="margin:10px 0 0">A device added straight in wg-easy (not here) is treated as a site and blocked. Off switch: <span class="mono">docker exec netwatch-hub python vpnfw.py off</span></p>`;
      }
    },
    add() {
      const d = CC.dialog(`<div class="dhd"><div><h2>Add phone or laptop</h2><p>Creates a WireGuard profile for one device</p></div><button class="btn icon ghost" data-close aria-label="Close">${icon("x")}</button></div>
        <form class="dbd" id="rm-form"><label class="fld">Device name<input class="inp" name="name" maxlength="40" placeholder="e.g. Riaan phone" required></label><div class="bad note" id="rm-err"></div>
        <div class="row" style="justify-content:flex-end"><button type="button" class="btn" data-close>Cancel</button><button class="btn pri">Create</button></div></form>`);
      $("#rm-form", d).onsubmit = async (e) => {
        e.preventDefault();
        await CC.busy(e.submitter, async () => {
          try {
            const j = await api("/api/hub/remote", { method: "POST", body: { name: e.target.name.value.trim() } });
            e.target.outerHTML = `<div class="dbd"><p class="muted" style="margin:0">Scan with the WireGuard app, or download the file for a laptop. Routes: <span class="mono">${esc(j.allowed_ips)}</span></p>
              <div class="qr">${j.qr_svg}</div>
              <div class="row" style="justify-content:flex-end"><button class="btn" id="rm-dl">${icon("down")} Download .conf</button><button class="btn pri" data-close>Done</button></div></div>`;
            $("#rm-dl", d).onclick = () => CC.download(new Blob([j.config], { type: "text/plain" }), `netwatch-${(j.name || "device").replace(/[^A-Za-z0-9_-]+/g, "-")}.conf`);
            this.load();
          } catch (err) { $("#rm-err", d).textContent = err.message; }
        });
      };
    },
  });

  // ---- settings ---------------------------------------------------------------------------
  CC.route("/settings/:tab", { enter(a) { settings.enter(a.tab); }, update() { settings.update(); } });
  CC.route("/settings", { enter() { settings.enter("sites"); }, update() { settings.update(); } });
  const settings = {
    tab: "sites",
    enter(tab) {
      this.tab = ["sites", "alerts"].includes(tab) ? tab : "sites";
      CC.setCrumbs([{ label: "Overview", href: "#/" }, { label: "Settings" }]);
      view().innerHTML = `<div class="ph"><div><div class="eyebrow">Hub</div><h1>Settings</h1></div></div>
        <nav class="tabs" aria-label="Settings"><a href="#/settings/sites" class="${this.tab === "sites" ? "on" : ""}">Sites</a><a href="#/settings/alerts" class="${this.tab === "alerts" ? "on" : ""}">Alerts & AI</a></nav>
        <div id="st-body"></div>`;
      this.tab === "sites" ? this.sites() : this.alerts();
    },
    update() { if (this.tab === "sites" && $("#st-sites")) this.drawSites(); },
    async sites() {
      $("#st-body").innerHTML = `<div class="cols"><div><section class="panel"><div class="phd"><h2>Sites</h2><button class="btn sm" id="st-manual">${icon("plus")} Add by address</button></div><div id="st-sites"><div class="pbd"><div class="skel" style="height:80px"></div></div></div></section></div>
        <div><section class="panel"><div class="phd"><h2>${icon("sparkle")} New-site wizard</h2></div>
          <form class="pbd" id="wz" style="display:grid;gap:12px"><p class="muted" style="margin:0">Creates the site's VPN client and gives you one command to paste on the farm Pi. It installs Docker, Netwatch and Kuma if missing, and links the site to this hub.</p>
            <label class="fld">Site id <span class="dim" style="font-weight:400">lowercase letters, digits and dashes</span><input class="inp" name="id" pattern="[a-z0-9][a-z0-9-]{0,31}" required placeholder="e.g. rietfontein"></label>
            <label class="fld">Display name<input class="inp" name="name" placeholder="e.g. Rietfontein Farm"></label>
            <label class="chk"><input type="checkbox" name="kuma" checked> Include Uptime Kuma</label>
            <div class="bad note" id="wz-err"></div><button class="btn pri">Create site</button></form></section></div></div>`;
      $("#wz").onsubmit = async (e) => {
        e.preventDefault();
        await CC.busy(e.submitter, async () => {
          try {
            const f = e.target;
            const j = await api("/api/hub/wizard", { method: "POST", body: { id: f.id.value.trim(), name: f.name.value.trim(), kuma: f.kuma.checked }, timeout: 30000 });
            f.reset(); $("#wz-err").textContent = "";
            this.enrollDialog(j);
            await this.loadSites(); CC.load({ deep: true });
          } catch (err) { $("#wz-err").textContent = err.message; }
        });
      };
      $("#st-manual").onclick = () => this.editDialog(null);
      $("#st-sites").onclick = (e) => {
        const b = e.target.closest("button[data-act]"); if (!b) return;
        const site = this.list.find((s) => s.id === b.dataset.id);
        if (b.dataset.act === "edit") this.editDialog(site);
        if (b.dataset.act === "enroll") api(`/api/hub/sites/${site.id}/enroll`).then((j) => this.enrollDialog({ ...j, site })).catch((err) => CC.toast(err.message, "bad"));
        if (b.dataset.act === "del") this.remove(site);
      };
      await this.loadSites();
    },
    async loadSites() { const j = await api("/api/hub/sites"); this.list = j.sites || []; this.drawSites(); },
    drawSites() {
      if (!this.list) return;
      $("#st-sites").innerHTML = !this.list.length ? `<div class="empty"><b>No sites</b>Use the wizard to add one.</div>` : `<ul class="alist">${this.list.map((s) => {
        const card = CC.site(s.id);
        return `<li><span class="sev ${card ? ({ ok: "info", warn: "warn", fault: "bad", offline: "bad", paused: "unk" })[CC.siteState(card)] : "unk"}">${icon("site")}</span>
          <div style="min-width:0"><div class="t">${esc(s.name || s.id)} ${s.enabled ? "" : `<span class="b unk">Paused</span>`}</div>
          <div class="d"><span class="mono">${esc(s.id)} · ${esc(s.vpn_ip)}:${s.netwatch_port}</span>${s.kuma_url ? ` · Kuma ${esc(s.kuma_url)}/${esc(s.kuma_status_slug || "")}` : " · no Kuma"}</div></div>
          <div class="row" style="flex-wrap:nowrap">${s.wg_client_id ? `<button class="btn sm" data-act="enroll" data-id="${esc(s.id)}" title="Show the install command again">Setup cmd</button>` : ""}<button class="btn sm" data-act="edit" data-id="${esc(s.id)}">Edit</button><button class="btn sm danger" data-act="del" data-id="${esc(s.id)}" aria-label="Remove ${esc(s.name || s.id)}">${icon("x")}</button></div></li>`;
      }).join("")}</ul>`;
    },
    enrollDialog(j) {
      const d = CC.dialog(`<div class="dhd"><div><h2>Set up ${esc((j.site && (j.site.name || j.site.id)) || "the site")}</h2><p>VPN address <span class="mono">${esc(j.vpn_ip)}</span></p></div><button class="btn icon ghost" data-close aria-label="Close">${icon("x")}</button></div>
        <div class="dbd"><div class="sect"><h3>Option A · one command on the farm Pi</h3><textarea class="inp mono" rows="4" readonly id="en-script">${esc(j.script)}</textarea><div class="row" style="margin-top:8px"><button class="btn sm" data-copy="en-script">Copy command</button></div></div>
        <div class="sect"><h3>Option B · WireGuard config only</h3><textarea class="inp mono" rows="6" readonly id="en-conf">${esc(j.conf)}</textarea><div class="row" style="margin-top:8px"><button class="btn sm" data-copy="en-conf">Copy config</button></div></div>
        <p class="note" style="margin:0">The command carries this site's private VPN key — send it only to whoever sets up the Pi.</p></div>
        <div class="dft"><button class="btn pri" data-close>Done</button></div>`, { cls: "modal wide" });
      $$("[data-copy]", d).forEach((b) => (b.onclick = () => CC.copy($("#" + b.dataset.copy, d).value, b)));
    },
    editDialog(site) {
      const s = site || { id: "", name: "", vpn_ip: "", netwatch_port: 8090, kuma_url: "", kuma_status_slug: "farm", enabled: true };
      const d = CC.dialog(`<div class="dhd"><div><h2>${site ? "Edit " + esc(site.name || site.id) : "Add site by address"}</h2><p>${site ? "Changes apply on the next poll" : "For a Netwatch already reachable from the hub"}</p></div><button class="btn icon ghost" data-close aria-label="Close">${icon("x")}</button></div>
        <form class="dbd" id="ed">
          <div class="grid2"><label class="fld">Site id<input class="inp" name="id" value="${esc(s.id)}" ${site ? "disabled" : "required"} pattern="[a-z0-9][a-z0-9-]{0,31}"></label>
          <label class="fld">Display name<input class="inp" name="name" value="${esc(s.name)}"></label>
          <label class="fld">VPN / LAN IP<input class="inp mono" name="vpn_ip" value="${esc(s.vpn_ip)}" required></label>
          <label class="fld">Netwatch port<input class="inp mono" name="netwatch_port" type="number" min="1" max="65535" value="${esc(s.netwatch_port)}"></label>
          <label class="fld">Kuma URL <span class="dim" style="font-weight:400">blank = none</span><input class="inp mono" name="kuma_url" value="${esc(s.kuma_url)}" placeholder="http://10.8.0.x:3001"></label>
          <label class="fld">Kuma status page slug<input class="inp mono" name="kuma_status_slug" value="${esc(s.kuma_status_slug)}"></label></div>
          <label class="chk"><input type="checkbox" name="enabled" ${s.enabled ? "checked" : ""}> Poll this site</label>
          <div class="bad note" id="ed-err"></div>
          <div class="row" style="justify-content:flex-end"><button type="button" class="btn" data-close>Cancel</button><button class="btn pri">Save</button></div></form>`, { cls: "modal wide" });
      $("#ed", d).onsubmit = async (e) => {
        e.preventDefault();
        const f = e.target;
        const body = { id: site ? site.id : f.id.value.trim(), name: f.name.value.trim(), vpn_ip: f.vpn_ip.value.trim(), netwatch_port: +f.netwatch_port.value || 8090, kuma_url: f.kuma_url.value.trim(), kuma_status_slug: f.kuma_status_slug.value.trim(), enabled: f.enabled.checked };
        await CC.busy(e.submitter, async () => {
          try {
            await api(site ? `/api/hub/sites/${site.id}` : "/api/hub/sites", { method: "POST", body });
            d.close(); CC.toast("Saved", "ok"); await this.loadSites(); CC.load({ deep: true });
          } catch (err) { $("#ed-err", d).textContent = err.message; }
        });
      };
    },
    async remove(site) {
      const extra = site.wg_client_id ? "\n\nIts VPN client is deleted too — the Pi would need a new setup command to rejoin." : "";
      if (!(await CC.confirm(`Remove ${site.name || site.id}?`, `The hub stops watching this site. The farm Pi itself keeps running.${extra}`, { ok: "Remove site", danger: true }))) return;
      try { await api(`/api/hub/sites/${site.id}`, { method: "DELETE" }); CC.toast("Site removed", "ok"); await this.loadSites(); CC.load({ deep: true }); }
      catch (e) { CC.toast(e.message, "bad"); }
    },
    async alerts() {
      $("#st-body").innerHTML = `<div class="grid2" style="align-items:start">
        <section class="panel"><div class="phd"><h2>${icon("bell")} Hub alerts</h2></div><form class="pbd" id="al" style="display:grid;gap:12px">
          <p class="muted" style="margin:0">Push notifications from the hub itself, via ntfy — separate from each site's own device alerts.</p>
          <label class="fld">ntfy server<input class="inp mono" name="ntfy_server"></label>
          <label class="fld">Topic <span class="dim" style="font-weight:400">blank = off · use a hard-to-guess name</span><input class="inp mono" name="ntfy_topic"></label>
          <label class="chk"><input type="checkbox" name="notify_site_offline"> Alert when a site goes offline</label>
          <label class="chk"><input type="checkbox" name="notify_ip_conflict"> Alert on new IP conflicts</label>
          <div class="note" id="al-msg" aria-live="polite"></div>
          <div class="row" style="justify-content:flex-end"><button type="button" class="btn" id="al-test">Send test</button><button class="btn pri">Save</button></div></form></section>
        <section class="panel"><div class="phd"><h2>${icon("sparkle")} AI reports</h2></div><form class="pbd" id="ai" style="display:grid;gap:12px">
          <p class="muted" style="margin:0">Google Gemini writes the site PDF reports and the Wi-Fi Doctor work lists. Device inventories are sent to Google when you use them.</p>
          <label class="fld">Gemini API key<input class="inp mono" name="key" type="password" autocomplete="off"></label>
          <label class="fld">Model<input class="inp mono" name="model"></label>
          <div class="note" id="ai-msg" aria-live="polite"></div>
          <div class="row" style="justify-content:flex-end"><button type="button" class="btn danger" id="ai-clear">Remove key</button><button class="btn pri">Save</button></div></form></section></div>`;
      const al = $("#al"), ai = $("#ai");
      try {
        const [a, g] = await Promise.all([api("/api/hub/alerts"), api("/api/hub/ai")]);
        al.ntfy_server.value = a.ntfy_server || "https://ntfy.sh"; al.ntfy_topic.value = a.ntfy_topic || "";
        al.notify_site_offline.checked = a.notify_site_offline !== false; al.notify_ip_conflict.checked = a.notify_ip_conflict !== false;
        ai.model.value = g.gemini_model || ""; ai.key.placeholder = g.key_set ? `saved (…${g.key_tail}) — leave blank to keep` : "not set";
        $("#ai-clear").disabled = !g.key_set;
      } catch (e) { CC.toast(e.message, "bad"); }
      const saveAlerts = () => api("/api/hub/alerts", { method: "POST", body: { ntfy_server: al.ntfy_server.value.trim(), ntfy_topic: al.ntfy_topic.value.trim(), notify_site_offline: al.notify_site_offline.checked, notify_ip_conflict: al.notify_ip_conflict.checked } });
      al.onsubmit = async (e) => { e.preventDefault(); await CC.busy(e.submitter, async () => { try { await saveAlerts(); $("#al-msg").textContent = "Saved."; } catch (err) { $("#al-msg").textContent = err.message; } }); };
      $("#al-test").onclick = (e) => CC.busy(e.currentTarget, async () => { try { await saveAlerts(); await api("/api/hub/alerts/test", { method: "POST" }); $("#al-msg").textContent = "Test sent — check your phone."; } catch (err) { $("#al-msg").textContent = err.message; } });
      ai.onsubmit = async (e) => {
        e.preventDefault();
        const body = { gemini_model: ai.model.value.trim() };
        if (ai.key.value.trim()) body.gemini_api_key = ai.key.value.trim();
        await CC.busy(e.submitter, async () => { try { await api("/api/hub/ai", { method: "POST", body }); ai.key.value = ""; $("#ai-msg").textContent = "Saved."; this.alerts(); } catch (err) { $("#ai-msg").textContent = err.message; } });
      };
      $("#ai-clear").onclick = async () => {
        if (!(await CC.confirm("Remove the Gemini key?", "AI reports and Wi-Fi Doctor stop working until a key is saved again.", { ok: "Remove", danger: true }))) return;
        try { await api("/api/hub/ai", { method: "POST", body: { gemini_model: ai.model.value.trim(), gemini_api_key: "" } }); CC.toast("Key removed", "ok"); this.alerts(); } catch (e) { CC.toast(e.message, "bad"); }
      };
    },
  };
})();
