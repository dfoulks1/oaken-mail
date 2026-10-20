+++
title = "The vector store"
subtitle = "sqlite-vec: embeddings for meaning-based search, beside the keyword index"
lead = "Vectors let a question find mail by meaning, not just by words. The first vector store is sqlite-vec, inside index.db: one file, one transaction, nothing to run."
badges = [["text-bg-warning", "Pre-alpha"]]
group = "concepts"
order = 35
description = "How Oaken stores and searches embeddings with sqlite-vec: the tables, filters, model swaps, measured speed and size, and why it was chosen."
+++

## Why sqlite-vec

A spike measured it on this project's own terms before any code was written (the script is
`docs/spikes/sqlite_vec_bench.py`, so you can rerun it). The decision was **go**:

- It loads under a normal Python on Linux; macOS and Windows are checked by CI, not yet by hand.
- FTS5 and the vectors share `index.db`, so one rollback undoes both. Ten thousand rows were
  written while four readers ran 491 queries with no errors (WAL).
- Search is brute force and exact. That is fast enough for a personal mailbox.

| Chunks | Dimensions | Search p50 | File size |
| ---: | ---: | ---: | ---: |
| 10,000 | 384 | 6.9 ms | 16 MB |
| 100,000 | 384 | 66.7 ms | 157 MB |
| 300,000 | 384 | 203.6 ms | 468 MB |
| 100,000 | 768 | 142.6 ms | 311 MB |
| 300,000 | 768 | 415.3 ms | 930 MB |

Through the store's own API, 300,000 chunks at 768 dimensions took 79 s to insert (3,800 a second),
with a p50 of 548 ms, or 360 ms with a filter that keeps a small share of the mail.

`int8` vectors are 4x smaller and a little faster, but recall at 10 fell to 0.89 at 384 dimensions
(0.98 at 768). **float32 is the default**; int8 is a setting: `dtype = "int8"` in
`[plugins.sqlite-vec]`. The choice is stored per model, so changing it applies to the next model
you build (see below), and the old vectors stay as they were.

## What it stores

| Table | Holds |
| --- | --- |
| `vec_models` | Each embedding model: id, dimensions, its `vec_N` table, and whether it is `active` or still `building`. |
| `vec_items` | One row per vector: model, message, chunk id, account and the chunk's content hash. The hash is required, so any vector can be traced to the exact text it came from. |
| `vec_N` | A `vec0` virtual table (cosine, float32), one per model. |

There are no triggers (a connection without the extension could not run them). `prune_orphans()`
removes vectors whose chunk no longer exists; call it after a reindex.

## Searching

Filters are applied **before** the nearest-neighbour search, so a narrow filter is still exact:
`account_id`, `model_id`, and, because the store sits beside the index, `thread_key`, `after`,
`before` and `sender`. An unknown filter is refused. Score is `1 - cosine distance`.

## Changing the embedding model

Vectors from two models cannot be compared. `begin_model` starts a new one beside the old,
`activate_model` switches search to it once it is full, and `drop_model` removes the old one.
`reembed_plan` reports how many chunks are left and, from the measured speed, how long.

## Installing

`pip install "oaken[vec]"`. Without the extra, the store reports a clear error naming it.
The plugin is registered as `sqlite-vec` in the `oaken.vector_stores` group, like any third-party one.
