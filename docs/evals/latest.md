# Retrieval evaluation

Run on 2026-10-20, corpus seed 0. 1000 messages in 2249 chunks; 48 answerable questions (12 lexical, 12 paraphrase, 12 identifier, 12 thread) and 12 unanswerable.

- Mode: **offline**. Embedder: concept-embed (offline stand-in). Reader: extractive stand-in (not a model).
- Summaries in the context: first sentence (stand-in for the summary enricher).
- Context budget: 3000 tokens.

> An offline run uses stand-ins for both models, so it measures how the retrieval and context code behaves, not how any real model does. The corpus is synthetic and was built so each retriever has a case it is bad at.

## Retrieval

Over the answerable questions; relevant means the one message that holds the planted fact.

| Retriever | Recall@5 | Recall@10 | MRR | nDCG@10 |
|---|---|---|---|---|
| keyword only (BM25) | 0.750 | 0.750 | 0.740 | 0.742 |
| vector only | 0.625 | 0.625 | 0.546 | 0.562 |
| hybrid (RRF + exact-text boost + minimum match) | 1.000 | 1.000 | 0.990 | 0.992 |
| hybrid, plain RRF | 0.646 | 0.667 | 0.605 | 0.612 |

nDCG@10 by kind of question:

| Retriever | lexical | paraphrase | identifier | thread |
|---|---|---|---|---|
| keyword only (BM25) | 0.969 | 0.000 | 1.000 | 1.000 |
| vector only | 0.969 | 1.000 | 0.000 | 0.279 |
| hybrid (RRF + exact-text boost + minimum match) | 0.969 | 1.000 | 1.000 | 1.000 |
| hybrid, plain RRF | 0.969 | 0.000 | 0.556 | 0.922 |

## Answers and cost

Every question goes through `ask`. The baseline sends the whole body of each of the top 10 messages, with no budget, to the same reader and checks its citations the same way.

| | Context builder | Top 10 full bodies |
|---|---|---|
| Answerable questions answered correctly | 48/48 | 48/48 |
| Unanswerable questions refused | 12/12 | 12/12 |
| Citation precision | 100.0% | 100.0% |
| Mean evidence tokens per question | 1885 | 3503 |
| Evidence tokens saved vs the baseline | 46.2% | |
| Mean share of the hits' full bodies not sent | 32.0% | |

Citation precision is the share of citations that point at the planted message and quote the planted fact. A claim whose quote is not in the cited excerpt is dropped before it is counted (0 dropped in this run).
