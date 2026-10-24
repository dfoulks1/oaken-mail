# 25 — Mailbox chrome: folders, labels and unread counts

**Read first:** `docs/prompts/00-context.md`, `docs/prompts/WEBUI_21b-webui-client-foundation.md` and
`docs/adr/0008-location-and-enrichment-attrs.md`. **Depends on:** 07, 12, 13, 16, 17, 21, 21b, 22
(22 adds the `location`/`starred` filters and `offset`/`total`; do not duplicate them here).

## Decisions already taken (the user's answers)

- The live sidebar shows **Inbox, Starred, Folders, Discarded, Labels**, in that order, and nothing
  else. Snoozed, Sent, Drafts, Archive, Spam, Trash are **not shown** (no backing concept). The
  "New folder" and "New label" buttons and modals are **removed**: folders are the distinct
  `location` values (ADR 0008) and labels come from Sieve and providers, so a client cannot create
  them.
- The folder section is titled **Folders** (not "Filed folders"). A folder is a `location`;
  `disposition=filed` is an implementation detail the UI never names.
- **Star is in scope.** It is the one new mail mutation in this phase, written back through the
  `\Flagged` flag.

## Goal

Populate the mailbox sidebar and list chrome from real data: the folders/labels a client shows,
the **unread counts** beside them, and the filtering that happens when one is selected. Today
`list_labels` and `list_messages` (with `label` / `unread_only` / disposition filters) exist, but
**nothing returns counts**, so the sidebar badges have no backing. This prompt adds the counts
operation and wires the chrome in both clients.

## Requirements (operations first)

1. **A counts operation.** Add `mailbox_counts` (name to confirm), `read`, returning the numbers a
   sidebar needs without pulling messages:
   - per **label**: total and unread;
   - per **disposition/folder** the UI shows (inbox, filed, discarded, starred, …): total and
     unread;
   - optionally per **account** when more than one is configured.
   It must be cheap (aggregate queries over the index, not a scan), honor the same "discarded hidden
   unless asked" rule as search, and never mark anything read. Decide whether counts are per-account
   or merged, and whether the UI can request a subset; keep it one round-trip for a sidebar render.

   **Output shape the UI is built against (21b A3):**
   `{inbox:{total,unread}, starred:{total,unread}, discarded:{total,unread},
   locations:{<name>:{total,unread}}, labels:{<name>:{total,unread}}, accounts?:{<id>:{…}}}`.
   `locations` is the folder list (distinct `location` values), so the sidebar needs no other call
   for folders. `starred` counts messages carrying `\Flagged` regardless of disposition, excluding
   discarded unless asked. Default: **merged across accounts**, with an optional `account_id` input.
2. **Labels and folders.** Confirm `list_labels` returns what the sidebar needs (name, and a stable
   identity for filtering); extend if the UI needs colour/ordering metadata (colour is UI config,
   not mail data — keep it client-side unless the user has set per-label colours server-side). A
   "folder" in Oaken is a disposition plus the account's provider folders where applicable; define
   precisely what the sidebar lists and from which operation.
3. **Star.** Add **`set_flagged {ids, flagged}`** — `mutate`, `mcp="on"`? **No: `mcp="off"`** (a
   model changing what the user flagged is the user's call to enable), confirm-gated, audited with
   undo, `marks_read=False`. It writes `\Flagged` locally with a dirty marker and writes back through
   the provider's `flags.write` exactly the way read state does (same durable job, same "stays
   local and says so" outcome). It is subject to the existing rule that only the read-state service
   names read state and calls `set_flags`: put the star path in that service (rename or generalise
   it to "flag state") rather than letting a new caller reach `set_flags`, and keep the enforcing
   test green. Output mirrors `MarkOutput` (`changed`, `unchanged`, `missing`, `local_only`,
   `audit_ids`). `undo` must restore it. `starred` appears on `MessageItem`/`GetMessageOutput` (22).
4. **Selection → filter.** Selecting a label/folder drives `list_messages`/`search` with the right
   filter (`label`, `location`, `starred`, `disposition`, `unread_only`); no new logic in the
   client. URL state is `?view=inbox|starred|discarded`, `?folder=<name>`, `?label=<name>` (21b C4). Unread badges update
   from `mailbox_counts` after actions that change read state (mark read/unread), via a refresh or
   the event stream.
5. **Live updates (optional but preferred).** If feasible, counts and the list update from the SSE
   event stream (`message.ingested`, read-state changes) rather than only on manual refresh. Keep it
   resilient: a dropped stream falls back to a re-fetch.
6. **CLI** parity: a `oaken counts [--account …]` verb (or the generic `op`) returns the same
   numbers the UI badges show; `oaken star|unstar ID…` calls `set_flagged`; update 17's table.
7. **UI** wiring, built on `Oaken.sidebar`/`Oaken.nav`/`Oaken.list` from 21b C4/C6: sidebar entries
   and badges from `mailbox_counts` + `list_labels`; clicking filters the list; the per-view empty
   state shows when a view is genuinely empty ("Your inbox is empty.", "No starred messages.",
   "No messages in <folder>."); the star toggle (row and reader) calls `set_flagged` with an Undo
   toast. Delete the static folder/label markup and the two "new" modals from every template.
8. **MCP ceiling.** Counts are `read`, `mcp="on"` is reasonable (no mail content leaves).
   `set_flagged` is `mcp="off"` as above.

## Acceptance criteria

- `mailbox_counts` returns correct totals/unread per label and disposition on a seeded synthetic
  mailbox; a property or example test checks counts match what `list_messages` would return for the
  same filter, and that discarded mail is excluded unless asked.
- Marking messages read/unread changes the counts a client sees (via refresh or event); a test
  covers the before/after.
- Starring a message sets `\Flagged` locally, is audited and undoable, is written back through the
  provider's `flags.write` (fake provider in tests), appears in the Starred count and list, and the
  "only the read-state service calls `set_flags`" test still passes.
- The sidebar shows exactly Inbox, Starred, Folders, Discarded, Labels; a folder created by a
  `fileinto` Sieve rule appears after `reprocess('filter')` with no client change.
- The sidebar is drivable identically from the CLI (`counts`) and the UI against `mailbox_counts`;
  the parity test from 22 covers it, including `set_flagged`.
- `ruff`, `ruff format --check`, `mypy`, `pytest` and the docs check are green.

## Report-back checkpoint

End with the six sections from `00-context.md`. Then stop.
