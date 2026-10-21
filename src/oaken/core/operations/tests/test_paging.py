# SPDX-License-Identifier: Apache-2.0
"""Paging and filtering for the listing operations: ``offset``, ``total``, stars, and the fields a
reader needs. These are what a client's "a–b of N" and its reply button stand on."""

from oaken.core.operations.tests.conftest import Ops


async def _star(ops: Ops, *ids: str) -> None:
    for i in ids:
        await ops.rig.raw_db.execute(
            "UPDATE message_state SET is_starred = 1 WHERE message_id = ?", (i,)
        )


async def test_list_messages_pages_and_counts_the_whole_match(ops: Ops) -> None:
    ids = await ops.add(7)
    first = await ops.out("list_messages", {"limit": 3})
    second = await ops.out("list_messages", {"limit": 3, "offset": 3})
    last = await ops.out("list_messages", {"limit": 3, "offset": 6})
    assert (first.total, second.total, last.total) == (7, 7, 7)
    assert first.total_exact is True and first.offset == 0 and second.offset == 3
    pages = [[i.message_id for i in p.items] for p in (first, second, last)]
    assert [len(p) for p in pages] == [3, 3, 1]
    assert sorted(m for p in pages for m in p) == sorted(ids)  # no overlap, nothing missed
    assert first.truncated and not last.truncated


async def test_total_is_the_match_not_the_page_and_follows_filters(ops: Ops) -> None:
    await ops.add(5)
    only = await ops.out("list_messages", {"from": "alex1", "limit": 1})
    assert only.total == 1 and len(only.items) == 1
    beyond = await ops.out("list_messages", {"offset": 50})
    assert beyond.items == [] and beyond.total == 5


async def test_search_keyword_pages_and_counts(ops: Ops) -> None:
    await ops.add(4)
    page = await ops.out("search_keyword", {"query": "quokkas", "limit": 2})
    rest = await ops.out("search_keyword", {"query": "quokkas", "limit": 2, "offset": 2})
    assert page.total == rest.total == 4
    ids = [i.message_id for i in (*page.items, *rest.items)]
    assert len(ids) == len(set(ids)) == 4


async def test_starred_and_unread_filters_and_the_flag_on_each_row(ops: Ops) -> None:
    a, b, c = await ops.add(3)
    await _star(ops, b, c)
    await ops.out("mark_read", {"ids": [c]})
    everything = await ops.out("list_messages")
    assert {i.message_id: i.starred for i in everything.items} == {a: False, b: True, c: True}
    starred = await ops.out("list_messages", {"starred": True})
    assert {i.message_id for i in starred.items} == {b, c} and starred.total == 2
    plain = await ops.out("list_messages", {"starred": False})
    assert [i.message_id for i in plain.items] == [a]
    unread = await ops.out("list_messages", {"unread_only": True})
    assert {i.message_id for i in unread.items} == {a, b}
    both = await ops.out("list_messages", {"unread_only": True, "starred": True})
    assert [i.message_id for i in both.items] == [b]
    # paging still holds when raw.db filters are in play
    first = await ops.out("list_messages", {"unread_only": True, "limit": 1})
    nxt = await ops.out("list_messages", {"unread_only": True, "limit": 1, "offset": 1})
    assert first.total == nxt.total == 2
    assert {first.items[0].message_id, nxt.items[0].message_id} == {a, b}


async def test_search_applies_is_starred_and_the_star_filter(ops: Ops) -> None:
    a, b = await ops.add(2)
    await _star(ops, b)
    by_operator = await ops.out("search", {"query": "quokkas is:starred"})
    assert [i.message_id for i in by_operator.items] == [b]
    by_field = await ops.out("search", {"query": "quokkas", "starred": False})
    assert [i.message_id for i in by_field.items] == [a]
    paged = await ops.out("search", {"query": "quokkas", "limit": 1, "offset": 1})
    assert len(paged.items) == 1 and paged.total == 2 and paged.offset == 1


async def test_query_operators_to_subject_has_attachment_and_location(ops: Ops) -> None:
    (ops.rig.sieve_dir / "10-test.sieve").write_text(
        'require ["fileinto"];\nif address :is "from" "alex1@example.com" { fileinto "Lists"; }\n'
    )
    ops.rig.engine.reload()
    a, b = await ops.add(2)
    by_subject = await ops.out(
        "search_keyword", {"query": "subject:Subject quokkas", "disposition": "any"}
    )
    assert by_subject.total == 2
    none = await ops.out("search_keyword", {"query": "subject:nonesuch", "disposition": "any"})
    assert none.items == [] and none.total == 0
    to = await ops.out("list_messages", {"disposition": "any"})
    assert to.total == 2
    in_folder = await ops.out("search_keyword", {"query": "location:Lists", "disposition": "any"})
    assert [i.message_id for i in in_folder.items] == [b]
    # the field and the operator agree
    field = await ops.out("list_messages", {"location": "Lists", "disposition": "any"})
    assert [i.message_id for i in field.items] == [b]
    attached = await ops.out("search_keyword", {"query": "has:attachment"})
    assert attached.items == [] and attached.total == 0  # the rig's messages carry none
    assert a != b


async def test_hostile_operator_values_never_raise_or_reach_fts_unquoted(ops: Ops) -> None:
    await ops.add(2)
    for q in (
        'to:"((( NEAR(" subject:" OR *',
        "subject:'; DROP TABLE docs; --",
        "location:% has:\x00attachment is:starred is:bogus to:",
        'subject:"unclosed',
    ):
        out = await ops.out("search_keyword", {"query": q})
        assert isinstance(out.items, list) and isinstance(out.warnings, list)
    assert (await ops.out("list_messages")).total == 2  # nothing was damaged


async def test_get_message_carries_the_reader_and_reply_fields(ops: Ops) -> None:
    (a,) = await ops.add(1)
    m = await ops.out("get_message", {"message_id": a})
    assert m.starred is False
    assert [x.address for x in m.to_addrs]  # recipients come from the raw message
    assert m.message_id_header  # the Message-ID header, for In-Reply-To
    assert isinstance(m.cc_addrs, list) and isinstance(m.references, list)
