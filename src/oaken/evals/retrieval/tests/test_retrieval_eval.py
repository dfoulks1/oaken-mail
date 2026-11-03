# SPDX-License-Identifier: Apache-2.0
"""The eval run offline, small enough for CI: hybrid must beat each single retriever."""

import json

import pytest

from oaken.evals.retrieval import report
from oaken.evals.retrieval.corpus import generate
from oaken.evals.retrieval.embedder import ConceptEmbedder
from oaken.evals.retrieval.run import Results, run
from oaken.testing.fakes import FakeLLM


@pytest.fixture(scope="module")
async def results() -> Results:
    corpus = generate(300, seed=1, per_kind=8)
    return await run(corpus, ConceptEmbedder(), FakeLLM(), date="2000-01-01")


async def test_hybrid_beats_both_single_retrievers_on_ndcg(results: Results) -> None:
    r = results.retrieval
    hybrid = r["hybrid"]["all"].ndcg_10
    assert hybrid > r["keyword"]["all"].ndcg_10 and hybrid > r["vector"]["all"].ndcg_10
    for kind in ("lexical", "paraphrase", "identifier", "thread"):
        best = max(r["keyword"][kind].ndcg_10, r["vector"][kind].ndcg_10)
        assert r["hybrid"][kind].ndcg_10 >= best - 1e-9, kind


async def test_the_report_carries_the_numbers_and_the_json_round_trips(results: Results) -> None:
    text = report.markdown(results)
    assert "nDCG@10" in text and "2000-01-01" in text
    data = json.loads(report.dumps(results))
    assert data["seed"] == 1 and data["retrieval"]["hybrid"]["all"]["ndcg_10"] > 0


async def test_a_rerun_gives_the_same_numbers(results: Results) -> None:
    again = await run(
        generate(300, seed=1, per_kind=8), ConceptEmbedder(), FakeLLM(),
        date="2000-01-01",
    )  # fmt: skip
    assert report.dumps(again) == report.dumps(results)
