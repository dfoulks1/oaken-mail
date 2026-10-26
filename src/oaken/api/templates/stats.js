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
  // Status messages are fixed on screen (see the page style): a success fades after a few
  // seconds, anything else stays until it is clicked, so none is lost off the scrolled page.
  function toast(el, kind, msg) {
    if (!el) return;
    clearTimeout(el._hide);
    el.className = "alert alert-" + kind + " small";
    el.textContent = msg;
    el.title = "Click to dismiss";
    if (kind === "success") el._hide = setTimeout(function () { el.classList.add("d-none"); }, 7000);
  }
  document.addEventListener("click", function (ev) {
    var t = ev.target.closest && ev.target.closest('[id$="-result"].alert');
    if (t) t.classList.add("d-none");
  });
  function say(kind, msg) { toast(result, kind, msg); }
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
    toast(document.getElementById("ai-result"), kind, msg);
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

  // --- AI & Models tab: model dropdowns, the three-dot menu and the plugin settings dialog ---
  // Bootstrap's own script is not loaded (scripts stay same-origin), so the menu and the dialog are
  // plain DOM: keyboard reachable, Escape closes, focus returns to where it came from.
  function fieldValue(el) {
    var t = el.getAttribute("data-field-type");
    if (t === "boolean") return el.checked;
    if (t === "number") return el.value === "" ? null : Number(el.value);
    return el.value;
  }
  function saveConfig(plugin, config) {
    return post("/stats/enricher-config", { plugin: plugin, config: config });
  }
  function failed(res) { return !res.ok || (res.body && res.body.ok === false); }
  function why(res, fallback) { return (res.body && (res.body.error || res.body.message)) || fallback; }

  // A model dropdown saves on change: the backend's model also reloads it, an enricher's applies
  // to its next job. A failure puts the choice back.
  document.addEventListener("change", function (ev) {
    var sel = ev.target.closest && ev.target.closest("[data-model-select]");
    if (!sel) return;
    var plugin = sel.getAttribute("data-plugin"), kind = sel.getAttribute("data-kind");
    var prev = sel.getAttribute("data-prev") || "";
    aiSay("secondary", "setting " + plugin + " model to " + (sel.value || "the backend default") + "…");
    var request = kind === "llm_backend"
      ? post("/stats/model", { plugin: plugin, model: sel.value })
      : kind === "ask"
        ? post("/stats/ask-model", { model: sel.value })
        : saveConfig(plugin, { model: sel.value });
    request.then(function (res) {
      if (failed(res)) { sel.value = prev; aiSay("danger", why(res, "could not set the model")); return; }
      sel.setAttribute("data-prev", sel.value);
      aiSay("success", kind === "llm_backend"
        ? "model set to " + sel.value + "; core is reloading (watch /v1/health, the port may change)."
        : kind === "ask"
          ? "ask will use " + (sel.value || "the backend default") + "; core is reloading."
          : plugin + " will use " + (sel.value || "the backend default") + " from its next job.");
    }).catch(function () { sel.value = prev; aiSay("danger", "core did not answer"); });
  });

  // The three-dot menu.
  var openMenu = null;
  function closeMenu(returnFocus) {
    if (!openMenu) return;
    openMenu.menu.classList.remove("show");
    openMenu.button.setAttribute("aria-expanded", "false");
    if (returnFocus) openMenu.button.focus();
    openMenu = null;
  }
  document.addEventListener("click", function (ev) {
    var button = ev.target.closest && ev.target.closest("[data-kebab]");
    if (button) {
      var menu = button.nextElementSibling, wasOpen = openMenu && openMenu.button === button;
      closeMenu(false);
      if (!wasOpen) {
        menu.classList.add("show");
        button.setAttribute("aria-expanded", "true");
        openMenu = { button: button, menu: menu };
        var first = menu.querySelector("button:not([disabled])");
        if (first) first.focus();
      }
      return;
    }
    if (openMenu && !ev.target.closest(".dropdown-menu")) closeMenu(false);
  });

  // Reload a plugin from the menu (the same route as the Plugins tab).
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest && ev.target.closest("[data-reload-plugin]");
    if (!btn) return;
    var name = btn.getAttribute("data-reload-plugin");
    closeMenu(true);
    aiSay("secondary", "reloading " + name + "…");
    post("/plugins/reload", { name: name }).then(function (res) {
      var row = ((res.body && res.body.plugins) || []).filter(function (p) { return p.name === name; })[0] || {};
      if (!res.ok) { aiSay("danger", (res.body && res.body.message) || "could not reload " + name); return; }
      aiSay(row.healthy === false ? "warning" : "success",
        "reloaded " + name + ": " + (row.detail || (row.healthy ? "healthy" : "done")));
    }).catch(function () { aiSay("danger", "core did not answer"); });
  });

  // The settings dialog.
  var modal = document.getElementById("plugin-modal");
  var backdrop = document.getElementById("pm-backdrop");
  var modalPlugin = null, modalOpener = null;
  function closeModal() {
    if (!modal || modal.hidden) return;
    modal.hidden = true; backdrop.hidden = true;
    modal.classList.remove("show", "d-block");
    document.body.classList.remove("modal-open");
    if (modalOpener) modalOpener.focus();
    modalPlugin = null;
  }
  document.addEventListener("click", function (ev) {
    var edit = ev.target.closest && ev.target.closest("[data-edit-plugin]");
    if (edit && modal) {
      var name = edit.getAttribute("data-edit-plugin"), kind = edit.getAttribute("data-kind");
      var tpl = document.getElementById("pf-" + kind + "-" + name);
      if (!tpl) return;
      modalOpener = openMenu ? openMenu.button : edit;
      closeMenu(false);
      modalPlugin = name;
      document.getElementById("pm-title").textContent = (edit.getAttribute("data-label") || name) + " settings";
      var fields = document.getElementById("pm-fields");
      fields.textContent = "";
      fields.appendChild(tpl.content.cloneNode(true));
      modal.hidden = false; backdrop.hidden = false;
      modal.classList.add("show", "d-block");
      document.body.classList.add("modal-open");
      var first = fields.querySelector("input, select");
      if (first) first.focus();
      return;
    }
    if (ev.target.closest && ev.target.closest("[data-pm-close]")) { closeModal(); return; }
    if (modal && !modal.hidden && ev.target === modal) closeModal();  // a click on the dim area
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") {
      if (modal && !modal.hidden) { closeModal(); return; }
      closeMenu(true);
      return;
    }
    if (ev.key === "Tab" && modal && !modal.hidden) {  // keep Tab inside the dialog
      var items = modal.querySelectorAll("input, select, button:not([disabled])");
      if (!items.length) return;
      var firstItem = items[0], lastItem = items[items.length - 1];
      if (ev.shiftKey && document.activeElement === firstItem) { ev.preventDefault(); lastItem.focus(); }
      else if (!ev.shiftKey && document.activeElement === lastItem) { ev.preventDefault(); firstItem.focus(); }
    }
  });
  document.addEventListener("submit", function (ev) {
    if (!ev.target.closest || !ev.target.closest("#pm-form")) return;
    ev.preventDefault();
    var plugin = modalPlugin, config = {};
    document.querySelectorAll("#pm-fields [data-field-type]").forEach(function (el) {
      config[el.getAttribute("name")] = fieldValue(el);
    });
    aiSay("secondary", "saving " + plugin + "…");
    saveConfig(plugin, config).then(function (res) {
      if (failed(res)) { aiSay("danger", why(res, "could not save " + plugin)); return; }
      closeModal();
      aiSay("success", plugin + " settings saved to config.toml. Reload the plugin (⋮ menu) to apply them.");
    }).catch(function () { aiSay("danger", "core did not answer"); });
  });

  // --- Reference tab: the MCP exposure switch (same revert-on-failure idiom as the plugin switch) ---
  function exposeSay(kind, msg) {
    toast(document.getElementById("expose-result"), kind, msg);
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

  // --- Reference tab: which workspaces an MCP client may reach ([mcp] workspaces) ---
  function saveMcpWorkspaces(names, undo) {
    exposeSay("secondary", "saving MCP workspaces…");
    post("/stats/mcp-workspaces", { workspaces: names }).then(function (res) {
      if (!res.ok || (res.body && res.body.ok === false)) {
        exposeSay("danger", (res.body && (res.body.error || res.body.message)) || "could not save the workspaces");
        undo();
        return;
      }
      exposeSay("success", "MCP workspaces saved (config.toml written); core is reloading.");
    }).catch(function () { exposeSay("danger", "core did not answer"); undo(); });
  }
  document.addEventListener("change", function (ev) {
    var box = ev.target.closest && ev.target.closest("#mcp-workspaces input");
    if (!box) return;
    var boxes = document.querySelectorAll("#mcp-workspaces [data-mcp-workspace]");
    var all = document.getElementById("mcpw-all");
    var names = box === all
      ? (all.checked ? [] : ["default"])
      : Array.prototype.filter.call(boxes, function (b) { return b.checked; })
          .map(function (b) { return b.getAttribute("data-mcp-workspace"); });
    if (box !== all && names.length === 0) {  // nothing ticked would mean "all": make that explicit
      exposeSay("warning", "tick at least one workspace, or switch on All workspaces.");
      box.checked = true;
      return;
    }
    saveMcpWorkspaces(names, function () { box.checked = !box.checked; });
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
