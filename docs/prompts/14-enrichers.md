# 14 — Enrichers: summary, triage, actions, header/MIME inspector

**Read first:** `docs/prompts/00-context.md`. **Depends on:** 10, 13.

## Goal

The ingest-time AI features, as `Enricher` plugins. Each result is computed once at
ingest, stored as small structured data, and reused so later queries never re-read the
full body (the token-thrift mechanism).

## Enrichers (first-party plugins, each independently versioned)

1. **`summary`** (`needs_llm`): 1–3 sentence summary + `key_points[]` (≤5, short).
   Thread-aware: for a reply, summarize only the *new* content (cleaned text), with the
   parent's stored summary supplied as context instead of the parent's body.
2. **`triage`** (`needs_llm`): `category` (enum: personal, work, transactional,
   notification, newsletter, automated, other), `priority` (0–3), `needs_reply` (bool),
   `waiting_on_others` (bool), `reason` (≤20 words). Cheap pre-filter: deterministic
   rules (`List-Id`, `Precedence: bulk`, `no-reply@`, `Auto-Submitted`) set the category
   **without calling the LLM** where confident; the LLM only handles the rest.
3. **`actions`** (`needs_llm`): `action_items[]` (text, owner, due-date-if-explicit,
   quote span) and `deadlines[]`. Quotes must be verbatim substrings of the cleaned
   text — validate and drop any item whose quote isn't found (anti-hallucination).
4. **`inspector`** (**no LLM**): deterministic header/MIME report. Port the logic of
   the legacy `HeaderAnalyzer` and `MIMEAnalyzer` (`/home/dfoulks/local/mailmind-unified/
   mailmind/analysis.py`, read-only reference): SPF/DKIM/DMARC from `Authentication-Results`,
   ARC presence, spam headers, hop count from **`Received`** headers (fix the legacy
   estimate, which wrongly counted `by ` in `Authentication-Results`), date sanity,
   `Reply-To` mismatch, `List-*`, risky attachment extensions. Add tests for each rule.
   The legacy LLM "troubleshoot" pass is out of scope.

## Requirements

- Every LLM enricher builds prompts only through `PromptBuilder` with `UntrustedText`
  (prompt 10), validates pydantic output, and goes through `generate_cached`.
- **Truncation policy:** inputs are cleaned text capped to a configurable token budget
  (`ai.enrich_max_input_tokens`); over-budget messages use head+tail with an explicit
  marker. Record `truncated: true` in the result.
- Results stored in `index.db` `enrichments(message_id, enricher, version, model_id,
  content_hash, json, created_at)`; stale rows (version/model changed) are replaced
  only after the new row exists.
- Enrichers can run on `FakeLLM` with scripted outputs for tests; add a handful of
  small **eval fixtures** per enricher (`tests/evals/<enricher>/*.json`: input
  message + expected properties, not exact strings) and a runner
  `uv run python -m oaken.evals enrich` that scores them against a configured real
  backend (manual, not in CI).
- Enrichment must **never** mark a message read, and must be skippable per-enricher
  via config.

## Acceptance criteria

- Deterministic triage rules classify the bulk/no-reply fixtures with zero LLM calls
  (assert `FakeLLM.calls == 0`).
- Hallucinated action-item quotes are dropped; verbatim ones kept.
- Prompt-injection fixtures (mail text instructing the model to change category, mark
  read, reveal other emails, output a different schema) cannot alter the stored
  schema/fields; the injection text only ever appears inside the delimited data block.
- Version bump of `summary` invalidates and refreshes only summary rows.
- Inspector rule tests cover pass/fail/absent for SPF, DKIM, DMARC and the corrected hop count.
- Token accounting shows ≥1 `cached=True` on rerun.

## Out of scope

Draft replies, auto-labeling, contact memory, attachment text analysis (later phases).

## Checkpoint

Standard report. Include the prompt templates (text) for the three LLM enrichers so
the user can review them.

**Note from prompt 04:** `enrich(msg)` takes no model argument, so an enricher with
`needs_llm = True` has no way to reach the LLM yet. Decide here how it gets one (suggested: the
host passes the configured backend through `PluginContext` at `start()`, and enrichers call it only
through `generate_cached()`). Use `BaseEnricher` / `@enricher`, and run `EnricherContract`.

## Note from prompt 06

The cleaner already reads bulk signals (`List-Id`, `List-Unsubscribe`, `Precedence`,
`Auto-Submitted`). The header/MIME inspector can reuse them so bulk and automated mail skips the
expensive enrichers, which is probably the largest CPU saving available.
