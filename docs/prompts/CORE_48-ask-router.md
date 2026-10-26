# 48 — `ask` as a router: understand the request, then do the right thing

> **Status: built (the first slice).** `AskIntent` and `BaseEnricher.ask_intent` (snapshot updated),
> declared by `summary`, `triage`, `actions` and `inspector`; `PromptBuilder.extended(catalog)`;
> `core/search/route.py` (the `ROUTE` prompt, `Route`, `catalog`, `sources`, `rule_route`); `ask`
> dispatches in `messages.py` and returns `intent`, `interpreted` and `results`; `oaken ask` prints
> them. The router's call is cached per question and catalog. Deviations: a plain question with no
> keyword goes to `qa` and a statement to `find` when the model cannot be used; `AskInput` gained
> `limit` (default 10); the router does not see account names (Q3). Not built: running an enricher
> on demand from `ask`.

**Read first:** `docs/prompts/00-context.md`, `CORE_15-retrieval-and-ask.md`,
`ENRICHER_14-enrichers.md` (the enrichers) and ADR 0010 (the stable plugin surface).
**Depends on:** 13, 14, 15. Nothing unbuilt.
**Track:** CORE. Touches `ask` (`src/oaken/core/operations/builtin/messages.py`), the prompt
builder (`src/oaken/ai/prompts.py`), `BaseEnricher` and the contract snapshot, and the first-party
enrichers. Adds a router in `src/oaken/core/search/`.

## Why

`ask` today is one thing: cited question answering. A question that is not a factual lookup ("what
needs me today?", "summarize what Alex sent", "any deadlines this week?") has no quote to cite, so
it comes back "there is not enough in your mail" and the person gets nothing. It also refuses to
search two accounts at once. A person expects `ask` to work out what was asked and do it.

## Goal

`ask "<anything>"` first works out the intent with a small model, then runs the matching read
operation, and always says how it read the request. Enrichers extend what it can understand: an
enricher declares an intent, and the router's menu is built from the enrichers that are installed.

## Decisions already taken (do not re-litigate)

- **The router sees only the question, never mail.** Its system prompt is a fixed template plus a
  catalog of intents declared by installed enrichers (plugin code, validated, never mail text), so
  the rule "mail text never enters a system prompt" holds. The question itself travels as
  `UntrustedText` in the user turn.
- **Enrichers extend it.** `BaseEnricher.ask_intent` is an optional `AskIntent` (a name, a short
  description of what a person would be asking for, and the result fields worth showing). The
  catalog is generated from the enrichers the host has started, so a new enricher teaches `ask` a
  new skill with no core change.
- **Everything dispatched is a read.** The router chooses among read paths only. `ask` never
  changes mail.
- **Stored enrichments, no new model calls.** An enrichment intent shows what is already stored.
  Mail that has not been enriched is listed as such, never silently dropped.
- **`qa` stays.** Cited question answering is one intent, unchanged, for factual lookups.
- **`ask` stays `mcp="off"`.** An AI client picks tools itself; this is for people.

## Not in scope

Running enrichers on demand from `ask`, multi-step plans, writing mail, per-intent permissions,
the idle-hours scheduler, and a UI. Cross-account `qa` still asks the model per account.

## Requirements

1. **`AskIntent`** (frozen dataclass in `contracts.py`): `name` (`[a-z_]{1,32}`), `description`
   (one plain sentence, at most 120 characters, no line breaks), `fields` (result keys to show;
   empty means all). Invalid declarations are refused at plugin load. Snapshot updated.
2. **`BaseEnricher.ask_intent`** (class variable, default `None`). The first-party enrichers
   declare: `summary`, `triage`, `actions`, `inspector`.
3. **`PromptBuilder.extended(catalog)`** returns a builder whose system text adds the catalog under a
   fixed header. The prompt version includes a digest of the result, so caches never mix catalogs.
4. **The router.** `ROUTE` prompt and `Route` answer (`intent`, `sender`, `days`, `unread`,
   `query`). The model supplies a period as a number of days and a sender as text; core does the
   date arithmetic and the lookups. An unknown or missing intent, a model error or a timeout falls
   back to a rule that reads obvious words ("summar", "gist", "urgent", "important", "to-do",
   "deadline"), and then to `find`.
5. **Dispatch.** Core intents: `find` (matching messages with their stored enrichments) and `qa`
   (today's cited answer). Each enricher intent returns the matching messages (newest first, up to
   a limit, narrowed by sender, days and unread) with that enricher's stored result, trimmed to its
   `fields`; a message with no stored result is returned with `not_enriched`. The `triage` intent
   also orders by priority.
6. **Output.** `AskOutput` gains `intent`, `interpreted` (one plain sentence: "read as: ..."), and
   `results` (message id, subject, sender, date, the shown fields, `not_enriched`). Existing fields
   are unchanged; the `qa` path fills them as before.
7. **Accounts.** `find` and the enricher intents search every account. Only `qa` keeps one account
   per model call.
8. **Failure.** With no model service the router is skipped and the rule decides. `ask` never
   returns an empty answer for a request it understood: no results says so.
9. **Docs and CLI.** `oaken ask` prints the interpretation first, then the results. Update the
   retrieval page, the plugin guide (`ask_intent`) and 17's table.

## Acceptance criteria

- "summarize what Alex sent last week" routes to `summary`, with `sender` and `days = 7`, and
  returns stored summaries; a message without one is marked `not_enriched`.
- "what needs my attention" routes to `triage` and orders by priority, highest first.
- A factual question routes to `qa` and answers exactly as before (same output, same refusals).
- A new enricher with an `ask_intent` appears in the catalog and can be chosen, with no core change;
  an invalid `AskIntent` is refused at load with a message naming the field.
- Mail text never appears in the router's system prompt; a question containing an instruction is
  treated as data (it can only choose among the fixed intents).
- A router failure falls back to the rule and then to `find`; nothing raises to the caller.
- Two accounts: `find` and the enricher intents return mail from both.
- `ruff`, `ruff format --check`, `mypy`, `pytest` and the docs check are green.

## Open questions (defaults in bold)

- Q1: the default result limit: **10**, `limit` adjustable.
- Q2: an intent with no matching mail: **say so plainly** ("no matching mail"), not an error.
- Q3: should the router see the list of account names to resolve "my work account"? **No**: later.

## Report-back checkpoint

End with the six sections from `00-context.md`. Then stop.
