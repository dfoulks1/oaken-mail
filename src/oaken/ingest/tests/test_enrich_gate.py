# SPDX-License-Identifier: Apache-2.0
"""``[plugins.<name>] min_priority``: triage runs first and its priority gates the other enrichers."""

import json
from typing import Any, cast

from oaken.core.settings import IngestSettings
from oaken.ingest.pipeline import EMBED, ENRICH, TRIAGE, MessagePayload, enrich_kind
from oaken.ingest.tests.conftest import Enricher, Options, mail
from oaken.plugins.base import BaseTriager
from oaken.plugins.contracts import EnrichInput, EnrichOutput, Triager


class Rater(BaseTriager):
    """A stand-in triager that rates every message with one priority."""

    version = "1"

    def __init__(self, priority: int) -> None:
        self.priority = priority
        self.calls = 0

    async def triage(self, msg: EnrichInput) -> EnrichOutput | None:
        self.calls += 1
        return EnrichOutput({"category": "personal", "priority": self.priority})


async def run(rig: Any, priority: int, bar: int | str | None) -> tuple[Rater, int]:
    rater = Rater(priority)
    # sorted by name "triage" would come after "words"; the gate must run it first anyway
    rig.pipeline.backends.enrichers = lambda: {"words": cast(Enricher, rig.enricher)}
    rig.pipeline.backends.triagers = lambda: {"triage": cast(Triager, rater)}
    if bar is not None:
        rig.pipeline.settings.plugins["words"] = {"min_priority": bar}
    rig.provider.add_message(mail(1))
    await rig.run_sync()
    await rig.drain(rounds=3)
    return rater, len(rig.enricher.seen)


async def test_a_message_below_the_bar_skips_the_gated_enricher(make_rig: Any) -> None:
    async with make_rig(Options(embedder=False)) as rig:
        rater, words = await run(rig, priority=1, bar=2)
        assert rater.calls == 1 and words == 0


async def test_a_message_at_the_bar_runs_it(make_rig: Any) -> None:
    async with make_rig(Options(embedder=False)) as rig:
        _, words = await run(rig, priority=2, bar=2)
        assert words == 1


async def test_no_bar_means_everything_runs(make_rig: Any) -> None:
    async with make_rig(Options(embedder=False)) as rig:
        _, words = await run(rig, priority=0, bar=None)
        assert words == 1


async def test_without_a_triage_result_nothing_is_gated(make_rig: Any) -> None:
    async with make_rig(Options(embedder=False)) as rig:
        rig.pipeline.settings.plugins["words"] = {"min_priority": 3}
        rig.provider.add_message(mail(1))
        await rig.run_sync()
        await rig.drain(rounds=3)
        assert len(rig.enricher.seen) == 1  # no triage enricher: the bar is not applied on a guess


async def test_a_bar_written_as_a_string_still_gates(make_rig: Any) -> None:
    # The console form writes ``min_priority = "2"``; a bar that is silently ignored is the worst bug.
    async with make_rig(Options(embedder=False)) as rig:
        rater, words = await run(rig, priority=1, bar="2")
        assert rater.calls == 1 and words == 0


class Broken(BaseTriager):
    """A triager whose model is down."""

    version = "1"

    def __init__(self) -> None:
        self.calls = 0

    async def triage(self, msg: EnrichInput) -> EnrichOutput | None:
        self.calls += 1
        raise RuntimeError("model down")


async def test_triage_is_its_own_job_and_queues_the_rest_when_done(make_rig: Any) -> None:
    async with make_rig(Options(embedder=False)) as rig:
        rater, words = await run(rig, priority=2, bar=2)
        assert rater.calls == 1 and words == 1  # the enricher's job did not ask triage again
        assert len(await rig.queue.query(kind=TRIAGE, status="succeeded")) == 1
        assert len(await rig.queue.query(kind=enrich_kind("words"), status="succeeded")) == 1
        assert await rig.queue.query(kind=ENRICH) == []  # the legacy kind is no longer queued


async def test_a_legacy_enrich_job_drains_into_triage_and_per_enricher_jobs(make_rig: Any) -> None:
    # An enrich job queued before the split has no triage job ahead of it; it queues triage, which
    # queues the rest, and it never calls a model itself.
    async with make_rig(Options(embedder=False, enrich=False)) as rig:
        pid = rig.provider.add_message(mail(1))
        await rig.run_sync()
        await rig.drain()
        rater = Rater(2)
        rig.pipeline.backends.enrichers = lambda: {"words": cast(Enricher, rig.enricher)}
        rig.pipeline.backends.triagers = lambda: {"triage": cast(Triager, rater)}
        rig.pipeline.register_enrichers(rig.runner)  # "words" was not there at start
        rig.pipeline.settings.plugins["words"] = {"min_priority": 2}
        await rig.queue.enqueue(ENRICH, MessagePayload(message_id=await rig.mid(pid)))
        await rig.drain(rounds=3)
        assert rater.calls == 1 and len(rig.enricher.seen) == 1
        assert len(await rig.queue.query(kind=ENRICH, status="succeeded")) == 1


async def test_triage_failing_for_good_still_queues_the_gated_enrichers(make_rig: Any) -> None:
    opts = Options(embedder=False, ingest=IngestSettings(stage_max_attempts=2))
    async with make_rig(opts) as rig:
        broken = Broken()
        rig.pipeline.backends.enrichers = lambda: {"words": cast(Enricher, rig.enricher)}
        rig.pipeline.backends.triagers = lambda: {"triage": cast(Triager, broken)}
        rig.pipeline.settings.plugins["words"] = {"min_priority": 2}
        rig.provider.add_message(mail(1))
        await rig.run_sync()
        await rig.drain(rounds=4)
        assert broken.calls == 2  # both attempts, and the enrich job did not try it a third time
        assert len(rig.enricher.seen) == 1


async def test_embedding_waits_for_triage_and_follows_it(make_rig: Any) -> None:
    async with make_rig(Options()) as rig:
        rater, _ = await run(rig, priority=2, bar=None)
        assert rater.calls == 1
        assert len(await rig.queue.query(kind=EMBED, status="succeeded")) == 1


async def test_no_embed_job_exists_while_triage_is_still_retrying(make_rig: Any) -> None:
    async with make_rig(Options(ingest=IngestSettings(stage_max_attempts=2))) as rig:
        broken = Broken()
        rig.pipeline.backends.enrichers = lambda: {"words": cast(Enricher, rig.enricher)}
        rig.pipeline.backends.triagers = lambda: {"triage": cast(Triager, broken)}
        rig.provider.add_message(mail(1))
        await rig.run_sync()
        await rig.drain()  # parse, then triage attempt 1 fails and waits out its backoff
        assert await rig.queue.query(kind=EMBED) == []
        await rig.drain(rounds=2)  # attempt 2 is the last: embed is queued anyway
        assert len(await rig.queue.query(kind=EMBED, status="succeeded")) == 1


async def test_junk_is_not_embedded_but_unrated_mail_is(make_rig: Any) -> None:
    async with make_rig(Options()) as rig:
        await run(rig, priority=0, bar=None)  # default embed_min_priority is 1
        assert await rig.queue.query(kind=EMBED) == []
    async with make_rig(Options(ingest=IngestSettings(embed_min_priority=None))) as rig:
        await run(rig, priority=0, bar=None)
        assert len(await rig.queue.query(kind=EMBED, status="succeeded")) == 1


async def test_a_triage_preview_shows_the_verdict_and_stores_nothing(make_rig: Any) -> None:
    async with make_rig(Options(embedder=False, enrich=False)) as rig:
        pid = rig.provider.add_message(mail(1))
        await rig.run_sync()
        await rig.drain()
        rater = Rater(2)
        rig.pipeline.backends.triagers = lambda: {"triage": cast(Triager, rater)}
        mid = await rig.mid(pid)
        shown = await rig.pipeline.triage_preview(mid)
        assert shown.triager == "triage" and shown.result is not None
        assert shown.result["priority"] == 2 and rater.calls == 1
        assert await rig.pipeline.enrichment(mid, "triage") is None  # nothing was stored
        pasted = await rig.pipeline.triage_preview(None, text="Please reply", subject="Hi")
        assert pasted.triager == "triage" and await rig.queue.query(kind=TRIAGE) == []


async def test_a_triage_preview_with_no_triager_says_so(make_rig: Any) -> None:
    async with make_rig(Options(embedder=False, enrich=False)) as rig:
        shown = await rig.pipeline.triage_preview(None, text="hello", subject="x")
        assert shown.triager is None and shown.result is None and "no triager" in shown.note


async def test_an_ingest_is_traced_job_by_job_under_its_message(
    make_rig: Any, tmp_path: Any
) -> None:
    from oaken.core.clock import FakeClock
    from oaken.core.trace import Tracer

    async with make_rig(Options(embedder=False)) as rig:
        t = Tracer(tmp_path / "t.jsonl", FakeClock(), max_bytes=1 << 20, backups=1, enabled=True)
        rig.runner.tracer = t
        rig.pipeline.tracer = t
        rater, _ = await run(rig, priority=2, bar=None)
        events = t.tail(500)
        kinds = {e["kind"] for e in events}
        assert {"job.queued", "job.start", "job.done", "ingest.stored"} <= kinds
        triage = next(
            e["data"]
            for e in events
            if e["kind"] == "ingest.stored" and e["data"]["enricher"] == "triage"
        )
        assert triage["job_kind"] == "ingest.triage" and triage["message_id"]
        assert triage["result"]["priority"] == 2 and rater.calls == 1
        # every event of that job carries its id, so `oaken trace --job` can follow it
        job_events = [e for e in events if e["data"].get("job_id") == triage["job_id"]]
        assert {e["kind"] for e in job_events} >= {"job.start", "ingest.stored", "job.done"}


class Boom(BaseTriager):
    """A model triager that must not be asked: the rules decide first."""

    version = "1"
    rank = 90
    needs_llm = False  # keeps the rig from needing a language model

    def __init__(self) -> None:
        self.calls = 0

    async def triage(self, msg: EnrichInput) -> EnrichOutput | None:
        self.calls += 1
        return EnrichOutput({"category": "work", "priority": 3})


class Rules(Rater):
    rank = 10


async def test_the_rules_triager_decides_before_any_model_is_asked(make_rig: Any) -> None:
    async with make_rig(Options(embedder=False)) as rig:
        rules, ai = Rules(0), Boom()
        # the model is handed over first, and its name sorts first: rank still puts the rules first
        rig.pipeline.backends.triagers = lambda: {
            "a-model": cast(Triager, ai),
            "rules": cast(Triager, rules),
        }
        rig.provider.add_message(mail(1))
        await rig.run_sync()
        await rig.drain(rounds=3)
        assert rules.calls == 1 and ai.calls == 0
        (stored,) = [r async for r in _stored(rig) if r["enricher"] == "triage"]
        assert json.loads(stored["data_json"])["priority"] == 0


async def test_the_model_is_asked_only_when_the_rules_are_off_or_leave_the_mail(
    make_rig: Any,
) -> None:
    async with make_rig(Options(embedder=False)) as rig:
        ai = Boom()
        rig.pipeline.backends.triagers = lambda: {
            "ai": cast(Triager, ai),
            "rules": cast(Triager, _Passes()),  # a rules triager that settles nothing
        }
        rig.provider.add_message(mail(1))
        await rig.run_sync()
        await rig.drain(rounds=3)
        assert ai.calls == 1  # the rules had nothing to say
        # switched off, the rules are skipped and the model decides
        ai2 = Boom()
        rules = Rules(0)
        rig.pipeline.backends.triagers = lambda: {
            "ai": cast(Triager, ai2),
            "rules": cast(Triager, rules),
        }
        rig.pipeline.settings.plugins["triager"] = {"rules": {"enabled": False}}
        rig.provider.add_message(mail(2))
        await rig.run_sync()
        await rig.drain(rounds=3)
        assert rules.calls == 0 and ai2.calls == 1


class _Passes(BaseTriager):
    version = "1"
    rank = 10

    async def triage(self, msg: EnrichInput) -> EnrichOutput | None:
        return None


async def _stored(rig: Any) -> Any:
    for r in await rig.raw_db.fetchall("SELECT enricher, data_json FROM enrichments"):
        yield r
