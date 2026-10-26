# SPDX-License-Identifier: Apache-2.0
"""``[plugins.<name>] min_priority``: triage runs first and its priority gates the other enrichers."""

from typing import Any, cast

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


async def run(rig: Any, priority: int, bar: int | None) -> tuple[Rater, int]:
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
