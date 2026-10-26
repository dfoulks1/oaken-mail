# 29 — System settings, storage, logs

> **Status: operations and CLI built (0.1.1); the UI panes are not. Where this prompt and the code
> differ, the code is right.**
>
> - **Built:** `get_settings`, `check_settings`, `set_settings` (sections `index`, `retrieval` (the
>   `[search]` table), `read_state`, `ui`, `sync`, `ai`, `log`; the key list is one constant,
>   `oaken.core.settings_edit`); `storage_info`; `vacuum` (a job, one at a time, refuses during an
>   index rebuild or without room on the disk); `cache_stats` and `clear_cache` (derived answers
>   only); `events_tail`; `list_audit` with `since` and a keyset `cursor`. CLI: `oaken settings`,
>   `storage`, `vacuum`, `cache`, `events`, `audit --since/--cursor/--all`, and a health summary in
>   `oaken status`. None is offered over MCP.
> - **Deviations.** `get_settings` takes an optional section and returns a list (all sections, or
>   one). `storage_info` is its own operation rather than a change to `status`. `set_settings` does
>   not reload core (a reload is `config_reload`, or `--reload` on the CLI verb) and says whether a
>   key needs a reload, a rebuild-index or a restart. The audit row is the registry's (an input
>   hash); the keys changed are in the result.
> - **Decided with Drew.** `events_tail` reads a **persisted JSON-lines log** (`events.jsonl`,
>   `[log] events_file`, owner-only, rotated), written by a handler on the event bus, not an in-memory
>   buffer. `[log] level` and `format` are editable. They were offered as restart-needed keys, which
>   was wrong: a reload reconfigures logging, so they apply on `oaken reload` like the rest. **No
>   listed key needs a restart**, so `restart_needed` is empty for real keys; the mechanism is tested
>   with a stand-in key.
> - **Environment overrides** (`OAKEN_AI__CHUNK_TOKENS`) are reported in `get_settings` and as a
>   warning on save, never silently shadowed.
> - **Not built:** the Settings panes (AI & Models, Schedule, Storage & Data, Logs) and
>   `Oaken.form.fromSchema` rendering; `ui-contract.json` entries for the new operations.

**Read first:** `docs/prompts/00-context.md`.
**Depends on:** 13, 22, 23.

## Goal

Back the Settings panes that no earlier UI prompt covers: **core settings** (AI token-thrift and
retrieval keys, read-state timing, sync defaults), **Storage & Data** (sizes and maintenance),
**Logs** (audit, events, export), the **enrichment cache** numbers and clear, and the reserved
**MCP exposure** link (the MCP pane itself is built in 32). Prompt 23 handles *plugin* config; this handles core's own `config.toml`
sections, with the same load → check → save shape.

## Decisions already taken

- Same rule as 23: scoped, comment-preserving read-modify-write of `config.toml`; env overrides
  (`OAKEN_*`) are reported, never silently shadowed; secrets never pass through here.
- Only an **allow-listed** set of core keys is editable from a client. Anything that changes where
  data lives, binds a port, or touches security (`api.*`, paths, token files) is not editable here.
  The one exception is the `[mcp]` section, added to the allow-list by prompt 32 (which owns it).
- Presentation prefs (theme, density, layout, composing prefs) stay in `localStorage`.
- MCP policy is **not** decided here: it is prompt 32 (the `[mcp]` section edited through these
  same generic `get/check/set_settings` operations). Workspaces are 31.

## Requirements (operations first)

1. **`get_settings(section)`** — `read`: the editable keys of one core section, their JSON Schema,
   current values and which are overridden by env. Sections: `index`, `retrieval`, `read_state`,
   `ui` (`mark_read_delay_ms`), `sync` (defaults), `ai` (token-thrift toggles, top-k). Non-allow-listed
   keys are absent.
2. **`check_settings(section, values)`** — `read`, persists nothing; field-level errors.
3. **`set_settings(section, values)`** — `mutate`, confirm-gated, audited (keys changed, never
   values for anything that could be sensitive), `mcp="never"`. Says plainly which keys take effect
   live and which need a restart (`restart_needed`).
4. **Storage figures.** Extend `status` (or add `storage_info`, `read`) with `raw_db_bytes`,
   `index_db_bytes`, `data_dir`, `quarantine_dir`, `quarantined_files`. Sizes come from the
   filesystem, not a scan of mail. (The mockup's numbers are fiction until this exists.)
5. **Maintenance.** `rebuild_index` and `reprocess` exist. Add **`vacuum`** (`mutate`, streaming job,
   `mcp="never"`, one at a time, refuses while a rebuild runs). "Open quarantine folder" is
   **removed** from the UI; show the path with a copy button (a browser cannot open a local folder).
6. **Enrichment cache.** `usage_summary` returns calls/tokens/cache saved. Add `cache_stats`
   (`read`: entries, bytes, prompt version, hit rate) and **`clear_cache`** (`mutate`,
   confirm-gated, audited, `mcp="never"`; clears derived LLM results only, never mail or the index).
7. **Logs.** `list_audit` exists (add `since` and a `cursor` for paging, plus a filter by `op`
   already there). The Events tab reads `GET /v1/events` live; add an `events_tail` (`read`) that
   returns the last N events from the JSON-lines log so the tab has history on load. Export is a
   client-side download of the loaded rows.
8. **MCP exposure.** Built in **32** (Settings → MCP: Default / Extended / Locked over
   `list_operations`/`set_operation_exposure`). Here, Settings only links to it. **No new policy.**
9. **CLI.** `oaken settings get|check|set SECTION`, `oaken storage`, `oaken vacuum`,
   `oaken cache stats|clear`, `oaken events --tail`. Update 17's table.

## UI

- Settings → AI & Models: the Ollama endpoint/models form is **23** (plugin config); the token-thrift
  toggles and top-k are `get/check/set_settings("ai")` rendered by `Oaken.form.fromSchema`,
  each section with its own **Test/Save** (the single global "Save changes" button is removed).
- Schedule pane: the "Default interval / Max concurrent / Min interval" rows are `get_settings("sync")`
  (read-only for `min_interval`, which is a floor).
- Storage & Data and Logs panes load lazily; streaming maintenance shows `Oaken.job.watch`.
- Security pane: **hidden** (no backing); the MCP exposure pane replaces its purpose.

## Acceptance criteria

- Round-trip test per section: `get` → edit → `check` → `set` → `get`; unrelated config and comments
  untouched; an env override is reported; a non-allow-listed key is refused.
- `restart_needed` is correct for a live key and a restart key.
- `cache stats`/`clear_cache` only touch the LLM cache (a test confirms mail rows and index rows are
  unchanged).
- Storage numbers match the file sizes on a seeded data directory.
- `vacuum` refuses while a rebuild runs.
- The parity test from 22 covers every new operation for the CLI and the UI; none is on MCP except the
  existing exposure operations as before.
- `ruff`, `ruff format --check`, `mypy`, `pytest` and the docs check are green.

## Open questions (defaults in bold)

- **Q1 — Editable core keys:** **only those listed above**; the list is a constant in one place.
- **Q2 — `vacuum`:** **ship it** (cheap and the mockup promises it).
- **Q3 — MCP pane:** **ship the pane read/toggle only**, pending your capability discussion.

## Report-back checkpoint

End with the six sections from `00-context.md`. Then stop.
