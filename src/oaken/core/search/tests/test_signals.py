# SPDX-License-Identifier: Apache-2.0
from oaken.core.search import signals
from oaken.core.search.tests.conftest import Box


async def test_read_state_and_triage_filters_use_raw_db(box: Box) -> None:
    a = await box.add("one", "alpha report")
    b = await box.add("two", "beta report")
    c = await box.add("three", "gamma report")
    await box.read(b)
    await box.enrich(a, "triage", {"category": "work", "priority": 3})
    await box.enrich(b, "triage", {"category": "newsletter", "priority": 0})
    raw, ids = box.rig.raw, [a, b, c]
    assert await signals.eligible(raw, ids) == set(ids)
    assert await signals.eligible(raw, ids, unread=True) == {a, c}
    assert await signals.eligible(raw, ids, unread=False) == {b}
    assert await signals.eligible(raw, ids, categories=["work"]) == {a}
    assert await signals.eligible(raw, ids, min_priority=1) == {a}
    assert await signals.eligible(raw, ids, categories=["work", "newsletter"], unread=True) == {a}
    # no triage row: passes only when no triage filter is asked for
    assert c not in await signals.eligible(raw, ids, min_priority=0)


async def test_summaries_come_back_only_when_there_is_one(box: Box) -> None:
    a = await box.add("one", "alpha report")
    b = await box.add("two", "beta report")
    await box.enrich(a, "summary", {"summary": "  A short one.  "})
    await box.enrich(b, "summary", {"summary": ""})
    assert await signals.summaries(box.rig.raw, [a, b]) == {a: "A short one."}
