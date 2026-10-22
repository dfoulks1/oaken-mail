# SPDX-License-Identifier: Apache-2.0
"""When each account syncs, and the loop that makes it happen (prompt 12b).

The schedule is data in ``raw.db`` (``sync_schedules``), so an edit from a UI or the CLI is live and
survives a restart. Core runs it from one asyncio task: ``tick()`` queues a ``sync.run`` job for
every due schedule and moves its ``next_run`` forward in the same transaction, so a crash neither
repeats nor skips a run. The job does the work through ``SyncRunner``.

* **Missed runs collapse.** After sleep or downtime a schedule is due once, runs once, and carries
  on from now; a wake from sleep therefore syncs once, at the next tick (at most 30 s later).
* **One sync at a time per account.** A sync request for an account that is already queued or
  running joins that job (a dedupe key per account and folder), and ``SyncRunner`` holds a lock per
  account, so a folder sync and a whole-account sync never overlap either.
* **At most ``sync.max_concurrent`` accounts sync at once**, through the job runner's per-kind
  limit. Sync and ``readstate.write`` are different kinds, so a slow pull never delays a mark-read.
* **A failure backs off** (``interval * 2**failures``, capped, with jitter). **An authentication
  failure pauses that account** until the user resumes it, and nothing else is affected.
* **Quiet hours never stop a sync.** They only mark its events ``quiet`` so clients do not alert.
"""

import asyncio
import contextlib
import random
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, tzinfo
from functools import lru_cache
from sqlite3 import Row
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel

from oaken.core.actors import SYSTEM, USER, Actor
from oaken.core.clock import Clock, fmt_ts, parse_ts
from oaken.core.cron import parse_cron
from oaken.core.errors import ConfigError, NotFound, OakenError
from oaken.core.events import EventBus, SyncPaused, SyncScheduleChanged
from oaken.core.ids import AccountId
from oaken.core.jobs import (
    PRIORITY_INGEST,
    PRIORITY_USER,
    Enqueued,
    Job,
    JobRunner,
    PermanentJobError,
)
from oaken.core.logging import get_logger
from oaken.core.rawstore import RawStore
from oaken.core.settings import SyncSettings
from oaken.ingest.sync import SyncResult, SyncRunner
from oaken.plugins.accounts import AccountProviders
from oaken.plugins.base import inferred_capabilities
from oaken.readstate.audit import AuditLog, new_audit_id

log = get_logger("oaken.sync.schedule")

SYNC_KIND = "sync.run"
MAX_SLEEP_SECONDS = 30.0
AUTH_PAUSE = "authentication failed"  # a paused_reason that starts so is cleared by a good sync
_EVERY = re.compile(r"^every\s+(\d+)\s*([smhd])$", re.IGNORECASE)
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_QUIET = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")


class SyncPayload(BaseModel):
    account_id: str
    folder: str = ""  # "" is the whole account
    reason: str = "scheduled"


@lru_cache(maxsize=64)
def zone(name: str) -> tzinfo:
    """A timezone from the system's tz database (``zoneinfo``), or ``ConfigError``."""
    if name.upper() == "UTC":
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ConfigError(
            f"unknown timezone {name!r} (use an IANA name such as Europe/Paris)"
        ) from None


def parse_spec(text: str) -> tuple[str, str]:
    """``every 15m`` (s, m, h or d) or a five-field cron expression, as ``(kind, spec)`` where an
    interval's spec is its seconds."""
    text = text.strip()
    m = _EVERY.match(text)
    if m:
        seconds = int(m.group(1)) * _UNITS[m.group(2).lower()]
        if seconds < 1:
            raise ConfigError("an interval must be at least one second")
        return "interval", str(seconds)
    if len(text.split()) == 5:
        parse_cron(text)
        return "cron", " ".join(text.split())
    raise ConfigError(f"not a schedule: {text!r} (write 'every 15m' or a five-field cron line)")


def describe(kind: str, spec: str) -> str:
    if kind == "cron":
        return spec
    seconds = int(float(spec))
    for unit in "dhm":
        if seconds % _UNITS[unit] == 0:
            return f"every {seconds // _UNITS[unit]}{unit}"
    return f"every {seconds}s"


def parse_quiet(text: str) -> tuple[str, str]:
    """``22:00-07:00`` as ``("22:00", "07:00")``."""
    m = _QUIET.match(text.strip())
    if not m:
        raise ConfigError(f"quiet hours are written HH:MM-HH:MM, not {text!r}")
    a, b, c, d = (int(x) for x in m.groups())
    if not (a < 24 and c < 24 and b < 60 and d < 60) or (a, b) == (c, d):
        raise ConfigError(f"quiet hours {text!r} are not a valid window")
    return f"{a:02d}:{b:02d}", f"{c:02d}:{d:02d}"


def _clock_time(text: str) -> time:
    h, _, m = text.partition(":")
    return time(int(h), int(m))


def _next_run(kind: str, spec: str, tz: tzinfo, after: datetime, anchor: datetime) -> datetime:
    """The first run strictly after ``after``; an interval keeps its phase (anchor + k * step)."""
    if kind == "cron":
        return parse_cron(spec).next_after(after, tz)
    step = float(spec)
    return anchor + timedelta(seconds=(int((after - anchor).total_seconds() // step) + 1) * step)


def _base_seconds(kind: str, spec: str, tz: tzinfo, now: datetime) -> float:
    """How long a schedule normally waits between runs (a cron line: the gap after the next)."""
    if kind == "interval":
        return float(spec)
    first = parse_cron(spec).next_after(now, tz)
    return (parse_cron(spec).next_after(first, tz) - first).total_seconds()


def _scrub(text: str) -> str:
    """One short line for ``last_error``. Providers never put mail text in an error, and this
    caps the length anyway."""
    return " ".join(text.split())[:200]


@dataclass(frozen=True)
class SyncSchedule:
    account_id: AccountId
    folder: str
    spec_kind: str
    spec: str
    timezone: str
    quiet: tuple[str, str] | None
    paused: bool
    paused_reason: str
    next_run: datetime
    last_run: datetime | None
    last_result: str | None
    last_error: str | None
    last_new: int | None
    failures: int
    # Why the account cannot sync right now (its provider failed to start), if so.
    problem: str | None = None

    @property
    def schedule(self) -> str:
        return describe(self.spec_kind, self.spec)

    def audit_view(self) -> dict[str, Any]:
        return {
            "schedule": self.schedule,
            "timezone": self.timezone,
            "quiet": "-".join(self.quiet) if self.quiet else None,
            "paused": self.paused,
            "paused_reason": self.paused_reason,
        }

    @classmethod
    def from_row(cls, r: Row, problem: str | None = None) -> "SyncSchedule":
        return cls(
            account_id=AccountId(r["account_id"]),
            folder=r["folder"],
            spec_kind=r["spec_kind"],
            spec=r["spec"],
            timezone=r["timezone"],
            quiet=(r["quiet_start"], r["quiet_end"]) if r["quiet_start"] else None,
            paused=bool(r["paused"]),
            paused_reason=r["paused_reason"],
            next_run=parse_ts(r["next_run"]),
            last_run=parse_ts(r["last_run"]) if r["last_run"] else None,
            last_result=r["last_result"],
            last_error=r["last_error"],
            last_new=r["last_new"],
            failures=r["failures"],
            problem=problem,
        )

    def in_quiet_hours(self, now: datetime) -> bool:
        if not self.quiet:
            return False
        start, end = _clock_time(self.quiet[0]), _clock_time(self.quiet[1])
        at = now.astimezone(zone(self.timezone)).time().replace(second=0, microsecond=0)
        return start <= at < end if start < end else (at >= start or at < end)


class SyncService:
    def __init__(
        self,
        raw: RawStore,
        runner: SyncRunner,
        accounts: AccountProviders,
        audit: AuditLog,
        clock: Clock,
        settings: SyncSettings | None = None,
        *,
        bus: EventBus | None = None,
        after_providers: Sequence[Callable[[], Awaitable[object]]] = (),
        rng: random.Random | None = None,
    ) -> None:
        self.raw, self.runner, self.accounts, self.audit = raw, runner, accounts, audit
        self.clock, self.bus = clock, bus
        self.settings = settings or SyncSettings()
        self._after_providers = tuple(after_providers)  # e.g. resume pending write-backs
        self._rng = rng or random.Random()  # noqa: S311 (retry jitter, not security)
        self._queue = runner.pipeline.queue
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    # -- wiring -----------------------------------------------------------------------------
    def register(self, jobs: JobRunner) -> None:
        """Handle ``sync.run`` jobs, at most ``sync.max_concurrent`` at a time."""
        jobs.register(SYNC_KIND, SyncPayload, self._run, concurrency=self.settings.max_concurrent)

    async def start(self) -> dict[str, str]:
        """Bring the daemon's mail side up, in order: a provider for every account, then whatever
        the caller asked to run once providers are up (the daemon passes read-state's
        ``resume_pending``, closing the window noted in prompt 12), then the schedule loop. Returns
        the accounts that have no provider, with the reason; the others are unaffected."""
        problems = await self.accounts.start()
        for step in self._after_providers:
            await step()
        await self.ensure_schedules()
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())
        return problems

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        task, self._task = self._task, None
        if task:
            await task

    async def ensure_schedules(self) -> int:
        """Give every account that can pull mail a schedule, from ``[sync]`` defaults. An account
        that already has one keeps it. Returns how many were added."""
        added = 0
        for acct in await self.raw.list_accounts():
            if await self.get(acct.id) is not None:
                continue
            try:
                caps = inferred_capabilities(self.accounts.get(acct.id), "provider")
            except OakenError:
                caps = frozenset({"sync.pull"})  # unknown: schedule it and show the problem
            if "sync.pull" not in caps:
                continue  # a send-only account (SMTP) has nothing to pull
            await self.set(
                acct.id,
                f"every {self.settings.interval_seconds}s",
                timezone=self.settings.timezone,
                actor=SYSTEM,
                reason="default_schedule",
                run_now=True,
            )
            added += 1
        return added

    # -- reading ----------------------------------------------------------------------------
    async def get(self, account_id: str, folder: str = "") -> SyncSchedule | None:
        row = await self.raw.db.fetchone(
            "SELECT * FROM sync_schedules WHERE account_id = ? AND folder = ?",
            (account_id, folder),
        )
        return SyncSchedule.from_row(row, self.accounts.problem(account_id)) if row else None

    async def schedules(self) -> list[SyncSchedule]:
        rows = await self.raw.db.fetchall(
            "SELECT * FROM sync_schedules ORDER BY account_id, folder"
        )
        return [SyncSchedule.from_row(r, self.accounts.problem(r["account_id"])) for r in rows]

    async def crontab(self) -> str:
        """The schedules as crontab lines, for reading or pasting. Read-only: the database is the
        only place a schedule is edited. An interval that cron cannot say (every 7 minutes) is a
        comment; a paused schedule is commented out."""
        lines = ["# Oaken Mail sync schedule (export only: edit it in Oaken, not here)"]
        for s in await self.schedules():
            what = f"oaken sync run {s.account_id}" + (
                f" --folder {s.folder!r}" if s.folder else ""
            )
            line = _cron_line(s)
            note = f"# {s.account_id}" + (f"/{s.folder}" if s.folder else "") + f" [{s.timezone}]"
            if s.paused:
                note += f" PAUSED: {s.paused_reason}"
            if line is None:
                lines.append(f"# {s.schedule} (no cron equivalent) {what}  {note}")
            else:
                lines += [
                    f"CRON_TZ={s.timezone}",
                    f"{'# ' if s.paused else ''}{line} {what}  {note}",
                ]
        return "\n".join(lines) + "\n"

    # -- editing (live, audited) --------------------------------------------------------------
    async def set(
        self,
        account_id: str,
        schedule: str,
        *,
        folder: str = "",
        timezone: str | None = None,
        quiet: str | None = None,
        clear_quiet: bool = False,
        actor: Actor = USER,
        reason: str = "user_set_schedule",
        run_now: bool = False,
    ) -> SyncSchedule:
        """Create or change a schedule. It is checked now (the interval floor, the timezone, the
        quiet window, the folder), not discovered at the first run. A new schedule runs straight
        away when ``run_now``, otherwise one period from now; a changed one is due one period after
        now. Unchanged fields keep their value."""
        await self.raw.get_account(AccountId(account_id))  # NotFound if there is no such account
        kind, spec = parse_spec(schedule)
        old = await self.get(account_id, folder)
        tz_name = timezone or (old.timezone if old else self.settings.timezone)
        tz = zone(tz_name)
        floor = self.settings.min_interval_seconds
        if _base_seconds(kind, spec, tz, self.clock.now()) < floor:
            raise ConfigError(
                f"{describe(kind, spec)} is faster than the {floor}s minimum "
                "(sync.min_interval_seconds); mail services limit how often they may be asked"
            )
        if folder:
            self._check_folder(account_id, folder)
        window = (
            parse_quiet(quiet) if quiet else (None if clear_quiet else (old.quiet if old else None))
        )
        now = self.clock.now()
        if old and (old.spec_kind, old.spec, old.timezone) == (kind, spec, tz_name):
            next_run = old.next_run
        elif old is None and run_now:
            next_run = now
        else:
            next_run = _next_run(kind, spec, tz, now, now)
        new_view = {
            "schedule": describe(kind, spec),
            "timezone": tz_name,
            "quiet": "-".join(window) if window else None,
            "paused": old.paused if old else False,
            "paused_reason": old.paused_reason if old else "",
        }
        subject = _subject(account_id, folder)
        async with self.raw.db.transaction() as tx:
            await tx.execute(
                "INSERT INTO sync_schedules (account_id, folder, spec_kind, spec, timezone,"
                " quiet_start, quiet_end, next_run, updated_at) VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(account_id, folder) DO UPDATE SET spec_kind = excluded.spec_kind,"
                " spec = excluded.spec, timezone = excluded.timezone,"
                " quiet_start = excluded.quiet_start, quiet_end = excluded.quiet_end,"
                " next_run = excluded.next_run, updated_at = excluded.updated_at",
                (
                    account_id,
                    folder,
                    kind,
                    spec,
                    tz_name,
                    window[0] if window else None,
                    window[1] if window else None,
                    fmt_ts(next_run),
                    fmt_ts(now),
                ),
            )
            await self._audit(
                tx, actor, reason, "schedule.set", subject,
                old.audit_view() if old else {}, new_view,
            )  # fmt: skip
        await self._changed(account_id, folder, "set" if old else "added")
        result = await self.get(account_id, folder)
        assert result is not None  # noqa: S101 (written in this call)
        return result

    async def pause(
        self,
        account_id: str,
        folder: str = "",
        *,
        why: str = "paused by the user",
        actor: Actor = USER,
        reason: str = "user_pause_schedule",
    ) -> SyncSchedule:
        return await self._set_paused(
            account_id, folder, True, why, actor, reason, "paused", "schedule.pause"
        )

    async def resume(
        self,
        account_id: str,
        folder: str = "",
        *,
        actor: Actor = USER,
        reason: str = "user_resume_schedule",
    ) -> SyncSchedule:
        """Resume a paused schedule: due now, with the failure count cleared."""
        return await self._set_paused(
            account_id, folder, False, "", actor, reason, "resumed", "schedule.resume"
        )

    async def remove(self, account_id: str, folder: str = "", *, actor: Actor = USER) -> bool:
        old = await self.get(account_id, folder)
        if old is None:
            return False
        async with self.raw.db.transaction() as tx:
            await tx.execute(
                "DELETE FROM sync_schedules WHERE account_id = ? AND folder = ?",
                (account_id, folder),
            )
            await self._audit(
                tx, actor, "user_remove_schedule", "schedule.remove",
                _subject(account_id, folder), old.audit_view(), {},
            )  # fmt: skip
        await self._changed(account_id, folder, "removed")
        return True

    async def _set_paused(
        self,
        account_id: str,
        folder: str,
        paused: bool,
        why: str,
        actor: Actor,
        reason: str,
        change: str,
        op: str,
    ) -> SyncSchedule:
        old = await self.get(account_id, folder)
        if old is None:
            raise NotFound(f"no sync schedule for {_subject(account_id, folder)}")
        now = self.clock.now()
        after = {**old.audit_view(), "paused": paused, "paused_reason": why}
        async with self.raw.db.transaction() as tx:
            await tx.execute(
                "UPDATE sync_schedules SET paused = ?, paused_reason = ?, updated_at = ?,"
                " failures = CASE WHEN ? = 0 THEN 0 ELSE failures END,"
                " next_run = CASE WHEN ? = 0 THEN ? ELSE next_run END"
                " WHERE account_id = ? AND folder = ?",
                (
                    int(paused), why, fmt_ts(now), int(paused), int(paused), fmt_ts(now),
                    account_id, folder,
                ),
            )  # fmt: skip
            await self._audit(
                tx, actor, reason, op,
                _subject(account_id, folder), old.audit_view(), after,
            )  # fmt: skip
        await self._changed(account_id, folder, change)
        result = await self.get(account_id, folder)
        assert result is not None  # noqa: S101 (written in this call)
        return result

    async def _audit(
        self,
        tx: Any,
        actor: Actor,
        reason: str,
        op: str,
        subject: str,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
    ) -> None:
        # The audit row's id list holds what changed: here "account" or "account/folder".
        await self.audit.record(
            tx,
            audit_id=(i := new_audit_id()),
            actor=actor,
            reason=reason,
            op=op,
            batch_id=i,
            before={subject: dict(before)} if before else {},
            after={subject: dict(after)} if after else {subject: {}},
        )

    async def _changed(self, account_id: str, folder: str, change: str) -> None:
        self._wake.set()  # the loop re-reads the next due time
        if self.bus:
            await self.bus.publish(
                SyncScheduleChanged(
                    ts=self.clock.now(),
                    account_id=AccountId(account_id),
                    folder=folder,
                    change=change,
                )
            )

    def _check_folder(self, account_id: str, folder: str) -> None:
        try:
            provider = self.accounts.get(account_id)
        except OakenError as exc:
            raise ConfigError(f"cannot check the folder: {exc}") from exc
        if not getattr(provider, "supports_folders", False):
            raise ConfigError(
                f"account {account_id!r} has no folders to sync one by one; schedule the account"
            )

    # -- asking for a sync now ----------------------------------------------------------------
    async def request(
        self,
        account_id: str,
        folder: str = "",
        *,
        actor: Actor = USER,
        reason: str = "sync_now",
    ) -> Enqueued:
        """Sync now. If that account (or folder) is already queued or running the request joins
        that sync (``created`` is False) instead of starting another. This is the one door for
        "something happened, go look": a button, and later IMAP IDLE or a Gmail push."""
        await self.raw.get_account(AccountId(account_id))
        return await self._queue.enqueue(
            SYNC_KIND,
            SyncPayload(account_id=account_id, folder=folder, reason=reason),
            priority=PRIORITY_USER if actor.kind == "user" else PRIORITY_INGEST,
            account_id=AccountId(account_id),
            dedupe_key=_dedupe(account_id, folder),
            actor=actor,
        )

    # -- the loop ---------------------------------------------------------------------------
    async def tick(self) -> int:
        """Queue a sync for every schedule that is due, and move each to its next run."""
        now = self.clock.now()
        changes = []
        async with self.raw.db.transaction() as tx:
            due = await tx.fetchall(
                "SELECT * FROM sync_schedules WHERE paused = 0 AND next_run <= ?"
                " ORDER BY next_run, account_id, folder",
                (fmt_ts(now),),
            )
            for r in due:
                payload = SyncPayload(account_id=r["account_id"], folder=r["folder"])
                job, change = await self._queue.enqueue_in(
                    tx,
                    SYNC_KIND,
                    payload,
                    priority=PRIORITY_INGEST,
                    account_id=AccountId(r["account_id"]),
                    dedupe_key=_dedupe(r["account_id"], r["folder"]),
                )
                changes.append(change)
                nxt = _next_run(
                    r["spec_kind"], r["spec"], zone(r["timezone"]), now, parse_ts(r["next_run"])
                )
                await tx.execute(
                    "UPDATE sync_schedules SET next_run = ? WHERE account_id = ? AND folder = ?",
                    (fmt_ts(nxt), r["account_id"], r["folder"]),
                )
        await self._queue.announce(*changes)
        return len(due)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                await self.tick()
            except Exception:  # the loop must outlive a bad tick
                log.exception("sync tick failed")
            row = await self.raw.db.fetchone(
                "SELECT MIN(next_run) AS t FROM sync_schedules WHERE paused = 0"
            )
            timeout = MAX_SLEEP_SECONDS
            if row and row["t"]:
                wait = (parse_ts(row["t"]) - self.clock.now()).total_seconds()
                timeout = min(MAX_SLEEP_SECONDS, max(wait, 0.05))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout)

    # -- the job ----------------------------------------------------------------------------
    async def _run(self, payload: SyncPayload, job: Job) -> None:
        sched = await self.get(payload.account_id, payload.folder)
        now = self.clock.now()
        quiet = bool(sched and sched.in_quiet_hours(now))
        try:
            provider = self.accounts.get(payload.account_id)
        except OakenError as exc:
            await self._record(payload, sched, None, _scrub(str(exc)), quiet)
            raise PermanentJobError(_scrub(str(exc))) from None
        result = await self.runner.sync(
            AccountId(payload.account_id),
            provider,
            folder=payload.folder or None,
            quiet=quiet,
        )
        await self._record(payload, sched, result, None, quiet)
        if result.errors:
            # The schedule row already backs off (or pauses); the job must not read as a success,
            # and must not be retried on top of that backoff.
            raise PermanentJobError(_scrub(result.errors[0]))

    async def _record(
        self,
        payload: SyncPayload,
        sched: SyncSchedule | None,
        result: SyncResult | None,
        problem: str | None,
        quiet: bool,
    ) -> None:
        """Write what happened onto the schedule row; back off, or pause on an auth failure."""
        if sched is None:  # removed while it ran
            return
        now = self.clock.now()
        error = problem or (result.errors[0] if result and result.errors else None)
        acct, folder = payload.account_id, payload.folder
        if error is None:
            assert result is not None  # noqa: S101 (no error means a result)
            # A good sync clears an authentication pause: the user has evidently fixed it.
            clear = sched.paused and sched.paused_reason.startswith(AUTH_PAUSE)
            soon = fmt_ts(now + timedelta(seconds=self.settings.min_interval_seconds))
            await self.raw.db.execute(
                "UPDATE sync_schedules SET last_run = ?, last_result = 'ok', last_error = NULL,"
                " last_new = ?, failures = 0,"
                " paused = CASE WHEN ? THEN 0 ELSE paused END,"
                " paused_reason = CASE WHEN ? THEN '' ELSE paused_reason END,"
                " next_run = CASE WHEN ? THEN ? ELSE next_run END"
                " WHERE account_id = ? AND folder = ?",
                (
                    fmt_ts(now), result.stored, clear, clear, result.paused, soon, acct, folder,
                ),
            )  # fmt: skip
            return
        if result is not None and result.auth_failed:
            why = f"{AUTH_PAUSE}: {_scrub(error)}"
            await self._pause_for_auth(sched, why, now, quiet)
            return
        failures = sched.failures + 1
        base = _base_seconds(sched.spec_kind, sched.spec, zone(sched.timezone), now)
        delay = min(base * 2 ** min(failures, 16), self.settings.backoff_max_seconds)
        jitter = self.settings.backoff_jitter
        delay *= 1 + self._rng.uniform(-jitter, jitter)
        await self.raw.db.execute(
            "UPDATE sync_schedules SET last_run = ?, last_result = 'error', last_error = ?,"
            " failures = ?, next_run = ? WHERE account_id = ? AND folder = ?",
            (
                fmt_ts(now), _scrub(error), failures,
                fmt_ts(now + timedelta(seconds=delay)), acct, folder,
            ),
        )  # fmt: skip

    async def _pause_for_auth(
        self, sched: SyncSchedule, why: str, now: datetime, quiet: bool
    ) -> None:
        async with self.raw.db.transaction() as tx:
            await tx.execute(
                "UPDATE sync_schedules SET last_run = ?, last_result = 'auth', last_error = ?,"
                " paused = 1, paused_reason = ?, updated_at = ?"
                " WHERE account_id = ? AND folder = ?",
                (fmt_ts(now), why, why, fmt_ts(now), sched.account_id, sched.folder),
            )
            await self._audit(
                tx, SYSTEM, "auth_failed", "schedule.pause",
                _subject(sched.account_id, sched.folder),
                sched.audit_view(), {**sched.audit_view(), "paused": True, "paused_reason": why},
            )  # fmt: skip
        log.warning("sync paused: authentication failed", account_id=sched.account_id)
        await self._changed(sched.account_id, sched.folder, "paused")
        if self.bus:
            await self.bus.publish(
                SyncPaused(ts=now, account_id=sched.account_id, reason=why, quiet=quiet)
            )


def _subject(account_id: str, folder: str) -> str:
    return f"{account_id}/{folder}" if folder else account_id


def _dedupe(account_id: str, folder: str) -> str:
    return f"sync:{account_id}:{folder}" if folder else f"sync:{account_id}"


def _cron_line(s: SyncSchedule) -> str | None:
    """The schedule as five cron fields, or None if cron cannot express it."""
    if s.spec_kind == "cron":
        return s.spec
    minutes = int(float(s.spec)) // 60
    if int(float(s.spec)) % 60 or minutes < 1:
        return None
    if minutes < 60 and 60 % minutes == 0:
        return f"*/{minutes} * * * *"
    if minutes % 60 == 0 and 24 % (minutes // 60) == 0 and minutes // 60 < 24:
        return f"0 */{minutes // 60} * * *"
    if minutes == 1440:
        return "0 0 * * *"
    return None
