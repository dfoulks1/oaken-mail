// SPDX-License-Identifier: Apache-2.0
// The /stats page's only script: an enhancement, not a requirement. The page is fully rendered by
// the server; this just re-fetches the same counts every few seconds and swaps them in place, so
// the numbers stay current without a full reload that would lose the open tab. Same-origin only
// (script-src 'self', connect-src 'self') — nothing external, works offline. Read it in full:
// it fetches one local JSON and sets textContent on elements the template marked with data-stat.
(function () {
  "use strict";
  var EVERY_MS = 10000;
  var THEME_KEY = "oaken-theme";  // same key the docs site uses

  // --- theme toggle: flip data-bs-theme on <html>, remember the choice ---
  function label(theme) {
    var el = document.getElementById("theme-label");
    if (el) el.textContent = theme === "dark" ? "Light" : "Dark";
  }
  function setTheme(theme) {
    document.documentElement.setAttribute("data-bs-theme", theme);
    try { localStorage.setItem(THEME_KEY, theme); } catch (e) { /* private mode: ignore */ }
    label(theme);
  }
  function initTheme() {
    var saved;
    try { saved = localStorage.getItem(THEME_KEY); } catch (e) { saved = null; }
    var theme = saved || document.documentElement.getAttribute("data-bs-theme") || "dark";
    setTheme(theme);
    var btn = document.getElementById("theme-toggle");
    if (btn) {
      btn.addEventListener("click", function () {
        var now = document.documentElement.getAttribute("data-bs-theme");
        setTheme(now === "dark" ? "light" : "dark");
      });
    }
  }

  function at(obj, path) {
    // "pipeline.stages.enrich.pending" -> walk the object; undefined if any step is missing.
    var cur = obj;
    var parts = path.split(".");
    for (var i = 0; i < parts.length; i++) {
      if (cur == null) return undefined;
      cur = cur[parts[i]];
    }
    return cur;
  }

  function apply(data) {
    var nodes = document.querySelectorAll("[data-stat]");
    for (var i = 0; i < nodes.length; i++) {
      var v = at(data, nodes[i].getAttribute("data-stat"));
      if (v !== undefined && v !== null) nodes[i].textContent = String(v);
    }
    var stamp = document.getElementById("stats-updated");
    if (stamp && data.generated_at) stamp.textContent = data.generated_at;
  }

  function tick() {
    fetch("/stats.json", { headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) { if (data) apply(data); })
      .catch(function () { /* a failed poll is harmless; the last values stand */ });
  }

  initTheme();
  setInterval(tick, EVERY_MS);
})();
