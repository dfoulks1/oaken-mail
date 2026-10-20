# SPDX-License-Identifier: Apache-2.0
"""The bulk-read guard, on a corpus big enough to matter: counts and characters are clamped, and
the answer says when it was cut."""

import base64
from typing import Any

from oaken.core.operations.tests.conftest import CLI, Ops, agent


async def test_clamps_hold_on_a_500_message_corpus(ops: Ops) -> None:
    await ops.add(500)
    # counts: a person gets the configured limit, an agent a tighter one, and both are told
    out = await ops.out("list_messages", {"limit": 1000})
    assert len(out.items) == 50 and out.truncated  # [operations] max_results
    assert all(len(i.snippet) <= 240 for i in out.items)
    assert len((await ops.out("list_messages", {"limit": 7})).items) == 7
    person = await ops.out("search_keyword", {"query": "quokkas", "limit": 1000}, CLI)
    bot = await ops.out("search_keyword", {"query": "quokkas", "limit": 1000}, agent())
    assert (len(person.items), len(bot.items)) == (50, 20) and person.truncated and bot.truncated
    # characters: text comes back within the budget and the answer says it was cut
    texts = await ops.out("list_messages", {"limit": 50, "include_text": True}, agent())
    assert sum(len(i.text or "") + len(i.snippet) for i in texts.items) <= 8_000  # mcp_max_chars
    assert texts.truncated  # more matched than an agent may have
    both = await ops.out("list_messages", {"limit": 50, "include_text": True}, CLI)
    assert sum(len(i.text or "") for i in both.items) <= 20_000
    # token thrift: by default a listing is snippets, not bodies
    plain = await ops.out("list_messages", {"limit": 5}, agent())
    assert all(i.text is None and i.snippet for i in plain.items)


async def test_one_message_text_is_clamped_to_the_callers_limit(ops: Ops) -> None:
    body = "word " * 5000  # 25,000 characters
    ops.rig.provider.add_message(
        b"From: A <a@example.com>\r\nTo: B <b@example.org>\r\nSubject: long\r\n"
        b"Message-ID: <long@example.com>\r\nDate: Mon, 01 Jan 2024 10:00:00 +0000\r\n\r\n"
        + body.encode()
        + b"\r\n"
    )
    await ops.rig.run_sync()
    await ops.rig.drain()
    mid = await ops.rig.mid("fake-1")
    person = await ops.out("get_message", {"message_id": mid}, CLI)
    bot = await ops.out("get_message", {"message_id": mid}, agent())
    assert len(person.text) == 20_000 and person.truncated
    assert len(bot.text) == 8_000 and bot.truncated
    listed = await ops.out("list_messages", {"include_text": True}, agent())
    assert len(listed.items[0].text or "") <= 8_000 and listed.truncated  # the budget ran out
    short = await ops.out("get_message", {"message_id": mid, "text_chars": 100}, CLI)
    assert len(short.text) == 100 and short.truncated
    none = await ops.out("get_message", {"message_id": mid, "include_text": False}, CLI)
    assert none.text == "" and not none.truncated


async def test_raw_access_is_clamped_and_a_person_only(ops: Ops) -> None:
    (mid,) = await ops.add(1)
    out = await ops.out("get_raw", {"message_id": mid, "include_raw": True})
    assert "Subject: Subject 0" in out.headers
    assert base64.b64decode(out.raw_base64).startswith(b"From: Alex <alex0")
    assert not out.truncated and out.size_raw > 0
    bare = await ops.out("get_raw", {"message_id": mid})
    assert bare.raw_base64 is None


async def test_the_limits_come_from_config(make_ops: Any) -> None:
    from oaken.core.settings import OperationsSettings

    async with make_ops(operations=OperationsSettings(max_results=5, snippet_chars=20)) as o:
        await o.add(10)
        out = await o.out("list_messages", {"limit": 100})
        assert len(out.items) == 5 and all(len(i.snippet) <= 20 for i in out.items)
