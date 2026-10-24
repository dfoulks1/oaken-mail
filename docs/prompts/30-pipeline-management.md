# 30 — Pipeline management: see, pause, retry, cancel and tune the queue

> **Status: built (0.1.1), with the deviations below.** Where this prompt and the code differ, the
> code is right.
>
> - **Names kept.** The operations that already existed were extended, not renamed:
>   `pause_backfill`/`resume_backfill` take an optional `stage` (`embed` or `enrich`; none means
>   every model stage), `list_jobs`/`cancel_job`/`retry_job`/`retry_parked` cover the job list.
>   The prompt's `pause_pipeline`, `resume_pipeline`, `list_pipeline_jobs`, `retry_jobs` and
>   `cancel_jobs` were not added.
> - **Built:** per-stage pause (table `pipeline_stage_pause`, survives a restart),
>   `set_pipeline_limits` (table `pipeline_limits`, outranks config, applies on the next claim),
>   `reprocess` with `since` and `limit` (embed and enrich only; enrich was already
>   "only what is missing" through the cache), `pipeline_status` with per-stage `paused`,
>   `concurrency` and `current`, interactive-over-background priority (`oaken.ai.priority`,
>   `[ai] background_yield_seconds`, default 120, 0 = never hold back), the CLI
>   (`oaken pipeline pause|resume [STAGE]`, `oaken pipeline limits STAGE N`, `oaken reprocess
>   --since --limit`) and the per-stage table on the core `/jobs` console.
> - **Not built:** bulk `retry`/`cancel` by stage or state, `pipeline.paused|resumed` events on the
>   stream, and the web UI's Settings → Pipeline pane (the core `/jobs` console covers it for now).
> - **How priority works:** the model service counts interactive calls (anything whose caller is
>   not `internal`). A background call holds its *next* call while one runs, up to the yield time.
>   It cannot interrupt a background call already running, so a question waits for at most one.

**Read first:** `docs/prompts/00-context.md` and `docs/prompts/21b-webui-client-foundation.md`.
**Depends on:** 05, 13, 16, 17, 21b, 22.

## Why

Today a person can **see** the pipeline (`pipeline_status`) and **add** to it (`reprocess`), and
nothing else. There is no pause, no drain, no cancel, no retry of a parked job, no live concurrency
change and no list of the jobs themselves. On a CPU-only machine a long enrich run starves
everything else that uses the local model: a `summarize_message` or `oaken ask` made while enrich
is running times out. The user needs to manage the queue, from the CLI and the UI, and every job
row must carry its **job id** so a job can be named, retried or cancelled.

What exists (do not rebuild it): `pipeline_status` (per-stage `pending/retrying/running/failed`,
`per_second`, `eta_seconds`, `parked[]` with `job_id`), `reprocess {stage, account_id}` (queues old
mail; `mcp="never"`), `enrich_message`, `[pipeline] enrich_concurrency/embed_concurrency`
(config-file only), and the automatic backfill cap (`backfill_paused`).

## Decisions already taken

- **Job id is always shown.** Every row the CLI prints and the UI renders for a job includes
  `job_id` (full id in `--json`/the API; the table may abbreviate with a copy affordance, never
  drop it). `pipeline_status.parked[]` already has it; the new list has it for every state.
- **Pipeline management is a CLI set and a UI pane against the same operations** (22's rule).

## Queue data is JSON (the UI parses it, never CLI text)

- Every operation here returns a **typed pydantic output** through the normal envelope
  (`POST /v1/operations/{name}` → `{request_id, status, output}`); the UI reads `output` directly
  and the CLI's `--json` prints the same object. Nothing the UI needs exists only as formatted text.
- **Stable, machine-friendly shapes:** fixed key names; `state` and `stage` as closed enums
  (published in the operation's JSON Schema, so the UI can build filters from it); timestamps as
  ISO-8601 UTC strings; durations and ETAs as numbers of seconds (or `null`), never "3m 20s";
  counts as integers; `job_id` and `message_id` as strings; `last_error` a short string.
- **Lists are paged and self-describing:** `{items[], total, limit, offset}` (the same convention
  22 gives `list_messages`), so the UI can render paging without a second call.
- **Live updates are JSON too:** queue changes (`job.queued`, `job.started`, `job.finished`,
  `job.parked`, `pipeline.paused|resumed`) are published on the SSE stream (`/v1/events`) as JSON
  event objects carrying `stage`, `job_id` and counts, with the existing `Last-Event-ID` replay.
  The UI patches its table from them and falls back to re-calling `list_pipeline_jobs`.
- A contract test asserts each output validates against its published schema and that the CLI
  `--json` and the API return identical objects for the same call.

## Requirements (operations first)

All new operations are `mutate`, confirm-gated where they discard or reorder work, **audited**
(stage, ids, counts, never mail text), and **`mcp="never"`** (default; see Q5). Reads follow 22.

1. **`list_pipeline_jobs {stage?, state?, message_id?, limit, offset}`** — `read`. Returns rows
   `{job_id, stage, state(pending|running|retrying|parked), message_id, account_id, attempts,
   next_attempt_at, started_at, last_error}` plus `total`. `last_error` is a short code/text, never
   mail content. Stable order (oldest first within state). Never marks anything read.
2. **`pause_pipeline {stage?}` / `resume_pipeline {stage?}`** — no stage = all stages. Pausing
   stops claiming new jobs; a running job finishes. State is stored in the database so it
   **survives a restart** (Q1), is reported in `pipeline_status` as `paused` per stage, and is
   announced on the event stream. Pausing a stage never loses or reorders its queue.
3. **`retry_jobs {job_ids?|stage?|all_parked?}`** — puts parked/retrying jobs back to pending with
   attempts reset; returns `{requeued, missing}`.
4. **`cancel_jobs {job_ids?|stage?, state?}`** — removes **pending/retrying/parked** jobs only
   (never a running one: pause, let it finish, then cancel). Never touches the mail or cached
   results. Returns `{cancelled, skipped_running}`. `confirm` required when it would cancel more
   than a handful (the gate already previews counts).
5. **`set_pipeline_limits {stage, concurrency}`** — changes `enrich_concurrency` /
   `embed_concurrency` **live** (workers scale up or drain down) and persists it. Where it persists
   follows 23/29 (the scoped `config.toml` writer, or the database if that proves better; one ADR
   covers both). Bounded by the settings schema (`ge=1`).
6. **Extend `pipeline_status`** with per-stage `paused: bool` and `current: [{job_id,
   message_id, started_at}]` (the running jobs), and keep `parked[]`. Extend **`reprocess`** with
   `only_missing: bool`, `since`, and `limit` so a person can enrich just the newest N rather than
   everything. Defaults preserve today's behaviour.
7. **Interactive work beats background work (Q2).** When an interactive model call
   (`summarize_message`, `ask`, `enrich_message`) is waiting on the local backend, background
   enrich yields: it does not start a new model call until the interactive one finishes. Decide
   the mechanism (a priority lane or semaphore in the model service) and record it; it must not
   starve background work forever, and it must be testable with a fake backend.
8. **CLI** (`oaken pipeline` stays the status view; subverbs are added): `oaken pipeline jobs
   [--stage S] [--state X] [--message ID]`, `oaken pipeline pause|resume [STAGE]`, `oaken pipeline
   retry [JOB_ID… | --stage S | --parked]`, `oaken pipeline cancel [JOB_ID… | --stage S]`,
   `oaken pipeline limits STAGE N`. The jobs table's first column is `job_id`. `--json` is the raw
   operation output. Update 17's table.
9. **UI** (Settings → Storage & Data → **Pipeline**, replacing the static metrics block):
   - A stage table: pending / running / retrying / failed, throughput, ETA, **Paused** badge, and
     per-stage **Pause / Resume**; a global Pause all / Resume all.
   - A **jobs table** from `list_pipeline_jobs`: **Job id** (shortened, click to copy), stage, state,
     message (links to the reader), attempts, last error; filters for stage and state; per-row
     **Retry** (parked/retrying) and **Cancel** (pending/retrying/parked); bulk "Retry all parked"
     and "Cancel selected" with the confirm modal showing counts.
   - A **Concurrency** control per stage (`set_pipeline_limits`).
   - Live updates from the SSE stream with a polling fallback (21b `Oaken.events`, `Oaken.job.watch`);
     elements carry `data-oaken-requires="…"` (21b C5).
   - The "Re-enrich" and "Rebuild index" buttons already map to `reprocess`/`rebuild_index`; add
     the only-missing / newest-N options to Re-enrich.

## Open questions (defaults in bold)

- Q1: pause **survives a daemon restart** (stored in the database).
- Q2: interactive model calls **take priority** over background enrich automatically; manual pause
  is in addition, not instead.
- Q3: scheduled quiet hours for enrichment: **not in this prompt**.
- Q4: choosing the model per enrichment stage from this pane: **no**; it belongs to plugin config
  (23).
- Q5: AI pausing/cancelling the queue over MCP: **never** (`mcp="never"`).
- Q6: `cancel_jobs` is the only destructive operation: **yes**; deleting cached enrichments stays
  with the cache clear in 29.

## Acceptance criteria

- `list_pipeline_jobs` returns every job with its `job_id` and the right state counts on a seeded
  queue; the CLI table and the UI table both show `job_id`; `pipeline_status.parked[]` ids match.
- Pausing a stage stops new claims while the running job finishes; resuming continues in the same
  order; pause survives a restart (test with a re-opened database); `pipeline_status.paused`
  reflects it.
- `retry_jobs` requeues parked jobs with attempts reset; `cancel_jobs` removes pending/parked jobs,
  reports `skipped_running` for running ones, and never touches mail or cached enrichments.
- `set_pipeline_limits` changes worker count live without dropping a running job and persists it.
- With a fake slow backend, an interactive call issued while enrich is running completes without
  waiting for the whole backlog (priority test); background work still completes afterward.
- None of the new mutating operations appears in the MCP tool set; every one is audited.
- Every queue operation's output validates against its published JSON Schema; CLI `--json` equals
  the API output; queue SSE events are JSON and replay by `Last-Event-ID`.
- The parity test from 22 covers them for both clients.
- `ruff`, `ruff format --check`, `mypy`, `pytest` and the docs check are green; ADR added.

## Report-back checkpoint

End with the six sections from `00-context.md`. Then stop.
