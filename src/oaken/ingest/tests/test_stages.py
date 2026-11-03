# SPDX-License-Identifier: Apache-2.0
"""Skipping, quarantine, the enrichment cache, sync errors, flags and usage."""

from email.message import EmailMessage
from typing import Any

import pytest

from oaken.core.events import SyncFinished
from oaken.index import SearchFilters
from oaken.ingest.tests.conftest import ACCT, Options, Rig, mail

SKIP = (
    'require ["fileinto", "vnd.oaken.analysis"];\n'
    'if exists "list-id" { fileinto "Lists"; skip_enrich; }\n'
)


def with_attachment(n: int, name: str, data: bytes, ctype: tuple[str, str]) -> bytes:
    m = EmailMessage()
    m["From"], m["To"] = f"Alex <alex{n}@example.com>", "sam@example.org"
    m["Subject"], m["Message-ID"] = f"Subject {n}", f"<a{n}@example.com>"
    m["Date"] = "Mon, 01 Jan 2024 10:00:00 +0000"
    m.set_content(f"Please see the attached file number {n}.")
    m.add_attachment(data, maintype=ctype[0], subtype=ctype[1], filename=name)
    return m.as_bytes()


# -- Sieve filtering comes first ---------------------------------------------------------------
async def test_skip_enrich_keeps_a_message_out_of_embedding_and_enrichment(rig: Rig) -> None:
    (rig.sieve_dir / "10.sieve").write_text(SKIP)
    rig.engine.reload()
    listed = rig.provider.add_message(mail(1, extra="List-Id: <dev.example.net>\r\n"))
    plain = rig.provider.add_message(mail(2))
    await rig.run_sync()
    await rig.drain()
    ids = {pid: await rig.mid(pid) for pid in (listed, plain)}
    assert rig.enricher.seen == [ids[plain]]
    assert {c[0] for c in rig.embedder.calls} and not [
        k for k in rig.store._rows if k[1] == ids[listed]
    ]
    assert [k for k in rig.store._rows if k[1] == ids[plain]]
    # ... but it is still found by keyword search
    hits = await rig.index.keyword_search("quokkas", SearchFilters(), k=10)
    assert len(hits.hits) == 2


# -- quarantine ----------------------------------------------------------------------------------
# The writer is the `attachment` action's (oaken_actions); core only queues the route (ADR 0019).
async def test_a_flagged_attachment_is_routed_to_quarantine_when_an_action_answers(rig: Rig) -> None:
    from oaken.plugins.base import BaseAction
    from oaken.plugins.contracts import ActionResult

    class Keeper(BaseAction):
        routes = ("quarantine",)
        auto_routes = ("quarantine",)

        def __init__(self) -> None:
            self.seen: list[str] = []

        async def act(self, msg: Any, route: str) -> ActionResult | None:
            self.seen.append(msg.message_id)
            return ActionResult({"quarantined": 1})

    keeper = Keeper()
    rig.pipeline.backends.actions = lambda: {"keeper": keeper}
    rig.pipeline.register_actions(rig.runner)
    bad = rig.provider.add_message(
        with_attachment(1, "invoice.exe", b"MZ-not-run", ("application", "octet-stream"))
    )
    rig.provider.add_message(with_attachment(2, "notes.txt", b"just text", ("text", "plain")))
    await rig.run_sync()
    await rig.drain(rounds=4)
    assert keeper.seen == [await rig.mid(bad)]  # unflagged mail is not routed; no yes was needed


async def test_nothing_is_routed_when_no_action_answers_quarantine(rig: Rig) -> None:
    rig.provider.add_message(
        with_attachment(1, "invoice.exe", b"MZ-not-run", ("application", "octet-stream"))
    )
    await rig.run_sync()
    await rig.drain(rounds=3)
    assert await rig.pipeline.waiting_actions() == []  # no parked job either


# -- enrichment cache --------------------------------------------------------------------------
async def test_identical_content_is_enriched_once_and_copied(rig: Rig) -> None:
    a = rig.provider.add_message(mail(1, body="same words here"))
    b = rig.provider.add_message(mail(1, body="same words here", extra="X-Copy: 1\r\n"))
    await rig.run_sync()
    await rig.drain()
    assert len(rig.enricher.seen) == 1
    for pid in (a, b):
        mid = await rig.mid(pid)
        assert (await rig.pipeline.enrichment(mid, "words")) is not None
    rows = {r.stage: r for r in await rig.usage.usage_summary()}
    assert rows["enrich"].cached_calls == 1 and rows["enrich"].tokens_saved > 0


async def test_a_new_enricher_version_reruns_only_on_reprocess(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.provider.add_message(mail(1))
    await rig.run_sync()
    await rig.drain()
    assert len(rig.enricher.seen) == 1
    await rig.pipeline.reprocess("enrich")
    await rig.drain()
    assert len(rig.enricher.seen) == 1, "unchanged version and content must be a cache hit"
    monkeypatch.setattr(type(rig.enricher), "version", "2")  # a new release of the enricher
    await rig.pipeline.reprocess("enrich")
    await rig.drain()
    assert len(rig.enricher.seen) == 2


# -- sync --------------------------------------------------------------------------------------
async def test_a_provider_failure_is_reported_and_the_next_sync_resumes(rig: Rig) -> None:
    for i in range(3):
        rig.provider.add_message(mail(i))
    rig.provider.fail_next("pull", RuntimeError("boom"))
    first = await rig.run_sync()
    assert first.errors and first.stored == 0
    done = rig.names(SyncFinished)[-1]
    assert done.error
    second = await rig.run_sync()
    assert second.stored == 3 and not second.errors


async def test_server_flag_changes_reach_the_flag_sink_by_message_id(rig: Rig) -> None:
    got: list[Any] = []

    async def sink(account: Any, changes: Any) -> None:
        got.append((account, list(changes)))

    rig.sync.flag_sink = sink
    pid = rig.provider.add_message(mail(1))
    await rig.run_sync()
    rig.provider.server_flags(pid, add={"\\Seen"})
    await rig.run_sync()
    mid = await rig.mid(pid)
    assert got and got[0][0] == ACCT and mid in repr(got[0][1])


# -- usage -------------------------------------------------------------------------------------
async def test_usage_summary_groups_by_stage_and_model_and_filters_by_time(rig: Rig) -> None:
    await rig.usage.record("embed", "m1", tokens_in=10, account_id=ACCT)
    await rig.usage.record("embed", "m1", tokens_in=7, cached=True, account_id=ACCT)
    rig.clock.advance(hours=1)
    cut = rig.clock.now()
    await rig.usage.record("enrich", "m2", tokens_in=5, tokens_out=2, account_id=ACCT)
    rows = {(r.stage, r.model_id): r for r in await rig.usage.usage_summary()}
    e = rows[("embed", "m1")]
    assert (e.calls, e.cached_calls, e.tokens_in, e.tokens_saved) == (1, 1, 10, 7)
    later = await rig.usage.usage_summary(cut)
    assert [(r.stage, r.tokens_out) for r in later] == [("enrich", 2)]


@pytest.mark.parametrize("flag", [True, False])
async def test_quarantine_can_be_switched_off(make_rig: Any, flag: bool) -> None:
    async with make_rig(Options(quarantine=flag)) as rig:
        rig.provider.add_message(
            with_attachment(1, "a.exe", b"MZ", ("application", "x-msdownload"))
        )
        await rig.run_sync()
        await rig.drain()
        assert bool(list(rig.quarantine.root.rglob("*.quarantined"))) is flag
