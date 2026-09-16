/* Hostile data on the three pages that show what client sites report:
 * app/static/index.html (the site page), hub/app/static/index.html (/classic)
 * and hub/app/static/site.html (/site/<id>). The pages are served straight from
 * the repo and every API answer is mocked with text that breaks out of HTML
 * and out of a JS string. Checks that none of it runs, that it still shows as
 * text, and that each button carrying a value (data-* attributes read by one
 * delegated listener) sends exactly that value. No server needed.
 *
 *   CHROMIUM_PATH=/usr/bin/chromium PLAYWRIGHT_MODULE=/path/to/node_modules/playwright-core \
 *     node tests/ui-escaping.cjs
 */
const fs = require("fs");
const path = require("path");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");

const ROOT = path.resolve(__dirname, "..");
const X = `x"'><img src=/pwn onerror=__pwn(1)>');__pwn(2);//`;   // HTML + JS-string breakout
const NOW = Math.floor(Date.now() / 1000);
const KEY = `aa:bb:cc:dd:ee:01'"`;                                 // a key that needs escaping too
const KEY2 = "aa:bb:cc:dd:ee:02";
let failures = 0;
const ok = (msg) => console.log("ok  ", msg);
const fail = (msg) => { failures++; console.log("FAIL", msg); };
const check = (cond, msg) => (cond ? ok(msg) : fail(msg));

function json(body, status = 200) { return { status, body }; }

async function mount(page, origin, staticRoot, pages, api, calls) {
  await page.route("**/*", async (route) => {
    const req = route.request();
    const u = new URL(req.url());
    if (u.origin !== origin) return route.abort();                // map tiles, CDNs: not needed
    const p = u.pathname;
    if (pages[p]) return route.fulfill({ path: pages[p], contentType: "text/html" });
    if (p === "/app.css") return route.fulfill({ body: "", contentType: "text/css" });
    if (p.startsWith("/static/")) {
      const f = path.join(staticRoot, p.slice("/static/".length));
      return fs.existsSync(f) ? route.fulfill({ path: f }) : route.fulfill({ status: 404, body: "" });
    }
    if (p.startsWith("/api/")) {
      let body = null;
      try { body = req.postDataJSON(); } catch (e) { body = req.postData(); }
      const call = { method: req.method(), path: decodeURIComponent(p), query: u.searchParams, body };
      calls.push(call);
      const res = api(call) || json({});
      return route.fulfill({ status: res.status, contentType: "application/json", body: JSON.stringify(res.body) });
    }
    return route.fulfill({ status: 404, body: "" });              // /pwn and friends
  });
}

async function newPage(browser, name) {
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  await page.addInitScript(() => { window.__pwned = []; window.__pwn = (n) => window.__pwned.push(n); });
  page.on("pageerror", (e) => fail(`${name}: script error: ${e.message}`));
  page.on("dialog", (d) => d.accept());                            // confirm() / alert()
  return page;
}

const last = (calls, method, p) => [...calls].reverse().find((c) => c.method === method && c.path === p);
const waitCall = async (page, calls, method, p) => {
  for (let i = 0; i < 40; i++) { const c = last(calls, method, p); if (c) return c; await page.waitForTimeout(50); }
  return null;
};
const pwned = (page) => page.evaluate(() => window.__pwned);

// ---------------------------------------------------------------- site page
function siteApi(state) {
  const dev = (key, ip, extra) => ({
    key, ip, mac: KEY2, name: X, category: X, type: X, vendor: X, hostname: X, model: X, serial: X,
    device_name: X, online: true, status: "online", last_seen: NOW, rtt: 3, features: ["W"], ports: [80],
    watch: true, target: "10.0.0.0/24", link: "", ...extra,
  });
  const devices = [
    dev(KEY, "10.0.0.2", { geo: { lat: -33.9, lon: 18.4, note: X, ts: NOW }, ip_conflict: true,
      ip_conflict_with: [{ key: KEY2, name: X, vendor: X, category: X, mac: X, online: true }] }),
    dev(KEY2, "10.0.0.3", { name: X + " two", watch: true }),
  ];
  const link = {
    id: X, name: X, grade: "warn", quality: 40,
    ap: { name: X, ip: "10.0.0.6", mac: "58:d6:1f:00:00:01", model: X },
    sta: { name: X, ip: "10.0.0.7", model: X, monitored: false },
    metrics: { freq_label: X, width: 20, signal_ap: { now: -70 }, signal_sta: { now: -71 },
               score_dl: { now: 40, avg: 40 }, score_ul: { now: 42, avg: 42 }, drops: { count: 0 }, last_ts: NOW },
    series: { ts: [NOW - 900, NOW], sig_ap: [-70, -70], sig_sta: [-71, -71], score_dl: [40, 40], score_ul: [42, 42] },
    findings: [{ id: "weak_signal", level: "warn", title: X, evidence: [X], causes: [X], steps: [{ text: X, where: "remote" }], good: X }],
  };
  const wifi = {
    ok: true, enabled: true, poll_min: 15, hours: 168, links: [link],
    radios: [{ key: X, name: X, ip: "10.0.0.6", mac: "58:d6:1f:00:00:01", model: X, ok: true, is_ap: true,
               stations: 1, current: {}, stats: {}, last_ts: NOW, findings: [] }],
    fixes: [{ link: X, level: "warn", title: X, target: X, where: "remote", first: X },
            { radio: X, level: "warn", title: X, target: X, where: "site", first: X }],
    shared: [], summary: { links: 1, radios: 1, radios_read: 1, warn: 1, crit: 0, good: 0 },
  };
  return (c) => {
    const p = c.path;
    if (p === "/api/status") return json({ site: { name: X, location: X }, is_scanning: false, last_scan_ts: NOW,
      scan_interval_min: 5, features: {}, configured: true,
      auth: { password_set: true, hub_key_set: true, logged_in: false, hub: false } });
    if (p === "/api/auth/state") return json({ password_set: true, hub_key_set: true, logged_in: false, hub: false });
    if (p === "/api/devices") return json({ targets: [{ cidr: "10.0.0.0/24", label: X }], devices, last_scan: "now", offline_after: 2 });
    if (p === "/api/problems") return json({ problems: [
      { type: "ip_change", severity: "high", ip: X, detail: X, fix: X, ack_key: KEY,
        devices: [{ key: KEY, ip: "10.0.0.2", name: X, category: X, mac: X, online: true }] },
      { type: "same_mac_multi_ip", severity: "medium", ip: "10.0.0.2", mac: X, detail: X, devices: [] },
      { type: "ip_conflict", severity: "high", ip: X, detail: X, devices: [] },
    ] });
    if (p === "/api/monitoring") return json({ ok: true, monitored: 2, online: 2, offline: 0, kuma_follows: false });
    if (p === "/api/radio/links") return json(wifi);
    if (p === "/api/events/summary") return json({ total: 1, counts: { new: 1 }, busiest: [{ ip: X, events: 3, devices: 2, types: ["new", X] }] });
    if (p === "/api/events") return json({ events: [{ ts: NOW, type: X, key: KEY, ip: X, name: X, mac: X, hostname: X, detail: { type_label: X, model: X } }] });
    if (p === "/api/ip-history") return json({ ips: [{ ip: X, name: X, mac: X, last_type: X, last_ts: NOW, device_count: 2, event_count: 3 }] });
    if (p === "/api/config" && c.query.get("full")) return json({ configured: true, site: { name: X, location: X },
      targets: [{ cidr: "10.0.0.0/24", label: X }, { cidr: X, label: X }], scan: { interval_min: 5 },
      alerts: { ntfy_topic: "" }, features: {}, integrations: { kuma: {} }, sysmon: {} });
    if (p === "/api/network") return json({ available: true, gateway: "10.0.0.1",
      interfaces: [{ iface: X, addrs: [X] }], addresses: [{ iface: X, cidr: X, target: true }],
      connections: [{ name: X, device: X, method: "manual", addresses: X, gateway: X, dns: X,
                      runtime: { address: "10.0.0.9/24", gateway: X, dns: X } }],
      pending: state.pending ? { connection: X, revert_deadline_ts: NOW + 100 } : null });
    if (p === "/api/bridge-macs" && c.method === "GET") return json({ bridge_macs: ["aabbccddee01", X] });
    if (p === `/api/devices/${KEY}` && c.method === "POST")
      return state.locked ? json({ ok: false, error: "auth_required", password_set: true }, 401) : json({ ok: true, registry: {} });
    if (p === `/api/devices/${KEY}/credentials`) return json({ ok: false, error: "auth_required", password_set: true }, 401);
    return null;
  };
}

async function sitePage(browser) {
  const page = await newPage(browser, "site");
  const calls = [], state = { locked: true, pending: false };
  await mount(page, "http://site.test", path.join(ROOT, "app/static"), { "/": path.join(ROOT, "app/static/index.html") }, siteApi(state), calls);
  await page.goto("http://site.test/");
  await page.waitForSelector("#network-grid .nw-card");
  check((await page.textContent("#network-grid")).includes(X), "site: a device name shows as text on its card");

  // Problems panel
  await page.waitForSelector('#conflict-monitor [data-nw-act="open-device"]', { state: "attached" });
  await page.evaluate(() => { const d = document.querySelector("#conflict-monitor details"); if (d) d.open = true; });
  check((await page.textContent("#conflict-monitor")).includes(X), "site: problem text shows as text");
  await page.click('#conflict-monitor [data-nw-act="open-device"]');
  await page.waitForSelector("#modal:not(.hidden)");
  check((await page.evaluate(() => CURRENT_KEY)) === KEY, "site: a problem's device chip opens that device");
  check((await page.textContent("#modal-conflict")).includes(X), "site: the conflict note shows the other device's name as text");
  check((await page.$$eval("#edit-link option", (os) => os.map((o) => o.textContent).join("|"))).includes(X + " two"), "site: the link list shows names as text");
  check((await page.inputValue("#edit-category")) === "unknown", "site: an unknown category shows as Unknown in the editor");
  // Save needs the login: cancel the prompt, the window stays open with a message
  await page.fill("#edit-name", "Front gate");
  await page.click('button[onclick="saveDevice()"]');
  await page.waitForSelector("#nw-auth[open]");
  const post = await waitCall(page, calls, "POST", `/api/devices/${KEY}`);
  check(post && post.body.name === "Front gate" && post.body.category === "unknown", "site: Save sends the fields for the right key");
  await page.click("#nw-auth [data-x]");
  await page.waitForSelector(".nw-toast.bad");
  check((await page.textContent("#nw-toasts")).includes("log in with the Pi password"), "site: a refused save says why");
  check(await page.isVisible("#modal"), "site: the device window stays open after a refused save");
  state.locked = false;
  await page.click('button[onclick="saveDevice()"]');
  await page.waitForSelector("#modal.hidden", { state: "attached" });
  ok("site: an accepted save closes the device window");

  await page.click('#conflict-monitor [data-nw-act="ack-ip"]');
  check(await waitCall(page, calls, "POST", `/api/devices/${KEY}/ack-ip`), "site: Acknowledge posts for the right key");
  await page.click('#conflict-monitor [data-nw-act="bridge-mark"]');
  const br = await waitCall(page, calls, "POST", "/api/bridge-macs");
  check(br && br.body.mac === X, "site: 'It's a bridge' sends the exact MAC");
  await page.click('#conflict-monitor [data-nw-act="conflict-clear"]');
  const cl = await waitCall(page, calls, "POST", "/api/conflicts/clear");
  check(cl && cl.body.ip === X, "site: Clear & re-test sends the exact address");

  // Map: placed list, pin popup, unplaced list
  await page.evaluate(() => { location.hash = "#/map"; });
  await page.waitForSelector('#map-list [data-nw-act="map-focus"]');
  await page.click('#map-list [data-nw-act="map-focus"]');
  check((await page.evaluate(() => MAP_FOCUS)) === KEY, "site: the map list focuses the right device");
  await page.click(".nw-pin");
  await page.waitForSelector('.leaflet-popup [data-nw-act="map-move"]');
  check((await page.textContent(".leaflet-popup")).includes(X), "site: the map popup shows text");
  await page.click('.leaflet-popup [data-nw-act="map-move"]');
  check((await page.evaluate(() => MAP_PLACING)) === KEY, "site: Move in the popup starts placing that device");
  await page.evaluate(() => mapCancelPlace());
  await page.click(".nw-pin");
  await page.waitForSelector('.leaflet-popup [data-nw-act="open-device"]');
  await page.click('.leaflet-popup [data-nw-act="open-device"]');
  await page.waitForSelector("#modal:not(.hidden)");
  check((await page.evaluate(() => CURRENT_KEY)) === KEY, "site: Open device in the popup opens that device");
  await page.evaluate(() => closeModal());
  await page.evaluate(() => mapTab("off"));
  await page.waitForSelector('#map-list [data-nw-act="map-place"]');
  await page.click('#map-list [data-nw-act="map-place"]');
  check((await page.evaluate(() => MAP_PLACING)) === KEY2, "site: Place in the unplaced list picks that device");
  await page.evaluate(() => { mapCancelPlace(); mapTab("on"); });

  // History
  await page.evaluate(() => { location.hash = "#/history"; });
  await page.waitForSelector('#hist-busy [data-nw-act="hist-ip"]');
  await page.click('#hist-busy [data-nw-act="hist-ip"]');
  check((await page.inputValue("#hist-filter")) === X, "site: a busy address filters on that address");
  await page.evaluate(() => { document.getElementById("hist-filter").value = ""; loadEvents(); });
  await page.waitForSelector('#hist-body button.dev[data-nw-act="open-device"]');
  check((await page.textContent("#hist-body")).includes(X), "site: event text shows as text");
  await page.click('#hist-body button.dev');
  await page.waitForSelector("#modal:not(.hidden)");
  check((await page.evaluate(() => CURRENT_KEY)) === KEY, "site: an event opens its device");
  await page.evaluate(() => closeModal());
  calls.length = 0;
  await page.click('#hist-body button.ipb');
  check((await page.inputValue("#hist-filter")) === X, "site: an event's address filters on it");
  await waitCall(page, calls, "GET", "/api/events");
  await page.waitForTimeout(300);                  // loadEvents() switches back to the timeline when it lands
  await page.evaluate(() => { document.getElementById("hist-filter").value = ""; histTab("byip"); });
  await page.waitForSelector('#hist-body tr[data-nw-act="hist-ip"]');
  await page.click('#hist-body tr[data-nw-act="hist-ip"]');
  check((await page.inputValue("#hist-filter")) === X, "site: a by-address row filters on it");

  // Wi-Fi
  await page.evaluate(() => { location.hash = "#/wifi"; });
  await page.waitForSelector('#wifi-links [data-nw-act="wifi-toggle"]');
  check((await page.textContent("#page-wifi")).includes(X), "site: Wi-Fi names show as text");
  await page.click('#wifi-links [data-nw-act="wifi-toggle"]');
  await page.waitForSelector('#wifi-links [data-nw-act="wifi-csv"]');
  check(await page.evaluate((id) => WF.open.has(id), X), "site: a link row opens on click");
  const [dl] = await Promise.all([page.waitForEvent("download"), page.click('#wifi-links [data-nw-act="wifi-csv"]')]);
  check(!!dl, "site: the CSV button downloads");
  await page.focus('#wifi-links [data-nw-act="wifi-toggle"]');
  await page.keyboard.press("Enter");
  await page.waitForFunction((id) => !WF.open.has(id), X);
  ok("site: Enter on a link row closes it");
  await page.click('#wifi-kpis [data-nw-act="wifi-link"]');
  check(await page.evaluate((id) => WF.open.has(id), X), "site: the weakest-link tile opens that link");
  await page.click('#wifi-fix [data-nw-act="wifi-radio"]');
  await page.click('#wifi-radios [data-nw-act="wifi-radio"]');
  ok("site: radio buttons run without errors");

  // Settings: network + bridges
  await page.evaluate(() => { location.hash = "#/settings/network"; });
  await page.waitForSelector('#net-addrs [data-nw-act="net-addr-remove"]', { state: "attached" });
  await page.evaluate(() => document.getElementById("net-addrs").scrollIntoView());
  check((await page.textContent("#net-cons")).includes(X), "site: connection names show as text");
  check((await page.inputValue("#net-cons .cn-gw")) === X, "site: a gateway value is kept exactly in its input");
  await page.click('#net-addrs [data-nw-act="net-addr-remove"]');
  const rm = await waitCall(page, calls, "POST", "/api/network/address");
  check(rm && rm.body.iface === X && rm.body.cidr === X && rm.body.action === "remove", "site: Remove address sends the exact interface and address");
  await page.click('#net-cons [data-nw-act="net-dhcp"]');
  const dh = await waitCall(page, calls, "POST", "/api/network/dhcp");
  check(dh && dh.body.connection === X, "site: DHCP sends the exact connection name");
  await page.evaluate(() => document.querySelector('[id^="cn-edit-"]').classList.remove("hidden"));
  await page.fill("#net-cons .cn-ip", "10.0.0.9");
  await page.fill("#net-cons .cn-mask", "255.255.255.0");
  await page.click('#net-cons [data-nw-act="net-static"]');
  const st = await waitCall(page, calls, "POST", "/api/network/static");
  check(st && st.body.connection === X && st.body.ip_prefix === "10.0.0.9/24", "site: Apply fixed IP sends the exact connection name");
  state.pending = true;
  await page.evaluate(() => loadNetwork());
  await page.waitForSelector('#net-cons [data-nw-act="net-confirm"]');
  await page.click('#net-cons [data-nw-act="net-confirm"]');
  check(await waitCall(page, calls, "POST", "/api/network/static/confirm"), "site: Keep changes confirms");
  await page.waitForSelector('#bridge-list [data-nw-act="bridge-remove"]', { state: "attached" });
  check((await page.textContent("#bridge-list")).includes("aa:bb:cc:dd:ee:01"), "site: bridge MACs are listed");
  calls.length = 0;
  await page.$$eval('#bridge-list [data-nw-act="bridge-remove"]', (bs) => bs[1].click());
  const brm = await waitCall(page, calls, "POST", "/api/bridge-macs");
  check(brm && brm.body.mac === X && brm.body.enable === false, "site: Remove bridge sends the exact MAC");
  check((await page.$$eval("#target-select option", (os) => os.map((o) => o.textContent).join("|"))).includes(X), "site: subnet labels show as text");

  check((await pwned(page)).length === 0, `site: nothing from the data ran (${JSON.stringify(await pwned(page))})`);
  await page.close();
}

// ---------------------------------------------------------------- hub pages
function hubApi() {
  const site = { id: "farm-1", name: X, location: X, reachable: true, devices_total: 2, devices_online: 1,
    watched_down: 1, conflicts: 1, rotated: 1, latency_ms: 12, last_scan_ts: NOW, spark: [1, null, 0.5],
    reach_24h: 99.5, kuma: { up: 1, down: 1 }, kuma_state: "ok", pi_health: "warn", api_auth: "ok", stale: false,
    links: { netwatch: "javascript:__pwn(3)", kuma: "http://10.0.0.1:3001" } };
  const cfgSite = { id: X, name: X, vpn_ip: X, netwatch_port: 8090, kuma_url: X, kuma_status_slug: X, enabled: true, wg_client_id: "c1" };
  const device = { key: KEY, ip: X, mac: X, name: X, category: X, type: X, vendor: X, hostname: "host-" + X,
    online: true, last_seen: NOW, rtt: 3, ports: [X, 22], services: { 22: X }, watch: true };
  return (c) => {
    const p = c.path;
    if (p === "/api/hub/overview") return json({ sites: [site] });
    if (p === "/api/hub/sites" && c.method === "GET") return json({ sites: [cfgSite] });
    if (p === "/api/hub/remote" && c.method === "GET") return json({ clients: [{ id: X, name: X, address: X }] });
    if (p === "/api/hub/sites/farm-1/devices") return json({ card: { ...site, links: { netwatch: "http://10.0.0.1:8090", kuma: "" } },
      devices: [device], stale: false, fetched_at: NOW });
    if (p === "/api/hub/sites/farm-1/reachability") return json({ series: [1, 1], summary: { reach_24h: 99 } });
    if (p === "/api/hub/sites/farm-1/conflicts") return json({ conflicts: [{ ip: X, kind: "ip_conflict", devices: [{ name: X, category: X, mac: X, online: true }] }] });
    if (p === "/api/hub/sites/farm-1/sysinfo") return json({ fetched_at: NOW, sysinfo: { ts: NOW, model: X, temp_c: X, cpu_pct: 5,
      load: [0.1], mem: { used_pct: X }, disks: [{ used_pct: 10, free_gb: X }], clock: { offset_s: 700, server: X },
      smart: { available: true, devices: [{ dev: X, model: X, healthy: true, notes: X }] },
      containers: { available: true, containers: [{ name: X, state: "up" }], flapping: [X] },
      storage: { filesystems: [], ext4_errors: [], cards: [{ dev: X, name: X, date: X, age_years: 1 }] },
      watchdog: { enabled: true, note: X } } });
    if (p === "/api/hub/sites/farm-1/backups" && c.method === "GET") return json({ backups: [{ name: X, ts: NOW, bytes: 2048, encrypted: true }] });
    if (p === `/api/hub/sites/farm-1/history/${KEY}`) return json({ summary: { uptime_24h: X } });
    if (p === "/api/hub/sites/farm-1/command")
      return json(c.body.action === "quality"
        ? { ok: true, quality: { ok: true, rating: X, loss: X, avg: 1, jitter: 1, max: 1, recv: X, sent: 5, note: X } }
        : { ok: true, result: X });
    if (p === "/api/hub/sites/farm-1/tunnel") return json({ ok: true, host: X, port: 8300, ip: X, device_port: 22, scheme: X });
    if (p === "/api/hub/sites/farm-1/tunnels") return json({ tunnels: [{ id: X, hub_port: 8300, scheme: X, ip: X, port: 22, conns: X }] });
    if (p === "/api/hub/sites/farm-1/events") return json({ events: [{ ts: NOW, type: X, ip: X, name: X, mac: X, hostname: X, detail: { type_label: X } }] });
    if (p === "/api/hub/sites/farm-1/ip-history") return json({ ips: [{ ip: X, name: X, last_type: X, device_count: X, event_count: 1, last_ts: NOW }] });
    return null;
  };
}

async function hubPages(browser) {
  const staticRoot = path.join(ROOT, "hub/app/static");
  const pages = { "/classic": path.join(staticRoot, "index.html"), "/site/farm-1": path.join(staticRoot, "site.html") };

  // /classic
  let page = await newPage(browser, "hub classic");
  let calls = [];
  await mount(page, "http://hub.test", staticRoot, pages, hubApi(), calls);
  await page.goto("http://hub.test/classic");
  await page.waitForSelector("#site-grid h3");
  check((await page.textContent("#site-grid")).includes(X), "hub classic: site name and location show as text");
  check((await page.getAttribute('#site-grid a[target="_blank"]', "href")) === "#", "hub classic: a javascript: link is not used");
  check((await page.getAttribute('#site-grid a.btn-primary', "href")) === "/site/farm-1", "hub classic: Details links to the site");
  await page.evaluate(() => openSites());
  await page.waitForSelector('#site-list [data-hub-act="edit-site"]');
  check((await page.textContent("#site-list")).includes(X), "hub classic: the site list shows text");
  await page.click('#site-list [data-hub-act="edit-site"]');
  check((await page.inputValue("#f-name")) === X && (await page.inputValue("#f-ip")) === X, "hub classic: Edit fills the form with that site");
  await page.click('#site-list [data-hub-act="enroll"]');
  check(await waitCall(page, calls, "GET", `/api/hub/sites/${X}/enroll`), "hub classic: Setup cmd asks for that site");
  await page.click('#site-list [data-hub-act="delete-site"]');
  check(await waitCall(page, calls, "DELETE", `/api/hub/sites/${X}`), "hub classic: Delete removes that site");
  await page.evaluate(() => openRemote());
  await page.waitForSelector('#rm-list [data-hub-act="rm-del"]');
  check((await page.textContent("#rm-list")).includes(X), "hub classic: remote devices show as text");
  await page.click('#rm-list [data-hub-act="rm-del"]');
  check(await waitCall(page, calls, "DELETE", `/api/hub/remote/${X}`), "hub classic: Remove deletes that remote device");
  await page.waitForTimeout(300);
  check((await pwned(page)).length === 0, `hub classic: nothing from the data ran (${JSON.stringify(await pwned(page))})`);
  await page.close();

  // /site/<id>
  page = await newPage(browser, "hub site");
  calls = [];
  await mount(page, "http://hub.test", staticRoot, pages, hubApi(), calls);
  await page.goto("http://hub.test/site/farm-1");
  await page.waitForSelector('#device-rows tr[data-hub-act="history"]');
  check((await page.textContent("#device-rows")).includes(X), "hub site: device text shows as text");
  await page.waitForSelector('#conflict-monitor [data-hub-act="conflict-clear"]');
  await page.waitForSelector("#pi-health:not(.hidden)");
  check((await page.textContent("#pi-health")).includes(X), "hub site: Pi health text shows as text");

  await page.click('#device-rows tr[data-hub-act="history"] td');
  await page.waitForSelector('#device-rows [data-hub-act="diag"][data-kind="ping"]');
  check(await waitCall(page, calls, "GET", `/api/hub/sites/farm-1/history/${KEY}`), "hub site: a row loads that device's history");
  check((await page.textContent("#device-rows")).includes(X + "%"), "hub site: an uptime value shows as text");
  await page.click('#device-rows [data-hub-act="beats"][data-r="12h"]');
  const beats = await waitCall(page, calls, "GET", `/api/hub/sites/farm-1/history/${KEY}/beats`);
  check(beats && beats.query.get("range") === "12h", "hub site: a range button loads that range");
  await page.click('#device-rows [data-hub-act="diag"][data-kind="ping"]');
  const ping = await waitCall(page, calls, "POST", "/api/hub/sites/farm-1/command");
  check(ping && ping.body.ip === X && ping.body.action === "ping", "hub site: Ping sends the exact address");
  await page.waitForSelector("#device-rows pre");
  check((await page.textContent("#device-rows pre")) === X, "hub site: command output shows as text");
  calls.length = 0;
  await page.click('#device-rows [data-hub-act="diag"][data-kind="quality"]');
  await page.waitForFunction(() => document.querySelector("#device-rows").textContent.includes("Connection quality"));
  check((await page.textContent("#device-rows")).includes(X), "hub site: quality results show as text");
  await page.click('#device-rows [data-hub-act="diag"][data-kind="deep"]');
  const deep = await waitCall(page, calls, "POST", "/api/hub/sites/farm-1/trigger");
  check(deep && deep.body.hosts[0] === X, "hub site: Deep scan sends the exact address");

  const rows = await page.$$eval("#device-rows tr", (t) => t.length);
  await page.click('#device-rows [data-hub-act="connect"]');
  await page.waitForSelector("#connect-modal:not(.hidden)");
  check((await page.$$eval("#device-rows tr", (t) => t.length)) === rows, "hub site: Connect does not also toggle the row");
  check((await page.textContent("#cn-ports")).includes(X), "hub site: port chips show text");
  await page.click('#cn-ports [data-hub-act="tunnel"]');                 // the hostile "port" is not a number
  check((await page.textContent("#cn-msg")).includes("Pick or enter a port"), "hub site: a non-number port is refused");
  await page.click('#cn-ports [data-hub-act="tunnel"][data-port="22"]');
  const tun = await waitCall(page, calls, "POST", "/api/hub/sites/farm-1/tunnel");
  check(tun && tun.body.ip === X && tun.body.port === 22, "hub site: a port chip opens a tunnel to the exact address");
  await page.waitForSelector('#cn-result [data-hub-act="copy"]');
  check((await page.textContent("#cn-result")).includes(X), "hub site: the tunnel result shows text");
  await page.click('#cn-result [data-hub-act="copy"]');
  await page.waitForSelector('#cn-active [data-hub-act="close-tunnel"]');
  await page.click('#cn-active [data-hub-act="close-tunnel"]');
  check(await waitCall(page, calls, "DELETE", `/api/hub/sites/farm-1/tunnel/${X}`), "hub site: Close closes that tunnel");
  await page.evaluate(() => closeConnect());

  await page.click('#conflict-monitor [data-hub-act="conflict-clear"]');
  const hc = await waitCall(page, calls, "POST", "/api/hub/sites/farm-1/conflicts/clear");
  check(hc && hc.body.ip === X, "hub site: Clear & re-test sends the exact address");
  await page.waitForSelector('#backup-list [data-hub-act="restore"]');
  await page.click('#backup-list [data-hub-act="restore"]');
  const rs = await waitCall(page, calls, "POST", "/api/hub/sites/farm-1/restore");
  check(rs && rs.body.name === X, "hub site: Restore sends the exact backup name");
  await page.click('#backup-list [data-hub-act="del-backup"]');
  check(await waitCall(page, calls, "DELETE", `/api/hub/sites/farm-1/backups/${X}`), "hub site: Delete removes that backup");
  check((await page.getAttribute("#backup-list a", "href")) === "/api/hub/sites/farm-1/backups/" + encodeURIComponent(X), "hub site: Download links to that backup");

  await page.evaluate(() => openHistory());
  await page.waitForSelector('#hist-body [data-hub-act="events-ip"]');
  check((await page.textContent("#hist-body")).includes(X), "hub site: history shows text");
  calls.length = 0;
  await page.click('#hist-body [data-hub-act="events-ip"]');
  const ev = await waitCall(page, calls, "GET", "/api/hub/sites/farm-1/events");
  check(ev && ev.query.get("ip") === X, "hub site: an event's address loads its events");
  await page.waitForTimeout(300);                  // loadEvents() switches back to the timeline when it lands
  await page.evaluate(() => histTab("byip"));
  await page.waitForSelector('#hist-body [data-hub-act="hist-ip"]');
  await page.click('#hist-body [data-hub-act="hist-ip"]');
  check((await page.inputValue("#hist-filter")) === X, "hub site: a by-address row filters on it");
  await page.waitForTimeout(300);
  check((await pwned(page)).length === 0, `hub site: nothing from the data ran (${JSON.stringify(await pwned(page))})`);
  await page.close();
}

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH, args: ["--no-sandbox"] });
  try {
    await sitePage(browser);
    await hubPages(browser);
  } catch (e) {
    fail("stopped: " + (e && e.stack || e));
  } finally {
    await browser.close();
  }
  console.log(failures ? `${failures} check(s) failed` : "all checks passed");
  process.exit(failures ? 1 : 0);
})();
