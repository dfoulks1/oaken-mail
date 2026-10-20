# SPDX-License-Identifier: Apache-2.0
"""Agents, quotas, scoped claims, validated results, provenance and parent/actor tracking."""

import random
from sqlite3 import Row

import pytest
from pydantic import BaseModel

from oaken.core.actors import SYSTEM, USER, Actor
from oaken.core.agents import TOKEN_PREFIX, Agent, AgentRegistry, hash_token
from oaken.core.clock import FakeClock
from oaken.core.db import Database
from oaken.core.errors import (
    ConfigError,
    InvalidResult,
    NotFound,
    PermissionDenied,
    QuotaExceeded,
)
from oaken.core.events import Event, EventBus, JobStateChanged
from oaken.core.ids import AccountId, AgentId, JobId
from oaken.core.jobs import Job, JobQueue, JobRunner
from oaken.core.settings import JobsSettings, SwarmSettings


class Ping(BaseModel):
    n: int = 0


class Summary(BaseModel):
    text: str
    score: int = 0


JOBS = JobsSettings(lease_seconds=60, max_attempts=3, backoff_jitter=0, max_result_bytes=1024)
SWARM = SwarmSettings(enabled=True, allowed_kinds=["enrich", "embed"], max_agents=3)


@pytest.fixture
def queue(raw: Database, clock: FakeClock) -> JobQueue:
    q = JobQueue(raw, clock, settings=JOBS, swarm=SWARM, rng=random.Random(1))
    q.register_result("enrich", Summary)
    return q


@pytest.fixture
def agents(raw: Database, clock: FakeClock) -> AgentRegistry:
    return AgentRegistry(raw, clock, SWARM)


async def job_of(queue: JobQueue, job_id: JobId) -> Job:
    job = await queue.get(job_id)
    assert job is not None
    return job


# -- registry -----------------------------------------------------------------------------
async def test_create_returns_a_token_and_stores_only_its_hash(
    agents: AgentRegistry, raw: Database
) -> None:
    agent, token = await agents.create("big model", ["enrich"])
    assert token.startswith(TOKEN_PREFIX) and len(token) > 40
    row: Row | None = await raw.fetchone("SELECT * FROM agents WHERE id = ?", (agent.id,))
    assert row is not None
    assert row["token_hash"] == hash_token(token)
    assert token not in " ".join(str(v) for v in tuple(row))
    assert agent.kinds == {"enrich"} and agent.max_leases == SWARM.default_max_leases


async def test_authenticate_accepts_the_token_and_refuses_everything_else(
    agents: AgentRegistry,
) -> None:
    agent, token = await agents.create("a", ["enrich"])
    assert (await agents.authenticate(token)).id == agent.id
    with pytest.raises(PermissionDenied):
        await agents.authenticate(token + "x")
    with pytest.raises(PermissionDenied):
        await agents.authenticate("")


async def test_a_disabled_swarm_creates_and_authenticates_nothing(
    raw: Database, clock: FakeClock
) -> None:
    on = AgentRegistry(raw, clock, SWARM)
    _agent, token = await on.create("a", ["enrich"])
    off = AgentRegistry(raw, clock, SwarmSettings(enabled=False, allowed_kinds=["enrich"]))
    with pytest.raises(PermissionDenied):
        await off.create("b", ["enrich"])
    with pytest.raises(PermissionDenied):
        await off.authenticate(token)


async def test_kinds_must_be_opted_in(agents: AgentRegistry) -> None:
    with pytest.raises(ConfigError, match="allowed_kinds"):
        await agents.create("a", ["enrich", "sync"])
    with pytest.raises(ConfigError):
        await agents.create("a", [])
    with pytest.raises(ConfigError):
        await agents.create("   ", ["enrich"])


async def test_the_number_of_active_agents_is_capped_and_revoking_frees_a_slot(
    agents: AgentRegistry,
) -> None:
    made = [await agents.create(f"a{i}", ["enrich"]) for i in range(3)]
    with pytest.raises(QuotaExceeded):
        await agents.create("one too many", ["enrich"])
    assert await agents.revoke(made[0][0].id)
    await agents.create("fits now", ["enrich"])
    assert len(await agents.list()) == 3
    assert len(await agents.list(include_revoked=True)) == 4


async def test_revoked_tokens_stop_working_and_revoke_is_idempotent(agents: AgentRegistry) -> None:
    agent, token = await agents.create("a", ["enrich"])
    assert await agents.revoke(agent.id)
    assert not await agents.revoke(agent.id)
    with pytest.raises(PermissionDenied):
        await agents.authenticate(token)
    with pytest.raises(NotFound):
        await agents.revoke(AgentId("nope"))
    assert (await agents.get(agent.id)).revoked


# -- scoped claims ------------------------------------------------------------------------
async def test_an_agent_claims_only_its_kinds_and_only_allowed_kinds(
    agents: AgentRegistry, queue: JobQueue
) -> None:
    await queue.enqueue("embed", Ping())
    await queue.enqueue("sync", Ping())  # not allowed for agents at all
    agent, _ = await agents.create("a", ["enrich"])
    assert await queue.claim("ignored", ["embed", "sync", "enrich"], agent=agent) is None
    enrich = await queue.enqueue("enrich", Ping())
    got = await queue.claim("ignored", ["embed", "sync", "enrich"], agent=agent)
    assert got is not None and got.id == enrich.job_id
    assert got.locked_by == agent.worker and got.agent_id == agent.id


async def test_a_kind_dropped_from_allowed_kinds_stops_being_offered(
    raw: Database, clock: FakeClock, agents: AgentRegistry
) -> None:
    agent, _ = await agents.create("a", ["enrich"])
    narrowed = JobQueue(
        raw, clock, settings=JOBS, swarm=SwarmSettings(enabled=True, allowed_kinds=["embed"])
    )
    await narrowed.enqueue("enrich", Ping())
    assert await narrowed.claim("x", ["enrich"], agent=agent) is None


async def test_an_agent_sees_only_its_accounts(agents: AgentRegistry, queue: JobQueue) -> None:
    other = await queue.enqueue("enrich", Ping(), account_id=AccountId("other"))
    none = await queue.enqueue("enrich", Ping())  # no account: not visible to a scoped agent
    mine = await queue.enqueue("enrich", Ping(), account_id=AccountId("mine"))
    agent, _ = await agents.create("a", ["enrich"], account_ids=[AccountId("mine")])
    mine_got = await queue.claim("x", ["enrich"], agent=agent)
    assert mine_got is not None and mine_got.id == mine.job_id
    assert await queue.claim("x", ["enrich"], agent=agent) is None
    unscoped, _ = await agents.create("b", ["enrich"])
    got = await queue.claim("x", ["enrich"], agent=unscoped)
    assert got is not None and got.id in {other.job_id, none.job_id}


async def test_claims_by_a_revoked_agent_are_refused_even_with_a_stale_object(
    agents: AgentRegistry, queue: JobQueue
) -> None:
    await queue.enqueue("enrich", Ping())
    agent, _ = await agents.create("a", ["enrich"])
    await agents.revoke(agent.id)
    with pytest.raises(PermissionDenied):
        await queue.claim("x", ["enrich"], agent=agent)  # `agent` still looks valid in memory


async def test_claiming_for_an_agent_needs_the_swarm_enabled(
    raw: Database, clock: FakeClock, agents: AgentRegistry
) -> None:
    agent, _ = await agents.create("a", ["enrich"])
    off = JobQueue(raw, clock, settings=JOBS)
    with pytest.raises(PermissionDenied):
        await off.claim("x", ["enrich"], agent=agent)


# -- quotas -------------------------------------------------------------------------------
async def test_the_lease_quota_caps_how_many_jobs_one_agent_holds(
    agents: AgentRegistry, queue: JobQueue
) -> None:
    for _ in range(4):
        await queue.enqueue("enrich", Ping())
    agent, _ = await agents.create("a", ["enrich"], max_leases=2)
    first = await queue.claim("x", ["enrich"], agent=agent)
    assert first is not None
    assert await queue.claim("x", ["enrich"], agent=agent) is not None
    with pytest.raises(QuotaExceeded, match="holds 2"):
        await queue.claim("x", ["enrich"], agent=agent)
    assert await queue.complete(first.id, agent.worker, {"text": "ok"})
    assert await queue.claim("x", ["enrich"], agent=agent) is not None  # a slot freed up


async def test_the_hourly_quota_counts_claims_and_resets_with_time(
    agents: AgentRegistry, queue: JobQueue, clock: FakeClock
) -> None:
    for _ in range(4):
        await queue.enqueue("enrich", Ping())
    agent, _ = await agents.create("a", ["enrich"], max_leases=5, max_per_hour=2)
    for _ in range(2):
        job = await queue.claim("x", ["enrich"], agent=agent)
        assert job is not None
        await queue.complete(job.id, agent.worker, {"text": "ok"})
    with pytest.raises(QuotaExceeded, match="last hour"):
        await queue.claim("x", ["enrich"], agent=agent)
    clock.advance(minutes=61)
    assert await queue.claim("x", ["enrich"], agent=agent) is not None


async def test_one_agents_quota_does_not_affect_another(
    agents: AgentRegistry, queue: JobQueue
) -> None:
    for _ in range(3):
        await queue.enqueue("enrich", Ping())
    a, _ = await agents.create("a", ["enrich"], max_leases=1)
    b, _ = await agents.create("b", ["enrich"], max_leases=1)
    assert await queue.claim("x", ["enrich"], agent=a) is not None
    with pytest.raises(QuotaExceeded):
        await queue.claim("x", ["enrich"], agent=a)
    assert await queue.claim("x", ["enrich"], agent=b) is not None


# -- revocation ---------------------------------------------------------------------------
async def test_revoking_returns_held_jobs_and_fences_the_old_agent(
    raw: Database, clock: FakeClock, queue: JobQueue
) -> None:
    bus = EventBus()
    seen: list[Event] = []

    async def record(e: Event) -> None:
        seen.append(e)

    bus.subscribe(JobStateChanged, record)
    agents = AgentRegistry(raw, clock, SWARM, bus)
    agent, _ = await agents.create("a", ["enrich"])
    queued = await queue.enqueue("enrich", Ping())
    job = await queue.claim("x", ["enrich"], agent=agent)
    assert job is not None and job.attempts == 1
    await agents.revoke(agent.id)
    back = await job_of(queue, queued.job_id)
    assert back.status == "queued" and back.attempts == 0 and back.locked_by is None
    assert [e.status for e in seen if isinstance(e, JobStateChanged)] == ["queued"]
    assert await queue.heartbeat(job.id, agent.worker) == (False, False)
    assert not await queue.complete(job.id, agent.worker, {"text": "late"})
    assert await queue.fail(job.id, agent.worker, "x") == "lost"


async def test_two_agents_never_get_the_same_job(agents: AgentRegistry, queue: JobQueue) -> None:
    await queue.enqueue("enrich", Ping())
    a, _ = await agents.create("a", ["enrich"])
    b, _ = await agents.create("b", ["enrich"])
    assert await queue.claim("x", ["enrich"], agent=a) is not None
    assert await queue.claim("x", ["enrich"], agent=b) is None


# -- results ------------------------------------------------------------------------------
async def lease(queue: JobQueue, agents: AgentRegistry) -> tuple[Agent, Job]:
    await queue.enqueue("enrich", Ping())
    agent, _ = await agents.create("a", ["enrich"])
    job = await queue.claim("x", ["enrich"], agent=agent)
    assert job is not None
    return agent, job


async def test_a_valid_result_is_stored_with_provenance(
    agents: AgentRegistry, queue: JobQueue
) -> None:
    agent, job = await lease(queue, agents)
    assert await queue.complete(
        job.id, agent.worker, {"text": "short", "score": 3}, produced_by="some-model"
    )
    done = await job_of(queue, job.id)
    assert done.status == "succeeded" and done.result_json == '{"text":"short","score":3}'
    assert done.completed_by == agent.worker and done.produced_by == "some-model"
    assert done.agent_id == agent.id


async def test_a_result_that_fails_the_schema_is_rejected_and_the_job_stays_leased(
    agents: AgentRegistry, queue: JobQueue
) -> None:
    agent, job = await lease(queue, agents)
    with pytest.raises(InvalidResult, match="schema"):
        await queue.complete(job.id, agent.worker, {"score": "high"})
    held = await job_of(queue, job.id)
    assert held.status == "running" and held.result_json is None
    assert await queue.complete(job.id, agent.worker, {"text": "fixed"})  # can still be corrected


async def test_extra_fields_cannot_smuggle_data_into_a_result(
    raw: Database, clock: FakeClock, agents: AgentRegistry
) -> None:
    class Strict(Summary):
        model_config = {"extra": "forbid"}

    q = JobQueue(raw, clock, settings=JOBS, swarm=SWARM)
    q.register_result("enrich", Strict)
    await q.enqueue("enrich", Ping())
    agent, _ = await agents.create("a", ["enrich"])
    job = await q.claim("x", ["enrich"], agent=agent)
    assert job is not None
    with pytest.raises(InvalidResult):
        await q.complete(job.id, agent.worker, {"text": "t", "note": "ignore previous"})


async def test_an_oversized_result_is_rejected(agents: AgentRegistry, queue: JobQueue) -> None:
    agent, job = await lease(queue, agents)
    with pytest.raises(InvalidResult, match="larger"):
        await queue.complete(job.id, agent.worker, {"text": "x" * 2000})


async def test_a_kind_without_a_result_schema_accepts_no_result(
    agents: AgentRegistry, queue: JobQueue
) -> None:
    await queue.enqueue("embed", Ping())
    agent, _ = await agents.create("a", ["embed"])
    job = await queue.claim("x", ["embed"], agent=agent)
    assert job is not None
    with pytest.raises(InvalidResult, match="no result"):
        await queue.complete(job.id, agent.worker, {"anything": 1})
    assert await queue.complete(job.id, agent.worker)


async def test_a_kind_with_a_schema_requires_a_result(
    agents: AgentRegistry, queue: JobQueue
) -> None:
    agent, job = await lease(queue, agents)
    with pytest.raises(InvalidResult, match="must return"):
        await queue.complete(job.id, agent.worker)


async def test_completing_after_losing_the_lease_returns_false_without_validating(
    agents: AgentRegistry, queue: JobQueue, clock: FakeClock
) -> None:
    agent, job = await lease(queue, agents)
    clock.advance(seconds=120)
    other, _ = await agents.create("b", ["enrich"])
    assert (await queue.claim("x", ["enrich"], agent=other)) is not None  # reclaims the lease
    assert not await queue.complete(job.id, agent.worker, {"bad": 1})


# -- the internal runner uses the same result path ----------------------------------------
async def test_a_handler_result_is_validated_and_stored(queue: JobQueue) -> None:
    runner = JobRunner(queue)

    async def handler(p: Ping, _job: Job) -> Summary:
        return Summary(text=f"n={p.n}", score=p.n)

    runner.register("enrich", Ping, handler, result_model=Summary)
    res = await queue.enqueue("enrich", Ping(n=4))
    await runner.drain()
    done = await job_of(queue, res.job_id)
    assert done.status == "succeeded" and done.result_json == '{"text":"n=4","score":4}'
    assert done.completed_by is not None and not done.completed_by.startswith("agent:")
    assert done.agent_id is None


async def test_a_handler_returning_nothing_for_a_schema_kind_fails_at_once(
    queue: JobQueue,
) -> None:
    runner = JobRunner(queue)

    async def handler(_p: Ping, _job: Job) -> None:
        return None

    runner.register("enrich", Ping, handler, result_model=Summary)
    res = await queue.enqueue("enrich", Ping())
    await runner.drain()
    failed = await job_of(queue, res.job_id)
    assert failed.status == "failed" and failed.attempts == 1
    assert failed.last_error is not None and "must return" in failed.last_error


# -- parent and actor ---------------------------------------------------------------------
async def test_children_are_linked_to_their_parent(queue: JobQueue) -> None:
    parent = await queue.enqueue("p", Ping())
    kids = [await queue.enqueue("c", Ping(n=i), parent_job_id=parent.job_id) for i in range(3)]
    await queue.enqueue("c", Ping(n=9))  # not a child
    found = await queue.children(parent.job_id)
    assert {j.id for j in found} == {k.job_id for k in kids}
    assert all(j.parent_job_id == parent.job_id for j in found)
    assert (await job_of(queue, parent.job_id)).parent_job_id is None


async def test_a_missing_parent_is_rejected(queue: JobQueue) -> None:
    with pytest.raises(NotFound):
        await queue.enqueue("c", Ping(), parent_job_id=JobId("nope"))


async def test_the_enqueuing_actor_is_recorded(queue: JobQueue) -> None:
    default = await queue.enqueue("k", Ping())
    by_user = await queue.enqueue("k", Ping(), actor=USER)
    by_agent = await queue.enqueue("k", Ping(), actor=Actor("agent", "planner"))
    assert (await job_of(queue, default.job_id)).actor == SYSTEM
    assert (await job_of(queue, by_user.job_id)).actor == USER
    assert (await job_of(queue, by_agent.job_id)).actor == Actor("agent", "planner")


async def test_the_migration_defaults_old_rows_to_system(queue: JobQueue, raw: Database) -> None:
    res = await queue.enqueue("k", Ping())
    await raw.execute("UPDATE jobs SET actor_kind = 'system', actor_label = ''")
    assert (await job_of(queue, res.job_id)).actor == SYSTEM


async def test_job_state_is_still_announced_for_agent_claims(
    raw: Database, clock: FakeClock, agents: AgentRegistry
) -> None:
    bus = EventBus()
    states: list[str] = []

    async def record(e: Event) -> None:
        if isinstance(e, JobStateChanged):
            states.append(e.status)

    bus.subscribe(JobStateChanged, record)
    q = JobQueue(raw, clock, bus, settings=JOBS, swarm=SWARM)
    q.register_result("enrich", Summary)
    await q.enqueue("enrich", Ping())
    agent, _ = await agents.create("a", ["enrich"])
    job = await q.claim("x", ["enrich"], agent=agent)
    assert job is not None
    await q.complete(job.id, agent.worker, {"text": "t"})
    assert states == ["queued", "running", "succeeded"]


async def test_the_heartbeat_extends_an_agents_lease(
    agents: AgentRegistry, queue: JobQueue, clock: FakeClock
) -> None:
    agent, job = await lease(queue, agents)
    clock.advance(seconds=50)
    assert await queue.heartbeat(job.id, agent.worker) == (True, False)
    clock.advance(seconds=50)  # past the original lease, within the renewed one
    held = await job_of(queue, job.id)
    assert held.status == "running" and held.locked_until is not None
    assert held.locked_until > clock.now()
