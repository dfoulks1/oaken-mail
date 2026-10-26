# 40 — `[mcp.allowed]`: MCP exposure in config.toml, seeded to the store, surfaced in the UI

**Read first:** `docs/prompts/00-context.md`, then `CORE_13-operation-registry.md` (operations, the
`mcp` ceiling/default), `MCP_18-mcp-shim.md` (what MCP exposes), `CORE_29-system-settings.md` and
`CORE_23-plugin-settings.md` (settings and the comment-preserving `config_edit`).
**Depends on:** 13, 16, 18, 29. Nothing unbuilt.
**Track:** `CORE_`. Touches core settings, the exposure store, the operation that sets exposure, the
first-run config writer, and core's `/stats` console page. It does **not** change any plugin, and it
does **not** widen the code ceiling (`mcp="never"` stays immovable).

## Goal

Control which operations MCP clients may call **from `config.toml`**, so Oaken runs headless with no
UI: an operator edits `[mcp.allowed]`, reloads (SIGHUP), and the change takes effect. The file is the
source of truth; the DB table is the live-read cache the resolver already uses. The `/stats` →
Reference page gains a Bootstrap switch per operation that writes the same file on a flip, so the UI
and the file never disagree. The eventual aim is that the UI is a convenience, never required.

## Decisions already taken (do not re-litigate)

Settled with the maintainer (2026-10-26), grounded in the current code:

0. **An agent may never manage capabilities — its own or any.** The only two doors to changing MCP
   exposure are the **config file** (human-edited) and the **UI** (`/stats`, loopback). No MCP tool
   or agent route may widen exposure; `set_operation_exposure` is `mcp="never"` for exactly this.
   Config-as-truth exists *because of* this rule: it puts capability control outside the agents'
   reach. Caveat, stated plainly: `mcp="never"` closes the MCP door, not the filesystem door — an
   agent with write access to `config.toml` would walk around it. If agents can reach the config
   path, the file must be protected (owner-only, daemon-enforced; see 41's open question on this).
1. **`config.toml` is the source of truth; file wins on conflict.** On load/reload the exposure
   store is seeded from `[mcp.allowed]`. A hand-edit to the file, after reload, is authoritative.
   The DB `operation_settings` table stays as the thing the resolver reads live (`effective()`),
   populated *from* config on load and *from* the set-op on a UI/CLI change.
2. **Key is `[mcp.allowed]`, a flat table.** Keys are an **operation name** (`op:` scope) or a
   **plugin name** (`plugin:` scope — every operation that plugin offers). A plugin's own operations
   are already named `<plugin>.<verb>`, so `"gmail.doctor" = false` is per-plugin-operation control
   with no new scope. `true` = allowed, `false` = denied.
3. **Precedence: an operation's own line beats its plugin's line.** This already falls out of
   `ExposureStore.effective()`: ceiling → `op:` row → `plugin:` row → code default. So
   `gmail = true` with `"gmail.doctor" = false` means "all of gmail's ops except doctor." No resolver
   change is needed for whole-plugin-then-deny-piecemeal; it already works.
4. **The code ceiling keeps its name.** The registry's `mcp_allowed` (the immovable floor,
   `mcp != "never"`) and `mcp_exposed` (the code default) are unchanged. The config word `allowed`
   names the **user-choice** layer, not the ceiling. A one-line comment in settings states this so
   the two "allowed"s are not confused. (No rename; that is scope creep.)
5. **Unknown key: warn and ignore.** A key naming an operation or plugin that does not exist is
   logged as a warning and skipped. The daemon starts. A one-character typo in a headless file must
   never brick startup.
6. **First-run writes a generated, commented menu** ("uncomment and configure"). Every *togglable*
   operation (so **not** the `never` ones) is listed commented at its default, generated from the
   registry (via `list_operations`), so it cannot drift. The same generator is exposed as a refresh
   command for after a plugin is added.

## Not in scope

- **Widening the ceiling.** `mcp="never"` operations (send, raw access, config, and
  `set_operation_exposure` itself) are omitted from the menu and ignored if named. Nothing here lets
  an agent or a config file reach them.
- **Per-account / per-provider visibility** ("hide list_messages while working gmail mail"). An
  operation name is globally unique; there is one `list_messages`. Provider-scoped visibility is a
  different feature and is not this.
- **A second store.** The DB table is not a competing source of truth; it is the seeded cache. No
  new table, no new scope. Reuse `operation_settings` and `effective()`.
- **Auth on `/stats`.** The page stays loopback-only and token-free, exactly as `/config` already
  mutates on loopback. This prompt does not add authentication; it follows the `/config` precedent.

## The seed-vs-audit reconciliation (the one subtle part)

Today every exposure change goes through `set_operation_exposure`, which writes an **audit** row
(who, when, why). Config-as-truth introduces a change with no actor: the file. The rule:

- **Seeding from config on load writes the store rows but records no audit** (there is no actor; it
  is configuration, not an action). Seeding is idempotent and replaces the config-derived rows.
- **`set_operation_exposure` (UI/CLI) audits as it does now, and additionally writes the change back
  to `[mcp.allowed]` in `config.toml`** via `config_edit` (the comment-preserving path `config_save`
  uses: read text → edit the one table → validate through `Settings` → write). Setting a value back
  to its code default removes the key (as `mcp=None` clears a row today), keeping the file to only
  the operator's deviations.
- **Consequence, stated plainly:** the audit log records changes made *through the operation*, not
  hand-edits to the file. With the file authoritative, the audit is no longer the complete history
  of exposure state. This is accepted; it is inherent to "the file can win."

## Requirements

Operations and settings first, then persistence, then the UI.

1. **Settings.** Add `allowed: dict[str, bool]` to an `[mcp]` settings section (new
   `McpSettings`, or the existing MCP keys' home). Validation is lenient: unknown keys warn and are
   dropped (requirement 5 of Decisions). Document the key with the `allowed`-is-the-choice-layer note.
2. **Seed on load.** Where `CoreClient` builds the `ExposureStore` (`client.py`), seed the store
   from `settings.mcp.allowed`: write an `op:`/`plugin:` row per valid key, no audit. Re-seeding on
   reload (core is rebuilt on SIGHUP) must converge, config-derived rows reflect the file exactly,
   and a key removed from the file is removed from the config-derived rows on next load.
3. **Op writes back to the file.** `set_operation_exposure` also edits `[mcp.allowed]` in
   `config.toml` through `config_edit`, validates the whole file via `Settings`, then writes. Clear
   a key when the choice returns to the code default. A file that cannot be written is an error the
   caller sees; the DB change does not silently diverge from the file.
4. **First-run generated menu.** The first-run `config.toml` writer emits `[mcp.allowed]` as a
   commented menu of every togglable operation at its default, generated from the registry (never a
   hand-kept list). Omit `never` operations. Include the precedence note and the refresh command.
5. **Refresh command.** A command (e.g. `oaken mcp allowed --scaffold`, exact spelling the
   implementer's call) regenerates the commented menu from the live registry into the file, for use
   after a plugin adds operations. It must not disturb uncommented (active) choices.
6. **UI toggle on `/stats` → Reference — reuse the Plugins-tab switch pattern.** The exact widget
   already ships in `stats.js` for plugin enable/disable: a Bootstrap switch
   (`.form-check.form-switch`) marked `data-op-switch="<op name>"`, driven by the file's one delegated
   `change` listener (`document.addEventListener("change", …)` matching `closest("[data-op-switch]")`),
   which POSTs same-origin and, **on failure, flips the switch back** (`box.checked = !box.checked`),
   exactly as the plugin switch does. Copy that pattern, do not invent a new one; no per-element
   inline handler, one delegated listener. The toggle acts **immediately** (like the plugin switch),
   there is no staged set and no separate Apply button, a flip is the action. `never` rows render a
   disabled, greyed switch (the `mcp_allowed=false` ceiling, already in the page data). The POST
   lands on a core route that runs `set_operation_exposure` (internal caller, `confirm=true`), which
   audits, writes the file, and updates the live resolve; the handler reports success/failure in the
   same result banner the plugin switch uses. Exposure takes effect on the agent's next tool-list
   call (`_list_tools` reads `visible()` live), so no reload is needed, the switch's own tooltip or
   the footer says so. Update the page's "read-only" footer to say exposure can be changed here.

## Acceptance criteria

Each a test that passes or fails; include the refusals and what must never happen.

- A `config.toml` with `[mcp.allowed]` `list_messages = false` → after load, an MCP `list_tools`
  omits `list_messages`; `ask` (default off) stays off unless `ask = true` is set.
- `gmail = true` with `"gmail.doctor" = false` → every gmail op is exposed **except** `gmail.doctor`
  (op line beats plugin line).
- A `never` operation named in `[mcp.allowed]` (e.g. `send_mail = true`) is **ignored**; `send_mail`
  is never in the tool list. An unknown key (`lst_mesages = false`) logs a warning and does not stop
  startup.
- Seeding records **no audit row**; a change via `set_operation_exposure` records **one** audit row
  **and** the key appears in `[mcp.allowed]` in the file, with comments and other tables intact
  (assert `config_edit` left the rest of the file byte-for-byte except the one table).
- Setting an operation back to its code default removes its key from the file.
- Reload after editing the file (SIGHUP) changes the exposed set with no restart; two reloads with
  the same file are idempotent.
- `/stats` Reference renders a disabled, greyed control for a `never` op and an active toggle for an
  `on`/`off` op; flipping it writes the file and the next `list_tools` reflects it; a failed POST
  reverts the switch (the plugin-switch behaviour, reused).
- Secrets never enter `config.toml` through this path (exposure is booleans only; the
  `config_edit` secret guard still stands for the file as a whole).

## Open questions (defaults in bold)

1. Settings home: a new `[mcp]` section, or fold into the existing MCP api keys? **A new `[mcp]`
   section for the exposure map; it is a distinct concern from the api listener's `mcp_*` sizes.**
2. Refresh command spelling and whether it is MCP-reachable. **`oaken mcp allowed --scaffold`, and
   like `set_operation_exposure` it is `mcp="never"` — an agent cannot regenerate or widen its reach.**
3. File-write failure on a switch flip: hard error, or write DB and warn? **Hard error. The file is
   the truth; a change that cannot reach the file must not pretend to have landed (the switch
   reverts and the banner says why).**
4. Does the generated menu show plugin-group lines (`gmail = …`) as well as per-op lines? **Yes — a
   group line per plugin plus each plugin op, so both granularities are discoverable.**

## Report-back checkpoint

End with the six sections from `00-context.md`. Name every file changed; state explicitly that no
plugin was touched and that the `mcp="never"` ceiling was not widened; and show one `config.toml`
before/after proving `config_edit` preserved the surrounding comments and tables.
