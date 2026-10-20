# 03b — Swarm foundations (agents, quotas, results, provenance)

**Read first:** `docs/prompts/00-context.md`. **Depends on:** 03.

## Goal

Let work run in parallel across more than core's own workers: optionally, **external AI agents**
(a larger model, or several) lease jobs from the same durable queue over the API, the CLI or MCP.
The user decides how many and which kinds of work, and nothing is sent to an agent unless
they opted in. This prompt lays the storage, scoping, quota and validation rules in core. The
transports (HTTP, MCP and CLI claim operations) come with prompts 13, 16, 17 and 18.

## Requirements

1. **Config.** A `[swarm]` section: `enabled` (default `false`), `allowed_kinds` (default empty:
   a job kind is never offered to an agent until listed), `max_agents`, `default_max_leases`,
   `default_max_per_hour`. `[jobs] max_result_bytes` caps a stored result. Internal
   parallelism stays in `[jobs] workers` and `kind_limits`. There are no profiles.
2. **Agents** (`core/agents.py`, migration `0003_swarm.sql`): a registry that creates an agent
   with its own random token (shown once, only the SHA-256 stored), a label, the job kinds it
   may lease, optionally the accounts it may see, and two quotas. `authenticate(token)`,
   `list`, `get`, `revoke`. Revoking is immediate: the agent's held jobs go back to the queue
   without counting the attempt, and the agent's later heartbeats and results are refused.
3. **Scoped, quota-checked claims.** `JobQueue.claim(..., agent=agent)` re-reads the agent inside
   the claim transaction and enforces: not revoked, swarm enabled, kind within the agent's kinds
   **and** `allowed_kinds`, account within the agent's accounts, `QuotaExceeded` at the concurrent
   lease limit and at the hourly claim limit.
4. **Validated results with provenance.** Jobs gain `result_json`. `complete(job, worker,
   result, produced_by=)` validates the result against the schema registered for the kind
   (`register_result`, or `JobRunner.register(result_model=)`) and a size limit **before**
   storing; a bad result raises `InvalidResult` and leaves the job leased. A kind with no
   schema accepts no result. The job records `completed_by` and the self-reported `produced_by`.
5. **Fan-out and join.** `parent_job_id` on enqueue (the parent must exist) and
   `JobQueue.children(parent)`. No scheduler support for waiting on children yet.
6. **Attribution.** `core/actors.py`: `Actor(kind: user | agent | system, label)`. Jobs record
   who enqueued them. The audit log (prompt 12) and operations (prompt 13) reuse it.
7. **Docs.** `docs/architecture/swarm.md`: the model, the config, the privacy stance, and how
   to set Ollama's `OLLAMA_NUM_PARALLEL` to match the job limits. Update `architecture/jobs.md`
   and the software map.

## Acceptance criteria (all with `FakeClock`, no network)

- Tokens are stored only as a hash; a wrong, empty or revoked token is refused.
- An agent claims only its kinds, only kinds in `allowed_kinds`, only its accounts.
- Lease and hourly quotas hold, are per agent, and the hourly one resets with time.
- Revoking returns held jobs to the queue and the old agent can no longer finish them.
- A result failing its schema, over the size limit, or for a kind with no schema is rejected and
  nothing is stored; a valid one is stored with `completed_by` and `produced_by`.
- A handler's returned result goes through the same validation as an agent's.

## Out of scope

The HTTP, MCP and CLI operations that expose claiming; token storage on the agent's side;
spawn budgets and loop guards (with the enrichment prompts); waiting on children in the
scheduler; any real agent or model.

## Checkpoint

Standard report.
