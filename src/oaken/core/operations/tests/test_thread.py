# SPDX-License-Identifier: Apache-2.0
"""``get_thread``: a conversation as a tree, and how the headers it needs get stored."""

import pytest

from oaken.core.errors import InvalidInput, NotFound
from oaken.core.ids import MessageId
from oaken.core.operations.tests.conftest import Ops


def eml(msg_id: str, subject: str, hour: int, *, irt: str = "", refs: str = "") -> bytes:
    head = (
        f"From: Alex <alex{hour}@example.com>\r\nTo: Sam <sam@example.org>\r\n"
        f"Subject: {subject}\r\nDate: Mon, 01 Jan 2024 {hour:02d}:00:00 +0000\r\n"
        f"Message-ID: <{msg_id}>\r\n"
    )
    if irt:
        head += f"In-Reply-To: <{irt}>\r\n"
    if refs:
        head += "References: " + " ".join(f"<{r}>" for r in refs.split()) + "\r\n"
    return (head + f"\r\nBody of {msg_id}.\r\n").encode()


async def put(ops: Ops, *raws: bytes) -> list[MessageId]:
    n = len(ops.rig.provider.messages)
    for raw in raws:
        ops.rig.provider.add_message(raw)
    await ops.rig.run_sync()
    await ops.rig.drain()
    return [await ops.rig.mid(f"fake-{n + i + 1}") for i in range(len(raws))]


async def test_replies_nest_under_the_message_they_answer(ops: Ops) -> None:
    a, b, c, d = await put(
        ops,
        eml("a@x", "Plan", 1),
        eml("b@x", "Re: Plan", 2, irt="a@x", refs="a@x"),
        eml("c@x", "Re: Plan", 3, irt="a@x", refs="a@x"),
        eml("d@x", "Re: Plan", 4, irt="b@x", refs="a@x b@x"),
    )
    out = await ops.out("get_thread", {"message_id": d})  # any member finds the whole thread
    shape = [(i.message_id, i.parent_id, i.depth) for i in out.items]
    assert shape == [(a, None, 0), (b, a, 1), (d, b, 2), (c, a, 1)]
    assert out.total == 4 and not out.truncated
    by_key = await ops.out("get_thread", {"thread_key": out.thread_key})
    assert [i.message_id for i in by_key.items] == [i.message_id for i in out.items]


async def test_a_reply_to_a_message_we_never_got_hangs_under_the_nearest_ancestor(
    ops: Ops,
) -> None:
    a, c = await put(
        ops,
        eml("a@x", "Plan", 1),
        eml("c@x", "Re: Plan", 3, irt="b@x", refs="a@x b@x"),  # b@x never arrived
    )
    out = await ops.out("get_thread", {"message_id": c})
    assert [(i.message_id, i.parent_id, i.depth) for i in out.items] == [(a, None, 0), (c, a, 1)]


async def test_a_single_message_is_a_thread_of_one_and_unknowns_are_errors(ops: Ops) -> None:
    (a,) = await put(ops, eml("solo@x", "Alone", 1))
    out = await ops.out("get_thread", {"message_id": a})
    assert [(i.message_id, i.parent_id, i.depth) for i in out.items] == [(a, None, 0)]
    with pytest.raises(NotFound):
        await ops.out("get_thread", {"message_id": "no-such"})
    with pytest.raises(InvalidInput):
        await ops.out("get_thread", {})


async def test_rows_indexed_before_threading_was_stored_are_filled_in_on_demand(ops: Ops) -> None:
    a, b = await put(ops, eml("a@x", "Plan", 1), eml("b@x", "Re: Plan", 2, irt="a@x", refs="a@x"))
    await ops.rig.index.db.execute("UPDATE docs SET msgid = NULL, refs_json = NULL")
    out = await ops.out("get_thread", {"message_id": b})
    assert [(i.message_id, i.parent_id) for i in out.items] == [(a, None), (b, a)]
    row = await ops.rig.index.db.fetchone(
        "SELECT msgid, refs_json FROM docs WHERE message_id = ?", (b,)
    )
    assert row["msgid"] == "b@x" and row["refs_json"] == '["a@x"]'  # kept for next time


async def test_a_reply_with_only_in_reply_to_joins_its_parents_thread(ops: Ops) -> None:
    a, b, c = await put(
        ops,
        eml("a@x", "Plan", 1),
        eml("b@x", "Re: Plan", 2, irt="a@x"),  # no References at all
        eml("c@x", "Re: Plan", 3, irt="b@x"),  # names b, not the root
    )
    out = await ops.out("get_thread", {"message_id": c})
    assert [(i.message_id, i.parent_id, i.depth) for i in out.items] == [
        (a, None, 0),
        (b, a, 1),
        (c, b, 2),
    ]


async def test_the_thread_is_cut_at_the_limit_and_says_so(ops: Ops) -> None:
    ids = await put(
        ops,
        eml("a@x", "Plan", 1),
        *(eml(f"r{n}@x", "Re: Plan", n + 2, irt="a@x", refs="a@x") for n in range(5)),
    )
    out = await ops.out("get_thread", {"message_id": ids[0], "limit": 3})
    assert len(out.items) == 3 and out.truncated and out.total == 6
