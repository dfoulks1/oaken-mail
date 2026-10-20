# SPDX-License-Identifier: Apache-2.0
import pytest

from oaken.core.actors import SYSTEM
from oaken.core.errors import Conflict, NotFound, PermissionDenied
from oaken.readstate.tests.conftest import AGENT, RS


async def test_undo_restores_the_prior_state_and_queues_the_reverse_write(rsrig: RS) -> None:
    a, b = await rsrig.add(2)
    await rsrig.rs.mark_read([b], "user_marked_read")
    await rsrig.settle()
    result = await rsrig.rs.mark_read([a, b], "user_marked_read")  # only a changes
    await rsrig.settle()
    assert await rsrig.is_read(a) and rsrig.server_has_seen("fake-1")

    undone = await rsrig.rs.undo(result.audit_ids[0])
    assert undone.changed == (a,)
    assert not await rsrig.is_read(a) and await rsrig.is_read(b), "b was read before, so it stays"
    assert await rsrig.marker(a) is not None  # the reverse write is waiting
    await rsrig.settle()
    assert not rsrig.server_has_seen("fake-1") and rsrig.server_has_seen("fake-2")


async def test_an_undo_is_itself_audited_and_can_be_undone_only_once(rsrig: RS) -> None:
    (mid,) = await rsrig.add(1)
    first = await rsrig.rs.mark_read([mid], "user_marked_read")
    undone = await rsrig.rs.undo(first.audit_ids[0])
    entries = await rsrig.audit.recent()
    assert [(e.op, e.reason) for e in entries] == [
        ("undo", "user_undo"),
        ("mark_read", "user_marked_read"),
    ]
    original = await rsrig.audit.get(first.audit_ids[0])
    assert original is not None and original.undone_by == undone.audit_ids[0]
    with pytest.raises(Conflict):
        await rsrig.rs.undo(first.audit_ids[0])
    # Undoing the undo is allowed: it puts the change back.
    await rsrig.rs.undo(undone.audit_ids[0])
    assert await rsrig.is_read(mid)


async def test_undo_restores_exactly_even_if_the_message_changed_since(rsrig: RS) -> None:
    (mid,) = await rsrig.add(1)
    first = await rsrig.rs.mark_read([mid], "user_marked_read")
    await rsrig.rs.mark_unread([mid], "user_marked_unread")
    await rsrig.rs.undo(first.audit_ids[0])
    assert not await rsrig.is_read(mid)  # the state before the first change


async def test_a_bulk_change_is_cut_into_rows_that_share_a_batch_and_undo_together(
    make_rs: object,
) -> None:
    async with make_rs(chunk=2) as rs:  # type: ignore[operator]
        ids = await rs.add(5)
        result = await rs.rs.mark_read(ids, "user_marked_read")
        assert len(result.audit_ids) == 3 and result.batch_id
        rows = await rs.audit.batch(result.batch_id)
        assert [len(r.message_ids) for r in rows] == [2, 2, 1]
        await rs.settle()
        await rs.rs.undo(result.audit_ids[1])  # any row of the batch undoes the whole batch
        assert [await rs.is_read(m) for m in ids] == [False] * 5
        assert all(r.undone_by for r in await rs.audit.batch(result.batch_id))


async def test_the_default_chunk_is_500(rsrig: RS) -> None:
    ids = await rsrig.add(501)
    result = await rsrig.rs.mark_read(ids, "user_marked_read")
    assert len(result.audit_ids) == 2
    rows = await rsrig.audit.batch(result.batch_id or "")
    assert [len(r.message_ids) for r in rows] == [500, 1]


async def test_the_audit_row_names_the_actor_and_the_reason(rsrig: RS) -> None:
    (mid,) = await rsrig.add(1)
    await rsrig.rs.mark_read([mid], "mcp_summarized", actor=AGENT, confirm=True)
    [entry] = await rsrig.audit.recent()
    assert (entry.actor.kind, entry.actor.label, entry.reason) == (
        "agent",
        "claude",
        "mcp_summarized",
    )
    assert entry.before == {mid: False} and entry.after == {mid: True}


async def test_undo_errors(rsrig: RS) -> None:
    (mid,) = await rsrig.add(1)
    with pytest.raises(NotFound):
        await rsrig.rs.undo("nope")
    with pytest.raises(PermissionDenied):
        await rsrig.rs.undo("whatever", actor=SYSTEM)
    first = await rsrig.rs.mark_read([mid], "user_marked_read")
    with pytest.raises(PermissionDenied):  # an agent's undo needs confirm like any other change
        await rsrig.rs.undo(first.audit_ids[0], actor=AGENT)
    assert await rsrig.is_read(mid)
    await rsrig.rs.undo(first.audit_ids[0], actor=AGENT, confirm=True)
    assert not await rsrig.is_read(mid)
