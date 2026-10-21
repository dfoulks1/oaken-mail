# SPDX-License-Identifier: Apache-2.0
"""Prompt 25: mailbox_counts (the sidebar's numbers) and set_flagged (star, written back)."""

from oaken.core.operations.builtin import core_operations
from oaken.core.operations.tests.conftest import Ops, agent


async def test_mailbox_counts_matches_list_messages_and_tracks_read_state(ops: Ops) -> None:
    ids = await ops.add(3)
    counts = await ops.out("mailbox_counts", {})
    # Every message the fake corpus makes lands in the inbox, unread, to start.
    assert counts.inbox.total == 3 and counts.inbox.unread == 3
    listed = await ops.out("list_messages", {"disposition": "inbox"})
    assert counts.inbox.total == (listed.total or len(listed.items))
    assert counts.starred.total == 0 and counts.discarded.total == 0

    # Marking one read drops the inbox unread count a client sees.
    await ops.out("mark_read", {"ids": [ids[0]]})
    after = await ops.out("mailbox_counts", {})
    assert after.inbox.total == 3 and after.inbox.unread == 2


async def test_set_flagged_stars_writes_back_shows_in_counts_and_undoes(ops: Ops) -> None:
    (a, b, _) = await ops.add(3)
    out = await ops.out("set_flagged", {"ids": [a], "flagged": True})
    assert out.changed == [a] and not out.local_only
    assert await ops.rs.is_starred(a)
    # written back through the provider's flags.write as \Flagged
    await ops.rs.settle()
    assert ops.rs.server_has_flag("fake-1", "\\Flagged")

    counts = await ops.out("mailbox_counts", {})
    assert counts.starred.total == 1 and counts.starred.unread == 1
    starred = await ops.out("list_messages", {"starred": True})
    assert [i.message_id for i in starred.items] == [a]

    # undo restores it
    [entry] = [e for e in await ops.svc.audit.recent(10) if e.op == "set_flagged"]
    undone = await ops.out("undo", {"audit_id": entry.id})
    assert undone.changed == [a] and not await ops.rs.is_starred(a)
    assert (await ops.out("mailbox_counts", {})).starred.total == 0
    assert not await ops.rs.is_starred(b)


async def test_unstar_removes_the_flag(ops: Ops) -> None:
    (a,) = await ops.add(1)
    await ops.out("set_flagged", {"ids": [a], "flagged": True})
    await ops.rs.settle()
    await ops.out("set_flagged", {"ids": [a], "flagged": False})
    await ops.rs.settle()
    assert not await ops.rs.is_starred(a)
    assert not ops.rs.server_has_flag("fake-1", "\\Flagged")


async def test_mailbox_counts_is_offered_but_set_flagged_is_off_for_agents(ops: Ops) -> None:
    visible = {o.name for o in await ops.client.visible(agent())}
    assert "mailbox_counts" in visible
    assert "set_flagged" not in visible  # mcp="off": the user turns it on


async def test_the_registry_declares_the_two_new_ops_correctly() -> None:
    ops_by_name = {o.name: o for o in core_operations()}
    assert ops_by_name["mailbox_counts"].permission == "read"
    assert ops_by_name["mailbox_counts"].mcp == "on"
    sf = ops_by_name["set_flagged"]
    assert sf.permission == "mutate" and sf.mcp == "off" and not sf.marks_read
    assert sf.preview is not None  # an agent-reachable mutation must preview
