# SPDX-License-Identifier: Apache-2.0
"""Sync scheduling (prompt 12b): FakeClock, FakeProvider, no sleeps, no network."""

import asyncio
import random
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib.metadata import EntryPoint
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from oaken.core.actors import SYSTEM, Actor
from oaken.core.clock import fmt_ts
from oaken.core.errors import AuthError, ConfigError, NotFound
from oaken.core.events import SyncFinished, SyncPaused, SyncScheduleChanged
from oaken.core.jobs import JobRunner
from oaken.core.settings import ReadStateSettings, Settings, SyncSettings
from oaken.ingest.schedule import SYNC_KIND, SyncService, parse_quiet, parse_spec
from oaken.ingest.tests.conftest import ACCT, Options, Rig, build, mail
from oaken.plugins.accounts import AccountProviders
from oaken.plugins.contracts import PullBatch
from oaken.plugins.host import PluginHost
from oaken.readstate import AuditLog, ReadState, providers_from_accounts
from oaken.testing.fakes import FakeProvider

AGENT = Actor("agent", "claude")
T0 = datetime(2026, 1, 1, tzinfo=UTC)


class Mailbox(FakeProvider):
    """A fake whose pulls can be held open, to see what runs at the same time."""

    gate: ClassVar[asyncio.Event | None] = None
    running: ClassVar[int] = 0
    most: ClassVar[int] = 0
    pulls: ClassVar[int] = 0

    async def pull(self, cursor: str | None, folder: str | None = None) -> AsyncIterator[PullBatch]:
        Mailbox.pulls += 1
        Mailbox.running += 1
        Mailbox.most = max(Mailbox.most, Mailbox.running)
        try:
            if Mailbox.gate is not None:
                await Mailbox.gate.wait()
            async for batch in super().pull(cursor, folder):
                yield batch
        finally:
            Mailbox.running -= 1


class _Nothing(BaseModel):
    pass


class FolderMailbox(Mailbox):
    supports_folders = True


@pytest.fixture(autouse=True)
def _reset() -> None:
    Mailbox.gate, Mailbox.running, Mailbox.most, Mailbox.pulls = None, 0, 0, 0


def _discover(group: str) -> Iterable[EntryPoint]:
    if group != "oaken.providers":
        return []
    return [
        EntryPoint("fake", f"{__name__}:Mailbox", group),
        EntryPoint("folders", f"{__name__}:FolderMailbox", group),
    ]


@dataclass
class World:
    rig: Rig
    host: PluginHost
    accounts: AccountProviders
    svc: SyncService
    audit: AuditLog

    def provider(self, account: str) -> Mailbox:
        p = self.accounts.get(account)
        assert isinstance(p, Mailbox)
        return p

    async def add_account(self, name: str, plugin: str = "fake") -> None:
        await self.rig.raw.create_account(name, plugin, name.title())
        await self.accounts.start()

    async def fire(self) -> int:
        """One tick, then run what it queued."""
        fired = await self.svc.tick()
        await self.rig.runner.drain()
        return fired


async def _world(
    rig: Rig,
    sync: SyncSettings,
    *,
    rng: random.Random | None = None,
    runner: JobRunner | None = None,
) -> World:
    """``runner`` is the job runner the sync handler goes on: the rig's own unless given."""
    host = PluginHost(Settings(), rig.clock, rig.bus, discover=_discover, health_interval=3600)
    await host.start()
    accounts = AccountProviders(host, rig.raw)
    await accounts.start()  # the rig's own account, "main", uses the "fake" plugin
    audit = AuditLog(rig.raw_db, rig.clock)
    svc = SyncService(
        rig.raw, rig.sync, accounts, audit, rig.clock, sync, bus=rig.bus,
        rng=rng or random.Random(0),
    )  # fmt: skip
    svc.register(runner or rig.runner)
    return World(rig, host, accounts, svc, audit)


@pytest.fixture
async def w(rig: Rig) -> AsyncIterator[World]:
    world = await _world(rig, SyncSettings(backoff_jitter=0.0))
    try:
        yield world
    finally:
        await world.svc.stop()
        await world.host.stop()


# -- parsing and validation -------------------------------------------------------------------
def test_a_schedule_is_every_n_units_or_a_cron_line() -> None:
    assert parse_spec("every 15m") == ("interval", "900")
    assert parse_spec("Every 2h") == ("interval", "7200")
    assert parse_spec("every 90s") == ("interval", "90")
    assert parse_spec("*/5 * * * *") == ("cron", "*/5 * * * *")
    for bad in ("", "often", "every 0m", "every m", "* * * *", "61 * * * *"):
        with pytest.raises(ConfigError):
            parse_spec(bad)


def test_quiet_hours_are_a_window_of_two_clock_times() -> None:
    assert parse_quiet("22:00-07:00") == ("22:00", "07:00")
    assert parse_quiet("9:05-17:30") == ("09:05", "17:30")
    for bad in ("22:00", "25:00-07:00", "10:00-10:00", "22:00-07:60"):
        with pytest.raises(ConfigError):
            parse_quiet(bad)


async def test_a_schedule_below_the_floor_is_refused_when_it_is_set(w: World) -> None:
    with pytest.raises(ConfigError, match="minimum"):
        await w.svc.set("main", "every 30s")
    with pytest.raises(ConfigError, match="minimum"):
        await w.svc.set("main", "every 59s")
    assert await w.svc.get("main") is None  # nothing was stored


async def test_a_valid_schedule_is_stored_and_listed(w: World) -> None:
    s = await w.svc.set("main", "every 5m", timezone="Europe/Paris", quiet="22:00-07:00")
    assert (s.schedule, s.timezone, s.quiet, s.paused) == (
        "every 5m", "Europe/Paris", ("22:00", "07:00"), False,
    )  # fmt: skip
    listed = await w.svc.schedules()
    assert [(x.account_id, x.folder, x.schedule) for x in listed] == [("main", "", "every 5m")]
    assert (await w.svc.get("main")) == s


async def test_a_cron_line_that_runs_too_often_is_refused_too(w: World) -> None:
    w.svc.settings = SyncSettings(min_interval_seconds=300)
    with pytest.raises(ConfigError, match="minimum"):
        await w.svc.set("main", "*/2 * * * *")
    await w.svc.set("main", "*/5 * * * *")


async def test_bad_timezone_unknown_account_and_bad_quiet_hours_are_refused(w: World) -> None:
    with pytest.raises(ConfigError, match="timezone"):
        await w.svc.set("main", "every 5m", timezone="Mars/Olympus")
    with pytest.raises(NotFound):
        await w.svc.set("nobody", "every 5m")
    with pytest.raises(ConfigError, match="quiet"):
        await w.svc.set("main", "every 5m", quiet="late")


async def test_a_folder_schedule_needs_a_provider_with_folders(w: World) -> None:
    with pytest.raises(ConfigError, match="no folders"):
        await w.svc.set("main", "every 5m", folder="INBOX")
    await w.add_account("mailbox", "folders")
    s = await w.svc.set("mailbox", "every 5m", folder="INBOX")
    assert s.folder == "INBOX"
    await w.svc.set("mailbox", "every 1h")  # the whole account too, as a separate row
    assert [(x.folder, x.schedule) for x in await w.svc.schedules()] == [
        ("", "every 1h"), ("INBOX", "every 5m"),
    ]  # fmt: skip


async def test_changing_a_schedule_keeps_what_was_not_given(w: World) -> None:
    await w.svc.set("main", "every 5m", timezone="Europe/Paris", quiet="22:00-07:00")
    s = await w.svc.set("main", "every 10m")
    assert (s.timezone, s.quiet) == ("Europe/Paris", ("22:00", "07:00"))
    s = await w.svc.set("main", "every 10m", clear_quiet=True)
    assert s.quiet is None


async def test_a_new_account_gets_the_default_schedule_every_fifteen_minutes(w: World) -> None:
    assert await w.svc.ensure_schedules() == 1
    s = await w.svc.get("main")
    assert s is not None and (s.schedule, s.timezone, s.paused) == ("every 15m", "UTC", False)
    assert s.next_run == T0  # a new account syncs straight away
    assert await w.svc.ensure_schedules() == 0  # and an existing schedule is left alone
    await w.svc.set("main", "every 1h")
    assert await w.svc.ensure_schedules() == 0
    assert (await w.svc.get("main")).schedule == "every 1h"  # type: ignore[union-attr]


# -- firing -----------------------------------------------------------------------------------
async def test_two_schedules_fire_at_the_expected_times_in_their_own_timezones(w: World) -> None:
    await w.add_account("paris")
    w.rig.clock.set(datetime(2026, 3, 7, 12, 0, tzinfo=UTC))
    await w.svc.set("main", "0 9 * * *", timezone="America/New_York")
    await w.svc.set("paris", "0 9 * * *", timezone="Europe/Paris")
    nxt = {s.account_id: s.next_run for s in await w.svc.schedules()}
    assert nxt == {
        "main": datetime(2026, 3, 7, 14, 0, tzinfo=UTC),  # 09:00 EST
        "paris": datetime(2026, 3, 8, 8, 0, tzinfo=UTC),  # 09:00 CET: today's has passed
    }

    w.rig.clock.set(datetime(2026, 3, 7, 14, 0, tzinfo=UTC))
    assert await w.fire() == 1 and Mailbox.pulls == 1
    # New York's clocks go forward that night, so tomorrow 09:00 is an hour earlier in UTC.
    assert (await w.svc.get("main")).next_run == datetime(2026, 3, 8, 13, 0, tzinfo=UTC)  # type: ignore[union-attr]

    w.rig.clock.set(datetime(2026, 3, 8, 8, 0, tzinfo=UTC))
    assert await w.fire() == 1 and Mailbox.pulls == 2
    assert (await w.svc.get("paris")).next_run == datetime(2026, 3, 9, 8, 0, tzinfo=UTC)  # type: ignore[union-attr]

    w.rig.clock.set(datetime(2026, 3, 8, 13, 0, tzinfo=UTC))
    assert await w.fire() == 1 and Mailbox.pulls == 3


async def test_a_tick_fires_a_schedule_once_and_a_restart_neither_repeats_nor_skips(
    w: World, rig: Rig
) -> None:
    await w.svc.set("main", "every 15m", run_now=True)
    assert await w.fire() == 1
    assert await w.svc.tick() == 0  # same instant: nothing more

    # a restart: a brand new service over the same database
    again = await _world(
        rig, SyncSettings(), rng=random.Random(1), runner=JobRunner(rig.queue, workers=1)
    )
    try:
        rig.clock.advance(minutes=14)
        assert await again.svc.tick() == 0  # not due yet: not repeated
        rig.clock.advance(minutes=1)
        assert await again.svc.tick() == 1  # due exactly on time: not skipped
    finally:
        await again.host.stop()


async def test_three_missed_runs_make_one_catch_up_sync(w: World) -> None:
    await w.svc.set("main", "every 15m", run_now=True)
    await w.fire()
    assert Mailbox.pulls == 1
    w.rig.clock.advance(minutes=50)  # the laptop slept through three runs
    assert await w.fire() == 1
    assert Mailbox.pulls == 2  # one catch-up, not three
    s = await w.svc.get("main")
    assert s is not None and s.next_run > w.rig.clock.now()
    assert await w.fire() == 0  # and the rhythm resumes from now


async def test_a_slow_sync_does_not_pile_up_more_syncs_of_the_same_account(w: World) -> None:
    Mailbox.gate = asyncio.Event()
    await w.svc.set("main", "every 1m", run_now=True)
    await w.svc.tick()
    runner = asyncio.create_task(w.rig.runner.drain())
    await asyncio.sleep(0.05)
    for _ in range(3):
        w.rig.clock.advance(minutes=1)
        await w.svc.tick()  # due again while the first is still running
    Mailbox.gate.set()
    await runner
    await w.rig.runner.drain()
    assert Mailbox.pulls == 1


async def test_sync_now_during_a_running_sync_results_in_one_sync(w: World) -> None:
    Mailbox.gate = asyncio.Event()
    await w.svc.set("main", "every 15m", run_now=True)
    await w.svc.tick()
    runner = asyncio.create_task(w.rig.runner.drain())
    await asyncio.sleep(0.05)
    assert Mailbox.running == 1
    first = await w.svc.request("main")
    second = await w.svc.request("main", actor=SYSTEM, reason="idle")
    assert not first.created and not second.created  # both joined the running job
    Mailbox.gate.set()
    await runner
    await w.rig.runner.drain()
    assert Mailbox.pulls == 1


async def test_sync_now_with_nothing_running_starts_one_even_if_paused(w: World) -> None:
    await w.svc.set("main", "every 15m")
    await w.svc.pause("main")
    queued = await w.svc.request("main")
    assert queued.created
    await w.rig.runner.drain()
    assert Mailbox.pulls == 1
    with pytest.raises(NotFound):
        await w.svc.request("nobody")


async def test_a_folder_sync_pulls_just_that_folder(w: World) -> None:
    await w.add_account("mailbox", "folders")
    await w.svc.set("mailbox", "every 5m", folder="INBOX", run_now=True)
    await w.fire()
    history = w.provider("mailbox").history
    assert ("pull", None, "INBOX") in history


@pytest.mark.parametrize(("limit", "expected"), [(1, 1), (2, 2), (3, 3)])
async def test_at_most_max_concurrent_accounts_sync_at_once(
    rig: Rig, limit: int, expected: int
) -> None:
    runner = JobRunner(rig.queue, workers=3)  # plenty of workers: the limit is the sync cap
    w = await _world(rig, SyncSettings(max_concurrent=limit), runner=runner)
    try:
        for name in ("a", "b", "c"):
            await w.add_account(name)
            await w.svc.set(name, "every 15m", run_now=True)
        Mailbox.gate = asyncio.Event()
        await w.svc.tick()
        running = asyncio.create_task(runner.drain())
        await asyncio.sleep(0.1)
        assert Mailbox.running == expected  # the rest wait their turn
        Mailbox.gate.set()
        await running
        assert Mailbox.pulls == 3 and Mailbox.most == expected
    finally:
        await w.host.stop()


async def test_a_slow_pull_never_delays_a_mark_read(rig: Rig) -> None:
    """Sync and ``readstate.write`` are different job kinds with their own limits."""
    runner = JobRunner(rig.queue, workers=2)
    w = await _world(rig, SyncSettings(max_concurrent=1), runner=runner)
    try:
        # one sync running and holding its slot
        Mailbox.gate = asyncio.Event()
        await w.svc.set("main", "every 15m", run_now=True)
        ran: list[str] = []

        async def quick(payload: Any, job: Any) -> None:
            ran.append(job.kind)

        runner.register("readstate.write", _Nothing, quick)
        runner.start()  # two live workers, as in the daemon
        await w.svc.tick()
        await asyncio.sleep(0.05)
        assert Mailbox.running == 1
        await rig.queue.enqueue("readstate.write", _Nothing())
        await asyncio.sleep(0.1)
        assert ran == ["readstate.write"]  # done while the pull is still held open
        Mailbox.gate.set()
        await runner.stop(grace=1)
    finally:
        await w.host.stop()


# -- failures ---------------------------------------------------------------------------------
async def test_an_auth_failure_pauses_that_account_only_and_says_why(w: World) -> None:
    await w.add_account("other")
    for name in ("main", "other"):
        await w.svc.set(name, "every 15m", run_now=True)
    w.provider("main").fail_next("pull", AuthError("the server refused the login"))
    await w.fire()

    s = await w.svc.get("main")
    assert s is not None and s.paused and s.last_result == "auth"
    assert s.paused_reason.startswith("authentication failed")
    assert "refused the login" in s.paused_reason
    other = await w.svc.get("other")
    assert other is not None and not other.paused and other.last_result == "ok"

    (paused,) = w.rig.names(SyncPaused)
    assert (paused.account_id, "refused" in paused.reason) == ("main", True)
    (entry,) = await w.audit.recent(op="schedule.pause")
    assert entry.actor == SYSTEM and entry.reason == "auth_failed"
    assert entry.after["main"]["paused"] is True and entry.before["main"]["paused"] is False

    # no retries until the user acts
    w.rig.clock.advance(hours=3)
    assert await w.fire() == 1  # only "other" is due
    assert Mailbox.pulls == 3  # main once, other twice


async def test_resuming_clears_the_pause_and_the_account_syncs_again(w: World) -> None:
    await w.svc.set("main", "every 15m", run_now=True)
    w.provider("main").fail_next("pull", AuthError("no password"))
    await w.fire()
    s = await w.svc.resume("main")
    assert not s.paused and s.paused_reason == "" and s.failures == 0
    assert s.next_run == w.rig.clock.now()  # due now
    await w.fire()
    s2 = await w.svc.get("main")
    assert s2 is not None and s2.last_result == "ok"
    ops = [e.op for e in await w.audit.recent(op=None)]
    assert "schedule.resume" in ops and "schedule.pause" in ops


async def test_a_good_manual_sync_clears_an_authentication_pause(w: World) -> None:
    await w.svc.set("main", "every 15m", run_now=True)
    w.provider("main").fail_next("pull", AuthError("no password"))
    await w.fire()
    assert (await w.svc.get("main")).paused  # type: ignore[union-attr]
    await w.svc.request("main")  # the user fixed the password and clicked "sync now"
    await w.rig.runner.drain()
    s = await w.svc.get("main")
    assert s is not None and not s.paused and s.last_result == "ok"


async def test_other_failures_back_off_exponentially_and_a_success_resets_them(w: World) -> None:
    await w.svc.set("main", "every 15m", run_now=True)
    w.provider("main").fail_next("pull", RuntimeError("boom"), times=2)
    await w.fire()
    s = await w.svc.get("main")
    assert s is not None and (s.failures, s.last_result) == (1, "error")
    assert s.last_error is not None and "boom" in s.last_error and not s.paused
    assert s.next_run == w.rig.clock.now() + timedelta(seconds=1800)  # 900 * 2

    w.rig.clock.advance(seconds=1800)
    await w.fire()
    s = await w.svc.get("main")
    assert s is not None and s.failures == 2
    assert s.next_run == w.rig.clock.now() + timedelta(seconds=3600)  # 900 * 4, at the cap

    w.rig.clock.advance(seconds=3600)
    await w.fire()  # the third try works
    s = await w.svc.get("main")
    assert s is not None and (s.failures, s.last_result, s.last_error) == (0, "ok", None)


async def test_backoff_has_jitter_and_a_cap(rig: Rig) -> None:
    w = await _world(
        rig, SyncSettings(backoff_jitter=0.2, backoff_max_seconds=2000), rng=random.Random(7)
    )
    try:
        await w.svc.set("main", "every 15m", run_now=True)
        w.provider("main").fail_next("pull", RuntimeError("boom"), times=9)
        waits = set()
        for _ in range(4):
            await w.fire()
            s = await w.svc.get("main")
            assert s is not None
            waits.add(round((s.next_run - rig.clock.now()).total_seconds()))
            rig.clock.set(s.next_run)
        assert len(waits) > 1  # jittered
        assert all(2000 * 0.8 <= x <= 2000 * 1.2 for x in list(waits)[-1:])  # capped, then jittered
    finally:
        await w.host.stop()


async def test_an_account_whose_provider_is_unavailable_is_reported_not_fatal(w: World) -> None:
    await w.rig.raw.create_account("lost", "no-such-plugin", "Lost")
    await w.accounts.start()
    await w.svc.set("lost", "every 15m", run_now=True)
    await w.svc.set("main", "every 15m", run_now=True)
    await w.fire()
    lost = await w.svc.get("lost")
    assert lost is not None and lost.last_result == "error" and not lost.paused
    assert lost.problem is not None and "no-such-plugin" in lost.problem
    main = await w.svc.get("main")
    assert main is not None and main.last_result == "ok"


# -- live edits, audit and export --------------------------------------------------------------
async def test_every_edit_is_audited_with_actor_before_and_after(w: World) -> None:
    await w.svc.set("main", "every 15m", actor=Actor("user"), reason="user_set_schedule")
    await w.svc.set("main", "every 1h", timezone="Europe/Paris", actor=AGENT, reason="agent_edit")
    await w.svc.pause("main", why="travelling")
    await w.svc.resume("main")
    await w.svc.remove("main")
    entries = list(reversed(await w.audit.recent(10)))
    assert [e.op for e in entries] == [
        "schedule.set", "schedule.set", "schedule.pause", "schedule.resume", "schedule.remove",
    ]  # fmt: skip
    first, second, paused, _resumed, removed = entries
    assert first.before == {} and first.after["main"]["schedule"] == "every 15m"
    assert second.actor == AGENT and second.reason == "agent_edit"
    assert second.before["main"]["schedule"] == "every 15m"
    assert second.after["main"] == {
        "schedule": "every 1h", "timezone": "Europe/Paris", "quiet": None,
        "paused": False, "paused_reason": "",
    }  # fmt: skip
    assert paused.after["main"]["paused_reason"] == "travelling"
    assert removed.after == {"main": {}}


async def test_a_live_edit_takes_effect_without_a_restart(w: World) -> None:
    w.svc._stop.clear()
    task = asyncio.create_task(w.svc._loop())
    try:
        await w.svc.set("main", "every 1h", run_now=True)  # wakes the loop
        for _ in range(100):
            await asyncio.sleep(0.01)
            if (await w.svc.get("main")).last_result is None and await w.rig.runner.drain():  # type: ignore[union-attr]
                break
        assert Mailbox.pulls == 1
        assert [e.change for e in w.rig.names(SyncScheduleChanged)] == ["added"]
        await w.svc.pause("main")
        w.rig.clock.advance(hours=5)
        await asyncio.sleep(0.1)
        assert await w.rig.runner.drain() == 0  # paused: nothing queued however late it gets
    finally:
        w.svc._stop.set()
        w.svc._wake.set()
        await task


async def test_pausing_keeps_the_schedule_and_resuming_makes_it_due_now(w: World) -> None:
    await w.svc.set("main", "every 15m")
    s = await w.svc.pause("main")
    assert s.paused and s.paused_reason == "paused by the user"
    w.rig.clock.advance(hours=1)
    assert await w.fire() == 0
    s = await w.svc.resume("main")
    assert not s.paused and s.next_run == w.rig.clock.now()
    assert await w.fire() == 1
    with pytest.raises(NotFound):
        await w.svc.pause("nobody")


async def test_the_crontab_export_is_readable_and_read_only(w: World) -> None:
    await w.add_account("mailbox", "folders")
    await w.add_account("slow")
    await w.svc.set("main", "every 15m", timezone="Europe/Paris")
    await w.svc.set("mailbox", "every 7m", folder="INBOX")
    await w.svc.set("slow", "every 6h")
    await w.svc.pause("slow")
    text = await w.svc.crontab()
    assert "CRON_TZ=Europe/Paris\n*/15 * * * * oaken sync run main" in text
    assert "# every 7m (no cron equivalent) oaken sync run mailbox --folder 'INBOX'" in text
    assert "# 0 */6 * * * oaken sync run slow" in text and "PAUSED" in text
    assert text.startswith("# Oaken Mail sync schedule (export only")


# -- quiet hours ------------------------------------------------------------------------------
async def test_quiet_hours_do_not_stop_a_sync_they_only_mark_it(w: World) -> None:
    await w.svc.set("main", "every 15m", timezone="America/New_York", quiet="22:00-07:00")
    # 03:00 UTC is 22:00 in New York (EST): quiet. The sync still runs.
    w.rig.clock.set(datetime(2026, 1, 2, 3, 0, tzinfo=UTC))
    await w.svc.request("main")
    await w.rig.runner.drain()
    # 15:00 UTC is 10:00 there: not quiet.
    w.rig.clock.set(datetime(2026, 1, 2, 15, 0, tzinfo=UTC))
    await w.svc.request("main")
    await w.rig.runner.drain()
    done = w.rig.names(SyncFinished)
    assert [e.quiet for e in done] == [True, False]
    assert Mailbox.pulls == 2


async def test_quiet_hours_that_do_not_cross_midnight(w: World) -> None:
    s = await w.svc.set("main", "every 15m", quiet="13:00-14:00")
    assert not s.in_quiet_hours(datetime(2026, 1, 2, 12, 59, tzinfo=UTC))
    assert s.in_quiet_hours(datetime(2026, 1, 2, 13, 0, tzinfo=UTC))
    assert not s.in_quiet_hours(datetime(2026, 1, 2, 14, 0, tzinfo=UTC))


# -- the whole thing ----------------------------------------------------------------------------
async def test_end_to_end_a_scheduled_sync_ingests_mail_inbound_flags_and_writes_back(
    rig: Rig,
) -> None:
    """Providers, SyncRunner with flag_sink, ReadState, resume_pending, then the schedule."""
    host = PluginHost(Settings(), rig.clock, rig.bus, discover=_discover, health_interval=3600)
    await host.start()
    try:
        accounts = AccountProviders(host, rig.raw)
        await accounts.start()
        provider = accounts.get(str(ACCT))
        assert isinstance(provider, FakeProvider)

        async def never(_delay: float) -> None:
            await asyncio.Event().wait()

        audit = AuditLog(rig.raw_db, rig.clock)
        rs = ReadState(
            rig.raw, rig.queue, audit, rig.clock, Settings(read_state=ReadStateSettings()),
            bus=rig.bus, provider_for=providers_from_accounts(accounts), sleep=never,
        )  # fmt: skip
        rs.register(rig.runner)
        rig.sync.flag_sink = rs.apply_server_flags
        svc = SyncService(
            rig.raw, rig.sync, accounts, audit, rig.clock, SyncSettings(),
            bus=rig.bus, after_providers=[rs.resume_pending],
        )  # fmt: skip
        svc.register(rig.runner)

        # an earlier run left a mark-read that never reached the server
        for i in range(2):
            provider.add_message(mail(i))
        await rig.sync.sync(ACCT, provider)
        await rig.drain()
        first, second = await rig.mid("fake-1"), await rig.mid("fake-2")
        await rs.mark_read([first], reason="user_marked_read")
        await rig.raw_db.execute("DELETE FROM jobs WHERE kind = 'readstate.write'")  # a "crash"
        assert "\\Seen" not in provider.messages["fake-1"].provider_flags

        provider.add_message(mail(2))  # new mail
        provider.server_flags("fake-2", add={"\\Seen"})  # read on another device

        problems = await svc.start()  # providers -> resume_pending -> schedule loop
        try:
            assert problems == {}
            for _ in range(200):
                await asyncio.sleep(0.01)
                await rig.runner.drain()
                if await rig.raw.message_id_for(ACCT, "fake-3"):
                    break
            await rig.runner.drain()
            assert await rig.raw.message_id_for(ACCT, "fake-3") is not None  # ingested
            assert "\\Seen" in provider.messages["fake-1"].provider_flags  # resumed and written
            row = await rig.raw_db.fetchone(
                "SELECT is_read FROM message_state WHERE message_id = ?", (second,)
            )
            assert row is not None and row["is_read"]  # the inbound flag arrived
            s = await svc.get(str(ACCT))
            assert s is not None and s.last_result == "ok" and s.schedule == "every 15m"
            assert s.last_run is not None and fmt_ts(s.last_run) == fmt_ts(rig.clock.now())
        finally:
            await svc.stop()
    finally:
        await host.stop()


async def test_a_send_only_account_gets_no_sync_schedule(rig: Rig) -> None:
    from oaken.plugins.base import BaseProvider

    class Sender(BaseProvider):
        version = "1"

        async def send(self, raw_bytes: bytes) -> str:
            return "id"

    def discover(group: str) -> Iterable[EntryPoint]:
        if group != "oaken.providers":
            return []
        return [
            EntryPoint("fake", f"{__name__}:Mailbox", group),
            EntryPoint("sender", f"{__name__}:_Sender", group),
        ]

    globals()["_Sender"] = Sender
    host = PluginHost(Settings(), rig.clock, rig.bus, discover=discover, health_interval=3600)
    await host.start()
    try:
        await rig.raw.create_account("out", "sender", "Out")
        accounts = AccountProviders(host, rig.raw)
        await accounts.start()
        svc = SyncService(rig.raw, rig.sync, accounts, AuditLog(rig.raw_db, rig.clock), rig.clock)
        assert await svc.ensure_schedules() == 1  # "main" only
        assert [s.account_id for s in await svc.schedules()] == ["main"]
    finally:
        await host.stop()


__all__ = ["ACCT", "SYNC_KIND", "Options", "build"]
