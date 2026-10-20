# SPDX-License-Identifier: Apache-2.0
"""Read state through the registry: reads never change it; each operation that can is declared."""

from typing import Any

import pytest

from oaken.core.errors import InvalidInput, NotFound, NotImplementedYet, PermissionDenied
from oaken.core.ids import MessageId
from oaken.core.operations.builtin import core_operations
from oaken.core.operations.tests.conftest import CLI, UI, Ops, agent
from oaken.core.untrusted import UntrustedText

SUMMARY = {"summary": "Alex writes about quokkas."}


def examples(a: MessageId) -> dict[str, Any]:
    """A valid input for every operation that does not change read state when called plainly."""
    return {
        "status": {}, "pipeline_status": {}, "usage_summary": {}, "list_accounts": {},
        "list_plugins": {}, "plugin_health": {}, "list_audit": {}, "list_operations": {},
        "list_labels": {}, "list_schedules": {"crontab": True}, "list_messages": {},
        "search_keyword": {"query": "quokkas"}, "get_message": {"message_id": a},
        "get_raw": {"message_id": a, "include_raw": True}, "get_message_rules": {"message_id": a},
        "message_dismissed": {"message_id": a}, "search": {"query": "x"}, "ask": {"question": "x"},
        "sync_now": {"account_id": "main"}, "reprocess": {"stage": "parse"}, "rebuild_index": {},
        "set_schedule": {"account_id": "main", "schedule": "every 20m"},
        "pause_schedule": {"account_id": "main"}, "resume_schedule": {"account_id": "main"},
        "remove_schedule": {"account_id": "main"},
        "set_operation_exposure": {"scope": "operation", "name": "status", "mcp": True},
        "summarize_message": {"message_ids": [a]},  # mark_read defaults to False
        "create_agent": {"label": "x", "kinds": ["enrich"]}, "list_agents": {},
        "revoke_agent": {"agent_id": "none"},
        "get_enrichment": {"message_id": a}, "enrich_message": {"message_id": a},
        "sieve_check": {"script": 'keep;'}, "sieve_list": {},
        "sieve_test": {"script": "keep;", "message_id": a},
        "get_account": {"account_id": "main"}, "check_account": {"account_id": "main"},
        "create_account": {"account_id": "extra", "provider": "fake", "display_name": "X"},
        "set_account_config": {"account_id": "main", "config": {}},
        "set_account_password": {"account_id": "main", "password": "x"},
        "start_authorization": {"account_id": "main"},
        "authorization_status": {"account_id": "main"},
    }  # fmt: skip


MARKING = {"mark_read", "mark_unread", "undo", "message_presented"}


async def test_no_operation_that_does_not_mark_read_changes_read_state(ops: Ops) -> None:
    (a, *_) = await ops.add(3)
    ops.rig.llm.responder = lambda _r: SUMMARY
    table = examples(a)
    declared = {o.name for o in core_operations() if o.marks_read}
    assert set(table) | MARKING == {o.name for o in core_operations()}, "add a case for the new one"
    assert declared == MARKING | {"summarize_message"}
    for name, payload in table.items():
        try:
            await ops.call(name, payload, CLI)
        except NotImplementedYet:
            assert name in {"search", "ask"}
        except NotFound:
            assert name == "revoke_agent"  # there is no such agent to revoke
        except InvalidInput:  # the fake provider has no password and no consent flow
            assert name in {"set_account_password", "start_authorization"}
        assert await ops.read_count() == 0, f"{name} changed read state"
        assert ops.rs.writes() == [], f"{name} wrote a flag to the server"
    assert ops.rs.writes() == [] and await ops.rs.marker(a) is None


async def test_reading_through_the_registry_as_an_agent_changes_nothing_either(ops: Ops) -> None:
    (a, b, c) = await ops.add(3)
    for name, payload in (
        ("list_messages", {"include_text": True}),
        ("search_keyword", {"query": "quokkas"}),
        ("get_message", {"message_id": b}),
        ("get_message_rules", {"message_id": c}),
        ("list_labels", {}),
    ):
        await ops.call(name, payload, agent())
    assert await ops.read_count() == 0 and await ops.svc.audit.recent() == []


async def test_a_summary_does_not_mark_read_unless_asked_and_asking_is_audited(ops: Ops) -> None:
    ids = await ops.add(3)
    ops.rig.llm.responder = lambda _r: SUMMARY
    out = await ops.out("summarize_message", {"message_ids": ids})
    assert [i.summary for i in out.items] == [SUMMARY["summary"]] * 3
    assert out.marked_read == [] and await ops.read_count() == 0
    # the person is asked at the end, and says yes: one call over the whole list
    done = await ops.out("mark_read", {"ids": ids, "after_summary": True})
    assert sorted(done.changed) == sorted(ids) and await ops.read_count() == 3
    [entry] = await ops.svc.audit.recent(5, op="mark_read")
    assert (entry.actor.kind, entry.reason, entry.op) == ("user", "user_summarized", "mark_read")
    assert len(entry.message_ids) == 3  # one batch: one row, one undo
    undone = await ops.out("undo", {"audit_id": entry.id})
    assert sorted(undone.changed) == sorted(ids) and await ops.read_count() == 0


async def test_summarize_with_mark_read_marks_only_what_got_a_summary(ops: Ops) -> None:
    a, b = await ops.add(2)
    ops.rig.llm.responder = lambda _r: SUMMARY
    out = await ops.out(
        "summarize_message", {"message_ids": [a, "not-a-message", b], "mark_read": True}
    )
    assert [i.error for i in out.items] == [None, "not_indexed", None]
    assert sorted(out.marked_read) == sorted([a, b])
    assert await ops.read_count() == 2


async def test_a_summary_is_cached_and_mail_reaches_the_model_only_as_untrusted_text(
    ops: Ops,
) -> None:
    (a,) = await ops.add(1)
    ops.rig.llm.responder = lambda _r: SUMMARY
    await ops.out("summarize_message", {"message_ids": [a]})
    asked = len(ops.rig.llm.requests)
    again = await ops.out("summarize_message", {"message_ids": [a]})
    assert again.items[0].summary and len(ops.rig.llm.requests) == asked  # the cache answered
    request = ops.rig.llm.requests[-1]
    assert isinstance(request.system, str) and "quokkas" not in request.system
    assert request.user and all(isinstance(p, UntrustedText | str) for p in request.user)
    assert any(isinstance(p, UntrustedText) and "quokkas" in p.unwrap() for p in request.user)
    usage = await ops.out("usage_summary")
    row = next(r for r in usage.rows if r.stage == "llm.summarize")
    assert row.calls == 1 and row.cached_calls == 1


async def test_a_secure_workspace_does_not_summarize_with_a_remote_model(ops: Ops) -> None:
    (a,) = await ops.add(1)
    ops.rig.llm.locality = "remote"
    out = await ops.out("summarize_message", {"message_ids": [a], "mark_read": True})
    assert out.items[0].summary is None and out.items[0].error is not None
    assert out.items[0].error.startswith("permission_denied")
    assert out.marked_read == [] and await ops.read_count() == 0
    assert ops.rig.llm.requests == []  # nothing about the mail went anywhere


async def test_an_agent_summarizing_and_marking_read_goes_through_a_proposal(ops: Ops) -> None:
    a, b = await ops.add(2)
    ops.rig.llm.responder = lambda _r: SUMMARY
    who = agent("claude")
    payload = {"message_ids": [a, b], "mark_read": True}
    p = (await ops.call("summarize_message", payload, who, confirm=False)).proposal
    assert p is not None
    assert "Summarize 2 message" in p.summary and p.total_changes == 2
    assert ops.rig.llm.requests == [] and await ops.read_count() == 0  # nothing yet
    done = await ops.call(
        "summarize_message", payload, who, confirm=False, token=p.confirmation_token
    )
    assert done.output is not None and await ops.read_count() == 2
    [entry] = [e for e in await ops.svc.audit.recent(10) if e.op == "mark_read"]
    assert (entry.actor.kind, entry.actor.label, entry.reason) == (
        "agent",
        "claude",
        "mcp_summarized",
    )
    # the registry recorded the call itself too, with a hash of the input and not the input
    call = next(e for e in await ops.svc.audit.recent(10) if e.op == "call:summarize_message")
    assert set(call.after) == {"request_id", "input_sha256"} and call.reason == "mcp_call"


async def test_a_summary_asked_for_without_marking_never_touches_read_state_over_mcp(
    ops: Ops,
) -> None:
    (a,) = await ops.add(1)
    ops.rig.llm.responder = lambda _r: SUMMARY
    p = (await ops.call("summarize_message", {"message_ids": [a]}, agent(), confirm=False)).proposal
    assert p is not None and p.total_changes == 0
    await ops.call(
        "summarize_message",
        {"message_ids": [a]},
        agent(),
        confirm=False,
        token=p.confirmation_token,
    )
    assert await ops.read_count() == 0


async def test_mark_unread_and_a_noop_mark_change_and_audit_nothing(ops: Ops) -> None:
    (a,) = await ops.add(1)
    first = await ops.out("mark_read", {"ids": [a]})
    again = await ops.out("mark_read", {"ids": [a]})
    assert first.changed == [a] and again.changed == [] and again.unchanged == [a]
    back = await ops.out("mark_unread", {"ids": [a]})
    assert back.changed == [a] and await ops.read_count() == 0
    assert len(await ops.svc.audit.recent()) == 2  # the no-op wrote no row


async def test_a_proposal_for_a_noop_says_nothing_would_change(ops: Ops) -> None:
    (a,) = await ops.add(1)
    await ops.out("mark_read", {"ids": [a]})
    p = (await ops.call("mark_read", {"ids": [a, "nope"]}, agent(), confirm=False)).proposal
    assert p is not None and p.total_changes == 0
    assert "1 already read" in p.summary and "1 not found" in p.summary


async def test_presenting_arms_the_timer_and_the_user_ui_marks_it_after_the_delay(ops: Ops) -> None:
    (a,) = await ops.add(1)
    armed = await ops.out("message_presented", {"message_id": a}, UI)
    assert armed.armed is True and await ops.read_count() == 0
    ops.rig.clock.advance(seconds=3)
    assert await ops.rs.rs.due() == 1 and await ops.read_count() == 1
    (b,) = await ops.add(1, start=1)
    await ops.out("message_presented", {"message_id": b}, UI)
    dismissed = await ops.out("message_dismissed", {"message_id": b}, UI)
    assert dismissed.cancelled is True
    ops.rig.clock.advance(seconds=3)
    assert await ops.rs.rs.due() == 0 and not await ops.rs.is_read(b)


async def test_an_agent_cannot_present_a_message(ops: Ops) -> None:
    from oaken.core.errors import NotExposed

    (a,) = await ops.add(1)
    with pytest.raises(NotExposed):
        await ops.call("message_presented", {"message_id": a}, agent())


async def test_an_internal_system_caller_is_still_refused_by_read_state(ops: Ops) -> None:
    from oaken.core.operations import CallerContext

    (a,) = await ops.add(1)
    with pytest.raises(PermissionDenied):
        await ops.call("mark_read", {"ids": [a]}, CallerContext.internal(), confirm=True)
    assert await ops.read_count() == 0


async def test_batches_are_capped_and_the_cap_is_a_refusal_not_a_cut(ops: Ops) -> None:
    too_many = [f"id-{i}" for i in range(201)]
    with pytest.raises(InvalidInput, match="at most 200"):
        await ops.call("mark_read", {"ids": too_many})
    with pytest.raises(InvalidInput, match="at most 200"):
        await ops.call("mark_read", {"ids": too_many}, agent(), confirm=False)  # also at proposal
    with pytest.raises(InvalidInput, match="at most 20"):
        await ops.call("summarize_message", {"message_ids": too_many[:21]})
    ok = await ops.out("mark_read", {"ids": too_many[:200]})  # 200 unknown ids: all missing
    assert len(ok.missing) == 200
