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
