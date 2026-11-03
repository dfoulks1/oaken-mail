# SPDX-License-Identifier: Apache-2.0
"""One job kind per enricher: stage names, pause and limit reach, and per-enricher status."""

from typing import Any

import pytest

from oaken.core.errors import InvalidInput
from oaken.core.jobs import _under, _with_overrides
from oaken.ingest.pipeline_control import stage_kind
from oaken.ingest.tests.conftest import Options, mail


def test_stage_names_map_to_kinds() -> None:
    assert stage_kind("enrich") == "ingest.enrich"
    assert stage_kind("triage") == "ingest.triage"
    assert stage_kind("enrich.summary") == "ingest.enrich.summary"
    for bad in ("nope", "enrich.", "enrich.a b", "ingest.enrich"):
        with pytest.raises(InvalidInput):
            stage_kind(bad)


def test_a_pause_on_a_stage_covers_each_enricher_under_it() -> None:
    assert _under("ingest.enrich.summary", {"ingest.enrich"})
    assert _under("ingest.enrich", {"ingest.enrich"})
    assert not _under("ingest.enrichment", {"ingest.enrich"})
    assert not _under("ingest.triage", {"ingest.enrich"})


def test_a_limit_on_a_stage_reaches_each_enricher_and_an_exact_one_wins() -> None:
    kinds = ["ingest.enrich.summary", "ingest.enrich.actions", "ingest.embed"]
    got = _with_overrides({}, kinds, {"ingest.enrich": 3, "ingest.enrich.actions": 1})
    assert got["ingest.enrich.summary"] == 3 and got["ingest.enrich.actions"] == 1
    assert "ingest.embed" not in got


async def test_status_sums_the_enrich_stage_and_lists_each_enricher(make_rig: Any) -> None:
    async with make_rig(Options(embedder=False)) as rig:
        rig.provider.add_message(mail(1))
        await rig.run_sync()
        await rig.drain(rounds=3)
        status = await rig.pipeline.pipeline_status()
        assert "enrich.words" in status.enrichers
        assert status.stages["enrich"].per_second == status.enrichers["enrich.words"].per_second


def test_stages_are_listed_in_the_order_they_run() -> None:
    from oaken.ingest.pipeline import STAGE_KINDS
    from oaken.ingest.pipeline_control import STAGES

    assert list(STAGES) == ["parse", "triage", "embed", "enrich", "action"]
    assert [k.removeprefix("ingest.") for k in STAGE_KINDS[:4]] == list(STAGES)
