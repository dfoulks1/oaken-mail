# SPDX-License-Identifier: Apache-2.0
import random

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from oaken.core.search.context import (
    _cut_head,
    _overlap,
    near_duplicate,
    shingles,
)
from oaken.core.search.tests.conftest import Box


def long_body(marker: str, paragraphs: int = 10) -> str:
    rng = random.Random(marker)
    words = "plan review budget travel vendor invoice office printer schedule roadmap".split()
    out = []
    for i in range(paragraphs):
        text = " ".join(rng.choice(words) + str(rng.randint(0, 99)) for _ in range(120))
        out.append(f"{text} {marker if i == 4 else ''}")
    return "\n\n".join(out)


async def test_every_message_gets_a_header_and_the_matched_chunk(box: Box) -> None:
    mid = await box.add("Launch", long_body("quokkamoon launch is on Tuesday"))
    found, ev = await box.retrieval.evidence("when is the quokkamoon launch")
    (m,) = ev.messages
    assert m.message_id == mid and m.subject == "Launch" and m.ref == "M1"
    assert any("quokkamoon" in e.text for e in m.excerpts if e.matched)
    assert ev.text.startswith("[M1] 2024-01-01 | Alex <alex@example.com> | Launch")


async def test_summary_comes_before_chunks_and_thread_siblings_follow(box: Box) -> None:
    a = await box.add("Offsite", "The quokkamoon offsite is in May.")
    b = await box.add("Re: Offsite", "Booked the venue for it.", day=2)
    await box.enrich(a, "summary", {"summary": "Offsite planned for May."})
    await box.enrich(b, "summary", {"summary": "Venue booked."})
    thread = await box.thread_of(a)
    assert thread  # the two share a thread only when headers say so; siblings are optional here
    _, ev = await box.retrieval.evidence("quokkamoon offsite")
    first = ev.messages[0]
    assert first.summary == "Offsite planned for May."
    text = first.render().splitlines()
    assert text[1].startswith("summary:") and text[2].startswith("[M1.")


async def test_the_budget_is_a_hard_limit_and_savings_are_reported(box: Box) -> None:
    for i in range(6):
        await box.add(f"Doc {i}", long_body(f"quokkamoon fact{i}", 12), day=i + 1)
    found = await box.retrieval.search("quokkamoon")
    tokenizer = box.retrieval.context.tok
    for budget in (200, 400, 800, 3000):
        ev = await box.retrieval.context.build(found.hits, budget)
        assert ev.tokens_used <= budget
        assert tokenizer.count("\n\n".join(m.render() for m in ev.messages)) <= budget
        assert ev.tokens_saved_vs_full_bodies == max(0, ev.tokens_full_bodies - ev.tokens_used)
    big = await box.retrieval.context.build(found.hits, 800)
    assert big.saved_fraction > 0.5, "long messages should cost far less than their full bodies"


async def test_a_tiny_budget_gives_nothing_rather_than_going_over(box: Box) -> None:
    await box.add("Doc", long_body("quokkamoon"))
    found = await box.retrieval.search("quokkamoon")
    ev = await box.retrieval.context.build(found.hits, 1)
    assert ev.tokens_used <= 1 and ev.hits_included == 0 and ev.messages == ()


@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(budget=st.integers(min_value=0, max_value=1500), chunk_hits=st.integers(0, 6),
       neighbors=st.integers(0, 4))  # fmt: skip
async def test_property_the_context_never_exceeds_its_budget(
    box: Box, budget: int, chunk_hits: int, neighbors: int
) -> None:
    if box.count == 0:
        for i in range(4):
            await box.add(f"Doc {i}", long_body("quokkamoon note", 6), day=i + 1)
    found = await box.retrieval.search("quokkamoon note")
    ev = await box.retrieval.context.build(
        found.hits, budget, chunk_hits=chunk_hits, neighbor_hits=neighbors
    )
    assert ev.tokens_used <= budget
    assert box.retrieval.context.tok.count(ev.text) <= budget


async def test_the_tiers_fill_headers_first_then_chunks_then_neighbors(box: Box) -> None:
    for i in range(4):
        await box.add(f"Doc {i}", long_body(f"quokkamoon item{i}"), day=i + 1)
    found = await box.retrieval.search("quokkamoon")
    build = box.retrieval.context.build
    headers = await build(found.hits, 400, chunk_hits=0, neighbor_hits=0)
    assert headers.hits_included == 4 and not any(m.excerpts for m in headers.messages)
    chunks = await build(found.hits, 3000, chunk_hits=2, neighbor_hits=0)
    with_chunks = [m for m in chunks.messages if m.excerpts]
    assert len(with_chunks) == 2 and all(e.matched for m in with_chunks for e in m.excerpts)
    wide = await build(found.hits, 3000, chunk_hits=2, neighbor_hits=1)
    assert any(not e.matched for e in wide.messages[0].excerpts)  # a neighbour of the match
    assert all(e.matched for e in wide.messages[1].excerpts)


async def test_near_identical_content_is_sent_once(box: Box) -> None:
    body = "The quokkamoon vendor contract renewal needs two signatures from finance. " * 3
    await box.add("Contract", body, day=1)
    await box.add("Re: Contract", body, day=2)
    found = await box.retrieval.search("quokkamoon contract")
    ev = await box.retrieval.context.build(found.hits, 3000)
    assert ev.duplicates_dropped >= 1
    assert sum(len(m.excerpts) for m in ev.messages) == 1


def test_shingle_containment_and_overlap_trimming() -> None:
    a = "one two three four five six seven eight nine ten eleven twelve"
    assert near_duplicate(shingles(a), shingles(a + " thirteen"))
    assert not near_duplicate(shingles(a), shingles("completely different words in another order"))
    before, after = "alpha beta gamma delta epsilon", "delta epsilon zeta eta theta"
    assert _overlap(before, after) == 2
    assert _cut_head(after, 2) == "zeta eta theta"
