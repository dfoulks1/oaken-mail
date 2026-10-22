# SPDX-License-Identifier: Apache-2.0
"""Token savings across the board: what a read kept out of the client's context, and the cache's
savings split by who asked."""

from oaken.ai.caller import CALLER
from oaken.core.operations.caller import CallerContext
from oaken.core.operations.savings import message_ids
from oaken.core.operations.tests.conftest import Ops

MCP = CallerContext.mcp("tester")


def long_mail(n: int) -> bytes:
    body = ("A long paragraph about the quarterly plan and its many details. " * 40).strip()
    return (
        f"From: Alex <alex{n}@example.com>\r\nTo: Sam <sam@example.org>\r\n"
        f"Subject: Plan {n}\r\nDate: Mon, 01 Jan 2024 0{n}:00:00 +0000\r\n"
        f"Message-ID: <m{n}@x>\r\n\r\n{body}\r\n"
    ).encode()


async def seed(ops: Ops, count: int = 3) -> None:
    for n in range(1, count + 1):
        ops.rig.provider.add_message(long_mail(n))
    await ops.rig.run_sync()
    await ops.rig.drain()


def test_message_ids_reads_ids_at_any_depth_and_ignores_header_references() -> None:
    reply = {
        "items": [{"message_id": "a", "references": ["<hdr@x>"]}, {"message_id": "b"}],
        "message_ids": ["c", "a"],
        "nested": {"citations": [{"message_id": "d"}]},
    }
    assert message_ids(reply) == {"a", "b", "c", "d"}


async def test_a_snippet_listing_keeps_most_of_the_text_out_of_the_clients_context(
    ops: Ops,
) -> None:
    await seed(ops)
    await ops.out("list_messages", {}, MCP)
    out = await ops.out("savings_summary", {})
    (row,) = [r for r in out.context if (r.caller, r.op) == ("mcp", "list_messages")]
    assert row.calls == 1 and row.messages == 3
    assert row.tokens_full > row.tokens_returned > 0
    assert row.tokens_saved == row.tokens_full - row.tokens_returned
    total = {t.caller: t for t in out.totals}["mcp"]
    assert total.context_tokens_saved == row.tokens_saved


async def test_each_door_is_counted_on_its_own(ops: Ops) -> None:
    await seed(ops)
    await ops.out("list_messages", {}, MCP)
    await ops.out("list_messages", {})  # the CLI
    out = await ops.out("savings_summary", {})
    assert {r.caller for r in out.context} == {"mcp", "cli"}


async def test_an_operation_that_only_changes_state_is_not_counted_as_a_saving(
    ops: Ops,
) -> None:
    await seed(ops)
    ids = [i.message_id for i in (await ops.out("list_messages", {})).items]
    await ops.out("mark_read", {"ids": ids})
    out = await ops.out("savings_summary", {})
    assert "mark_read" not in {r.op for r in out.context}


async def test_the_caches_savings_are_split_by_who_asked(ops: Ops) -> None:
    usage = ops.svc.need_usage()
    token = CALLER.set("mcp")
    try:
        await usage.record("llm.summary", "m", tokens_in=100, tokens_out=20, cached=True)
        await usage.record("llm.summary", "m", tokens_in=100, tokens_out=20)
    finally:
        CALLER.reset(token)
    await usage.record("llm.summary", "m", tokens_in=50, tokens_out=10, cached=True, caller="cli")
    out = await ops.out("savings_summary", {})
    saved = {(r.caller, r.stage): (r.cached_calls, r.tokens_saved) for r in out.cache}
    assert saved[("mcp", "llm.summary")] == (1, 120)
    assert saved[("cli", "llm.summary")] == (1, 60)
    assert {t.caller: t.cache_tokens_saved for t in out.totals}["mcp"] == 120
