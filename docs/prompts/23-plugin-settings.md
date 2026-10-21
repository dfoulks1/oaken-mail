# 23 — Plugin settings: load, test, save, enable

**Read first:** `docs/prompts/00-context.md` and `docs/prompts/21b-webui-client-foundation.md`.
**Depends on:** 04, 13, 16, 17, 21, 21b, 22.
**Related, split out on purpose:** workspaces are **31**, MCP settings are **32**. This prompt is
plugins only. It also builds the **shared config-section writer** that 29, 31 and 32 reuse.

## Decisions already taken (the user's answers)

- **Each plugin gets its own section** on Settings → Plugins: a header (name, kind, health, Enabled
  switch) and below it that plugin's slice of `config.toml` in a **TOML textarea**. **No
  schema-rendered form in this phase** (no `Oaken.form.fromSchema` here). The textarea is Load /
  Test / Save. A form renderer can come later without changing the operations, because the schema
  is still returned.
- **Secrets get special handling** and never appear in the textarea (see "Secrets").
- **Enable/disable** is a first-class switch, backed by an `enabled` attribute on the plugin base
  class that the loader reads (see "Enabled").
- **One source of truth:** `config.toml`. The UI and the CLI both go through the API operations
  below; nothing keeps a second copy of plugin settings.
- Extensibility is a founding principle: the plugin contract gains `enabled` and a secret marker,
  both optional, so a plugin that ignores them still works.

Who this is for: most users will find configuration the hardest part of Oaken, and a minority
write the plugins the rest use. The UI must be in the browser, per plugin, and relevant (only that
plugin's keys); nothing here may require editing a file on disk.

## Goal

Configure a plugin from a client (UI and CLI) with a **load → test → save** flow, mirroring the
account pattern (`check_account` vs `set_account_config`) and the Sieve pattern (`sieve_check` /
`sieve_test` vs a stored script). Saving writes just that plugin's `[plugins.<name>]` table back to
`config.toml` and refreshes the plugin.

## Decisions to make in this prompt (record an ADR)

- **Persistence target.** `config.toml` stays the source of truth: a scoped read-modify-write of one
  named table, preserving comments and the rest of the file, atomic and owner-only. Build it as a
  **generic section writer** (`write_section(path_in_file, text)`), not a plugin-only one: 29 (core
  sections), 31 (`[workspaces.<name>]`) and 32 (`[mcp]`) call the same code. (If the database proves
  clearly better, as it did for the sync schedule, record why instead.)
- **Live settings reload.** `Settings` is frozen and built once. Decide how a saved section reaches
  running code (re-load and swap a settings holder, notify subscribers) so that plugin refreshes,
  and later 31/32, take effect without a restart where safe. Say which keys need a restart.
- **Reload semantics for a plugin.** Stop that instance (quiescing in-flight pipeline/sync use),
  re-read merged config, start, re-health-check. A plugin that cannot hot-reload safely reports
  "saved; effective on next daemon restart". Record which plugins get which.
- **Enabled semantics** (below), including what a disabled plugin that config selects does.

## Enabled (loader change)

- Add `enabled: ClassVar[bool] = True` to `_Base` (`src/oaken/plugins/base.py`). It is the plugin
  author's default (a shipped-off experimental plugin sets it `False`). Snake case, matching
  `supports_folders`. `contracts.py` and `validate.py` treat it as optional, so a plugin that does
  not use the base classes still works (missing means `True`).
- **Effective value** = the user's `enabled` key in `[plugins.<name>]` if present, else the class
  default. The switch writes that key; the key also shows in the textarea.
- `PluginHost._load_one` still imports, instantiates and validates every plugin (so a disabled one
  can be listed and enabled without a restart). `start()` and the health loop **skip** a disabled
  record; it is reported `enabled:false, started:false, healthy:false, detail:"disabled"`.
  Toggling live calls `_start_one` or `stop` on that one record and announces the change.
- `get()`, `enrichers()`, `named_enrichers()`, `providers_with()` and the other lookups never return
  a disabled plugin. A disabled plugin that config **selects** (`sync.provider`, `ai.embedder`,
  `ai.llm_backend`, `ai.vector_store`) fails with a clear "X is disabled" error, not "not started".
- Per-account instances (`gmail@work`) follow their base plugin's enabled state.
- `PluginStatus` and `list_plugins` gain `enabled` and `enabled_default`.

## Requirements (operations first, per prompt 22's rule)

1. **`get_plugin_config(name)`** — `read`. Returns, for this plugin only:
   - `toml`: the text of its `[plugins.<name>]` table **excluding secret fields** (an empty table
     gives a short commented template built from the schema defaults);
   - `enabled` (effective) and `enabled_default`;
   - `schema`: the settings JSON Schema from `config_model` (returned, not rendered yet);
   - `secrets`: `[{field, set: bool}]`, one per secret-typed field, never a value;
   - `overridden`: keys currently overridden by `OAKEN_*` env.
   Never returns the rest of the file, other plugins, or secret values.
2. **`check_plugin_config(name, toml)`** — `read`, **persists nothing**. Parses the TOML (syntax
   errors with line), rejects any secret-typed key in the text, validates against the schema
   (field-level errors), and where the plugin supports it starts a **throwaway instance** with the
   proposed config and runs its **health probe**. Returns `{valid, errors[{line?,field?,message}],
   health, probed}`. A valid config whose probe fails returns `valid:true, health:unhealthy`.
3. **`set_plugin_config(name, toml, enabled?)`** — `mutate`, confirm-gated, audited. Re-runs the
   `check` validation, writes the table (scoped, comment-preserving), and refreshes the plugin per
   the reload decision. `enabled` may be passed alone (the switch). Audit records name and changed
   keys, never secret values, never mail.
4. **Secrets.** The textarea never contains one. Each secret-typed field in the schema
   (`json_schema_extra={"secret": True}`; reuse core's markers if any) gets a **write-only row below
   the textarea**: status ("stored" / "not set"), a password input, **Set** and **Clear**. They use
   **`set_plugin_secret(name, field, value)`** and **`clear_plugin_secret(name, field)`** —
   `mutate`, `mcp="never"`, audited with name+field only — storing to the OS keyring under a
   plugin-scoped service key, like account passwords. The value is never returned, logged, audited,
   or kept in the browser after submit. A test proves no secret value reaches `config.toml`, the
   audit log, the event stream or logs, and that `check` refuses a secret in the TOML text.
5. **MCP ceiling for these operations.** `get` and `check` are `mcp="off"`; `set_plugin_config`,
   `set_plugin_secret`, `clear_plugin_secret` are `mcp="never"`.
6. **CLI:** `oaken plugins get NAME`, `oaken plugins check NAME [--file F | --toml '…']`,
   `oaken plugins set NAME [--file F] [--enable|--disable]`, `oaken plugins secret set|clear NAME
   FIELD` (value from stdin or a hidden prompt, never argv). Add rows to 17's table.
7. **UI** (Settings → Plugins; one section per plugin from `list_plugins`):
   - Header: name, kind, version, capabilities, health dot with `detail`, the **Enabled switch**
     (`set_plugin_config {enabled}`, showing the live start/stop outcome).
   - Body: the TOML **textarea** pre-filled by `get_plugin_config.toml` (monospace, never kept in
     `localStorage`), buttons **Test** (`check`: valid/errors with the line marked, health,
     "nothing saved"), **Save** (`set` with `confirm:true`; shows the reload outcome), **Revert**.
     Dirty-state guard on leaving. An `overridden` notice lists env-overridden keys.
   - **Secrets rows** below the textarea, per item 4.
   - A read-only line **"MCP access: N of M operations exposed"** linking to Settings → MCP (32),
     which owns those toggles. Hidden for a plugin that offers no operations.
   - Per-account instances (`<plugin>@<account>`) show once under their plugin; an account's own
     settings belong to 24.
   - The `ollama` plugin's section lives under **Settings → AI & Models** with the same widgets;
     the token-thrift toggles and top-k are core settings built in 29.
   - Elements carry `data-oaken-requires="…"` (21b C5).
8. A comment-preserving TOML writer is allowed as a new dependency (Apache-compatible, non-GPL;
   `tomlkit` is MIT); justify it in the report.

## Open questions (defaults in bold)

- Q1: disabling a provider that accounts use: **allowed, with a warning that those accounts stop
  syncing** (blocking is the alternative).
- Q2: disabling an enricher: its cached results are **kept** (not refreshed, not purged).
- Q3: the textarea shows the **effective** table, defaults as comments; keys the plugin does not
  know are kept and flagged as warnings by `check`, not rejected. **Warn, not reject.**

## Acceptance criteria

- Round-trip test: `get` → edit → `check` (valid + health) → `set` → `get` shows the change; a
  second plugin's table and the file's comments are untouched; an `OAKEN_*` override is reported.
- `check` with bad TOML or a schema violation returns line/field errors and persists nothing; with
  a valid config that fails a live probe it returns `valid:true, health:unhealthy`.
- The section writer is generic and covered by its own tests (another table and comments untouched,
  atomic, owner-only, no partial file on failure).
- **Enabled:** a plugin whose class says `enabled = False` loads but does not start; a user key
  turns it on live and `false` turns a running one off live, with `list_plugins`, health events and
  the lookups agreeing; a selected-but-disabled plugin gives the "disabled" error; account
  instances follow; a plugin not using the base classes still loads (missing means enabled).
- **Secrets:** no secret value reaches `config.toml`, audit, events or logs; `check` rejects a
  secret in the text; a set secret is reported only as `set:true` and the plugin reads it from the
  keyring at start.
- The parity test from 22 covers these operations for both clients.
- `ruff`, `ruff format --check`, `mypy`, `pytest` and the docs check are green; ADR added.

## Report-back checkpoint

End with the six sections from `00-context.md`. Then stop.
