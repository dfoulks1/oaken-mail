# SPDX-License-Identifier: Apache-2.0
import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel

from oaken.core.clock import FakeClock
from oaken.core.db import Database
from oaken.core.errors import ConfigError
from oaken.core.jobs import Job, JobQueue, JobRunner
from oaken.core.scheduler import Scheduler
from oaken.core.settings import JobsSettings


class Tick(BaseModel):
    label: str = "t"


@pytest.fixture
def queue(raw: Database, clock: FakeClock) -> JobQueue:
    return JobQueue(raw, clock, settings=JobsSettings(lease_seconds=60))


@pytest.fixture
def sched(raw: Database, queue: JobQueue, clock: FakeClock) -> Scheduler:
    return Scheduler(raw, queue, clock)


async def job_count(raw: Database, kind: str = "sync") -> int:
    row = await raw.fetchone("SELECT COUNT(*) AS n FROM jobs WHERE kind = ?", (kind,))
    assert row is not None
    return int(row["n"])


async def test_interval_fires_at_the_expected_times(
    sched: Scheduler, raw: Database, clock: FakeClock
) -> None:
    await sched.add("sync", "sync", Tick(), every=timedelta(minutes=5))
    assert await sched.tick() == 0
    clock.advance(minutes=4, seconds=59)
    assert await sched.tick() == 0
    clock.advance(seconds=1)
    assert await sched.tick() == 1 and await job_count(raw) == 1
    assert await sched.tick() == 0  # same instant: nothing more
    clock.advance(minutes=5)
    assert await sched.tick() == 1 and await job_count(raw) == 2


async def test_cron_fires_at_the_expected_times(
    sched: Scheduler, raw: Database, clock: FakeClock
) -> None:
    clock.set(datetime(2026, 3, 1, 2, 59, tzinfo=UTC))
    await sched.add("nightly", "sync", Tick(), cron="0 3 * * *")
    assert await sched.next_run("nightly") == datetime(2026, 3, 1, 3, 0, tzinfo=UTC)
    clock.set(datetime(2026, 3, 1, 3, 0, tzinfo=UTC))
    assert await sched.tick() == 1
    assert await sched.next_run("nightly") == datetime(2026, 3, 2, 3, 0, tzinfo=UTC)
    clock.set(datetime(2026, 3, 1, 23, 0, tzinfo=UTC))
    assert await sched.tick() == 0 and await job_count(raw) == 1


async def test_missed_runs_collapse_into_one_and_keep_the_rhythm(
    sched: Scheduler, raw: Database, clock: FakeClock
) -> None:
    await sched.add("sync", "sync", Tick(), every=timedelta(minutes=10))
    start = clock.now()
    clock.advance(hours=3, minutes=3)  # the process was down for 18 periods
    assert await sched.tick() == 1 and await job_count(raw) == 1
    assert await sched.next_run("sync") == start + timedelta(hours=3, minutes=10)  # phase kept


async def test_restart_does_not_duplicate_or_skip(raw: Database, clock: FakeClock) -> None:
    def build() -> tuple[JobQueue, Scheduler]:
        q = JobQueue(raw, clock, settings=JobsSettings(lease_seconds=60))
        return q, Scheduler(raw, q, clock)

    _, s1 = build()
    await s1.add("sync", "sync", Tick(), every=timedelta(minutes=5))
    clock.advance(minutes=5)
    assert await s1.tick() == 1

    _, s2 = build()  # "restart": new objects over the same database
    await s2.add("sync", "sync", Tick(), every=timedelta(minutes=5))  # re-registered at start-up
    assert await s2.tick() == 0  # already fired; not fired again
    clock.advance(minutes=5)
    assert await s2.tick() == 1
    assert await job_count(raw) == 2


async def test_crash_between_enqueue_and_advance_cannot_double_fire(
    sched: Scheduler, raw: Database, clock: FakeClock
) -> None:
    await sched.add("sync", "sync", Tick(), every=timedelta(minutes=5))
    slot = await sched.next_run("sync")
    assert slot is not None
    clock.advance(minutes=5)
    await sched.tick()
    # Rewind next_run as if the advance had not been saved: the slot's dedupe key still holds.
    await raw.execute(
        "UPDATE schedules SET next_run = ?", (slot.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),)
    )
    await sched.tick()
    assert await job_count(raw) == 1


async def test_readding_unchanged_keeps_next_run_and_changed_recomputes(
    sched: Scheduler, clock: FakeClock
) -> None:
    await sched.add("sync", "sync", Tick(), every=timedelta(minutes=5))
    first = await sched.next_run("sync")
    clock.advance(minutes=2)
    await sched.add("sync", "sync", Tick(), every=timedelta(minutes=5))
    assert await sched.next_run("sync") == first
    await sched.add("sync", "sync", Tick(), every=timedelta(minutes=30))
    assert await sched.next_run("sync") == clock.now() + timedelta(minutes=30)


async def test_payload_priority_and_account_reach_the_job(
    sched: Scheduler, queue: JobQueue, raw: Database, clock: FakeClock
) -> None:
    from oaken.core.ids import AccountId

    await sched.add(
        "s",
        "sync",
        Tick(label="hello"),
        every=timedelta(minutes=1),
        priority=77,
        account_id=AccountId("acct"),
    )
    clock.advance(minutes=1)
    await sched.tick()
    row = await raw.fetchone("SELECT id FROM jobs")
    assert row is not None
    job = await queue.get(row["id"])
    assert job is not None
    assert job.payload_json == '{"label":"hello"}' and job.priority == 77
    assert job.account_id == "acct"


async def test_scheduled_jobs_run_through_the_runner(
    sched: Scheduler, queue: JobQueue, clock: FakeClock
) -> None:
    ran: list[str] = []

    async def handler(p: Tick, _job: Job) -> None:
        ran.append(p.label)

    runner = JobRunner(queue)
    runner.register("sync", Tick, handler)
    await sched.add("s", "sync", Tick(label="x"), every=timedelta(minutes=1))
    clock.advance(minutes=1)
    await sched.tick()
    await runner.drain()
    assert ran == ["x"]


async def test_remove(sched: Scheduler, clock: FakeClock) -> None:
    await sched.add("s", "sync", Tick(), every=timedelta(minutes=1))
    assert await sched.remove("s") and not await sched.remove("s")
    clock.advance(minutes=5)
    assert await sched.tick() == 0


async def test_add_validates_its_arguments(sched: Scheduler) -> None:
    with pytest.raises(ConfigError, match="exactly one"):
        await sched.add("s", "k", Tick())
    with pytest.raises(ConfigError, match="exactly one"):
        await sched.add("s", "k", Tick(), every=timedelta(1), cron="* * * * *")
    with pytest.raises(ConfigError, match="positive"):
        await sched.add("s", "k", Tick(), every=timedelta(0))
    with pytest.raises(ConfigError):
        await sched.add("s", "k", Tick(), cron="nonsense")
    assert await sched.next_run("s") is None


async def test_background_loop_fires_due_schedules(
    sched: Scheduler, raw: Database, clock: FakeClock
) -> None:
    await sched.add("s", "sync", Tick(), every=timedelta(minutes=1))
    clock.advance(minutes=1)
    sched.start()
    for _ in range(200):  # yield to the loop until the first tick has run
        if await job_count(raw):
            break
        await asyncio.sleep(0.01)
    await sched.stop()
    assert await job_count(raw) == 1
    with pytest.raises(RuntimeError):
        sched.start()
        sched.start()
    await sched.stop()
