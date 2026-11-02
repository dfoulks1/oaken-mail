# 51 — Pluggable triagers: custom labels, and rules that launch automations (DRAFT)

**Status:** draft. Harden it (questions, ADR) before anyone builds it.
**Depends on:** 47 (triage is its own kind; fan-out asks one predicate; `MessageTriaged` exists).
**Track:** CORE. Changes the plugin contract, so it needs an ADR first.

## Why

Triage today rates every message 0 to 3 and names a category from a fixed list. A triager is more
than that: it classifies, and a class can route work. A user should be able to add a triager for one
kind of mail (a build failure, a billing alert, a system notice), give it its own labels, and have a
label start an automation. oaken-mail is not only for enrichment.

## Goal

1. A triager is a plugin. It declares its labels and returns one or more of them per message, with
   or without a priority.
   Authors subclass `BaseTriager`, which extends the plugin base the way `BaseEnricher` does today
   (`_Base` -> `BasePlugin` -> `BaseTriager`; `BasePlugin` is the public name for what is `_Base` in
   `src/oaken/plugins/base.py`, so other plugin bases can share it). The built-in `triage` enricher
   becomes the first `BaseTriager`.
   Two ship by default, each its own plugin with its own config model: **rules** (deterministic,
   from `oaken_enrichers/triage_rules.py`; options such as which rules are on, the priority and
   category each gives, and user-added header or sender rules) and **ai** (a model; options such as
   the model, the prompt version and the body budget). Triagers run as an ordered chain: the first
   that settles a message wins, so the model is asked only for what the rules leave.
2. A rule maps a label (and optionally a priority) to an action: queue an enricher, queue any
   registered job kind (core's or a plugin's), or publish an event.
3. The fan-out in 47 uses the same rules. `min_priority` becomes a built-in rule.

4. Triagers and rules are operable from the CLI (list triagers and labels, list and test rules, run
   one triager on one message as a dry run), and the dry run is exposed over MCP (the TODO item
   "Expose triage for tuning" lands here).

5. A triager, and every plugin it uses, is easy to configure from outside the config file. Core
   already has `get_plugin_config`, `check_plugin_config` and `set_plugin_config`
   (`operations/builtin/config.py`, with a `config.toml` writer): build on them, and check that each
   plugin's declared config model drives the form. Each
   plugin declares a validated config model; one operation reads it (current values,
   defaults, help text) and one writes a change (validated, audited, with undo), generated from that
   model so a new triager gets the form and the command with no extra code. The CLI
   (`oaken plugins config <name> [key=value ...]`) and the console form call the same operations.
   Secrets go to the keyring, never to the config file or the audit row.

## Constraints (from `00-context.md`)

- Mail is data, never instructions: a label is a value the triager returned, and mail text never
  chooses the action.
- An action that mutates mail goes through the confirm gate in code, with an audit row and undo
  where possible. Sending is never an action (invariant 8).
- Triage results stay cached by `(content_hash, model, prompt_version)`.

## Open questions (defaults in bold)

- Q1: one triage result per message or one per triager: **the chain stores one result, under
  `triage`, naming the triager that settled it (`source`); each triager keeps its own options.**
- Q2: where rules live: **`[[triage.rules]]` in `config.toml`, validated at load**, not a UI yet.
- Q3: may a rule launch a plugin job kind: **yes, if it is registered; the payload is the message id
  and the label only.**
- Q5: how a change takes effect: **applied live to the running plugin when it can be, else by a
  plugin-only reload that leaves the MCP bridge connected** (a full reload drops it today).
- Q6: where a written change lives: **the user's `config.toml` edited in place, preserving
  comments**; the tracked `config.example.toml` is never touched.
- Q4: label sets: **declared by the triager and checked on every answer**; an unknown label fails
  the answer, as `output_model` does today.

## Report-back checkpoint

End with the six sections from `00-context.md`. Then stop.
