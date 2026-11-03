# SPDX-License-Identifier: Apache-2.0
"""Skipping, quarantine, the enrichment cache, sync errors, flags and usage."""

import base64
import os
import stat
from email.message import EmailMessage
from typing import Any, cast

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
# Core moves every attachment out of the message on the way in. A scanner is an action on the `scan`
# route; core only queues it. Nothing here depends on an action being installed (ADR 0019).
EXE = ("application", "x-msdownload")


async def test_an_attachment_never_enters_raw_db_and_is_routed_to_scan(make_rig: Any) -> None:
    from oaken.plugins.base import BaseAction
    from oaken.plugins.contracts import ActionResult

    class Scanner(BaseAction):
        routes = ("scan",)
        auto_routes = ("scan",)

        def __init__(self) -> None:
            self.seen: list[str] = []

        async def act(self, msg: Any, route: str) -> ActionResult | None:
            self.seen.append(msg.message_id)
            return ActionResult({"scanned": 1})

    async with make_rig(Options(vault=True)) as rig:
        scanner = Scanner()
        rig.pipeline.backends.actions = lambda: {"scanner": cast(Any, scanner)}
        rig.pipeline.register_actions(rig.runner)
        bad = rig.provider.add_message(with_attachment(1, "invoice.exe", b"MZ-not-run", EXE))
        txt = rig.provider.add_message(
            with_attachment(2, "notes.txt", b"just text", ("text", "plain"))
        )
        await rig.run_sync()
        await rig.drain(rounds=4)
        for pid, payload in ((bad, b"MZ-not-run"), (txt, b"just text")):
            raw = await rig.raw.get_raw(await rig.mid(pid))
            assert payload not in raw and base64.b64encode(payload) not in raw  # not in raw.db
            stub = b"X-Oaken-Detache"  # a stub is there, plain or base64 like its part
            assert stub in raw or base64.b64encode(stub) in raw
        files = list(rig.quarantine.rglob("*.quarantined"))
        assert len(files) == 2
        if os.name != "nt":
            assert all(stat.S_IMODE(f.stat().st_mode) == 0 for f in files)  # nothing can open it
        assert sorted(scanner.seen) == sorted([str(await rig.mid(bad)), str(await rig.mid(txt))])
        row = await rig.raw.db.fetchone(
            "SELECT COUNT(*) AS n FROM quarantined_files WHERE scan_status = 'pending'"
        )
        assert row is not None and row["n"] == 2  # held until a scanner says clean


async def test_a_message_is_still_stored_and_searchable_with_no_scanner_installed(
    make_rig: Any,
) -> None:
    async with make_rig(Options(vault=True)) as rig:
        rig.provider.add_message(with_attachment(1, "invoice.exe", b"MZ-not-run", EXE))
        await rig.run_sync()
        await rig.drain(rounds=3)
        assert await rig.pipeline.waiting_actions() == []  # no parked job
        assert len(list(rig.quarantine.rglob("*.quarantined"))) == 1  # still moved out
        hits = await rig.index.keyword_search("attached", SearchFilters(), k=10)
        assert len(hits.hits) == 1


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


async def test_every_redo_is_a_reprocess_stage(rig: Rig) -> None:
    rig.provider.add_message(mail(1))
    await rig.run_sync()
    await rig.drain()
    for stage in ("start", "triage", "scan"):
        await rig.pipeline.reprocess(stage)
        await rig.drain()
    with pytest.raises(ValueError, match="route"):
        await rig.pipeline.reprocess("action")


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
    async with make_rig(Options(quarantine=flag, vault=True)) as rig:
        rig.provider.add_message(with_attachment(1, "a.exe", b"MZ", EXE))
        await rig.run_sync()
        await rig.drain()
        assert bool(list(rig.quarantine.rglob("*.quarantined"))) is flag
