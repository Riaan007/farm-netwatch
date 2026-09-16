// The network view renders data a client site sends (hub Control Center), so
// app/static/topoview.js coerces it before drawing. Run: node tests/topoview-sanitize.cjs
const fs = require("fs");
const path = require("path");
global.window = {};
eval(fs.readFileSync(path.join(__dirname, "../app/static/topoview.js"), "utf8"));
const TV = window.TopoView;
let fails = 0;
const ok = (cond, msg) => { console.log((cond ? "ok   " : "FAIL ") + msg); if (!cond) fails++; };
const evil = '<img src=x onerror=alert(1)>';
const g = TV.sanitize({
  nodes: [
    { id: "a", name: evil, state: evil, pos: { x: evil, y: 3 }, geo: { lat: evil, lon: 1 }, last_seen: evil,
      problems: [{ detail: evil }], inferred: { state: evil, online: evil, total: "3" },
      radio: { signal: evil, stations: evil, mode: evil } },
    { id: 7 }, null,
  ],
  groups: [{ id: "g", name: evil, status: evil, counts: { online: evil, total: "4" }, pos: { x: "10", y: "20" },
             geo: { lat: "-33.1", lon: "18.2" }, lat: evil }],
  links: [{ id: "l", a: "a", b: "a", medium: evil, status: evil, label: 5, metrics: { signal: evil, distance: "2300" } },
          { id: "x", a: 1, b: "a" }],
  suggestions: [{ id: "s", members: [1, 2], unknown: evil, medium: "fibre" }],
  unassigned: { pos: { x: evil }, size: {} },
  grid: { cell_w: evil },
  kinds: [{ id: "camera", label: evil, tier: evil }, { id: 3 }],
  site: { lat: evil, lon: "18" },
});
const n = g.nodes[0];
ok(g.nodes.length === 1, "nodes without a string id are dropped");
ok(n.state === "quiet" && n.pos === null && n.geo === null && n.last_seen === 0, "node state/pos/geo/last_seen coerced");
ok(n.inferred.state === "passing" && n.inferred.online === 0 && n.inferred.total === 3, "inferred coerced");
ok(n.radio.signal === null && n.radio.stations === null, "radio numbers coerced");
ok(typeof n.problems[0].detail === "string", "problem text stays text (escaped at draw time)");
const gr = g.groups[0];
ok(gr.status === "unknown" && gr.counts.online === 0 && gr.counts.total === 4 && gr.pos.x === 10 && gr.lat === null, "group coerced");
ok(gr.geo.lat === -33.1 && gr.geo.lon === 18.2, "group geo numbers");
ok(g.links.length === 1 && g.links[0].medium === "ethernet" && g.links[0].status === "unknown" && g.links[0].label === "5", "link coerced");
ok(g.links[0].metrics.signal === null && g.links[0].metrics.distance === 2300, "link metrics coerced");
ok(g.suggestions[0].members.every((m) => typeof m === "string") && g.suggestions[0].unknown === 0, "suggestion coerced");
ok(g.unassigned.pos.x === 0 && g.grid.cell_w === 150, "review area + grid defaults");
ok(g.kinds.length === 1 && g.kinds[0].tier === 4, "kinds coerced");
ok(g.site.lat === null && g.site.lon === 18, "site location coerced");
ok(TV.esc(evil) === "&lt;img src=x onerror=alert(1)&gt;", "esc() escapes markup");
ok(TV.parseLatLon("https://www.google.com/maps/@-33.8801,18.9900,17z").lat === -33.8801, "Google Maps links parse");
ok(TV.parseLatLon("33°52'48\"S 18°59'24\"E").lat < -33.87, "DMS parses");
ok(TV.parseLatLon("0, 0") === null && TV.parseLatLon("nowhere") === null, "junk and 0,0 refused");
if (fails) { console.log(`${fails} failed`); process.exit(1); }
console.log("all passed");
