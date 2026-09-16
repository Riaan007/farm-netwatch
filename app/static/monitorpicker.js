/* Netwatch "Monitored devices" picker — ONE component for the site's own page and
 * the hub's Control Center (the hub image copies this file to
 * /static/cc/monitorpicker.js at build time, like switchview.js). Vanilla JS, no
 * dependencies, its own scoped CSS (.mpk-*) on the dark palette both apps share.
 *
 *   MonitorPicker.open({
 *     site: "Tankwa Farm",                        heading suffix
 *     devices: [...],                             device records (key, ip, mac, name, online, last_seen, watch …)
 *     name(d) -> string                           display name
 *     type(d) -> {icon, label, group}             group: one of the `groups` keys
 *     groups: [[key, label], …]                   display order
 *     save({monitor: [keys], stop: [keys]}) -> Promise<result>   throws Error(message)
 *     note: "…"                                   optional line under the intro (what Kuma does here)
 *     pick: "cctv"                                optional quick pick applied on open
 *     preset: [keys]                              optional devices to tick on open (still needs Save)
 *     confirm(title, text) -> Promise<bool>       optional (default window.confirm)
 *   }) -> Promise<result | null>                  null when closed without saving
 */
(function () {
  "use strict";
  if (window.MonitorPicker) return;

  const STYLES = `
dialog.mpk{padding:0;border:1px solid rgba(148,163,184,.3);border-radius:16px;background:#0b1324;color:#e2e8f0;width:min(760px,calc(100vw - 24px));max-height:calc(100dvh - 32px);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;overflow:hidden}
dialog.mpk[open]{display:flex;flex-direction:column}
dialog.mpk::backdrop{background:rgba(2,6,23,.74);backdrop-filter:blur(3px)}
.mpk *{box-sizing:border-box}
.mpk button,.mpk input,.mpk select{font:inherit;color:inherit}
.mpk-hd{display:flex;gap:12px;align-items:flex-start;justify-content:space-between;padding:16px 18px 12px;border-bottom:1px solid rgba(148,163,184,.14)}
.mpk-hd h2{margin:0;font-size:18px;line-height:1.25}
.mpk-hd p{margin:4px 0 0;color:#94a3b8;font-size:13px}
.mpk-x{flex:none;width:34px;height:34px;border-radius:10px;border:1px solid rgba(148,163,184,.25);background:transparent;cursor:pointer;font-size:18px;line-height:1}
.mpk-x:hover{border-color:#22d3ee}
.mpk-tools{display:grid;gap:10px;padding:12px 18px;border-bottom:1px solid rgba(148,163,184,.14)}
.mpk-row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.mpk-q{flex:1 1 220px;min-width:0;height:36px;padding:0 11px;border-radius:10px;border:1px solid rgba(148,163,184,.28);background:rgba(2,6,23,.6)}
.mpk-q:focus,.mpk-sel:focus{outline:none;border-color:#22d3ee;box-shadow:0 0 0 3px rgba(34,211,238,.15)}
.mpk-sel{height:36px;border-radius:10px;border:1px solid rgba(148,163,184,.28);background:#0b1322;padding:0 8px}
.mpk-picks{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.mpk-picks span{font-size:12px;color:#64748b;margin-right:2px}
.mpk-pick{height:30px;padding:0 10px;border-radius:99px;border:1px solid rgba(148,163,184,.25);background:rgba(148,163,184,.07);font-size:12.5px;font-weight:600;cursor:pointer;white-space:nowrap}
.mpk-pick:hover{border-color:#22d3ee;color:#cffafe}
.mpk-pick.done{border-color:rgba(52,211,153,.45);color:#6ee7b7;background:rgba(16,185,129,.1)}
.mpk-pick b{color:#94a3b8;font-weight:600;margin-left:3px}
.mpk-list{overflow-y:auto;flex:1 1 auto;min-height:160px;padding:4px 0 8px;overscroll-behavior:contain}
.mpk-grp{display:flex;align-items:center;gap:10px;padding:12px 18px 6px;position:sticky;top:-4px;background:#0b1324;z-index:1}
.mpk-grp label{display:flex;align-items:center;gap:10px;cursor:pointer;font-size:11px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:#94a3b8}
.mpk-grp small{margin-left:auto;font-size:12px;color:#64748b;letter-spacing:0;text-transform:none;font-weight:600}
.mpk input[type=checkbox]{width:18px;height:18px;accent-color:#22d3ee;flex:none;cursor:pointer;margin:0}
.mpk-it{display:grid;grid-template-columns:18px 9px minmax(0,1fr) auto;gap:3px 12px;align-items:center;padding:8px 18px;cursor:pointer;border-left:3px solid transparent}
.mpk-it:hover{background:rgba(34,211,238,.05)}
.mpk-it.add{border-left-color:#34d399;background:rgba(16,185,129,.06)}
.mpk-it.rem{border-left-color:#fb7185;background:rgba(244,63,94,.06)}
.mpk-dot{width:9px;height:9px;border-radius:50%;background:#34d399}
.mpk-dot.off{background:#fb7185} .mpk-dot.quiet{background:#64748b}
.mpk-nm{min-width:0}
.mpk-nm b{display:block;font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mpk-nm small{display:block;font-size:12px;color:#64748b;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mpk-nm .mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:#a5f3fc}
.mpk-st{font-size:12px;color:#64748b;text-align:right;white-space:nowrap}
.mpk-st.off{color:#fda4af}
.mpk-st em{display:block;font-style:normal;font-size:11px;font-weight:700}
.mpk-it.add .mpk-st em{color:#6ee7b7} .mpk-it.rem .mpk-st em{color:#fda4af}
.mpk-empty{padding:34px 18px;text-align:center;color:#94a3b8}
.mpk-ft{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:12px 18px;border-top:1px solid rgba(148,163,184,.14);background:rgba(2,6,23,.35)}
.mpk-sum{font-size:13px;color:#94a3b8;margin-right:auto}
.mpk-sum b{color:#e2e8f0} .mpk-sum .p{color:#6ee7b7} .mpk-sum .m{color:#fda4af}
.mpk-err{flex-basis:100%;color:#fda4af;font-size:13px}
.mpk-btn{min-height:36px;padding:0 15px;border-radius:10px;border:1px solid rgba(148,163,184,.28);background:rgba(148,163,184,.08);font-weight:650;font-size:13px;cursor:pointer}
.mpk-btn:hover:not(:disabled){border-color:#22d3ee}
.mpk-btn:disabled{opacity:.5;cursor:not-allowed}
.mpk-btn.pri{background:#0891b2;border-color:#22d3ee;color:#fff}
.mpk-note{margin:6px 0 0;font-size:12px;color:#64748b}
@media (max-width:640px){
  dialog.mpk{width:100vw;max-width:100vw;height:100dvh;max-height:100dvh;border-radius:0;border:0}
  .mpk-hd,.mpk-tools,.mpk-ft{padding-left:14px;padding-right:14px}
  .mpk-it{padding-left:14px;padding-right:14px;gap:2px 10px}
  .mpk-grp{padding-left:14px;padding-right:14px}
  .mpk-picks{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;padding-bottom:2px}
  .mpk-ft .mpk-btn{flex:1}
  .mpk-sum{flex-basis:100%}
}`;

  const QUIET_AFTER = 7 * 86400;
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const now = () => Math.floor(Date.now() / 1000);
  const vshort = (v) => String(v || "").replace(/,?\s*(Co\.,?\s*Ltd\.?|Ltd\.?|Inc\.?|Corp\.?|Corporation)\s*$/i, "")
    .replace(/\s+(Digital\s+)?Technolog(y|ies)(\s+Co.*)?$/i, "").replace(/^Hangzhou\s+/i, "").trim();
  const ipNum = (ip) => (ip || "").split(".").reduce((a, o) => a * 256 + (+o || 0), 0) || Infinity;
  const ago = (ts) => {
    if (!ts) return "never seen";
    const s = Math.max(0, now() - ts);
    return s < 5400 ? Math.max(1, Math.round(s / 60)) + " min" : s < 129600 ? Math.round(s / 3600) + " h" : Math.round(s / 86400) + " d";
  };

  function injectCss() {
    if (document.getElementById("mpk-css")) return;
    const s = document.createElement("style");
    s.id = "mpk-css";
    s.textContent = STYLES;
    document.head.appendChild(s);
  }

  function open(o) {
    injectCss();
    const devices = (o.devices || []).filter((d) => d && d.key);
    const orig = new Map(devices.map((d) => [d.key, !!d.watch]));
    const want = new Map(orig);
    const type = (d) => (o.type && o.type(d)) || { icon: "•", label: d.category || "Device", group: "other" };
    const name = (d) => (o.name && o.name(d)) || d.name || d.ip || d.key;
    const groups = o.groups || [["other", "Devices"]];
    const gOf = (d) => { const g = type(d).group; return groups.some((x) => x[0] === g) ? g : groups[groups.length - 1][0]; };
    const recent = (d) => d.online || (d.last_seen && now() - d.last_seen < QUIET_AFTER);
    const ui = { q: "", show: "recent" };

    const dlg = document.createElement("dialog");
    dlg.className = "mpk";
    dlg.setAttribute("aria-labelledby", "mpk-title");
    dlg.innerHTML = `
      <div class="mpk-hd"><div style="min-width:0"><h2 id="mpk-title">Choose monitored devices${o.site ? ` · ${esc(o.site)}` : ""}</h2>
        <p>Tick the equipment you need to see online or offline. It is listed first, offline devices at the top, and the hub alerts when one drops. Everything else stays under “Other devices”.</p>
        ${o.note ? `<p class="mpk-note">${esc(o.note)}</p>` : ""}</div>
        <button type="button" class="mpk-x" data-x aria-label="Close">×</button></div>
      <div class="mpk-tools">
        <div class="mpk-row"><input class="mpk-q" type="search" placeholder="Find by name, IP, MAC, vendor…" aria-label="Find a device">
          <select class="mpk-sel" aria-label="Show"><option value="recent">Seen this week</option><option value="all">Everything, incl. not seen for 7+ days</option><option value="on">Ticked only</option><option value="off">Not ticked only</option></select></div>
        <div class="mpk-picks" data-picks></div>
      </div>
      <div class="mpk-list" data-list role="group" aria-label="Devices"></div>
      <div class="mpk-ft"><span class="mpk-sum" data-sum aria-live="polite"></span>
        <button type="button" class="mpk-btn" data-x>Cancel</button><button type="button" class="mpk-btn pri" data-save>Save</button>
        <div class="mpk-err" data-err hidden role="alert"></div></div>`;
    document.body.appendChild(dlg);
    const $ = (s) => dlg.querySelector(s);
    const list = $("[data-list]");

    const changes = () => {
      const monitor = [], stop = [];
      want.forEach((v, k) => { if (v !== orig.get(k)) (v ? monitor : stop).push(k); });
      return { monitor, stop };
    };
    const visible = () => {
      const q = ui.q.trim().toLowerCase();
      return devices.filter((d) => {
        const on = want.get(d.key);
        if (ui.show === "recent" && !recent(d) && !on && !orig.get(d.key)) return false;
        if (ui.show === "on" && !on) return false;
        if (ui.show === "off" && on) return false;
        if (!q) return true;
        const t = type(d);
        return [name(d), d.name, d.device_name, d.ip, d.mac, d.vendor, d.model, d.hostname, t.label].join(" ").toLowerCase().includes(q);
      });
    };

    function drawPicks() {
      const pool = devices.filter(recent);
      const picks = groups.filter(([g]) => g !== "unknown" && g !== "other").map(([g, label]) => [g, label, pool.filter((d) => gOf(d) === g)])
        .filter(([, , ds]) => ds.length);
      const ident = pool.filter((d) => gOf(d) !== "unknown" || d.name);
      const btn = (id, label, ds) => { const done = ds.every((d) => want.get(d.key)); return `<button type="button" class="mpk-pick ${done ? "done" : ""}" data-pick="${esc(id)}" title="${done ? "All ticked" : "Tick all of these"}">${done ? "✓ " : "+ "}${esc(label)}<b>${ds.length}</b></button>`; };
      $("[data-picks]").innerHTML = `<span>Quick:</span>${picks.map(([g, label, ds]) => btn(g, label, ds)).join("")}${ident.length ? btn("@ident", "Everything identified", ident) : ""}<button type="button" class="mpk-pick" data-pick="@none">Untick all</button>`;
      return { picks, ident };
    }
    function applyPick(id) {
      if (id === "@none") { want.forEach((_, k) => want.set(k, false)); return; }
      const pool = devices.filter(recent);
      const ds = id === "@ident" ? pool.filter((d) => gOf(d) !== "unknown" || d.name) : pool.filter((d) => gOf(d) === id);
      ds.forEach((d) => want.set(d.key, true));
    }

    function draw() {
      const rows = visible();
      const scroll = list.scrollTop;
      const act = document.activeElement;
      const refocus = act && list.contains(act) ? (act.dataset.key ? `[data-key="${CSS.escape(act.dataset.key)}"]` : act.dataset.grp ? `[data-grp="${CSS.escape(act.dataset.grp)}"]` : null) : null;
      let html = "";
      groups.forEach(([g, label]) => {
        const ds = rows.filter((d) => gOf(d) === g)
          .sort((a, b) => ipNum(a.ip) - ipNum(b.ip) || name(a).localeCompare(name(b)));
        if (!ds.length) return;
        const on = ds.filter((d) => want.get(d.key)).length;
        html += `<div class="mpk-grp"><label><input type="checkbox" data-grp="${esc(g)}" ${on === ds.length ? "checked" : ""} ${on && on < ds.length ? 'data-mixed="1"' : ""} aria-label="All ${esc(label)}">${esc(label)}</label><small>${on} of ${ds.length} ticked</small></div>`;
        html += ds.map((d) => {
          const w = want.get(d.key), was = orig.get(d.key);
          const t = type(d);
          const st = d.online ? "" : recent(d) || was ? "off" : "quiet";
          const seen = d.online ? "online" : st === "off" ? `offline ${ago(d.last_seen)}` : `not seen ${ago(d.last_seen)}`;
          const sub = [d.ip ? `<span class="mono">${esc(d.ip)}</span>` : "", `${t.icon} ${esc(t.label)}`, esc([d.model, vshort(d.vendor)].filter(Boolean).join(" · ") || d.mac || "")].filter(Boolean).join(" · ");
          return `<label class="mpk-it ${w && !was ? "add" : !w && was ? "rem" : ""}"><input type="checkbox" data-key="${esc(d.key)}" ${w ? "checked" : ""}>
            <i class="mpk-dot ${st}" aria-hidden="true"></i><span class="mpk-nm"><b>${esc(name(d))}</b><small>${sub}</small></span>
            <span class="mpk-st ${st}">${seen}${w && !was ? "<em>+ will be monitored</em>" : !w && was ? "<em>− will stop</em>" : ""}</span></label>`;
        }).join("");
      });
      list.innerHTML = html || `<div class="mpk-empty"><b>No devices match</b><br>${ui.show === "recent" ? "Devices not seen for 7+ days are hidden — pick “Everything” to see them." : "Clear the search or change what is shown."}</div>`;
      list.querySelectorAll("[data-mixed]").forEach((x) => (x.indeterminate = true));
      list.scrollTop = scroll;
      if (refocus) { const el = list.querySelector(refocus); if (el) el.focus({ preventScroll: true }); }
      drawPicks();
      const c = changes();
      const total = [...want.values()].filter(Boolean).length;
      $("[data-sum]").innerHTML = `<b>${total}</b> monitored${c.monitor.length ? ` · <span class="p">+${c.monitor.length}</span>` : ""}${c.stop.length ? ` · <span class="m">−${c.stop.length}</span>` : ""}${!c.monitor.length && !c.stop.length ? " · no changes yet" : ""}`;
      $("[data-save]").disabled = !(c.monitor.length || c.stop.length);
      $("[data-save]").textContent = c.monitor.length || c.stop.length ? `Save ${c.monitor.length + c.stop.length} change${c.monitor.length + c.stop.length === 1 ? "" : "s"}` : "Save";
    }

    return new Promise((resolve) => {
      let result = null, closing = false;
      const confirmFn = o.confirm || ((t, x) => Promise.resolve(window.confirm(t + "\n\n" + x)));
      const close = async () => {
        if (closing) return;
        const c = changes();
        const n = c.monitor.length + c.stop.length;
        if (n && result === null) {
          closing = true;
          const ok = await confirmFn("Discard your changes?", `${n} device${n === 1 ? "" : "s"} ticked or unticked here will stay as they were.`);
          closing = false;
          if (!ok) return;
        }
        dlg.close();
      };
      dlg.addEventListener("cancel", (e) => { e.preventDefault(); close(); });
      dlg.addEventListener("close", () => { dlg.remove(); resolve(result); });
      dlg.addEventListener("click", (e) => {
        if (e.target === dlg) return close();
        if (e.target.closest("[data-x]")) return close();
        const pick = e.target.closest("[data-pick]");
        if (pick) { applyPick(pick.dataset.pick); draw(); }
      });
      list.addEventListener("change", (e) => {
        const cb = e.target;
        if (cb.dataset.key) want.set(cb.dataset.key, cb.checked);
        else if (cb.dataset.grp) {
          visible().filter((d) => gOf(d) === cb.dataset.grp).forEach((d) => want.set(d.key, cb.checked));
        }
        draw();
      });
      $(".mpk-q").addEventListener("input", (e) => { ui.q = e.target.value; draw(); });
      $(".mpk-sel").addEventListener("change", (e) => { ui.show = e.target.value; draw(); });
      $("[data-save]").addEventListener("click", async (e) => {
        const btn = e.currentTarget, err = $("[data-err]");
        const c = changes();
        if (!c.monitor.length && !c.stop.length) return;
        btn.disabled = true; err.hidden = true;
        const label = btn.textContent;
        btn.textContent = "Saving…";
        try {
          result = await o.save(c);
          dlg.close();
        } catch (x) {
          err.textContent = (x && x.message) || "Could not save — try again.";
          err.hidden = false;
          btn.disabled = false; btn.textContent = label;
        }
      });
      if (o.pick) applyPick(o.pick);
      (o.preset || []).forEach((k) => { if (want.has(k)) want.set(k, true); });
      draw();
      dlg.showModal();
      $(".mpk-q").focus();
    });
  }

  window.MonitorPicker = { open, injectCss };
})();
