+++
title = "The search index"
subtitle = "index.db: everything derived from your mail, safe to delete and rebuild"
lead = "The index holds the parsed fields, cleaned text, chunks and a keyword index that make search fast. It is built from raw.db and can be thrown away at any time: rebuilding it costs minutes, never mail."
badges = [["text-bg-warning", "Pre-alpha"]]
group = "concepts"
order = 30
description = "index.db in Oaken: what it holds, how a message is chunked and indexed, how a search query is made safe, how the index is tied to one raw store, and how to rebuild it."
+++

## Disposable by design

`raw.db` is the truth. `index.db` is a **copy of what search needs**, in the shape search needs it.
Delete it and run a rebuild, and you get the same results back. That is also how a change in the
cleaning rules, the chunk size or the pipeline reaches old mail: re-derive, never re-download.

```text
raw.db message ──parse──▶ clean ──▶ chunk ──▶ index.db: docs, chunks, FTS5
                  │
                  └── thread key (a reply with no parent joins a recent thread with the same subject)
```

`index.db` has its own migration set, separate from `raw.db`.

## What is in it

| Table | What it holds |
| --- | --- |
| `index_meta` | The `pipeline_version`, when the index was created, and the **id of the raw store it was built from**. |
| `docs` | One row per message: account, thread key, subject, sender, recipients, date, list id, attachment metadata (never bytes), the cleaned text, a content hash and parse warnings. Also the message's disposition and labels, copied from `raw.db` so search can filter on them. |
| `chunks` | The cleaned text cut into pieces of about 256 tokens, each with a hash of its own text, a token count and the subject and sender kept in **separate fields** (context for a result, not part of the text). |
| `fts` | An FTS5 index over each chunk's subject, sender and text. |

A message with an empty body still has one empty chunk, so its subject and sender are searchable.
A forwarded message inside another is cleaned and chunked on its own and belongs to the parent.

## Idempotent by hash

A message's **content hash** covers everything that goes into the index (the parsed fields, the
cleaned text, the chunk settings and the pipeline version). Indexing a message whose hash has not
changed writes nothing. When it has changed, only the chunks whose own hash changed are rewritten,
so an embedding or summary attached to an unchanged chunk is kept.

## Keyword search

Search is ranked by BM25, with matches in the subject weighing more than the sender and the sender
more than the text. Results are one per message (its best chunk) with a highlighted snippet.

Your query is **never handed to FTS5 as it is**. It is read by a small parser into terms, phrases,
filters and negations, and each term is quoted before it reaches the index, so no input (quotes,
`NEAR(`, `*`, `OR 1=1`, ten thousand characters) can change what a query means or make it fail.

| You type | It means |
| --- | --- |
| `budget review` | both words |
| `"budget review"` | the phrase |
| `-invoice` | not this word (needs at least one other term) |
| `budg*` | words starting with `budg` |
| `from:alex` | the sender's name or address contains `alex` |
| `after:2024-03-01` `before:2024-04-01` | a date range, UTC; a time can follow as `2024-03-01T09:30` |
| `label:Lists` `in:discarded` | filter on what Sieve decided (`in:inbox`, `in:filed`, `in:discarded`, or `in:any` for all of them) (see [Sieve filtering](sieve.html)) |

A filter or term the parser could not read (a bad date, a lone `-word`) is reported back as a
warning, not silently dropped. **Messages Sieve discarded are hidden** unless you ask for them with
`in:discarded`; they are still in the index.

## Belongs to one raw store

`raw.db` holds a random store id. The index records it when it is built. An index opened against a
different `raw.db` is **refused**, so a copied or swapped file cannot quietly show you the wrong
mail. A different `pipeline_version` marks the index stale; a rebuild replaces it.

## Rebuilding

A rebuild walks every message in `raw.db` oldest first, parses, cleans, chunks and indexes it again.
It runs as a **job**, so it is resumable after a crash and reports progress. Messages whose hash
did not change cost almost nothing. A message that Sieve marked `skip_enrich` is indexed for
keywords like any other; only embedding and enrichment skip it.

## Settings

```toml
[ai]
chunk_tokens = 256       # target size of a chunk
chunk_overlap = 32       # tokens of the previous chunk repeated at the start of the next

[index]
subject_join_days = 30   # how recent a message with the same subject can be to join its thread
max_query_chars = 1000   # longer queries are cut
max_query_terms = 32
```
