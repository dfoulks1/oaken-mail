# 21b — Web UI client foundation: function ⇄ API map and the JS it needs

> **Superseded (0.1.1): `oaken auth`.** The sign-in rows in the function map below are history.
> Sign-in is each provider's own command: `oaken gmail auth`, `oaken imap auth`, `oaken smtp auth`
> (`--account-id`, `--check`). A password is never an argument: it is read without echo, or from
> standard input with `--password-stdin`. The core operations `set_account_password`,
> `check_account`, `start_authorization` and `authorization_status` are gone; the provider's
> `<provider>.auth` and `<provider>.auth_status` replace them. See [35](CORE_35-onboarding-0-1-1.md).

**Read first:** `docs/prompts/00-context.md`, `docs/adr/0007-webui-is-a-separate-client.md`,
`docs/prompts/WEBUI_21-webui-server.md`. **Depends on:** 13, 16, 21. **Feeds:** 22–29.

## Goal

Make the browser UI in `apps/webui/oakenmail-templates/` a **working client of the real API**.
Prompt 21 built the server that serves and proxies it. The pages themselves are mockups: most
controls are inert, and the two JS files that exist (`api.js`, `oaken.js`) were written against
**fixture shapes that do not match the real operation outputs**. This prompt is the source of
record for three things:

1. **The map**: every UI function → the registry operation(s) it calls, the exact fields it reads,
   and where the operation is built (existing, or which prompt adds it).
2. **The contract gaps**: places where the UI assumes something the API does not return.
3. **The JS extensions**: the client modules that must exist, and how the pages wire to them.

It also **builds the shared client foundation** (section C) so 22–29 only add features, not
plumbing. It does not build new operations; those belong to 22–29 (section B tells you which).

## Decisions already taken (do not re-litigate)

These are the user's answers for this phase.

- **Mail actions in the live UI:** read, mark read/unread, **star** (maps to `\Flagged`
  write-back). **Archive, Spam, Delete, Move, Label-apply, Snooze, Print, Block sender are hidden**
  in the served UI (not greyed) until a later prompt gives them a provider write path. They stay in
  the templates, behind the capability gate in C5.
- **Compose and send are in scope** (prompt 28): full send through whichever provider has the
  `send` capability. How MCP and plugin/capability permissions are managed in the UI and
  `config.toml` is **an open discussion** (see "Open questions"); do not invent a policy here.
- **Reader body:** render core's cleaned `text` safely in the client. No `html` from core in this
  phase. Original HTML is only reachable through View source.
- **Sidebar:** `Inbox`, `Starred`, `Folders`, `Discarded`, `Labels`. Nothing else.
  Snoozed, Sent, Drafts, Archive, Spam, Trash are hidden. "New folder" and "New label" buttons are
  removed: folders come from Sieve (`location`, ADR 0008) and labels from Sieve/providers.
- **Thin client, one backend.** The browser calls `/v1/operations/{name}` through `oaken-webui`
  and nothing else (plus `GET /v1/events`, `GET /v1/operations`). Business logic never moves into
  JS. Mail text is untrusted data: inserted as text, never markup.

## A. The function map

Legend for "Status": **have** = operation exists and returns what the UI needs; **shape** = exists
but the UI reads fields it does not return (fixed by the client, or by a field added in the named
prompt); **new** = no operation yet (the named prompt builds it); **hide** = not shown in the live
UI this phase.

### A1. Shell (every page)

| UI function | Operation / call | Status |
|---|---|---|
| Brand, nav links, topbar | none | have |
| Search box (`/` focuses) → `search.html?q=` | none until the results page loads (A4) | have |
| Theme, density, layout toggles | none: `localStorage` (`oaken.theme/density/layout`) | have |
| Keyboard shortcuts modal; `c` `/` `?` | none. `j k r a e` are documented but **not implemented**: implement `j k` (list move) and `r a` (reply, reply all; need 28); `e` (archive) is hidden | shape |
| Avatar menu: name + address | `list_accounts` (first account's `display_name`; address from `get_account.config`) | shape |
| Avatar menu: **Sign out** | none: the API token is held by `oaken-webui` and there is no session | hide |
| Sidebar counts and entries | `mailbox_counts` | new (25) |
| Compose button / `c` | A7 | new (28) |
| **Sync split button** (new, in the list toolbar beside Refresh, and in the topbar on narrow screens): main button = all accounts; caret menu = pick one account | `list_accounts` to know the targets, then `sync_now {account_id}` for each enabled account, or for the one chosen in the menu (optionally `folder` for a per-folder IMAP schedule). Returns `{job_id, created}`: `created:false` means it joined a sync already queued or running, say "Already syncing". Progress and completion from SSE `sync.started` / `sync.progress` / `sync.finished`; on finish, refresh the list and `mailbox_counts`. See C10 | have (op) / new (client) |
| Sync status chip next to the button | `list_schedules` (`last_run`, `last_result`, `last_error`, `paused`, `problem`) refreshed on `sync.finished` and `sync.paused` | have |
| Live updates | `GET /v1/events` (SSE, `Last-Event-ID` replay, `gap` event) | have |

### A2. Login (`login.html`)

The whole page is **hide** in the live UI. Core is loopback + token; the web UI server holds the
token. `GET /` redirects to `index.html`. Username/password, "Keep me signed in", "Forgot?" and SSO
have no backing and are not built. (Open question Q1.)

### A3. Mailbox list (`index.html`)

| UI function | Operation | Input the UI sends | Reads from output | Status |
|---|---|---|---|---|
| Load list (Inbox) | `list_messages` | `disposition:"inbox"`, `limit`, `offset` | `items[]`, `truncated`, `warnings` | shape (see gaps G1–G4) |
| Folder entry click | `list_messages` | `location:<name>` | same | new filter (22) |
| Label entry click | `list_messages` | `label:<name>` | same | have |
| Starred | `list_messages` | `starred:true` | same | new filter (25) |
| Discarded | `list_messages` | `disposition:"discarded"` | same | have |
| Unread badge per entry | `mailbox_counts` | none | `{inbox, starred, discarded, locations{}, labels{}}` each `{total, unread}` | new (25) |
| "1–50 of 1,284", Newer/Older | `list_messages` | `offset`, `limit` | **`total`** | new (22: add `offset`, `total`) |
| Select-all, per-row checkbox | none (client state) | | | have |
| Row click (split layout) → reader | `get_message` then `message_presented` | `{message_id, include_text:true}` | G2 | shape |
| Mark as read (toolbar) | `mark_read` | `{ids}` + `confirm:true` | `changed, unchanged, missing, local_only` | have |
| Mark as unread (More menu) | `mark_unread` | `{ids}` + `confirm:true` | same | have |
| Star toggle (row, reader) | `set_flagged` | `{ids, flagged}` + `confirm:true` | `changed, local_only` | new (25) |
| Refresh | re-run the current list call + `mailbox_counts` (local only, does not contact the mail server) | | | have |
| **Sync** (pull fresh mail) | `sync_now` per account, see A1 and C10 | `{account_id}` + `confirm:true` | `job_id`, `created` | have (op) |
| Undo toast after mark | `undo {audit_id}` using `audit_ids[0]` from the mark output | | | have |
| Archive / Report spam / Delete / Move / Label apply | none | | | **hide** |
| Layout split/list | `localStorage` | | | have |

### A4. Search (`search.html`)

| UI function | Operation | Notes | Status |
|---|---|---|---|
| Results for `?q=` | `search {query, limit, offset, label?, location?, disposition?, unread_only?}` | Returns `degraded`/`degraded_reason`: show the existing toast | have |
| Folder select | folds into `location` filter | | new filter (22) |
| Time select (week/month/year) | client turns it into `after` | | have |
| Has attachment | `has_attachment:true` filter (or `has:attachment` in query) | | new (22) |
| Unread | `unread_only:true` | | have |
| Advanced: From / To / Subject / Has the words / Doesn't have / After / Before | the client **builds a query string**: `from:` (have), `to:` and `subject:` (new, 22), words, `-word` (have), `after:` `before:` (have). No second query language | shape |
| "Save as filter" | none. A saved search is a Sieve rule: hidden until 26's editor can seed from a query | **hide** |
| Ask a question (not in the mockup) | `ask {question}`; result: `answer`, `citations[]`, `confidence`, `insufficient_evidence` | new UI element (Q2) |

### A5. Reader (`message.html`, and the split reader on `index.html`)

| UI function | Operation | Status |
|---|---|---|
| Open | `get_message {message_id, include_text:true}` then `message_presented {message_id}` (core owns the delay and `auto_mark`) | shape (G2) |
| Leaving a message before the delay | `message_dismissed {message_id}` on selection change / unload | have (client never calls it today) |
| Subject, from, date, labels | `get_message` | have |
| `to` line | `get_message.to_addrs` | new field (22) |
| Folder tag | `get_message.location` | new field (22) |
| Security line ("Signed · TLS") | `get_enrichment {message_id, enricher:"inspector"}` rendered as text; absent if not enriched | shape (G5) |
| Body | `get_message.text`, rendered by the client's safe text renderer (C6) | shape |
| Summary panel (not in mockup) | `get_enrichment` (summary/triage/actions) and `summarize_message` | optional (Q2) |
| Attachments list | `get_message.attachments[]` metadata only; **no download** this phase (Q3) | shape (G6) |
| Thread (collapsible `ok-thread-msg`) | `search {thread_key}` (the filter exists) | have |
| View source | `get_raw {message_id}` → `headers` into the modal. Real id must come from the current message, not the static `data-oaken-source` | shape |
| Reply / Reply all / Forward | A7 (needs the message's `to_addrs`, `cc_addrs`, `message_id_header`, `references`) | new (28) |
| Archive / Move / Delete / Snooze / Print / Block | | **hide** |
| Pop-out | `window.open(message.html?id=…&popout=1)` | have |

### A6. Contacts (`contacts.html`) — prompt 27, derived

| UI function | Operation | Status |
|---|---|---|
| People list, filter box | `list_people {query, sort, limit, offset}` | new (27) |
| Person panel (name, address) | `get_person {address}` | new (27) |
| "Recent messages" | `list_messages {participant:<address>}` (27 adds the participant filter if `from` is not enough) | new filter (27) |
| Email button | compose with `to` prefilled | new (28) |
| New / Edit modal, Groups, PGP key, Notes | none (people are **derived**, nothing is stored) | **hide** |
| Group tag on each person | none | **hide** |

### A7. Compose (every page: float, modal, `compose.html`) — prompt 28

| UI function | Operation | Status |
|---|---|---|
| From dropdown | `list_identities` (accounts that have a send route; from address + display name) | new (28) |
| To/Cc/Bcc chips; recipient suggestions | `list_people {query}` for autocomplete | new (27) |
| Subject, body (plain text) | client state | have |
| Bold/Italic/Link/List/Quote, "Plain text" switch | **hide the formatting buttons**; send plain text only (Q4) | hide |
| Attach | `send_message.attachments` (base64, size-capped) | new (28) |
| **Send** | `send_message` (permission `send`) | new (28) |
| Send later / schedule | | **hide** |
| Sign (PGP) | | **hide** |
| "Draft saved" | none; see Q5 | hide |
| Discard | client only; confirm if the body is non-empty | have |

### A8. Settings (`settings.html`, rail panes)

| Pane | Function | Operation | Status |
|---|---|---|---|
| Appearance | theme, layout, density, reading toggles | `localStorage` for presentation. "Load remote images" is **hidden** (nothing loads remote content); "Group into conversations" and "keyboard shortcuts" stay local | have |
| Workspaces | | placeholder is already labelled TBD | hide |
| Accounts | list | `list_accounts` + `get_account` + `list_schedules` (status, last sync) | have |
| | Add account (Gmail / IMAP / SMTP) | `create_account`, then `start_authorization` + `authorization_status` (Gmail) or `set_account_password` (IMAP/SMTP), then `check_account` | have/new (24) |
| | Manage, receive-only switch | `get_account`, `set_account_config`; receive-only is new | new (24) |
| | Pause / Resume | `pause_schedule` / `resume_schedule {account_id}` | have |
| | Re-authenticate | `start_authorization` | have |
| | Remove | `remove_account` | new (24) |
| Identities | list/add/edit, signature | identities derive from accounts (A7); the signature is an account config key | new (28) |
| Schedule | defaults, quiet hours, table, Sync now, new schedule | `list_schedules`, `set_schedule {schedule,timezone,quiet,run_now}`, `pause/resume/remove_schedule`, `sync_now`. "Export crontab" = `list_schedules {crontab:true}`. The "Default interval/Max concurrent" rows are read-only core settings (29) | have |
| Sieve | list, editor, check, test, save, delete | `sieve_list/get/check/test/save/delete` | new (26) |
| Composing | default format, quoting, pop-up, wrap | `localStorage` | have |
| AI & Models | Ollama endpoint, models | plugin config for `ollama`: a TOML textarea (`get/check/set_plugin_config`), no schema form in this phase | new (23) |
| | token thrift toggles, top-k | **core settings** (29), not plugin config | new (29) |
| | enrichment cache numbers; Clear cache | `usage_summary` (calls/tokens/cache saved) for numbers; **Clear cache has no operation** | new (29) |
| Plugins | list + health + enable toggle | `list_plugins`, `plugin_health`; one section per plugin with Enabled switch (`set_plugin_config {enabled}`), a TOML textarea (Test/Save), write-only secret rows (`set/clear_plugin_secret`) | new (23) |
| MCP | which operations AI clients may call, `allow_send`, linked workspaces | the `[mcp]` section in a textarea via `get/check/set_settings("mcp")`; `list_operations`, `set_operation_exposure`; Default / Extended / Locked groups | new (32) |
| Workspaces | confidentiality levels | `list/get/check/set/remove_workspace` | new (31) |
| Storage & Data | metrics | `status`, `pipeline_status` (docs, chunks, quarantined_files); database sizes are **not** returned | shape (29) |
| | Rebuild index / Re-enrich | `rebuild_index`, `reprocess {stage:"enrich"}` (both streaming jobs) | have |
| | Vacuum, Open quarantine folder | none (a browser cannot open a local folder; show the path) | new (29) / hide |
| Logs | Audit log + Undo | `list_audit`, `undo {audit_id}` where `undoable` | have |
| | Events | `GET /v1/events` (SSE), rendered live | have |
| | Export | client-side download of the loaded rows | have |
| Security | 2FA, PGP keys, sessions | no backing in a loopback single-user app | **hide** |
| | **MCP / operation exposure** (not in the mockup) | `list_operations`, `set_operation_exposure`; this is the surface for the open plugin/permission discussion | new pane (Q6) |
| Save changes / Cancel | per-section Save buttons replace the one global button (each section calls its own op) | | shape |

## B. Contract gaps (what the UI reads that the API does not return)

Each gap is fixed in the named prompt, or in the client as stated. Do not paper over any of these
in `demo-fixtures.js`: the fixtures must be **corrected to the real shapes** (C1) so the published
demo stays an honest mockup of the real UI.

- **G1 — list items.** `MessageItem` has no `starred`, `location`, `to`, `tags` or `group`, and
  `date` is an ISO datetime (the UI expects a pre-formatted string). Fix: core adds `starred` and
  `location` (22); the client formats `date` and computes day groups ("Today", "Yesterday",
  "Earlier this week") from it. `tags` (the folder chip in the mockup) is **`location`**.
- **G2 — message body.** `GetMessageOutput` returns `text`, not `html`; it has no `to`, `cc`,
  `security`, `tags`, `starred`. `attachments[].size` is a byte count and has no `icon`. Fix: core
  adds `to_addrs`, `cc_addrs`, `starred`, `location`, `message_id_header`, `references` (22); the
  client derives `icon` from `content_type` and formats `size`.
- **G3 — `list_labels`** returns `{labels:{name:count}, dispositions:{name:count}}`. The fixture
  returns a plain list. Fix the fixture; the sidebar uses `mailbox_counts` (25) for numbers and
  `list_labels` only for the label set.
- **G4 — paging and filters.** `list_messages` and `search` take `limit` only: add `offset` and
  return `total`; add filters `location`, `starred`, `has_attachment`, `participant`; add query
  operators `to:`, `subject:`, `has:attachment`, `in:`/`location:` to the index parser (22, with 27
  for `participant`). The index parser change touches prompt 07's code: say so in the report.
- **G5 — security line.** There is no "signed / TLS" field. The header/MIME **inspector** enrichment
  exists; render a one-line summary from `get_enrichment`, or nothing. Do not invent a signature
  verdict.
- **G6 — attachments.** Core quarantines attachments and never opens them. There is no download
  operation. The reader lists name/type/size only.
- **G7 — the confirm gate.** `confirm:true` from the `ui` client is honoured (the person asked for
  this exact action), so a UI mutation never returns a `proposal`. The `res.proposal` branch in
  `mutate()` is dead for the UI today. Keep it as a defensive branch, but **bulk delete/destructive
  confirmation is the UI's own modal**, not the gate. (This matters for `send_message`: the UI
  shows the recipient summary before sending.)
- **G8 — caller class.** The browser sends `X-Oaken-Client: webui`; the proxy overwrites it with
  `ui`. Leave it. Remove the header from `api.js` to avoid implying it is trusted.
- **G9 — `?folder=` and `?label=`.** The sidebar emits `?folder=inbox|star|...` and `?label=board`
  (lowercase, an id-style slug), but `loadList` only reads `label`, and labels are case-sensitive
  names. Replace the slugs with real names from the API (C4).
- **G10 — `view source`** uses a static `data-oaken-source` id. It must read the open message's id.
- **G11 — `api.js` demo loader** only fetches `assets/demo-fixtures.js` when `data-oaken-demo` is
  present, and the server strips that marker. Keep this exactly as is.

## C. The JS extensions (what must exist)

All plain JS, no build step and no framework (softmap: framework choice is deferred). Files live in
`apps/webui/oakenmail-templates/assets/`, are listed by every page, and are packaged unchanged.
Structure `oaken.js` into small modules attached to `window.Oaken` rather than adding to the
existing IIFEs. Where this prompt names a function, keep that name so 22–29 can refer to it.

### C1. `api.js` — the data seam (extend, don't fork)

- `Oaken.api(name, input, {confirm, token})` stays the only network call for operations. Add:
  - `Oaken.mutate(name, input, {ok, undo})` — wraps `api(..., {confirm:true})`, toasts the outcome,
    handles `res.proposal` defensively, surfaces `ApiError.code`, and, for `mark_*`/`set_flagged`,
    offers an **Undo** toast calling `undo {audit_id}`.
  - `Oaken.events(handlers)` — one shared `EventSource("/v1/events")`; reconnect with the browser's
    `Last-Event-ID`; on a `gap` event call `handlers.resync()`; dispatch by event `name`
    (`message.indexed`, `message.read_changed`, `sync.finished`, `plugin.health_changed`, …;
    use the names in `src/oaken/core/events.py`). Close on `pagehide`.
  - `Oaken.ops()` — cached `GET /v1/operations` so the UI can ask "does this operation exist and is
    it on the general surface?" (feeds C5).
  - Normalise errors: `ApiError.code` ∈ the wire codes in `src/oaken/api/errors.py`
    (`core_unavailable` 503 from the proxy gets a persistent banner, not a toast).
- Correct `demo-fixtures.js` to the real shapes in section B; add fixtures for every operation the
  published demo pages call, shaped like the real output.

### C2. `Oaken.format` — presentation helpers (pure functions, unit-testable)

`date(iso)` (time today, weekday this week, date otherwise), `dayGroup(iso)`, `bytes(n)`,
`attachmentIcon(content_type)`, `initials(name)`, `labelColor(name)` (keep the palette, add a
stable hash fallback so a new label gets a colour), `plural`. No DOM access.

### C3. `Oaken.text` — the safe mail-text renderer (the only place mail text becomes markup)

`Oaken.text.toHtml(text)`: escape everything, preserve line breaks, collapse the quoted `>` blocks
into a toggle, linkify only `http(s):` and `mailto:` URLs with `rel="noopener noreferrer"` and
`target="_blank"`, never render `javascript:`/`data:`. Output is built with DOM APIs or an escaped
template. **Delete the `b.html` insertion in `readerHtml`.** Add a test page or a JS test that feeds
hostile text (`<img onerror>`, `javascript:`, unicode tricks) and asserts nothing becomes an element.

### C4. `Oaken.nav` — URL state and the sidebar

- One query-string model for the list view: `?view=inbox|starred|discarded`, `?folder=<location>`,
  `?label=<name>`, `?q=`, `?page=`. `loadList` reads it; the sidebar sets it. (Fixes G9.)
- `Oaken.sidebar.load()` renders the sidebar from `mailbox_counts` + `list_labels`: fixed entries
  (Inbox, Starred, Discarded), then a Folders section from the `locations` keys, then Labels. Unread
  badges from `unread`. Marks the active entry from the URL. Refreshes on the SSE events that change
  counts (`message.indexed`, `message.read_changed`) with a short debounce, and on `resync`.
- Remove the static folder/label markup from every template and the two "new folder/label" modals.
  Each page keeps one `<nav data-oaken-sidebar>` mount; the empty markup is the static fallback.

### C5. `Oaken.capabilities` — what to show

A tiny registry: `Oaken.has(op)` is true if `GET /v1/operations` lists `op`. Elements carry
`data-oaken-requires="send_message"` (or a comma list); the layer hides any element whose operation
is missing. This is how "hidden until it exists" is implemented without editing HTML per phase, and
it is the hook the plugin/capability discussion will extend (a per-account capability such as
`send` can be added to the same check without redesign). In demo mode every requirement is
satisfied.

### C6. List and reader components (refactor of the existing render layer)

- `Oaken.list.mount(container, {source})` replaces `loadList`: builds the request from `Oaken.nav`,
  renders rows with `esc()` + DOM text, handles paging (Newer/Older, "a–b of total"), selection
  (checkbox set, select-all, `j`/`k`), the empty state per view ("Your inbox is empty." /
  "No messages in <folder>."), and a loading/error state. Row template keeps the existing markup.
- `Oaken.reader.open(id)` replaces `loadReader`: calls `get_message`, renders with C3, fills
  attachments from metadata, wires Reply/Reply all/Forward (28), the star, the More menu
  (mark unread, view source), and arms/cancels `message_presented`/`message_dismissed`.
- Row actions: `star` (`set_flagged`), `mark-read`, `mark-unread`, all through `Oaken.mutate`, with
  optimistic UI that reverts on error and a refreshed `mailbox_counts` on success.

### C7. Forms and settings support

- `Oaken.form.fromSchema(el, jsonSchema, values)` renders a settings form from a plugin or core
  `config_schema` (string, integer, number, boolean, enum, secret-typed fields shown as "stored in
  the keyring" with no value), returns `{values(), errors(map)}`, and shows field-level errors from
  `check_*`. Used by 24 (account config) and 29 (core settings); **23 does not use it** (plugins are a TOML textarea for now). Secret fields never get a
  value input except the dedicated password operations.
- `Oaken.job.watch(job_id | op)` — shows progress for streaming operations (`sync_now`,
  `rebuild_index`, `reprocess`) from SSE (`sync.progress`, `index.rebuild_progress`, `job.state_changed`).
- Settings pane loader: each pane lazy-loads when its tab is first shown (`shown.bs.tab`), so opening
  Settings does not fire every operation.

### C8. Chips, compose host, and shortcuts (extend existing code)

- `initChips` gains address validation, paste-splitting on `,`/`;`/whitespace, and the
  `list_people` autocomplete; the produced chips are read by `Oaken.compose` (28).
- Replace the three compose entry paths (float, modal, page) with one `Oaken.compose.open(opts)`
  controller that owns state; the templates are views of it (28 specifies it).
- Implement `j`/`k`/`r`/`a`; keep `c`, `/`, `?`. Remove `e` from the shortcut table while archive is
  hidden (derive the table from `Oaken.has`).

### C10. Sync button (`Oaken.sync`)

The existing `data-oaken-action="sync"` handler calls `sync_now` with `{}`; `account_id` is
required, so it fails today. Replace it:

- **Two buttons, two meanings, never merged:** *Refresh* re-reads local data; *Sync* asks core to
  pull from the mail server. Distinct icons and tooltips ("Check for new mail").
- **Sync is a split button** (Bootstrap `btn-group`: main button + caret `dropdown-toggle-split`):
  - **Pressing the main button syncs all accounts** (`Oaken.sync.run()`).
  - **The caret opens a menu** headed "Sync account", listing one item per account from
    `list_accounts` (display name + address, small status dot from `list_schedules`). Choosing an
    item runs `Oaken.sync.run({account_id})` for that account only. The first menu item is
    "All accounts" (same as the main button). Accounts that cannot sync are listed disabled with
    the reason (paused, needs auth, send-only); a per-folder IMAP schedule may appear as a nested
    item that passes `folder` (only if the account's `list_schedules` rows include folders).
  - The menu is rendered when opened, from a cached `list_accounts` (refreshed on
    `schedule.changed`/`sync.paused` events), so it never goes stale or blocks the button.
  - While any sync runs the main button shows the spinner; items for accounts already syncing show
    "syncing…" and are disabled.
- `Oaken.sync.run(opts?)`: with no `account_id`, `list_accounts`, skip accounts that cannot sync (a
  send-only SMTP account has no mailbox; `list_schedules.problem` or a paused schedule is shown,
  not hidden); with `account_id`, only that one. Call `sync_now {account_id}` for each target
  through `Oaken.mutate`, collect `job_id`s.
- **State machine** driven by SSE, not timers: idle → syncing (spinner, button disabled, tooltip
  "Syncing 2 accounts…") → done (toast "3 new messages", the count from `sync.finished`; list and
  counts refresh) or failed (toast with the account and a plain reason; an auth failure links to
  Settings → Accounts → Re-authenticate). On a dropped stream, fall back to polling
  `list_schedules.last_run` every few seconds for up to a minute.
- `created:false` from core is not an error: show "Already syncing".
- Disable with an explanation when there are no accounts ("Add an account in Settings first").
- Keyboard: `g` then `s` (or `Shift+R`; pick one and list it in the shortcuts modal). The button
  carries no `data-oaken-requires` since `sync_now` ships today.
- Never run on page load or on a timer: sync is user-initiated here; scheduled sync stays core's
  job (12b).

### C9. Global behaviour

- Core-down banner: a 503 `core_unavailable` shows "Oaken core isn't running — start it with
  `oaken start`" and retries with backoff; the page stays usable (static shell).
- `Oaken.toast` gains a variant with an action button (Undo, Retry).
- No mail text in `console.*`, `localStorage`, URLs (the open message id is fine; subject is not),
  or error messages.
- Accessibility floor: list rows are keyboard-reachable (`tabindex`, Enter opens), toasts use
  `role="status"`, modals keep Bootstrap focus handling.

## D. Server changes (small, in `apps/webui/src/oaken_webui/`)

1. `GET /` → serve `index.html` (already via `html=True`); `GET /login.html` → 302 to `/`.
2. Add `Cache-Control: no-store` to HTML so a deploy never serves a stale shell; keep static assets
   cacheable with a version query string from `oaken.__version__`.
3. A `Content-Security-Policy` on the HTML responses: `default-src 'self'`, allow the two jsdelivr
   origins and Google Fonts already used by the templates (or vendor them: see Q7), `img-src 'self'
   data:`, `connect-src 'self'`, `frame-ancestors 'none'`. **No remote images** can load, which is the
   enforcement behind "Load remote images" being hidden.
4. Tests: the CSP header is present; `/login.html` redirects; a proxied non-JSON error from core still
   reaches the browser as JSON (`core_unavailable`).

## Acceptance criteria

- With a seeded synthetic mailbox and a running core, the served Inbox, Search and Reader pages load
  real data (no fixtures), page and filter, open a message (and it is marked read after the delay),
  mark read/unread with Undo, and star, with the sidebar badges updating from the event stream.
- The Sync split button: the main button syncs every syncable account; the caret menu lists each
  account (unsyncable ones disabled with a reason) and syncs only the chosen one (a test asserts
  exactly one `sync_now` call with that `account_id`).
- Clicking **Sync** against a fake provider shows syncing → "N new messages", the list and sidebar
  badges update, a second click during a run says "Already syncing", and a paused or unauthenticated
  account is reported rather than silently skipped.
- `get_message` text containing HTML and script is shown as inert text (C3 test).
- The sidebar shows exactly Inbox, Starred, Folders, Discarded, Labels; hidden controls are absent
  from the DOM when their operation is missing and present in demo mode.
- No page reads a field that its operation does not return: a checked-in **contract test** loads
  `/v1/openapi.json` (or `list_operations` schemas) and asserts that every `output.<field>` named in a
  `data-oaken-field` / render map exists in the schema. (Add a small `FIELDS` table in `assets/` that
  both the renderer and the test read.)
- The published demo pages still work with `data-oaken-demo` and the corrected fixtures.
- JS lint (add a minimal ESLint or `node --check` step) and the webui tests pass; `ruff`, `mypy`,
  `pytest`, and the docs check are green.

## Open questions (answers change scope; defaults in bold)

- **Q1 — Login/sign-out/SSO/2FA/PGP/sessions:** **hide all** for the loopback single-user app.
- **Q2 — Ask and Summary in the UI:** neither is in the mockup, but `ask` and `summarize_message`
  are the product's point. **Add a minimal "Ask" box on the search page and a Summary strip on the
  reader**, both optional if the LLM plugin is unhealthy.
- **Q3 — Attachment download:** **none this phase** (quarantine rules). A later prompt decides a safe
  download path.
- **Q4 — Rich-text compose:** **plain text only**; the formatting buttons are hidden.
- **Q5 — Drafts:** **no server drafts** (`gmail.modify` cannot write them); compose is in memory
  with an unsent-draft recovery in `localStorage` for the current compose only.
- **Q6 — MCP / plugin capability permissions: decided.** MCP policy is the `[mcp]` section of
  `config.toml` (`allow_send`, `workspaces`, enable/disable) shown in a textarea at Settings → MCP
  (32); workspaces are managed in 31; plugins in 23. All three use the API, with `config.toml` as
  the single source of truth. Send becomes an ordinary operation with an `off` MCP ceiling (28).
- **Q7 — Vendor Bootstrap and fonts** instead of the CDN so the UI works offline and the CSP is
  `'self'` only: **yes, vendor them** under `assets/vendor/` (license files kept).

## Report-back checkpoint

End with the six sections from `00-context.md`. Then stop.
