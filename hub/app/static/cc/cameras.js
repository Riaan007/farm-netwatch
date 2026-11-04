/* Netwatch Control Center — cameras: the all-sites Cameras page (#/cameras), the site
 * tab "Cameras" and a picture at the top of a camera's device drawer. The grid and the
 * camera view are CamView (camview.js — the same file the site's own page uses).
 * Lists: /api/hub/sites/<id>/cameras (the hub caches ~30 s and keeps the last list
 * while a site is offline). Pictures: …/cameras/<key>/picture, read by the site Pi —
 * the hub never sees a camera's password. */
(function () {
  "use strict";
  const { $, esc, api, state: S } = CC;
  const enc = encodeURIComponent;
  const view = () => $("#view");
  const D = (CC.camData = {});        // site id -> { data, at, error }
  const picUrl = (g, c, o) => `/api/hub/sites/${enc(g.id)}/cameras/${enc(c.key)}/picture?size=${o && o.size === "full" ? "full" : "thumb"}${o && o.fresh ? "&fresh=1" : ""}${o && o.view > 1 ? "&view=" + o.view : ""}`;

  async function load(siteId, force) {
    const cur = D[siteId];
    if (cur && cur.data && !force && CC.now() - cur.at < 45) return cur;
    try {
      const j = await api(`/api/hub/sites/${enc(siteId)}/cameras${force ? "?fresh=1" : ""}`, { timeout: 40000 });
      D[siteId] = { data: j, at: CC.now() };
    } catch (e) {
      D[siteId] = { ...(cur || {}), at: CC.now(),
        error: e.status === 501 ? "This site's Netwatch is too old for camera pictures — update it." : e.message || "could not load the cameras" };
    }
    return D[siteId];
  }

  function groupOf(s) {
    const d = D[s.id] || {};
    const stale = d.data && d.data.stale;
    return {
      id: s.id, href: `#/site/${enc(s.id)}/cameras`,
      title: s.name + (stale ? " — offline, showing saved pictures" : ""),
      data: d.data && !d.data.__error && Array.isArray(d.data.cameras) ? d.data
        : d.error ? { __error: d.error } : { __loading: true },
    };
  }

  async function openOne(host, siteId, key, back, alive) {
    host.innerHTML = `<div class="skel" style="height:320px"></div>`;
    const d = await load(siteId);
    if (!alive()) return null;
    const s = CC.site(siteId);
    const cam = d.data && (d.data.cameras || []).find((c) => c.key === key);
    if (!cam) {
      host.innerHTML = `<div class="empty"><b>This camera is not in ${esc(s ? s.name : "the site")}'s camera list.</b><a href="#/site/${enc(siteId)}/cameras">All cameras</a></div>`;
      return null;
    }
    const dev = (S.devices[siteId] || []).find((x) => x.key === key);
    return CamView.camera(host, {
      group: { id: siteId, title: s ? s.name : siteId }, cam, picUrl, onBack: back,
      deviceAction: dev ? { label: "Device details", run: () => CC.openDevice(siteId, key) } : null,
    });
  }

  // ---- all sites --------------------------------------------------------------------------
  const fleet = {
    ctl: null, timer: null, gen: 0,
    async enter() {
      this.leave();
      const gen = ++this.gen, alive = () => gen === this.gen;
      const p = CC.params(), sid = p.get("site"), key = p.get("key");
      CC.setCrumbs([{ label: "Overview", href: "#/" }, { label: "Cameras", href: "#/cameras" }, ...(key ? [{ label: "Camera" }] : [])]);
      view().innerHTML = (key ? "" : `<div class="ph"><div><div class="eyebrow">Fleet</div><h1>Cameras</h1><p>A current picture from every camera at every site, read by each site's Pi with the camera's own login or through its recorder. Click a picture for the camera page. A camera that is off shows its last saved picture.</p></div></div>`)
        + `<section id="cm-host"></section>`;
      if (key && sid) {
        const ctl = await openOne($("#cm-host"), sid, key, () => CC.go("#/cameras"), alive);
        if (alive()) this.ctl = ctl; else if (ctl) ctl.destroy();
        return;
      }
      const sites = () => S.sites.filter((s) => s.enabled);
      this.ctl = CamView.mount($("#cm-host"), {
        groups: () => sites().map(groupOf), picUrl, fleet: true, storeKey: "cc.cams",
        onOpen: (g, c) => CC.go(`#/cameras?site=${enc(g.id)}&key=${enc(c.key)}`),
      });
      const refresh = async () => {
        await CC.pool(sites(), 3, async (s) => { await load(s.id); if (alive() && this.ctl) this.ctl.update(); });
      };
      await refresh();
      if (alive()) this.timer = setInterval(refresh, 60000);
    },
    update() {
      // the fleet loader just learned the sites: fetch the camera lists it hadn't
      if (this.ctl && S.sites.some((s) => s.enabled && !D[s.id])) {
        CC.pool(S.sites.filter((s) => s.enabled && !D[s.id]), 3, async (s) => { await load(s.id); if (this.ctl) this.ctl.update(); });
      }
    },
    leave() {
      this.gen++;
      if (this.ctl) this.ctl.destroy();
      this.ctl = null;
      clearInterval(this.timer); this.timer = null;
    },
  };
  CC.route("/cameras", fleet);

  // ---- one site's tab ---------------------------------------------------------------------
  CC.camTab = {
    ctl: null, timer: null, gen: 0,
    async enter(s, el) {
      this.leave();
      const gen = ++this.gen, alive = () => gen === this.gen;
      const p = CC.params(), key = p.get("key");
      el.innerHTML = `<div id="ct-note"></div><div id="ct-host"></div>`;
      if (key) {
        const ctl = await openOne($("#ct-host"), s.id, key, () => CC.go(`#/site/${enc(s.id)}/cameras`), alive);
        if (alive()) this.ctl = ctl; else if (ctl) ctl.destroy();
        return;
      }
      this.ctl = CamView.mount($("#ct-host"), {
        groups: () => [groupOf(s)], picUrl, storeKey: "cc.cams", nvr: p.get("nvr") || "",
        onOpen: (_g, c) => CC.go(`#/site/${enc(s.id)}/cameras?key=${enc(c.key)}`),
      });
      const refresh = async (force) => {
        await load(s.id, force);
        if (!alive() || !this.ctl) return;
        const d = D[s.id], note = $("#ct-note");
        if (note) note.innerHTML = d && d.data && d.data.stale
          ? `<p class="note" style="margin:0 0 12px">${esc(d.data.error || "The site can't be reached.")} — the pictures are the last ones saved.</p>` : "";
        this.ctl.update();
      };
      await refresh(true);
      if (alive()) this.timer = setInterval(() => refresh(false), 60000);
    },
    update() {},
    leave() {
      this.gen++;
      if (this.ctl) this.ctl.destroy();
      this.ctl = null;
      clearInterval(this.timer); this.timer = null;
    },
  };

  // ---- the device drawer: a picture at the top ------------------------------------------------
  CC.cameraDrawer = (dlg, siteId, d) => {
    if (!d || !["camera", "nvr"].includes(d.category)) return;
    const bd = $(".dbd", dlg);
    if (!bd) return;
    const sect = document.createElement("div");
    sect.className = "sect";
    if (d.category === "nvr") {
      sect.innerHTML = `<h3>Cameras</h3><a class="btn" href="#/site/${enc(siteId)}/cameras?nvr=${enc(d.key)}" data-close>📷 This recorder's cameras</a>`;
      bd.insertBefore(sect, bd.firstChild);
      return;
    }
    sect.innerHTML = `<h3>Picture</h3>
      <div class="cv"><div class="cv-pic" style="aspect-ratio:1.7778;border-radius:10px"><img alt="" hidden><div class="cv-ph"><span>Loading…</span></div><span class="cv-age" hidden></span></div></div>
      <div class="row" style="margin-top:8px"><a class="btn pri" href="#/site/${enc(siteId)}/cameras?key=${enc(d.key)}" data-close>📷 Open camera page</a></div>`;
    bd.insertBefore(sect, bd.firstChild);
    const box = $(".cv-pic", sect), img = $("img", sect), ph = $(".cv-ph span", sect), age = $(".cv-age", sect);
    load(siteId).then((x) => {
      const cam = x.data && (x.data.cameras || []).find((c) => c.key === d.key);
      if (!cam) { ph.textContent = x.error || "Not in this site's camera list yet."; return; }
      if (cam.aspect) box.style.aspectRatio = String(cam.aspect);
      if (!cam.route && !cam.picture) { ph.textContent = cam.why_text || "No picture."; return; }
      return CamView.fetchPic(siteId, picUrl({ id: siteId }, cam, { size: "thumb" })).then((p) => {
        if (!dlg.isConnected) { URL.revokeObjectURL(p.src); return; }
        img.onload = () => URL.revokeObjectURL(p.src);
        img.src = p.src; img.hidden = false; $(".cv-ph", sect).hidden = true;
        age.hidden = false; age.textContent = (p.stale ? "Saved · " : "") + CamView.ago(p.ts);
      });
    }).catch((e) => { ph.textContent = e.message || "No picture."; });
  };
})();
