/* OakenMail demo fixtures — dummy data for the PUBLISHED MOCKUP ONLY.
 *
 * This file is loaded by api.js solely in demo mode (html[data-oaken-demo]), which only the
 * published mockups under docs/demo carry. A real oaken-webui serves the same pages with that
 * marker stripped, so this script is never loaded there and no dummy data ships in the live app.
 *
 * It exposes window.OakenDemoFixtures.demoCall(name, input, opts), which answers each registry
 * operation from the fake data below, shaped exactly like the real operation output
 * (MessageList / MessageItem, GetMessageOutput, …) so the same render code works unchanged.
 *
 * Everything here is fake: example.com addresses only, no real mail.
 */
(function () {
  const FIX_MESSAGES = [
    { message_id: "m-vote-2463", account_id: "demo", thread_key: "t-vote", group: "Today",
      subject: "[VOTE] Release Example Server 2.4.63", from_name: "Sam Sample", from_addr: "sam@example.com",
      date: "9:12 AM", snippet: "Please vote on releasing this package. The vote is open for at least 72 hours…",
      labels: ["Releases"], tags: ["dev@lists"], is_read: false, starred: true, has_attachments: true },
    { message_id: "m-vote-reply", account_id: "demo", thread_key: "t-vote", group: "Today",
      subject: "Re: [VOTE] Release Example Server 2.4.63", from_name: "Dana Grey", from_addr: "dana@example.com",
      date: "9:40 AM", snippet: "+1 (binding). Checked the signatures and ran the test suite on two platforms…",
      labels: ["Releases"], tags: ["dev@lists"], is_read: false, starred: false, has_attachments: false },
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

  const FIX_BODY = {
    "m-vote-reply": {
      message_id: "m-vote-reply", thread_key: "t-vote", subject: "Re: [VOTE] Release Example Server 2.4.63",
      from_name: "Dana Grey", from_addr: "dana@example.com", to: "dev@lists.example.com",
      date: "Tue, 20 Oct 2026 09:40 -0400", labels: ["Releases"], tags: ["dev@lists"], text: "+1 (binding)\n\nChecked the signatures and ran the test suite on two platforms.", attachments: [],
    },
    "m-vote-2463": {
      message_id: "m-vote-2463", thread_key: "t-vote", subject: "[VOTE] Release Example Server 2.4.63",
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

  function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

  // Answers an operation from the fixtures, shaped like the real output.
  async function demoCall(name, input, _opts) {
    await sleep(40); // a touch of latency so loading states are visible
    switch (name) {
      case "list_messages":
      case "search_keyword":
      case "search": {
        let items = FIX_MESSAGES.slice();
        const q = (input.query || "").trim().toLowerCase();
        if (q) items = items.filter((m) =>
          (m.subject + " " + m.from_name + " " + m.snippet).toLowerCase().includes(q));
        if (input.thread_key) items = items.filter((m) => m.thread_key === input.thread_key);
        if (input.unread_only) items = items.filter((m) => m.is_read === false);
        if (input.label) items = items.filter((m) => (m.labels || []).includes(input.label));
        if (input.location) items = items.filter((m) => (m.tags || []).includes(input.location));
        if (input.disposition === "inbox") items = items.filter((m) => !(m.tags || []).length);
        const total = items.length, off = input.offset || 0, lim = input.limit || 50;
        items = items.slice(off, off + lim);
        return { request_id: "demo", status: "ok", output: { items, truncated: off + lim < total, total, total_exact: true, offset: off, warnings: [], degraded: false } };
      }
      case "get_thread": {
        const m = FIX_MESSAGES.find((x) => x.message_id === input.message_id);
        const items = FIX_MESSAGES.filter((x) => m && x.thread_key === m.thread_key)
          .map((x, i) => ({ ...x, parent_id: i ? FIX_MESSAGES.find((y) => y.thread_key === m.thread_key).message_id : null, depth: i ? 1 : 0 }));
        return { request_id: "demo", status: "ok", output: { thread_key: m ? m.thread_key : "", items, total: items.length, truncated: false } };
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
      case "list_labels": {
        // Folders (locations) come from the mailing-list "tags"; labels are the search tags.
        const locations = {}, labels = {};
        let inbox = 0;
        for (const m of FIX_MESSAGES) {
          (m.tags || []).forEach((t) => { locations[t] = (locations[t] || 0) + 1; });
          (m.labels || []).forEach((l) => { labels[l] = (labels[l] || 0) + 1; });
          if (!(m.tags || []).length) inbox += 1;
        }
        return {
          request_id: "demo", status: "ok",
          output: { labels, locations, dispositions: { inbox, filed: FIX_MESSAGES.length - inbox } },
        };
      }
      case "list_mailbox": {
        // The sidebar structure: mailing-list tags are the demo's folders; labels are search tags.
        const folders = {}, labels = {};
        let inbox = 0;
        for (const m of FIX_MESSAGES) {
          (m.tags || []).forEach((t) => { folders[t] = (folders[t] || 0) + 1; });
          (m.labels || []).forEach((l) => { labels[l] = (labels[l] || 0) + 1; });
          if (!(m.tags || []).length) inbox += 1;
        }
        const c = (n) => ({ total: n, unread: n });
        return {
          request_id: "demo", status: "ok",
          output: {
            inbox: c(inbox), discarded: c(0),
            folders: Object.keys(folders).sort().map((p) => ({ path: p, name: p, depth: 0, direct: c(folders[p]), tree: c(folders[p]) })),
            labels: Object.keys(labels).sort().map((n) => ({ name: n, count: c(labels[n]) })),
          },
        };
      }
      case "create_folder":
        return { request_id: "demo", status: "ok", output: { path: input.path || "", created: [input.path || ""] } };
      case "create_label":
        return { request_id: "demo", status: "ok", output: { name: input.name || "", created: true } };
      case "move_to_folder":
        return { request_id: "demo", status: "ok", output: { path: input.path || null, moved: input.ids || [] } };
      case "set_labels":
        return { request_id: "demo", status: "ok", output: { changed: input.ids || [] } };
      // Mutations: the demo just acknowledges, changing nothing on a server.
      case "mark_read": case "mark_unread": case "sync_now":
      case "summarize_message": case "undo":
        return { request_id: "demo", status: "ok", output: { ok: true, ids: input.ids || [] } };
      default:
        return { request_id: "demo", status: "ok", output: {} };
    }
  }

  window.OakenDemoFixtures = { demoCall, messages: FIX_MESSAGES, bodies: FIX_BODY, accounts: FIX_ACCOUNTS };
})();
