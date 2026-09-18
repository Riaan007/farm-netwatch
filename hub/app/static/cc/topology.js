/* Netwatch Control Center — a site's Network tab: the SAME diagram + map the
 * site's own Network page draws (cc/topoview.js is copied from the site app at
 * build time), reading and changing the site's records through the hub proxy
 * (hub/app/topology.py). The hub key unlocks editing, so there is no login. */
(function () {
  "use strict";
  const { api } = CC;
  const enc = encodeURIComponent;
  let ctl = null;

  CC.topoTab = {
    enter(s, el) {
      this.leave();
      if (!window.TopoView) {
        el.innerHTML = `<div class="empty"><b>The network view did not load</b>Reload the page.</div>`;
        return;
      }
      el.innerHTML = `<div style="--tv-height: calc(100vh - 290px)"></div>`;
      const base = `/api/hub/sites/${enc(s.id)}/topology`;
      const p = CC.params();
      ctl = TopoView.mount(el.firstElementChild, {
        fitHeight: true,
        load: (o) => api(base + (o && o.scope === "all" ? "?scope=all" : ""), { timeout: 45000 }),
        call: (method, path, body) => api(base + path, { method, body, timeout: 60000 }),
        iconUrl: (id) => `${base}/icons/${enc(id)}`,
        openDevice: (key) => {
          if ((CC.state.devices[s.id] || []).some((d) => d.key === key)) CC.openDevice(s.id, key);
          else CC.toast("That device is not in the hub's device list yet — refresh the site first");
        },
        placeDevicesHref: `#/site/${enc(s.id)}/map`,
        mapTiles: (map) => {
          const t = CC.mapTiles(false);
          t.base.forEach((layer) => layer.addTo(map));
          if (t.control) t.control.addTo(map);
        },
        toast: CC.toast,
        confirm: (title, text, o) => CC.confirm(title, text, o || {}),
        storageKey: `nw.hub.topo.${s.id}`,
        refreshMs: 45000,
        view: p.get("view") || undefined,
        focus: p.get("focus") || undefined,
      });
    },
    update() { /* the view refreshes itself; the fleet loader has nothing it needs */ },
    leave() {
      if (ctl) { ctl.destroy(); ctl = null; }
    },
  };
})();
