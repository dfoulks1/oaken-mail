# 08 — Vector store: spike, then plugin

**Read first:** `docs/prompts/00-context.md`. **Depends on:** 04, 07.

## Goal

First prove sqlite-vec works everywhere we promise to run, then ship it as the
first `VectorStore` plugin. LanceDB is the documented fallback, not built unless the
spike fails.

## Part A — Spike (write findings, then continue only if it passes)

Write `docs/spikes/sqlite-vec.md` with measured results, not guesses:

1. Does `sqlite_vec` load via `enable_load_extension` under the **uv-managed Python**
   on Linux (this machine), and does CI show it on macOS and Windows? (Add a CI job
   that only runs the loader smoke test on all three; report results from the first CI
   run if available, otherwise say what remains unverified.)
2. Brute-force KNN latency for 10k / 100k / 300k vectors at 384 and 768 dimensions,
   float32 vs. int8 quantization, on this machine. Report p50/p95 and DB size.
3. Whether it can live inside `index.db` (same file as FTS5) with transactional
   consistency, and its behavior under concurrent read + write.
4. Decision: **go** (proceed with Part B) or **fallback** (stop and report with a
   concrete LanceDB proposal instead). If fallback, do not do Part B.

## Part B — Plugin (`oaken-vec-sqlite`, in-repo first-party plugin)

1. Implements the `VectorStore` contract from prompt 04 inside `index.db`:
   `vectors(chunk_id, model_id, dimensions, …)` plus the vec0 virtual table(s).
   **Model id and dimensions are stored per vector**; a different model gets a different
   table/partition and mixing is rejected with a clear error.
2. `query(vector, k, filters)` supports metadata filters (account, date range, sender,
   `thread_key`) by pre-filtering candidate chunk ids through `docs`, then KNN.
   Document the performance consequence of this approach.
3. `delete(message_ids)` removes vectors with their chunks; re-embedding the same
   `content_hash` + `model_id` is a no-op.
4. `reembed_plan(new_model_id)` reports how many chunks need new vectors and the
   estimated time from measured throughput (used later by the UI/CLI).
5. Passes `VectorStoreContract` from prompt 04 plus store-specific tests (persistence
   across reopen, dimension mismatch, empty store, k > N, duplicate upserts).

## Acceptance criteria

- Spike document committed with real numbers and a go/fallback decision.
- (If go) contract suite and store tests pass; a 50k-vector synthetic benchmark runs
  in CI-friendly time (mark heavier benchmarks `@pytest.mark.slow`, off by default).
- Core imports nothing from the plugin; selection is via entry point + config only.

## Out of scope

Embedding model calls (10), hybrid ranking (15), ANN indexes.

## Checkpoint

Standard report. Lead with the spike result and decision.
