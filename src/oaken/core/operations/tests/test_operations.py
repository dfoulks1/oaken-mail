# SPDX-License-Identifier: Apache-2.0
"""The operations over what exists: what they return, and that each does the one thing it says."""

import pytest

from oaken.core.errors import ConfigError, NotFound, Unavailable
from oaken.core.operations import CallerContext
from oaken.core.operations.tests.conftest import CLI, Ops, agent


async def test_listing_and_searching_find_the_messages_with_their_read_state(ops: Ops) -> None:
    a, b, c = await ops.add(3)
    await ops.out("mark_read", {"ids": [b]})
    listed = await ops.out("list_messages")
    assert {i.message_id for i in listed.items} == {a, b, c}
    assert {i.message_id: i.is_read for i in listed.items} == {a: False, b: True, c: False}
    assert listed.content_is_untrusted is True
    hit = await ops.out("search_keyword", {"query": "topic1"})
    assert [i.message_id for i in hit.items] == [b] and "**topic1**" in hit.items[0].snippet
    none = await ops.out("search_keyword", {"query": "zebra"})
    assert none.items == []
    junk = await ops.out("search_keyword", {"query": '"((( NEAR('})
    assert junk.items == [] and isinstance(junk.warnings, list)  # never an error from user text


async def test_filters_narrow_by_sender_label_and_disposition(ops: Ops) -> None:
    (ops.rig.sieve_dir / "10-test.sieve").write_text(
        'require ["fileinto"];\nif address :is "from" "alex1@example.com" { fileinto "Lists"; }\n'
    )
    ops.rig.engine.reload()
    a, b = await ops.add(2)  # filed as they arrive
    by_sender = await ops.out("list_messages", {"from": "alex0"})
    assert [i.message_id for i in by_sender.items] == [a]
    by_label = await ops.out("list_messages", {"label": "Lists", "disposition": "any"})
    assert [i.message_id for i in by_label.items] == [b]
    labels = await ops.out("list_labels")
    assert labels.labels.get("Lists") == 1 and "inbox" in labels.dispositions
    rules = await ops.out("get_message_rules", {"message_id": b})
    assert any(
        x.kind == "label" and x.value == "Lists" and x.source == "sieve" for x in rules.attrs
    )
    assert rules.filter is not None and rules.filter.error_count == 0


async def test_get_message_gives_cleaned_text_and_metadata(ops: Ops) -> None:
    (a,) = await ops.add(1)
    m = await ops.out("get_message", {"message_id": a})
    assert (m.subject, m.from_addr, m.indexed) == ("Subject 0", "alex0@example.com", True)
    assert "quokkas" in m.text and m.is_read is False and m.disposition == "inbox"
    with pytest.raises(NotFound):
        await ops.call("get_message", {"message_id": "nope"})
    with pytest.raises(NotFound):
        await ops.call("get_message_rules", {"message_id": "nope"})


async def test_hybrid_search_finds_mail_says_why_and_never_marks_it_read(ops: Ops) -> None:
    a, b, _ = await ops.add(3)
    out = await ops.out("search", {"query": "topic1"})
    assert out.items[0].message_id == b and out.items[0].why == ["keyword", "vector"]
    assert not out.degraded
    ops.svc.need_retrieval().hybrid.embedder = lambda: None  # the model goes away
    down = await ops.out("search", {"query": "topic1"})
    assert [i.message_id for i in down.items] == [b] and down.degraded and down.degraded_reason
    flags = {i.message_id: i.is_read for i in (await ops.out("list_messages")).items}
    assert flags[b] is False and flags[a] is False


async def test_ask_without_a_model_is_unavailable_not_a_crash(ops: Ops) -> None:
    await ops.add(1)
    ops.svc.llm = None
    ops.svc.retrieval = None
    with pytest.raises(Unavailable):
        await ops.call("ask", {"question": "what is topic0?"})


async def test_status_and_pipeline_status_and_accounts(ops: Ops) -> None:
    await ops.add(4)
    s = await ops.out("status")
    assert (s.version, s.accounts, s.messages, s.indexed, s.index_stale) == ("test", 1, 4, 4, False)
    assert s.plugins_total is not None and s.plugins_healthy == s.plugins_total
    assert s.schedules_paused == 0 and s.queue_depth == 0
    p = await ops.out("pipeline_status")
    assert p.docs == 4 and set(p.stages) >= {"parse", "embed", "enrich"} and p.parked == []
    accounts = await ops.out("list_accounts")
    assert [(a.account_id, a.provider) for a in accounts.accounts] == [("main", "fake")]


async def test_usage_summary_reports_what_the_models_cost_and_the_cache_saved(ops: Ops) -> None:
    (a,) = await ops.add(1)
    ops.rig.llm.responder = lambda _r: {"summary": "s"}
    await ops.out("summarize_message", {"message_ids": [a]})
    await ops.out("summarize_message", {"message_ids": [a]})
    rows = {r.stage: r for r in (await ops.out("usage_summary")).rows}
    assert rows["llm.summarize"].calls == 1 and rows["llm.summarize"].cached_calls == 1
    old = await ops.out("usage_summary", {"since_hours": 0.001})
    assert old.rows == [] or all(r.calls >= 0 for r in old.rows)


async def test_sync_now_queues_one_sync_and_a_second_request_joins_it(ops: Ops) -> None:
    ops.rig.provider.add_message(b"Subject: x\r\n\r\nbody\r\n")
    first = await ops.out("sync_now", {"account_id": "main"})
    again = await ops.out("sync_now", {"account_id": "main"})
    assert first.created is True and again.created is False and first.job_id == again.job_id
    with pytest.raises(NotFound):
        await ops.call("sync_now", {"account_id": "ghost"})
    assert (
        "call:sync_now" in await ops.audit_ops()
    )  # the registry audits what has no audit of its own


async def test_an_agent_can_ask_for_a_sync_only_through_a_proposal(ops: Ops) -> None:
    p = (await ops.call("sync_now", {"account_id": "main"}, agent(), confirm=False)).proposal
    assert p is not None and "Sync main now" in p.summary
    queued = await ops.svc.raw.db.fetchone("SELECT COUNT(*) AS n FROM jobs WHERE kind = 'sync.run'")
    assert queued is not None and queued["n"] == 0


async def test_reprocess_and_rebuild_queue_jobs_and_repeat_requests_join(ops: Ops) -> None:
    await ops.add(2)
    r1 = await ops.out("reprocess", {"stage": "embed"})
    r2 = await ops.out("reprocess", {"stage": "embed"})
    assert r1.created and not r2.created and r1.job_id == r2.job_id
    b1 = await ops.out("rebuild_index", {})
    b2 = await ops.out("rebuild_index", {})
    assert b1.created and not b2.created
    assert {"call:reprocess", "call:rebuild_index"} <= set(await ops.audit_ops())
    await ops.rig.drain()


async def test_schedules_are_listed_edited_paused_resumed_and_removed(ops: Ops) -> None:
    await ops.svc.need_sync().ensure_schedules()
    listed = await ops.out("list_schedules", {"crontab": True})
    assert [s.account_id for s in listed.schedules] == ["main"]
    assert listed.schedules[0].schedule == "every 15m" and "oaken sync run main" in (
        listed.crontab or ""
    )
    out = await ops.out(
        "set_schedule", {"account_id": "main", "schedule": "every 20m", "quiet": "22:00-07:00"}
    )
    assert out.schedule.schedule == "every 20m" and out.schedule.quiet == "22:00-07:00"
    with pytest.raises(ConfigError, match="minimum"):
        await ops.call("set_schedule", {"account_id": "main", "schedule": "every 10s"})
    paused = await ops.out("pause_schedule", {"account_id": "main", "why": "travelling"})
    assert paused.schedule.paused and paused.schedule.paused_reason == "travelling"
    assert not (await ops.out("resume_schedule", {"account_id": "main"})).schedule.paused
    ops_audited = [e.op for e in await ops.svc.audit.recent(20)]
    assert {"schedule.pause", "schedule.resume"} <= set(ops_audited)
    # the service audited it: the registry added no second row for the same change
    assert not any(o in ops_audited for o in ("call:set_schedule", "call:pause_schedule"))
    assert (await ops.out("remove_schedule", {"account_id": "main"})).removed is True
    assert (await ops.out("remove_schedule", {"account_id": "main"})).removed is False


async def test_schedule_edits_are_not_available_to_an_agent(ops: Ops) -> None:
    from oaken.core.errors import NotExposed

    for name in ("set_schedule", "pause_schedule", "resume_schedule", "remove_schedule"):
        with pytest.raises(NotExposed):
            await ops.call(name, {"account_id": "main", "schedule": "every 1h"}, agent())
    assert (await ops.out("list_schedules", None, agent())).schedules is not None  # reading is fine


async def test_the_audit_log_can_be_listed_by_a_person(ops: Ops) -> None:
    (a,) = await ops.add(1)
    await ops.out("mark_read", {"ids": [a]})
    rows = (await ops.out("list_audit", {"limit": 5})).entries
    assert rows[0].op == "mark_read" and rows[0].undoable and rows[0].count == 1
    await ops.out("undo", {"audit_id": rows[0].audit_id})
    after = {e.audit_id: e for e in (await ops.out("list_audit", {"limit": 5})).entries}
    assert after[rows[0].audit_id].undone_by is not None and not after[rows[0].audit_id].undoable


async def test_undo_errors_are_reported_before_anything_is_proposed(ops: Ops) -> None:
    from oaken.core.errors import Conflict

    with pytest.raises(NotFound):
        await ops.call("undo", {"audit_id": "nope"}, agent(), confirm=False)
    (a,) = await ops.add(1)
    await ops.out("mark_read", {"ids": [a]})
    entry = (await ops.svc.audit.recent(1))[0]
    await ops.out("undo", {"audit_id": entry.id})
    with pytest.raises(Conflict, match="already undone"):
        await ops.call("undo", {"audit_id": entry.id}, agent(), confirm=False)


async def test_a_missing_service_is_unavailable_not_a_crash(ops: Ops) -> None:
    ops.svc.llm = None
    ops.svc.sync = None
    (a,) = await ops.add(1)
    with pytest.raises(Unavailable, match="language model"):
        await ops.call("summarize_message", {"message_ids": [a]})
    with pytest.raises(Unavailable, match="sync"):
        await ops.call("list_schedules")


async def test_the_operations_a_caller_can_see_are_the_same_through_every_door(ops: Ops) -> None:
    for caller in (CLI, CallerContext.ui(), CallerContext.internal()):
        assert {o.name for o in await ops.client.visible(caller)} == set(ops.registry.names())
