# 32 — MCP settings: which workspaces AI clients may reach

> **Status: built (the scoping slice).** `[mcp] workspaces` (default `["default"]`; `[]` = all) is
> enforced in `CoreClient.call` for MCP callers by `McpScope` (`core/operations/scope.py`);
> `workspace_of` is the one message-to-workspace function. `mcp` is an editable settings section
> (`workspaces` only; `allowed` stays with the exposure switches), `check_settings("mcp")` rejects an
> unknown workspace and warns on `[]`, `list_operations` gained `mcp_locked_reason`, `status` gained
> `mcp_workspaces`, the MCP instructions name the workspaces, and `/stats` → Reference has a status
> line and a workspace picker (`POST /stats/mcp-workspaces`, then reload).
> Deviations: no new CLI verbs (`oaken mcp expose` and `oaken settings get|check|set mcp` already
> cover them); the bridge already says "the MCP server is off in core"; the secure-workspace warning
> fires on `[]` only, because `default` is secure and would warn on every install (Q3). Search,
> list, ask and the label and count calls are **refused, not filtered**, when a restricted scope
> cannot cover every account (name an account in scope); filtering them by a set of accounts needs
> `account_ids` through the index, vector and ask paths and is left for a later prompt. Live reload
> goes through core's config reload; `tools/list_changed` was not spiked.

**Read first:** `docs/prompts/00-context.md`, `CORE_40-mcp-allowed-config.md` (the `[mcp.allowed]`
exposure this builds on), `CORE_31-workspace-management.md` and `CORE_44-workspace-ops-fixes.md`
(workspaces, and the one validator both `check` and `set` obey), `CORE_23-plugin-settings.md` (the
comment-preserving writer, live reload) and ADR 0004.
**Depends on:** 13, 16, 17, 18, 22, 29, 31, 40 (all built). Nothing unbuilt.
**Track:** MCP. Touches `McpSettings` (`src/oaken/core/settings.py`), the MCP layer, the exposure
resolver, `list_operations`, the `bridge` message, and Core's `/stats` console page.
**Not here:** sending. See "Split out".

## Why

The MCP is a surface of the same registry (ADR 0004) that a person points an AI client at. Today
its policy is *which operations* (the code ceiling, plus `[mcp.allowed]` from 40). It has nothing
about *which mail*. Mail text returned to an AI client leaves the core process, and workspaces are
Oaken's confidentiality levels (31). So the next control is: **which workspaces an MCP client may
see.**

## What changed from the first draft

The first draft of this prompt predates 40 and 44. Three of its decisions no longer hold:

- **Exposure lives in `[mcp.allowed]`, not in `[mcp] enable` / `disable`.** 40 built it
  (`McpSettings.allowed`, `ExposureStore.seed`, `write_mcp_allowed`). There is one store and one
  shape. No `enable`/`disable` lists, no migration, and `operation_settings` stays the live-read
  cache 40 made it.
- **A call does not name its workspace; its account does.** `Workspaces.for_account` resolves a
  message's workspace from its account's `workspace` setting. So scoping filters by the account of
  the mail a call touches. There is no `workspace` input and no "first listed workspace" rule.
- **The UI is Core's console, not a separate product's.** The pane extends `/stats` (its Reference
  tab already has a switch per operation). It does not add a "Settings → MCP" page.

## Decisions already taken

- **The policy lives in `config.toml` under `[mcp]`**, one source of truth. The console and the CLI
  edit it through the same operations, never a second store.
- **`[mcp] workspaces` links the MCP to workspaces by name, and ships as `["default"]`.** A fresh
  install exposes only the `default` workspace; reaching more is an explicit edit. **An empty list
  means all workspaces.** Every name must be an existing workspace (`check` rejects an unknown one).
- **An agent never manages capabilities** (40, decision 0). Nothing here is writable over MCP: the
  setting operations stay `mcp="never"`.
- **MCP token management is internal.** No token operations here.
- **The MCP, the console and the CLI all use the API.** No MCP-only path.

## The `[mcp]` section

```toml
[mcp]
workspaces = ["default"]  # only these (names from [workspaces.*]); [] = every workspace

[mcp.allowed]             # from 40: operation or plugin name -> true | false
```

`allow_send` joins this section when `send_message` exists (CORE_28). It is not added now.

## Requirements

1. **`McpSettings.workspaces: list[str] = ["default"]`.** The first-run config writer (40's
   generated menu) emits it, commented, with the `[]` meaning explained. Validated on load and by `check_settings("mcp")`
   (the generic section operations from 29; no `get/set_mcp_config`). An unknown workspace name is
   an error that names it. On load, an unknown name is a warning and is skipped, as 40 does for
   `[mcp.allowed]`: a typo must not stop a headless start. The writers stay `mcp="never"`.
   *Check first:* whether `set_settings` already accepts an `mcp` section. 29 is only partly built.
2. **Scoping by account.** When `workspaces` is non-empty, an MCP call that returns or reasons over
   mail text is limited to mail whose account is in a listed workspace:
   - A single-message call (`get_message`, `get_thread`, `summarize_message`, `get_enrichment`)
     for a message outside the list is refused `not_exposed`. The error names no mail.
   - A listing or search call filters out mail from unlisted workspaces. It is never refused, and
     the totals do not count the hidden mail.
   - Calls that touch no mail (`status`, `list_plugins`, `list_accounts` without mail text) are
     unaffected.
   Enforcement is in core's MCP layer, not the client, and covers `/mcp` and `/mcp/agent`.
   Resolve a message's workspace through **one function** (`workspace_of(message)`), today a lookup
   of its account's `workspace` setting (`Workspaces.for_account`). CORE_36 plans routing a message
   to a workspace by Sieve rule; when that lands, only this function changes and the scoping with
   it. Do not call `for_account` from the MCP layer directly.
3. **Q4 is reopened by this.** The first draft gated only model-reaching calls. That leaves
   `get_message` free to return a secure workspace's mail text to any client. The filter above gates
   every operation that returns mail text. See the open question.
4. **Status.** `status` and the MCP `initialize` instructions report which workspaces the MCP can
   use, never mail. When a listed or implied workspace is `secure`, the status line says so (Q3).
5. **`list_operations`** keeps `mcp_allowed`, `mcp_default`, `mcp_enabled`, and gains
   `mcp_locked_reason` (`"ceiling"` or `null`) so the console can explain a locked row.
6. **Live reload.** A saved `[mcp]` applies to the running MCP server without a restart (23's
   reload): an in-flight call is not cut off. Whether a client that cached `tools/list` gets
   `tools/list_changed` depends on what the server supports. Spike that first; otherwise it sees
   the change on its next `tools/list`. `[api] mcp` (mounting `/mcp`) still needs a restart, and the
   console says so.
7. **CLI.** `oaken settings get|check|set mcp` (29's verbs). `oaken operations enable|disable NAME
   [--plugin]` and `oaken operations reset NAME` wrap `set_operation_exposure` (**not** `oaken
   mcp`, the stdio bridge). `oaken operations` lists with the locked reason. Fix the bridge's
   misleading "core is not running" when MCP is simply off: say "MCP is not enabled on this core
   (set `[api] mcp = true`)". Add rows to 17's table. This item is independent of the rest and may
   ship alone.
8. **Console (`/stats`, Reference tab).** Core's console is server-rendered and has no operation of
   its own; it calls the same operations.
   - A **status line**: is the MCP mounted, where it listens, which workspaces it may use, with each
     workspace's trust shown, and a warning on a `secure` one.
   - A **workspace picker** that adds or removes names in `workspaces`, with an "All workspaces"
     choice that writes `[]`, listing workspaces from `list_workspaces`. It writes through
     `set_settings("mcp")`, so the file and the page never disagree.
   - Locked rows on the existing operation list show their reason.
   - No textarea, presets or Test/Save/Revert flow: `/stats` has no such pattern. Add them only if a
     later prompt asks.

## Phase 2: exposure per workspace (decided, not built)

Commands can be turned on and off **per workspace**: `list_messages` in one, not another; send in
one, not another. Same file, same `[mcp.allowed]` shape, one level down:

```toml
[mcp.allowed]                     # the default for every workspace
get_message = true
send_message = false

[mcp.workspace.work.allowed]      # overrides for one workspace
send_message = true
list_messages = false
```

- **Resolution:** the code ceiling first (`never` cannot be lifted), then the workspace's own line
  for the operation, then its plugin line, then the global operation line, the global plugin line
  and the default. A workspace line may widen as well as narrow, up to the ceiling.
- **Enforcement** is where `McpScope` already sits: a call is checked against the workspace of the
  mail it names (`workspace_of`). A call that names no mail (`status`, `list_plugins`) uses the
  global lines only. A call that cannot be narrowed to one workspace is refused if any reachable
  workspace denies it.
- **`tools/list`** offers an operation if any reachable workspace allows it.
- Surfaces: `oaken mcp expose NAME on|off --workspace W`, the `/stats` Reference tab as an
  operations-by-workspace matrix, and `list_operations` reporting the effective value per workspace.
- Needs `[mcp] workspaces` (built) and, for send, CORE_28. Open question: whether a workspace may
  list a plugin's operations as a group (default: yes, as the global table does).

## Split out (not in this prompt)

- **Send.** `send_message` does not exist (CORE_28 is Open). `allow_send`, the Locked
  "allow_send is false" reason and the send acceptance criteria wait for it. When 28 lands it adds
  `[mcp] allow_send` and amends ADR 0004. The registry's refusal of `send` with `mcp="on"` is
  unchanged.
- **Presets and a textarea for `[mcp]`.** A convenience, not needed for the control.

## Open questions (defaults in bold)

- Q1: per-operation exposure moves into `[mcp] enable`/`disable`: **no.** 40's `[mcp.allowed]` is
  the one shape; this prompt adds only `workspaces`.
- Q2: **there is no per-call workspace.** The workspace is the mail's account's. (The first draft's
  rule is withdrawn.)
- Q3: a `secure` workspace in scope: **warn only for `[]` (every workspace), on the status line and
  in `check`; do not block.** `default` is itself `secure`, so warning on every listed secure
  workspace would warn on every install. An operator who lists one has chosen it.
- Q4: does `workspaces` gate read operations that return mail text: **yes** (requirement 2). This
  reverses the first draft's answer.
- Q5: the default. **`["default"]`, decided.** Not fail-open: `[]` (all workspaces) is an explicit
  choice, and `check` warns when it or a listed workspace is `secure` (Q3).

## Acceptance criteria

- `get` → edit → `check` → `set` → `get` round-trips `[mcp] workspaces`; an unknown workspace is
  rejected with a field error and nothing is persisted on a failed check.
- A fresh config serves only `default`. With `workspaces = []` the MCP serves every workspace. With `["a"]`, `get_message` for mail in
  workspace `b` is refused `not_exposed`, and one in `a` succeeds. A search returns only `a`'s mail
  and its totals exclude `b`'s.
- The scoping holds on `/mcp` and on `/mcp/agent`.
- A saved `[mcp]` takes effect without a restart; an in-flight call completes.
- A `secure` workspace in scope shows the warning on the status line and in `check`.
- The bridge says "MCP is not enabled" instead of "core is not running" when MCP is off.
- `set_settings` and `set_operation_exposure` are not in the MCP tool set.
- The parity test from 22 covers the new operations for CLI and console.
- `ruff`, `ruff format --check`, `mypy`, `pytest` and the docs check are green.

## Report-back checkpoint

End with the six sections from `00-context.md`. Then stop.
