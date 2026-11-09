/* Settings: every tab below reads core through the adapter. Nothing here is example data. */
(function () {
  "use strict";
  const O = window.Oaken || {}, api = O.api, toast = O.toast || (() => {});
  if (!api) return;
  const $ = (s, r) => (r || document).querySelector(s), $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const bytes = (n) => (n >= 1e9 ? (n / 1e9).toFixed(1) + " GB" : n >= 1e6 ? (n / 1e6).toFixed(1) + " MB" : n >= 1e3 ? Math.round(n / 1e3) + " KB" : n + " B");
  const when = (t) => { const d = new Date(t); return isNaN(d) ? esc(t || "") : d.toLocaleString(); };
  const every = (s) => (s % 86400 === 0 ? s / 86400 + " d" : s % 3600 === 0 ? s / 3600 + " h" : s % 60 === 0 ? s / 60 + " min" : s + " s");
  const tile = (v, l) => `<div class="col-6 col-md-3"><div class="ok-metric"><div class="v">${esc(v)}</div><div class="l">${esc(l)}</div></div></div>`;
  const failed = (box, e) => { box.innerHTML = `<div class="text-danger small">Could not load: ${esc((e && e.message) || e)}</div>`; };
  async function call(name, input) {
    try { return (await api(name, input || {}, { confirm: true })).output || {}; }
    catch (e) { toast((e && e.message) || "That did not work."); return null; }
  }

  const loaders = {
    async identities(box) {
      const { senders = [] } = (await api("list_senders", {})).output || {};
      box.innerHTML = senders.length ? `<div class="list-group">${senders.map((s) =>
        `<div class="list-group-item d-flex align-items-center"><div class="fw-semibold">${esc(s.account)}</div><span class="ok-tag ms-2">${esc(s.provider)}</span></div>`).join("")}</div>`
        : '<div class="text-muted small">No account can send yet.</div>';
    },

    async schedule(box) {
      const { schedules = [] } = (await api("list_schedules", {})).output || {};
      box.innerHTML = schedules.length ? `<div class="table-responsive"><table class="table table-sm align-middle"><thead><tr><th>Name</th><th>Runs</th><th>Queue</th><th>Next</th><th></th></tr></thead><tbody>` +
        schedules.map((s) => `<tr><td><code>${esc(s.name)}</code></td><td>${s.every_seconds ? "every " + every(s.every_seconds) : "cron <code>" + esc(s.cron) + "</code> (UTC)"}</td>` +
          `<td>${esc(s.queue)}</td><td class="text-nowrap">${when(s.next_run)}</td><td class="text-end"><button class="btn btn-sm btn-outline-oaken" data-run="${esc(s.name)}">Run now</button></td></tr>`).join("") +
        "</tbody></table></div>" : '<div class="text-muted small">Nothing is scheduled.</div>';
    },

    async ai(box) {
      const { usage, cache } = (await api("get_ai", {})).output || {};
      const tiles = [];
      if (usage) tiles.push(tile(usage.calls.toLocaleString(), "model calls"), tile(usage.tokens_spent.toLocaleString(), "tokens spent"),
        tile(usage.tokens_saved.toLocaleString(), "tokens saved"), tile(usage.answered_without_a_model.toLocaleString(), "answered without a model"));
      tiles.push(tile(cache.entries.toLocaleString(), "cached answers"), tile(bytes(cache.bytes), "cache size"));
      const rows = ((usage && usage.rows) || []).map((r) => `<tr><td>${esc(r.analyzer)}</td><td>${esc(r.model)}</td><td>${esc(r.backend)}</td><td class="text-end">${r.calls}</td>` +
        `<td class="text-end">${r.tokens_in.toLocaleString()} / ${r.tokens_out.toLocaleString()}</td><td class="text-end">${r.cache_hits + r.shortcut_hits}</td></tr>`).join("");
      box.innerHTML = `<div class="row g-3 mb-3">${tiles.join("")}</div>` + (usage ? "" : '<p class="text-muted small">No model router is running in this stack.</p>') +
        (rows ? `<div class="table-responsive"><table class="table table-sm"><thead><tr><th>Analyzer</th><th>Model</th><th>Backend</th><th class="text-end">Calls</th><th class="text-end">Tokens in / out</th><th class="text-end">No-model answers</th></tr></thead><tbody>${rows}</tbody></table></div>` : "") +
        `<button class="btn btn-sm btn-outline-danger" data-clear-cache ${cache.entries ? "" : "disabled"}>Clear the answer cache</button>
         <span class="small text-muted ms-2">Answers are recomputed on demand, at the cost of the model calls the cache saved.</span>`;
    },

    async agents(box) {
      const { operations = [] } = (await api("get_exposure", {})).output || {};
      const groups = {}; operations.forEach((o) => (groups[o.group || "other"] = groups[o.group || "other"] || []).push(o));
      const row = (o) => `<tr class="${o.allowed ? "" : "text-muted"}"><td class="text-nowrap"><div class="form-check form-switch m-0"><input class="form-check-input" type="checkbox" data-expose="${esc(o.name)}" ${o.exposed ? "checked" : ""} ${o.allowed ? "" : "disabled"} aria-label="${esc(o.name)}"></div></td>` +
        `<td><code>${esc(o.name)}</code>${o.allowed ? "" : ' <i class="bi bi-lock-fill" title="Closed in code"></i>'}</td><td><span class="badge text-bg-${o.permission === "read" ? "secondary" : "warning"}">${esc(o.permission)}</span></td><td class="small">${esc(o.summary)}</td></tr>`;
      const on = operations.filter((o) => o.exposed).length;
      box.innerHTML = `<p class="small mb-3"><strong>${on}</strong> of ${operations.length} operations are open to agents.</p>` + Object.keys(groups).sort().map((g) =>
        `<h3 class="h6 mt-3">${esc(g)}</h3><div class="table-responsive"><table class="table table-sm align-middle"><tbody>${groups[g].map(row).join("")}</tbody></table></div>`).join("");
    },

    async plugins(box) {
      const { plugins = [] } = (await api("list_all_plugins", {})).output || {};
      const badge = (p) => !p.enabled ? '<span class="badge text-bg-secondary">off</span>' : p.started && p.healthy ? '<span class="badge text-bg-success">running</span>'
        : `<span class="badge text-bg-warning">${p.started ? "unhealthy" : "not running"}</span>`;
      box.innerHTML = `<div class="table-responsive"><table class="table table-sm align-middle"><thead><tr><th>Plugin</th><th>Version</th><th>State</th><th></th></tr></thead><tbody>` +
        plugins.map((p) => `<tr data-plugin="${esc(p.id)}"><td><code>${esc(p.id)}</code><div class="small text-muted">${esc(p.provider)}${p.warning ? ` · <span class="text-danger">${esc(p.warning)}</span>` : ""}${p.hint ? ` ${esc(p.hint)}` : ""}</div></td>` +
          `<td>${esc(p.version)}</td><td>${badge(p)}</td><td class="text-end text-nowrap">` + (p.switchable ?
            `<button class="btn btn-sm btn-ghost" data-reload title="Reload it"><i class="bi bi-arrow-repeat"></i></button>
             <div class="form-check form-switch d-inline-block align-middle m-0 ms-2"><input class="form-check-input" type="checkbox" role="switch" data-switch ${p.enabled ? "checked" : ""} aria-label="Switch ${esc(p.id)}"></div>` : "") +
          "</td></tr>").join("") + "</tbody></table></div>";
    },

    async storage(box) {
      const s = (await api("get_storage", {})).output || {};
      box.innerHTML = `<div class="row g-3 mb-3">${tile(bytes(s.core_db_bytes), "core database")}${tile(bytes(s.wal_bytes), "write-ahead log")}</div>
        <p class="small text-muted">Data lives in <code>${esc(s.data_dir)}</code>.</p>` +
        ((s.files || []).length ? `<table class="table table-sm"><thead><tr><th>Other databases</th><th class="text-end">Size</th></tr></thead><tbody>` +
          s.files.map((f) => `<tr><td><code>${esc(f.name || f.path)}</code></td><td class="text-end">${bytes(f.bytes)}</td></tr>`).join("") + "</tbody></table>" : "") +
        `<div class="d-flex align-items-center mt-3"><div><div class="fw-semibold">Vacuum the database</div><div class="small text-muted">Gives back the space deleted rows left. It holds the write lock while it works, so writes wait, and it needs about the database's size free on disk.</div></div>
          <button class="btn btn-sm btn-outline-oaken ms-auto" data-vacuum>Vacuum</button></div>`;
    },

    async logs(box) {
      const kind = $("[data-log-kind]").value, level = $("[data-log-level]").value;
      if (kind === "events") {
        const { events = [] } = (await api("get_events", { limit: 200 })).output || {};
        box.innerHTML = events.length ? `<pre class="ok-log">${events.map((e) => `${esc(when(e.ts))}  ${esc(e.kind)}  ${esc(JSON.stringify(e.data))}`).reverse().join("\n")}</pre>` : '<div class="text-muted small">No events yet.</div>';
        return;
      }
      const out = (await api("get_logs", { level: level || undefined, limit: 300 })).output || {};
      const lines = out.lines || [];
      box.innerHTML = (out.kept_in === "off" ? '<div class="text-muted small">Logging to the table is off.</div>' : lines.length
        ? `<pre class="ok-log">${lines.map((l) => `${esc(l.ts)}  ${esc(l.level.toUpperCase().padEnd(7))} ${esc(l.plugin || l.logger)}  ${esc(l.event)}  ${esc(Object.keys(l.data || {}).length ? JSON.stringify(l.data) : "")}`).reverse().join("\n")}</pre>`
        : '<div class="text-muted small">Nothing at this level.</div>') + (out.path ? `<div class="small text-muted mt-2">File: <code>${esc(out.path)}</code></div>` : "");
    },
  };

  async function load(name) {
    const box = $(`[data-set="${name}"]`); if (!box) return;
    try { await loaders[name](box); } catch (e) { failed(box, e); }
  }

  document.addEventListener("click", async (ev) => {
    const t = ev.target;
    const reload = t.closest("[data-set-reload]"); if (reload) return load(reload.dataset.setReload);
    const run = t.closest("[data-run]");
    if (run) { if (await call("run_schedule", { name: run.dataset.run })) { toast("Started."); setTimeout(() => load("schedule"), 1500); } return; }
    if (t.closest("[data-clear-cache]")) {
      if (!confirm("Delete every cached model answer?")) return;
      const out = await call("clear_cache"); if (out) { toast(`${out.deleted} deleted.`); load("ai"); } return;
    }
    if (t.closest("[data-vacuum]")) {
      if (!confirm("Vacuum the database now? Writes wait while it runs.")) return;
      if (await call("run_vacuum")) toast("Vacuum queued."); return;
    }
    const rl = t.closest("[data-reload]");
    if (rl) { if (await call("plugin_reload", { plugin: rl.closest("[data-plugin]").dataset.plugin })) { toast("Reloaded."); load("plugins"); } }
  });
  document.addEventListener("change", async (ev) => {
    const sw = ev.target.closest("[data-switch]");
    if (sw) {
      const ok = await call("stage_plugin_toggle", { plugin: sw.closest("[data-plugin]").dataset.plugin, enabled: sw.checked });
      if (!ok) sw.checked = !sw.checked;
      load("plugins"); return;
    }
    const ex = ev.target.closest("[data-expose]");
    if (ex) { const ok = await call("set_exposure", { name: ex.dataset.expose, enabled: ex.checked }); if (!ok) ex.checked = !ex.checked; load("agents"); return; }
    if (ev.target.matches("[data-log-level],[data-log-kind]")) load("logs");
  });
  // a tab loads when it is shown, so Settings opens fast and each pane is current
  const map = { "#s-identities": "identities", "#s-schedule": "schedule", "#s-ai": "ai", "#s-plugins": "plugins", "#s-agents": "agents", "#s-storage": "storage", "#s-logs": "logs" };
  document.addEventListener("shown.bs.tab", (ev) => { const n = map[ev.target.getAttribute("data-bs-target")]; if (n) load(n); });
})();
