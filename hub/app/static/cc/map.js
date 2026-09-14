/* Netwatch Control Center — device map (fleet and per site).
 * Only devices a site has given a GPS position appear; imagery is Esri World
 * Imagery (free, no key, attribution shown). Positions are set on the site
 * (stored in its device registry) — from here through the hub's proxy route. */
(function () {
  "use strict";
  const { $, esc, icon, api, state: S } = CC;
  const view = () => $("#view");
  const enc = encodeURIComponent;

  /* Accepts "-33.92, 18.42", "-33.92 18.42", Google Maps links (@lat,lon · q=lat,lon ·
     !3dlat!4dlon) and DMS like 33°55'29.5"S 18°25'26.6"E. Returns {lat, lon} or null. */
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

  const M = {
    map: null, layer: null, siteId: null, placing: null, edit: false, tab: "on", fitted: false, focus: null, el: null,

    mount(el, { siteId = null } = {}) {
      this.destroy();
      this.siteId = siteId; this.el = el; this.fitted = false; this.tab = "on";
      const p = CC.params();
      this.focus = p.get("focus"); const place = p.get("place");
      el.innerHTML = `
        <div class="tbar" style="border:0;padding:0 0 12px">
          <label class="search">${icon("search")}<span class="sr">Find a device</span><input class="inp" id="mp-q" type="search" placeholder="Find a device…"></label>
          ${siteId ? "" : `<select class="sel" id="mp-site" aria-label="Site"><option value="">All sites</option>${S.sites.map((s) => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join("")}</select>`}
          <select class="sel" id="mp-show" aria-label="Show"><option value="">All placed devices</option><option value="online">Online</option><option value="offline">Offline</option><option value="cctv">Cameras &amp; recorders</option><option value="net">Network &amp; wireless</option></select>
          <label class="chk"><input type="checkbox" id="mp-labels"> Names</label>
          <button class="btn sm" id="mp-edit">${icon("lock")} <span>Move pins</span></button>
          <button class="btn sm" id="mp-fit">Fit all</button>
          <span class="note" id="mp-sub" style="margin-left:auto"></span>
        </div>
        <div class="mapgrid">
          <section class="panel mapbox"><div class="mapbanner" id="mp-banner" hidden><span id="mp-banner-t"></span><button id="mp-cancel">Cancel</button></div><div class="mapel" id="mp-map" role="application" aria-label="Device map"></div></section>
          <aside class="panel">
            <div class="phd"><div class="tabs" style="margin:0;border:0"><a href="#" data-t="on" class="on">On the map <span class="count quiet" id="mp-n-on">0</span></a><a href="#" data-t="off">Not placed <span class="count quiet" id="mp-n-off">0</span></a></div></div>
            <p class="note" id="mp-hint" style="margin:10px 18px"></p>
            <div class="mlist" id="mp-list"></div>
          </aside>
        </div>`;
      if (!window.L) { $("#mp-map").innerHTML = `<div class="empty"><b>Map library did not load</b>Reload the page.</div>`; return; }
      const sat = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", { maxZoom: 21, maxNativeZoom: 19, attribution: "Imagery © Esri, Maxar, Earthstar Geographics, GIS User Community" });
      const labels = L.layerGroup([
        L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}", { maxZoom: 21, maxNativeZoom: 19, opacity: 0.85 }),
        L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}", { maxZoom: 21, maxNativeZoom: 19 }),
      ]);
      const street = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 21, maxNativeZoom: 19, attribution: "© OpenStreetMap contributors" });
      this.map = L.map($("#mp-map"), { layers: [sat], worldCopyJump: true }).setView([-29.0, 24.5], 5);
      L.control.layers({ Satellite: sat, "Street map": street }, { "Roads & place names": labels }, { position: "topright" }).addTo(this.map);
      L.control.scale({ imperial: false }).addTo(this.map);
      this.layer = L.layerGroup().addTo(this.map);
      this.map.on("click", (e) => { if (this.placing) this.save(this.placing, e.latlng.lat, e.latlng.lng); });
      const redraw = () => this.draw();
      $("#mp-q", el).oninput = redraw;
      $("#mp-show", el).onchange = () => { this.fitted = false; redraw(); };
      if (!siteId) $("#mp-site", el).onchange = () => { this.fitted = false; redraw(); };
      $("#mp-labels", el).onchange = redraw;
      $("#mp-fit", el).onclick = () => this.fit(true);
      $("#mp-cancel", el).onclick = () => this.cancel();
      $("#mp-edit", el).onclick = (e) => { this.edit = !this.edit; e.currentTarget.classList.toggle("pri", this.edit); e.currentTarget.querySelector("span").textContent = this.edit ? "Pins unlocked" : "Move pins"; redraw(); };
      el.querySelectorAll(".tabs a").forEach((a) => (a.onclick = (e) => { e.preventDefault(); this.tab = a.dataset.t; el.querySelectorAll(".tabs a").forEach((x) => x.classList.toggle("on", x === a)); redraw(); }));
      $("#mp-list", el).onclick = (e) => {
        const b = e.target.closest("[data-place]"); if (b) { e.stopPropagation(); return this.start(b.dataset.site, b.dataset.place); }
        const row = e.target.closest("[data-focus]"); if (row) { this.focus = row.dataset.focus; this.draw(); }
      };
      this.keyh = (e) => { if (e.key === "Escape" && this.placing) this.cancel(); };
      document.addEventListener("keydown", this.keyh);
      setTimeout(() => { if (!this.map) return; this.map.invalidateSize(); if (place) this.start(siteId || p.get("site"), place); this.draw(); }, 60);
    },
    destroy() {
      if (this.map) { this.map.remove(); this.map = null; }
      if (this.keyh) document.removeEventListener("keydown", this.keyh);
      document.body.classList.remove("placing");
      this.placing = null;
    },
    devices() {
      const siteId = this.siteId || ($("#mp-site") && $("#mp-site").value) || "";
      const sites = siteId ? [CC.site(siteId)].filter(Boolean) : S.sites;
      const q = ($("#mp-q") && $("#mp-q").value.trim().toLowerCase()) || "";
      const show = ($("#mp-show") && $("#mp-show").value) || "";
      const ipm = CC.ipMatcher(q);
      return sites.flatMap((s) => (S.devices[s.id] || []).map((d) => Object.assign(d, { __site: s }))).filter((d) => {
        if (q && !(ipm ? ipm(d.ip) : [CC.devName(d), d.ip, d.mac, d.vendor, d.model, d.__site.name, (d.geo || {}).note].join(" ").toLowerCase().includes(q))) return false;
        const st = CC.devState(d);
        if (show === "online" && st !== "online") return false;
        if (show === "offline" && st !== "offline") return false;
        if ((show === "cctv" || show === "net") && CC.cat(d).group !== show) return false;
        return true;
      });
    },
    /** Live refreshes: never yank an open popup or a pin being dragged. */
    softDraw() {
      if (!this.map) return;
      const pop = this.map._popup;
      if (pop && pop.isOpen && pop.isOpen()) return;
      this.draw();
    },
    pinCls(d) { const st = CC.devState(d); return st === "online" ? (d.category && d.category !== "unknown" ? "" : "myst") : st === "offline" ? "off" : "quiet"; },
    draw() {
      if (!this.map || !this.el || !document.body.contains(this.el)) return;
      const all = this.devices();
      const placed = all.filter((d) => d.geo), unplaced = all.filter((d) => !d.geo && CC.devState(d) !== "quiet");
      const labels = $("#mp-labels").checked;
      this.layer.clearLayers();
      const bounds = [];
      for (const d of placed) {
        const k = d.__site.id + "|" + d.key;
        const ic = L.divIcon({ className: "", html: `<div class="pin ${this.pinCls(d)} ${this.focus === d.key ? "sel" : ""}"><span>${CC.cat(d).icon}</span></div>`, iconSize: [32, 32], iconAnchor: [16, 32], popupAnchor: [0, -30], tooltipAnchor: [0, -28] });
        const mk = L.marker([d.geo.lat, d.geo.lon], { icon: ic, draggable: this.edit, title: `${CC.devName(d)} ${d.ip}` });
        mk.bindTooltip(esc(CC.devName(d)), { className: "ptip", direction: "top", permanent: labels });
        mk.bindPopup(() => this.popup(d), { maxWidth: 290 });
        mk.on("dragend", (e) => { const ll = e.target.getLatLng(); this.save(k, ll.lat, ll.lng, true); });
        mk.on("popupopen", (e) => {
          const box = e.popup.getElement();
          box.querySelector("[data-open]").onclick = () => CC.openDevice(d.__site.id, d.key);
          box.querySelector("[data-move]").onclick = () => { this.map.closePopup(); this.start(d.__site.id, d.key); };
        });
        mk.addTo(this.layer);
        bounds.push([d.geo.lat, d.geo.lon]);
        if (this.focus === d.key) setTimeout(() => { if (!this.map) return; this.map.setView([d.geo.lat, d.geo.lon], Math.max(this.map.getZoom(), 18)); mk.openPopup(); }, 60);
      }
      if (!this.fitted && !this.focus) { if (bounds.length) this.fit(false, bounds); this.fitted = true; }
      const total = all.length;
      $("#mp-sub").textContent = placed.length ? `${placed.length} of ${total} devices placed · only devices with a GPS position are shown` : "No device has a GPS position yet — use “Not placed” to put one on the map";
      $("#mp-n-on").textContent = placed.length; $("#mp-n-off").textContent = unplaced.length;
      const box = $("#mp-list"), hint = $("#mp-hint");
      const siteLbl = (d) => (this.siteId ? "" : ` · ${esc(d.__site.name)}`);
      if (this.tab === "on") {
        hint.textContent = placed.length ? "Click a device to fly to it." : "Nothing placed yet.";
        box.innerHTML = placed.map((d) => `<div class="alist-row" data-focus="${esc(d.key)}" style="display:grid;grid-template-columns:auto 1fr auto;gap:2px 10px;align-items:center;padding:9px 18px;border-top:1px solid var(--line);cursor:pointer">
          <i class="dot ${CC.devState(d) === "online" ? "" : CC.devState(d) === "offline" ? "bad" : "unk"}"></i><b style="font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${CC.cat(d).icon} ${esc(CC.devName(d))}</b><span class="mono cyan" style="font-size:12px">${esc(d.ip)}</span>
          <span class="note" style="grid-column:2/4">${esc((d.geo.note || "") || CC.cat(d).label)}${siteLbl(d)}</span></div>`).join("") || `<div class="empty">No placed devices match.</div>`;
      } else {
        hint.textContent = this.siteId || S.sites.length === 1 ? "Click Place, then click where the device is on the map." : "Click Place, then click where the device is. Pick a site above to narrow the list.";
        box.innerHTML = unplaced.slice(0, 300).map((d) => `<div style="display:grid;grid-template-columns:auto 1fr auto;gap:2px 10px;align-items:center;padding:9px 18px;border-top:1px solid var(--line)">
          <i class="dot ${CC.devState(d) === "online" ? "" : "bad"}"></i><b style="font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${CC.cat(d).icon} ${esc(CC.devName(d))}</b>
          <button class="btn sm ${this.placing === d.__site.id + "|" + d.key ? "pri" : ""}" data-place="${esc(d.key)}" data-site="${esc(d.__site.id)}">${this.placing === d.__site.id + "|" + d.key ? "Placing…" : "Place"}</button>
          <span class="note" style="grid-column:2/4"><span class="mono">${esc(d.ip)}</span> · ${esc(CC.cat(d).label)}${siteLbl(d)}</span></div>`).join("") || `<div class="empty"><b>Everything is placed</b>Every device seen this week has a position.</div>`;
      }
    },
    popup(d) {
      const st = CC.devState(d), g = d.geo;
      return `<div><b style="font-size:14px">${esc(CC.devName(d))}</b><br>
        <span class="mono">${esc(d.ip)}</span> · ${CC.cat(d).icon} ${esc(CC.cat(d).label)}${this.siteId ? "" : ` · ${esc(d.__site.name)}`}<br>
        <span style="color:${st === "online" ? "#34d399" : st === "offline" ? "#fb7185" : "#94a3b8"}">● ${st === "online" ? "Online" : st === "offline" ? "Offline · " + CC.ago(d.last_seen) : "Not seen for 7+ days"}</span><br>
        ${g.note ? `<span class="muted">${esc(g.note)}</span><br>` : ""}<span class="mono dim" style="font-size:11.5px">${CC.fmtLatLon(g)}</span>
        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:9px"><button class="btn sm pri" data-open>Open device</button><button class="btn sm" data-move>Move</button><a class="btn sm" href="${CC.directions(g)}" target="_blank" rel="noopener">Directions ↗</a></div></div>`;
    },
    fit(animate, bounds) {
      if (!this.map) return;
      bounds = bounds || this.devices().filter((d) => d.geo).map((d) => [d.geo.lat, d.geo.lon]);
      if (!bounds.length) return;
      if (bounds.length === 1) this.map.setView(bounds[0], 18, { animate: !!animate });
      else this.map.fitBounds(bounds, { padding: [50, 50], maxZoom: 19, animate: !!animate });
    },
    start(siteId, key) {
      const d = (S.devices[siteId] || []).find((x) => x.key === key);
      if (!d) return;
      this.placing = siteId + "|" + key;
      document.body.classList.add("placing");
      $("#mp-banner-t").textContent = `Click where ${CC.devName(d)} (${d.ip}) is`;
      $("#mp-banner").hidden = false;
      this.draw();
    },
    cancel() {
      this.placing = null;
      document.body.classList.remove("placing");
      if ($("#mp-banner")) $("#mp-banner").hidden = true;
      this.draw();
    },
    async save(ref, lat, lon, moved) {
      const [siteId, key] = ref.split("|");
      const d = (S.devices[siteId] || []).find((x) => x.key === key);
      try {
        await CC.setLocation(siteId, key, { lat, lon, note: (d && d.geo && d.geo.note) || "" });
        CC.toast(`${d ? CC.devName(d) : "Device"} ${moved ? "moved" : "placed"}`, "ok");
        this.placing = null; document.body.classList.remove("placing"); $("#mp-banner").hidden = true;
        this.focus = key; this.draw();
      } catch (e) { CC.toast(`Could not save the position: ${e.message}`, "bad"); if (moved) this.draw(); }
    },
  };
  CC.mapView = M;

  // fleet map
  CC.route("/map", {
    enter() {
      CC.setCrumbs([{ label: "Overview", href: "#/" }, { label: "Map" }]);
      view().innerHTML = `<div class="ph"><div><div class="eyebrow">Fleet</div><h1>Device map</h1><p>Where each site's equipment is. Only devices that have been given a GPS position appear. Satellite imagery © Esri.</p></div></div><div id="mp-host"></div>`;
      M.mount($("#mp-host"));
    },
    update() { M.softDraw(); },
    leave() { M.destroy(); },
  });
})();
