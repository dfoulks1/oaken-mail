// SPDX-License-Identifier: Apache-2.0
// The /stats page's only script: an enhancement, not a requirement. The page is fully rendered by
// the server; this just re-fetches the same counts every few seconds and swaps them in place, so
// the numbers stay current without a full reload that would lose the open tab. Same-origin only
// (script-src 'self', connect-src 'self') — nothing external, works offline. Read it in full:
// it fetches one local JSON and sets textContent on elements the template marked with data-stat,
// and, on the Plugins tab, posts the switch and Reload button to core's /plugins/* routes (which
// run plugin_enable, plugin_disable and plugin_reload) and then refreshes that row.
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
    show(data.plugins);
    var stamp = document.getElementById("stats-updated");
    if (stamp && data.generated_at) stamp.textContent = data.generated_at;
  }

  // --- plugins tab: the enable switch and the reload button ---
  var result = document.getElementById("plugin-result");
  function say(kind, msg) {
    if (!result) return;
    result.className = "alert alert-" + kind + " mb-0 rounded-0 small";
    result.textContent = msg;
  }
  function badge(p) {
    var cls = p.healthy ? "text-bg-success" : p.started ? "text-bg-danger" : "badge-oaken-warn";
    var text = p.healthy ? "healthy" : (p.detail || (p.started ? "unhealthy" : "not started"));
    var span = document.createElement("span");
    span.className = "badge " + cls;
    span.textContent = text;
    return span;
  }
  function show(plugins) {
    // Set each row's switch and health from fresh plugin data (textContent only: no HTML is built).
    (plugins || []).forEach(function (p) {
      document.querySelectorAll("[data-plugin-switch]").forEach(function (el) {
        if (el.getAttribute("data-plugin-switch") === p.name) el.checked = !!p.enabled;
      });
      document.querySelectorAll("[data-plugin-health]").forEach(function (cell) {
        if (cell.getAttribute("data-plugin-health") !== p.name) return;
        while (cell.firstChild) cell.removeChild(cell.firstChild);
        cell.appendChild(badge(p));
      });
    });
  }
  function control(action, name) {
    return fetch("/plugins/" + action.replace("plugin_", ""), {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ name: name })
    }).then(function (r) {
      return r.json().then(function (body) { return { ok: r.ok, body: body }; });
    });
  }
  function act(action, name, box) {
    say("secondary", action + " " + name + "…");
    return control(action, name).then(function (res) {
      if (!res.ok) {
        say("danger", (res.body && res.body.message) || action + " failed");
        if (box) box.checked = !box.checked;  // it did not happen: put the switch back
        return;
      }
      var plugins = res.body.plugins || [];
      show(plugins);
      var row = plugins.filter(function (p) { return p.name === name; })[0] || plugins[0] || {};
      if (action === "plugin_enable" && !row.started && row.detail === "disabled") {
        say("warning", name + " stays off: its [plugins." + name.split("@")[0] + "] table says " +
          "enabled = false. Change it with: oaken plugins set " + name.split("@")[0] +
          " enabled=true, then reload.");
      } else {
        say(row.healthy === false && action !== "plugin_disable" ? "warning" : "success",
          action.replace("plugin_", "") + " " + name + ": " + (row.detail || (row.healthy ? "healthy" : "done")));
      }
    }).catch(function () {
      say("danger", "core did not answer");
      if (box) box.checked = !box.checked;
    });
  }
  document.addEventListener("change", function (ev) {
    var box = ev.target.closest && ev.target.closest("[data-plugin-switch]");
    if (!box) return;
    act(box.checked ? "plugin_enable" : "plugin_disable", box.getAttribute("data-plugin-switch"), box);
  });
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest && ev.target.closest("[data-plugin-reload]");
    if (btn) act("plugin_reload", btn.getAttribute("data-plugin-reload"), null);
  });

  function tick() {
    fetch("/stats.json", { headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) { if (data) apply(data); })
      .catch(function () { /* a failed poll is harmless; the last values stand */ });
  }

  initTheme();
  setInterval(tick, EVERY_MS);
})();
