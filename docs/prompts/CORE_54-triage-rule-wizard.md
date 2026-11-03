# 54 — The triage rule wizard: in the CLI and in the console

**Status: Built** 2026-11-03 (written first, then built): `PluginHost.trial`, `Pipeline.recent_inputs`,
`triage_rule_test`, the CLI wizard and `oaken rule test`, and the rules in the `rules` plugin's settings dialog and a new-rule page (`/triage/rules/new`). Not built: dragging.
**Read first:** `00-context.md`, `docs/adr/0015-database-backed-triage-rules.md` (the rules, the
working copy, the snapshot, staging, drift), `CORE_50-console-without-script.md` (no script: forms,
redirects, `popover`), `CORE_52-console-by-stage.md` (the Triage page), ADR 0013 (the console shows
counts and health, never mail text).
**Depends on:** 47, 50, 52 and ADR 0015's backend (built: store, operations, `oaken rule`).
**Track:** CORE + CLI. Touches `src/oaken/plugins/host.py`, `src/oaken/ingest/pipeline.py`, one new
operation, `apps/cli/src/oaken_cli/commands/triage_cmd.py` and `src/oaken/api/` (`console.py`,
`app.py`, `templates/stage.html`).

## Why

`oaken rule add` asks three questions and stops. A rule is easy to get wrong (a glob that matches
everything, a condition that matches nothing) and the person finds out when mail is rated badly. The
rules also need a home in the console, where the owner reads status and where reordering is a
button, not a command.

## Goal

1. **A dry run**: one operation, `triage_rule_test`, takes a candidate rule (its `when`) and says how
   many of the most recent stored messages it would match, plus up to five message ids. It stores
   nothing, records no usage and asks no model. It never returns mail text, so the console can show
   it (ADR 0013).
2. **A real wizard in the CLI**: `oaken rule add NAME` in a terminal walks through the rule:
   - a condition menu (numbered): sender, recipient, subject, subject contains, body contains,
     header present, has attachment, is a reply; one value per pick, booleans as yes/no; the list so
     far is shown after each; a blank pick finishes;
   - the level (the defined levels are listed) and the category (the seven, listed);
   - **the test**: "matches 12 of 200 recent messages (ids: ...)", and a warning when it matches
     none or more than half;
   - a confirm: stage it, change a condition, or cancel.
   With flags it stays one command for scripts. `oaken rule test --when ...` runs the dry run alone.
3. **The same process in the console**, in the `rules` plugin's dialog (your rules, staged and drift bars, levels) and a new-rule page, with no script:
   - **Your rules**, in the order they run: name, level, category, conditions, on/off. Each row has
     buttons: up, down, turn on or off, delete. (Dragging needs script; up and down do the same.)
   - **Add a rule**: a form with the level (a select of the defined levels), the category (a
     select), and one input per condition (blank means unused), and two buttons: **Test** (shows
     the counts in the page's message and keeps what was typed) and **Add**. A rule with no
     condition is refused.
   - **Levels**: the defined levels, and a form to add one (a number from 4 and a name).
   - **State bars** at the top of the section: *N edits staged* with **Apply** and **Discard**
     (Apply is the same as the CLI's `oaken rule apply`), and *config.toml was edited by hand* with
     the diff, **Accept** and **Revert**.
   Every button posts to `/ui/<action>` and runs the same operation the CLI runs.

## Decisions already taken (do not re-litigate)

1. **Counts and ids only.** The dry run and the console never show subjects, senders or bodies.
   (ADR 0013; the CLI user can `oaken show ID`.)
2. **The test runs the real matcher**, so what it says is what the triager will do. It starts a
   throwaway instance of the `rules` plugin on the candidate (`PluginHost.trial`), which is internal
   to the host and not part of the plugin contract.
3. **The wizard stages, never writes.** It calls `triage_rule_set`; the file is written by Apply or
   a reload (ADR 0015, decision 2a).
4. **No script in the console** (CORE_50). Reordering is up/down. A form keeps what the person typed
   when Test is pressed by posting back with the values in the query string of the redirect, bounded
   and never holding more than the form's own fields.
5. **Dry run sample:** the newest 200 stored messages by default, at most 1000.

## Not in scope

- Dragging (needs script), editing a rule's conditions in place (delete and add; or the file).
- Python or sieve rules; labels and automations (CORE_51).
- A rule's position in the add form (it goes last; move it with the buttons).

## Tests

- Store/host: `trial` starts, matches and stops a plugin on a candidate and leaves the running
  one alone; a candidate the plugin refuses raises.
- Operation: counts match a hand count over fixture mail; no mail text in the output; never over MCP.
- CLI: the wizard end to end with scripted answers (conditions, level, category, test, confirm,
  cancel, change), the none-match and most-match warnings, and `rule test`.
- Console: the `rules` plugin dialog shows the rules in order, the staged and drift bars, forms carry the CSRF
  token, each action runs its operation (add, test, up, down, on/off, delete, apply, discard, accept,
  revert, level add), a refused rule shows its reason, and the page has no `<script>` of its own.

## Report-back checkpoint

End with the six sections from `00-context.md`. Then stop.
