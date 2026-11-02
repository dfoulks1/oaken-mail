# 52 — The console by stage: status on `/`, a tab per kind (DRAFT)

**Status:** draft. Answer the open questions, then build in the slices below.
**Read first:** `00-context.md`, `CORE_50-console-without-script.md` (no script, no CDN: forms,
redirects and `popover` only), `CORE_41-stats-controls.md`, `CORE_47-split-enrich.md`.
**Depends on:** 41, 47, 50. Nothing unbuilt.
**Track:** CORE. Touches `src/oaken/api/` (`console.py`, `stats.py`, `templates/`).

## Why

The console grew a tab at a time. The stats page mixes status, the pipeline, usage, plugins and the
audit log. The Plugins tab lists every kind in one table with a Kind column that wraps mid-word, and
only `embed` and `enrich` can be paused or limited. Triage is now a plugin kind of its own, and an
embedder is a bottleneck that must be scalable and removable. The structure has been outgrown.

## Goal

1. **`/` is the front page**: status and the daemon's environment. Not `/general`.
2. **General** (the page now called Stats) is information-forward: **Ingest**, **Audit log** and
   **Reference**.
3. **One tab per kind** (a kind is a stage): `provider` (sync), `triager` (triage), `embedder`
   (embed), `enricher` (enrich), `llm_backend`, `vector_store`, `cache_store`. Each tab has:
   - the stage's controls: pause and resume, how many jobs run at once (`Set`), retry parked, and
     its queue numbers (pending, running, failed, per second);
   - a table of the plugins of that kind, with **no Kind column** (the tab is the kind): name,
     version, capabilities, health, an enable toggle, Reload, and a pause for that plugin alone;
   - for a model-backed plugin, its model (an embedder's included).

## Constraints

- **No script, no CDN** (CORE_50): a form per action with a redirect and a flash message.
- **Secrets never reach the page or the audit row.**

## Requirements

1. **Routes and nav.** `/` shows status and the daemon environment. The old Stats page is General,
   with Ingest, Audit log and Reference. The nav lists General and one entry per kind.
2. **Stage controls on the kind's tab**, over the existing operations (`pause_backfill`,
   `resume_backfill`, `set_pipeline_limits`, `retry_parked`), which already take a stage name
   (`triage`, `embed`, `enrich`, `enrich.<name>`). Every stage with a job kind gets the controls,
   `parse` and `triage` included. One pause/resume toggle per stage replaces the pair of buttons.
3. **Per-plugin pause.** An enricher pauses its own job kind (`enrich.<name>`). A plugin with no job
   kind of its own (a triager, an embedder, a provider, a backend) is paused by disabling it, and
   the control says "Remove temporarily". The page shows which plugins are removed, so a bottleneck
   can be taken out of the chain and put back.
4. **Embedders scale and swap.** The embed stage takes the same `Set` concurrency as any stage. An
   embedder's model is changed on its tab; a change that leaves vectors stale offers
   `reprocess embed` (`embeddings_stale` already says so).
5. **Fix on the way:** the columns that wrap mid-word ("embed der", "Relo ad"); the inline `style=`
   on the `/jobs` chart that breaks the CSP (a failing test today).

## Slices

- **A.** Routes and nav: `/` front page, General, one tab per kind with its plugin table (no Kind
  column), the wrapping fixed.
- **B.** Stage controls on each tab, the single pause toggle, per-plugin pause, embedder model and
  concurrency.

## Acceptance criteria

- `/` shows status and the environment; General has Ingest, Audit log and Reference.
- Each kind has a tab with its stage controls and a plugin table with no Kind column.
- Every stage can be paused, resumed and limited from the console; each plugin can be removed and
  put back, and the page shows which are removed.
- An embedder's model and concurrency are changeable from its tab.
- The CSP test passes; no script, no CDN.
- `ruff`, `ruff format --check`, `mypy`, `pytest` and the docs check are green.

## Open questions (defaults in bold)

- Q1: General's route: **`/stats`, relabelled General**, with `/` the new front page.
- Q2: pausing a triager: **disable it, so the chain skips it**; the AI triager off leaves mail
  unrated and embedded.
- Q3: the tab for `parse` (core, no plugin): **Ingest on General**.

## Parked (an idea, not planned)

Configuration beside what it configures: each configurable thing gets a collapsed `Config` section
holding its own `config.toml` entry in a small textarea with Test, Save and Reload, rendered by one
generic component with no per-plugin code (so a new plugin gets its editor for free), which would
retire the Config tab and give scoped and full linting and reloading. The plugin half already has
its operations (`get_plugin_config`, `check_plugin_config`, `set_plugin_config`, `plugin_reload`);
core sections would need scoped equivalents. Not part of this prompt.

## Report-back checkpoint

End with the six sections from `00-context.md`. Then stop.
