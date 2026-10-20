# Spike: sqlite-vec

Result: **GO.** Method and numbers are summarised on the site (architecture/vectors); the script is
`docs/spikes/sqlite_vec_bench.py [latency|recall|consistency]`.

Machine: Linux, 16 cores, 29 GB, SQLite 3.50.4, sqlite-vec 0.1.9, no numpy. Cosine, brute force, k=10, 30 queries.

| N | dim | type | insert/s | p50 ms | p95 ms | MB |
|---|---|---|---|---|---|---|
| 10k | 384 | f32 | 66068 | 6.9 | 7.7 | 16.0 |
| 100k | 384 | f32 | 67894 | 66.7 | 73.7 | 156.6 |
| 300k | 384 | f32 | 65070 | 203.6 | 228.3 | 468.4 |
| 10k | 384 | int8 | 135982 | 6.1 | 6.6 | 4.2 |
| 100k | 384 | int8 | 130548 | 62.6 | 69.4 | 40.9 |
| 300k | 384 | int8 | 132415 | 181.5 | 189.4 | 122.3 |
| 10k | 768 | f32 | 8409 | 15.3 | 18.0 | 31.8 |
| 100k | 768 | f32 | 8737 | 142.6 | 157.9 | 310.9 |
| 300k | 768 | f32 | 8793 | 415.3 | 421.5 | 929.8 |
| 10k | 768 | int8 | 96075 | 12.4 | 13.6 | 8.1 |
| 100k | 768 | int8 | 96053 | 120.2 | 124.1 | 79.4 |
| 300k | 768 | int8 | 97742 | 359.4 | 385.2 | 237.6 |

Note: raw float32 insert speed drops about 8x from 384 to 768 dimensions.

## Recall (int8 vs float32, recall@10)

0.892 at 384 dimensions, 0.976 at 768.

## Metadata filtering (100k x 384)

- `rowid IN (subquery)` is exact at any selectivity: 27.8 ms (1% kept), 47.8 ms (none), 73.7 ms (99%).
- Post-filtering an oversampled result (k=200) returned 0 rows for a selective filter: wrong, rejected.

## Consistency

A rollback undid the FTS5 and vec0 rows together. 10,000 rows in 0.4 s while 4 reader threads ran 491 queries, 0 errors (WAL).

## Through the plugin (300k x 768)

Inserted in 78.8 s (3,807/s); search p50 548 ms, filtered p50 360 ms.

## Decision

Go. float32 default; int8 later. Pre-filter with `rowid IN`. macOS/Windows loading is verified by CI only.
