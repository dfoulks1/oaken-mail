// SPDX-License-Identifier: Apache-2.0
// The console's one optional script. It refreshes the numbers on a page that asks for it, and does
// nothing else: every page is complete and correct without it, and every action is a plain form.
// A page opts in with <body data-refresh="/general.json">, and marks each live value with
// data-bind="path.in.the.json" (and optionally data-format="int", "fixed2" or "secs"). A page that
// paints first and fills its numbers after says so with <body data-lazy>.
(function () {
  "use strict";
  var body = document.body;

  // A <details class="fold" id="..."> remembers whether it was left open, per browser. Without the
  // script (or without storage) it simply starts closed.
  document.querySelectorAll("details.fold[id]").forEach(function (d) {
    var key = "oaken.fold." + d.id;
    try { if (localStorage.getItem(key) === "1") d.open = true; } catch (e) { /* no storage */ }
    d.addEventListener("toggle", function () {
      try { localStorage.setItem(key, d.open ? "1" : "0"); } catch (e) { /* no storage */ }
    });
  });

  var every = parseInt(body.getAttribute("data-refresh-seconds") || "15", 10) * 1000;

  // Live plugin tables (independent of the numbers refresh below). A <div data-plugin-tables=
  // "<kind>"> is replaced with freshly server-rendered HTML from /table_refresh, so enabling or
  // disabling a plugin (here, another tab, or the CLI) shows up without a reload, and the markup is
  // the server's own, never rebuilt in JS. Opt-in: a page with no such div fetches nothing.
  var back = window.location.pathname + window.location.search;
  function refreshTables() {
    document.querySelectorAll("[data-plugin-tables]").forEach(function (el) {
      var kind = el.getAttribute("data-plugin-tables");
      var url = "/table_refresh?kind=" + encodeURIComponent(kind) + "&back=" + encodeURIComponent(back);
      fetch(url, { headers: { Accept: "text/html" }, cache: "no-store" })
        .then(function (r) { return r.ok ? r.text() : null; })
        .then(function (html) {
          if (html == null) return;  // a missed refresh is harmless: the last table stands
          var open = el.querySelector("details.fold");
          var wasOpen = open && open.open;   // keep the inactive fold open if the user opened it
          el.innerHTML = html;
          var now = el.querySelector("details.fold");
          if (now && wasOpen) now.open = true;
        })
        .catch(function () { /* keep the last table on error */ });
    });
  }
  if (document.querySelector("[data-plugin-tables]")) {
    // the server already rendered the current state on load; poll only for a change made elsewhere.
    setInterval(refreshTables, every);
  }

  // Live running-jobs card on the LLM backends tab: swap in the server's own HTML.
  if (document.querySelector("[data-running-jobs]")) {
    setInterval(function () {
      fetch("/running_refresh", { headers: { Accept: "text/html" }, cache: "no-store" })
        .then(function (r) { return r.ok ? r.text() : null; })
        .then(function (html) {
          var el = document.querySelector("[data-running-jobs]");
          if (html == null || !el) return;
          el.innerHTML = html;
          var v = el.querySelector("[data-agents-value]");  // the plugin table's Agents cell
          if (v && v.textContent) document.querySelectorAll("[data-agents-live]").forEach(function (c) { c.textContent = v.textContent; });
        })
        .catch(function () { /* keep the last table */ });
    }, Math.min(every, 5000));
  }

  var url = body.getAttribute("data-refresh");
  if (!url) return;

  function pick(data, path) {
    return path.split(".").reduce(function (node, key) { return node == null ? node : node[key]; }, data);
  }
  function show(el, value) {
    if (value === null || value === undefined) return "—";
    var how = el.getAttribute("data-format");
    if (how === "int" && typeof value === "number") return Math.round(value).toLocaleString();
    if (how === "fixed2" && typeof value === "number") return value.toFixed(2);
    if (how === "secs" && typeof value === "number") return Math.round(value) + "s";
    return String(value);
  }
  function tick() {
    fetch(url, { headers: { Accept: "application/json" }, cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        document.querySelectorAll("[data-bind]").forEach(function (el) {
          el.textContent = show(el, pick(data, el.getAttribute("data-bind")));
        });
      })
      .catch(function () { /* a missed refresh is harmless: the last numbers stand */ });
  }
  // A page that paints its numbers later (<body data-lazy>) asks for them as soon as it loads.
  if (body.hasAttribute("data-lazy")) tick();
  setInterval(tick, every);
})();

// Prompt override: the unlock box enables its textarea; locking puts the built-in text back, and
// a disabled textarea is not submitted, so the default is never saved as an override.
document.addEventListener("change", function (ev) {
  var id = ev.target.getAttribute && ev.target.getAttribute("data-unlock");
  var ta = id && document.getElementById(id);
  if (!ta) return;
  ta.disabled = !ev.target.checked;
  if (!ev.target.checked) ta.value = ta.getAttribute("data-default");
});

// Select-all box in a table header ticks every job box in that table.
document.addEventListener("change", function (ev) {
  if (!ev.target.hasAttribute || !ev.target.hasAttribute("data-select-all")) return;
  ev.target.closest("table").querySelectorAll('tbody input[type="checkbox"]').forEach(function (c) {
    c.checked = ev.target.checked;
  });
});

// Job log: a [data-job-log] button fills the shared #job-log modal with the server's own HTML for
// that job (/job_log), fetched on click so the table carries no logs.
document.addEventListener("click", function (ev) {
  var btn = ev.target.closest && ev.target.closest("[data-job-log]");
  var body = document.getElementById("job-log-body");
  if (!btn || !body) return;
  body.textContent = "Loading…";
  fetch("/job_log?job_id=" + encodeURIComponent(btn.getAttribute("data-job-log")), { cache: "no-store" })
    .then(function (r) { return r.ok ? r.text() : "No log."; })
    .then(function (html) { body.innerHTML = html; })
    .catch(function () { body.textContent = "Could not load the log."; });
});
