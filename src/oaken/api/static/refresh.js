// SPDX-License-Identifier: Apache-2.0
// The console's one optional script. It refreshes the numbers on a page that asks for it, and does
// nothing else: every page is complete and correct without it, and every action is a plain form.
// A page opts in with <body data-refresh="/stats.json">, and marks each live value with
// data-bind="path.in.the.json" (and optionally data-format="int" or "fixed2").
(function () {
  "use strict";
  var body = document.body;
  var url = body.getAttribute("data-refresh");
  if (!url) return;
  var every = parseInt(body.getAttribute("data-refresh-seconds") || "15", 10) * 1000;

  function pick(data, path) {
    return path.split(".").reduce(function (node, key) { return node == null ? node : node[key]; }, data);
  }
  function show(el, value) {
    if (value === null || value === undefined) return "—";
    var how = el.getAttribute("data-format");
    if (how === "int" && typeof value === "number") return Math.round(value).toLocaleString();
    if (how === "fixed2" && typeof value === "number") return value.toFixed(2);
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
  setInterval(tick, every);
})();
