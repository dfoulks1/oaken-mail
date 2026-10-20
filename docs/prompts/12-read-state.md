# 12 — Read state: `mark_read` on open/summarize, write-back, audit, undo

**Read first:** `docs/prompts/00-context.md`. **Depends on:** 11 (and 05, 09).

## Goal

The feature the user cares about most: **read state follows what the user has actually
seen**. One core path owns every read/unread change; it writes through to the provider,
survives failures, and is fully auditable and undoable.

## Policy (from the user — implement exactly)

- A message is marked read when the user **opens/presents** it, or the user
  **explicitly asks for a summary** of it. Reason codes: `user_opened`,
  `user_summarized`, `mcp_summarized` (MCP `summarize_email` counts).
- **Never** marked read by: sync, ingest, embedding, enrichment (including any stored
  summary produced in the background), search results, list views, or bulk reads.
  A test must make this impossible to regress.
- Opening applies after a **configurable delay** (`ui.mark_read_delay_ms`, default 2000),
  implemented as a cancellable core-side pending-mark: the client sends
  `message_presented(id)` / `message_dismissed(id)`; core marks read only if still
  presented after the delay. (Core owns the timer so every client behaves the same.)
- A global setting `read_state.auto_mark = true|false` disables all automatic marking
  without breaking explicit `mark_read`/`mark_unread` operations.

## Requirements

1. **Local-first state machine** in `message_state`: set local `is_read` immediately,
   record the desired server state in `local_dirty_json`, then enqueue a durable
   `flags.write` job (dedupe key per message) that calls the provider plugin. Success
   clears the dirty marker; failure retries with backoff; the UI sees the local state
   throughout. Provider capability check: if no provider has `flags.write`, the
   change stays local and is reported as `local_only`.
2. **Inbound reconciliation:** provider `flag_changes` from sync update local state
   unless a **newer local dirty change** for that message exists (last-writer rule
   documented; local dirty wins until acknowledged). Emit `MessageReadChanged`.
3. **Operations (core API, not yet HTTP):** `mark_read(ids, reason)`,
   `mark_unread(ids, reason)`, `message_presented(id)`, `message_dismissed(id)`,
   `undo(audit_id)`. Mutating ops go through the **confirm gate** concept from prompt
   13 (stub the interface here: reasons beginning `user_` are pre-confirmed; anything
   else requires an explicit `confirm=True`).
4. **Audit log** (`raw.db` `audit_log`): `(id, ts, actor, reason, op, message_ids,
   before_json, after_json, undone_by NULL)`. `undo` restores the exact prior state and
   is itself audited. Bulk changes are one audit row with ids (cap and chunk >500).
5. Events: `MessageReadChanged` carries `reason` and `audit_id` so clients can offer undo.

## Acceptance criteria

- Property/regression test: run a full fake sync + ingest + embed + enrich over 500
  unread messages → **zero** `mark_read` calls and all still unread locally and on the
  `FakeProvider`.
- Presented for 2s (FakeClock) → marked read once; presented then dismissed at 1s →
  never marked; re-presented → fresh timer.
- Provider outage: local state flips immediately, job retries, eventually the
  `FakeProvider` shows read; a competing inbound `UNREAD` flag from sync does not
  clobber a pending local change, but does apply after acknowledgment.
- `undo` restores prior state locally and enqueues the reverse write-back.
- `auto_mark=false` leaves everything unread but explicit `mark_read` still works.
- MCP-style `summarize` path calls `mark_read(reason="mcp_summarized")`; a **search**
  call returning the same message does not.

## Out of scope

The summary generator itself (14), HTTP/MCP transport (16/18), UI timing UX.

## Docs site

Add `docs/architecture/read-state.md` (the policy table and the state-machine table) to
the *Concepts* group. This is a headline feature; give it a short, plain-language
"what marks a message read, and what never does" section at the top. `check_docs.py`
must pass.

## Checkpoint

Standard report. Include the state-machine table (local state × server state ×
dirty marker → action).
