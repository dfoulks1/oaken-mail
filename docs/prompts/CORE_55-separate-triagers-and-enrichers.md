# 55 — Separate triagers from enrichers

**Status: Partly built, not verified** 2026-11-03 (code written; the owner's checks not yet run). Steps 1 to 6 are in, with `ask` removed and `BaseAnalyzer` added; see ADR 0017's Built line for what is left. Lead: the top tier (it changes a contract
and stored data); workers may take the mechanical steps.
**Read first:** `00-context.md`, `docs/adr/0017-triagers-and-enrichers-are-separate-kinds.md` (the
ruling), `docs/adr/0012-plugin-extension-hooks.md`, `docs/adr/0010-the-stable-surface-and-its-versioning.md`
(snapshots), `docs/bbq/2026-11-03-adr-triager-enricher-split.md` (the owner's answers).
**Depends on:** ADR 0017 ratified. **Blocks:** CORE_56 (ADR 0016: the rules move and wizard).
**Track:** CORE. Touches `src/oaken/plugins/`, `src/oaken/ingest/pipeline.py`, `src/oaken/core/`
(migration `0019`, operations), `src/oaken_enrichers/`, the new `src/oaken_triagers/`, `pyproject.toml`,
`docs/`.

## Why

A triager is not an enricher, yet the code shares their types, their base members, their package
and their stored row (ADR 0017). The rules move (ADR 0016) would build on that tangle.

## Goal

Each step lands green on its own, with its test. The owner runs ruff, mypy, pytest and the docs
check (do not run them); stage by path; never commit `config.toml`.

1. **Shared members to `BasePlugin`:** `version`, `needs_llm`, `output_model`, `ask_intent`,
   `output_schema`. Remove them from `BaseEnricher` and `BaseTriager`. Test: both kinds still validate
   at load; the contract snapshot is regenerated and reviewed.
2. **`EnrichInput` becomes `CleanMessage`**, everywhere (`contracts.py`, `adapt.py`, `testing/`,
   `evals/`, `core/search/`, enrichers). No alias. A triager returns `TriageVerdict`, a new type in
   `contracts.py`; the `Triager` protocol names no enricher type. Test: `rg EnrichInput` is empty;
   the three triagers and the pipeline use `TriageVerdict`.
3. **The package move:** create `src/oaken_triagers/` (`spamcheck`, `rules`, `ai`, their shared
   helpers, their tests). Move the shared LLM-access helper to `oaken/plugins/`, neutral. Update the
   entry points (names unchanged), `[tool.importlinter]` roots and contracts, and the editable
   install. Test: `uv run lint-imports` passes (the owner runs it); the plugin loads under the same
   `[plugins.triager.*]` config.
4. **The verdict table:** migration `0019_triage_verdicts.sql` creates `triage_verdicts`, copies every
   enrichment row named `triage`, then deletes them, in one transaction. Re-point the pipeline gate
   (`GATE`), `min_priority`, `embed_min_priority`, search signals and routing. Tests: a database with
   old `triage` rows migrates and loses none; a failure leaves the old rows; the gate behaves as
   before (strict and promiscuous).
5. **Operations:** add `get_triage`; `get_enrichment` returns enrichers only; `enrich_message` runs
   enrichers only; add `triage_message`; keep `try_triage`; the usage stage is `llm.triage` (read the
   old key too, for stored history). Regenerate the operation snapshot. Tests: each operation's input
   and output; the CLI and MCP exposure unchanged for `try_triage`.
6. **One conformance suite per kind** in `oaken/testing/contracts.py`, and `docs/plugins.md` rewritten
   so a triager is described on its own. Regenerate the CLI and operations references.

## Not in this prompt

The `ai` triager's role (not ruled), the rules move, `store`, `draft` and the wizard (CORE_56), and a
plugin-extended console (an idea, no ADR yet).

## Risks

- The migration rewrites the owner's live database: back up `~/.oaken` first (the owner does it).
- `get_enrichment` changes shape for any reader of the verdict: grep every caller before the edit.
- A renamed type in a plugin context may break `adapt.py` silently; the snapshot tests are the check.

## Done when

The owner's checks pass, the old names are gone from `src/`, `docs/plugins.md` reads clean, and
ADR 0017's Built line cites a test.
