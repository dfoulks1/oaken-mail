# SPDX-License-Identifier: Apache-2.0
"""The person's job-queue control operations: list, cancel and retry (prompt 17 CLI gap).

These steer the durable queue a person owns. They are never offered over MCP (an agent must not
cancel or re-run the user's work), and the mutations go through the confirm gate and leave an
audit row.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from oaken.core.errors import NotExposed, NotFound
from oaken.core.ids import JobId
from oaken.core.jobs import PRIORITY_INGEST
from oaken.core.operations.tests.conftest import Ops, agent


class _Payload(BaseModel):
    note: str = "x"


async def _enqueue(ops: Ops, kind: str = "ingest.enrich") -> str:
    e = await ops.svc.queue.enqueue(kind, _Payload(), priority=PRIORITY_INGEST)
    return str(e.job_id)


async def test_list_jobs_returns_the_newest_with_filters(ops: Ops) -> None:
    a = await _enqueue(ops, "ingest.parse")
    ops.rig.clock.advance(seconds=1)  # the clock is frozen: without this the order is a coin flip
    b = await _enqueue(ops, "ingest.enrich")
    out = await ops.out("list_jobs", {})
    ids = [j.id for j in out.jobs]
    assert a in ids and b in ids
    # newest first
    assert ids.index(b) < ids.index(a)
    # filter by kind
    only = await ops.out("list_jobs", {"kind": "ingest.enrich"})
    assert [j.id for j in only.jobs] == [b]
    # filter by status
    queued = await ops.out("list_jobs", {"status": "queued"})
    assert {j.id for j in queued.jobs} == {a, b}


async def test_cancel_job_cancels_a_queued_job_and_is_audited(ops: Ops) -> None:
    job_id = await _enqueue(ops)
    out = await ops.out("cancel_job", {"job_id": job_id})
    assert out.status == "cancelled"
    assert (await ops.svc.queue.get(JobId(job_id))).status == "cancelled"  # type: ignore[union-attr]
    assert any("cancel_job" in op for op in await ops.audit_ops())


async def test_retry_job_requeues_a_cancelled_job(ops: Ops) -> None:
    job_id = await _enqueue(ops)
    await ops.out("cancel_job", {"job_id": job_id})
    out = await ops.out("retry_job", {"job_id": job_id})
    assert out.status == "queued"
    job = await ops.svc.queue.get(JobId(job_id))
    assert job is not None and job.status == "queued" and job.last_error is None
    assert any("retry_job" in op for op in await ops.audit_ops())


async def test_retry_an_unknown_job_is_not_found(ops: Ops) -> None:
    with pytest.raises(NotFound):
        await ops.out("retry_job", {"job_id": "01999999-0000-7000-8000-000000000000"})


async def test_job_mutations_are_never_offered_to_an_agent(ops: Ops) -> None:
    # Reading the queue is fine for an agent (no mail content); cancelling or re-running the
    # user's work is not — those are mcp="never".
    job_id = await _enqueue(ops)
    for name, payload in (("cancel_job", {"job_id": job_id}), ("retry_job", {"job_id": job_id})):
        with pytest.raises(NotExposed):
            await ops.call(name, payload, agent())
