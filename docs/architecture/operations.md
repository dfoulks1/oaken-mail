+++
title = "Operations"
subtitle = "Every capability is one typed operation, with a permission class and a confirm gate"
lead = "The CLI, the web UI and the MCP server do not each decide what is safe. They all call one registry of typed operations, and the rules live in code: a permission class on every operation, a confirm gate on every change, and a ceiling on what an agent may ever be offered."
badges = [["text-bg-warning", "Pre-alpha"]]
group = "concepts"
order = 50
description = "The operation registry, permission classes, the confirm gate and proposals, MCP exposure you control, result limits, plugin operations, the OpenAPI document and what is not built yet."
+++

## One registry, three doors

An operation has a name, a one-line summary, input and output models, a permission class and a
handler. The [full list](../reference/operations.html) is generated from the registry. The OpenAPI 3.1
document is derived from the same models, so it cannot disagree with the code.

A caller says which door it came through: `cli`, `ui`, `mcp` or `internal`. An `mcp` caller must be
an agent and a `cli` or `ui` caller must be a person. Only a person can confirm.

## Permission classes

| Class | Meaning |
|---|---|
| `read` | Changes nothing, and never marks mail read. |
| `mutate` | Changes something. Needs confirming. |
| `send` | Reserved. Refused in this version, and never offered over MCP. |

Defining an operation that breaks a rule fails at import, so it cannot ship: a `send` that could be
offered over MCP, a `read` that marks mail read, or a `mutate` an agent can reach that cannot say what
it would change.

## The confirm gate

Every call passes the same checks, in this order:

1. **Exposure.** An MCP caller gets "not available" for anything not offered to it, in the same words
   whether it is switched off or can never be offered.
2. **Input.** It must parse. The error names the field, never the value.
3. **Class.** `send` is refused.
4. **Confirmation, for `mutate`.**
   - A person at the CLI or UI confirms by passing `confirm`, meaning they asked for exactly this.
   - An agent cannot. It gets a **proposal**: what would change (ids with before and after, never
     mail text), a count, and a single-use token. Nothing has changed yet. It calls again with the
     token, which is accepted only for the same operation, the same input and the same caller.
5. **Run, validate the output, audit.**

Tokens are random, stored only as a hash, last two minutes by default, are burned by any attempt to
use them, and a caller can hold only so many at once.

## What an agent is offered

Each operation has a **ceiling** fixed in code: `never`, `off` or `on`.

- `never`: cannot be offered over MCP, whatever you set. Sending, raw mail bytes, the audit log,
  rebuilding the index, editing exposure itself and editing schedules.
- `off`: may be offered, and is not until you turn it on.
- `on`: offered by default.

You can lower an operation, or a whole plugin, from the default, and raise one up to its ceiling. The
setting is one global list in the database, changed through `set_operation_exposure`, which is never
available to an agent, so an agent cannot widen its own reach. Each change is audited. Per-token
scoping is left for the MCP server.

## Read state

Reading never marks mail read. The handler that marks it is the one place that does, after the gate
has confirmed it, and a test keeps the registry, the gate and the plugin bridge from naming the
read-state service at all.

`summarize_message` is a `mutate` with an option to mark the messages read afterwards. A summary on
its own never marks anything read: opening a message is the only automatic reason left. Several
messages can be summarized in one call (20 by default), then marked read together in one confirmed
step.

## Limits

Results are bounded, and the bounds are settings in `[operations]`. When in doubt, it is a setting.

| Setting | Default | Meaning |
|---|---|---|
| `max_results` / `mcp_max_results` | 50 / 20 | Rows in one listing, for you and for an agent |
| `max_chars` / `mcp_max_chars` | 20000 / 8000 | Mail text in one answer |
| `snippet_chars` | 240 | One snippet |
| `max_batch_ids` | 200 | Ids in one mark or undo |
| `max_summarize_ids` | 20 | Messages in one summary call |
| `max_raw_bytes` | 256 KiB | Raw bytes in one answer |
| `confirm_ttl_seconds` | 120 | How long a proposal lasts |
| `max_pending_confirmations` | 1000 | Open proposals |
| `proposal_changes_shown` | 50 | Changes listed in a proposal |

A request over a count limit is refused, not cut, so nothing is silently skipped. Text is cut and the
answer says it was.

## Plugin operations

A plugin can offer operations, named `<plugin>.<name>`. They get the same gate, the same audit and
the same ceiling rules, with these differences:

- they start **off** over MCP, and can never mark mail read;
- the handler gets no database handle, only its own service, its settings and the clock;
- a failure inside a plugin is reduced to the exception type, because it could quote mail;
- a call has a timeout, and an unhealthy plugin offers nothing.

## What is not built

- A **generic plugin settings store** for the UI. `list_plugins` shows each plugin's settings
  schema, but nothing yet writes them.
- `search` is on over MCP and `ask` is off until the user turns it on; both read through
  [retrieval](retrieval.md), which the [daemon](daemon.md) attaches to the registry along with the index, vectors and model
  service.
- **Summaries run inline**, one after another, capped per call. They are not a job.
- The registry's audit row is written just after the change, not in the same transaction. A crash in
  between loses the row, not the change. Operations that already audit atomically (read state,
  schedules, exposure) are not affected.
- Plugin operations come from a plugin's shared instance, not from per-account instances.
- The HTTP surface is built (the [daemon](daemon.md)), the MCP mount is not (prompt 18).
