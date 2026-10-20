# 03 — Event bus, durable job queue, scheduler

**Read first:** `docs/prompts/00-context.md`. **Depends on:** 02.

## Goal

The async nervous system of core: an in-memory typed event bus, a **durable**
job queue that survives crashes, and a scheduler. This replaces the legacy
APScheduler usage.

## Requirements

1. **Event bus** (`core/events.py`): typed event dataclasses (frozen, with
   `event_id`, `ts`, `account_id` where relevant) — initial set: `SyncStarted`,
   `SyncProgress`, `SyncFinished`, `MessageStored`, `MessageParsed`, `MessageIndexed`,
   `MessageEmbedded`, `MessageEnriched`, `MessageReadChanged`, `JobStateChanged`,
   `PluginHealthChanged`. API: `subscribe(event_type, handler)`, `publish(event)`,
   `stream(filter)` returning an async iterator (this later feeds SSE). Handler
   failures are logged and never break the publisher or other handlers. Bounded per-
   subscriber queues with a documented drop-oldest policy for slow stream consumers
   (never for internal handlers).
2. **Durable job queue** (`core/jobs.py`) in `raw.db` (migration `0001_jobs.sql` for
   now in a core-owned migrations dir; prompt 05 adds its own tables in the same DB):
   `jobs(id, kind, payload_json, status, priority, attempts, max_attempts,
   run_after, locked_by, locked_until, last_error, dedupe_key, created_at, updated_at)`.
   - Atomic claim (`UPDATE … RETURNING` or equivalent) with a lease; expired leases are
     reclaimed on restart.
   - Retries with exponential backoff + jitter; `max_attempts`; terminal `failed`.
   - `dedupe_key` makes `enqueue` idempotent (same key while queued/running = no-op).
   - Priority (user-initiated > ingest > backfill), cancellation, and per-kind
     concurrency limits.
   - Handlers register by `kind`; payloads are JSON only and validated by pydantic models.
3. **Worker runtime**: an asyncio `JobRunner` with N workers, graceful shutdown that
   lets in-flight jobs finish or releases their lease, and a `drain()` for tests.
4. **Scheduler** (`core/scheduler.py`): interval and cron-style recurring enqueues
   (use a small dependency-free cron parser or `croniter`; justify the choice),
   persisted `next_run` so restarts don't double-fire or skip, driven by the injected `Clock`.

## Acceptance criteria (all with `FakeClock`, no sleeps)

- A job enqueued twice with the same `dedupe_key` runs once.
- A handler that raises is retried with the documented backoff and ends `failed`
  after `max_attempts`; `last_error` is stored and **truncated/redacted**.
- Kill-simulation: a job claimed but not completed is re-run after lease expiry.
- Priorities and per-kind concurrency limits are honored (test with a recording handler).
- A slow `stream()` consumer does not block publishers; a failing handler does not
  prevent other handlers from receiving the event.
- Scheduler fires at the expected fake times, survives a restart without duplicates.

## Out of scope

Real jobs (ingest, sync); the HTTP/SSE layer; plugin loading.

## Checkpoint

Standard report. Include a short sequence diagram (mermaid) of enqueue → claim →
run → retry → complete in `docs/architecture/jobs.md`.
