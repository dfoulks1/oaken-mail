# 09 — The ingest pipeline (core orchestrates the AI backend)

**Read first:** `docs/prompts/00-context.md`. **Depends on:** 03–08.

## Goal

The heart of the product: a staged, idempotent, resumable pipeline that core runs
for every new or changed message. AI work happens **here**, in the ingest flow — there
is no separate "analyze after sync" pass. Built and tested entirely with fakes.

## Stages (each is a job kind; each emits an event; each is skippable by content hash)

```
fetched ─▶ stored ─▶ parsed+cleaned ─▶ indexed(FTS5) ─▶ embedded ─▶ enriched
 (provider)  raw.db     06                07               08/embedder  Enrichers
   message is keyword-searchable here ──────┘          async, can lag behind
```

1. `ingest.store` — `RawStore.put_message` (from provider batches, via a core-owned
   `SyncRunner`, see below). Emits `MessageStored`.
2. `ingest.parse` — parse + clean + chunk + FTS index. Cheap, CPU-only, runs first and
   fast so search works immediately. Emits `MessageParsed`/`MessageIndexed`.
3. `ingest.embed` — batch chunks through the active `Embedder` (batch size from the
   backend's limits), write via `VectorStore`. Skips chunks whose
   `(content_hash, model_id)` already has a vector. Emits `MessageEmbedded`.
4. `ingest.enrich` — runs each configured `Enricher` whose
   `(content_hash, enricher.name, enricher.version, model_id)` result is not cached.
   No enrichers exist yet (14); the stage must work with `FakeEnricher`.

## Requirements

1. **`SyncRunner`** (core): given a `Provider` plugin and account, loops
   `pull(cursor)` → store each `RawMessage` → persist the **cursor only after** the
   batch's messages are durably stored (at-least-once, idempotent) → enqueue stage
   jobs → publish `SyncProgress`. Handles provider-reported deletions (soft delete +
   de-index) and `flag_changes` (applied to `message_state` — see prompt 12).
2. **Priority and backpressure:** user-initiated work > new mail > backfill. Embedding
   and enrichment have bounded concurrency and a **queue-depth cap** that pauses
   backfill enqueueing (not new-mail enqueueing). Expose `pipeline_status()`
   (counts per stage: pending/running/failed, oldest pending age, ETA from rolling throughput).
3. **Failure policy:** an embedder or LLM outage retries with backoff and never blocks
   earlier stages; messages stay searchable by keyword. A poison message (stage fails
   `max_attempts` times) is parked with an error and surfaced in `pipeline_status()`,
   and does not stall the queue.
4. **Model/version changes:** changing `ai.embedder` model or an enricher `version`
   makes affected items "stale"; a `reprocess(stage, scope)` operation enqueues them
   at **backfill** priority. Nothing is deleted until the replacement exists.
5. **Token accounting:** every LLM/embedder call records `(stage, model_id, tokens_in,
   tokens_out, cached: bool)` in `raw.db` `ai_usage`, with a `usage_summary(since)`
   query. This is how we prove the token-thrift claim.
6. **Idempotency & crash safety:** killing the process at any point and restarting
   converges to the same final state with no duplicates (test by killing the
   `JobRunner` mid-stage with the `FakeClock`/fault injection).

## Acceptance criteria (all with `FakeProvider`, `FakeEmbedder`, `FakeLLM`)

- 500 synthetic messages: after `SyncRunner` finishes the `store`+`parse` stages, all are
  keyword-searchable **before** any embedding job has run (assert ordering).
- Re-running the same sync produces zero new embedder calls (assert `FakeEmbedder` call count).
- Crash-injection test at each stage boundary converges correctly.
- Embedder outage for N attempts, then recovery: messages end up embedded; keyword
  search worked throughout.
- Changing the fake embedder `model_id` marks vectors stale, and `reprocess` rebuilds them
  without removing the old vectors first.
- `ai_usage` shows the second run with `cached=True`/zero calls.

## Out of scope

Real providers/models; the user-visible operations (13); read-state (12).

## Docs site

Add `docs/architecture/ingest-pipeline.md` (stage diagram as a ```` ```mermaid ```` block,
idempotency and failure policy, the token-accounting explanation) to the site *Concepts*
group. `check_docs.py` must pass.

## Checkpoint

Standard report. Include the stage diagram as it was actually implemented and the
queue/lease parameters chosen.
