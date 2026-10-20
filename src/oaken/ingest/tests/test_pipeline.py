# SPDX-License-Identifier: Apache-2.0
from collections.abc import Callable
from typing import Any

from oaken.core.errors import PluginError
from oaken.core.events import MessageEmbedded, MessageIndexed
from oaken.core.settings import IngestSettings
from oaken.index import SearchFilters
from oaken.ingest.tests.conftest import ACCT, Options, Rig, mail


def add(rig: Rig, count: int, start: int = 0) -> list[str]:
    return [rig.provider.add_message(mail(i)) for i in range(start, start + count)]


async def test_500_messages_are_keyword_searchable_before_any_embedding(rig: Rig) -> None:
    add(rig, 500)
    seen_calls: list[int] = []

    async def note(_e: MessageIndexed) -> None:
        seen_calls.append(len(rig.embedder.calls))

    rig.bus.subscribe(MessageIndexed, note)  # type: ignore[arg-type]
    result = await rig.run_sync()
    assert result.stored == 500 and not result.errors
    await rig.drain()
    assert len(seen_calls) == 500
    assert set(seen_calls) == {0}, "an embedding job ran before every message was indexed"
    hits = await rig.index.keyword_search("quokkas", SearchFilters(), k=1000)
    assert len(hits.hits) == 500
    assert (await rig.store.stats()).count >= 500


async def test_the_same_sync_again_makes_no_embedder_calls_and_no_new_jobs(rig: Rig) -> None:
    add(rig, 12)
    await rig.run_sync()
    await rig.drain()
    calls = len(rig.embedder.calls)
    assert calls > 0
    await rig.raw_db.execute("DELETE FROM sync_cursors")  # pull it all again
    again = await rig.run_sync()
    assert again.stored == 0 and again.unchanged == 12
    assert await rig.drain() == 0
    assert len(rig.embedder.calls) == calls


async def test_reprocess_embed_when_nothing_changed_is_free_and_recorded_as_cached(
    rig: Rig,
) -> None:
    add(rig, 5)
    await rig.run_sync()
    await rig.drain()
    calls = len(rig.embedder.calls)
    first = {r.stage: r for r in await rig.usage.usage_summary()}["embed"]
    assert first.calls > 0 and first.cached_calls == 0 and first.tokens_in > 0
    await rig.pipeline.reprocess("embed")
    await rig.drain()
    assert len(rig.embedder.calls) == calls
    second = {r.stage: r for r in await rig.usage.usage_summary()}["embed"]
    assert second.calls == first.calls  # no new calls
    assert second.cached_calls > 0 and second.tokens_saved > 0  # and what they would have cost


async def test_usage_summary_since(rig: Rig) -> None:
    add(rig, 2)
    await rig.run_sync()
    await rig.drain()
    later = rig.clock.now()
    assert await rig.usage.usage_summary(later) == []
    assert await rig.usage.usage_summary()


async def test_an_embedder_outage_never_blocks_search_and_recovers(rig: Rig) -> None:
    add(rig, 6)
    real = rig.embedder.embed
    failures = {"left": 3}

    async def flaky(texts: Any) -> Any:
        if failures["left"] > 0:
            failures["left"] -= 1
            raise PluginError("embedder is down")
        return await real(texts)

    rig.embedder.embed = flaky  # type: ignore[method-assign]
    await rig.run_sync()
    await rig.runner.drain()
    hits = await rig.index.keyword_search("quokkas", SearchFilters(), k=50)
    assert len(hits.hits) == 6  # during the outage
    assert (await rig.store.stats()).count < 6 or failures["left"] == 0
    await rig.drain(rounds=6)
    assert {r.message_id for r in rig.names(MessageEmbedded)} == {h.message_id for h in hits.hits}
    status = await rig.pipeline.pipeline_status()
    assert status.stages["embed"].failed == 0 and status.stages["embed"].pending == 0


async def test_a_poison_message_is_parked_and_does_not_stall_the_queue(
    make_rig: Callable[..., Any],
) -> None:
    async with make_rig(Options(ingest=IngestSettings(stage_max_attempts=2))) as rig:
        real = rig.embedder.embed

        async def picky(texts: Any) -> Any:
            if any("poison" in t for t in texts):
                raise PluginError("cannot embed this")
            return await real(texts)

        rig.embedder.embed = picky
        rig.provider.add_message(mail(1, "ordinary words"))
        rig.provider.add_message(mail(2, "a poison pill"))
        rig.provider.add_message(mail(3, "more ordinary words"))
        await rig.run_sync()
        await rig.drain(rounds=4)
        status = await rig.pipeline.pipeline_status()
        assert status.stages["embed"].failed == 1
        assert [p.stage for p in status.parked] == ["embed"]
        assert status.parked[0].message_id and "cannot embed" in status.parked[0].error
        assert len(rig.names(MessageEmbedded)) == 2  # the others went through
        assert (await rig.index.keyword_search("poison", SearchFilters())).hits  # still searchable


async def test_changing_the_embedder_model_marks_vectors_stale_and_reprocess_swaps_them(
    rig: Rig,
) -> None:
    add(rig, 4)
    await rig.run_sync()
    await rig.drain()
    old = rig.embedder.model_id
    assert (await rig.store.stats()).model_id == old
    rig.embedder.model_id = "fake-embed-2"
    status = await rig.pipeline.pipeline_status()
    assert status.embeddings_stale and status.vector_model == old

    snapshots: list[set[str]] = []
    activate = rig.store.activate_model

    async def watched(model_id: str) -> None:
        snapshots.append({k[0] for k in rig.store._rows})  # noqa: SLF001
        await activate(model_id)

    rig.rig_store_activate = watched  # type: ignore[attr-defined]
    rig.store.activate_model = watched  # type: ignore[method-assign]

    await rig.pipeline.reprocess("embed")
    await rig.drain()
    assert snapshots == [{old, "fake-embed-2"}], (
        "the old vectors were removed before the new existed"
    )
    assert (await rig.store.stats()).model_id == "fake-embed-2"
    assert {k[0] for k in rig.store._rows} == {"fake-embed-2"}  # noqa: SLF001
    assert not (await rig.pipeline.pipeline_status()).embeddings_stale


async def test_new_mail_waits_for_reprocess_when_the_model_changed(rig: Rig) -> None:
    add(rig, 2)
    await rig.run_sync()
    await rig.drain()
    rig.embedder.model_id = "fake-embed-2"
    add(rig, 1, start=10)
    await rig.run_sync()
    await rig.drain()
    assert {k[0] for k in rig.store._rows} == {"fake-embed"}  # noqa: SLF001
    assert (await rig.index.stats()).docs == 3  # but keyword search has it


async def test_backfill_pauses_at_the_queue_cap_and_resumes_but_new_mail_never_waits(
    make_rig: Callable[..., Any],
) -> None:
    opts = Options(ingest=IngestSettings(backfill_queue_cap=5), provider_batch=4)
    async with make_rig(opts) as rig:
        add(rig, 20)
        first = await rig.run_sync()
        assert first.paused and 0 < first.stored < 20
        cursor = first.cursor
        for _ in range(20):
            await rig.drain()
            more = await rig.run_sync()
            if not more.paused:
                break
        await rig.drain()
        assert (await rig.index.stats()).docs == 20
        assert cursor is not None
        # an account that has synced before: new mail is never held, whatever the depth
        add(rig, 12, start=100)
        fresh = await rig.run_sync()
        assert not fresh.paused and fresh.stored == 12
        assert (await rig.pipeline.pipeline_status()).backfill_paused


async def test_new_mail_runs_before_backfill_and_parse_before_embed(rig: Rig) -> None:
    add(rig, 3)
    await rig.run_sync()  # the first pull is a backfill
    add(rig, 2, start=50)
    await rig.run_sync()  # then new mail
    jobs = await rig.raw_db.fetchall("SELECT kind, priority FROM jobs WHERE status = 'queued'")
    prio = {(r["kind"], r["priority"]) for r in jobs}
    assert ("ingest.parse", 10) in prio and ("ingest.parse", 50) in prio
    order = await rig.raw_db.fetchone(
        "SELECT priority FROM jobs WHERE status = 'queued' "
        "ORDER BY priority DESC, run_after LIMIT 1"
    )
    assert order is not None and order["priority"] == 50


async def test_pipeline_status_counts_stages_and_estimates_time(rig: Rig) -> None:
    add(rig, 8)
    await rig.run_sync()
    before = await rig.pipeline.pipeline_status()
    assert before.stages["parse"].pending == 8 and before.stages["embed"].pending == 0
    assert before.stages["parse"].oldest_pending_seconds is not None
    rig.clock.advance(seconds=30)
    await rig.runner.drain()
    rig.clock.advance(seconds=30)
    after = await rig.pipeline.pipeline_status()
    assert after.stages["parse"].pending == 0 and after.stages["parse"].per_second > 0
    assert after.docs == 8 and after.chunks >= 8 and after.vectors >= 8


async def test_deletions_remove_a_message_from_the_index_and_the_vectors(rig: Rig) -> None:
    pids = add(rig, 3)
    await rig.run_sync()
    await rig.drain()
    assert (await rig.store.stats()).count >= 3
    rig.provider.remove_message(pids[0])
    result = await rig.run_sync()
    assert result.deleted == 1
    await rig.drain()
    assert (await rig.index.stats()).docs == 2
    assert (
        {k[1] for k in rig.store._rows}
        == {  # noqa: SLF001
            (await rig.raw.message_id_for(ACCT, p)) for p in pids[1:]
        }
    )


async def test_a_changed_message_is_reindexed_and_its_old_vectors_dropped(rig: Rig) -> None:
    pid = rig.provider.add_message(mail(1, "first version of the text"))
    await rig.run_sync()
    await rig.drain()
    mid = await rig.raw.message_id_for(ACCT, pid)
    assert mid
    await rig.raw.put(ACCT, pid, mail(1, "a completely different second version"))
    await rig.pipeline.enqueue_parse(mid, ACCT, changed=True)
    await rig.drain()
    texts = {r["text"] for r in await rig.index.chunk_rows(mid)}
    assert any("second version" in t for t in texts)
    vecs = {k[2] for k in rig.store._rows if k[1] == mid}  # noqa: SLF001
    assert vecs == {str(r["id"]) for r in await rig.index.chunk_rows(mid)}


async def test_embedding_is_off_when_no_embedder_is_configured(
    make_rig: Callable[..., Any],
) -> None:
    async with make_rig(Options(embedder=False)) as rig:
        add(rig, 3)
        await rig.run_sync()
        await rig.drain()
        assert (await rig.pipeline.pipeline_status()).stages["embed"].pending == 0
        assert rig.embedder.calls == []
        assert (await rig.index.stats()).docs == 3
