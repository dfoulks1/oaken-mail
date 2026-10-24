# SPDX-License-Identifier: Apache-2.0
"""Pausing the model: pause_backfill / resume_backfill, retry_parked, and that the job runner
stops claiming the model-bound stages while paused (prompt 17 CLI gap)."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from oaken.core.errors import NotExposed
from oaken.core.jobs import PRIORITY_INGEST
from oaken.core.operations.tests.conftest import Ops, agent
from oaken.ingest.pipeline_control import LLM_STAGES


class _Payload(BaseModel):
    note: str = "x"


async def _noop(_payload: _Payload, _job: object) -> None:
    return None


async def test_pause_and_resume_flip_the_durable_flag_and_show_in_status(ops: Ops) -> None:
    paused = await ops.out("pause_backfill", {"why": "on battery"})
    assert paused.llm_paused is True and paused.reason == "on battery"
    status = await ops.out("pipeline_status", {})
    assert status.llm_paused is True
    resumed = await ops.out("resume_backfill", {})
    assert resumed.llm_paused is False
    assert (await ops.out("pipeline_status", {})).llm_paused is False


async def test_pause_is_audited(ops: Ops) -> None:
    await ops.out("pause_backfill", {"why": "freeing the gpu"})
    assert any("pipeline.pause" in op for op in await ops.audit_ops())


async def test_the_runner_does_not_claim_model_stages_while_paused(ops: Ops) -> None:
    # The pause lives in the runner: it subtracts the paused kinds before asking the queue to
    # claim. The low-level claim() is unaware (it is a primitive); the runner is the gate.
    from oaken.core.jobs import JobRunner

    control = ops.svc.pipeline_control
    assert control is not None
    runner = JobRunner(ops.svc.queue, paused_kinds=control.paused_kinds)
    runner.register("ingest.parse", _Payload, _noop)
    runner.register("ingest.embed", _Payload, _noop)
    runner.register("ingest.enrich", _Payload, _noop)

    assert set(await runner._claimable_kinds()) == {"ingest.parse", "ingest.embed", "ingest.enrich"}
    await ops.out("pause_backfill", {})
    assert await control.paused_kinds() == LLM_STAGES
    # While paused the runner offers only the non-model stage.
    assert set(await runner._claimable_kinds()) == {"ingest.parse"}
    await ops.out("resume_backfill", {})
    assert "ingest.embed" in await runner._claimable_kinds()


async def test_retry_parked_requeues_failed_ingest_jobs(ops: Ops) -> None:
    # Make an ingest job fail permanently so it parks.
    e = await ops.svc.queue.enqueue("ingest.enrich", _Payload(), priority=PRIORITY_INGEST)
    got = await ops.svc.queue.claim("w", ["ingest.enrich"])
    assert got is not None
    await ops.svc.queue.fail(got.id, "w", "boom", permanent=True)
    assert (await ops.svc.queue.get(e.job_id)).status == "failed"  # type: ignore[union-attr]

    out = await ops.out("retry_parked", {"all": True})
    assert str(e.job_id) in out.retried
    assert (await ops.svc.queue.get(e.job_id)).status == "queued"  # type: ignore[union-attr]


async def test_retry_parked_needs_exactly_one_of_job_id_or_all(ops: Ops) -> None:
    from oaken.core.errors import Conflict

    with pytest.raises(Conflict):
        await ops.out("retry_parked", {})
    with pytest.raises(Conflict):
        await ops.out("retry_parked", {"job_id": "x", "all": True})


async def test_pipeline_control_is_never_offered_to_an_agent(ops: Ops) -> None:
    for name, payload in (
        ("pause_backfill", {}),
        ("resume_backfill", {}),
        ("retry_parked", {"all": True}),
    ):
        with pytest.raises(NotExposed):
            await ops.call(name, payload, agent())


# -- prompt 30: one stage at a time, live limits, recent-only reprocess ------------------------


async def test_one_stage_can_be_paused_alone_and_the_status_says_so(ops: Ops) -> None:
    out = await ops.out("pause_backfill", {"stage": "enrich", "why": "gpu is busy"})
    assert out.llm_paused is False and out.paused_stages == ["enrich"]
    status = await ops.out("pipeline_status", {})
    assert status.stages["enrich"].paused is True and status.stages["embed"].paused is False
    control = ops.svc.pipeline_control
    assert control is not None and await control.paused_kinds() == {"ingest.enrich"}
    again = await ops.out("resume_backfill", {"stage": "enrich"})
    assert again.paused_stages == []
    with pytest.raises(Exception, match="stage"):
        await ops.call("pause_backfill", {"stage": "parse"})


async def test_resuming_one_stage_while_everything_is_paused_keeps_the_other_paused(
    ops: Ops,
) -> None:
    await ops.out("pause_backfill", {})
    out = await ops.out("resume_backfill", {"stage": "embed"})
    assert out.llm_paused is False and out.paused_stages == ["enrich"]
    out = await ops.out("resume_backfill", {})
    assert out.paused_stages == []


async def test_a_stage_pause_survives_a_restart(ops: Ops) -> None:
    from oaken.ingest.pipeline_control import PipelineControl

    await ops.out("pause_backfill", {"stage": "embed"})
    reopened = PipelineControl(ops.svc.raw.db, ops.svc.clock, ops.svc.audit)  # a new process
    assert await reopened.paused_kinds() == {"ingest.embed"}
    assert any("pipeline.pause" in op for op in await ops.audit_ops())


async def test_concurrency_is_set_stored_audited_and_bounded(ops: Ops) -> None:
    out = await ops.out("set_pipeline_limits", {"stage": "enrich", "concurrency": 2})
    assert (out.stage, out.concurrency) == ("enrich", 2)
    control = ops.svc.pipeline_control
    assert control is not None and await control.limits() == {"ingest.enrich": 2}
    assert (await ops.out("pipeline_status", {})).stages["enrich"].concurrency == 2
    assert "pipeline.limit" in await ops.audit_ops()
    for bad in (0, 65):
        with pytest.raises(Exception, match="concurrency"):
            await ops.call("set_pipeline_limits", {"stage": "enrich", "concurrency": bad})


async def test_no_pipeline_management_is_offered_to_an_agent(ops: Ops) -> None:
    names = {"pause_backfill", "resume_backfill", "set_pipeline_limits", "retry_parked"}
    assert not names & {o.name for o in await ops.client.visible(agent())}


async def test_reprocess_can_be_limited_to_recent_mail(ops: Ops) -> None:
    ids = await ops.add(4)
    newest = await ops.rig.index.recent_doc_ids(limit=2)
    assert len(newest) == 2 and set(newest) <= {str(i) for i in ids}
    assert await ops.rig.index.recent_doc_ids(since="2999-01-01") == []
    assert len(await ops.rig.index.recent_doc_ids()) == 4
    queued = await ops.out("reprocess", {"stage": "enrich", "limit": 2, "since": "2020-01-01"})
    other = await ops.out("reprocess", {"stage": "enrich"})
    assert queued.job_id != other.job_id, "a limited run is not the same work as a full one"
    with pytest.raises(Exception, match="since"):
        await ops.call("reprocess", {"stage": "enrich", "since": "last tuesday"})
