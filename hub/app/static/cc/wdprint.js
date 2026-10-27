/* Wi-Fi Doctor print sheet - shared by the Control Center and the classic site page.
   wdPrint(siteName, result) opens the analysis as a plain black-on-white page (tick boxes
   beside each action, so it works as a work list on paper) and asks the browser to print it. */
(function () {
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const ticks = (items) => `<ul class="tick">${items.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`;

  function sheet(siteName, j) {
    const a = (j && j.analysis) || {};
    const when = new Date().toLocaleString("en-ZA", { dateStyle: "medium", timeStyle: "short" });
    const links = a.links || [];
    return `<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Wi-Fi Doctor \u00b7 ${esc(siteName)} \u00b7 ${esc(when)}</title>
<style>
  @page { size: A4; margin: 14mm 14mm 16mm; }
  * { box-sizing: border-box; }
  body { font: 10.5pt/1.45 system-ui, -apple-system, "Segoe UI", Roboto, Arial, sans-serif; color: #111; background: #fff; margin: 0; padding: 16px; }
  @media print { body { padding: 0; } .noprint { display: none; } }
  header { display: flex; justify-content: space-between; align-items: flex-end; border-bottom: 2px solid #111; padding-bottom: 6px; margin-bottom: 12px; }
  h1 { font-size: 17pt; margin: 0; }
  h2 { font-size: 11pt; text-transform: uppercase; letter-spacing: .04em; margin: 16px 0 6px; border-bottom: 1px solid #bbb; padding-bottom: 2px; }
  .meta { font-size: 9pt; color: #444; text-align: right; }
  .overall { display: inline-block; border: 1.5px solid #111; border-radius: 4px; padding: 1px 8px; font-weight: 700; text-transform: uppercase; font-size: 9.5pt; }
  .overall.critical, .sev.high { background: #111; color: #fff; }
  .summary { margin: 8px 0 2px; }
  .counts { font-size: 9pt; color: #444; }
  .link { border: 1px solid #999; border-radius: 6px; padding: 8px 10px; margin: 0 0 8px; break-inside: avoid; }
  .link.high { border: 2px solid #111; }
  .lhd { display: flex; justify-content: space-between; gap: 8px; }
  .sev { font-size: 8.5pt; font-weight: 700; text-transform: uppercase; border: 1px solid #111; border-radius: 3px; padding: 0 6px; height: fit-content; }
  .verdict { margin: 4px 0; }
  .likely { font-size: 9pt; color: #333; font-style: italic; }
  ul, ol { margin: 4px 0 0; padding-left: 20px; }
  ul.tick { list-style: none; padding-left: 2px; }
  ul.tick li { padding-left: 22px; position: relative; margin: 3px 0; }
  ul.tick li::before { content: ""; position: absolute; left: 0; top: 2px; width: 11px; height: 11px; border: 1.3px solid #111; border-radius: 2px; }
  footer { margin-top: 18px; border-top: 1px solid #bbb; padding-top: 6px; font-size: 8.5pt; color: #555; }
  .noprint { margin-bottom: 14px; }
  .noprint button { font: inherit; padding: 6px 14px; border-radius: 6px; border: 1px solid #111; background: #111; color: #fff; cursor: pointer; }
</style></head><body>
<div class="noprint"><button onclick="window.print()">Print</button> <button onclick="window.close()" style="background:#fff;color:#111">Close</button></div>
<header><div><h1>Wi-Fi Doctor</h1><div>${esc(siteName)}</div></div>
  <div class="meta">${esc(when)}<br>Farm Netwatch</div></header>
<div><span class="overall ${esc(a.overall || "")}">${esc(a.overall || "unknown")}</span></div>
<p class="summary">${esc(a.summary || "")}</p>
<div class="counts">${esc(j.radio_count || 0)} radio(s) read \u00b7 ${esc(j.finding_count || 0)} monitor finding(s)</div>
${(a.site_actions || []).length ? `<h2>Do these first</h2>${ticks(a.site_actions)}` : ""}
${links.length ? `<h2>Links (${links.length})</h2>` + links.map((l) => `<div class="link ${esc(l.severity || "")}">
  <div class="lhd"><b>${esc(l.name)}</b>${l.severity ? `<span class="sev ${esc(l.severity)}">${esc(l.severity)}</span>` : ""}</div>
  <div class="verdict">${esc(l.verdict || "")}</div>
  ${(l.likely_causes || []).length ? `<div class="likely">Likely: ${l.likely_causes.map(esc).join(" \u00b7 ")}</div>` : ""}
  ${(l.actions || []).length ? ticks(l.actions) : ""}</div>`).join("") : ""}
${(a.watch || []).length ? `<h2>Keep an eye on</h2><ul>${a.watch.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
<footer>Written by Gemini${a.model ? " (" + esc(a.model) + ")" : ""} from this site's own radio telemetry. Check each point on site before acting on it.</footer>
</body></html>`;
  }

  window.wdPrint = function (siteName, j) {
    const html = sheet(siteName, j);
    const w = window.open("", "_blank");
    if (w) {
      w.document.open(); w.document.write(html); w.document.close();
      w.focus();
      setTimeout(() => w.print(), 300);
      return;
    }
    // Pop-up blocked: print from a hidden frame instead.
    const f = document.createElement("iframe");
    f.style.cssText = "position:fixed;right:0;bottom:0;width:0;height:0;border:0";
    document.body.appendChild(f);
    f.contentDocument.open(); f.contentDocument.write(html); f.contentDocument.close();
    setTimeout(() => { f.contentWindow.focus(); f.contentWindow.print(); setTimeout(() => f.remove(), 2000); }, 300);
  };
  window.wdPrint.sheet = sheet;
})();
