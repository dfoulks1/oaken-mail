# 47 — One job per enricher: triage first, then only what earns it

**Read first:** `docs/prompts/00-context.md`, `CORE_09-ingest-pipeline.md`, `CORE_33-agent-enrichment.md`
(the agent door and its payload), `CORE_42-per-enricher-models.md` and ADR 0010.
**Depends on:** 09, 14, 33, 42. Nothing unbuilt.
**Track:** CORE. Touches `src/oaken/ingest/pipeline.py`, the agent operations
(`src/oaken/core/operations/builtin/jobs.py`), `pipeline_status` and the stage accounting. No plugin
contract changes.

## Why

Today one `ingest.enrich` job per message runs every enricher in turn. Measured on a real claimed
job: about 1,600 tokens went to an agent for a message of about 100 tokens, because it carried the
instructions and schema of all three model-backed enrichers, and a message that triage rated 0 still
needed two more calls to learn that. Bundling also means a farm of workers only works if one worker
does everything. Rows are free; tokens are what cost.

## Goal

Each enricher is its own job kind, `ingest.enrich.<name>`. Triage runs first, and its priority decides
which other enrichers are ever queued. A worker (core's own, or an external agent) claims only the
kinds it is good at, and a job carries only that enricher's instructions.

## Decisions already taken (do not re-litigate)

- **One kind per enricher**, named `ingest.enrich.<enricher name>`, for every enricher including the
  deterministic ones. It is the default behavior, with no switch.
- **Triage fans out.** A message gets its triage job (and every enricher that has no
  `min_priority`) when it is parsed. The enrichers that have a bar are queued only when triage
  completes, and only if the priority meets it. Below the bar they are never created.
- **The old kind drains cheaply, with no migration.** An existing `ingest.enrich` job becomes a
  converter: it queues the per-enricher jobs for its message and finishes.
- **Core's workers and agents race for the same jobs.** Whoever claims first wins, so nothing
  stalls when no agent is connected.
- **Triage failing must not strand mail.** If triage fails for good, the gated enrichers are queued
  anyway.

## Not in scope

New enrichers, scheduling work for idle hours, a per-kind lease setting beyond today's, and a UI.
Specialist (non-LLM) workers are PLUGIN_49.

## Requirements

1. **Kinds.** `ingest.enrich.<name>` registered once the plugin host has started (the way plugin job
   kinds are), with `result_model=StageJobResult`. `STAGE_KINDS`, `pause`, `limits` and
   `pipeline_status` treat every kind under the `ingest.enrich` prefix as the **enrich** stage.
2. **Queueing at parse.** The parse job queues the gate enricher's job and each enricher with no bar.
   With no gate enricher, or it disabled, it queues them all. Dedupe key is `(kind, message)`.
3. **Fan-out.** When the gate's job completes, natively or from an agent, core queues each remaining
   enricher whose bar the stored priority meets. Their jobs run at the triage priority: the higher
   the priority the sooner, always below parse so enrichment never outranks ingest.
4. **The converter.** A legacy `ingest.enrich` job queues what (1) to (3) would have for its message,
   skipping what is already stored, and finishes. It never calls a model.
5. **Agent door.** Claiming a split kind returns the message and only that enricher's `version`,
   instructions and answer schema. `complete_job` takes that enricher's answer; an LLM enricher's goes
   through `from_answer`, a deterministic one's is validated against its `output_model`. No partial
   answers and no `still_owed` for a split kind. `max_chars` applies as before.
6. **Concurrency.** `[ingest] enrich_concurrency` is the default per enricher kind;
   `[jobs] kind_limits` overrides one. The total is still capped by `[jobs] workers`.
7. **Reprocess.** `reprocess("enrich")` queues the per-enricher jobs that have no current result.
8. **Status.** `pipeline_status` reports the enrich stage as the sum, and `oaken pipeline status` can
   show it per enricher.

## Acceptance criteria

- A parsed message queues triage and the unbarred enrichers, and nothing for an enricher whose bar its
  priority does not meet; a message at or above the bar gets them after triage completes.
- A message whose triage fails for good still gets the gated enrichers.
- A legacy `ingest.enrich` job drains into per-enricher jobs and finishes with no model call.
- An agent allowed only `ingest.enrich.triage` can never claim a summary job; its payload names only
  the triage enricher and is a fraction of the combined size.
- Pausing `enrich` pauses every enricher kind; `limits enrich N` applies to each.
- A deterministic enricher's job completes natively and never reaches an agent unless allowed.
- `ruff`, `ruff format --check`, `mypy`, `pytest` and the docs check are green.

## Open questions (defaults in bold)

- Q1: the job priority for fanned-out jobs: **base minus the enrich step, plus the triage priority,
  capped below parse.**
- Q2: should the inspector (deterministic) run inline at parse instead of as a job: **no**, a job like
  the rest, so it shows in the queue.
- Q3: a bar set while jobs are queued: **applies when triage completes**, not retroactively.

## Report-back checkpoint

End with the six sections from `00-context.md`. Then stop.
