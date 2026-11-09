/*
 * Config page wiring.
 *
 * This template is a thin client. Each section carries:
 *   data-config-section="plugins.gmail"   the config.toml table path
 *   data-config-toml                        the TOML textarea
 *   data-config-enabled                     (plugins) the Enabled switch
 *   data-config-caps                        (plugins) capability checkboxes
 *   data-config-action="test|save"          the two buttons
 *
 * The server operations (see docs/prompts/23-plugin-settings.md):
 *   Test        -> check_plugin_config(name, toml)   / check_section(path, toml)  [persists nothing]
 *   Apply&Save  -> set_plugin_config(name, toml, enabled?, capabilities?) / write_section(path, toml)
 *
 * Wire these to window.Oaken.api when the API bridge is available. Until then the handlers
 * run a light client-side TOML sanity check so the page is usable as a static demo.
 */
(function () {
  "use strict";

  // Minimal, forgiving TOML sanity check for the demo: flags unbalanced quotes/brackets and
  // obviously malformed key = value lines. Real validation happens server-side against the schema.
  function sanityCheck(text) {
    const errors = [];
    const lines = text.split("\n");
    for (let i = 0; i < lines.length; i++) {
      const raw = lines[i];
      const line = raw.trim();
      if (!line || line.startsWith("#") || line.startsWith("[")) continue;
      if (!line.includes("=")) {
        errors.push({ line: i + 1, message: "expected 'key = value'" });
        continue;
      }
      const quotes = (line.match(/"/g) || []).length;
      if (quotes % 2 !== 0) {
        errors.push({ line: i + 1, message: "unbalanced quotes" });
      }
      const open = (line.match(/\[/g) || []).length;
      const close = (line.match(/\]/g) || []).length;
      if (open !== close) {
        errors.push({ line: i + 1, message: "unbalanced brackets" });
      }
    }
    return errors;
  }

  function setStatus(el, kind, msg) {
    el.className = "ok-config-status ms-2";
    if (kind) el.classList.add("is-" + kind);
    el.textContent = msg;
  }

  function sectionPayload(section) {
    const name = section.getAttribute("data-config-section");
    const toml = section.querySelector("[data-config-toml]").value;
    const enabledEl = section.querySelector("[data-config-enabled]");
    const payload = { name: name, toml: toml };
    if (enabledEl) payload.enabled = enabledEl.checked;
    const caps = section.querySelectorAll("[data-config-caps] input[type=checkbox]");
    if (caps.length) {
      payload.capabilities = Array.from(caps)
        .filter((c) => c.checked)
        .map((c) => c.nextElementSibling.textContent.trim());
    }
    return payload;
  }

  async function runTest(section, statusEl) {
    const { name, toml } = sectionPayload(section);
    setStatus(statusEl, "", "Testing…");
    const errors = sanityCheck(toml);
    if (errors.length) {
      setStatus(statusEl, "err", "Line " + errors[0].line + ": " + errors[0].message);
      return false;
    }
    // Prefer the real API if the bridge is present.
    const api = window.Oaken && window.Oaken.api;
    if (api && api.checkConfig) {
      try {
        const res = await api.checkConfig(name, toml);
        if (!res.valid) {
          const e = (res.errors && res.errors[0]) || {};
          setStatus(statusEl, "err", (e.line ? "Line " + e.line + ": " : "") + (e.message || "invalid"));
          return false;
        }
        if (res.health && res.health !== "healthy") {
          setStatus(statusEl, "warn", "Valid, but health probe: " + res.health);
          return true;
        }
        setStatus(statusEl, "ok", "Valid" + (res.probed ? " · health OK" : ""));
        return true;
      } catch (err) {
        setStatus(statusEl, "err", "Test failed: " + (err && err.message ? err.message : err));
        return false;
      }
    }
    setStatus(statusEl, "ok", "Looks valid (client-side check only)");
    return true;
  }

  async function runSave(section, statusEl) {
    const payload = sectionPayload(section);
    const ok = await runTest(section, statusEl);
    if (!ok) return;
    setStatus(statusEl, "", "Saving…");
    const api = window.Oaken && window.Oaken.api;
    if (api && api.setConfig) {
      try {
        await api.setConfig(payload);
        setStatus(statusEl, "ok", "Saved · applied to running daemon");
      } catch (err) {
        setStatus(statusEl, "err", "Save failed: " + (err && err.message ? err.message : err));
      }
      return;
    }
    setStatus(statusEl, "ok", "Saved (demo: not persisted — API bridge not connected)");
  }

  document.addEventListener("click", function (ev) {
    const btn = ev.target.closest("[data-config-action]");
    if (!btn) return;
    const action = btn.getAttribute("data-config-action");
    if (action === "reload-all") {
      document.querySelectorAll("[data-config-section]").forEach(function (s) {
        const st = s.querySelector("[data-config-status]");
        if (st) setStatus(st, "", "");
      });
      return;
    }
    const section = btn.closest("[data-config-section]");
    if (!section) return;
    const statusEl = section.querySelector("[data-config-status]");
    if (action === "test") runTest(section, statusEl);
    else if (action === "save") runSave(section, statusEl);
  });
})();
