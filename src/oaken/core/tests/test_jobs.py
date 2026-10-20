# SPDX-License-Identifier: Apache-2.0
import asyncio
import random
from collections.abc import Awaitable, Callable
from datetime import timedelta

import pytest
from pydantic import BaseModel

from oaken.core.clock import FakeClock
from oaken.core.db import Database
from oaken.core.errors import NotFound
from oaken.core.events import Event, EventBus, JobStateChanged
from oaken.core.ids import AccountId, JobId
from oaken.core.jobs import (
    MAX_ERROR_CHARS,
    PRIORITY_BACKFILL,
    PRIORITY_INGEST,
    PRIORITY_USER,
    Job,
    JobQueue,
    JobRunner,
    PermanentJobError,
    scrub_error,
)
from oaken.core.settings import JobsSettings


class Ping(BaseModel):
    n: int = 0


SETTINGS = JobsSettings(
    workers=4, lease_seconds=60, max_attempts=3, backoff_base_seconds=5, backoff_jitter=0
)


@pytest.fixture
def queue(raw: Database, clock: FakeClock) -> JobQueue:
    return JobQueue(raw, clock, settings=SETTINGS, rng=random.Random(1))


async def row(queue: JobQueue, job_id: JobId) -> Job:
    job = await queue.get(job_id)
    assert job is not None
    return job


# -- enqueue ------------------------------------------------------------------------------
async def test_enqueue_stores_a_queued_job(queue: JobQueue) -> None:
    res = await queue.enqueue("k", Ping(n=3), account_id=AccountId("a"), dedupe_key="x")
    job = await row(queue, res.job_id)
    assert res.created and job.status == "queued" and job.attempts == 0
    assert job.payload_json == '{"n":3}' and job.account_id == "a" and job.max_attempts == 3


async def test_same_dedupe_key_runs_once(queue: JobQueue) -> None:
    runner, ran = JobRunner(queue), []

    async def handler(p: Ping, _job: Job) -> None:
        ran.append(p.n)

    runner.register("k", Ping, handler)
    first = await queue.enqueue("k", Ping(n=1), dedupe_key="same")
    second = await queue.enqueue("k", Ping(n=2), dedupe_key="same")
    assert first.created and not second.created and first.job_id == second.job_id
    await runner.drain()
    assert ran == [1]


async def test_dedupe_key_is_released_when_the_job_finishes(queue: JobQueue) -> None:
    runner = JobRunner(queue)

    async def handler(_p: Ping, _job: Job) -> None:
        return None

    runner.register("k", Ping, handler)
    first = await queue.enqueue("k", Ping(), dedupe_key="same")
    await runner.drain()
    again = await queue.enqueue("k", Ping(), dedupe_key="same")
    assert again.created and again.job_id != first.job_id


async def test_run_after_delays_a_job(queue: JobQueue, clock: FakeClock) -> None:
    runner, ran = JobRunner(queue), []

    async def handler(p: Ping, _job: Job) -> None:
        ran.append(p.n)

    runner.register("k", Ping, handler)
    await queue.enqueue("k", Ping(), run_after=clock.now() + timedelta(minutes=10))
    assert await runner.drain() == 0
    clock.advance(minutes=10)
    assert await runner.drain() == 1


# -- retries and failure -------------------------------------------------------------------
async def test_failing_handler_backs_off_then_fails_for_good(
    queue: JobQueue, clock: FakeClock
) -> None:
    runner = JobRunner(queue)

    async def handler(_p: Ping, _job: Job) -> None:
        raise RuntimeError("nope")

    runner.register("k", Ping, handler)
    jid = (await queue.enqueue("k", Ping())).job_id
    t0 = clock.now()

    assert await runner.drain() == 1
    job = await row(queue, jid)
    assert (job.status, job.attempts, job.run_after) == ("queued", 1, t0 + timedelta(seconds=5))

    assert await runner.drain() == 0  # still backing off
    clock.advance(seconds=5)
    assert await runner.drain() == 1
    job = await row(queue, jid)
    assert (job.status, job.attempts) == ("queued", 2)
    assert job.run_after == clock.now() + timedelta(seconds=10)  # doubled

    clock.advance(seconds=10)
    assert await runner.drain() == 1
    job = await row(queue, jid)
    assert (job.status, job.attempts) == ("failed", 3)
    assert job.last_error == "RuntimeError: nope"
    clock.advance(hours=5)
    assert await runner.drain() == 0  # terminal


def test_backoff_is_capped_and_jittered(queue: JobQueue) -> None:
    s = JobsSettings(backoff_base_seconds=10, backoff_max_seconds=60, backoff_jitter=0.5)
    q = JobQueue(queue.db, queue.clock, settings=s, rng=random.Random(7))
    delays = [q.backoff(n).total_seconds() for n in range(1, 8)]
    assert all(0 < d <= 60 for d in delays)
    assert delays[-1] >= 30  # the cap (60) minus at most 50% jitter
    assert q.backoff(1).total_seconds() < 10  # jitter only ever shortens the delay


async def test_permanent_error_skips_retries(queue: JobQueue) -> None:
    runner = JobRunner(queue)

    async def handler(_p: Ping, _job: Job) -> None:
        raise PermanentJobError("bad account")

    runner.register("k", Ping, handler)
    jid = (await queue.enqueue("k", Ping())).job_id
    await runner.drain()
    job = await row(queue, jid)
    assert (job.status, job.attempts) == ("failed", 1) and "bad account" in (job.last_error or "")


async def test_invalid_payload_fails_without_calling_the_handler(
    queue: JobQueue, raw: Database
) -> None:
    runner, called = JobRunner(queue), []

    async def handler(p: Ping, _job: Job) -> None:
        called.append(p)

    runner.register("k", Ping, handler)
    jid = (await queue.enqueue("k", Ping())).job_id
    await raw.execute("UPDATE jobs SET payload_json = ? WHERE id = ?", ('{"n": "x"}', jid))
    await runner.drain()
    job = await row(queue, jid)
    assert called == [] and job.status == "failed" and "invalid payload" in (job.last_error or "")


async def test_last_error_is_redacted_and_truncated(queue: JobQueue) -> None:
    runner = JobRunner(queue)

    async def handler(_p: Ping, _job: Job) -> None:
        raise RuntimeError("auth failed: access_token=ya29.SECRET " + "x" * 2000)

    runner.register("k", Ping, handler)
    jid = (await queue.enqueue("k", Ping())).job_id
    await runner.drain()
    err = (await row(queue, jid)).last_error or ""
    assert "ya29.SECRET" not in err and "access_token=[REDACTED]" in err
    assert len(err) < MAX_ERROR_CHARS + 60 and "truncated" in err


@pytest.mark.parametrize(
    "text",
    [
        "token=abc123",
        "Authorization: Bearer abc123",
        'bad {"password": "abc123"}',
        "client_secret = abc123",
        "sent Bearer abc123 to server",
        "api-key: abc123",
    ],
)
def test_scrub_error_hides_secrets(text: str) -> None:
    out = scrub_error(text)
    assert "abc123" not in out and "[REDACTED]" in out


def test_scrub_error_formats_exceptions() -> None:
    assert scrub_error(ValueError("plain")) == "ValueError: plain"


# -- leases and crashes --------------------------------------------------------------------
async def test_claimed_but_unfinished_job_reruns_after_lease_expiry(
    queue: JobQueue, clock: FakeClock
) -> None:
    runner, ran = JobRunner(queue), []

    async def handler(p: Ping, _job: Job) -> None:
        ran.append(p.n)

    runner.register("k", Ping, handler)
    jid = (await queue.enqueue("k", Ping(n=7))).job_id
    dead = await queue.claim("dead-worker", ["k"])  # the process "dies" holding this job
    assert dead is not None and dead.attempts == 1

    clock.advance(seconds=59)
    assert await runner.drain() == 0  # lease still valid: nobody else may take it
    clock.advance(seconds=2)
    assert await runner.drain() == 1 and ran == [7]
    job = await row(queue, jid)
    assert (job.status, job.attempts) == ("succeeded", 2)


async def test_lease_expiring_on_the_final_attempt_fails_the_job(
    queue: JobQueue, clock: FakeClock
) -> None:
    jid = (await queue.enqueue("k", Ping(), max_attempts=1)).job_id
    await queue.claim("dead", ["k"])
    clock.advance(seconds=61)
    assert await queue.claim("w", ["k"]) is None
    job = await row(queue, jid)
    assert job.status == "failed" and "lease expired" in (job.last_error or "")


async def test_a_worker_that_lost_its_lease_cannot_finish_the_job(
    queue: JobQueue, clock: FakeClock
) -> None:
    jid = (await queue.enqueue("k", Ping())).job_id
    await queue.claim("slow", ["k"])
    clock.advance(seconds=61)
    taken = await queue.claim("other", ["k"])
    assert taken is not None and taken.id == jid
    assert not await queue.complete(jid, "slow")
    assert await queue.fail(jid, "slow", "late") == "lost"
    assert await queue.complete(jid, "other")


async def test_heartbeat_extends_the_lease(queue: JobQueue, clock: FakeClock) -> None:
    jid = (await queue.enqueue("k", Ping())).job_id
    await queue.claim("w", ["k"])
    clock.advance(seconds=50)
    assert await queue.heartbeat(jid, "w") == (True, False)
    clock.advance(seconds=50)  # 100s since claim: would have expired without the heartbeat
    assert await queue.claim("x", ["k"]) is None
    assert await queue.heartbeat(jid, "intruder") == (False, False)


async def test_runner_heartbeats_while_a_handler_runs(queue: JobQueue, clock: FakeClock) -> None:
    tick, waiting, started, release = (asyncio.Event() for _ in range(4))

    async def fake_sleep(_seconds: float) -> None:
        waiting.set()
        await tick.wait()
        tick.clear()

    async def handler(_p: Ping, _job: Job) -> None:
        started.set()
        await release.wait()

    runner = JobRunner(queue, workers=1, sleep=fake_sleep)
    runner.register("k", Ping, handler)
    jid = (await queue.enqueue("k", Ping())).job_id
    runner.start()
    await started.wait()
    await waiting.wait()
    waiting.clear()
    before = (await row(queue, jid)).locked_until
    clock.advance(seconds=40)
    tick.set()
    await waiting.wait()  # the heartbeat ran and the loop is sleeping again
    after = (await row(queue, jid)).locked_until
    assert before is not None and after is not None and after == before + timedelta(seconds=40)
    release.set()
    await runner.stop()
    assert (await row(queue, jid)).status == "succeeded"


# -- priority and concurrency --------------------------------------------------------------
async def test_priority_then_fifo(queue: JobQueue, clock: FakeClock) -> None:
    order: list[str] = []

    async def handler(p: Ping, job: Job) -> None:
        order.append(f"{job.priority}:{p.n}")

    runner = JobRunner(queue, workers=1)
    runner.register("k", Ping, handler)
    await queue.enqueue("k", Ping(n=1), priority=PRIORITY_BACKFILL)
    await queue.enqueue("k", Ping(n=2), priority=PRIORITY_INGEST)
    clock.advance(seconds=1)
    await queue.enqueue("k", Ping(n=3), priority=PRIORITY_INGEST)
    await queue.enqueue("k", Ping(n=4), priority=PRIORITY_USER)
    await runner.drain()
    assert order == ["100:4", "50:2", "50:3", "10:1"]


async def test_per_kind_concurrency_limit_is_honored(queue: JobQueue) -> None:
    active = {"slow": 0, "fast": 0}
    peak = {"slow": 0, "fast": 0}
    two_running, release = asyncio.Event(), asyncio.Event()

    def make(kind: str) -> Callable[[Ping, Job], Awaitable[None]]:
        async def handler(_p: Ping, _job: Job) -> None:
            active[kind] += 1
            peak[kind] = max(peak[kind], active[kind])
            if kind == "slow" and active[kind] == 2:
                two_running.set()
            if kind == "slow":
                await release.wait()
            active[kind] -= 1

        return handler

    runner = JobRunner(queue, workers=4)
    runner.register("slow", Ping, make("slow"), concurrency=2)
    runner.register("fast", Ping, make("fast"))
    for _ in range(5):
        await queue.enqueue("slow", Ping())
    await queue.enqueue("fast", Ping())

    drain = asyncio.create_task(runner.drain())
    await two_running.wait()
    # Two slow jobs hold the limit: a third slow claim is refused, other kinds still flow.
    assert await queue.claim("probe", ["slow"], {"slow": 2}) is None
    release.set()
    assert await drain == 6
    assert peak["slow"] == 2 and peak["fast"] == 1


async def test_config_kind_limits_override_the_registration(queue: JobQueue, raw: Database) -> None:
    settings = JobsSettings(lease_seconds=60, kind_limits={"k": 1})
    q = JobQueue(raw, queue.clock, settings=settings)
    runner = JobRunner(q, workers=3)
    started, release = asyncio.Event(), asyncio.Event()
    running = 0
    peak = 0

    async def handler(_p: Ping, _job: Job) -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        started.set()
        await release.wait()
        running -= 1

    runner.register("k", Ping, handler, concurrency=3)  # config says 1
    for _ in range(3):
        await q.enqueue("k", Ping())
    drain = asyncio.create_task(runner.drain())
    await started.wait()
    release.set()
    await drain
    assert peak == 1


async def test_runner_only_claims_registered_kinds(queue: JobQueue) -> None:
    runner = JobRunner(queue)
    jid = (await queue.enqueue("unknown", Ping())).job_id
    assert await runner.drain() == 0
    assert (await row(queue, jid)).status == "queued"


def test_duplicate_registration_is_rejected(queue: JobQueue) -> None:
    runner = JobRunner(queue)

    async def handler(_p: Ping, _job: Job) -> None:
        return None

    runner.register("k", Ping, handler)
    with pytest.raises(ValueError, match="already registered"):
        runner.register("k", Ping, handler)


# -- cancellation --------------------------------------------------------------------------
async def test_cancel_a_queued_job(queue: JobQueue) -> None:
    runner = JobRunner(queue)

    async def handler(_p: Ping, _job: Job) -> None:
        raise AssertionError("must not run")

    runner.register("k", Ping, handler)
    jid = (await queue.enqueue("k", Ping())).job_id
    assert await runner.cancel(jid) == "cancelled"
    assert await runner.drain() == 0
    assert await queue.cancel(jid) == "cancelled"  # idempotent on finished jobs


async def test_cancel_unknown_job(queue: JobQueue) -> None:
    with pytest.raises(NotFound):
        await queue.cancel(JobId("nope"))


async def test_cancel_a_running_job_interrupts_its_handler(queue: JobQueue) -> None:
    started, interrupted = asyncio.Event(), asyncio.Event()

    async def handler(_p: Ping, _job: Job) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            interrupted.set()
            raise

    runner = JobRunner(queue, workers=1)
    runner.register("k", Ping, handler)
    jid = (await queue.enqueue("k", Ping())).job_id
    runner.start()
    await started.wait()
    assert await runner.cancel(jid) == "running"  # the worker acts on it right away
    await interrupted.wait()
    await runner.stop()
    assert (await row(queue, jid)).status == "cancelled"


async def test_cancel_request_is_noticed_at_the_next_heartbeat(queue: JobQueue) -> None:
    tick, waiting, started = (asyncio.Event() for _ in range(3))

    async def fake_sleep(_s: float) -> None:
        waiting.set()
        await tick.wait()
        tick.clear()

    async def handler(_p: Ping, _job: Job) -> None:
        started.set()
        await asyncio.Event().wait()

    runner = JobRunner(queue, workers=1, sleep=fake_sleep)
    runner.register("k", Ping, handler)
    jid = (await queue.enqueue("k", Ping())).job_id
    runner.start()
    await started.wait()
    await waiting.wait()
    await queue.cancel(jid)  # as another process would: only sets the request flag
    tick.set()
    await runner.stop(grace=5)
    assert (await row(queue, jid)).status == "cancelled"


# -- shutdown ------------------------------------------------------------------------------
async def test_stop_lets_an_in_flight_job_finish(queue: JobQueue) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    async def handler(_p: Ping, _job: Job) -> None:
        started.set()
        await release.wait()

    runner = JobRunner(queue, workers=1)
    runner.register("k", Ping, handler)
    jid = (await queue.enqueue("k", Ping())).job_id
    runner.start()
    await started.wait()
    stopping = asyncio.create_task(runner.stop(grace=30))
    release.set()
    await stopping
    assert (await row(queue, jid)).status == "succeeded"


async def test_stop_releases_the_lease_of_a_job_that_will_not_finish(queue: JobQueue) -> None:
    started = asyncio.Event()

    async def handler(_p: Ping, _job: Job) -> None:
        started.set()
        await asyncio.Event().wait()

    runner = JobRunner(queue, workers=1)
    runner.register("k", Ping, handler)
    jid = (await queue.enqueue("k", Ping())).job_id
    runner.start()
    await started.wait()
    await runner.stop(grace=0)
    job = await row(queue, jid)
    assert (job.status, job.attempts, job.locked_by) == ("queued", 0, None)


async def test_background_workers_wake_on_enqueue(queue: JobQueue) -> None:
    done = asyncio.Event()

    async def handler(_p: Ping, _job: Job) -> None:
        done.set()

    runner = JobRunner(queue, workers=2)
    runner.register("k", Ping, handler)
    runner.start()
    await queue.enqueue("k", Ping())
    await asyncio.wait_for(done.wait(), timeout=5)
    await runner.stop()
    with pytest.raises(RuntimeError):
        runner.start()
        runner.start()
    await runner.stop(grace=0)


# -- events --------------------------------------------------------------------------------
async def test_state_changes_are_published(raw: Database, clock: FakeClock) -> None:
    bus = EventBus()
    q = JobQueue(raw, clock, bus, settings=SETTINGS)
    seen: list[str] = []

    async def on_job(e: Event) -> None:
        assert isinstance(e, JobStateChanged)
        seen.append(e.status)

    bus.subscribe(JobStateChanged, on_job)
    runner = JobRunner(q)
    flaky = {"left": 1}

    async def handler(_p: Ping, _job: Job) -> None:
        if flaky["left"]:
            flaky["left"] -= 1
            raise RuntimeError("once")

    runner.register("k", Ping, handler)
    await q.enqueue("k", Ping())
    await runner.drain()
    clock.advance(seconds=5)
    await runner.drain()
    assert seen == ["queued", "running", "queued", "running", "succeeded"]
