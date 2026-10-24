// SPDX-License-Identifier: Apache-2.0
// The /config editor's only script. Same-origin (script-src 'self', connect-src 'self'): it talks
// to core's loopback /config/* routes, which delegate to the config_* operations. The page is
// loopback-only and token-free, like /stats; config.toml holds no secrets.
(function () {
  "use strict";

  // --- dark/light theme toggle: same behavior and storage key as the /stats page ---
  var THEME_KEY = "oaken-theme";
  function themeLabel(theme) {
    var el = document.getElementById("theme-label");
    if (el) el.textContent = theme === "dark" ? "Light" : "Dark";
  }
  function setTheme(theme) {
    document.documentElement.setAttribute("data-bs-theme", theme);
    try { localStorage.setItem(THEME_KEY, theme); } catch (e) { /* private mode: ignore */ }
    themeLabel(theme);
  }
  (function initTheme() {
    var saved;
    try { saved = localStorage.getItem(THEME_KEY); } catch (e) { saved = null; }
    var theme = saved || document.documentElement.getAttribute("data-bs-theme") || "light";
    setTheme(theme);
    var btn = document.getElementById("theme-toggle");
    if (btn) btn.addEventListener("click", function () {
      var now = document.documentElement.getAttribute("data-bs-theme");
      setTheme(now === "dark" ? "light" : "dark");
    });
  })();

  var ta = document.getElementById("config");
  var pathEl = document.getElementById("config-path");
  var result = document.getElementById("result");
  var saved = ""; // last-known on-disk text, for "Revert to saved"

  function show(kind, msg) {
    result.className = "alert alert-" + kind;
    result.textContent = msg;
  }
  function busy(on) {
    ["btn-test", "btn-save", "btn-reload", "btn-revert"].forEach(function (id) {
      document.getElementById(id).disabled = on;
    });
  }

  function post(path, body) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body || {}),
    }).then(function (r) { return r.json(); });
  }

  function load() {
    fetch("/config/get", { headers: { Accept: "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        saved = d.text || "";
        ta.value = saved;
        if (pathEl) pathEl.textContent = d.path || "config.toml";
        if (!d.exists) show("info", "No config.toml yet — saving will create it.");
      })
      .catch(function () { show("danger", "Could not load config.toml."); });
  }

  document.getElementById("btn-test").addEventListener("click", function () {
    busy(true);
    post("/config/test", { text: ta.value })
      .then(function (d) {
        if (d.ok) show("success", "Valid. Safe to save.");
        else show("danger", "Invalid: " + (d.error || "unknown error"));
      })
      .catch(function () { show("danger", "Test failed to run."); })
      .finally(function () { busy(false); });
  });

  document.getElementById("btn-save").addEventListener("click", function () {
    busy(true);
    post("/config/save", { text: ta.value })
      .then(function (d) {
        if (d.ok) {
          saved = ta.value;
          var note = d.backup ? " (backup: " + d.backup + ")" : "";
          show("success", "Saved" + note + ". Click Reload Oaken to apply.");
        } else {
          show("danger", "Not saved — " + (d.error || "validation failed"));
        }
      })
      .catch(function () { show("danger", "Save failed to run."); })
      .finally(function () { busy(false); });
  });

  document.getElementById("btn-reload").addEventListener("click", function () {
    busy(true);
    show("info", "Reloading core…");
    post("/config/reload", {})
      .then(function (d) {
        if (!d.ok) { show("danger", "Reload refused: " + (d.error || "unknown")); busy(false); return; }
        // Core rebuilds in place; the port may change and this very server briefly restarts.
        // Poll /v1/health (unauthenticated, loopback) until it answers again.
        waitForHealth(0);
      })
      .catch(function () {
        // A dropped connection is expected mid-reload; start polling anyway.
        waitForHealth(0);
      });
  });

  function waitForHealth(attempt) {
    if (attempt > 30) { show("warning", "Core is taking a while to come back. Check /stats."); busy(false); return; }
    setTimeout(function () {
      fetch("/v1/health", { headers: { Accept: "application/json" } })
        .then(function (r) { if (r.ok) { show("success", "Core reloaded on the new config."); busy(false); } else { waitForHealth(attempt + 1); } })
        .catch(function () { waitForHealth(attempt + 1); });
    }, 1000);
  }

  document.getElementById("btn-revert").addEventListener("click", function () {
    ta.value = saved;
    show("secondary", "Reverted to the last saved config.");
  });

  load();
})();
