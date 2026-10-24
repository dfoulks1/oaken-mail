// SPDX-License-Identifier: Apache-2.0
// The /jobs console script. Same-origin (script-src 'self', connect-src 'self'): it talks to
// core's loopback /jobs/* routes, which delegate to the real list_jobs / pipeline_status and the
// mutate ops (pause_backfill, resume_backfill, retry_parked, cancel_job, retry_job) with
// confirm=true. Loopback-only and token-free, like /stats and /config.
(function () {
  "use strict";

  // --- theme toggle (shared behavior/key with /stats and /config) ---
  var THEME_KEY = "oaken-theme";
  function themeLabel(t) { var e = document.getElementById("theme-label"); if (e) e.textContent = t === "dark" ? "Light" : "Dark"; }
  function setTheme(t) { document.documentElement.setAttribute("data-bs-theme", t); try { localStorage.setItem(THEME_KEY, t); } catch (e) {} themeLabel(t); }
  (function () {
    var s; try { s = localStorage.getItem(THEME_KEY); } catch (e) { s = null; }
    setTheme(s || document.documentElement.getAttribute("data-bs-theme") || "dark");
    var b = document.getElementById("theme-toggle");
    if (b) b.addEventListener("click", function () {
      setTheme(document.documentElement.getAttribute("data-bs-theme") === "dark" ? "light" : "dark");
    });
  })();

  var result = document.getElementById("result");
  var body = document.getElementById("jobs-body");

  function show(kind, msg) { result.className = "alert alert-" + kind; result.textContent = msg; }

  function getJSON(url) { return fetch(url, { headers: { Accept: "application/json" } }).then(function (r) { return r.json(); }); }
  function post(url, b) {
    return fetch(url, { method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" }, body: JSON.stringify(b || {}) }).then(function (r) { return r.json(); });
  }

  function esc(s) { var d = document.createElement("div"); d.textContent = s == null ? "" : String(s); return d.innerHTML; }

  function statusBadge(s) {
    var cls = { failed: "text-bg-danger", running: "text-bg-primary", queued: "text-bg-secondary",
                cancelled: "badge-oaken-warn", succeeded: "text-bg-success" }[s] || "text-bg-secondary";
    return '<span class="badge ' + cls + '">' + esc(s) + "</span>";
  }

  function renderJobs(jobs) {
    if (!jobs || !jobs.length) { body.innerHTML = '<tr><td colspan="6" class="text-secondary">No jobs.</td></tr>'; return; }
    body.innerHTML = jobs.map(function (j) {
      var canCancel = j.status === "queued" || j.status === "running";
      var canRetry = j.status === "failed" || j.status === "cancelled";
      var actions = "";
      if (canCancel) actions += '<button class="btn btn-sm btn-outline-danger me-1" data-cancel="' + esc(j.id) + '">Cancel</button>';
      if (canRetry) actions += '<button class="btn btn-sm btn-outline-secondary" data-retry="' + esc(j.id) + '">Retry</button>';
      return "<tr>" +
        '<td class="mono small">' + esc(j.id) + "</td>" +
        '<td class="mono small">' + esc(j.kind) + "</td>" +
        "<td>" + statusBadge(j.status) + "</td>" +
        "<td>" + esc(j.attempts) + "/" + esc(j.max_attempts) + "</td>" +
        '<td class="err text-danger small" title="' + esc(j.last_error) + '">' + esc(j.last_error || "") + "</td>" +
        '<td class="text-end">' + actions + "</td>" +
      "</tr>";
    }).join("");
  }

  function loadJobs() {
    var status = document.getElementById("status-filter").value;
    getJSON("/jobs/list" + (status ? "?status=" + encodeURIComponent(status) : ""))
      .then(function (d) { renderJobs(d.jobs); })
      .catch(function () { body.innerHTML = '<tr><td colspan="6" class="text-danger">Failed to load jobs.</td></tr>'; });
  }

  function loadPipeline() {
    getJSON("/jobs/pipeline").then(function (p) {
      if (!p || !p.stages) { document.getElementById("pipeline-state").textContent = "pipeline not running"; return; }
      var paused = p.llm_paused;
      document.getElementById("pipeline-state").innerHTML = paused
        ? '<span class="badge badge-oaken-warn">model paused</span>'
        : '<span class="badge text-bg-success">running</span>';
      renderStages(p.stages);
      document.getElementById("pipeline-summary").textContent =
        (p.docs || 0) + " docs · " + (p.vectors || 0) + " vectors · " + (p.quarantined_files || 0) + " quarantined";
    }).catch(function () {});
  }

  // Only the model-bound stages can be paused or limited; the others are listed for the numbers.
  var CONTROLLED = { embed: true, enrich: true };
  function esc(t) { var d = document.createElement("div"); d.textContent = String(t); return d.innerHTML; }

  function renderStages(stages) {
    var rows = Object.keys(stages).map(function (name) {
      var s = stages[name], ctl = CONTROLLED[name];
      var state = s.paused ? '<span class="badge badge-oaken-warn">paused</span>' : "";
      var limit = ctl
        ? '<input type="number" min="1" max="64" class="form-control form-control-sm d-inline-block" style="width:5rem" ' +
          'data-limit-input="' + esc(name) + '" value="' + esc(s.concurrency || "") + '" placeholder="default">' +
          ' <button class="btn btn-sm btn-outline-secondary" data-limit="' + esc(name) + '">Set</button>'
        : "";
      var toggle = !ctl ? "" : s.paused
        ? '<button class="btn btn-sm btn-outline-secondary" data-stage-resume="' + esc(name) + '"><i class="bi bi-play-fill"></i> Resume</button>'
        : '<button class="btn btn-sm btn-outline-secondary" data-stage-pause="' + esc(name) + '"><i class="bi bi-pause-fill"></i> Pause</button>';
      return "<tr><td>" + esc(name) + "</td><td>" + esc(s.pending || 0) + "</td><td>" + esc(s.running || 0) +
        "</td><td>" + esc(s.failed || 0) + "</td><td>" + esc((s.per_second || 0).toFixed(2)) + "</td><td>" + state +
        "</td><td>" + limit + "</td><td>" + toggle + "</td></tr>";
    });
    document.getElementById("stage-body").innerHTML = rows.join("");
  }

  document.getElementById("stage-body").addEventListener("click", function (e) {
    var t = e.target.closest("button");
    if (!t) return;
    var pause = t.getAttribute("data-stage-pause"), resume = t.getAttribute("data-stage-resume"), lim = t.getAttribute("data-limit");
    if (pause) act("/jobs/pause", { stage: pause }, pause + " paused.");
    if (resume) act("/jobs/resume", { stage: resume }, resume + " resumed.");
    if (lim) {
      var input = document.querySelector('[data-limit-input="' + lim + '"]');
      act("/jobs/limit", { stage: lim, concurrency: parseInt(input.value, 10) }, lim + " limit set.");
    }
  });

  function act(url, b, okMsg) {
    post(url, b).then(function (d) {
      show("success", okMsg);
      loadPipeline(); loadJobs();
    }).catch(function () { show("danger", "Action failed."); });
  }

  document.getElementById("btn-pause").addEventListener("click", function () {
    if (confirm("Pause the model-bound ingest stages (embed, enrich)?")) act("/jobs/pause", {}, "Pipeline paused.");
  });
  document.getElementById("btn-resume").addEventListener("click", function () { act("/jobs/resume", {}, "Pipeline resumed."); });
  document.getElementById("btn-retry-parked").addEventListener("click", function () {
    if (confirm("Re-queue every parked ingest job?")) act("/jobs/retry-parked", {}, "Parked jobs re-queued.");
  });
  document.getElementById("btn-refresh").addEventListener("click", function () { loadPipeline(); loadJobs(); });
  document.getElementById("status-filter").addEventListener("change", loadJobs);

  body.addEventListener("click", function (e) {
    var c = e.target.getAttribute("data-cancel");
    var r = e.target.getAttribute("data-retry");
    if (c && confirm("Cancel job " + c + "?")) act("/jobs/cancel", { job_id: c }, "Job cancelled.");
    if (r) act("/jobs/retry", { job_id: r }, "Job re-queued.");
  });

  loadPipeline(); loadJobs();
  setInterval(function () { loadPipeline(); loadJobs(); }, 15000);
})();
