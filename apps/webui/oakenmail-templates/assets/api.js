/* OakenMail data seam — the single place the UI reaches the local core.
 *
 * The web UI is a thin client of the general API (see the architecture overview): every call is
 * one registry operation, POSTed to /v1/operations/{name}. This file is the ONLY place that knows
 * the wire format, so the render code in oaken.js never talks to the network directly.
 *
 * No dummy data lives here. A real oaken-webui serves these pages and every Oaken.api() call goes
 * to the daemon that served the page (same origin). The published mockups under docs/demo carry
 * `data-oaken-demo` on <html> and additionally load assets/demo-fixtures.js; in that case the
 * calls are answered from those fixtures instead of the network, so the demo needs no daemon. The
 * served app never loads the fixtures, so it shows real (or empty) data only.
 *
 * Shapes returned by the live API and by the demo fixtures match the operation output models
 * (MessageList / MessageItem, GetMessageOutput, …), so the same rendering works for both.
 */
(function () {
  const root = document.documentElement;
  const DEMO = root.hasAttribute("data-oaken-demo");

  /* ---- wire format (live core) ----
   * POST /v1/operations/{name}  body {input, confirm, confirmation_token}
   * -> {request_id, status, output, proposal}
   * Auth is same-origin for now (the daemon serves this page); a token scheme is a later concern
   * and lives only here when it arrives. */
  const BASE = "/v1/operations/";

  async function callLive(name, input, opts) {
    const body = {
      input: input || {},
      confirm: !!(opts && opts.confirm),
      confirmation_token: (opts && opts.token) || null,
    };
    const res = await fetch(BASE + encodeURIComponent(name), {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Oaken-Client": "webui", Accept: "application/json" },
      body: JSON.stringify(body),
    });
    let data = null;
    try { data = await res.json(); } catch { data = null; }
    if (!res.ok) {
      const msg = (data && (data.message || data.code)) || `core answered ${res.status}`;
      throw new ApiError(msg, res.status, data && data.code);
    }
    return data || {};
  }

  class ApiError extends Error {
    constructor(message, status, code) { super(message); this.name = "ApiError"; this.status = status; this.code = code; }
  }

  /* The one call the UI makes. Returns {output, proposal, request_id}. A mutation that is not
   * confirmed comes back with a `proposal` and changes nothing — the caller re-calls with
   * {confirm:true} or the proposal's token, exactly like the CLI.
   *
   * In demo mode the call is served by the fixtures module (assets/demo-fixtures.js), which is
   * loaded on demand the first time it is needed. It is never fetched by the served app, because
   * only the published mockups carry the data-oaken-demo marker. If the fixtures cannot be loaded
   * we fall through to the live path rather than fabricate data. */
  let _fixturesPromise = null;
  function loadFixtures() {
    if (window.OakenDemoFixtures) return Promise.resolve(window.OakenDemoFixtures);
    if (!_fixturesPromise) {
      _fixturesPromise = new Promise((resolve) => {
        const s = document.createElement("script");
        s.src = "assets/demo-fixtures.js";
        s.onload = () => resolve(window.OakenDemoFixtures || null);
        s.onerror = () => resolve(null);
        document.head.appendChild(s);
      });
    }
    return _fixturesPromise;
  }

  async function api(name, input, opts) {
    if (DEMO) {
      const fx = await loadFixtures();
      if (fx && typeof fx.demoCall === "function") {
        return fx.demoCall(name, input || {}, opts || {});
      }
    }
    return callLive(name, input || {}, opts || {});
  }

  /* ---- UI config (not mail data) ----
   * The label colour palette. This is presentation, not dummy content: it maps a label name to a
   * theme colour for the dot shown beside it. Real labels come from the API. */
  const LABEL_COLORS = {
    Board: "var(--ok-feather-purple)", Infra: "var(--ok-primary)",
    Releases: "var(--ok-feather-orange)", Personal: "var(--ok-feather-amber)",
  };

  window.Oaken = window.Oaken || {};
  window.Oaken.api = api;
  window.Oaken.ApiError = ApiError;
  window.Oaken.isDemo = DEMO;
  window.Oaken.labelColor = (name) => LABEL_COLORS[name] || "var(--ok-primary)";
})();
