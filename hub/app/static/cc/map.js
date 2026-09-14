/* Netwatch Control Center — maps.
 *
 * One component (FleetMap) behind three places: the fleet Map page, the small map
 * on the Overview, and a site's Map tab.
 *
 *   All sites  one marker per site (its state colour, name, device count). A site
 *              without its own GPS position but with placed devices sits at their
 *              centre, marked "approximate".
 *   One site   click a site marker (or open a site's Map tab): the map zooms to it
 *              and shows that site's device pins; the list becomes its devices.
 *
 * Only devices with a GPS position appear. Device positions live in each site's
 * device registry, site positions in the site's config (site.lat/lon) — both set
 * through the hub's proxy routes. Imagery: Esri World Imagery (free, no key).
 */
(function () {
  "use strict";
  const { $, esc, icon, api, state: S } = CC;
  const view = () => $("#view");
  const enc = encodeURIComponent;

  /* "-33.92, 18.42", "-33.92 18.42", Google Maps links (@lat,lon · q=lat,lon · !3dlat!4dlon)
     or DMS like 33°55'29.5"S 18°25'26.6"E  ->  {lat, lon} | null */
  CC.parseLatLon = (raw) => {
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
    return m ? ok(+m[1].replace(",", "."), +m[2].replace(",", ".")) : null;
  };
  CC.fmtLatLon = (g) => (g ? `${(+g.lat).toFixed(6)}, ${(+g.lon).toFixed(6)}` : "");
  CC.directions = (g) => `https://www.google.com/maps/dir/?api=1&destination=${g.lat},${g.lon}`;

  /** Save (or clear) a device position through the hub; patches the local copy. */
  CC.setLocation = async (siteId, key, body) => {
    const j = await api(`/api/hub/sites/${enc(siteId)}/devices/${enc(key)}/location`, { method: "POST", body });
    const d = (S.devices[siteId] || []).find((x) => x.key === key);
    if (d) d.geo = j.geo || null;
    CC.emit();
    return j.geo || null;
  };
  /** Save (or clear) a site's own position through the hub; patches the card. */
  CC.setSiteLocation = async (siteId, body) => {
    const j = await api(`/api/hub/sites/${enc(siteId)}/location`, { method: "POST", body });
    const s = CC.site(siteId);
    if (s) s.geo = j.geo || null;
    CC.emit();
    return j.geo || null;
  };
  CC.siteLocationDialog = (siteId) => {
    const s = CC.site(siteId); if (!s) return;
    const d = CC.dialog(`<div class="dhd"><div><h2>Location · ${esc(s.name)}</h2><p>Where the site is — the map shows its devices around this point</p></div><button class="btn icon ghost" data-close aria-label="Close">${icon("x")}</button></div>
      <form class="dbd" id="sl-form"><label class="fld">GPS position<input class="inp mono" name="pos" placeholder="-28.110100, 26.432100  or a Google Maps link" value="${s.geo ? CC.fmtLatLon(s.geo) : ""}" required></label>
        <p class="note" style="margin:0">Stored on the site Pi (its Settings → Site GPS position), so the site page and backups agree.</p>
        <div class="bad note" id="sl-err" aria-live="polite"></div>
        <div class="row" style="justify-content:space-between"><span>${s.geo ? `<button type="button" class="btn danger" id="sl-clear">Remove</button>` : ""}</span><span class="row"><button type="button" class="btn" data-close>Cancel</button><button class="btn pri">Save</button></span></div></form>`);
    $("#sl-form", d).onsubmit = async (e) => {
      e.preventDefault();
      const pos = CC.parseLatLon(e.target.pos.value);
      if (!pos) { $("#sl-err", d).textContent = "Not a position — use e.g. -28.1101, 26.4321 or a Google Maps link."; return; }
      await CC.busy(e.submitter, async () => {
        try { await CC.setSiteLocation(siteId, pos); d.close(); CC.toast(`${s.name} location saved`, "ok"); } catch (err) { $("#sl-err", d).textContent = err.message; }
      });
    };
    const clr = $("#sl-clear", d);
    if (clr) clr.onclick = async () => { try { await CC.setSiteLocation(siteId, { clear: true }); d.close(); CC.toast("Site location removed", "ok"); } catch (err) { $("#sl-err", d).textContent = err.message; } };
  };

  const pinCls = (d) => { const st = CC.devState(d); return st === "online" ? (d.category && d.category !== "unknown" ? "" : "myst") : st === "offline" ? "off" : "quiet"; };
  const stateDot = { ok: "", warn: "warn", fault: "bad", offline: "bad", paused: "unk" };

  /** Where a site is: its own GPS, else the centre of its placed devices (approximate). */
  CC.siteGeo = (s) => {
    if (s.geo && s.geo.lat != null) return { lat: s.geo.lat, lon: s.geo.lon, exact: true };
    const placed = (S.devices[s.id] || []).filter((d) => d.geo);
    if (!placed.length) return null;
    return { lat: placed.reduce((a, d) => a + d.geo.lat, 0) / placed.length, lon: placed.reduce((a, d) => a + d.geo.lon, 0) / placed.length, exact: false };
  };

  function tiles(mini) {
    const sat = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", { maxZoom: 21, maxNativeZoom: 19, attribution: "Imagery © Esri, Maxar, Earthstar Geographics, GIS User Community" });
    const places = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}", { maxZoom: 21, maxNativeZoom: 19 });
    if (mini) return { base: [sat, places] };
    const roads = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}", { maxZoom: 21, maxNativeZoom: 19, opacity: 0.85 });
    const street = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 21, maxNativeZoom: 19, attribution: "© OpenStreetMap contributors" });
    return { base: [sat, places], control: L.control.layers({ Satellite: sat, "Street map": street }, { "Place names": places, Roads: roads }, { position: "topright" }) };
  }

  class FleetMap {
    constructor(el, { mini = false, siteId = null } = {}) {
      this.el = el; this.mini = mini; this.fixedSite = siteId;
      this.focusSite = siteId; this.focusKey = null; this.placing = null;
      this.edit = false; this.tab = "on"; this.viewKey = ""; this.sig = "";
      this.map = null;
    }
    mount({ focusSite = null, focusKey = null, place = null } = {}) {
      if (focusSite && !this.fixedSite) this.focusSite = focusSite;
      this.focusKey = focusKey;
      const el = this.el;
      if (this.mini) {
        el.innerHTML = `<div class="ovmap-wrap" hidden><div class="ovmap-bar" hidden><button class="btn sm" data-back>← All sites</button><b data-site-name></b><span class="note" data-site-n></span><a class="btn sm ghost" data-site-open href="#">Open on the full map</a></div><div class="ovmap" role="application" aria-label="Map of sites and devices"></div></div>
          <div class="ovmap-empty pbd row" hidden><span class="muted" style="flex:1 1 300px">No site or device is on the map yet. Give each site its GPS location, then place its cameras, radios and switches.</span><a class="btn sm pri" href="#/map">Open the map</a></div>`;
      } else {
        el.innerHTML = `
          <div class="tbar" style="border:0;padding:0 0 12px">
            <button class="btn sm" data-back hidden>← All sites</button>
            <label class="search">${icon("search")}<span class="sr">Find</span><input class="inp" data-q type="search" placeholder="Find a site or device…"></label>
            <select class="sel" data-show aria-label="Show"><option value="">All placed devices</option><option value="online">Online</option><option value="offline">Offline</option><option value="cctv">Cameras &amp; recorders</option><option value="net">Network &amp; wireless</option></select>
            <label class="chk"><input type="checkbox" data-labels> Names</label>
            <button class="btn sm" data-edit hidden>${icon("lock")} <span>Move pins</span></button>
            <button class="btn sm" data-site-loc hidden>${icon("pin")} <span>Site location</span></button>
            <button class="btn sm" data-fit>Fit</button>
            <span class="note" data-sub style="margin-left:auto"></span>
          </div>
          <div class="mapgrid">
            <section class="panel mapbox"><div class="mapbanner" data-banner hidden><span data-banner-t></span><button data-cancel>Cancel</button></div><div class="mapel" role="application" aria-label="Map of sites and devices"></div></section>
            <aside class="panel"><div class="phd" data-list-head></div><p class="note" data-hint style="margin:10px 18px"></p><div class="mlist" data-list></div></aside>
          </div>`;
      }
      const q = (sel) => el.querySelector(sel);
      if (!window.L) { (q(".ovmap") || q(".mapel")).innerHTML = `<div class="empty"><b>Map library did not load</b>Reload the page.</div>`; return this; }
      const box = q(".ovmap") || q(".mapel");
      const t = tiles(this.mini);
      this.map = L.map(box, { layers: t.base, worldCopyJump: true, scrollWheelZoom: !this.mini }).setView([-29.0, 24.5], 5);
      if (t.control) t.control.addTo(this.map);
      if (!this.mini) L.control.scale({ imperial: false }).addTo(this.map);
      else { this.map.on("focus", () => this.map.scrollWheelZoom.enable()); this.map.on("blur", () => this.map.scrollWheelZoom.disable()); }
      this.sitesLayer = L.layerGroup().addTo(this.map);
      this.devLayer = L.layerGroup().addTo(this.map);
      this.map.on("click", (e) => { if (this.placing) this.savePlace(e.latlng.lat, e.latlng.lng); });
      el.addEventListener("click", (e) => {
        if (e.target.closest("[data-back]")) return this.focus(null);
        if (e.target.closest("[data-cancel]")) return this.cancelPlace();
        if (e.target.closest("[data-fit]")) return this.fit(true);
        if (e.target.closest("[data-site-loc]")) return this.siteLocMenu();
        const ed = e.target.closest("[data-edit]");
        if (ed) { this.edit = !this.edit; ed.classList.toggle("pri", this.edit); ed.querySelector("span").textContent = this.edit ? "Pins unlocked" : "Move pins"; return this.render(true); }
        const tab = e.target.closest("[data-tab]");
        if (tab) { e.preventDefault(); this.tab = tab.dataset.tab; return this.render(true); }
        const pl = e.target.closest("[data-place]");
        if (pl) { e.stopPropagation(); return this.startPlace({ type: pl.dataset.type, siteId: pl.dataset.site, key: pl.dataset.place }); }
        const fs = e.target.closest("[data-focus-site]");
        if (fs) return this.focus(fs.dataset.focusSite);
        const fk = e.target.closest("[data-focus-key]");
        if (fk) { this.focusKey = fk.dataset.focusKey; this.viewKey = ""; return this.render(true); }
      });
      const inp = q("[data-q]"); if (inp) inp.oninput = () => this.render(true);
      const show = q("[data-show]"); if (show) show.onchange = () => { this.viewKey = ""; this.render(true); };
      const lab = q("[data-labels]"); if (lab) lab.onchange = () => this.render(true);
      this.keyh = (e) => { if (e.key === "Escape" && this.placing) this.cancelPlace(); };
      document.addEventListener("keydown", this.keyh);
      setTimeout(() => {
        if (!this.map) return;
        this.map.invalidateSize();
        if (place) this.startPlace({ type: "device", siteId: this.focusSite, key: place });
        this.render(true);
      }, 60);
      return this;
    }
    destroy() {
      if (this.map) { this.map.remove(); this.map = null; }
      if (this.keyh) document.removeEventListener("keydown", this.keyh);
      document.body.classList.remove("placing");
    }
    q(sel) { return this.el.querySelector(sel); }
    query() { const i = this.q("[data-q]"); return i ? i.value.trim().toLowerCase() : ""; }
    focus(siteId) {
      if (this.fixedSite) return;
      this.focusSite = siteId; this.focusKey = null; this.viewKey = ""; this.tab = "on";
      this.cancelPlace(true);
      if (!this.mini) history.replaceState(null, "", siteId ? `#/map?site=${enc(siteId)}` : "#/map");
      this.render(true);
    }
    devices(siteId) {
      const s = CC.site(siteId); if (!s) return [];
      const q = this.query(), show = (this.q("[data-show]") || {}).value || "";
      const ipm = CC.ipMatcher(q);
      return (S.devices[siteId] || []).map((d) => Object.assign(d, { __site: s })).filter((d) => {
        if (q && !(ipm ? ipm(d.ip) : [CC.devName(d), d.device_name, d.ip, d.mac, d.vendor, d.model, (d.geo || {}).note].join(" ").toLowerCase().includes(q))) return false;
        const st = CC.devState(d);
        if (show === "online" && st !== "online") return false;
        if (show === "offline" && st !== "offline") return false;
        if ((show === "cctv" || show === "net") && CC.cat(d).group !== show) return false;
        return true;
      });
    }
    /** Live refreshes: never yank an open popup or a placement in progress. */
    softRender() {
      if (!this.map) return;
      const pop = this.map._popup;
      if ((pop && pop.isOpen && pop.isOpen()) || this.placing) return;
      this.render(false);
    }
    render(force) {
      if (!this.map || !this.el.isConnected) return;
      const siteGeos = S.sites.map((s) => [s, CC.siteGeo(s)]).filter(([, g]) => g);
      const focus = this.focusSite && CC.site(this.focusSite);
      const devs = focus ? this.devices(focus.id) : [];
      const placed = devs.filter((d) => d.geo);
      const labels = (this.q("[data-labels]") || {}).checked;
      if (this.mini) this.renderMiniChrome(focus, siteGeos, placed);
      else this.renderChrome(focus, siteGeos, devs, placed);
      const sig = JSON.stringify([this.focusSite, this.focusKey, this.edit, labels, this.query(),
        siteGeos.map(([s, g]) => [s.id, g.lat, g.lon, g.exact, CC.siteState(s), (S.devices[s.id] || []).filter((d) => d.geo).length]),
        placed.map((d) => [d.key, d.geo.lat, d.geo.lon, pinCls(d)])]);
      if (!force && sig === this.sig) return;
      this.sig = sig;

      // --- site markers (all sites; in a site view only that site, as its anchor)
      this.sitesLayer.clearLayers();
      const bounds = [];
      for (const [s, g] of siteGeos) {
        const here = focus && focus.id === s.id;
        if (focus && !here) continue;
        // inside a site, an approximate position is just the middle of its pins — no anchor
        if (here && !g.exact) continue;
        const st = CC.siteState(s);
        const c = CC.siteCounts(s);
        const nPlaced = (S.devices[s.id] || []).filter((d) => d.geo).length;
        const icon = here
          ? L.divIcon({ className: "sitepin-wrap", html: `<div class="sitepin here"><span class="home">🏠</span><b>${esc(s.name)}</b></div>`, iconSize: null })
          : L.divIcon({ className: "sitepin-wrap", html: `<div class="sitepin ${g.exact ? "" : "approx"}"><i class="dot ${stateDot[st]}"></i><b>${esc(s.name)}</b><span>${nPlaced} 📍</span></div>`, iconSize: null });
        const mk = L.marker([g.lat, g.lon], { icon, zIndexOffset: here ? -1000 : 1000, keyboard: true, title: s.name });
        mk.bindTooltip(`${esc(s.name)} · ${esc(CC.STATE[st].label)}${c.total != null ? ` · ${c.online}/${c.total} online` : ""}${g.exact ? "" : " · approximate position — set the site location"}${here ? "" : " · click to show its devices"}`, { className: "ptip", direction: "top", offset: [0, -20] });
        if (!here) mk.on("click", () => this.focus(s.id));
        mk.addTo(this.sitesLayer);
        bounds.push([g.lat, g.lon]);
      }

      // --- device pins: only for the site being looked at
      this.devLayer.clearLayers();
      for (const d of placed) {
        const ic = L.divIcon({ className: "", html: `<div class="pin ${pinCls(d)} ${this.focusKey === d.key ? "sel" : ""}"><span>${CC.cat(d).icon}</span></div>`, iconSize: [32, 32], iconAnchor: [16, 32], popupAnchor: [0, -30], tooltipAnchor: [0, -28] });
        const mk = L.marker([d.geo.lat, d.geo.lon], { icon: ic, draggable: this.edit && !this.mini, title: `${CC.devName(d)} ${d.ip}` });
        mk.bindTooltip(esc(CC.devName(d)), { className: "ptip", direction: "top", permanent: labels });
        mk.bindPopup(() => this.popup(d), { maxWidth: 290 });
        mk.on("popupopen", (e) => {
          const b = e.popup.getElement();
          b.querySelector("[data-open]").onclick = () => CC.openDevice(d.__site.id, d.key);
          const mv = b.querySelector("[data-move]");
          if (mv) mv.onclick = () => { this.map.closePopup(); this.startPlace({ type: "device", siteId: d.__site.id, key: d.key }); };
        });
        mk.on("dragend", (e) => { const ll = e.target.getLatLng(); this.saveDevice(d.__site.id, d.key, ll.lat, ll.lng, true); });
        mk.addTo(this.devLayer);
        bounds.push([d.geo.lat, d.geo.lon]);
      }

      // --- view: re-fit when what we're looking at changed, not on every refresh
      const vk = JSON.stringify([this.focusSite, this.focusKey, bounds.map((b) => b.map((x) => (+x).toFixed(5)))]);
      if (vk !== this.viewKey) {
        this.viewKey = vk;
        const fk = this.focusKey && placed.find((d) => d.key === this.focusKey);
        if (fk) {
          this.map.setView([fk.geo.lat, fk.geo.lon], Math.max(this.map.getZoom(), 18));
          setTimeout(() => this.devLayer && this.devLayer.eachLayer((l) => { const ll = l.getLatLng(); if (ll.lat === fk.geo.lat && ll.lng === fk.geo.lon) l.openPopup(); }), 80);
        } else this.fit(false, bounds, !!focus);
      }
    }
    fit(animate, bounds, siteView) {
      if (!this.map) return;
      if (!bounds) {
        const focus = this.focusSite && CC.site(this.focusSite);
        if (focus) {
          const g = CC.siteGeo(focus);
          bounds = this.devices(focus.id).filter((d) => d.geo).map((d) => [d.geo.lat, d.geo.lon]).concat(g ? [[g.lat, g.lon]] : []);
        } else bounds = S.sites.map(CC.siteGeo).filter(Boolean).map((g) => [g.lat, g.lon]);
        siteView = !!focus;
      }
      if (!bounds.length) return;
      if (bounds.length === 1) this.map.setView(bounds[0], siteView ? 17 : 9, { animate: !!animate });
      else this.map.fitBounds(bounds, { padding: [60, 60], maxZoom: siteView ? 19 : 12, animate: !!animate });
    }
    popup(d) {
      const st = CC.devState(d), g = d.geo;
      return `<div><b style="font-size:14px">${esc(CC.devName(d))}</b><br><span class="mono">${esc(d.ip)}</span> · ${CC.cat(d).icon} ${esc(CC.cat(d).label)}<br>
        <span style="color:${st === "online" ? "#34d399" : st === "offline" ? "#fb7185" : "#94a3b8"}">● ${st === "online" ? "Online" : st === "offline" ? "Offline · " + CC.ago(d.last_seen) : "Not seen for 7+ days"}</span><br>
        ${g.note ? `<span class="muted">${esc(g.note)}</span><br>` : ""}<span class="mono dim" style="font-size:11.5px">${CC.fmtLatLon(g)}</span>
        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:9px"><button class="btn sm pri" data-open>Open device</button>${this.mini ? `<a class="btn sm" href="#/map?site=${enc(d.__site.id)}&focus=${enc(d.key)}">Full map</a>` : `<button class="btn sm" data-move>Move</button>`}<a class="btn sm" href="${CC.directions(g)}" target="_blank" rel="noopener">Directions ↗</a></div></div>`;
    }
    renderMiniChrome(focus, siteGeos, placed) {
      const wrap = this.q(".ovmap-wrap"), empty = this.q(".ovmap-empty");
      const ready = S.loaded && !S.sites.some((s) => s.enabled && !S.devices[s.id]);
      const any = siteGeos.length > 0;
      if (wrap.hidden === any) { wrap.hidden = !any; if (any) setTimeout(() => { if (this.map) { this.map.invalidateSize(); this.viewKey = ""; this.render(true); } }, 30); }
      empty.hidden = any || !ready;
      const bar = this.q(".ovmap-bar");
      bar.hidden = !focus;
      if (focus) {
        bar.querySelector("[data-site-name]").textContent = focus.name;
        bar.querySelector("[data-site-n]").textContent = placed.length ? `${CC.plural(placed.length, "device")} on the map` : "no devices placed yet";
        bar.querySelector("[data-site-open]").href = `#/map?site=${enc(focus.id)}`;
      }
      const host = this.el.closest("section");
      const n = host && host.querySelector("[data-n]");
      if (n) n.textContent = any ? (focus ? `· ${focus.name}` : `${CC.plural(siteGeos.length, "site")} · click a site to see its devices`) : "";
    }
    renderChrome(focus, siteGeos, devs, placed) {
      const q = (s) => this.q(s);
      q("[data-back]").hidden = !focus || !!this.fixedSite;
      q("[data-edit]").hidden = !focus;
      q("[data-site-loc]").hidden = !focus;
      q("[data-q]").placeholder = focus ? "Find a device…" : "Find a site…";
      const head = q("[data-list-head]"), list = q("[data-list]"), hint = q("[data-hint]");
      if (!focus) {
        const query = this.query();
        const match = (s) => !query || s.name.toLowerCase().includes(query) || (s.location || "").toLowerCase().includes(query);
        const withLoc = siteGeos.map(([s]) => s);
        const without = S.sites.filter((s) => !CC.siteGeo(s));
        q("[data-sub]").textContent = `${CC.plural(withLoc.length, "site")} on the map${without.length ? ` · ${without.length} without a location` : ""}`;
        head.innerHTML = `<h2>Sites</h2>`;
        hint.textContent = withLoc.length ? "Click a site — on the map or here — to see its devices." : "Set each site's location to put it on the map.";
        list.innerHTML = withLoc.filter(match).map((s) => {
          const g = CC.siteGeo(s), n = (S.devices[s.id] || []).filter((d) => d.geo).length;
          return `<div class="mrow" data-focus-site="${esc(s.id)}"><i class="dot ${stateDot[CC.siteState(s)]}"></i><b>${esc(s.name)}</b><span class="note">${n} 📍</span><span class="note sub">${esc(s.location || "")}${g.exact ? "" : " · approximate — set its location"}</span></div>`;
        }).join("") + (without.length ? `<div class="mhead">No location yet</div>` + without.filter(match).map((s) => `<div class="mrow"><i class="dot ${stateDot[CC.siteState(s)]}"></i><b>${esc(s.name)}</b><button class="btn sm ${this.placing && this.placing.type === "site" && this.placing.siteId === s.id ? "pri" : ""}" data-type="site" data-site="${esc(s.id)}" data-place="${esc(s.id)}">Place</button><span class="note sub">${esc(s.location || s.vpn_ip)}</span></div>`).join("") : "")
          || `<div class="empty">No sites match.</div>`;
        return;
      }
      const unplaced = devs.filter((d) => !d.geo && CC.devState(d) !== "quiet");
      const sg = CC.siteGeo(focus);
      q("[data-site-loc]").querySelector("span").textContent = focus.geo ? "Site location" : "Set site location";
      q("[data-site-loc]").classList.toggle("pri", !focus.geo);
      q("[data-sub]").textContent = `${focus.name} · ${placed.length} of ${devs.length} devices placed${sg ? (sg.exact ? "" : " · site position approximate") : " · site has no location yet"}`;
      head.innerHTML = `<div class="tabs" style="margin:0;border:0"><a href="#" data-tab="on" class="${this.tab === "on" ? "on" : ""}">On the map <span class="count quiet">${placed.length}</span></a><a href="#" data-tab="off" class="${this.tab === "off" ? "on" : ""}">Not placed <span class="count quiet">${unplaced.length}</span></a></div>`;
      if (this.tab === "on") {
        hint.textContent = placed.length ? "Click a device to fly to it." : "Nothing placed at this site yet — use Not placed.";
        list.innerHTML = placed.map((d) => `<div class="mrow" data-focus-key="${esc(d.key)}"><i class="dot ${CC.devState(d) === "online" ? "" : CC.devState(d) === "offline" ? "bad" : "unk"}"></i><b>${CC.cat(d).icon} ${esc(CC.devName(d))}</b><span class="mono cyan" style="font-size:12px">${esc(d.ip)}</span><span class="note sub">${esc(d.geo.note || CC.cat(d).label)}</span></div>`).join("") || `<div class="empty">No placed devices match.</div>`;
      } else {
        hint.textContent = "Click Place, then click where the device is on the map.";
        list.innerHTML = unplaced.slice(0, 300).map((d) => {
          const on = this.placing && this.placing.type === "device" && this.placing.key === d.key;
          return `<div class="mrow"><i class="dot ${CC.devState(d) === "online" ? "" : "bad"}"></i><b>${CC.cat(d).icon} ${esc(CC.devName(d))}</b><button class="btn sm ${on ? "pri" : ""}" data-place="${esc(d.key)}" data-type="device" data-site="${esc(focus.id)}">${on ? "Placing…" : "Place"}</button><span class="note sub"><span class="mono">${esc(d.ip)}</span> · ${esc(CC.cat(d).label)}</span></div>`;
        }).join("") || `<div class="empty"><b>Everything is placed</b>Every device seen this week has a position.</div>`;
      }
    }
    siteLocMenu() {
      const s = CC.site(this.focusSite); if (!s) return;
      const d = CC.dialog(`<div class="dhd"><div><h2>${esc(s.name)} location</h2><p>${s.geo ? CC.fmtLatLon(s.geo) : "Not set yet"}</p></div><button class="btn icon ghost" data-close aria-label="Close">${icon("x")}</button></div>
        <div class="dbd"><button class="btn pri" data-a="click">🗺️ Click the spot on the map</button><button class="btn" data-a="type">Type or paste coordinates</button>${s.geo ? `<button class="btn danger" data-a="clear">Remove the site location</button>` : ""}</div>`);
      d.addEventListener("click", async (e) => {
        const a = e.target.closest("[data-a]"); if (!a) return;
        d.close();
        if (a.dataset.a === "click") this.startPlace({ type: "site", siteId: s.id, key: s.id });
        if (a.dataset.a === "type") CC.siteLocationDialog(s.id);
        if (a.dataset.a === "clear") { try { await CC.setSiteLocation(s.id, { clear: true }); CC.toast("Site location removed", "ok"); } catch (err) { CC.toast(err.message, "bad"); } }
      });
    }
    startPlace(p) {
      if (this.mini) return;
      let label = "";
      if (p.type === "site") label = (CC.site(p.siteId) || {}).name || "";
      else { const d = (S.devices[p.siteId] || []).find((x) => x.key === p.key); label = d ? `${CC.devName(d)} (${d.ip})` : ""; }
      if (!label) return;
      if (p.type === "device" && this.focusSite !== p.siteId && !this.fixedSite) { this.focusSite = p.siteId; this.viewKey = ""; }
      this.placing = p;
      document.body.classList.add("placing");
      this.q("[data-banner-t]").textContent = p.type === "site" ? `Click where the ${label} site is` : `Click where ${label} is`;
      this.q("[data-banner]").hidden = false;
      this.render(true);
    }
    cancelPlace(silent) {
      this.placing = null;
      document.body.classList.remove("placing");
      const b = this.q("[data-banner]"); if (b) b.hidden = true;
      if (!silent) this.render(true);
    }
    async savePlace(lat, lon) {
      const p = this.placing; if (!p) return;
      if (p.type === "site") {
        try { await CC.setSiteLocation(p.siteId, { lat, lon }); CC.toast(`${CC.site(p.siteId).name} location saved`, "ok"); this.cancelPlace(true); this.viewKey = ""; this.render(true); }
        catch (e) { CC.toast(`Could not save: ${e.message}`, "bad"); }
      } else await this.saveDevice(p.siteId, p.key, lat, lon, false);
    }
    async saveDevice(siteId, key, lat, lon, moved) {
      const d = (S.devices[siteId] || []).find((x) => x.key === key);
      try {
        await CC.setLocation(siteId, key, { lat, lon, note: (d && d.geo && d.geo.note) || "" });
        CC.toast(`${d ? CC.devName(d) : "Device"} ${moved ? "moved" : "placed"}`, "ok");
        this.cancelPlace(true); this.focusKey = key; this.render(true);
      } catch (e) { CC.toast(`Could not save the position: ${e.message}`, "bad"); if (moved) this.render(true); }
    }
  }
  CC.FleetMap = FleetMap;

  /* ---- adapters used by the pages ---- */
  CC.miniMap = (el) => {
    const fm = new FleetMap(el, { mini: true }).mount();
    return { update: () => fm.softRender(), destroy: () => fm.destroy() };
  };
  let active = null;
  CC.mapView = {
    mount(el, { siteId = null } = {}) {
      if (active) active.destroy();
      const p = CC.params();
      active = new FleetMap(el, { siteId }).mount({ focusSite: p.get("site"), focusKey: p.get("focus"), place: p.get("place") });
    },
    softDraw() { if (active) active.softRender(); },
    destroy() { if (active) { active.destroy(); active = null; } },
  };

  CC.route("/map", {
    enter() {
      CC.setCrumbs([{ label: "Overview", href: "#/" }, { label: "Map" }]);
      view().innerHTML = `<div class="ph"><div><div class="eyebrow">Fleet</div><h1>Sites &amp; equipment map</h1><p>Every site at its location — click one to see its devices. Only devices with a GPS position appear. Satellite imagery © Esri.</p></div></div><div id="mp-host"></div>`;
      CC.mapView.mount($("#mp-host"));
    },
    update() { CC.mapView.softDraw(); },
    leave() { CC.mapView.destroy(); },
  });
})();
