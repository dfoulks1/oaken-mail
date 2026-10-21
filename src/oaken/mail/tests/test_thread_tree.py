# SPDX-License-Identifier: Apache-2.0
from datetime import UTC, datetime, timedelta

from oaken.mail.thread_tree import ThreadMember, build_tree, parent_ids

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def m(i: str, msgid: str | None, parents: list[str], hours: int | None) -> ThreadMember:
    return ThreadMember(i, msgid, parents, None if hours is None else T0 + timedelta(hours=hours))


def shape(members: list[ThreadMember]) -> list[tuple[str, str | None, int]]:
    return [(p.id, p.parent_id, p.depth) for p in build_tree(members)]


def test_parent_ids_put_in_reply_to_first_then_references_newest_first() -> None:
    assert parent_ids(["c"], ["a", "b", "c"]) == ["c", "b", "a"]
    assert parent_ids([], ["a", "b"]) == ["b", "a"]
    assert parent_ids(["x"], []) == ["x"] and parent_ids([], []) == []


def test_a_branching_conversation_nests_and_reads_depth_first() -> None:
    got = shape(
        [
            m("root", "a", [], 0),
            m("r1", "b", ["a"], 1),
            m("r2", "c", ["a"], 2),
            m("r1a", "d", ["b", "a"], 3),
            m("r2a", "e", ["c", "a"], 4),
        ]
    )
    assert got == [
        ("root", None, 0),
        ("r1", "root", 1),
        ("r1a", "r1", 2),
        ("r2", "root", 1),
        ("r2a", "r2", 2),
    ]


def test_a_missing_parent_falls_back_to_the_nearest_ancestor_we_have() -> None:
    got = shape([m("root", "a", [], 0), m("late", "z", ["y", "a"], 5)])  # y never arrived
    assert got == [("root", None, 0), ("late", "root", 1)]


def test_a_message_naming_nothing_we_hold_is_a_root_in_date_order() -> None:
    got = shape([m("b", "b", ["nope"], 2), m("a", "a", [], 1), m("c", None, [], None)])
    assert got == [("a", None, 0), ("b", None, 0), ("c", None, 0)]


def test_arrival_order_does_not_matter() -> None:
    rows = [m("root", "a", [], 0), m("r1", "b", ["a"], 1), m("r1a", "c", ["b"], 2)]
    assert shape(rows) == shape(list(reversed(rows)))


def test_cycles_and_self_replies_and_duplicate_ids_never_loop_or_drop() -> None:
    got = shape([m("x", "x", ["y"], 0), m("y", "y", ["x"], 1)])
    assert {i for i, _, _ in got} == {"x", "y"} and got[0][1] is None
    assert shape([m("s", "s", ["s"], 0)]) == [("s", None, 0)]
    dup = shape([m("one", "d", [], 0), m("two", "d", [], 1), m("kid", "k", ["d"], 2)])
    assert sorted(i for i, _, _ in dup) == ["kid", "one", "two"]
    assert dict((i, p) for i, p, _ in dup)["kid"] == "one"


def test_a_long_chain_does_not_exhaust_the_stack() -> None:
    n = 3000
    rows = [m(f"m{i}", f"id{i}", [f"id{i - 1}"] if i else [], i) for i in range(n)]
    got = build_tree(rows)
    assert len(got) == n and got[-1].depth == n - 1
