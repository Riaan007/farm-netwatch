/* Control Center check against a LIVE hub: every page renders without script
 * errors or failed requests, and the figures on screen agree with the hub API.
 *
 *   HUB_URL=http://localhost:8091 HUB_PASSWORD=… CHROMIUM_PATH=/usr/bin/chromium \
 *     node tests/control-center.cjs
 *
 * PLAYWRIGHT_MODULE may point at a playwright package outside normal resolution.
 * Read-only: it never clicks anything that changes a site. */
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const HUB = process.env.HUB_URL || "http://localhost:8091";
const QUIET = 7 * 86400;

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH, args: ["--no-sandbox"] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const problems = [];
  const fail = (msg) => { problems.push(msg); console.log("FAIL", msg); };
  const ok = (msg) => console.log("ok  ", msg);
  page.on("pageerror", (e) => fail("script error: " + e.message));
  page.on("response", (r) => { if (r.status() >= 400 && !r.url().includes("/api/login")) fail(`HTTP ${r.status()} ${r.url()}`); });

  await page.goto(HUB + "/login");
  const login = await page.evaluate((pw) => fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password: pw }) }).then((r) => r.status), process.env.HUB_PASSWORD);
  if (login !== 200) { console.log("login failed", login); process.exit(2); }

  const api = (p) => page.evaluate((p) => fetch(p).then((r) => r.json()), p);
  const ov = await api("/api/hub/overview");
  const devs = {};
  for (const s of ov.sites) devs[s.id] = (await api(`/api/hub/sites/${s.id}/devices`)).devices || [];

  await page.goto(HUB + "/#/");
  await page.waitForFunction(() => window.CC && CC.state.loaded && CC.state.sites.every((s) => !s.enabled || CC.state.devices[s.id]), null, { timeout: 60000 });
  await page.waitForTimeout(1500);

  // sidebar lists every site
  const side = await page.$$eval("#nav-sites a", (a) => a.length);
  side === ov.sites.length ? ok(`sidebar lists ${side} sites`) : fail(`sidebar lists ${side} of ${ov.sites.length} sites`);

  // KPI: sites online
  const enabled = ov.sites.filter((s) => s.enabled);
  const reach = enabled.filter((s) => s.reachable).length;
  const kpi = async (label) => page.$$eval(".kpi", (ks, label) => { const k = ks.find((x) => x.querySelector(".k").textContent.trim().toLowerCase() === label); return k ? k.querySelector(".v").textContent.replace(/\s+/g, "") : null; }, label);
  const sitesKpi = await kpi("sites online");
  sitesKpi === `${reach}/${enabled.length}` ? ok(`sites online ${sitesKpi}`) : fail(`sites online shows ${sitesKpi}, API says ${reach}/${enabled.length}`);

  // KPI: devices online
  let total = 0, online = 0, watchedDown = 0;
  enabled.forEach((s) => { total += devs[s.id].length; online += devs[s.id].filter((d) => d.online).length; watchedDown += devs[s.id].filter((d) => d.watch && !d.online).length; });
  const devKpi = await kpi("devices online");
  devKpi === `${online}/${total}` ? ok(`devices online ${devKpi}`) : fail(`devices online shows ${devKpi}, API says ${online}/${total} (devices may have refreshed between reads)`);
  const wd = await kpi("watched down");
  wd === String(watchedDown) ? ok(`watched down ${wd}`) : fail(`watched down shows ${wd}, API says ${watchedDown}`);

  // attention count in nav == attention list length
  const navCount = await page.$eval("#nav-att-count", (e) => (e.hidden ? 0 : +e.textContent));
  await page.evaluate(() => (location.hash = "#/attention"));
  await page.waitForTimeout(800);
  const listCount = await page.$$eval("#at-list li", (l) => l.length);
  navCount === listCount ? ok(`attention: ${listCount} items, nav agrees`) : fail(`attention nav says ${navCount}, list has ${listCount}`);
  for (const s of enabled) {
    if (!s.reachable) {
      const has = await page.$$eval("#at-list li", (l, n) => l.some((x) => x.textContent.includes("Site offline") && x.textContent.includes(n)), s.name);
      has ? ok(`${s.name} offline is listed`) : fail(`${s.name} is unreachable but not in Needs attention`);
    }
  }

  // every page renders
  const routes = ["#/devices", "#/backups", "#/vpn", "#/settings/sites", "#/settings/alerts"];
  for (const s of ov.sites) for (const t of ["", "/devices", "/problems", "/health", "/history", "/backups", "/access"]) routes.push(`#/site/${s.id}${t}`);
  for (const r of routes) {
    await page.evaluate((h) => (location.hash = h), r);
    await page.waitForTimeout(1800);
    const txt = await page.$eval("#view", (v) => v.textContent.trim().length);
    txt > 40 ? ok(`renders ${r}`) : fail(`blank page ${r}`);
  }

  // site header online count and device table totals
  for (const s of enabled) {
    await page.evaluate((h) => (location.hash = h), `#/site/${s.id}/devices`);
    await page.waitForTimeout(1500);
    const head = await page.$eval("#st-head", (e) => e.textContent);
    const want = `${devs[s.id].filter((d) => d.online).length}/${devs[s.id].length} online`;
    head.includes(want) ? ok(`${s.name} header ${want}`) : fail(`${s.name} header lacks "${want}"`);
    const now = Math.floor(Date.now() / 1000);
    const visible = devs[s.id].filter((d) => d.online || (d.last_seen && now - d.last_seen < QUIET)).length;
    const n = await page.$eval("#dv-n", (e) => e.textContent);
    n.startsWith(`${visible} of ${devs[s.id].length}`) ? ok(`${s.name} table ${n}`) : fail(`${s.name} table says "${n}", expected ${visible} of ${devs[s.id].length}`);
  }

  // mobile layout: no horizontal overflow
  await page.setViewportSize({ width: 390, height: 844 });
  for (const r of ["#/", `#/site/${ov.sites[0].id}/devices`]) {
    await page.evaluate((h) => (location.hash = h), r);
    await page.waitForTimeout(1200);
    const over = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    over <= 1 ? ok(`mobile ${r} fits`) : fail(`mobile ${r} scrolls sideways by ${over}px`);
  }

  await page.evaluate(() => fetch("/api/logout", { method: "POST" }));
  await browser.close();
  console.log(problems.length ? `\n${problems.length} problem(s)` : "\nall checks passed");
  process.exit(problems.length ? 1 : 0);
})();
