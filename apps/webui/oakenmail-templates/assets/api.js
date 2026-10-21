/* OakenMail data seam — the single place the UI reaches the local core.
 *
 * The web UI is a thin client of the general API (see the architecture overview): every call is
 * one registry operation, POSTed to /v1/operations/{name}. This file is the ONLY place that knows
 * the wire format, so the render code in oaken.js never talks to the network directly.
 *
 * Demo mode: the published mockups under docs/demo carry `data-oaken-demo` on <html>. In that
 * mode Oaken.api() never touches the network — it answers from DEMO_FIXTURES below, so the demo
 * stays populated with dummy data and needs no running daemon. The real app has no marker and
 * talks to the daemon that served the page (same origin).
 *
 * Shapes here mirror the operation output models (MessageList / MessageItem, GetMessageOutput,
 * etc.) exactly, so the same rendering works against a live core and against the fixtures.
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
   * {confirm:true} or the proposal's token, exactly like the CLI. */
  async function api(name, input, opts) {
    if (DEMO) return demoCall(name, input || {}, opts || {});
    return callLive(name, input || {}, opts || {});
  }

  /* ---- demo fixtures ----
   * Dummy data for the published mockup. The rows mirror what the static HTML showed, so the demo
   * looks the same whether JS runs or not. Everything here is fake (example.com addresses only). */
  const FIX_MESSAGES = [
    { message_id: "m-vote-2463", account_id: "demo", thread_key: "t-vote", group: "Today",
      subject: "[VOTE] Release Example Server 2.4.63", from_name: "Sam Sample", from_addr: "sam@example.com",
      date: "9:12 AM", snippet: "Please vote on releasing this package. The vote is open for at least 72 hours…",
      labels: ["Releases"], tags: ["dev@lists"], is_read: false, starred: true, has_attachments: true },
    { message_id: "m-maint", account_id: "demo", thread_key: "t-maint", group: "Today",
      subject: "Planned maintenance: mail relays, Oct 22", from_name: "Infrastructure", from_addr: "infra@example.com",
      date: "8:47 AM", snippet: "We will be rotating the outbound relays on Thursday between 14:00 and 16:00 UTC…",
      labels: ["Infra"], tags: ["users@lists"], is_read: false, starred: false, has_attachments: false },
    { message_id: "m-keynote", account_id: "demo", thread_key: "t-keynote", group: "Today",
      subject: "Re: ExampleCon keynote draft", from_name: "Riley Brook", from_addr: "riley@example.com",
      date: "7:30 AM", snippet: "Loved the outline. One thought on the second section — could we lead with the community story…",
      labels: ["Personal"], tags: [], is_read: true, starred: false, has_attachments: true },
    { message_id: "m-board", account_id: "demo", thread_key: "t-board", group: "Yesterday",
      subject: "Board meeting agenda — October", from_name: "Board Secretary", from_addr: "secretary@example.com",
      date: "Mon", snippet: "Attached is the draft agenda. Reports are due by Friday. Please flag anything that needs a vote…",
      labels: ["Board"], tags: ["private@board"], is_read: false, starred: true, has_attachments: true },
    { message_id: "m-pr42", account_id: "demo", thread_key: "t-pr42", group: "Yesterday",
      subject: "[example/oaken-ui] PR #42: theme picker", from_name: "GitHub", from_addr: "notifications@example.com",
      date: "Mon", snippet: "dana requested your review on this pull request. 3 files changed…",
      labels: [], tags: [], is_read: true, starred: false, has_attachments: false },
    { message_id: "m-lunch", account_id: "demo", thread_key: "t-lunch", group: "Yesterday",
      subject: "Lunch Thursday?", from_name: "Dana Grey", from_addr: "dana@example.com",
      date: "Mon", snippet: "There's a new place near the office that does wood-fired everything. Thought of you…",
      labels: ["Personal"], tags: [], is_read: true, starred: true, has_attachments: false },
    { message_id: "m-jira", account_id: "demo", thread_key: "t-jira", group: "Earlier this week",
      subject: "[jira] Created: (EXAMPLE-1201) mod_proxy timeout regression", from_name: "Jira", from_addr: "jira@example.com",
      date: "Sun", snippet: "Priority: Major. Affects version 2.4.62. Reproduction steps attached…",
      labels: [], tags: ["dev@lists"], is_read: true, starred: false, has_attachments: true },
    { message_id: "m-trademark", account_id: "demo", thread_key: "t-trademark", group: "Earlier this week",
      subject: "Re: Trademark question from a downstream project", from_name: "Rory Stone", from_addr: "rory@example.com",
      date: "Sat", snippet: "Thanks for looping me in. I'll draft a reply this afternoon and send it over for review…",
      labels: ["Board"], tags: [], is_read: true, starred: false, has_attachments: false },
  ];

  const LABEL_COLORS = {
    Board: "var(--ok-feather-purple)", Infra: "var(--ok-primary)",
    Releases: "var(--ok-feather-orange)", Personal: "var(--ok-feather-amber)",
  };

  // A full message body for the reader (get_message / get_raw equivalents).
  const FIX_BODY = {
    "m-vote-2463": {
      message_id: "m-vote-2463", subject: "[VOTE] Release Example Server 2.4.63",
      from_name: "Sam Sample", from_addr: "sam@example.com", to: "dev@lists.example.com",
      date: "Tue, 20 Oct 2026 09:12 -0400", labels: ["Releases"], tags: ["dev@lists"],
      security: "Signed (DKIM: example.com) · TLS",
      html: "<p>Hi all,</p><p>Please vote on releasing this package as Example Server 2.4.63. The vote is open for at least the next 72 hours and passes if a majority of at least three +1 PMC votes are cast.</p><p>[ ] +1 Release this package<br>[ ] &nbsp;0 I don't feel strongly about it<br>[ ] -1 Do not release this package because…</p><p class=\"ok-quote\">The tarballs, signatures and checksums are staged at the usual dist/dev location. The tag is 2.4.63.</p><p>Thanks,<br>Sander</p>",
      attachments: [
        { filename: "example-server-2.4.63.tar.gz", size: "9.4 MB", icon: "bi-file-earmark-zip" },
        { filename: "example-server-2.4.63.tar.gz.asc", size: "833 B", icon: "bi-file-earmark-lock" },
      ],
      raw: "Return-Path: <sam@example.com>\nReceived: from mx1.example.com ...\nDKIM-Signature: v=1; a=rsa-sha256; d=example.com; ...\nFrom: Sam Sample <sam@example.com>\nTo: dev@lists.example.com\nSubject: [VOTE] Release Example Server 2.4.63\nDate: Tue, 20 Oct 2026 09:12:00 -0400\nMessage-ID: <abc123@example.com>\nList-Id: <dev.lists.example.com>",
    },
  };

  const FIX_ACCOUNTS = [
    { account_id: "demo", display_name: "Alex Example", address: "alex@example.com", provider: "gmail" },
  ];

  // Demo dispatch: answers each operation from the fixtures, shaped like the real output.
  async function demoCall(name, input, opts) {
    await sleep(40); // a touch of latency so loading states are visible
    switch (name) {
      case "list_messages":
      case "search_keyword":
      case "search": {
        let items = FIX_MESSAGES.slice();
        const q = (input.query || "").trim().toLowerCase();
        if (q) items = items.filter((m) =>
          (m.subject + " " + m.from_name + " " + m.snippet).toLowerCase().includes(q));
        if (input.unread_only) items = items.filter((m) => m.is_read === false);
        if (input.label) items = items.filter((m) => (m.labels || []).includes(input.label));
        return { request_id: "demo", status: "ok", output: { items, truncated: false, warnings: [], degraded: false } };
      }
      case "get_message": {
        const body = FIX_BODY[input.message_id] || FIX_BODY["m-vote-2463"];
        return { request_id: "demo", status: "ok", output: body };
      }
      case "get_raw": {
        const body = FIX_BODY[input.message_id] || FIX_BODY["m-vote-2463"];
        return { request_id: "demo", status: "ok", output: { message_id: body.message_id, headers: body.raw, size_raw: body.raw.length } };
      }
      case "list_accounts":
        return { request_id: "demo", status: "ok", output: { accounts: FIX_ACCOUNTS } };
      case "list_labels":
        return { request_id: "demo", status: "ok", output: { labels: Object.keys(LABEL_COLORS) } };
      // Mutations: the demo just acknowledges, changing nothing on a server.
      case "mark_read": case "mark_unread": case "sync_now":
      case "summarize_message": case "undo":
        return { request_id: "demo", status: "ok", output: { ok: true, ids: input.ids || [] } };
      default:
        return { request_id: "demo", status: "ok", output: {} };
    }
  }

  function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

  window.Oaken = window.Oaken || {};
  window.Oaken.api = api;
  window.Oaken.ApiError = ApiError;
  window.Oaken.isDemo = DEMO;
  window.Oaken.labelColor = (name) => LABEL_COLORS[name] || "var(--ok-primary)";
})();
