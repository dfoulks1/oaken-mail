# 07 — `index.db`, chunking, FTS5, rebuild-from-raw

**Read first:** `docs/prompts/00-context.md`. **Depends on:** 05, 06.

## Goal

The derived, **disposable** index: parsed fields, cleaned text, chunks and a keyword
index, all rebuildable from `raw.db`. Replaces the legacy hand-rolled BM25.

## Requirements

1. **Separate DB file and migration set** (`index.db`). It must be safe to delete.
   A `schema`-level `index_meta` table records `pipeline_version`, creation time and
   the source `raw.db` identity (a random id stored in `raw.db`), so an index built from
   a *different* raw store is detected and refused.
2. **Schema:**
   - `docs(message_id PK, account_id, thread_key, subject, from_addr, from_name, to_json,
     date_utc, list_id, has_attachments, cleaned_text, content_hash, parse_warnings_json)`.
   - `chunks(id, message_id, ordinal, text, token_estimate, content_hash)`.
   - FTS5 virtual table over `subject`, `from`, `cleaned chunks` with the `unicode61`
     tokenizer + diacritics folding; external-content or contentless-delete (choose
     and justify) so updates/deletes stay consistent. Prefix indexes for typeahead.
   - `chunks` carry a `content_hash` so downstream embeddings/enrichments can skip
     unchanged chunks.
3. **Chunker** (`mail/chunk.py`): token-aware (use a cheap estimator, e.g.
   `len(text)/4`, behind an interface so a real tokenizer can plug in), paragraph/sentence
   boundaries first, target and overlap configurable (`ai.chunk_tokens` default 256,
   overlap 32). Each chunk is prefixed in a *separate field* (not in the text) with
   subject/sender so retrieval can show context without bloating the text.
4. **`IndexStore` API:** `index_message(msg_id, parsed, cleaned) -> IndexResult`
   (idempotent by `content_hash`), `delete_message`, `keyword_search(query, filters, k)`
   with BM25 ranking and highlighted snippets, filter by `from`, `date` range,
   `thread_key`, `has_attachments`, `list_id`. A safe query parser: user text is
   **never** passed raw to FTS5 `MATCH` (escape/quote; support `"phrases"`, `from:`,
   `before:`/`after:`, `-negation`).
5. **`rebuild_index(raw_store, index_store, *, since=None)`**: streams every message
   from `raw.db`, re-parses, re-cleans, re-chunks, re-indexes; resumable and enqueued as
   a **job** (prompt 03) with progress events. `oaken`-level `pipeline_version`
   bump invalidates the index and triggers this.
6. Emit `MessageParsed` / `MessageIndexed` events.

## Acceptance criteria

- Delete `index.db` → `rebuild_index` → identical search results (compare on the
  synthetic corpus from 06/05).
- Re-indexing an unchanged message performs zero chunk writes (assert via counters).
- FTS injection tests: queries like `" OR 1=1 --`, `NEAR(`, `*`, unbalanced quotes,
  and 10k-char inputs never raise and never change query semantics unexpectedly.
- Search relevance smoke test: on a 200-message synthetic corpus, queries for known
  planted phrases return the planted message in the top 3.
- `index.db` built from raw store A is refused when attached to raw store B.

## Out of scope

Vectors (08), pipeline orchestration (09).

## Checkpoint

Standard report. Include index size vs. `raw.db` size on the synthetic corpus, and
the legacy comparison: the old `term_freq` table was ~3.0M rows for ~1.1k emails;
report the equivalent FTS5 size for a similarly sized synthetic corpus.
