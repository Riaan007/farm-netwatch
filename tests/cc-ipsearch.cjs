/* IP search rules of the Control Center (hub/app/static/cc/core.js CC.ipMatcher).
 * No browser needed:  node tests/cc-ipsearch.cjs */
global.window = {};
global.document = { querySelector() {}, querySelectorAll() { return []; } };
require("../hub/app/static/cc/core.js");
const CC = window.CC;
const ips = ["192.168.0.1", "192.168.0.10", "192.168.0.11", "192.168.0.110", "192.168.0.199", "192.168.0.2",
  "192.168.88.3", "192.168.88.31", "192.168.88.35", "192.168.10.1", ""];
const cases = {
  "192.168.0.1": ["192.168.0.1"],                                     // a full address is exact
  " 192.168.0.1 ": ["192.168.0.1"],
  "192.168.0.1*": ["192.168.0.1", "192.168.0.10", "192.168.0.11", "192.168.0.110", "192.168.0.199"],
  ".31": ["192.168.88.31"],                                           // ends in
  ".0.1": ["192.168.0.1"],
  "192.168.0.": ["192.168.0.1", "192.168.0.10", "192.168.0.11", "192.168.0.110", "192.168.0.199", "192.168.0.2"],
  "88.3": ["192.168.88.3", "192.168.88.31", "192.168.88.35"],         // octet-aligned, last octet a prefix
  "0.1": ["192.168.0.1", "192.168.0.10", "192.168.0.11", "192.168.0.110", "192.168.0.199"],
  "camera": null, "31": null, "1..2": null,                            // not address-shaped: text search
};
let bad = 0;
for (const [q, want] of Object.entries(cases)) {
  const m = CC.ipMatcher(q);
  const got = m ? ips.filter(m) : null;
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) bad++;
  console.log(ok ? "ok  " : "FAIL", JSON.stringify(q).padEnd(16), ok ? "" : `got ${JSON.stringify(got)}`);
}
console.log(bad ? `${bad} failed` : "all passed");
process.exit(bad ? 1 : 0);
