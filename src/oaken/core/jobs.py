# SPDX-License-Identifier: Apache-2.0
"""Durable job queue and the worker runtime (see docs/architecture/jobs.md).

``JobQueue`` is the storage side: every state change is one short transaction in ``raw.db``, so a
crash at any point leaves a job queued, or running with a lease that will expire and be reclaimed.
``JobRunner`` is the execution side: N asyncio workers claim jobs and run the handler registered
for the job's ``kind``.

Delivery is **at least once**: a job whose worker dies is run again after its lease expires, so
handlers must be idempotent. Times are UTC and stored as fixed-width ISO strings.

Retry delay after the Nth failed attempt is ``min(backoff_max, backoff_base * 2**(N-1))``, reduced
by up to ``backoff_jitter`` (a fraction) at random so retries do not arrive in lockstep.
"""

import asyncio
import contextlib
import random
import re
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from sqlite3 import Row
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from oaken.core.actors import SYSTEM, Actor, ActorKind
from oaken.core.agents import Agent
from oaken.core.clock import Clock, fmt_ts, parse_ts
from oaken.core.db import Database, Tx
from oaken.core.errors import (
    InvalidResult,
    NotFound,
    OakenError,
    PermissionDenied,
    QuotaExceeded,
    StorageError,
)
from oaken.core.events import EventBus, JobStateChanged
from oaken.core.ids import AccountId, JobId, new_job_id, uuid7
from oaken.core.logging import get_logger
from oaken.core.settings import JobsSettings, SwarmSettings

log = get_logger("oaken.jobs")

M = TypeVar("M", bound=BaseModel)

PRIORITY_USER = 100  # something the user just asked for
PRIORITY_INGEST = 50
PRIORITY_BACKFILL = 10

MAX_ERROR_CHARS = 500
_SECRET_TEXT = re.compile(
    r"""(?ix)
    \b(?P<name>[\w-]*(?:token|secret|passw(?:or)?d|api[_-]?key|authorization|credential)[\w-]*)
    ["']?\s*[=:]\s*["']?(?:bearer\s+)?[^\s"',}]+
    | \bbearer\s+[\w.~+/=-]+
    """
)


class PermanentJobError(OakenError):
    """Raise from a handler to fail the job at once, without retries."""

    code = "job_permanent_failure"


def scrub_error(error: BaseException | str) -> str:
    """Error text safe to store: secrets redacted, then truncated."""
    text = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else error

    def mask(m: re.Match[str]) -> str:
        name = m.group("name")
        return f"{name}=[REDACTED]" if name else "Bearer [REDACTED]"

    text = _SECRET_TEXT.sub(mask, text)
    if len(text) > MAX_ERROR_CHARS:
        return f"{text[:MAX_ERROR_CHARS]}...[truncated {len(text) - MAX_ERROR_CHARS} chars]"
    return text


@dataclass(frozen=True)
class Job:
    id: JobId
    account_id: AccountId | None
    kind: str
    payload_json: str
    status: str
    priority: int
    attempts: int
    max_attempts: int
    run_after: datetime
    locked_by: str | None
    locked_until: datetime | None
    cancel_requested: bool
    last_error: str | None
    dedupe_key: str | None
    created_at: datetime
    updated_at: datetime
    parent_job_id: JobId | None
    actor: Actor  # who enqueued it
    agent_id: str | None  # the last external agent to lease it
    claimed_at: datetime | None
    result_json: str | None
    completed_by: str | None  # the worker or agent that finished it
    produced_by: str | None  # the model the finisher says produced the result (self-reported)

    @classmethod
    def from_row(cls, r: Row) -> "Job":
        return cls(
            id=JobId(r["id"]),
            account_id=AccountId(r["account_id"]) if r["account_id"] else None,
            kind=r["kind"],
            payload_json=r["payload_json"],
            status=r["status"],
            priority=r["priority"],
            attempts=r["attempts"],
            max_attempts=r["max_attempts"],
            run_after=parse_ts(r["run_after"]),
            locked_by=r["locked_by"],
            locked_until=parse_ts(r["locked_until"]) if r["locked_until"] else None,
            cancel_requested=bool(r["cancel_requested"]),
            last_error=r["last_error"],
            dedupe_key=r["dedupe_key"],
            created_at=parse_ts(r["created_at"]),
            updated_at=parse_ts(r["updated_at"]),
            parent_job_id=JobId(r["parent_job_id"]) if r["parent_job_id"] else None,
            actor=Actor(_actor_kind(r["actor_kind"]), r["actor_label"]),
            agent_id=r["agent_id"],
            claimed_at=parse_ts(r["claimed_at"]) if r["claimed_at"] else None,
            result_json=r["result_json"],
            completed_by=r["completed_by"],
            produced_by=r["produced_by"],
        )


def _actor_kind(value: str) -> ActorKind:
    if value == "user":
        return "user"
    if value == "agent":
        return "agent"
    return "system"


@dataclass(frozen=True)
class Enqueued:
    job_id: JobId
    created: bool  # False: an equal dedupe_key was already queued or running


@dataclass(frozen=True)
class _Change:
    """A state change to announce on the event bus once its transaction has committed."""

    job_id: JobId
    kind: str
    status: str
    account_id: AccountId | None


class JobQueue:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        bus: EventBus | None = None,
        *,
        settings: JobsSettings | None = None,
        swarm: SwarmSettings | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.db = db
        self.clock = clock
        self.bus = bus
        self.settings = settings or JobsSettings()
        self.swarm = swarm or SwarmSettings()
        # Result schema per kind. A job's result is accepted only if its kind has one.
        self.result_models: dict[str, type[BaseModel]] = {}
        self._rng = rng or random.Random()  # noqa: S311 (retry jitter, not security)
        self.wake = asyncio.Event()  # set whenever work may have become runnable

    # -- producing --------------------------------------------------------------------------
    async def enqueue(
        self,
        kind: str,
        payload: BaseModel,
        *,
        priority: int = PRIORITY_INGEST,
        account_id: AccountId | None = None,
        dedupe_key: str | None = None,
        run_after: datetime | None = None,
        max_attempts: int | None = None,
        actor: Actor = SYSTEM,
        parent_job_id: JobId | None = None,
    ) -> Enqueued:
        """Queue a job. With a ``dedupe_key`` that is already queued or running, nothing is
        added and the existing job is returned (``created=False``)."""
        async with self.db.transaction() as tx:
            result, change = await self.enqueue_in(
                tx,
                kind,
                payload,
                priority=priority,
                account_id=account_id,
                dedupe_key=dedupe_key,
                run_after=run_after,
                max_attempts=max_attempts,
                actor=actor,
                parent_job_id=parent_job_id,
            )
        await self.announce(change)
        return result

    async def enqueue_in(
        self,
        tx: Tx,
        kind: str,
        payload: BaseModel | str,
        *,
        priority: int = PRIORITY_INGEST,
        account_id: AccountId | None = None,
        dedupe_key: str | None = None,
        run_after: datetime | None = None,
        max_attempts: int | None = None,
        actor: Actor = SYSTEM,
        parent_job_id: JobId | None = None,
    ) -> tuple[Enqueued, _Change | None]:
        """``enqueue`` inside the caller's transaction (``payload`` may be already-serialised
        JSON). Call ``announce`` after it commits."""
        if dedupe_key is not None:
            row = await tx.fetchone(
                "SELECT id FROM jobs WHERE dedupe_key = ? AND status IN ('queued', 'running')",
                (dedupe_key,),
            )
            if row:
                return Enqueued(JobId(row["id"]), created=False), None
        if parent_job_id is not None and not await tx.fetchone(
            "SELECT 1 FROM jobs WHERE id = ?", (parent_job_id,)
        ):
            raise NotFound(f"no such parent job: {parent_job_id}")
        now = self.clock.now()
        job_id = new_job_id()
        await tx.execute(
            "INSERT INTO jobs (id, account_id, kind, payload_json, priority, max_attempts,"
            " run_after, dedupe_key, created_at, updated_at, parent_job_id, actor_kind,"
            " actor_label) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                account_id,
                kind,
                payload if isinstance(payload, str) else payload.model_dump_json(),
                priority,
                max_attempts or self.settings.max_attempts,
                fmt_ts(run_after or now),
                dedupe_key,
                fmt_ts(now),
                fmt_ts(now),
                parent_job_id,
                actor.kind,
                actor.label,
            ),
        )
        return Enqueued(job_id, created=True), _Change(job_id, kind, "queued", account_id)

    async def announce(self, *changes: _Change | None) -> None:
        self.wake.set()
        if self.bus is None:
            return
        for c in changes:
            if c is not None:
                await self.bus.publish(
                    JobStateChanged(
                        ts=self.clock.now(),
                        job_id=c.job_id,
                        kind=c.kind,
                        status=c.status,
                        account_id=c.account_id,
                    )
                )

    # -- claiming ---------------------------------------------------------------------------
    async def claim(
        self,
        worker: str,
        kinds: Collection[str],
        limits: Mapping[str, int] | None = None,
        *,
        agent: Agent | None = None,
        hidden_accounts: Collection[str] = (),
    ) -> Job | None:
        """Atomically take the best runnable job of one of ``kinds`` and lease it to ``worker``.

        Best = highest priority, then earliest ``run_after``. Kinds already running at their
        ``limits`` cap are skipped. Jobs whose lease has expired are reclaimed first.

        With ``agent`` the claim is made on behalf of an external agent (``worker`` is then
        ``agent.worker``), and the agent's row is re-read inside the transaction: it must not be
        revoked, only kinds it is scoped to and ``[swarm] allowed_kinds`` permit are considered,
        only its accounts are visible, and ``QuotaExceeded`` is raised at its lease or hourly
        limit. ``hidden_accounts`` are accounts whose mail this agent may not be offered (the
        workspace policy, decided by the caller).
        """
        now = self.clock.now()
        extra = ""
        extra_args: tuple[str, ...] = ()
        if agent is not None:
            worker = agent.worker
            if not self.swarm.enabled:
                raise PermissionDenied("the swarm is disabled ([swarm] enabled = false)")
        if not kinds:
            return None
        changes: list[_Change] = []
        async with self.db.transaction() as tx:
            changes += await self._reclaim_expired(tx, now)
            if agent is not None:
                scoped = await self._agent_scope(tx, agent, now)
                kinds = [k for k in kinds if k in scoped.kinds and k in self.swarm.allowed_kinds]
                if scoped.account_ids is not None:
                    marks = ",".join("?" * len(scoped.account_ids))
                    extra = f" AND account_id IN ({marks})"
                    extra_args = tuple(sorted(scoped.account_ids))
                if hidden_accounts:
                    marks = ",".join("?" * len(hidden_accounts))
                    extra += f" AND (account_id IS NULL OR account_id NOT IN ({marks}))"
                    extra_args += tuple(sorted(hidden_accounts))
            blocked = await self._blocked_kinds(tx, limits or {})
            allowed = [k for k in kinds if k not in blocked]
            if not allowed:
                row = None
            else:
                marks = ",".join("?" * len(allowed))
                row = await tx.fetchone(
                    "SELECT id FROM jobs WHERE status = 'queued' AND run_after <= ?"  # noqa: S608
                    f" AND kind IN ({marks}){extra}"  # only "?" placeholders are interpolated
                    " ORDER BY priority DESC, run_after, id LIMIT 1",
                    (fmt_ts(now), *allowed, *extra_args),
                )
            job: Job | None = None
            if row:
                lease = now + timedelta(seconds=self._lease(row["kind"], agent is not None))
                await tx.execute(
                    "UPDATE jobs SET status = 'running', locked_by = ?, locked_until = ?,"
                    " attempts = attempts + 1, updated_at = ?, agent_id = ?, claimed_at = ?"
                    " WHERE id = ?",
                    (
                        worker,
                        fmt_ts(lease),
                        fmt_ts(now),
                        agent.id if agent else None,
                        fmt_ts(now) if agent else None,
                        row["id"],
                    ),
                )
                got = await tx.fetchone("SELECT * FROM jobs WHERE id = ?", (row["id"],))
                if got is None:
                    raise StorageError(f"job {row['id']} vanished while being claimed")
                job = Job.from_row(got)
                changes.append(_Change(job.id, job.kind, "running", job.account_id))
        await self.announce(*changes)
        return job

    async def _agent_scope(self, tx: Tx, agent: Agent, now: datetime) -> Agent:
        """The agent as stored right now, after checking it may claim at all."""
        row = await tx.fetchone("SELECT * FROM agents WHERE id = ?", (agent.id,))
        fresh = Agent.from_row(row) if row else None
        if fresh is None or fresh.revoked:
            raise PermissionDenied("invalid or revoked agent token")
        held = await tx.fetchone(
            "SELECT COUNT(*) AS n FROM jobs WHERE status = 'running' AND agent_id = ?",
            (agent.id,),
        )
        if held and held["n"] >= fresh.max_leases:
            raise QuotaExceeded(f"agent already holds {fresh.max_leases} jobs")
        recent = await tx.fetchone(
            "SELECT COUNT(*) AS n FROM jobs WHERE agent_id = ? AND claimed_at > ?",
            (agent.id, fmt_ts(now - timedelta(hours=1))),
        )
        if recent and recent["n"] >= fresh.max_per_hour:
            raise QuotaExceeded(f"agent reached {fresh.max_per_hour} jobs in the last hour")
        await tx.execute("UPDATE agents SET last_seen_at = ? WHERE id = ?", (fmt_ts(now), agent.id))
        return fresh

    async def _reclaim_expired(self, tx: Tx, now: datetime) -> list[_Change]:
        rows = await tx.fetchall(
            "SELECT id, kind, account_id, attempts, max_attempts, cancel_requested FROM jobs"
            " WHERE status = 'running' AND locked_until <= ?",
            (fmt_ts(now),),
        )
        changes = []
        for r in rows:
            if r["cancel_requested"]:
                status, error = "cancelled", None
            elif r["attempts"] >= r["max_attempts"]:
                status, error = "failed", "lease expired on the final attempt"
            else:
                status, error = "queued", "lease expired; worker presumed dead"
            await tx.execute(
                "UPDATE jobs SET status = ?, locked_by = NULL, locked_until = NULL,"
                " last_error = COALESCE(?, last_error), run_after = ?, updated_at = ?"
                " WHERE id = ?",
                (status, error, fmt_ts(now), fmt_ts(now), r["id"]),
            )
            changes.append(_Change(JobId(r["id"]), r["kind"], status, _acct(r["account_id"])))
        return changes

    @staticmethod
    async def _blocked_kinds(tx: Tx, limits: Mapping[str, int]) -> set[str]:
        if not limits:
            return set()
        rows = await tx.fetchall(
            "SELECT kind, COUNT(*) AS n FROM jobs WHERE status = 'running' GROUP BY kind"
        )
        return {r["kind"] for r in rows if r["kind"] in limits and r["n"] >= limits[r["kind"]]}

    # -- finishing --------------------------------------------------------------------------
    async def heartbeat(self, job_id: JobId, worker: str) -> tuple[bool, bool]:
        """Extend the lease. Returns ``(still_owner, cancel_requested)``."""
        now = self.clock.now()
        async with self.db.transaction() as tx:
            kind = await tx.fetchone("SELECT kind FROM jobs WHERE id = ?", (job_id,))
            lease = now + timedelta(
                seconds=self._lease(kind["kind"] if kind else "", worker.startswith("agent:"))
            )
            changed = await tx.execute(
                "UPDATE jobs SET locked_until = ?, updated_at = ?"
                " WHERE id = ? AND locked_by = ? AND status = 'running'",
                (fmt_ts(lease), fmt_ts(now), job_id, worker),
            )
            row = await tx.fetchone("SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,))
        return changed > 0, bool(row and row["cancel_requested"])

    def _lease(self, kind: str, agent: bool) -> float:
        """How long a lease lasts. An agent's enrich job gets longer: a large model on modest
        hardware is slow, and the agent extends it with heartbeat_job."""
        if agent and kind == "ingest.enrich":
            return max(self.settings.lease_seconds, self.swarm.enrich_lease_seconds)
        return self.settings.lease_seconds

    def register_result(self, kind: str, model: type[BaseModel]) -> None:
        """Declare the schema of the result that jobs of ``kind`` produce."""
        self.result_models[kind] = model

    async def complete(
        self,
        job_id: JobId,
        worker: str,
        result: BaseModel | Mapping[str, Any] | None = None,
        *,
        produced_by: str | None = None,
    ) -> bool:
        """Mark a job succeeded, storing its ``result``. Returns False if the lease was lost.

        A result is validated against the schema registered for the job's kind and size-limited
        *before* anything is stored. A bad result raises ``InvalidResult`` and the job stays
        leased, so the worker can correct it or fail the job. A kind with no registered schema
        accepts no result at all, so an agent cannot use a job to store arbitrary data.
        ``produced_by`` is the finisher's own claim about which model did the work.
        """
        current = await self.get(job_id)
        if current is None or current.locked_by != worker or current.status != "running":
            return False
        result_json = self._check_result(current.kind, result)
        return await self._finish(
            job_id, worker, "succeeded", result_json=result_json, produced_by=produced_by
        )

    def _check_result(self, kind: str, result: BaseModel | Mapping[str, Any] | None) -> str | None:
        model = self.result_models.get(kind)
        if result is None:
            if model is not None:
                raise InvalidResult(f"jobs of kind {kind!r} must return a result")
            return None
        if model is None:
            raise InvalidResult(f"jobs of kind {kind!r} take no result")
        try:
            data = result.model_dump() if isinstance(result, BaseModel) else dict(result)
            text = model.model_validate(data).model_dump_json()
        except ValueError as exc:  # pydantic's ValidationError is a ValueError
            raise InvalidResult(f"result does not match the schema for {kind!r}: {exc}") from exc
        if len(text.encode()) > self.settings.max_result_bytes:
            raise InvalidResult(f"result is larger than {self.settings.max_result_bytes} bytes")
        return text

    async def mark_cancelled(self, job_id: JobId, worker: str) -> bool:
        return await self._finish(job_id, worker, "cancelled")

    async def release(self, job_id: JobId, worker: str) -> bool:
        """Hand a running job back unharmed (shutdown): queued again, attempt not counted."""
        return await self._finish(job_id, worker, "queued", run_now=True, undo_attempt=True)

    async def fail(
        self, job_id: JobId, worker: str, error: BaseException | str, *, permanent: bool = False
    ) -> str:
        """Record a failure. Retries with backoff unless ``permanent`` or out of attempts.
        Returns the new status (``queued`` or ``failed``), or ``lost`` if the lease was lost."""
        now = self.clock.now()
        async with self.db.transaction() as tx:
            row = await tx.fetchone(
                "SELECT * FROM jobs WHERE id = ? AND locked_by = ? AND status = 'running'",
                (job_id, worker),
            )
            if row is None:
                return "lost"
            job = Job.from_row(row)
            if permanent or job.attempts >= job.max_attempts:
                status, run_after = "failed", now
            else:
                status, run_after = "queued", now + self.backoff(job.attempts)
            await tx.execute(
                "UPDATE jobs SET status = ?, run_after = ?, last_error = ?, locked_by = NULL,"
                " locked_until = NULL, updated_at = ? WHERE id = ?",
                (status, fmt_ts(run_after), scrub_error(error), fmt_ts(now), job_id),
            )
        await self.announce(_Change(job.id, job.kind, status, job.account_id))
        return status

    async def _finish(
        self,
        job_id: JobId,
        worker: str,
        status: str,
        *,
        run_now: bool = False,
        undo_attempt: bool = False,
        result_json: str | None = None,
        produced_by: str | None = None,
    ) -> bool:
        now = self.clock.now()
        async with self.db.transaction() as tx:
            row = await tx.fetchone(
                "SELECT kind, account_id FROM jobs"
                " WHERE id = ? AND locked_by = ? AND status = 'running'",
                (job_id, worker),
            )
            if row is None:
                return False
            await tx.execute(
                "UPDATE jobs SET status = ?, locked_by = NULL, locked_until = NULL,"
                " attempts = attempts - ?, run_after = CASE WHEN ? THEN ? ELSE run_after END,"
                " updated_at = ?, result_json = ?, produced_by = ?, completed_by = ?"
                " WHERE id = ?",
                (
                    status,
                    int(undo_attempt),
                    int(run_now),
                    fmt_ts(now),
                    fmt_ts(now),
                    result_json,
                    produced_by,
                    worker if status == "succeeded" else None,
                    job_id,
                ),
            )
        await self.announce(_Change(job_id, row["kind"], status, _acct(row["account_id"])))
        return True

    def backoff(self, attempts: int) -> timedelta:
        s = self.settings
        delay = min(s.backoff_max_seconds, s.backoff_base_seconds * 2 ** max(attempts - 1, 0))
        return timedelta(seconds=delay * (1 - s.backoff_jitter * self._rng.random()))

    # -- cancelling and inspecting ----------------------------------------------------------
    async def cancel(self, job_id: JobId) -> str:
        """Cancel a queued job now, or ask a running one to stop. Returns the job's status
        afterwards (a running job stays ``running`` until its worker acts on the request)."""
        now = self.clock.now()
        async with self.db.transaction() as tx:
            row = await tx.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
            if row is None:
                raise NotFound(f"no such job: {job_id}")
            job = Job.from_row(row)
            if job.status == "queued":
                await tx.execute(
                    "UPDATE jobs SET status = 'cancelled', updated_at = ? WHERE id = ?",
                    (fmt_ts(now), job_id),
                )
                status = "cancelled"
            elif job.status == "running":
                await tx.execute(
                    "UPDATE jobs SET cancel_requested = 1, updated_at = ? WHERE id = ?",
                    (fmt_ts(now), job_id),
                )
                status = "running"
            else:
                return job.status
        if status == "cancelled":
            await self.announce(_Change(job.id, job.kind, status, job.account_id))
        return status

    async def get(self, job_id: JobId) -> Job | None:
        row = await self.db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return Job.from_row(row) if row else None

    async def query(
        self,
        *,
        status: str | None = None,
        kind: str | None = None,
        account_id: AccountId | None = None,
        limit: int = 50,
    ) -> list[Job]:
        """The newest jobs matching the filters (newest first). For a person inspecting the queue;
        the filters are optional and additive. ``limit`` is clamped to a sane maximum."""
        where: list[str] = []
        args: list[object] = []
        if status is not None:
            where.append("status = ?")
            args.append(status)
        if kind is not None:
            where.append("kind = ?")
            args.append(kind)
        if account_id is not None:
            where.append("account_id = ?")
            args.append(account_id)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        args.append(max(1, min(limit, 500)))
        rows = await self.db.fetchall(
            f"SELECT * FROM jobs{clause} ORDER BY created_at DESC, id DESC LIMIT ?",  # noqa: S608
            tuple(args),  # only "?" placeholders; the clause is built from fixed column names
        )
        return [Job.from_row(r) for r in rows]

    async def retry(self, job_id: JobId) -> str:
        """Re-queue a failed or cancelled job to run now, clearing its last error. Returns the
        job's status afterwards. A job that is queued or running is left as it is (nothing to
        retry); a succeeded job cannot be retried. Raises ``NotFound`` for an unknown id."""
        now = self.clock.now()
        async with self.db.transaction() as tx:
            row = await tx.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
            if row is None:
                raise NotFound(f"no such job: {job_id}")
            job = Job.from_row(row)
            if job.status not in ("failed", "cancelled"):
                return job.status
            await tx.execute(
                "UPDATE jobs SET status = 'queued', run_after = ?, last_error = NULL,"
                " locked_by = NULL, locked_until = NULL, cancel_requested = 0, updated_at = ?"
                " WHERE id = ?",
                (fmt_ts(now), fmt_ts(now), job_id),
            )
        await self.announce(_Change(job.id, job.kind, "queued", job.account_id))
        return "queued"

    async def children(self, parent_id: JobId) -> list[Job]:
        """The jobs enqueued with this parent, oldest first (order within a millisecond is
        arbitrary)."""
        rows = await self.db.fetchall(
            "SELECT * FROM jobs WHERE parent_job_id = ? ORDER BY created_at, id", (parent_id,)
        )
        return [Job.from_row(r) for r in rows]

    async def next_due(self) -> datetime | None:
        """When the queue next needs attention: the earliest queued ``run_after`` or the earliest
        lease expiry (whichever is first), or None if idle."""
        row = await self.db.fetchone(
            "SELECT MIN(CASE WHEN status = 'queued' THEN run_after ELSE locked_until END) AS t"
            " FROM jobs WHERE status IN ('queued', 'running')"
        )
        return parse_ts(row["t"]) if row and row["t"] else None


def _acct(value: str | None) -> AccountId | None:
    return AccountId(value) if value else None


# -- the runtime ----------------------------------------------------------------------------
Handler = Callable[[Any, Job], Awaitable[BaseModel | None]]


@dataclass(frozen=True)
class _Registration:
    model: type[BaseModel]
    handler: Handler
    concurrency: int | None


@dataclass
class _Running:
    task: "asyncio.Task[BaseModel | None]"
    user_cancelled: bool = False


class JobRunner:
    """Runs jobs from a ``JobQueue`` with N concurrent workers."""

    def __init__(
        self,
        queue: JobQueue,
        *,
        workers: int | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        paused_kinds: Callable[[], Awaitable[Collection[str]]] | None = None,
        limit_overrides: Callable[[], Awaitable[dict[str, int]]] | None = None,
    ) -> None:
        self.queue = queue
        self.workers = workers or queue.settings.workers
        self._sleep = sleep  # used by the lease heartbeat; replaceable in tests
        # Optional: kinds the runner must not claim right now (e.g. the model-bound ingest stages
        # while the person has paused the model). Consulted once per claim cycle.
        self._paused_kinds = paused_kinds
        self._limit_overrides = limit_overrides  # chosen at run time; they outrank config
        self._registry: dict[str, _Registration] = {}
        self._running: dict[JobId, _Running] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False
        self._id = uuid7().hex[-8:]
        self.processed = 0

    def register(
        self,
        kind: str,
        model: type[M],
        handler: Callable[[M, Job], Awaitable[BaseModel | None]],
        *,
        concurrency: int | None = None,
        result_model: type[BaseModel] | None = None,
    ) -> None:
        """Run ``handler(payload, job)`` for jobs of ``kind``. The payload is validated against
        ``model``; a job that does not validate fails at once. ``concurrency`` caps how many of
        this kind run at once (``[jobs] kind_limits`` in config takes precedence). With
        ``result_model`` the handler must return a result of that shape, which is stored on the
        job; external agents completing the same kind are held to the same schema."""
        if kind in self._registry:
            raise ValueError(f"a handler for {kind!r} is already registered")
        self._registry[kind] = _Registration(model, handler, concurrency)
        if result_model is not None:
            self.queue.register_result(kind, result_model)

    def _limits(self) -> dict[str, int]:
        limits = {k: r.concurrency for k, r in self._registry.items() if r.concurrency}
        return {**limits, **self.queue.settings.kind_limits}

    # -- lifecycle --------------------------------------------------------------------------
    def start(self) -> None:
        if self._tasks:
            raise RuntimeError("runner already started")
        self._stopping = False
        self._tasks = [
            asyncio.create_task(self._worker(f"{self._id}-{i}", idle_exit=False))
            for i in range(self.workers)
        ]

    async def stop(self, grace: float = 30.0) -> None:
        """Stop claiming. In-flight jobs get ``grace`` seconds to finish; any still running are
        cancelled and their leases released, so another run picks them up unharmed."""
        self._stopping = True
        self.queue.wake.set()
        tasks, self._tasks = self._tasks, []
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=grace)
        for t in pending:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def drain(self) -> int:
        """Run every job that is runnable *now* (and any it enqueues), then return how many ran.
        Jobs scheduled for later (retry backoff) are left: advance the clock and drain again."""
        before = self.processed
        workers = [
            asyncio.create_task(self._worker(f"{self._id}-d{i}", idle_exit=True))
            for i in range(self.workers)
        ]
        await asyncio.gather(*workers)
        return self.processed - before

    async def cancel(self, job_id: JobId) -> str:
        """Cancel a job; if it is running here, interrupt its handler immediately."""
        status = await self.queue.cancel(job_id)
        entry = self._running.get(job_id)
        if entry and not entry.task.done():
            entry.user_cancelled = True
            entry.task.cancel()
        return status

    # -- workers ----------------------------------------------------------------------------
    async def _worker(self, name: str, *, idle_exit: bool) -> None:
        while not self._stopping:
            self.queue.wake.clear()
            kinds = await self._claimable_kinds()
            limits = self._limits()
            if self._limit_overrides is not None and kinds:
                limits = {**limits, **await self._limit_overrides()}
            job = await self.queue.claim(name, kinds, limits) if kinds else None
            if job is None:
                if idle_exit:
                    return
                await self._idle_wait()
                continue
            await self._execute(job, name)
            self.processed += 1
            self.queue.wake.set()  # a finished job may free a concurrency slot

    async def _claimable_kinds(self) -> list[str]:
        """The registered kinds, minus any the control says to hold back right now."""
        kinds = list(self._registry)
        if self._paused_kinds is None:
            return kinds
        paused = set(await self._paused_kinds())
        return [k for k in kinds if k not in paused]

    async def _idle_wait(self) -> None:
        cap = min(self.queue.settings.lease_seconds / 2, 30.0)
        due = await self.queue.next_due()
        timeout = cap
        if due is not None:
            timeout = min(cap, max((due - self.queue.clock.now()).total_seconds(), 0.25))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.queue.wake.wait(), timeout)

    async def _execute(self, job: Job, worker: str) -> None:
        q = self.queue
        reg = self._registry[job.kind]
        try:
            payload = reg.model.model_validate_json(job.payload_json)
        except ValidationError as exc:
            await q.fail(job.id, worker, f"invalid payload: {exc}", permanent=True)
            return
        task = asyncio.ensure_future(_call(reg.handler, payload, job))
        entry = _Running(task)
        self._running[job.id] = entry
        beat = asyncio.create_task(self._heartbeat(job, worker, entry))
        try:
            result = await task
            if not await q.complete(job.id, worker, result):
                log.warning("job finished after losing its lease", job_id=job.id, kind=job.kind)
        except asyncio.CancelledError:
            if entry.user_cancelled:
                await q.mark_cancelled(job.id, worker)
                return
            await q.release(job.id, worker)  # shutting down
            raise
        except (PermanentJobError, InvalidResult) as exc:  # a retry would only repeat it
            await q.fail(job.id, worker, exc, permanent=True)
        except Exception as exc:
            log.warning("job failed", job_id=job.id, kind=job.kind, attempt=job.attempts)
            await q.fail(job.id, worker, exc)
        finally:
            beat.cancel()
            self._running.pop(job.id, None)

    async def _heartbeat(self, job: Job, worker: str, entry: _Running) -> None:
        interval = self.queue.settings.lease_seconds / 3
        while True:
            await self._sleep(interval)
            owner, cancel = await self.queue.heartbeat(job.id, worker)
            if not owner:
                log.warning("lease lost", job_id=job.id, kind=job.kind)
                return
            if cancel:
                entry.user_cancelled = True
                entry.task.cancel()
                return


async def _call(handler: Handler, payload: Any, job: Job) -> BaseModel | None:
    return await handler(payload, job)
