# 31 — Workspace management

> **Status: operations and CLI built (0.1.x); the UI pane is not. Where this prompt and the code
> differ, the code is right, except the two gaps named under "Still owed", which the code must
> close.**
>
> - **Built:** `list_workspaces`, `get_workspace`, `check_workspace`, `set_workspace`,
>   `remove_workspace` (`src/oaken/core/operations/builtin/workspaces.py`), reusing 23's
>   `config_edit` section writer and `_write`, with a live settings reload. CLI: `oaken workspaces
>   list|get|check|set|remove` (`workspace_cmd.py`). Confidentiality rule (`secure` ⇒ a
>   `locality="local"` backend) via `check_backend`. 20 tests under a throwaway `OAKEN_HOME`.
> - **Names changed from the original spec:** `check_workspace` is **`mcp="never"`**, not `off`
>   (stricter; a trust/backend probe is not an MCP read). `list_workspaces` returns
>   `{name, trust, llm_backend, is_default, usage}` — **no** `effective_backend` or `used_by.mcp`
>   yet (both wait on 32). `get_workspace` returns `llm_backend` (resolved) **and** `llm_backend_raw`
>   (as stored). These are the code, and the code is right.
> - **Still owed (the code does not yet meet the spec):**
>   1. **`set_workspace` must enforce the confidentiality rule, not only `check`.** Today `set`
>      validates the `WorkspaceSettings` shape but never runs `check_backend`, so a `secure`
>      workspace with a remote backend can be saved by skipping Test. `set` must run the same
>      validation `check` runs and refuse on `valid:false`. This is a security gap, not a nicety.
>   2. **The UI pane** (Settings → Workspaces) and its `list_workspaces` fields (`effective_backend`,
>      `used_by.mcp`) are not built; both of those fields and the "refuse a workspace MCP names"
>      guard wait on 32.

**Read first:** `docs/prompts/00-context.md`, `docs/prompts/CORE_23-plugin-settings.md` (the section
writer and live reload). **Depends on:** 10, 13, 16, 22, 23. **Used by:** 32 (MCP settings), which
links MCP access to workspaces by name.

## Why

A **workspace** is Oaken's confidentiality level (prompt 10): a named place AI work happens, with
its own LLM cache and usage, and a `trust` of `secure` (a `locality="local"` backend only) or
`insecure` (a remote backend is allowed). They live as `[workspaces.<name>]` in `config.toml`, with
`default` always present. This prompt makes them manageable from a client so 32 can tie MCP access
to them.

## Decisions already taken

- **Workspaces are the one confidentiality mechanism.** Do not invent a second notion of trust.
- **One source of truth:** `[workspaces.<name>]` in `config.toml`, via 23's section writer. No
  second store. The UI and CLI go through the operations below.
- **Management verbs:** list, get, check, set, remove. No rename (create-new, remove-old; cache and
  usage are keyed by name).
- **`check_workspace` is `mcp="never"`.** A check reveals backend/trust wiring; it is not an MCP
  read. (Divergence from the first draft, recorded; the stricter code is right.)
- **`set_workspace` enforces the confidentiality rule itself** (see Requirement 4), so the guarantee
  does not depend on the caller running `check` first.
- Token handling for MCP is internal to core (32); nothing here exposes tokens.

## Requirements (operations first)

Each a thing that can be checked. The ops exist; the lines in **bold** are what the code still owes.

1. **`list_workspaces`** — `read`, `mcp="off"`. Returns one row per workspace (plus `default`):
   `{name, trust, llm_backend, is_default, usage:{calls, tokens_in, tokens_out, cache_entries}}`.
   *(32 adds `effective_backend` and `used_by:{mcp}` so the UI can warn before a removal; not now.)*
2. **`get_workspace(name)`** — `read`, `mcp="off"`. Returns the table as editable TOML text (no
   header), `trust`, `llm_backend` (resolved), `llm_backend_raw` (as stored), `overridden_keys`, and
   `usage`. `NotFound` for an absent name (`default` always resolves).
3. **`check_workspace(name, toml)`** — `read`, `mcp="never"`, persists nothing. Validates the name
   (`[a-z0-9_-]{1,32}`) and the table against `WorkspaceSettings`, applies the confidentiality rule
   (`secure` + a non-`local` effective backend is an error, via `check_backend`), and warns on a
   `secure`→`insecure` change. Returns `{valid, errors[{key,message}], warnings[], effective_backend}`.
4. **`set_workspace(name, toml)`** — `mutate`, confirm-gated, audited (name + changed keys),
   `mcp="never"`. **Runs the Requirement-3 validation and refuses on `valid:false`** (so a `secure`
   workspace can never be saved behind a remote backend, Test or no Test), then writes the table
   through the section writer and reloads workspaces live. **This is the gap the code must close:
   today `set` validates shape only.**
5. **`remove_workspace(name)`** — `mutate`, confirm-gated, audited, `mcp="never"`. Refuses `default`.
   Keeps the workspace's cache and usage rows (derived data; a separate clear comes later).
   *(32 adds: also refuse a workspace the `[mcp]` config names, with a "edit [mcp] first" message.)*
6. **Confidentiality stays enforced in core** (the cache key partitions by workspace;
   `check_backend` gates the backend), proven by tests: a config saved here cannot put a remote
   backend behind a `secure` workspace (**the Requirement-4 test is owed**), and results cached in
   one workspace are never served from another.
7. **CLI** (built): `oaken workspaces list|get|check|set|remove`. `set`/`check` take TOML as
   positional lines or `--file F` (`-` for stdin). Rows are in 17's table.
8. **UI** (Settings → Workspaces; **not built**): a table from `list_workspaces` (name, trust badge,
   backend, usage); New/Edit open the same TOML textarea as plugins (23) with Test/Save/Revert;
   Remove confirms and names anything linking to it; `default` shows no Remove. Elements carry
   `data-oaken-requires="set_workspace"`. The remove-warning and the "used by MCP" column wait on 32.

## Acceptance criteria

- `list_workspaces` returns every configured workspace plus `default` with correct usage on a seeded
  DB; `get` → edit → `check` → `set` → `get` round-trips, other tables and comments untouched.
- `check_workspace` rejects a `secure` workspace backed by a non-local backend and warns on
  `secure`→`insecure`; a bad name and an invalid `trust` are field errors; empty TOML is valid.
- **`set_workspace` refuses a `secure` + remote-backend config even when `check` was not called
  first** (the owed security test), and refuses a bad name.
- `remove_workspace` refuses `default`, raises `NotFound` for an absent name, and keeps cache/usage.
- A saved workspace is usable by the next request with no restart (live reload); an in-flight call
  is undisturbed.
- None of `check_workspace`/`set_workspace`/`remove_workspace` is in the MCP tool set; the two read
  ops are `off`.
- The 22 parity test covers these for both clients. `ruff`, `ruff format --check`, `mypy`, `pytest`
  and the docs check are green.

## Open questions (defaults in bold)

- Q1: anything on a workspace beyond `trust` and `llm_backend` this phase? **No.** The MCP link
  lives in `[mcp]` (32), so the policy is read in one place.
- Q2: rename? **No** (cache/usage are name-keyed).
- Q3: which workspace do `ask`/`summarize`/the enrichers use when none is named? **`default`**,
  unchanged.

## Report-back checkpoint

End with the six sections from `00-context.md`. Then stop.
