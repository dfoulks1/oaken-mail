# SPDX-License-Identifier: Apache-2.0
"""A concurrency limit chosen at run time outranks config and takes effect without a restart."""

import asyncio
import random

import pytest
from pydantic import BaseModel

from oaken.core.clock import FakeClock
from oaken.core.db import Database
from oaken.core.jobs import Job, JobQueue, JobRunner
from oaken.core.settings import JobsSettings


class Ping(BaseModel):
    n: int = 0


@pytest.fixture
def queue(raw: Database, clock: FakeClock) -> JobQueue:
    return JobQueue(raw, clock, settings=JobsSettings(lease_seconds=60), rng=random.Random(1))


async def test_a_limit_changed_while_running_applies_to_the_next_claim(queue: JobQueue) -> None:
    limits = {"k": 1}

    async def overrides() -> dict[str, int]:
        return dict(limits)

    running = peak = 0
    first, release = asyncio.Event(), asyncio.Event()

    async def handler(_p: Ping, _job: Job) -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        first.set()
        await release.wait()
        running -= 1

    runner = JobRunner(queue, workers=4, limit_overrides=overrides)
    runner.register("k", Ping, handler, concurrency=4)  # registered at 4; the override says 1
    for _ in range(3):
        await queue.enqueue("k", Ping())
    drain = asyncio.create_task(runner.drain())
    await first.wait()
    await asyncio.sleep(0.05)
    assert running == 1, "the run-time limit of 1 outranks the registered 4"
    limits["k"] = 3  # raised live
    assert await queue.claim("probe", ["k"], {"k": limits["k"]}) is not None
    release.set()
    await drain
    assert peak <= 2
