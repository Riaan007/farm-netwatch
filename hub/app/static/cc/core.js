/* Netwatch Control Center — core: API, state, derived truth, router, shell.
 *
 * Every figure on screen is derived HERE from hub data (never guessed in a
 * view), so the overview, the sidebar counts, the attention list and a site's
 * own page can never disagree. The rules, in one place:
 *
 *  site state   paused   = site disabled in the hub
 *               offline  = hub can't reach it (VPN / Pi / Netwatch down)
 *               stale    = reachable, but the device list is out of date
 *               fault    = watched device down, Kuma monitor down, internet down,
 *                          Pi health critical
 *               warn     = live IP conflict, Pi health warning, Pi login/key
 *                          problem, no backup in 26 h, Wi-Fi link degraded
 *               ok       = none of the above
 *  devices      "offline" counts only devices seen in the last 7 days; older
 *               ones are "gone quiet" (old discoveries, visitors' phones) and
 *               are listed separately, never mixed into the fault picture.
 */
(function () {
  "use strict";
  const CC = (window.CC = {});
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  CC.$ = $; CC.$$ = $$;

  // ---- tiny helpers ----------------------------------------------------------
  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  CC.esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ESC[c]);
  CC.now = () => Math.floor(Date.now() / 1000);
  CC.ago = (ts) => {
    if (!ts) return "never";
    const s = CC.now() - ts;
    if (s < 45) return "just now";
    if (s < 90 * 60) return Math.max(1, Math.round(s / 60)) + " min ago";
    if (s < 36 * 3600) return Math.round(s / 3600) + " h ago";
    return Math.round(s / 86400) + " d ago";
  };
  CC.when = (ts) => (ts ? new Date(ts * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "—");
  CC.dur = (s) => {
    if (s == null) return "—";
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
  };
  CC.pct = (v, digits = 0) => (v == null ? "—" : Number(v).toFixed(digits) + "%");
  CC.kb = (b) => (b == null ? "—" : b < 1024 ? b + " B" : b < 1048576 ? (b / 1024).toFixed(0) + " kB" : (b / 1048576).toFixed(1) + " MB");
  CC.ipNum = (ip) => (ip || "").split(".").reduce((a, o) => a * 256 + (+o || 0), 0);
  CC.plural = (n, one, many) => `${n} ${n === 1 ? one : many || one + "s"}`;
  /** An address-shaped search (digits and dots, at least one dot) becomes an
   *  octet-aware matcher; anything else returns null and is searched as text.
   *    192.168.0.1   exactly that address (not .10–.199)
   *    .31  / .0.1   addresses ENDING in those octets
   *    192.168.0.    that subnet (addresses starting with those octets)
   *    88.3          octets starting "88.3" anywhere: 192.168.88.3, .30–.39
   *    192.168.0.1*  trailing * widens a full address: .1 and .10–.199
   */
  CC.IP_SEARCH_HELP = "IP search: 192.168.0.1 = that address only · .31 = ends in .31 · 192.168.0. = that subnet · 192.168.0.1* = .1 and .10–.199";
  CC.ipMatcher = (raw) => {
    let q = String(raw || "").trim();
    if (/^[\d.]+\*$/.test(q) && q.includes(".")) { const pre = q.slice(0, -1); return (ip) => !!ip && ip.startsWith(pre); }
    if (!/^[\d.]+$/.test(q) || !q.includes(".") || q.includes("..")) return null;
    if (/^\d{1,3}(\.\d{1,3}){3}$/.test(q)) return (ip) => ip === q;
    if (q.startsWith(".")) return (ip) => !!ip && ip.endsWith(q);
    if (q.endsWith(".")) return (ip) => !!ip && ip.startsWith(q);          // a subnet: from the first octet
    return (ip) => !!ip && ("." + ip + ".").includes("." + q);
  };
  CC.isFullIp = (raw) => /^\d{1,3}(\.\d{1,3}){3}$/.test(String(raw || "").trim());
  CC.debounce = (fn, ms = 200) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

  // ---- icons (inline SVG, stroke = currentColor) ------------------------------
  const P = {
    grid: "M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z",
    alert: "M12 3 2 20h20L12 3zm0 6v5m0 3h.01",
    chip: "M9 3v2M15 3v2M9 19v2M15 19v2M3 9h2M3 15h2M19 9h2M19 15h2M6 6h12v12H6zM10 10h4v4h-4z",
    save: "M5 3h11l3 3v15H5zM8 3v5h8V3M8 14h8v7H8z",
    shield: "M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6l-8-3z",
    gear: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zm7.4-3a7.4 7.4 0 0 0-.1-1.3l2-1.6-2-3.4-2.4 1a7 7 0 0 0-2.2-1.3L14.4 3h-4l-.4 2.4a7 7 0 0 0-2.2 1.3l-2.4-1-2 3.4 2 1.6a7.4 7.4 0 0 0 0 2.6l-2 1.6 2 3.4 2.4-1a7 7 0 0 0 2.2 1.3l.4 2.4h4l.4-2.4a7 7 0 0 0 2.2-1.3l2.4 1 2-3.4-2-1.6c.1-.4.1-.9.1-1.3z",
    site: "M3 21h18M5 21V8l7-5 7 5v13M9 21v-6h6v6",
    refresh: "M4 4v5h5M20 20v-5h-5M5 9a7.5 7.5 0 0 1 13.5-2.5M19 15a7.5 7.5 0 0 1-13.5 2.5",
    search: "M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14zm9 3-4.3-4.3",
    ext: "M14 4h6v6M20 4l-9 9M18 14v6H4V6h6",
    term: "M4 17l6-5-6-5M12 19h8",
    lock: "M6 11h12v10H6zM9 11V8a3 3 0 0 1 6 0v3",
    key: "M14 10a4 4 0 1 1-8 0 4 4 0 0 1 8 0zm0 0h7v3m-3-3v3",
    file: "M6 3h9l4 4v14H6zM14 3v5h5M9 13h6M9 17h6",
    wifi: "M2 9a15 15 0 0 1 20 0M5 12.5a10 10 0 0 1 14 0M8.5 16a5 5 0 0 1 7 0M12 20h.01",
    sparkle: "M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8zM19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8z",
    clock: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zm0-13v5l3 2",
    plug: "M9 2v6M15 2v6M6 8h12v4a6 6 0 0 1-12 0zM12 18v4",
    down: "M12 4v12m0 0-5-5m5 5 5-5M4 20h16",
    plus: "M12 5v14M5 12h14",
    menu: "M4 6h16M4 12h16M4 18h16",
    x: "M6 6l12 12M18 6 6 18",
    phone: "M8 2h8a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1H8a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1zm4 17h.01",
    bell: "M6 16V11a6 6 0 0 1 12 0v5l2 2H4zM10 20a2 2 0 0 0 4 0",
    logout: "M15 4h4v16h-4M10 16l-4-4 4-4M6 12h10",
    history: "M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5M12 7v5l3 2",
    list: "M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01",
    pin: "M12 21s-7-6.2-7-11.5A7 7 0 0 1 19 9.5C19 14.8 12 21 12 21zm0-9a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z",
  };
  CC.icon = (n, cls = "") => `<svg class="ic ${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${P[n] || ""}"/></svg>`;

  // ---- toasts + dialogs -------------------------------------------------------
  CC.toast = (msg, kind = "") => {
    const t = document.createElement("div");
    t.className = "toast " + kind;
    t.setAttribute("role", kind === "bad" ? "alert" : "status");
    t.textContent = msg;
    $("#toasts").appendChild(t);
    setTimeout(() => t.remove(), kind === "bad" ? 7000 : 3800);
  };
  /** Open a <dialog>. html = inner markup; returns the element. Closed dialogs remove themselves. */
  CC.dialog = (html, { cls = "modal", onClose } = {}) => {
    const d = document.createElement("dialog");
    d.className = cls;
    d.innerHTML = html;
    document.body.appendChild(d);
    d.addEventListener("close", () => { onClose && onClose(); d.remove(); });
    d.addEventListener("click", (e) => { if (e.target === d) d.close(); });
    d.addEventListener("click", (e) => { if (e.target.closest("[data-close]")) d.close(); });
    d.showModal();
    return d;
  };
  CC.confirm = (title, body, { ok = "Continue", danger = false } = {}) =>
    new Promise((resolve) => {
      let answered = false;
      const d = CC.dialog(
        `<div class="dhd"><div><h2>${CC.esc(title)}</h2></div></div>
         <div class="dbd"><p style="margin:0;white-space:pre-line">${CC.esc(body)}</p></div>
         <div class="dft"><button class="btn" data-close>Cancel</button><button class="btn ${danger ? "danger" : "pri"}" data-ok>${CC.esc(ok)}</button></div>`,
        { onClose: () => resolve(answered) }
      );
      d.querySelector("[data-ok]").onclick = () => { answered = true; d.close(); };
      d.querySelector("[data-ok]").focus();
    });
  CC.copy = async (text, btn) => {
    try { await navigator.clipboard.writeText(text); }
    catch (e) {
      const ta = document.createElement("textarea");
      ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove();
    }
    if (btn) { const o = btn.textContent; btn.textContent = "Copied"; setTimeout(() => (btn.textContent = o), 1400); }
    else CC.toast("Copied");
  };
  CC.busy = async (btn, fn) => {
    if (btn) { btn.disabled = true; btn.classList.add("busy"); }
    try { return await fn(); }
    finally { if (btn) { btn.disabled = false; btn.classList.remove("busy"); } }
  };
  CC.download = (blob, name) => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  };

  // ---- API ---------------------------------------------------------------------
  class ApiError extends Error { constructor(msg, status, body) { super(msg); this.status = status; this.body = body; } }
  CC.ApiError = ApiError;
  CC.api = async (path, { method = "GET", body, timeout = 20000, raw = false } = {}) => {
    const opt = { method, credentials: "same-origin", headers: {} };
    if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    if (window.AbortSignal && AbortSignal.timeout) opt.signal = AbortSignal.timeout(timeout);
    let r;
    try { r = await fetch(path, opt); }
    catch (e) { throw new ApiError(e.name === "TimeoutError" ? "The hub took too long to answer" : "Can't reach the hub", 0); }
    if (r.status === 401 && !path.startsWith("/api/hub/backup-key")) {
      location.href = "/login?next=" + encodeURIComponent(location.pathname + location.hash);
      throw new ApiError("Signed out", 401);
    }
    if (raw) return r;
    let j = null;
    try { j = await r.json(); } catch (e) { /* non-JSON */ }
    if (!r.ok || (j && j.ok === false)) throw new ApiError((j && (j.error || j.message)) || `Request failed (${r.status})`, r.status, j);
    return j || {};
  };
  /** Run async jobs with at most n in flight. */
  CC.pool = async (items, n, fn) => {
    const out = new Array(items.length);
    let i = 0;
    const work = async () => { while (i < items.length) { const k = i++; try { out[k] = await fn(items[k], k); } catch (e) { out[k] = e; } } };
    await Promise.all(Array.from({ length: Math.min(n, items.length) }, work));
    return out;
  };

  // ---- device knowledge ------------------------------------------------------------
  const QUIET_AFTER = 7 * 86400;
  CC.QUIET_AFTER = QUIET_AFTER;
  CC.CATS = {
    camera: { label: "Camera", group: "cctv", icon: "📷" },
    nvr: { label: "Recorder (NVR)", group: "cctv", icon: "📼" },
    alarm: { label: "Alarm", group: "power", icon: "🚨" },
    router: { label: "Router", group: "net", icon: "🧭" },
    network: { label: "Switch / AP", group: "net", icon: "🔀" },
    "internet-ap": { label: "Wireless link", group: "net", icon: "📡" },
    solar: { label: "Solar / power", group: "power", icon: "☀️" },
    nas: { label: "Storage (NAS)", group: "other", icon: "🗄️" },
    printer: { label: "Printer", group: "other", icon: "🖨️" },
    voip: { label: "Phone", group: "other", icon: "📞" },
    media: { label: "Media", group: "other", icon: "📺" },
    iot: { label: "Smart device", group: "other", icon: "💡" },
    pc: { label: "Computer", group: "other", icon: "💻" },
    server: { label: "Server", group: "other", icon: "🖥️" },
    unknown: { label: "Unidentified", group: "unknown", icon: "❔" },
  };
  CC.GROUPS = [
    ["cctv", "Cameras & recorders"],
    ["net", "Network & wireless"],
    ["power", "Alarm & power"],
    ["other", "Other devices"],
    ["unknown", "Unidentified"],
  ];
  CC.cat = (d) => CC.CATS[d.category] || { label: d.category || "Unidentified", group: d.category ? "other" : "unknown", icon: "•" };
  /** Equipment a monthly support contract is normally about. */
  CC.isInfra = (d) => ["cctv", "net", "power"].includes(CC.cat(d).group) || CC.isMikrotik(d);
  CC.isMikrotik = (d) => /mikrotik|routerboard|routeros/i.test([d.vendor, d.model, d.hostname, d.name, d.banner, d.os].join(" "));
  CC.devName = (d) =>
    d.name || d.device_name || d.model || (d.hostname && !/^(localhost|unknown)$/i.test(d.hostname) ? d.hostname : "") || (d.type && !/^unknown/i.test(d.type) ? d.type : "") || (d.vendor ? d.vendor.replace(/,?\s*(Co\.|Ltd|Inc|Corp|Technology|Digital).*$/i, "") : "") || "Unknown device";
  /** online | offline (seen in the last 7 days) | quiet (not seen for 7+ days) */
  CC.devState = (d) => (d.online ? "online" : d.last_seen && CC.now() - d.last_seen < QUIET_AFTER ? "offline" : "quiet");

  // ---- state ---------------------------------------------------------------------------
  const S = (CC.state = {
    loaded: false, error: "", updated: 0,
    hubName: "", sites: [],            // overview cards
    devices: {}, devicesAt: {},        // site id -> devices[]
    internet: {}, wifi: {}, kuma: {}, backups: {}, sysinfo: {},
    vpn: null, remote: null,
    listeners: new Set(),
  });
  CC.onChange = (fn) => { S.listeners.add(fn); return () => S.listeners.delete(fn); };
  const emit = () => S.listeners.forEach((fn) => { try { fn(); } catch (e) { console.error(e); } });
  CC.emit = emit;
  CC.site = (id) => S.sites.find((s) => s.id === id);

  /** Backups: newest entry age in hours, or null when there are none. */
  CC.backupAge = (id) => {
    const b = S.backups[id];
    if (!b || !b.list) return undefined;
    return b.list.length ? (CC.now() - b.list[0].ts) / 3600 : null;
  };

  /** Everything worth a technician's attention at one site, most severe first. */
  CC.siteIssues = (s) => {
    const out = [];
    const add = (sev, title, detail, act) => out.push({ sev, title, detail, site: s, act });
    const link = (tab) => `#/site/${encodeURIComponent(s.id)}${tab ? "/" + tab : ""}`;
    if (!s.enabled) return [{ sev: "unk", title: "Monitoring paused", detail: "This site is switched off in the hub", site: s, act: link() }];
    if (!s.reachable) {
      add("bad", "Site offline", `The hub can't reach ${s.name} over the VPN${s.error ? " (" + s.error + ")" : ""}. Device states below are the last known.`, link());
      return out;
    }
    const devs = S.devices[s.id] || [];
    const watchedDown = devs.filter((d) => d.watch && !d.online);
    if (watchedDown.length) {
      watchedDown.slice(0, 4).forEach((d) => add("bad", `${CC.devName(d)} is down`, `Watched ${CC.cat(d).label.toLowerCase()} · ${d.ip || "no IP"} · last seen ${CC.ago(d.last_seen)}`, link("devices") + "?q=" + encodeURIComponent(d.ip || "")));
      if (watchedDown.length > 4) add("bad", `${watchedDown.length - 4} more watched devices down`, "", link("devices"));
    } else if (s.watched_down) {
      add("bad", CC.plural(s.watched_down, "watched device") + " down", "Device list still loading", link("devices"));
    }
    const net = S.internet[s.id];
    if (net && net.checked_ts && !net.ok) {
      add("bad", net.has_gateway && !net.gateway ? "Farm router not answering" : !net.dns && net.external ? "Internet DNS broken" : "No internet upstream",
        `Checked ${CC.ago(net.checked_ts)} from the site Pi`, link());
    }
    if (s.kuma && s.kuma.down) add("bad", CC.plural(s.kuma.down, "Kuma monitor") + " down", "Uptime Kuma status page", link());
    if (s.pi_health === "crit") add("bad", "Site Pi critical", "Temperature, disk, power or storage — see Pi health", link("health"));
    if (s.stale) add("warn", "Device list out of date", `Last device refresh ${CC.ago(s.fetched_at)}`, link("devices"));
    if (s.conflicts) add("warn", CC.plural(s.conflicts, "IP address conflict"), "Two devices answering on one address", link("problems"));
    if (s.pi_health === "warn") add("warn", "Site Pi needs a look", "A health reading is past its warning level", link("health"));
    if (s.api_auth === "mismatch") add("warn", "Pi rejects the hub's key", "Backups and restores fail. On the Pi: docker exec netwatch python siteauth.py reset-hub-key", link("backups"));
    else if (s.api_auth === "refused") add("warn", "Pi not claimed by the hub", "Set the hub key on the Pi (see README)", link("backups"));
    else if (s.api_auth === "legacy") add("warn", "Pi runs an old Netwatch", "Saved passwords unprotected — update with docker compose pull", link());
    const age = CC.backupAge(s.id);
    if (age === null) add("warn", "No config backup yet", "Take one from the Backups tab", link("backups"));
    else if (age !== undefined && age > 26) add("warn", "Config backup overdue", `Newest backup is ${Math.round(age)} h old`, link("backups"));
    const wifi = S.wifi[s.id];
    if (wifi && wifi.problems && wifi.problems.length) {
      const crit = wifi.problems.filter((p) => p.level === "crit").length;
      add(crit ? "bad" : "warn", CC.plural(wifi.problems.length, "wireless link issue"),
        wifi.problems.slice(0, 2).map((p) => p.what).join(" · "), link("problems"));
    }
    return out;
  };
  CC.siteState = (s) => {
    if (!s.enabled) return "paused";
    if (!s.reachable) return "offline";
    const iss = CC.siteIssues(s);
    if (iss.some((i) => i.sev === "bad")) return "fault";
    if (iss.some((i) => i.sev === "warn")) return "warn";
    return "ok";
  };
  CC.STATE = {
    ok: { label: "Healthy", b: "ok", dot: "", card: "" },
    warn: { label: "Needs a look", b: "warn", dot: "warn", card: "st-warn" },
    fault: { label: "Fault", b: "bad", dot: "bad", card: "st-bad" },
    offline: { label: "Offline", b: "bad", dot: "bad", card: "st-bad" },
    paused: { label: "Paused", b: "unk", dot: "unk", card: "st-unk" },
  };
  CC.stateBadge = (s) => { const st = CC.STATE[CC.siteState(s)]; return `<span class="b ${st.b}">${st.label}</span>`; };
  CC.allIssues = () => {
    const rank = { bad: 0, warn: 1, unk: 2, info: 3 };
    return S.sites.flatMap(CC.siteIssues).sort((a, b) => rank[a.sev] - rank[b.sev]);
  };
  CC.siteCounts = (s) => {
    const devs = S.devices[s.id];
    if (!devs) return { total: s.devices_total, online: s.devices_online, offline: null, quiet: null, infra: null, infraUp: null };
    let online = 0, offline = 0, quiet = 0, infra = 0, infraUp = 0;
    devs.forEach((d) => {
      const st = CC.devState(d);
      if (st === "online") online++; else if (st === "offline") offline++; else quiet++;
      if (CC.isInfra(d) && st !== "quiet") { infra++; if (st === "online") infraUp++; }
    });
    return { total: devs.length, online, offline, quiet, infra, infraUp };
  };

  // ---- loading -----------------------------------------------------------------------
  let loading = false;
  CC.load = async ({ deep = false } = {}) => {
    if (loading) return;
    loading = true;
    try {
      const ov = await CC.api("/api/hub/overview");
      S.hubName = ov.hub_name || "Netwatch Hub";
      S.sites = ov.sites || [];
      S.error = ""; S.updated = CC.now(); S.loaded = true;
      emit();
      const due = (map, id, ttl) => deep || !map[id] || CC.now() - (map[id].__at || 0) > ttl;
      await CC.pool(S.sites.filter((s) => s.enabled), 3, async (s) => {
        const jobs = [];
        if (deep || CC.now() - (S.devicesAt[s.id] || 0) > 55 || (S.devicesAt[s.id] || 0) < (s.fetched_at || 0))
          jobs.push(CC.api(`/api/hub/sites/${s.id}/devices`).then((j) => { S.devices[s.id] = j.devices || []; S.devicesAt[s.id] = CC.now(); }));
        if (s.reachable && due(S.internet, s.id, 60))
          jobs.push(CC.api(`/api/hub/sites/${s.id}/internet`, { timeout: 15000 }).then((j) => { S.internet[s.id] = { ...j, __at: CC.now() }; }).catch(() => {}));
        if (s.reachable && due(S.wifi, s.id, 300))
          jobs.push(CC.api(`/api/hub/sites/${s.id}/wifi`, { timeout: 15000 }).then((j) => { S.wifi[s.id] = { ...j, __at: CC.now() }; }).catch(() => { S.wifi[s.id] = { __at: CC.now() }; }));
        if (due(S.backups, s.id, 300))
          jobs.push(CC.api(`/api/hub/sites/${s.id}/backups`).then((j) => { S.backups[s.id] = { list: j.backups || [], __at: CC.now() }; }).catch(() => {}));
        await Promise.all(jobs.map((p) => p.catch(() => {})));
        emit();
      });
    } catch (e) {
      if (e.status !== 401) { S.error = e.message; emit(); }
    } finally {
      loading = false;
    }
  };
  CC.pollAll = async (btn) => CC.busy(btn, async () => {
    await CC.pool(S.sites.filter((s) => s.enabled), 4, (s) => CC.api(`/api/hub/sites/${s.id}/poll`, { method: "POST" }).catch(() => {}));
    CC.toast("Asked every site for fresh data…");
    await new Promise((r) => setTimeout(r, 2500));
    S.devicesAt = {};
    await CC.load({ deep: true });
  });

  // ---- router ------------------------------------------------------------------------
  const routes = [];
  CC.route = (pattern, view) => routes.push({ re: new RegExp("^" + pattern.replace(/:(\w+)/g, "([^/?]+)") + "(?:\\?(.*))?$"), keys: (pattern.match(/:(\w+)/g) || []).map((k) => k.slice(1)), view });
  CC.params = () => new URLSearchParams((location.hash.split("?")[1] || ""));
  CC.go = (hash) => { if (location.hash === hash) render(); else location.hash = hash; };
  let current = null;
  function render() {
    const h = (location.hash || "#/").slice(1) || "/";
    for (const r of routes) {
      const m = h.match(r.re);
      if (!m) continue;
      const args = {};
      r.keys.forEach((k, i) => (args[k] = decodeURIComponent(m[i + 1])));
      if (current && current.leave) current.leave();
      current = r.view;
      $(".side").classList.remove("open");
      r.view.enter(args);
      renderShell();
      $("#main").focus({ preventScroll: true });
      return;
    }
    CC.go("#/");
  }
  CC.rerender = () => { if (current && current.update) current.update(); renderShell(); };

  // ---- shell -------------------------------------------------------------------------
  CC.setCrumbs = (parts) => {
    $("#crumbs").innerHTML = parts.map((p, i) => (i < parts.length - 1 && p.href ? `<a href="${p.href}">${CC.esc(p.label)}</a><span class="dim">/</span>` : `<b>${CC.esc(p.label)}</b>`)).join("");
    document.title = parts[parts.length - 1].label + " · Netwatch";
  };
  function renderShell() {
    const h = location.hash || "#/";
    $$(".nav a[data-nav]").forEach((a) => {
      const k = a.dataset.nav;
      a.classList.toggle("on", k === "/" ? h === "#/" || h === "#" || h === "" : h.startsWith("#" + k));
    });
    const issues = CC.allIssues();
    const bad = issues.filter((i) => i.sev === "bad").length;
    const cnt = $("#nav-att-count");
    cnt.hidden = !issues.length;
    cnt.textContent = issues.length;
    cnt.classList.toggle("quiet", !bad);
    $("#nav-sites").innerHTML = S.sites.map((s) => {
      const st = CC.STATE[CC.siteState(s)];
      const on = h.startsWith("#/site/" + encodeURIComponent(s.id));
      return `<a href="#/site/${encodeURIComponent(s.id)}" class="${on ? "on" : ""}"><i class="dot ${st.dot}"></i><span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${CC.esc(s.name)}</span></a>`;
    }).join("") || `<span class="note" style="padding:4px 10px">No sites yet</span>`;
    const live = $("#live");
    const age = CC.now() - S.updated;
    live.className = "live" + (S.error ? " down" : age > 90 ? " stale" : "");
    $("#live-text").textContent = S.error ? "Hub unreachable" : S.updated ? "Updated " + CC.ago(S.updated) : "Connecting…";
  }
  CC.renderShell = renderShell;

  // ---- boot ----------------------------------------------------------------------------
  CC.start = () => {
    $("#menu-btn").onclick = () => $(".side").classList.toggle("open");
    $("#refresh-all").onclick = (e) => CC.pollAll(e.currentTarget);
    $("#logout").onclick = async () => { await CC.api("/api/logout", { method: "POST" }).catch(() => {}); location.href = "/login"; };
    window.addEventListener("hashchange", render);
    CC.onChange(CC.rerender);
    render();
    CC.load({ deep: true });
    setInterval(() => { if (!document.hidden) CC.load(); }, 20000);
    setInterval(renderShell, 15000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden && CC.now() - S.updated > 20) CC.load(); });
  };
})();
