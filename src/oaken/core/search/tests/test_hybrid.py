# SPDX-License-Identifier: Apache-2.0
import asyncio
import random
import string
from typing import Any

import pytest

from oaken.core.ids import MessageId
from oaken.core.search import HybridFilters
from oaken.core.search.tests.conftest import Box
from oaken.core.settings import SearchSettings
from oaken.index.store import SearchFilters
from oaken.ingest.backends import Vectors
from oaken.testing.fakes import InMemoryVectorStore


async def corpus(box: Box) -> dict[str, MessageId]:
    return {
        "budget": await box.add("Budget", "The zebrafinch budget review is on Thursday.", day=1),
        "lunch": await box.add("Lunch", "Lunch plans for the team on Friday.", day=2, sender="bo"),
        "trip": await box.add("Trip", "Flights for the quokkaland trip are booked.", day=3),
    }


async def test_a_message_found_by_both_retrievers_outranks_one_found_by_one(box: Box) -> None:
    ids = await corpus(box)
    found = await box.retrieval.search("zebrafinch")
    top = found.hits[0]
    assert top.hit.message_id == ids["budget"] and top.why == ("keyword", "vector")
    assert top.keyword_rank == 1 and top.vector_rank is not None
    cfg = box.retrieval.hybrid.cfg
    expected = cfg.keyword_weight / (cfg.rrf_k + 1) + cfg.vector_weight / (
        cfg.rrf_k + top.vector_rank
    )
    assert top.hit.score == pytest.approx(expected)


async def test_weights_and_k_change_the_fusion(box: Box) -> None:
    ids = await corpus(box)
    hybrid = box.retrieval.hybrid
    hybrid.cfg = SearchSettings(vector_weight=0.0, rrf_k=10)
    top = (await box.retrieval.search("zebrafinch")).hits[0]
    assert top.hit.message_id == ids["budget"]
    assert top.hit.score == pytest.approx(1.0 / 11)


async def test_every_hit_says_why_and_carries_a_snippet_and_matched_chunk(box: Box) -> None:
    await corpus(box)
    (hit,) = (await box.retrieval.search("zebrafinch", k=1)).hits
    assert "zebrafinch" in hit.hit.snippet and hit.matched_chunks
    assert "keyword" in hit.matched_chunks[0].by and hit.why


async def test_chunks_collapse_to_one_hit_per_message(box: Box) -> None:
    paragraphs = [f"Paragraph {i} " + "filler words about nothing much " * 60 for i in range(12)]
    paragraphs[3] += " The quokkamoon launch code is blue."
    paragraphs[9] += " The quokkamoon rehearsal is Tuesday."
    mid = await box.add("Long", "\n\n".join(paragraphs))
    await box.add("Short", "Nothing to see.")
    found = await box.retrieval.search("quokkamoon")
    assert [h.hit.message_id for h in found.hits].count(mid) == 1
    first = found.hits[0]
    assert first.hit.message_id == mid and first.matched_chunks
    ordinals = [c.ordinal for c in first.matched_chunks]
    assert len(ordinals) == len(set(ordinals))


async def test_filters_from_dates_thread_and_operators(box: Box) -> None:
    ids = await corpus(box)

    async def got(text: str, **kw: Any) -> list[str]:
        f = HybridFilters(index=SearchFilters(**kw))
        return [h.hit.message_id for h in (await box.retrieval.search(text, f)).hits]

    assert await got("plans from:bo") == [ids["lunch"]]
    assert ids["lunch"] not in await got("plans", from_=("alex",))
    assert await got("zebrafinch before:2024-01-01") == []
    assert (await got("zebrafinch after:2024-01-01"))[0] == ids["budget"]
    thread = await box.thread_of(ids["trip"])
    assert set(await got("plans", thread_key=thread)) <= {ids["trip"]}
    assert ids["budget"] not in await got("flights", thread_key=thread)


async def test_unread_category_and_priority_filters_come_from_raw_db(box: Box) -> None:
    ids = await corpus(box)
    await box.read(ids["lunch"])
    await box.enrich(ids["budget"], "triage", {"category": "work", "priority": 3})
    await box.enrich(ids["lunch"], "triage", {"category": "social", "priority": 1})
    await box.enrich(ids["trip"], "triage", {"category": "social", "priority": 2})

    async def got(text: str, **kw: Any) -> set[str]:
        res = await box.retrieval.search(text, HybridFilters(**kw))
        return {h.hit.message_id for h in res.hits}

    everyone = set(ids.values())
    assert await got("team", unread_only=True) == everyone - {ids["lunch"]}
    assert await got("is:unread") == everyone - {ids["lunch"]}
    assert await got("team", categories=("social",)) == {ids["lunch"], ids["trip"]}
    assert await got("category:work") == {ids["budget"]}
    assert await got("team", min_priority=2) == {ids["budget"], ids["trip"]}
    assert await got("priority:3 category:work is:unread") == {ids["budget"]}
    assert await got("team", categories=("work",), unread_only=True) == {ids["budget"]}


async def test_search_never_marks_anything_read(box: Box) -> None:
    await corpus(box)
    before = await box.read_flags()
    for text in ("zebrafinch", "is:unread", "from:alex", "budget review"):
        await box.retrieval.search(text)
        await box.retrieval.evidence(text)
    assert await box.read_flags() == before and not any(before.values())


async def test_a_query_with_no_words_lists_the_newest_that_pass(box: Box) -> None:
    ids = await corpus(box)
    found = await box.retrieval.search("from:alex")
    assert [h.hit.message_id for h in found.hits] == [ids["trip"], ids["budget"]]
    assert found.hits[0].why == ("filter",)


class Down:
    model_id = "fake-embed"
    dimensions = 8

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise ConnectionError("model server is down")


class Slow(Down):
    async def embed(self, texts: list[str]) -> list[list[float]]:
        await asyncio.sleep(5)
        return [[0.0] * 8 for _ in texts]


class Other(Down):
    model_id = "some-other-model"


@pytest.mark.parametrize(
    ("embedder", "reason"),
    [
        (None, "no embedder"),
        (Down(), "vector search failed"),
        (Slow(), "vector search failed"),
        (Other(), "another model"),
    ],
)
async def test_a_vector_outage_gives_keyword_results_and_degraded(
    box: Box, embedder: Any, reason: str
) -> None:
    ids = await corpus(box)
    box.retrieval.hybrid.embedder = lambda: embedder
    box.retrieval.hybrid.cfg = SearchSettings(vector_timeout_seconds=0.05)
    found = await box.retrieval.search("zebrafinch")
    assert found.degraded and reason in found.degraded_reason
    assert [h.hit.message_id for h in found.hits] == [ids["budget"]]
    assert found.hits[0].why == ("keyword",)


async def test_no_vector_store_or_no_vectors_degrades(box: Box) -> None:
    ids = await corpus(box)
    box.retrieval.hybrid.vectors = Vectors(lambda: None)  # no store at all
    found = await box.retrieval.search("zebrafinch")
    assert found.degraded and "vector store" in found.degraded_reason
    assert found.hits[0].hit.message_id == ids["budget"]


async def test_no_vectors_built_yet_degrades(box: Box) -> None:
    ids = await corpus(box)
    box.retrieval.hybrid.vectors = Vectors(lambda: InMemoryVectorStore())
    found = await box.retrieval.search("zebrafinch")
    assert found.degraded and "no vectors" in found.degraded_reason
    assert found.hits[0].hit.message_id == ids["budget"]


async def test_the_question_form_matches_any_word_but_needs_half_of_them(box: Box) -> None:
    ids = await corpus(box)
    res = await box.retrieval.hybrid.search(
        "when is the zebrafinch review?",
        None,
        5,
        keyword_text="zebrafinch review",
        keyword_any=True,
    )
    assert res.hits[0].hit.message_id == ids["budget"] and "keyword" in res.hits[0].why


async def test_search_survives_hostile_and_random_queries(box: Box) -> None:
    await corpus(box)
    rng = random.Random(7)
    alphabet = string.printable + "日本語​‮'\"()*:-^~"
    nasty = [
        "", " ", '"', '""', '"unterminated', "NEAR(", "a AND", "* OR *", "from:", "after:notadate",
        "before:2024-13-45", "is:", "category:", "priority:99", "priority:-1", "-", "--",
        "x" * 3000, "'; DROP TABLE docs; --", "from:%_\\", "\x00\x01", "{[(<>)]}", "col:ign",
    ]  # fmt: skip
    nasty += ["".join(rng.choice(alphabet) for _ in range(rng.randint(1, 60))) for _ in range(150)]
    for text in nasty:
        res = await box.retrieval.search(text)
        assert isinstance(res.degraded, bool)
        await box.retrieval.evidence(text)
    count = await box.rig.raw_db.fetchone("SELECT COUNT(*) AS n FROM messages")
    assert count is not None and count["n"] == 3  # nothing was written
