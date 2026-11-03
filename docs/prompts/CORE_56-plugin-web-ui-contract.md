# 56 — A web UI contract for plugins

**Status: Draft.** For the owner's review before any code. Lead: the top tier (a contract and a route
family); workers may take the renderer and the tests.
**Read first:** `00-context.md`, `docs/adr/0018-a-web-ui-contract-for-plugins.md` (the ruling),
`docs/adr/0013-read-only-stats-page.md`, `CORE_50-console-without-script.md` (no script, forms,
`popover`), `docs/adr/0012-plugin-extension-hooks.md`, `docs/bbq/2026-11-03-adr-webui-contract.md`.
**Depends on:** ADR 0018 ratified; CORE_55 (ADR 0017) built. **Blocks:** CORE_57 (ADR 0016).
**Track:** CORE. Touches `src/oaken/plugins/` (`base.py`, `contracts.py`, `host.py`, `validate.py`),
`src/oaken/api/` (`app.py`, `console.py`, `templates/`), `apps/cli/` and `docs/plugins.md`.

## Why

A plugin can add operations, jobs and settings, but not shape its console modal or serve a form, so
the rules wizard (ADR 0016) would be core code. The same plugin that extends everything else should
extend the console, with no edit to oaken-core.

## Goal

Each step lands green with its test. The owner runs ruff, mypy, pytest and the docs check (do not
run them); stage by path.

1. **The declaration types** in `contracts.py`: `UiSpec`, `UiSection`, `UiButton`, `UiPage`, `UiField`
   (string, number, boolean, enum, list of strings), frozen dataclasses. `BasePlugin.ui()` returns
   `UiSpec | None`. The per-field hint is `json_schema_extra={"ui": {...}}`. Test: the contract snapshot
   shows them; a plugin without `ui()` is unchanged.
2. **Validation at load** in `validate.py`: names, lengths, that every operation a button or page names
   is the plugin's own, that fields exist on the config model or the page. A bad `ui()` is logged and
   dropped, the plugin stays healthy (Q8). Test: raising and malformed `ui()` fall back.
3. **The settings modal** renders sections, labels, order, help and `show_if` from the hints; with no
   hints it renders exactly as today. Buttons post to one fixed route that runs the plugin's own
   operation (a `mutate` is confirmed). Test: a label with markup is escaped; a button cannot name
   another plugin's operation.
4. **Pages:** route `/plugins/<name>/<page>`, the `load` operation fills the form, Test calls `test`,
   Confirm calls `submit`. Core owns the chrome, CSRF, the CSP, escaping and length caps; the page is
   linked from the plugin's modal and its stage row. Test: no script in the page; CSRF refused; a
   page of an unloaded plugin is a 404.
5. **The CLI** walks a declared page when stdin is a terminal and required fields are missing; the
   same operations run by flags or `--json` unchanged. Test: scripted answers end to end.
6. **A sample plugin** in `docs/plugins.md`'s how-to and a conformance test over it, so a third party
   copies a working example. Regenerate the plugin contract and operation snapshots and the references.

## Not in this prompt

Masthead entries, panels, repeatable field groups, plugin HTML, and moving the template's special
cases (`rules`, `skip_rules`, `steps`) onto declarations. The rules wizard (CORE_57).

## Risks

- A plugin's text is untrusted input to the console: escape every string, cap every length, and
  never emit a plugin string as an attribute, URL or class.
- A button runs a plugin operation as the console's person: keep the registry's permission and
  confirm path; do not add a side door.
- Route names under `/plugins/` must not collide with core's pages; test the reserved names.

## Done when

The owner's checks pass, the sample plugin's page and modal render in the console with no edit to
oaken-core, and ADR 0018's Built line cites a test.
