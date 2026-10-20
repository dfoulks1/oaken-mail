+++
title = "The swarm"
subtitle = "Optional extra workers, local or not"
lead = "Core runs its own parallel workers. If you have the tokens and the hardware, you can also let external AI agents lease work from the same queue. It is off by default and you choose what they may touch."
badges = [["text-bg-warning", "Pre-alpha"]]
group = "concepts"
order = 25
description = "External agents that lease jobs: opt-in config, scoped tokens, quotas, validated results, and matching Ollama parallelism."
+++

## Two kinds of parallelism

- **Internal workers.** `[jobs] workers` sets how many jobs core runs at once, and
  `[jobs] kind_limits` caps a kind of job (for example one `embed` job at a time on a single
  GPU). This is all most people need.
- **External agents (the swarm).** A separate program, such as a larger model driven from the
  command line or an MCP client, leases jobs from the same queue, does the work, and hands back a
  result. A laptop might run no agents. A workstation with a big model, or someone with lots of
  tokens to spend, might run several.

An agent uses exactly the rules internal workers do: leases with heartbeats, retries, fencing so
a stalled agent cannot overwrite the next one's result, and dead-worker recovery. Claiming work
goes through core, so nothing here lets an agent touch the databases.

## Turning it on

```toml
[swarm]
enabled = false          # nothing is offered to agents unless this is true
allowed_kinds = []       # job kinds an agent may ever lease, for example ["enrich"]
max_agents = 8           # active (unrevoked) agents
default_max_leases = 2   # jobs one agent may hold at once
default_max_per_hour = 600  # jobs one agent may claim per hour

[jobs]
workers = 4              # internal workers
max_result_bytes = 262144
```

Each setting can also be given as an environment variable, for example
`OAKEN_SWARM__ENABLED=true`. Both `enabled` and `allowed_kinds` have to be set, so turning the
swarm on does not hand any work to anyone by itself.

## Agents and their tokens

Each agent has its own token, its own list of job kinds, optionally the accounts it may see, and
its own quotas. The token is random, shown **once** when the agent is created, and only its
SHA-256 is stored. Revoking an agent takes effect at once: the jobs it held go back to the queue
without counting the attempt, and anything it sends afterwards is refused because it no longer
holds the lease. An agent token works only for the agent operations, never for the general API.

A claim is checked inside the same transaction that leases the job. The agent must not be
revoked, the kind must be in both the agent's list and `allowed_kinds`, the account must be one
the agent may see, and the agent must be under its limits (`quota_exceeded` otherwise). Quotas
are per agent, so one runaway agent cannot starve another.

## Results are untrusted

An agent's output is data from an outside party, and could come from a model that read a hostile
email. So a result is checked **before** anything is stored:

- it must match the schema registered for that job kind (jobs of a kind with no schema accept no
  result at all, so a job cannot be used to store arbitrary data),
- it must be under `max_result_bytes`,
- and a bad result is rejected with `invalid_result` and the job stays leased, so the agent can
  fix it or fail the job.

Each finished job records who finished it (`completed_by`) and which model the finisher says
produced the result (`produced_by`). The second is self-reported, and is a label for people, not
a guarantee. Results from the internal handlers go through the same validation.

## Privacy

Oaken Mail's promise is that mail leaves your machine only to clients you connect. Giving a job
to an agent sends that job's mail text to that agent, and if the agent is a hosted model, to its
provider. So:

- core never offers a job kind to an agent unless you listed it in `allowed_kinds`,
- you choose per kind, so you can let an agent summarize newsletters and never touch anything
  else,
- an agent can be limited to certain accounts,
- sending mail is never available to agents.

A local agent, for example a second Ollama instance on another machine you own, is under the same
rules. Whether it is "local" is your call, not something core can tell.

## Fan-out and attribution

A job can name a **parent**, and `JobQueue.children()` lists the children, so a coordinator can
split work into pieces and collect the results. There is no waiting on children in the scheduler
yet. Every job also records its **actor**: the user, an agent (with its label), or the system.
That attribution carries into the audit log and the rules for what an agent may change.

## Matching Ollama to the swarm

Core's limits and Ollama's limits are separate, and the smaller one wins.

- `OLLAMA_NUM_PARALLEL` is how many requests Ollama works on at once **per loaded model**. The
  default is chosen automatically (up to 4, or 1 when memory is tight). Each extra slot raises
  memory use, because the context is multiplied by it. Set it in the environment of the Ollama
  server, not in Oaken's config.
- `OLLAMA_MAX_LOADED_MODELS` limits how many models stay in memory together (for example an
  embedding model and a chat model).
- `OLLAMA_MAX_QUEUE` is how many requests it will hold before it answers busy.

A good starting point is to set `[jobs] kind_limits` for the model-using kinds equal to
`OLLAMA_NUM_PARALLEL`. Setting it higher only queues requests inside Ollama, and setting it lower
leaves capacity unused. For example, a machine that runs a large model with room for two
concurrent requests would use `OLLAMA_NUM_PARALLEL=2` and `kind_limits = { enrich = 2 }`. On a
small laptop, leave both at 1. If you also run agents, each agent's `max_leases` counts toward
whatever backend serves them, so lower the internal limit to leave room.

The model itself is a setting, not a default you are stuck with:
`[ai] llm_model = "qwen3:8b"` is a starting point, and a larger model is one line away.

## Over HTTP and the CLI

A person manages agents with three operations, which are never offered over MCP:
`create_agent` (the token is returned once and only its hash is kept), `list_agents` and
`revoke_agent`. An agent then works through four routes on the [local API](daemon.md), each
authenticated by its own token:

| Route | Does |
|---|---|
| `POST /v1/agent/jobs/claim` | Lease the next job of a kind the agent holds, `{"kinds": [...]}` optional |
| `POST /v1/agent/jobs/{id}/heartbeat` | Extend the lease; the answer says if the job was cancelled |
| `POST /v1/agent/jobs/{id}/complete` | Hand back `{"result": {...}, "produced_by": "model-name"}` |
| `POST /v1/agent/jobs/{id}/fail` | Report an error; `queued` if it will be retried, else `failed` |

The CLI covers both sides: `oaken agents create|list|revoke` for the person, and
`oaken jobs claim|heartbeat|complete|fail` for a scripted worker, which reads its token from
`OAKEN_AGENT_TOKEN` (or `--agent-token-file`) and never needs the API token.

An agent token opens only these routes, and no other route accepts one. A claim past the agent's
quota is a `429`, a result that does not match the kind's schema is a `422`, a kind the agent was not
given is a `403`, and a lease it no longer holds (expired, or the agent was revoked) is a `409`.
Revoking an agent puts its jobs back on the queue at once.

## Not built yet

The MCP front end for agents arrives with the MCP prompt. Spawn budgets and loop
guards for agents that create jobs belong with the enrichment work, where the token costs appear.
