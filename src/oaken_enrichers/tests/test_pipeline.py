# SPDX-License-Identifier: Apache-2.0
"""The enrichers inside the real pipeline: stored, cached, refreshed one at a time, never marking
mail read, and given a reply's parent summary."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest

from oaken.ai import LLMCache, LLMService, Workspaces
from oaken.ai.access import WorkspaceLLM
from oaken.core.logging import get_logger
from oaken.core.settings import Settings
from oaken.ingest.tests.conftest import Options, Rig, build, mail
from oaken.plugins.contracts import Enricher, LLMRequest, PluginContext, Triager
from oaken_enrichers import ActionsEnricher, InspectorEnricher, SummaryEnricher
from oaken_triagers import AITriager, SpamCheckTriager

NAMES = ("summary", "actions", "inspector")


def answer(request: LLMRequest) -> dict[str, Any]:
    props = set((request.json_schema or {}).get("properties", {}))
    if "key_points" in props:
        return {"summary": "A short summary.", "key_points": ["one"]}
    if "category" in props:
        return {"category": "work", "priority": 2, "needs_reply": False,
                "waiting_on_others": False, "reason": "ordinary"}  # fmt: skip
    return {"action_items": [], "deadlines": []}


async def start_all(rig: Rig, settings: Settings) -> tuple[dict[str, Enricher], dict[str, Triager]]:
    service = LLMService(
        LLMCache(rig.raw_db, rig.clock), rig.usage, Workspaces(settings), lambda _n: rig.llm,
        settings,
    )  # fmt: skip

    def context(name: str, kind: Any) -> PluginContext:
        return PluginContext(
            name=name, kind=kind, settings=settings, clock=rig.clock,
            log=get_logger("test"), llm=WorkspaceLLM(service, rig.raw),
        )  # fmt: skip

    found: dict[str, Enricher] = {}
    for name, cls in zip(NAMES, (SummaryEnricher, ActionsEnricher, InspectorEnricher), strict=True):
        plugin = cls()
        await plugin.start(context(name, "enricher"))
        found[name] = cast(Enricher, plugin)
    triagers: dict[str, Triager] = {}
    for name, tcls in (("spamcheck", SpamCheckTriager), ("ai", AITriager)):
        triager = tcls()
        await triager.start(context(name, "triager"))
        triagers[name] = cast(Triager, triager)
    return found, triagers


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    async for r in build(tmp_path, Options(enrich=False)):
        r.llm.responder = answer
        started, triagers = await start_all(r, r.pipeline.settings)

        def enrichers(found: dict[str, Enricher] = started) -> dict[str, Enricher]:
            return found

        def triager_set(found: dict[str, Triager] = triagers) -> dict[str, Triager]:
            return found

        r.pipeline.backends.enrichers = enrichers
        r.pipeline.backends.triagers = triager_set
        r.pipeline.register_enrichers(r.runner)  # the enrichers were not known at build time
        yield r


async def rows(rig: Rig, mid: str) -> dict[str, dict[str, Any]]:
    got = await rig.raw_db.fetchall(
        "SELECT enricher, version, created_at FROM enrichments WHERE message_id = ?"
        " UNION ALL SELECT 'triage', version, created_at FROM triage_verdicts WHERE message_id = ?",
        (mid, mid),
    )  # the verdict is stored apart from the enrichers (ADR 0017); the tests count both
    return {r["enricher"]: dict(r) for r in got}


async def test_every_enricher_stores_a_result(rig: Rig) -> None:
    pid = rig.provider.add_message(mail(1, body="Please send the report by Friday."))
    await rig.run_sync()
    await rig.drain()
    mid = await rig.mid(pid)
    assert set(await rows(rig, mid)) == set(NAMES) | {"triage"}
    summary = await rig.pipeline.enrichment(mid, "summary")
    assert summary is not None and summary["summary"] == "A short summary."
    inspector: Any = await rig.pipeline.enrichment(mid, "inspector")
    assert inspector is not None and inspector["authentication"]["spf"] == "absent"


async def test_a_rerun_is_all_cache_hits_and_asks_the_model_nothing(rig: Rig) -> None:
    rig.provider.add_message(mail(1))
    await rig.run_sync()
    await rig.drain()
    asked = len(rig.llm.requests)
    assert asked == 3  # summary, triage, actions
    await rig.pipeline.reprocess("enrich")
    await rig.drain()
    assert len(rig.llm.requests) == asked
    enrich = [r for r in await rig.usage.usage_summary() if r.stage == "enrich"]
    assert sum(r.cached_calls for r in enrich) >= 4  # every enricher, from the stored rows
    assert sum(r.tokens_saved for r in enrich) > 0


async def test_bumping_one_enricher_refreshes_only_its_rows(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = rig.provider.add_message(mail(1))
    await rig.run_sync()
    await rig.drain()
    mid = await rig.mid(pid)
    before = await rows(rig, mid)
    rig.clock.advance(hours=3)
    asked = len(rig.llm.requests)
    monkeypatch.setattr(SummaryEnricher, "version", "2")
    await rig.pipeline.reprocess("enrich")
    await rig.drain()
    after = await rows(rig, mid)
    assert (
        after["summary"]["version"] == "2"
        and after["summary"]["created_at"] != before["summary"]["created_at"]
    )
    for other in ("triage", "actions", "inspector"):
        assert after[other] == before[other], f"{other} must not be touched"
    # The summary re-ran (its stored row was stale) but its model answer is cached, so the
    # version bump costs no tokens; the other three enrichers were not asked at all.
    assert len(rig.llm.requests) == asked


async def test_enrichment_never_marks_mail_read(rig: Rig) -> None:
    pid = rig.provider.add_message(mail(1))
    await rig.run_sync()
    await rig.drain()
    mid = await rig.mid(pid)
    state = await rig.raw_db.fetchone("SELECT is_read FROM message_state WHERE message_id = ?",
                                      (mid,))  # fmt: skip
    assert state is not None and state["is_read"] == 0


async def test_an_enricher_can_be_switched_off_in_config(rig: Rig) -> None:
    rig.pipeline.settings = rig.pipeline.settings.model_copy(
        update={"plugins": {"summary": {"enabled": False}}}
    )
    pid = rig.provider.add_message(mail(1))
    await rig.run_sync()
    await rig.drain()
    assert set(await rows(rig, await rig.mid(pid))) == {"triage", "actions", "inspector"}


async def test_a_reply_is_summarized_with_its_parents_stored_summary(rig: Rig) -> None:
    def thread(n: int, day: int, body: str, extra: str) -> bytes:
        return (
            f"From: Alex <alex@example.com>\r\nTo: Sam <sam@example.org>\r\n"
            f"Subject: {'Re: ' if n > 1 else ''}Lunch\r\n"
            f"Date: {day:02d} Jan 2024 10:00:00 +0000\r\n"
            f"Message-ID: <t{n}@example.com>\r\n{extra}\r\n{body}\r\n"
        ).encode()

    summaries: list[object] = []  # the first summary asked for is the parent's

    def responder(r: Any) -> Any:
        if "key_points" in (r.json_schema or {}).get("properties", {}) and not summaries:
            summaries.append(r)
            return {"summary": "Alex proposes lunch.", "key_points": []}
        return answer(r)

    rig.llm.responder = responder
    first = rig.provider.add_message(thread(1, 1, "Lunch on Tuesday?", ""))
    await rig.run_sync()
    await rig.drain()
    rig.llm.requests.clear()
    second = rig.provider.add_message(
        thread(
            2,
            2,
            "Tuesday works.",
            "In-Reply-To: <t1@example.com>\r\nReferences: <t1@example.com>\r\n",
        )
    )
    await rig.run_sync()
    await rig.drain()
    mid = await rig.mid(second)
    assert await rig.pipeline.enrichment(await rig.mid(first), "summary") is not None
    summary_request = next(
        r for r in rig.llm.requests if "key_points" in (r.json_schema or {}).get("properties", {})
    )
    labels = {
        getattr(p, "label", None): p.unwrap()
        for p in summary_request.user
        if not isinstance(p, str)
    }
    assert labels["parent_summary"] == "Alex proposes lunch."
    assert "Lunch on Tuesday" not in labels["body"], "the parent's body is never sent"
    assert await rig.pipeline.enrichment(mid, "summary") is not None
