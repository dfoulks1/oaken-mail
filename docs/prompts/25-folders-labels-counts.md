# 25 — Mailbox chrome: folders, labels and unread counts

**Read first:** `docs/prompts/00-context.md`. **Depends on:** 07, 12, 13, 16, 17, 21, 22.

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
2. **Labels and folders.** Confirm `list_labels` returns what the sidebar needs (name, and a stable
   identity for filtering); extend if the UI needs colour/ordering metadata (colour is UI config,
   not mail data — keep it client-side unless the user has set per-label colours server-side). A
   "folder" in Oaken is a disposition plus the account's provider folders where applicable; define
   precisely what the sidebar lists and from which operation.
3. **Selection → filter.** Selecting a label/folder drives `list_messages`/`search` with the right
   filter (`label`, `disposition`, `unread_only`); no new logic in the client. Unread badges update
   from `mailbox_counts` after actions that change read state (mark read/unread), via a refresh or
   the event stream.
4. **Live updates (optional but preferred).** If feasible, counts and the list update from the SSE
   event stream (`message.ingested`, read-state changes) rather than only on manual refresh. Keep it
   resilient: a dropped stream falls back to a re-fetch.
5. **CLI** parity: a `oaken counts [--account …]` verb (or the generic `op`) returns the same
   numbers the UI badges show; update 17's table.
6. **UI** wiring: sidebar folders/labels with unread badges from `mailbox_counts`; clicking filters
   the list; the empty state (prompt from the dummy-data removal) shows when a folder is genuinely
   empty.
7. **MCP ceiling.** Counts are `read`, `mcp="on"` is reasonable (no mail content leaves).

## Acceptance criteria

- `mailbox_counts` returns correct totals/unread per label and disposition on a seeded synthetic
  mailbox; a property or example test checks counts match what `list_messages` would return for the
  same filter, and that discarded mail is excluded unless asked.
- Marking messages read/unread changes the counts a client sees (via refresh or event); a test
  covers the before/after.
- The sidebar is drivable identically from the CLI (`counts`) and the UI against `mailbox_counts`;
  the parity test from 22 covers it.
- `ruff`, `ruff format --check`, `mypy`, `pytest` and the docs check are green.

## Report-back checkpoint

End with the six sections from `00-context.md`. Then stop.
