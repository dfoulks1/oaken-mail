# SPDX-License-Identifier: Apache-2.0
"""What may mark a message read, and what never does."""

import re
from pathlib import Path

import pytest

from oaken.core.actors import SYSTEM, USER
from oaken.core.errors import NotFound, PermissionDenied
from oaken.index import SearchFilters
from oaken.readstate import ConfirmRequired
from oaken.readstate.tests.conftest import AGENT, RS


async def test_a_full_sync_ingest_embed_and_enrich_of_500_unread_messages_marks_nothing(
    rsrig: RS,
) -> None:
    ids = await rsrig.add(500)
    assert len(ids) == 500
    assert rsrig.rig.embedder.calls, "the embedder never ran: this test would prove nothing"
    assert rsrig.rig.enricher.seen, "the enricher never ran: this test would prove nothing"
    await rsrig.rig.index.keyword_search("quokkas", SearchFilters(), k=1000)
    assert rsrig.writes() == []
    assert not any(rsrig.server_has_seen(p) for p in rsrig.rig.provider.messages)
    row = await rsrig.rig.raw_db.fetchone(
        "SELECT COUNT(*) AS n FROM message_state WHERE is_read = 1 OR local_dirty_json IS NOT NULL"
    )
    assert row is not None and row["n"] == 0
    assert await rsrig.audit.recent() == []


def test_no_background_module_can_reach_read_state() -> None:
    """Only the read-state package (and the API layers built on it) may name it. Sync, ingest, the
    index, the Sieve engine and plugins do not, and none of them may call ``set_flags``."""
    root = Path(__file__).resolve().parents[2]
    forbidden = re.compile(r"readstate|ReadState|mark_read|mark_unread")
    offenders = []
    for package in ("ingest", "index", "mail", "ai", "plugins"):
        for path in (root / package).rglob("*.py"):
            if "tests" in path.parts or path.name in {"fakes.py", "contracts.py"}:
                continue
            text = path.read_text(encoding="utf-8")
            if forbidden.search(text):
                offenders.append(f"{package}/{path.name}")
    assert not offenders, offenders
    for path in (root / "ingest").rglob("*.py"):
        if "tests" not in path.parts:
            assert "set_flags" not in path.read_text(encoding="utf-8"), path.name


async def test_background_work_is_refused_even_with_confirm(rsrig: RS) -> None:
    (mid,) = await rsrig.add(1)
    with pytest.raises(PermissionDenied):
        await rsrig.rs.mark_read([mid], "ingest", actor=SYSTEM, confirm=True)
    with pytest.raises(PermissionDenied):
        await rsrig.rs.mark_read([mid], "user_opened", actor=SYSTEM)
    assert not await rsrig.is_read(mid)
    assert await rsrig.audit.recent() == []


async def test_the_confirm_gate(rsrig: RS) -> None:
    a, b, c = await rsrig.add(3)
    # A user's own reasons are pre-confirmed ...
    assert (await rsrig.rs.mark_read([a], "user_marked_read")).changed == (a,)
    # ... an agent claiming a user reason is not ...
    with pytest.raises(ConfirmRequired):
        await rsrig.rs.mark_read([b], "user_opened", actor=AGENT)
    # ... any other reason needs confirm ...
    with pytest.raises(ConfirmRequired):
        await rsrig.rs.mark_read([b], "triage")
    assert (await rsrig.rs.mark_read([b], "triage", confirm=True)).changed == (b,)
    # ... and the MCP summarize reason is pre-confirmed for an agent.
    assert (await rsrig.rs.mark_read([c], "mcp_summarized", actor=AGENT)).changed == (c,)
    assert all([await rsrig.is_read(m) for m in (a, b, c)])


async def test_a_summary_marks_read_but_a_search_returning_the_same_message_does_not(
    rsrig: RS,
) -> None:
    ids = await rsrig.add(3)
    hits = await rsrig.rig.index.keyword_search("topic1", SearchFilters(), k=10)
    assert hits.hits, "the search found nothing, so it proves nothing"
    assert not await rsrig.is_read(ids[1])
    await rsrig.rs.mark_read([ids[1]], "mcp_summarized", actor=AGENT)
    assert await rsrig.is_read(ids[1])
    assert not await rsrig.is_read(ids[0]) and not await rsrig.is_read(ids[2])


async def test_marking_a_message_that_is_already_read_changes_and_audits_nothing(rsrig: RS) -> None:
    (mid,) = await rsrig.add(1)
    first = await rsrig.rs.mark_read([mid], "user_marked_read")
    again = await rsrig.rs.mark_read([mid], "user_marked_read")
    assert first.changed == (mid,) and again.changed == () and again.unchanged == (mid,)
    assert again.audit_ids == ()
    assert len(await rsrig.audit.recent()) == 1
    missing = await rsrig.rs.mark_read(["no-such-id"], "user_marked_read")  # type: ignore[list-item]
    assert missing.missing == ("no-such-id",)


async def test_auto_mark_off_leaves_automatic_marks_alone_but_explicit_marks_work(
    make_rs: object,
) -> None:
    async with make_rs(auto_mark=False) as rs:  # type: ignore[operator]
        (mid,) = await rs.add(1)
        assert await rs.rs.message_presented(mid) is False
        rs.rig.clock.advance(seconds=60)
        assert await rs.rs.due() == 0
        for reason, actor in (("user_opened", USER), ("mcp_summarized", AGENT)):
            result = await rs.rs.mark_read([mid], reason, actor=actor)
            assert result.skipped == "auto_mark_off" and result.changed == ()
        assert not await rs.is_read(mid)
        assert (await rs.rs.mark_read([mid], "user_marked_read")).changed == (mid,)
        assert await rs.is_read(mid)
        assert (await rs.rs.mark_unread([mid], "user_marked_unread")).changed == (mid,)
        assert not await rs.is_read(mid)


async def test_presenting_an_unknown_message_is_an_error(rsrig: RS) -> None:
    with pytest.raises(NotFound):
        await rsrig.rs.message_presented("nope")  # type: ignore[arg-type]
