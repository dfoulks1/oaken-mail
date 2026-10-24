# 32 — MCP settings: what AI clients may do, and in which workspaces

**Read first:** `docs/prompts/00-context.md`, `docs/prompts/WEBUI_21b-webui-client-foundation.md`,
`docs/prompts/CORE_23-plugin-settings.md` (the section writer, live reload) and
`docs/prompts/CORE_31-workspace-management.md`.
**Depends on:** 13, 16, 17, 18, 21b, 22, 23, 29, 31.
**Related:** 28 makes `send_message` an ordinary operation and amends ADR 0004; this prompt is
where a person decides whether the MCP may use it.

## Why

The MCP is a thing on its own: a second surface of the same registry (ADR 0004) that a person
points an AI client at. Today its policy is split: a code **ceiling** per operation, a database
table of per-operation / per-plugin on-off rows (`operation_settings`), a global `[api] mcp` flag,
and nothing about **which workspaces** it may work in or whether it may **send**. The user wants
all of it managed from the UI, in one place, and wants the MCP tied to workspaces, which are
Oaken's confidentiality levels (31).

## Decisions already taken (the user's answers)

- **The policy lives in `config.toml` under `[mcp]`** and Settings → MCP shows that section in a
  **TOML textarea** (Load / Test / Save), exactly like a plugin (23). The UI toggles and the CLI
  verbs edit the same section through the same API operations, so there is **one source of truth
  and no second store**.
- **`[mcp] allow_send = false`** is the send ceiling, in that section. Send is off over MCP until
  it is `true` *and* the operation is enabled. Because the UI edits `config.toml`, the person can
  change it in the textarea; it is the person's call (the user accepted this).
- **`[mcp] workspaces = []`** links the MCP to workspaces by name. **An empty list means all
  workspaces.** A non-empty list restricts the MCP to those; every name must be an existing
  workspace (`check` rejects an unknown one). Workspaces are managed in 31.
- **MCP token management is internal** to core. This prompt exposes no token UI or operations; keep
  today's single `mcp_token` file unless a workspace binding needs more, and decide that inside the
  ADR.
- **The MCP, the UI and the CLI all use the API.** There is no MCP-only path to anything.
- Extensibility: the section is plain TOML with defaulted keys, so a later key (a new limit, a new
  list) is added without a migration or a new store.

## The `[mcp]` section

```toml
[mcp]
allow_send = false        # true lets the operator switch send_message on for MCP clients
workspaces = []           # [] = every workspace; otherwise only these (names from [workspaces.*])
enable  = []              # extra operations to expose, beyond their defaults: "op_name" | "plugin:name"
disable = []              # operations to hide, even if on by default
```

`enable`/`disable` replace the database `operation_settings` rows as the store for per-operation and
per-plugin exposure (Q1). The effective rule is unchanged: the code ceiling first (`never` can
never be enabled; `send` additionally needs `allow_send = true`), then an `op` entry, then a
`plugin:` entry, then the operation's default.

## Requirements (operations first, per prompt 22's rule)

1. **Reuse 29's generic section operations** for `[mcp]`: `get_settings("mcp")`,
   `check_settings("mcp", …)`, `set_settings("mcp", …)` (all `mcp="never"` for the writers;
   `get` `mcp="off"`). 29's rule "ceilings are not editable from a client" is **lifted for `[mcp]`
   only**, by an explicit entry in its allow-list, recorded in the ADR. Do not add `get/set_mcp_config`:
   one generic mechanism, many sections.
2. **`check_settings("mcp")` validates:** TOML syntax with line; `workspaces` entries all exist
   (error names the unknown one); `enable`/`disable` entries name real operations or plugins; an
   operation in both lists is an error; `enable` of an operation whose ceiling is `never` is an
   error with the reason; `allow_send = false` with `send_message` in `enable` is a warning, not an
   error (it stays Locked).
3. **`set_operation_exposure`** stays as the convenience the CLI and UI toggles call, but becomes a
   thin wrapper that edits the `enable`/`disable` lists through the same writer, so the textarea and
   the toggles can never disagree. Every change is still audited (`set_exposure`). A migration moves
   existing `operation_settings` rows into `[mcp]` once, then stops reading the table.
4. **`list_operations`** keeps returning `mcp_allowed`, `mcp_default`, `mcp_enabled`, and gains
   `mcp_locked_reason` (`"ceiling"`, `"allow_send"`, or `null`) so the UI can explain a Locked row.
5. **Workspace scoping.** An MCP call that runs in a workspace (anything that reaches the model
   or the workspace-keyed cache: `summarize_message`, `ask`/`search` with rewrite, enrichers) is
   refused with `not_exposed` if its workspace is not in `[mcp] workspaces` (when non-empty). The
   call's workspace comes from an optional `workspace` input, else the first listed workspace,
   else `default` (Q2). The MCP `initialize` instructions and `status` report the workspaces it can
   use, never mail. Enforcement is in core's MCP layer, not the client.
6. **Live reload.** A saved `[mcp]` section applies to the running MCP server without a restart
   (23's settings reload): the tool list changes, an in-flight call is not cut off, and a client
   that cached the old list gets the standard MCP `tools/list_changed` notice if the server
   supports it, else sees it on the next `tools/list`. `[api] mcp` (mounting `/mcp`) is separate
   and still needs a restart; the pane says so.
7. **Send.** No send-specific guardrail: no MCP rate limit, no recipient allowlist; an MCP send is
   the same `send_message` call, job, queue priority and audit row as a UI or CLI send (28). With
   `allow_send = false` the three send operations are Locked ("allow_send is false").
8. **CLI.** `oaken settings get|check|set mcp` (29's verbs); `oaken operations enable|disable NAME
   [--plugin]` and `oaken operations reset NAME` (wrappers over `set_operation_exposure`; **not**
   `oaken mcp`, which is the stdio bridge). `oaken operations` lists with the locked reason. Fix
   the bridge's misleading "core is not running" when MCP is simply disabled: say "MCP is not
   enabled on this core (set `[api] mcp = true`)". Add rows to 17's table.
9. **UI** (Settings → MCP):
   - A **status line**: is the MCP mounted, where it listens, which workspaces it may use, and
     whether send is permitted. A short note that mail text returned to an AI client leaves the core
     process, with the workspace's trust shown next to each linked workspace.
   - The **textarea** for `[mcp]` (Test / Save / Revert, `overridden` notice, dirty-state guard),
     pre-filled with the commented template above when empty.
   - Below it, the **operation list** from `list_operations`, grouped **Default / Extended /
     Locked**, each row with its permission class, whether it can change or send mail, whether it
     returns mail text, and an on/off switch (`set_operation_exposure`); Locked rows show the
     reason. A plugin group switch per plugin. **Presets** as client-side shortcuts that set rows:
     **Read only**, **Default**, **Everything allowed**; the pane shows the matching preset or
     "Custom". Toggling rewrites the textarea text so what you see is the file.
   - A **workspace picker** that adds or removes names in `workspaces = [...]` (with a "All
     workspaces" choice that writes `[]`), listing workspaces from `list_workspaces`.
   - Elements carry `data-oaken-requires="list_operations"` (21b C5).

## Open questions (defaults in bold)

- Q1: per-operation exposure moves from the database table into `[mcp]` `enable`/`disable`:
  **yes** (single source of truth); the table is migrated and dropped.
- Q2: an MCP call that names no workspace: **uses the first listed workspace, else `default`**.
  (Alternative: the call must name one when the list has more than one.)
- Q3: an `insecure` workspace linked to the MCP: **show a warning on the status line and the
  workspace row, do not block**; an operator who links one has chosen it.
- Q4: should `[mcp] workspaces` also gate **read** operations that touch no model (list, get)?
  **No**; it gates only workspace-scoped (model-reaching) calls.

## Acceptance criteria

- `get` → edit → `check` → `set` → `get` round-trips `[mcp]`; unknown workspace, unknown
  operation, a `never` operation in `enable`, and an operation in both lists are each rejected with
  a field/line error; nothing is persisted on a failed check.
- With `workspaces = []` the MCP serves every workspace; with `["a"]`, a model-reaching call in
  workspace `b` is refused `not_exposed` and one in `a` succeeds; a call with no workspace uses the
  rule in Q2.
- Toggling an operation in the UI, `oaken operations disable`, and editing the textarea all change
  the same `[mcp]` text and `tools/list`; an audit row exists for each change; migrated database
  rows produce the same exposure as before.
- Send: absent from `tools/list` with `allow_send = false` (Locked, reason `allow_send`); present
  only after `allow_send = true` **and** the operation is enabled; never on `/mcp/agent`; the
  registry still refuses `send` with `mcp="on"`; the MCP, CLI and UI sends reach one handler (28).
- A saved `[mcp]` takes effect without a restart; an in-flight call completes.
- The bridge says "MCP is not enabled" instead of "core is not running" when MCP is off.
- None of `set_settings`, `set_operation_exposure` is in the MCP tool set.
- The parity test from 22 covers these operations for CLI and UI.
- `ruff`, `ruff format --check`, `mypy`, `pytest` and the docs check are green; ADR added (amends
  0004 together with 28).

## Report-back checkpoint

End with the six sections from `00-context.md`. Then stop.
