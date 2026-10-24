// SPDX-License-Identifier: Apache-2.0
// The welcome page's only script: a dark/light theme toggle, same behavior and storage key as the
// /stats and /config pages. Same-origin (script-src 'self'); no data is fetched.
(function () {
  "use strict";
  var THEME_KEY = "oaken-theme";
  function label(theme) {
    var el = document.getElementById("theme-label");
    if (el) el.textContent = theme === "dark" ? "Light" : "Dark";
  }
  function setTheme(theme) {
    document.documentElement.setAttribute("data-bs-theme", theme);
    try { localStorage.setItem(THEME_KEY, theme); } catch (e) { /* private mode: ignore */ }
    label(theme);
  }
  var saved;
  try { saved = localStorage.getItem(THEME_KEY); } catch (e) { saved = null; }
  setTheme(saved || document.documentElement.getAttribute("data-bs-theme") || "light");
  var btn = document.getElementById("theme-toggle");
  if (btn) btn.addEventListener("click", function () {
    var now = document.documentElement.getAttribute("data-bs-theme");
    setTheme(now === "dark" ? "light" : "dark");
  });
})();
