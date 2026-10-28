// SPDX-License-Identifier: Apache-2.0
// Real-time pipeline throughput chart for the jobs trends page.
// Tracks per_second for each stage over the last ~10 min.
(function () {
  "use strict";
  var canvas = document.getElementById("throughput-chart");
  if (!canvas || typeof Chart === "undefined") return;

  var ctx = canvas.getContext("2d");
  var history = {};  // stage -> array of {time, throughput}
  var maxPoints = 40; // ~10 min at 15-sec intervals
  var chart = null;

  var colors = {
    parse: "#8b7355",
    embed: "#527e61",
    enrich: "#c6792e",
    index: "#5a7a9d",
    reprocess: "#9a7a6d"
  };

  function initChart() {
    var ctx = canvas.getContext("2d");
    chart = new Chart(ctx, {
      type: "line",
      data: { labels: [], datasets: [] },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: true, position: "top" } },
        scales: {
          y: { beginAtZero: true, title: { display: true, text: "items/sec" } },
          x: { title: { display: true, text: "time" } }
        },
        animation: { duration: 0 }
      }
    });
  }

  function updateChart() {
    if (!chart) return;
    var now = new Date();
    var labels = [];
    for (var i = 0; i < maxPoints; i++) {
      labels.push("");
    }
    if (labels.length > 0) {
      labels[labels.length - 1] = now.toLocaleTimeString();
    }

    var datasets = [];
    Object.keys(history).sort().forEach(function (stage) {
      var data = history[stage].map(function (p) { return p.throughput; });
      datasets.push({
        label: stage,
        data: data,
        borderColor: colors[stage] || "#999",
        backgroundColor: "transparent",
        borderWidth: 2,
        tension: 0.2,
        pointRadius: 2,
        pointHoverRadius: 4
      });
    });

    chart.data.labels = labels;
    chart.data.datasets = datasets;
    chart.update("none");
  }

  function tick() {
    var url = document.body.getAttribute("data-refresh");
    if (!url) return;

    fetch(url, { headers: { Accept: "application/json" }, cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data || !data.stages) return;

        var now = Date.now();
        Object.keys(data.stages).forEach(function (stage) {
          var s = data.stages[stage];
          if (!history[stage]) history[stage] = [];
          history[stage].push({ time: now, throughput: s.per_second || 0 });
          if (history[stage].length > maxPoints) {
            history[stage].shift();
          }
        });

        if (!chart) initChart();
        updateChart();
      })
      .catch(function () { /* silently ignore fetch errors */ });
  }

  // tick on the same interval as the page refresh (default 10s for jobs)
  var every = (parseInt(document.body.getAttribute("data-refresh-seconds") || "10", 10) * 1000);
  tick(); // initial
  setInterval(tick, every);
})();
