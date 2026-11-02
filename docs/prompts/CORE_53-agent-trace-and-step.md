# 53 — Trace and single-step: see exactly what a model or agent is told, thinks and returns (DRAFT)

**Status:** slice 1 built: the trace (requirements 1 to 4). Not built: single-step (5) and the
rules view (6), which are the second slice. Where the code differs: events carry `plugin_kind`,
`plugin`, `job_id` and `message_id` from a trace context (so `oaken trace -f plugin.<kind>.<name>` and
`--job` are exact); every `BasePlugin` method is traced by `__init_subclass__` and `self.trace(...)`
adds events; the trace rides the event bus as `TraceEvent` (`publish_nowait`, so the persisting
handlers never see it) and `oaken trace -f` is pushed from `GET /v1/trace/stream`.
**Read first:** `00-context.md`, `CORE_33-agent-enrichment.md` (the agent door), `CORE_47-split-enrich.md`,
`CORE_52-console-by-stage.md`, ADR 0013 (the console's rules: counts and health only).
**Depends on:** 33, 47, 52. Nothing unbuilt.
**Track:** CORE. Touches `src/oaken/ai/` (the LLM service), `src/oaken_ollama/`, the agent operations
(`core/operations/builtin/jobs.py`), `core/events.py`, a trace sink and `oaken trace` in the CLI.

## Why

To tune triage and enrichment a person has to see what is really happening: the prompt a model was
given, whether it thought, what it returned, how long it took, and what was committed back. Today the
audit log holds ids and metadata only, usage holds token counts, and the prompt and the answer are
invisible. A person should be able to sit with an agent as it claims a job, does its triage or
enrichment, and commits the result, one step at a time.

## Goal

1. A **trace**: an ordered stream of what crosses each boundary, with the full text.
2. **Single-step**: run one job on a paused stage, then stop, and watch it in the trace.
3. With the per-stage and per-plugin pauses and concurrency of CORE_52, that is the tuning loop.

## Decisions already taken (do not re-litigate)

- **Opt-in.** `[log] trace = false` by default. A trace holds mail text, so it is off until asked for.
- **Token-protected.** The trace is served over the authenticated API (a streaming operation) and
  read with `oaken trace` (`-f` to follow). It is never shown on the console's pages, which are
  loopback and token-free and carry counts and health only (ADR 0013). It is never offered over MCP.
- **A file of its own.** `trace.jsonl` in the data directory, mode `0600`, rotated like the event
  log. Never the audit log, which never records message text.
- **Door traffic plus optional agent reports.** Core records every agent claim (the prompt and
  message it handed over), heartbeat, completion (the answer committed) and failure. An agent may
  also attach its own thinking and tool calls to a heartbeat or completion; those are recorded as
  agent-supplied and untrusted.

## Requirements

1. **LLM calls.** Every call core's own model makes records: task, prompt version, model, account and
   message ids, the system text, each user part (fenced as mail), the schema, the response, the
   thinking or reasoning the backend returns, any tool calls, the backend's token counts and
   timings (for Ollama: prompt and response token counts, load, prompt and total durations), whether
   it was a cache hit, the duration and the outcome. Add `thinking` and `meta` to `LLMResponse`
   (both optional, so existing plugins are unchanged) and have `OllamaLLM` fill them from
   `message.thinking`, `message.tool_calls` and the timing fields.
2. **Agent door.** `claim_job`, `heartbeat_job`, `complete_job` and `fail_job` each record an event
   naming the agent, the job and the kind, with the payload handed over, the answer committed, or
   the error. `heartbeat_job` and `complete_job` take an optional `trace` list (size-limited, each
   item a `thinking`, `tool_call` or `note`).
3. **Job edges.** Queued, claimed, stored (which enrichment, which version, the model or agent),
   fan-out queued, parked.
4. **Delivery.** A `trace_tail` streaming operation (permission `read`, `mcp="never"`) and
   `oaken trace [--follow] [--job ID] [--message ID] [--stage NAME]`. Events past a size cap are cut
   and say so.
5. **Single-step.** `step_stage(stage)` lets exactly one queued job of a paused stage run, then the
   stage is paused again. `oaken pipeline step enrich.summary`, and a Step button beside a paused
   stage's pill on its page. A step on an unpaused stage is refused.
6. **The rules that run.** The Triage page lists each triager in rank order and, for `rules`, every
   rule with a one-line description and an on/off switch (it writes `skip_rules`). `BaseTriager`
   gains `describe()` for this, so a custom triager lists its own steps.

## Open questions (defaults in bold)

- Q1: retention: **rotate at 10 MB, keep 3**, like the event log.
- Q2: redact mail text in the trace: **no**; seeing it is the point, and the opt-in and the 0600 file
  are the protection.
- Q3: the per-event size cap: **200 KB**, cut with a marker.
- Q4: should the trace be switchable at run time without a reload: **yes, `oaken trace on|off`**
  (a runtime flag, audited), with `[log] trace` as the start-up default.

## Acceptance criteria

- With `trace = true`, an ingest of one message shows, in order: the triage prompt and answer (with
  thinking and timings), the stored result, the fan-out, and each enricher's prompt and answer.
- An agent that claims a job shows the payload it was handed and, on completion, the answer it
  committed; a `trace` it attaches appears labelled as agent-supplied.
- With `trace = false` nothing is written and no prompt text is kept.
- `trace_tail` needs the token and is not offered over MCP; no console page shows trace content.
- A paused stage steps exactly one job and is paused again.
- `ruff`, `ruff format --check`, `mypy`, `pytest` and the docs check are green.

## Report-back checkpoint

End with the six sections from `00-context.md`. Then stop.
