# SPDX-License-Identifier: Apache-2.0
"""Turn a ``Results`` into ``latest.json`` (what the docs site renders) and ``latest.md``."""

import dataclasses
import json
from typing import Any

from oaken.evals.retrieval.corpus import KINDS
from oaken.evals.retrieval.run import RETRIEVERS, AskScores, Results

LABELS = {
    "keyword": "keyword only (BM25)",
    "vector": "vector only",
    "hybrid": "hybrid (RRF + exact-text boost + minimum match)",
    "hybrid-plain": "hybrid, plain RRF",
}
ANSWERABLE = tuple(k for k in KINDS if k != "unanswerable")


def as_json(r: Results) -> dict[str, Any]:
    """The results as plain data, with the figures a reader wants worked out."""
    data = dataclasses.asdict(r)
    for key, scores in (("context", r.context), ("naive", r.naive)):
        data[key]["citation_precision"] = scores.citation_precision
        data[key]["mean_evidence_tokens"] = mean_tokens(scores)
    data["tokens_saved_vs_naive"] = tokens_saved(r)
    return data


def mean_tokens(a: AskScores) -> float:
    return a.evidence_tokens / a.reached_reader if a.reached_reader else 0.0


def tokens_saved(r: Results) -> float:
    """Share of the naive baseline's evidence tokens the context builder did not spend."""
    naive = mean_tokens(r.naive)
    return 1 - mean_tokens(r.context) / naive if naive else 0.0


def dumps(r: Results) -> str:
    return json.dumps(as_json(r), indent=2, sort_keys=True) + "\n"


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def markdown(r: Results) -> str:
    q = r.questions
    answerable = sum(q[k] for k in ANSWERABLE)
    lines = [
        "# Retrieval evaluation",
        "",
        f"Run on {r.date}, corpus seed {r.seed}. {r.messages} messages in {r.chunks} chunks; "
        f"{answerable} answerable questions "
        f"({', '.join(f'{q[k]} {k}' for k in ANSWERABLE)}) and {q['unanswerable']} unanswerable.",
        "",
        f"- Mode: **{r.mode}**. Embedder: {r.embedder}. Reader: {r.reader}.",
        f"- Summaries in the context: {r.summaries}.",
        f"- Context budget: {r.budget} tokens.",
        "",
    ]
    if r.mode == "offline":
        lines += [
            "> An offline run uses stand-ins for both models, so it measures how the retrieval and "
            "context code behaves, not how any real model does. The corpus is synthetic and was "
            "built so each retriever has a case it is bad at.",
            "",
        ]
    lines += [
        "## Retrieval",
        "",
        "Over the answerable questions; relevant is the one message holding the planted fact.",
        "",
        "| Retriever | Recall@5 | Recall@10 | MRR | nDCG@10 |",
        "|---|---|---|---|---|",
    ]
    for name in RETRIEVERS:
        s = r.retrieval[name]["all"]
        lines.append(
            f"| {LABELS[name]} | {s.recall_5:.3f} | {s.recall_10:.3f} | {s.mrr:.3f} | "
            f"{s.ndcg_10:.3f} |"
        )
    lines += ["", "nDCG@10 by kind of question:", "", f"| Retriever | {' | '.join(ANSWERABLE)} |"]
    lines.append("|---|" + "---|" * len(ANSWERABLE))
    for name in RETRIEVERS:
        cells = " | ".join(f"{r.retrieval[name][k].ndcg_10:.3f}" for k in ANSWERABLE)
        lines.append(f"| {LABELS[name]} | {cells} |")
    c, n = r.context, r.naive
    lines += [
        "",
        "## Answers and cost",
        "",
        "Every question goes through `ask`. The baseline sends the whole body of each of the top "
        "10 messages, with no budget, to the same reader and checks its citations the same way.",
        "",
        "| | Context builder | Top 10 full bodies |",
        "|---|---|---|",
        f"| Answerable questions answered correctly | {_correct(c, answerable)} "
        f"| {_correct(n, answerable)} |",
        f"| Unanswerable questions refused | {c.abstained_right}/{q['unanswerable']} "
        f"| {n.abstained_right}/{q['unanswerable']} |",
        f"| Citation precision | {_pct(c.citation_precision)} | {_pct(n.citation_precision)} |",
        f"| Mean evidence tokens per question | {mean_tokens(c):.0f} | {mean_tokens(n):.0f} |",
        f"| Evidence tokens saved vs the baseline | {_pct(tokens_saved(r))} | |",
        f"| Mean share of the hits' full bodies not sent | {_pct(r.saved_fraction)} | |",
        "",
        "Citation precision is the share of citations that point at the planted message and quote "
        "the planted fact. A claim whose quote is not in the cited excerpt is dropped before it is "
        f"counted ({c.dropped_claims} dropped in this run).",
        "",
    ]
    return "\n".join(lines)


def _correct(a: AskScores, answerable: int) -> str:
    return f"{sum(v[1] for k, v in a.by_kind.items() if k != 'unanswerable')}/{answerable}"
