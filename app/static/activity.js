/* Hands-on time for the hub's time keeping (app/useractivity.py).
 * About once a minute, while someone is using this page (input in the last two
 * minutes and the tab visible), tell the Pi. The Pi counts it only for a session
 * logged in with its password, and not when the page came through the hub. */
(function () {
  let lastInput = 0, lastMove = 0, lastBeat = 0;
  const touched = () => { lastInput = Date.now(); };
  ["pointerdown", "keydown", "wheel", "touchstart", "scroll"].forEach((ev) =>
    window.addEventListener(ev, touched, { passive: true, capture: true }));
  window.addEventListener("mousemove", () => {
    const n = Date.now();
    if (n - lastMove > 5000) { lastMove = n; lastInput = n; }
  }, { passive: true });
  function beat(force) {
    const now = Date.now();
    if (document.hidden || now - lastInput > 120000 || now - lastBeat < (force ? 20000 : 55000)) return;
    lastBeat = now;
    fetch("/api/activity/beat", { method: "POST", credentials: "same-origin" }).catch(() => {});
  }
  setInterval(() => beat(false), 15000);
  window.addEventListener("hashchange", () => { touched(); beat(true); });
})();
