# SPDX-License-Identifier: Apache-2.0
"""Turn a ``Results`` into ``latest.json`` (what the docs site renders) and ``latest.md``."""

import dataclasses
import json
from typing import Any

from oaken.evals.retrieval.corpus import KINDS
from oaken.evals.retrieval.run import RETRIEVERS, Results

LABELS = {
    "keyword": "keyword only (BM25)",
    "vector": "vector only",
    "hybrid": "hybrid (RRF + exact-text boost + minimum match)",
    "hybrid-plain": "hybrid, plain RRF",
}
ANSWERABLE = tuple(k for k in KINDS if k != "unanswerable")


def as_json(r: Results) -> dict[str, Any]:
    """The results as plain data, with the figures a reader wants worked out."""
    return dataclasses.asdict(r)


def dumps(r: Results) -> str:
    return json.dumps(as_json(r), indent=2, sort_keys=True) + "\n"


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
        f"- Mode: **{r.mode}**. Embedder: {r.embedder}.",
        "",
    ]
    if r.mode == "offline":
        lines += [
            "> An offline run uses a stand-in embedder, so it measures how the retrieval code behaves, "
            "not how any real model does. The corpus is synthetic and was "
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
    return "\n".join(lines)

