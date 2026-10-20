# SPDX-License-Identifier: Apache-2.0
"""Reference -> Evaluation results, rendered from ``docs/evals/latest.json``.

The numbers come from ``uv run python -m oaken.evals retrieval``; this page only formats them, so
it does not import ``oaken`` and a stale or missing file is a build error, not a blank page.
"""

import json
from typing import Any

import generated
from model import BuildError, Config, Page, page_from_meta

PAGE_ID = "reference/evaluation"
LABELS = {
    "keyword": "Keyword only (BM25)",
    "vector": "Vector only",
    "hybrid": "Hybrid (RRF, exact-text boost, minimum match)",
    "hybrid-plain": "Hybrid, plain RRF",
}
KINDS = ("lexical", "paraphrase", "identifier", "thread")
KIND_NOTES = {
    "lexical": "the question uses the message's own words",
    "paraphrase": "the question uses different words for the same idea",
    "identifier": "the question is about a code, number or other exact string",
    "thread": "the answer is in a reply, so the thread has to be followed",
}


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _load(cfg: Config) -> dict[str, Any]:
    path = cfg.docs_dir / "evals" / "latest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BuildError(
            f"{path}: cannot read the evaluation results ({exc}); run"
            " 'uv run python -m oaken.evals retrieval'"
        ) from exc
    if not isinstance(data, dict) or "retrieval" not in data or "context" not in data:
        raise BuildError(f"{path}: not a retrieval evaluation result")
    return data


def build(cfg: Config) -> Page:
    d = _load(cfg)
    ret, ctx, naive = d["retrieval"], d["context"], d["naive"]
    qs = d["questions"]
    answerable = sum(qs[k] for k in KINDS)
    mean_c, mean_n = ctx["mean_evidence_tokens"], naive["mean_evidence_tokens"]
    saved = 1 - mean_c / mean_n if mean_n else 0.0
    offline = d["mode"] == "offline"
    lines = [
        f"Measured on **{d['date']}** with corpus seed **{d['seed']}**: {d['messages']:,}"
        f" messages in {d['chunks']:,} chunks, {answerable} answerable questions"
        f" ({', '.join(f'{qs[k]} {k}' for k in KINDS)}) and {qs['unanswerable']} that the mail"
        " does not answer. How the pieces work: [Retrieval and ask](architecture/retrieval.md).",
        "",
        f"- Mode: **{d['mode']}**. Embedder: {d['embedder']}. Reader: {d['reader']}.",
        f"- Context budget: {d['budget']:,} tokens. Summaries in the context: {d['summaries']}.",
        "",
    ]
    if offline:
        lines += [
            "> An offline run uses stand-ins for both models, so it measures how the retrieval and"
            " context code behaves, not how any real model does. The corpus is synthetic and was"
            " built so that each retriever has a kind of question it is bad at. Do not read these"
            " numbers as what a real mailbox would score; a run with real models is a manual step"
            " (`--live`).",
            "",
        ]
    lines += [
        "## Retrieval",
        "",
        "Over the answerable questions. A message is relevant when it holds the planted fact.",
        "",
        "| Retriever | Recall@5 | Recall@10 | MRR | nDCG@10 |",
        "|---|---|---|---|---|",
    ]
    for key, label in LABELS.items():
        r = ret[key]["all"]
        lines.append(
            f"| {label} | {r['recall_5']:.3f} | {r['recall_10']:.3f} | {r['mrr']:.3f} |"
            f" {r['ndcg_10']:.3f} |"
        )
    lines += ["", "nDCG@10 by kind of question:", ""]
    lines += ["| Retriever | " + " | ".join(KINDS) + " |", "|---|" + "---|" * len(KINDS)]
    for key, label in LABELS.items():
        cells = " | ".join(f"{ret[key][k]['ndcg_10']:.3f}" for k in KINDS)
        lines.append(f"| {label} | {cells} |")
    lines += [""] + [f"- **{k}**: {KIND_NOTES[k]}." for k in KINDS]
    lines += [
        "",
        "## Answers and cost",
        "",
        "Every question goes through `ask`. The baseline puts the whole body of each of the top 10"
        " messages into the prompt, with no budget, and checks the reader's citations the same"
        " way.",
        "",
        "| | Context builder | Top 10 full bodies |",
        "|---|---|---|",
        f"| Answerable questions answered correctly | {ctx['correct']}/{answerable}"
        f" | {naive['correct']}/{answerable} |",
        f"| Unanswerable questions refused | {ctx['abstained_right']}/{qs['unanswerable']}"
        f" | {naive['abstained_right']}/{qs['unanswerable']} |",
        f"| Citation precision | {_pct(ctx['citation_precision'])}"
        f" | {_pct(naive['citation_precision'])} |",
        f"| Mean evidence tokens per question | {mean_c:,.0f} | {mean_n:,.0f} |",
        f"| Evidence tokens saved vs the baseline | {_pct(saved)} | |",
        f"| Mean share of the hits' full bodies not sent | {_pct(d['saved_fraction'])} | |",
        "",
        "Citation precision is the share of citations that point at the planted message and quote"
        " the planted fact. A claim whose quote is not in the cited excerpt is dropped before it"
        f" is counted ({ctx['dropped_claims']} dropped in this run).",
        "",
    ]
    if saved < 0.6:
        lines += [
            "The saving is below the 60% the design aims for. The messages in this synthetic"
            " mailbox are short, so the chunk that matched is often most of the message and there"
            " is little to leave out; the context builder's gain grows with the length of the"
            " mail. The figure is reported as measured.",
            "",
        ]
    lines += [
        "## Reproduce",
        "",
        "```",
        f"uv run python -m oaken.evals retrieval --messages {d['messages']} --seed {d['seed']}",
        "```",
        "",
        "The command rewrites `docs/evals/latest.md` and `docs/evals/latest.json`, which this page"
        " is built from. With the offline stand-ins it is deterministic.",
        "",
    ]
    return page_from_meta(
        PAGE_ID,
        "Evaluation results",
        "\n".join(lines),
        {
            "subtitle": "Evaluation results",
            "lead": "How well hybrid search finds the right message and how few tokens it spends,"
            " rendered from the latest run.",
            "badges": [["text-bg-secondary", "Generated"]],
            "group": "reference",
            "order": 30,
            "description": "Recall, MRR and nDCG for keyword, vector and hybrid search, and the"
            " token cost of cited answers, from the latest evaluation run.",
        },
        None,
        False,
    )


generated.register(PAGE_ID, build)
