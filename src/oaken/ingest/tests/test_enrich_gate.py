# SPDX-License-Identifier: Apache-2.0
"""``[plugins.<name>] min_priority``: triage runs first and its priority gates the other enrichers."""

from typing import Any, cast

from oaken.core.settings import IngestSettings
from oaken.ingest.pipeline import EMBED, ENRICH, TRIAGE, MessagePayload, enrich_kind
from oaken.ingest.tests.conftest import Enricher, Options, mail
from oaken.plugins.base import BaseEnricher
from oaken.plugins.contracts import EnrichInput, EnrichOutput


class Rater(BaseEnricher):
    """A stand-in for triage: priority from the message number in its id order."""

    version = "1"

    def __init__(self, priority: int) -> None:
        self.priority = priority
        self.calls = 0

    async def enrich(self, msg: EnrichInput) -> EnrichOutput:
        self.calls += 1
        return EnrichOutput({"category": "personal", "priority": self.priority})


async def run(rig: Any, priority: int, bar: int | str | None) -> tuple[Rater, int]:
    rater = Rater(priority)
    # sorted by name "triage" would come after "words"; the gate must run it first anyway
    rig.pipeline.backends.enrichers = lambda: {
        "words": cast(Enricher, rig.enricher),
        "triage": cast(Enricher, rater),
    }
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


class Broken(BaseEnricher):
    """A triage whose model is down."""

    version = "1"

    def __init__(self) -> None:
        self.calls = 0

    async def enrich(self, msg: EnrichInput) -> EnrichOutput:
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
        rig.pipeline.backends.enrichers = lambda: {
            "words": cast(Enricher, rig.enricher),
            "triage": cast(Enricher, rater),
        }
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
        rig.pipeline.backends.enrichers = lambda: {
            "words": cast(Enricher, rig.enricher),
            "triage": cast(Enricher, broken),
        }
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
        rig.pipeline.backends.enrichers = lambda: {
            "words": cast(Enricher, rig.enricher),
            "triage": cast(Enricher, Broken()),
        }
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
