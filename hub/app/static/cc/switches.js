/* Netwatch Control Center — switches: the site tab "Switches" and the switch parts
 * of the device drawer. The switch picture itself is SwitchView (switchview.js,
 * the same file the site's own page uses). Data: core.js keeps S.switches[site]
 * (the site's /api/switches, every 2 min); one switch's detail and history are
 * fetched by the view when it is open. */
(function () {
  "use strict";
  const { $, esc, api, state: S } = CC;
  const enc = encodeURIComponent;
  const portNo = (id) => String(id || "").split("/").pop();
  const base = (siteId, key) => `/api/hub/sites/${enc(siteId)}/devices/${enc(key)}/switch`;

  function viewOpts(s, key, port) {
    const b = base(s.id, key);
    return {
      port,
      load: (hours, p) => api(`${b}?hours=${hours || 24}${p ? "&port=" + enc(p) : ""}`, { timeout: 45000 }),
      poll: () => api(`${b}/poll`, { method: "POST", timeout: 60000 }),
      action: (body) => api(`${b}/action`, { method: "POST", body, timeout: 100000 }),
      backups: {
        list: () => api(`${b}/backups`, { timeout: 30000 }),
        take: () => api(`${b}/backups`, { method: "POST", timeout: 80000 }),
        href: (name) => `${b}/backups/${enc(name)}`,
      },
      deviceHref: (d) => (d.ip || d.mac ? `#/site/${enc(s.id)}/devices?q=${enc(d.ip || d.mac)}` : null),
      onLogin: () => CC.openDevice(s.id, key),
      settingsHref: s.links && s.links.netwatch ? s.links.netwatch + "#/settings/advanced" : null,
      confirm: (title, text, o) => CC.confirm(title, text || "", { ok: "Yes, go ahead", danger: !!(o && o.danger) }),
      toast: (m, k) => CC.toast(m, k || ""),
    };
  }

  // ---- site tab ---------------------------------------------------------------------------
  CC.switchTab = {
    enter(s, el) {
      this.leave();
      this.site = s.id; this.el = el; this.sig = "";
      const q = CC.params();
      this.key = q.get("key"); this.port = q.get("port");
      el.innerHTML = `<div id="sw-list" class="grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px;margin-bottom:14px"></div><div id="sw-view"></div>`;
      this.update(s);
    },
    update(s) {
      if (!this.el || s.id !== this.site) return;
      const data = S.switches[s.id];
      const list = $("#sw-list", this.el), host = $("#sw-view", this.el);
      if (!list || !host) return;
      if (!data || (!data.switches && !data.error)) {
        if (!this.ctl) host.innerHTML = `<div class="skel" style="height:160px"></div>`;
        return;
      }
      if (data.legacy) { host.innerHTML = `<div class="empty"><b>This site's Netwatch is too old for switches</b>Update the site Pi (docker compose pull) to see its switch ports here.</div>`; return; }
      if (data.error && !data.switches) { host.innerHTML = `<div class="banner bad">${esc(data.error)}</div>`; return; }
      const sws = data.switches || [];
      if (!sws.length) {
        list.innerHTML = "";
        host.innerHTML = `<div class="empty"><b>No Ubiquiti switch at ${esc(s.name)}</b>EdgeSwitch / UISP switches show up here once the site has scanned one and its web login is saved on the site's Netwatch.</div>`;
        return;
      }
      const pick = this.key && sws.some((x) => x.key === this.key) ? this.key : sws[0].key;
      const sig = JSON.stringify(sws.map((x) => [x.key, x.read_ts, x.kind, (x.problems || []).length, pick]));
      if (sig !== this.sig) {
        this.sig = sig;
        list.hidden = sws.length < 2;
        list.innerHTML = sws.map((x) => `<div style="${x.key === pick ? "outline:2px solid rgba(34,211,238,.5);border-radius:14px" : ""}">${SwitchView.summaryCard(x, `#/site/${enc(s.id)}/switches?key=${enc(x.key)}`)}</div>`).join("");
      }
      if (this.mounted !== pick) {
        if (this.ctl) this.ctl.destroy();
        host.innerHTML = "";
        this.mounted = pick;
        this.ctl = SwitchView.mount(host, viewOpts(s, pick, this.port));
      }
    },
    leave() {
      if (this.ctl) this.ctl.destroy();
      this.ctl = null; this.mounted = null; this.el = null;
    },
  };

  // One line for a switch event in the site's History / Recent changes.
  const SW_EV = { link_down: "link lost", link_up: "link up", speed_change: "speed changed", poe_lost: "PoE power lost", poe_mode: "PoE setting changed",
    port_disabled: "switched off", port_enabled: "switched on", port_renamed: "renamed", device_moved: "device moved here", rebooted: "switch restarted" };
  const SW_ACT = { "port-off": "turn port off", "port-on": "turn port on", "poe-off": "PoE off", "poe-mode": "PoE mode", "poe-cycle": "PoE power cycle", name: "rename port", "cable-test": "cable test", locate: "blink LEDs", reboot: "restart", backup: "config backup" };
  CC.switchEventText = (e) => {
    const d = e.detail || {};
    const port = d.port ? `Port ${portNo(d.port)}${d.port_name && !/^port\s*\d+$/i.test(d.port_name) ? " (" + d.port_name + ")" : ""}` : "";
    const who = (d.devices || []).length ? " — " + d.devices.join(", ") : "";
    if (d.switch_event === "action") return `${SW_ACT[d.action] || d.action}${port ? " on " + port : ""} from Netwatch — ${d.result === "ok" ? "done" : "failed" + (d.error ? ": " + d.error : "")}`;
    if (d.switch_event === "speed_change") return `${port}: ${d.was} → ${d.speed} Mbit/s${who}`;
    return [port, SW_EV[d.switch_event] || d.switch_event].filter(Boolean).join(" ") + who;
  };

  // ---- device drawer ------------------------------------------------------------------------
  const LED = (p, probs) => {
    const pp = probs.filter((x) => x.port === p.id);
    if (pp.some((x) => x.level === "crit")) return "bad";
    if (!p.enabled || !p.up) return "unk";
    if (pp.length || (p.speed && p.speed < 1000)) return "warn";
    return "ok";
  };

  CC.switchDrawer = (dlg, siteId, d) => {
    const s = CC.site(siteId);
    const sws = CC.switchesOf(siteId);
    const sw = sws.find((x) => x.key === d.key);
    const identity = dlg.querySelector(".dbd .sect");
    if (!identity || !s) return;
    const sect = document.createElement("div");
    sect.className = "sect";

    if (sw || d.is_switch) {
      const probs = (sw && sw.problems) || [];
      const sum = (sw && sw.summary) || {}, poe = (sw && sw.poe) || {};
      const state = !sw ? "" : sw.kind === "no_login" ? `<span class="b warn">Login needed</span>` : sw.kind === "auth_failed" ? `<span class="b bad">Switch rejected the saved login</span>` : !(sw.ports || []).length ? `<span class="b unk">Not read yet</span>` : "";
      sect.innerHTML = `<h3>Switch</h3>
        ${state ? `<p style="margin:0 0 8px">${state}</p>` : ""}
        ${sw && (sw.ports || []).length ? `<div class="row" style="gap:5px;margin-bottom:8px">${sw.ports.map((p) => `<span class="b ${LED(p, probs)} nodot" title="Port ${esc(portNo(p.id))}${p.name ? " · " + esc(p.name) : ""}">${esc(portNo(p.id))}</span>`).join("")}</div>
          <p class="note" style="margin:0 0 8px">${sum.up}/${sum.ports} ports up · PoE ${poe.used_w ?? "—"}${poe.budget_w ? " of " + poe.budget_w : ""} W · read ${CC.ago(sw.read_ts)}</p>` : ""}
        ${probs.filter((p) => p.level !== "info").slice(0, 3).map((p) => `<div class="note ${p.level === "crit" ? "bad" : "warn"}" style="margin:0 0 4px">${p.level === "crit" ? "🔴" : "🟠"} ${esc(p.what)}</div>`).join("")}
        <div class="row"><a class="btn sm pri" href="#/site/${enc(siteId)}/switches?key=${enc(d.key)}" data-close>🔀 Open switch view</a></div>`;
      identity.after(sect);
      return;
    }

    const sp = d.switch_port;
    if (!sp) return;
    const host = sws.find((x) => x.key === sp.switch_key);
    const port = host && (host.ports || []).find((p) => p.id === sp.port);
    const hostName = host ? host.name : "a switch";
    const probs = host ? (host.problems || []).filter((p) => p.port === sp.port) : [];
    // Passive 24 V ports report no watts at all, so "PoE on with a link" is the test.
    const canCycle = host && (S.switches[siteId] || {}).manage && port && !port.protected && port.poe_mode && port.poe_mode !== "off" && port.up;
    sect.innerHTML = `<h3>Plugged into</h3>
      <p style="margin:0 0 6px"><a href="#/site/${enc(siteId)}/switches?key=${enc(sp.switch_key)}&port=${enc(sp.port)}" data-close><b>${esc(hostName)} · port ${esc(portNo(sp.port))}</b></a>${sp.port_name && !/^port\s*\d+$/i.test(sp.port_name) ? ` <span class="muted">(${esc(sp.port_name)})</span>` : ""}${sp.uplink ? ` <span class="note">through an uplink — the device is further down the line</span>` : ""}</p>
      ${port ? `<p class="note" style="margin:0 0 6px">${port.up ? `Link ${port.speed ? port.speed + " Mbit/s" : "up"}` : "No link"}${(port.poe_w || 0) >= 0.5 ? ` · drawing ${(+port.poe_w).toFixed(1)} W PoE` : ""}${port.up ? ` · ↓ ${SwitchView.fmt.bps(port.rx_bps)} ↑ ${SwitchView.fmt.bps(port.tx_bps)}` : ""}</p>` : ""}
      ${probs.map((p) => `<div class="note ${p.level === "crit" ? "bad" : "warn"}" style="margin:0 0 4px">${p.level === "crit" ? "🔴" : "🟠"} ${esc(p.what)}</div>`).join("")}
      ${canCycle ? `<div class="row"><button class="btn sm" id="dd-sw-cycle" title="PoE off for 8 seconds, then on again">⚡ Power cycle this device</button><span class="note" id="dd-sw-msg"></span></div>` : ""}`;
    identity.after(sect);
    const btn = sect.querySelector("#dd-sw-cycle");
    if (btn) btn.onclick = () => CC.busy(btn, async () => {
      const msg = sect.querySelector("#dd-sw-msg");
      if (!(await CC.confirm(`Power cycle ${CC.devName(d)}?`, `PoE on ${hostName} port ${portNo(sp.port)} goes off for 8 seconds and comes back on.${port && port.uplink ? ` ${port.uplink} devices are behind this port and lose their connection too.` : ""} The device restarts — cameras and radios take 1–2 minutes to come back.`, { ok: "Power cycle", danger: true }))) return;
      msg.textContent = "Switching PoE off and on…"; msg.className = "note";
      try {
        await api(`${base(siteId, sp.switch_key)}/action`, { method: "POST", body: { action: "poe-cycle", port: sp.port, off_s: 8 }, timeout: 100000 });
        msg.textContent = "✓ Power cycled — give it a minute or two"; msg.className = "note ok";
      } catch (e) {
        msg.textContent = e.status === 403 ? "Switch management is off for this site (turn on “Manage switches” in the site's Settings)" : e.message;
        msg.className = "note bad";
      }
    });
  };
})();
