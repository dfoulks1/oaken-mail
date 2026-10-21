# 33 — Agent enrichment: let an external model enrich a message through `/mcp/agent`

**Read first:** `docs/prompts/00-context.md`, `docs/prompts/03b-swarm-foundations.md` and
`docs/architecture/swarm.md`. **Depends on:** 03b, 05, 13, 18, 31, 32.

## Why

The swarm door exists and works: `oaken mcp --agent` (or `POST /mcp/agent`) authenticates an agent
token, lists `claim_job`, `heartbeat_job`, `complete_job` and `fail_job`, enforces `allowed_kinds`,
quotas and revocation, and keeps the person's token and the agent's token out of each other's door
(401 both ways). The headline use is a person running a large model on their own hardware
(LM Studio with a 27B, say) that takes the work the small native model is slow at, through MCP, and
hands the findings back to Oaken.

It cannot do that yet, for three reasons found by claiming a live `ingest.enrich` job:

1. **The payload is only an id.** `claim_job` returns `{message_id, backfill}`. The agent has no
   message text, no instructions for what to produce and no schema for the answer. Fetching the
   text with the person's token would skip the swarm's scoping and workspace rules, so it is not
   the answer.
2. **There is nowhere to put an enrichment.** `ingest.enrich` is registered with
   `StageJobResult` (`done`, `skipped`, `note`). `complete_job` with that shape marks the job
   finished and stores no summary, triage, actions or inspector result, so the message is never
   enriched and the native stage will not retry it. Completing is worse than failing.
3. **Nothing says whose answer is whose.** Enrichment rows are keyed by
   `(content_hash, enricher, version, model_id)`. An agent's result has to be stored so that the
   native pipeline sees the message as done, `get_enrichment` returns it, and `produced_by` is
   recorded, without making the cache believe the small model produced it.

What exists (do not rebuild it): the agent registry and tokens (`core/agents.py`), scoped and
quota-checked claims (`JobQueue.claim(agent=…)`), `register_result` and `InvalidResult` (a bad
result leaves the job leased, so the agent can fix it), the `/mcp/agent` door and its bridge flag,
`oaken jobs claim|heartbeat|complete|fail`, `Pipeline.enrich_message` and `_enrich_input`, the
four enrichers in `src/oaken_enrichers/` (`summary`, `triage`, `actions` use the model;
`inspector` is deterministic), their `PromptBuilder` prompts and pydantic output models, and the
`UntrustedText` envelope.

## Decisions already taken

- **One door, one rule set.** Agents work through `/mcp/agent`, the HTTP agent routes and the CLI,
  with their own token. The person's MCP door does not gain job tools: an assistant that can read
  and change mail must not also be able to impersonate a worker.
- **Opt-in stays kind by kind.** A kind is offered only if it is in `[swarm] allowed_kinds`
  (`ingest.enrich` is the one this prompt is about). The person's real config enables it locally;
  it is never committed.
- **The agent's answer goes through the same validation as a native one.** Core, not the agent,
  checks the result against the enricher's output model before anything is stored.

## What to build

1. **A claimable payload for `ingest.enrich`.** `claim_job` on this kind returns, in addition to
   the ids: the message as core would hand its own enricher (`EnrichInput`: cleaned text,
   subject, `from`, the headers an enricher needs, attachment metadata, `is_reply`,
   `parent_summary`), **every field of mail origin wrapped in the `<<<EMAIL …>>>` markers**; the
   list of enrichers still owed and their `version`s; and, for each, its instructions and result
   schema (the `PromptBuilder` text and `output_schema` JSON Schema). Text is size-capped with a
   `truncated` flag, using the limits the native enrichers already use (`fit`).
2. **A result type for the kind.** `ingest.enrich` accepts, from an agent, a result of the form
   `{enrichments: {<enricher>: <that enricher's output>}, produced_by}`. Core validates each
   entry against that enricher's `output_model`, rejects an enricher that was not owed or is
   deterministic (the inspector always runs in core; an agent never supplies it), and returns an
   `invalid_result` the model can read and correct. A partial answer is allowed: the
   enrichers not answered stay owed and the job is requeued rather than completed (decide: see
   Q2).
3. **Storing it.** A valid entry is written to the same `enrichments` store the native path uses,
   with `produced_by` as the recorded model and the usual `MessageEnriched` event, and a usage
   row (`stage=enrich`, the agent's label, tokens if reported). After a full answer the job is
   completed with a `StageJobResult` (`done` = number stored) so the stage counts and the queue
   view stay truthful.
4. **Make the cache agree.** Native `_cached_enrichment` must treat an agent-produced row as
   current for that `(content_hash, enricher, version)` so the native stage does not redo it.
   Write the rule down (Q1) and test both directions: an agent row stops a native re-run; a
   `--force` or version bump still replaces it.
5. **Workspace and trust gating.** A job's mail text is handed only to an agent that the
   message's workspace allows (31/32): a `secure` workspace's mail is never offered to an agent
   that is not marked local (Q3). The agent record gains a `local` flag set at `agents create`
   (`--local`), recorded in the audit, and shown in `list_agents`.
6. **Skips and safety.** A message carrying the `analysis=skip_enrich` attr is never offered
   (same as `_skip_ai`). Mail text in a payload is untrusted data; the tool descriptions say so
   (they already do) and the instructions the agent receives come from core, never from the mail.
7. **Hygiene for small clients.** `claim_job` returns at once (a `job: null` when the queue is
   empty), a lease long enough for a slow model with `heartbeat_job` to extend it, and a payload
   that fits a modest context window (a `max_chars` input the agent may lower). `fail_job` with
   `retry=true` puts the job back without spending an attempt when the agent simply could not do
   it (Q4).
8. **CLI and docs.** `oaken jobs claim --kind ingest.enrich` prints the payload; `complete
   --result -` takes the new shape; `agents create --local`. Update 17's table, the swarm
   architecture page, and add a worked example for an LM Studio setup: an `mcp.json` entry that
   runs `oaken mcp --agent` with `OAKEN_AGENT_TOKEN` (or `--agent-token-file`), the claim/work/
   complete loop in a short system prompt, and how to create and revoke the agent.

## Acceptance criteria (FakeClock, fake model, no network)

- Claiming an `ingest.enrich` job returns the text, the owed enrichers, their instructions and
  schemas; every mail-derived field is inside the markers; oversized text sets `truncated`.
- A job whose message is in a `secure` workspace is not offered to a non-local agent, is offered
  to a local one, and a skipped message is never offered.
- `complete_job` with a valid answer stores the rows, publishes `MessageEnriched`, records
  `produced_by`, writes a usage row and completes the job.
- A schema-invalid answer, an unowed enricher, and an attempt to supply `inspector` are each
  rejected as `invalid_result` with a message naming the field; the job stays leased and nothing
  is stored.
- After an agent enriches a message the native stage skips it (`cached` counts it); `force`
  replaces it.
- A revoked agent, an agent over quota, or a kind outside `allowed_kinds` still cannot claim or
  complete (03b's tests keep passing).
- End to end through the stdio bridge: `oaken mcp --agent` lists the tools, claims a job and
  completes it; the person's door still lists none of them.

## Open questions (defaults in bold)

- Q1: an agent's row **satisfies the native cache** for that content and version regardless of
  model; the row records who produced it, and `reprocess enrich --force` replaces it.
- Q2: a **partial answer is accepted**: the stored enrichers count, the rest stay owed, and the job
  is requeued with its attempt not spent.
- Q3: the `local` flag is **set by the person at creation and trusted**; core cannot verify it.
  Alternative: infer it from a loopback client address.
- Q4: **`fail_job` gains `retry`** (requeue, attempt not spent) alongside `permanent`.
- Q5: the lease for this kind is **10 minutes**, extended by heartbeat, because a 27B on modest
  hardware is slow.

## Out of scope

Agents for kinds other than `ingest.enrich` (embed, parse and the rest are mechanical and stay in
core); letting an agent read or change mail through the swarm door; agent-written Sieve; a UI for
agents (Settings → MCP, prompt 32, may link to it later); spawn budgets and loop guards.

## Checkpoint

Standard report, plus one live demonstration: create a local agent, claim one queued job through
`oaken mcp --agent`, complete it with an answer written by a model, and show `oaken enrich show`
returning it and `oaken pipeline` counting it.
