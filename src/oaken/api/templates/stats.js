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

  // --- AI & Models tab: clear usage stats, and revoke an agent (same idiom as the plugin tab) ---
  function aiSay(kind, msg) {
    var el = document.getElementById("ai-result");
    if (!el) return;
    el.className = "alert alert-" + kind + " mb-3 rounded small";
    el.textContent = msg;
  }
  function post(path, body) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().then(function (b) { return { ok: r.ok, body: b }; });
    });
  }
  document.addEventListener("click", function (ev) {
    var reset = ev.target.closest && ev.target.closest("[data-reset-usage]");
    if (reset) {
      if (!window.confirm("Clear all token-usage figures? Mail, the index and the cache are untouched.")) return;
      aiSay("secondary", "clearing usage…");
      post("/stats/reset-usage").then(function (res) {
        if (!res.ok) { aiSay("danger", (res.body && res.body.message) || "could not clear usage"); return; }
        aiSay("success", "usage cleared (" + (res.body.deleted || 0) + " rows); the table updates on the next refresh.");
      }).catch(function () { aiSay("danger", "core did not answer"); });
      return;
    }
    var rev = ev.target.closest && ev.target.closest("[data-revoke-agent]");
    if (rev) {
      var id = rev.getAttribute("data-revoke-agent");
      if (!window.confirm("Revoke this agent? The jobs it holds return to the queue.")) return;
      aiSay("secondary", "revoking…");
      post("/stats/revoke-agent", { agent_id: id }).then(function (res) {
        if (!res.ok) { aiSay("danger", (res.body && res.body.message) || "could not revoke"); return; }
        aiSay(res.body.revoked ? "success" : "warning", res.body.revoked ? "agent revoked; it updates on the next refresh." : "it was already revoked.");
      }).catch(function () { aiSay("danger", "core did not answer"); });
    }
  });

  // --- AI & Models tab: enricher config forms and the model picker (reuse post/aiSay) ---
  function fieldValue(el) {
    var t = el.getAttribute("data-field-type");
    if (t === "boolean") return el.checked;
    if (t === "number") return el.value === "" ? null : Number(el.value);
    return el.value;
  }
  document.addEventListener("submit", function (ev) {
    var enr = ev.target.closest && ev.target.closest("[data-enricher-save]");
    if (enr) {
      ev.preventDefault();
      var name = enr.getAttribute("data-enricher-save");
      var config = {};
      enr.querySelectorAll("[data-field-type]").forEach(function (el) {
        config[el.getAttribute("name")] = fieldValue(el);
      });
      aiSay("secondary", "saving " + name + "…");
      post("/stats/enricher-config", { plugin: name, config: config }).then(function (res) {
        if (!res.ok || (res.body && res.body.ok === false)) {
          aiSay("danger", (res.body && (res.body.error || res.body.message)) || "could not save " + name);
          return;
        }
        aiSay("success", name + " settings saved to config.toml; they apply on the next enrich job.");
      }).catch(function () { aiSay("danger", "core did not answer"); });
      return;
    }
    var mdl = ev.target.closest && ev.target.closest("[data-model-save]");
    if (mdl) {
      ev.preventDefault();
      var plugin = mdl.getAttribute("data-model-save");
      var sel = mdl.querySelector("[name=model]");
      var model = sel ? sel.value : "";
      aiSay("secondary", "setting model to " + model + " and reloading…");
      post("/stats/model", { plugin: plugin, model: model }).then(function (res) {
        if (!res.ok || (res.body && res.body.ok === false)) {
          aiSay("danger", (res.body && (res.body.error || res.body.message)) || "could not set the model");
          return;
        }
        aiSay("success", "model set to " + model + "; core is reloading (watch /v1/health, the port may change).");
      }).catch(function () { aiSay("danger", "core did not answer"); });
    }
  });

  // --- Reference tab: the MCP exposure switch (same revert-on-failure idiom as the plugin switch) ---
  function exposeSay(kind, msg) {
    var el = document.getElementById("expose-result");
    if (!el) return;
    el.className = "alert alert-" + kind + " mb-3 rounded small";
    el.textContent = msg;
  }
  document.addEventListener("change", function (ev) {
    var box = ev.target.closest && ev.target.closest("[data-op-switch]");
    if (!box) return;
    var name = box.getAttribute("data-op-switch");
    exposeSay("secondary", (box.checked ? "exposing " : "hiding ") + name + "…");
    post("/stats/expose", { name: name, mcp: box.checked }).then(function (res) {
      if (!res.ok) {
        exposeSay("danger", (res.body && res.body.message) || "could not change exposure");
        box.checked = !box.checked;  // it did not happen: put the switch back
        return;
      }
      exposeSay("success", name + " is now " + (box.checked ? "exposed to" : "hidden from") +
        " MCP clients (config.toml written); it takes effect on the next tool list.");
    }).catch(function () {
      exposeSay("danger", "core did not answer");
      box.checked = !box.checked;
    });
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
