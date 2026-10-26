# CORE_44 — Workspace ops: close the save-path confidentiality gap, and two smaller fixes

**Read first:** `docs/prompts/00-context.md`, `docs/prompts/CORE_31-workspace-management.md` (the
feature), `docs/prompts/CORE_10` / `src/oaken/ai/workspaces.py` (`check_backend`, the confidentiality
rule). **Depends on:** 31 (built). **Track:** CORE. **Touches:**
`src/oaken/core/operations/builtin/workspaces.py` and its test
(`src/oaken/core/operations/builtin/tests/test_workspace_ops.py`) only.

> **Status: built.** `_verdict` is the one validator; `check_workspace` reports it and `set_workspace`
> obeys it (`SetWorkspaceOutput` gained `errors`). `_count_cache` returns `None` on `StorageError`
> and lets other errors surface (`WorkspaceUsage.cache_entries` is `int | None`; the CLI prints `?`).
> Gap: a secure workspace whose backend is not loaded cannot be judged and still saves; the call-time
> `check_backend` is the backstop.

> This fixes code that already shipped (CORE_31's ops). It adds no feature. Three findings from an
> audit of the live `workspaces.py`; the first has teeth, the other two are a silent wrong number
> and dead weight.

## Why

`set_workspace` is the one write path for a workspace's `trust`/`backend`. Its whole job includes a
confidentiality guarantee, a `secure` workspace never points at a remote backend, so a `secure`
workspace's mail never reaches a remote model. The audit found the save path does not enforce it:
`set_workspace` validates only the `WorkspaceSettings` shape (`_validate_name` +
`WorkspaceSettings.model_validate`). The real gate, `check_backend` (`ws.secure and where !=
"local"` → `PermissionDenied`), lives only in `check_workspace`, which is advisory and persists
nothing. So a `secure` workspace with a remote backend saves cleanly if the caller (or the UI)
skips Test. A guarantee that only fires when you remember to click Test is not a guarantee.

## Decisions already taken (do not re-litigate)

- **The gate belongs at `set`, not only at `check`.** `set_workspace` must run the same validation
  `check_workspace` runs and refuse on `valid:false`. (Drew's ruling in the CORE_31 refinement.)
- **`check_workspace` stays `mcp="never"`.** Unchanged.
- **One validation, reused.** `set` calls the check logic rather than rolling a second, lossy copy;
  the current `except (ConfigError, Exception)` in `set` loses the field-level detail `check`
  produces. Fold them.

## Not in scope

- The Settings → Workspaces UI pane (CORE_31, waits on 32).
- `used_by.mcp` / `effective_backend` on `list_workspaces`, and the refuse-a-workspace-MCP-names
  guard on `remove` (all CORE_32).
- Any change to `check_backend` itself or to the cache key (confidentiality enforcement in core is
  correct; this is only about the *save path* calling it).

## Requirements

1. **Fix 1 (security): `set_workspace` enforces the confidentiality rule.** Before writing,
   `set_workspace` runs the Requirement-3 validation of CORE_31 (name, `WorkspaceSettings`, and
   `check_backend` for a `secure` workspace against its effective backend) and returns
   `ok:false` with the field error on `valid:false`, writing nothing. The cleanest form is to call
   the existing `check_workspace` logic (extract a plain helper both ops call, so there is one
   validator). A `secure` + non-local-backend config is refused at `set` whether or not `check` was
   called first. Keep the field-level error detail (drop the `except (ConfigError, Exception)`
   catch-all that flattens it).
2. **Fix 2 (silent wrong number): `_count_cache` must not hide a changed cache surface.** It calls
   `ctx.svc.cache().stats(name)` positionally and swallows every exception to `return 0`, so if the
   pluggable-cache surface (PLUGIN_43: `CacheOps.cache()`, `stats(workspace=…)`, `CacheStats.entries`)
   shifts, `cache_entries` reads 0 forever with no sign. Call `stats(workspace=name)` by keyword,
   and narrow the `except` to the specific failure that means "no cache yet" (a missing table / no
   cache configured), letting anything else surface. If the number cannot be had cheaply, omit it
   rather than report a false 0 (make `cache_entries` `int | None` and leave it `None`), so a wrong
   zero is never shown as truth.
3. **Fix 3 (lean): drop dead weight.** Remove `_parse_ws_toml`'s second parse attempt (the
   wrap-in-a-header fallback) once the first path is confirmed to accept the snippet form the CLI
   and UI send; one parse, one error. Replace `_pydantic_errors`' `getattr(exc, "errors", callable)`
   reflection with a typed `except ValidationError` (the only thing `model_validate` raises here).
   Neither changes behaviour; both remove a branch that cannot be reached.

## Acceptance criteria

- **The owed security test:** `set_workspace(name, 'trust = "secure"\nllm_backend = "<a remote
  backend>"')`, with **no prior `check_workspace` call**, returns `ok:false` with an error naming the
  backend/locality, and `config.toml` is unchanged (the table is not written). The matching
  `secure` + local backend, and `insecure` + remote, both save.
- `set_workspace` and `check_workspace` give the same verdict for the same input (one validator).
- A schema or name error from `set` still carries the field/key (no flattening to a bare string).
- `_count_cache`: with a working cache, `cache_entries` is the real count; with the cache surface
  absent or raising its "not configured" error, `cache_entries` is `None` (or omitted), never a
  false `0`; any other exception is not swallowed.
- The existing 20 workspace tests still pass; `ruff`, `ruff format --check`, `mypy`, `pytest` and
  the docs check are green.

## Open questions (defaults in bold)

- Q1: extract the validator as a module helper both ops call, or have `set` call `check_workspace`'s
  handler directly? **A plain helper** (`_validate_candidate(ctx, inp) -> CheckWorkspaceOutput`), so
  neither op depends on the other's operation wrapper.
- Q2: `cache_entries` on failure, **`None`** (honest "unknown") over `0` (a lie). If a client needs a
  number, it shows "—".

## Report-back checkpoint

End with the six sections from `00-context.md`. Then stop.
