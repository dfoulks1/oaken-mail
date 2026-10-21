# 31 — Workspace management

**Read first:** `docs/prompts/00-context.md`, `docs/prompts/21b-webui-client-foundation.md` and
`docs/prompts/23-plugin-settings.md` (the section writer and live settings reload).
**Depends on:** 10, 13, 16, 17, 21b, 22, 23.
**Used by:** 32 (MCP settings), which links the MCP to existing workspaces by name.

## Why

A **workspace** is the mechanism Oaken already has for **confidentiality levels** (prompt 10): a
named place AI work happens, with its own LLM cache and usage, and a `trust` of `secure` (only a
backend that runs on this machine) or `insecure` (a remote service is allowed). Today workspaces
exist only as `[workspaces.<name>]` tables in `config.toml`, with `default` always present. There is
no way to see, create, change or remove one from a client, and nothing can be linked to one. This
prompt makes them manageable from the UI and CLI, so 32 can tie MCP access to them.

## Decisions already taken (the user's answers)

- **Workspaces are the confidentiality mechanism.** Do not invent a second notion of trust.
- **One source of truth:** `config.toml` `[workspaces.<name>]`. The UI and CLI use the API
  operations below; no second store. Persistence uses 23's generic section writer.
- Management commands: list, get, set, remove (the set the user approved).
- Token handling for MCP is internal to core (32); nothing here exposes tokens.

## Requirements (operations first, per prompt 22's rule)

1. **`list_workspaces`** — `read`, `mcp="off"`. Returns `{workspaces:[{name, trust, llm_backend,
   effective_backend, is_default, usage:{calls, tokens_in, tokens_out, cache_entries},
   used_by:{mcp: bool}}]}`. `used_by.mcp` says whether the `[mcp] workspaces` list (32) names it, so
   the UI can warn before a removal.
2. **`get_workspace(name)`** — `read`, `mcp="off"`. Returns the table as TOML text (the textarea
   content), the schema, `effective_backend`, and `overridden` env keys.
3. **`check_workspace(name, toml)`** — `read`, persists nothing. Validates the table against
   `WorkspaceSettings` and applies the **confidentiality rule**: a `secure` workspace whose backend
   is not `locality = "local"` is an error (the same `check_backend` rule), with a plain message.
   Returns `{valid, errors[], effective_backend, warnings[]}`; changing `trust` from `secure` to
   `insecure` adds a warning ("a remote model may now see mail text; caches stay separate").
4. **`set_workspace(name, toml)`** — `mutate`, confirm-gated, audited (name, changed keys),
   `mcp="never"`. Creates or replaces `[workspaces.<name>]` through the section writer, then
   reloads workspaces live (23's settings reload). Names are `[a-z0-9_-]{1,32}`.
5. **`remove_workspace(name)`** — `mutate`, confirm-gated, audited, `mcp="never"`. Refuses
   `default`, and refuses a workspace the MCP config still names (the error says to edit `[mcp]`
   first; a link, not a cascade). Removing does **not** delete its cache or usage rows (they are
   derived data and kept apart; offer a separate "clear this workspace's cache" later via 29's
   `clear_cache {workspace}`).
6. **Confidentiality stays enforced where it is today** (the cache key and `check_backend`), not by
   this UI. A test proves a config saved here cannot put a remote backend behind a `secure`
   workspace, and that results cached in one workspace are never served from another.
7. **CLI:** `oaken workspaces list`, `oaken workspaces get NAME`, `oaken workspaces check NAME
   [--file F | --toml '…']`, `oaken workspaces set NAME [--file F]`, `oaken workspaces remove
   NAME`. Add rows to 17's table.
8. **UI** (Settings → Workspaces, or under AI & Models; say which in the report): a table from
   `list_workspaces` (name, trust badge, backend, usage numbers, "used by MCP"); **New workspace**
   and **Edit** open the same **TOML textarea** widget as plugins (23) with Test / Save / Revert;
   **Remove** opens a confirm modal that names anything that links to it. `default` shows no Remove.
   Elements carry `data-oaken-requires="set_workspace"` (21b C5).

## Open questions (defaults in bold)

- Q1: does a workspace carry anything beyond `trust` and `llm_backend` in this phase? **No.** The
  MCP link lives in `[mcp]` (32), not on the workspace, so there is one place to read the policy.
- Q2: renaming a workspace: **not supported** (create the new one, remove the old); cache and usage
  are keyed by name.
- Q3: which workspace do `ask`, `summarize_message` and the enrichers use when none is named?
  **`default`**, as today; this prompt changes nothing about that.

## Acceptance criteria

- `list_workspaces` returns every configured workspace plus `default`, with correct usage numbers
  on a seeded database; `get` → edit → `check` → `set` → `get` round-trips and leaves other tables
  and comments untouched.
- `check_workspace` rejects a `secure` workspace backed by a non-local backend and warns on a
  secure → insecure change; `remove_workspace` refuses `default` and a workspace `[mcp]` names.
- A saved workspace is usable by the next request with no restart (live reload), and an in-flight
  call is not disturbed.
- None of `set_workspace`/`remove_workspace` is in the MCP tool set; the read operations are `off`.
- The parity test from 22 covers these operations for both clients.
- `ruff`, `ruff format --check`, `mypy`, `pytest` and the docs check are green.

## Report-back checkpoint

End with the six sections from `00-context.md`. Then stop.
