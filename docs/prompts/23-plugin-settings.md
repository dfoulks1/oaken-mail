# 23 — Plugin settings: load, test, save

**Read first:** `docs/prompts/00-context.md`. **Depends on:** 04, 13, 16, 17, 21, 22.

## Goal

Configure plugins from a client (UI and CLI) with a **load → test → save** flow, mirroring the
account pattern (`check_account` vs `set_account_config`) and the Sieve pattern
(`sieve_check`/`sieve_test` vs a stored script). A client shows **only the fields relevant to that
plugin** — its own `[plugins.<name>]` table, nothing else in `config.toml` — rendered from the
plugin's declared settings schema. Saving writes just that table back and refreshes the plugin.

This is the prerequisite the user identified: accounts, folders, labels and the rest can only be
populated once plugins can be configured.

## Decisions to make in this prompt (record an ADR)

- **Persistence target.** Write plugin config back to `config.toml` (today's source of truth) — a
  scoped read-modify-write of just the `[plugins.<name>]` table, preserving comments and the rest
  of the file. (If, while building, the database proves clearly better — as it did for the sync
  schedule — record that choice and why in the ADR instead.)
- **Reload semantics.** Prefer a live per-plugin refresh (re-read merged config, restart that
  instance, re-health-check) that quiesces in-flight pipeline/sync use. If a given plugin cannot be
  hot-reloaded safely, the safe fallback is "saved; effective on next daemon restart", reported
  plainly to the client. Decide and record which plugins get which.

## Requirements (operations first, per prompt 22's rule)

1. **`get_plugin_config(name)`** — `read`. Returns the current `[plugins.<name>]` table
   (**non-secret only**), the plugin's settings **JSON Schema** (the host already exposes
   `config_model`; surface it), the `enabled` flag, and which fields are currently **overridden by
   `OAKEN_*` env** (so a client can show that a saved value may not be the live one). Returns only
   this plugin's slice — never the whole file, never other plugins, never secrets.
2. **`check_plugin_config(name, config)`** — `read`, **persists nothing**. Validates the proposed
   config against the schema (field-level errors). Where the plugin supports it, instantiate it
   with the proposed config in a throwaway context and run its **health probe** (e.g. Ollama URL
   reachable, IMAP/SMTP TLS+login) so the user learns it actually works before saving. Returns
   `{valid, errors[], health, probed: bool}`.
3. **`set_plugin_config(name, config)`** — `mutate`, confirm-gated, audited. Re-validates, writes
   the `[plugins.<name>]` table back (scoped, comment-preserving), and refreshes the plugin per the
   reload decision. `enabled = true|false` is a field here, so **enable/disable** falls out. Audit
   records name + changed keys (never secret values, never mail).
4. **Secrets stay out.** This path never reads or writes keyring secrets; those keep their
   account-level operations (`set_account_password`, etc.). A test enforces that the config written
   here contains no secret-typed fields.
5. **MCP ceiling.** `get`/`check` may be `mcp="off"` (user can enable); `set_plugin_config` is
   `mcp="never"` (configuring the host from a model is out of scope).
6. **CLI** verbs: `oaken plugins get NAME`, `oaken plugins check NAME --json '{…}'`,
   `oaken plugins set NAME --json '{…}' [--enable|--disable]`. Add rows to 17's table.
7. **UI**: a per-plugin settings form rendered **from the schema**, pre-filled by `get`, a **Test**
   button (`check`, shows valid/errors/health, nothing persisted) and a **Save** button (`set`,
   persists + reloads, shows the reload outcome). Only this plugin's fields are shown.
8. A comment-preserving TOML writer is allowed as a new dependency (Apache-compatible, non-GPL);
   justify it in the report.

## Acceptance criteria

- Round-trip test: `get` → edit → `check` (valid + health) → `set` → `get` shows the change;
  a second plugin's table and file comments are untouched; an `OAKEN_*` override is reported.
- `check` with an invalid config returns field-level errors and persists nothing; with a valid one
  that fails a live probe, returns `valid:true, health:unhealthy`.
- Enable/disable via `set` is reflected in `list_plugins` and takes effect per the reload decision.
- A test proves no secret-typed field is ever written through this path.
- The parity test from 22 covers these operations for both clients.
- `ruff`, `ruff format --check`, `mypy`, `pytest` and the docs check are green; ADR added.

## Report-back checkpoint

End with the six sections from `00-context.md`. Then stop.
