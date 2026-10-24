# 15 — Hybrid retrieval, token-budgeted context, cited Q&A, eval harness

**Read first:** `docs/prompts/00-context.md`. **Depends on:** 08, 14.

## Goal

"Easy search with hyper-efficient token usage": hybrid retrieval, a context builder
that spends tokens deliberately, and Q&A whose every claim cites a message. Plus an
evaluation harness, because retrieval quality can't be tuned without one.

## Requirements

1. **Hybrid search** (`core/search/hybrid.py`): run FTS5 BM25 and vector KNN in
   parallel (vector side skipped, with a reported degraded flag, if the embedder is
   unavailable or vectors are stale), merge with **Reciprocal Rank Fusion**
   (configurable `k`, weights), collapse chunks to messages (best chunk per message,
   plus `matched_chunks`), apply filters (`from`, date range, thread, category/priority
   from enrichments, unread-only). Returns message-level hits with scores, snippets and
   `why` (which retriever matched). **Search never marks read.**
2. **Query understanding without an LLM first:** parse operators (`from:`, `before:`,
   `after:`, `is:unread`, `category:`), detect quoted phrases, and only then embed the
   remaining free text. An optional LLM query-rewrite step is behind a flag, off by default.
3. **Context builder** (`core/search/context.py`): given hits and a token budget
   (`ai.ask_context_tokens`), assembles evidence in this order of preference, spending
   the cheapest representation that suffices: stored `summary` + metadata → matched
   chunk(s) → neighboring chunks → thread siblings' summaries. Deduplicate overlapping
   chunks and near-identical quoted content; always include sender/date/subject
   as a compact header line. Reports `tokens_used`, `tokens_saved_vs_full_bodies`.
4. **`ask(question, filters)`:** retrieve → build context → LLM (via `PromptBuilder`,
   mail as `UntrustedText`) → structured answer: `answer`, `citations[] (message_id,
   chunk ordinal, quote)`, `confidence`, `insufficient_evidence: bool`. Verify each
   quote is a substring of the cited chunk; drop uncited claims or return
   `insufficient_evidence` rather than guessing. `ask` **does not** mark read (it is
   not "presenting" a message); cited messages are returned as references the UI can open.
5. **Eval harness** (`oaken/evals/`, `uv run python -m oaken.evals retrieval`):
   - A **synthetic corpus generator** (seeded, e.g. ~1,000 messages across threads,
     newsletters, receipts, scheduling, with planted facts) and a question set with
     ground-truth message ids. No real mail.
   - Metrics: Recall@k, MRR, nDCG@10 for keyword-only, vector-only, hybrid; answer
     citation precision; average context tokens per answered question; cost vs a
     naive "stuff top-10 full bodies" baseline.
   - Output a markdown report to `docs/evals/latest.md` and a JSON file; deterministic
     with the fake embedder in CI (assert hybrid ≥ each single retriever on the planted set),
     real-model runs are manual.

## Acceptance criteria

- On the synthetic corpus with `FakeEmbedder`, hybrid beats both single retrievers on
  nDCG@10 (test asserts this; if the fake can't show it, justify and adjust the fake, not the test).
- Context builder never exceeds the budget (property test) and reports savings ≥ 60%
  vs. naive full bodies on the corpus (report the actual number; if lower, say why).
- Citation validator rejects fabricated quotes; `insufficient_evidence` returned for
  questions whose answer isn't in the corpus.
- Vector outage → keyword results with `degraded=true`, no exception.
- Operator parsing tests, including malicious inputs.

## Out of scope

Reranking models, query rewriting by default, cross-account search, attachments.

## Docs site

Add `docs/architecture/retrieval.md` and register a generator that renders
`docs/evals/latest.json` as a *Reference → Evaluation results* page (metrics table per
retriever, tokens-saved figures, date and corpus seed). `check_docs.py` must pass.

## Checkpoint

Standard report. Paste the metrics table from the eval report.
