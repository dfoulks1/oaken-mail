+++
title = "How Oaken counts token savings"
subtitle = "What the numbers in oaken usage mean, how each is worked out, and what they do not say"
lead = "Oaken saves tokens in two different ways and reports them separately. This page explains each one: what is counted, the arithmetic, who it is credited to, and the limits of an estimate."
badges = [["text-bg-warning", "Pre-alpha"]]
group = "concepts"
order = 20
description = "How Oaken measures token savings: cache savings (model calls skipped) and context savings (mail text kept out of a client's context window), split by CLI, UI, MCP and internal callers, with the formulas, the baseline and the caveats."
+++

## Two savings, kept apart

| | Cache savings | Context savings |
| --- | --- | --- |
| What was saved | A call to a local model that Oaken did not make | Mail text a client did not have to read |
| Whose tokens | Oaken's own models (embedder, enrichers, summaries) | The model that is calling Oaken, for example Claude over MCP |
| What it is worth | Compute and time with a local model | Billed tokens when that client is hosted |
| Where it is recorded | `ai_usage`, rows marked `cached` | `context_savings` |

They answer different questions, so Oaken never adds them together. Where a table shows both, they
are separate columns.

## Cache savings

Before Oaken runs a model on a message it looks for an answer it already has for the same content
hash, model and prompt version. If there is one, the call is skipped and a *cached* row is written
instead. For a cached row the token counts are what the call **would have** cost, input plus
output, taken from the answer that filled the cache.

`tokens_saved` is the sum of those cached rows. `tokens_in` and `tokens_out` are the sum of the
calls that really ran.

A large number here is normal after a rebuild, a re-enrich or a second sync: the work repeats and
the cache answers. With a local model this saves time and energy, not money.

## Context savings

A client that reads mail through Oaken gets snippets, summaries and cited evidence, not every
body. For each read operation that returns mail content, Oaken records two numbers:

- **Returned.** The size of the reply as JSON, divided by four. This is what the client actually
  took in.
- **Full.** For every distinct message the reply names, the length of its subject plus its cleaned
  text, added up and divided by four. This is what the client would have taken in by opening each
  of those messages whole.

The saving for a call is `full - returned`, never less than zero. A column of percentages is
`saved / full` for the rows it covers.

### Which operations are measured

`list_messages`, `search`, `search_keyword`, `ask`, `get_message`, `get_thread`, `get_enrichment`
and `summarize_message`.

Operations that change state, such as `mark_read`, are not measured. They name messages but return
almost nothing, so they would look like large savings without saving anything.

### An example

A search returns 5 hits as snippets. The reply is 842 tokens. The five messages in full would be
2,067 tokens. The saving is 1,225 tokens, 59 percent.

`get_message` is the honest case: it returns the whole cleaned text, so it saves little or
nothing. A client's `get_message` rows show exactly that.

## Who is credited

Every operation goes through one client that knows which door the call came in through: `cli`,
`ui`, `mcp` or `internal` (work Oaken does on its own, such as the ingest pipeline). That door is
recorded with each row, so `oaken usage` can show the savings by caller.

Rows written before callers were tracked appear as `unknown`.

## Reading oaken usage

- **usage.** Model calls actually made, per stage and model, with tokens spent and tokens saved by
  the cache. A totals row sits under a rule.
- **saved, by caller.** Cache tokens saved and context tokens saved for each door, and the share of
  context kept out.
- **context kept out of the client, by operation.** Calls, messages, tokens returned, the full
  cost, and the saving, for each caller and operation.

`oaken usage --json` returns the same data. `rows` holds the usage table and `savings` holds the
other two. The `savings_summary` operation returns them too, and is available over MCP.

## What these numbers do not say

- **They are estimates.** Everything is characters divided by four, the same rough rule the rest of
  Oaken uses for tokens. A hosted model's own tokenizer will differ, usually by tens of percent.
- **The baseline is plain.** "Full" means opening every named message in full. It does not say what
  a client would really have done without Oaken, which might have been to read less, or more. It is
  a fixed yardstick, and the same one every time.
- **Counting starts when tracking was added.** Calls before that have no context rows, and the
  cache rows from before have no caller.
- **A reply's size includes everything in it.** Metadata and field names count as returned
  tokens, so a very small reply can show less saving than its text alone would suggest.
- **No message text is stored.** Both tables hold only counts, names and timestamps.

## Where this lives

The recording is in `src/oaken/core/operations/savings.py` and the client that sets the caller is
`src/oaken/core/operations/client.py`. The tables are created by the `0016_savings` migration, and
the totals are read by the `savings_summary` operation.
