+++
title = "Retrieval and ask"
subtitle = "Hybrid search, a token-budgeted context and answers that quote their sources"
lead = "Search runs the keyword index and the vector store side by side and merges them. Ask builds a small context from the best hits and holds the model to it: every claim must quote the message it came from, and a quote that is not really there is dropped."
badges = [["text-bg-warning", "Pre-alpha"]]
group = "concepts"
order = 55
description = "How hybrid search merges BM25 and vector results with rank fusion, how the context is built within a token budget, how cited answers are verified, what happens when the vector side is down, and how it is measured."
+++

## Search

`search` asks two retrievers at once and merges what they find.

| Retriever | Finds | Misses |
| --- | --- | --- |
| Keyword (FTS5, BM25) | the exact words, names and identifiers | a question that says the same thing in other words |
| Vector (nearest chunks) | the same meaning in different words | an order number, a code, an exact phrase |

Each ranked list is first filtered, and chunks are collapsed to messages (the best chunk stands
for the message, and the other chunks that matched are kept as `matched_chunks`). A message then
scores the sum of `weight / (rrf_k + rank)` over the lists it appears in: Reciprocal Rank Fusion.
Only ranks are used, so a BM25 score and a cosine similarity never have to be compared. Every hit
carries its score, a snippet and `why`: which retrievers found it.

Settings, under `[search]`:

| Setting | Default | What it does |
| --- | --- | --- |
| `rrf_k` | 60 | A larger value flattens the gap between the first and the tenth hit. |
| `keyword_weight`, `vector_weight` | 1.0 | Scale each retriever's contribution. |
| `exact_boost` | 3.0 | Multiplies the keyword weight when the query has a quoted phrase or a token with a digit, which meaning cannot tell apart. |
| `keyword_min_match` | 0.5 | For a question, a keyword hit must hold at least this share of its words. |
| `overfetch`, `max_candidates` | 5, 500 | How many candidates each retriever returns per hit wanted, and how far a search widens when filters leave too few. |
| `vector_timeout_seconds` | 10 | After this the vector side is given up on. |
| `llm_rewrite` | off | Rephrase the free text with the model before it is embedded. |

### Reading the query

No model is involved. The query is read into words, `"phrases"`, `-exclusions` and operators,
and every word is quoted before it reaches SQLite, so nothing a person types can be FTS5 syntax.

| Operator | Meaning |
| --- | --- |
| `from:alex` | sender name or address |
| `after:2024-03-01`, `before:2024-04-01` | date range (UTC) |
| `label:Lists`, `in:discarded` | label, disposition |
| `is:unread`, `is:read` | read state |
| `category:work` | the triage category (repeat for several) |
| `priority:2` | triage priority 2 or higher |

A value that cannot be read (`priority:9`, `category:nope`) is reported as a warning and filters
nothing. Read state and the triage result live in `raw.db`, not in the index, so those filters are
applied to the candidates there. The operation also takes the same filters as fields, plus a
thread. A query with no words, such as `is:unread from:alex`, lists the newest matching messages.

### When the vector side is unavailable

Search never fails because of it. If there is no embedder, no vector store, no vectors yet, the
vectors were built by another model (so they would mean nothing), the call times out or it raises,
the keyword results come back with `degraded: true` and a `degraded_reason`. A stale index is
treated like a missing one rather than mixed in.

### Search does not mark mail read

A result is a reference. Opening a message is what changes read state, and that is a separate,
user-initiated operation.

## Ask

`ask(question, filters)` retrieves, builds a context, and asks the model to answer in **claims**.
Each claim has the excerpt it rests on and the exact words it quotes.

1. A question has its stop words removed for the keyword side, which then matches messages
   containing at least half of what is left; the vector side gets the question as written.
2. The context builder spends `ai.ask_context_tokens` (3,000 by default) in tiers, across all
   hits in rank order before going deeper into any: a header line and the stored summary for each
   hit; the matched chunks for the first `ask_chunk_hits`; the neighbouring chunks and the
   summaries of the rest of the thread for the first `ask_neighbor_hits`.
3. Near-identical chunks are sent once, and words a neighbouring chunk already shows are cut. The
   total is counted again at the end and trimmed, so the budget is a limit and not a target.
4. The mail goes to the model as `UntrustedText`, one block per message, behind the usual data
   rules (see [AI safety](ai-safety.md)).
5. Core keeps a claim only if its excerpt was in the context and its quote really is in that
   chunk's stored text (whitespace may differ, nothing else may). Claims that fail are dropped and
   counted. If any were dropped, confidence is `low`: a model that invented something is not
   trusted to rate itself. If none are left, the answer is `insufficient_evidence`, and the text
   says so instead of guessing.

The result has `answer`, `citations` (message, chunk ordinal, the quote as it stands in the
message, and its offsets), `confidence`, `insufficient_evidence`, `dropped_claims`, `degraded`
and the token figures: `evidence_tokens` and `full_body_tokens`, the cost of sending every hit
whole, so the saving is visible on every call. When nothing is found there is no model call at all.

Mail from more than one account is never put in one prompt, because an account decides which
workspace, and so which models, may see it. A question that finds mail in several accounts is
refused with a request to name one. `ask` does not mark anything read.

`ask` is off over MCP by default. It would let an agent read mail by asking questions, so the
user turns it on, like any operation (see [Operations](operations.md)).

## How it is measured

`uv run python -m oaken.evals retrieval` builds a seeded synthetic mailbox of about a thousand
messages with planted facts and questions about them, ingests it through the real pipeline and
scores each retriever. [Evaluation results](../reference/evaluation.html) shows the latest run. The
corpus has four kinds of answerable question and one kind that the mail does not answer, chosen so
that keyword search and vector search each fail on one of them. That is why hybrid wins by a wide
margin offline, and why the numbers say little about real mail.

Offline, the embedder and the reader are stand-ins: a small concept embedder and an extractive
reader that quotes the sentence closest to the question. The run is deterministic, and a smaller
run is a test, which asserts that hybrid is at least as good as each single retriever. A run with
a real embedder and model (`--live`) is manual and is not part of CI.

## What it does not do

No reranking model, no cross-account search, no search inside attachments, and no query rewriting
unless `llm_rewrite` is on.
