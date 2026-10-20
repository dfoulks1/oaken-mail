# SPDX-License-Identifier: Apache-2.0
"""Operations over the sync schedule (prompt 12b). The schedule is data in the database; these are
how a client reads and edits it. ``SyncService`` audits every edit itself, so these ask it to.

Editing a schedule is never offered over MCP: an agent that can stretch or pause the sync can hide
new mail from the user.
"""

from datetime import datetime

from pydantic import BaseModel

from oaken.core.operations.context import OpContext
from oaken.core.operations.registry import AnyOperation, operation
from oaken.ingest.schedule import SyncSchedule


class ScheduleInfo(BaseModel):
    account_id: str
    folder: str
    schedule: str  # "every 15m" or the cron expression
    timezone: str
    quiet: str | None = None  # "22:00-07:00": alerts are muted, the sync still runs
    paused: bool
    paused_reason: str = ""
    next_run: datetime
    last_run: datetime | None = None
    last_result: str | None = None
    last_error: str | None = None
    last_new: int | None = None
    failures: int = 0
    problem: str | None = None  # why the account cannot sync right now


def schedule_info(s: SyncSchedule) -> ScheduleInfo:
    return ScheduleInfo(
        account_id=s.account_id,
        folder=s.folder,
        schedule=s.schedule,
        timezone=s.timezone,
        quiet="-".join(s.quiet) if s.quiet else None,
        paused=s.paused,
        paused_reason=s.paused_reason,
        next_run=s.next_run,
        last_run=s.last_run,
        last_result=s.last_result,
        last_error=s.last_error,
        last_new=s.last_new,
        failures=s.failures,
        problem=s.problem,
    )


class ListSchedulesInput(BaseModel):
    crontab: bool = False  # also return the read-only crontab export


class ListSchedulesOutput(BaseModel):
    schedules: list[ScheduleInfo]
    crontab: str | None = None


@operation(
    "list_schedules",
    "Every sync schedule: when it next runs, how the last run went, and whether it is paused.",
    permission="read",
    input=ListSchedulesInput,
    output=ListSchedulesOutput,
    mcp="on",
)
async def list_schedules(ctx: OpContext, inp: ListSchedulesInput) -> ListSchedulesOutput:
    sync = ctx.svc.need_sync()
    return ListSchedulesOutput(
        schedules=[schedule_info(s) for s in await sync.schedules()],
        crontab=await sync.crontab() if inp.crontab else None,
    )


class ScheduleKey(BaseModel):
    account_id: str
    folder: str = ""


class SetScheduleInput(ScheduleKey):
    schedule: str  # "every 15m" or a five-field cron expression
    timezone: str | None = None  # an IANA name; unchanged if omitted
    quiet: str | None = None  # "22:00-07:00"
    clear_quiet: bool = False
    run_now: bool = False


class ScheduleOutput(BaseModel):
    schedule: ScheduleInfo


@operation(
    "set_schedule",
    "Create or change a sync schedule. The floor and the timezone are checked now.",
    permission="mutate",
    input=SetScheduleInput,
    output=ScheduleOutput,
    mcp="never",
    audit="service",
)
async def set_schedule(ctx: OpContext, inp: SetScheduleInput) -> ScheduleOutput:
    s = await ctx.svc.need_sync().set(
        inp.account_id,
        inp.schedule,
        folder=inp.folder,
        timezone=inp.timezone,
        quiet=inp.quiet,
        clear_quiet=inp.clear_quiet,
        actor=ctx.caller.actor,
        reason=f"{ctx.caller.client}_set_schedule",
        run_now=inp.run_now,
    )
    return ScheduleOutput(schedule=schedule_info(s))


class PauseInput(ScheduleKey):
    why: str = "paused by the user"


@operation(
    "pause_schedule",
    "Pause a sync schedule until it is resumed.",
    permission="mutate",
    input=PauseInput,
    output=ScheduleOutput,
    mcp="never",
    audit="service",
)
async def pause_schedule(ctx: OpContext, inp: PauseInput) -> ScheduleOutput:
    s = await ctx.svc.need_sync().pause(
        inp.account_id,
        inp.folder,
        why=inp.why,
        actor=ctx.caller.actor,
        reason=f"{ctx.caller.client}_pause_schedule",
    )
    return ScheduleOutput(schedule=schedule_info(s))


@operation(
    "resume_schedule",
    "Resume a paused sync schedule: it is due now and its failure count is cleared.",
    permission="mutate",
    input=ScheduleKey,
    output=ScheduleOutput,
    mcp="never",
    audit="service",
)
async def resume_schedule(ctx: OpContext, inp: ScheduleKey) -> ScheduleOutput:
    s = await ctx.svc.need_sync().resume(
        inp.account_id,
        inp.folder,
        actor=ctx.caller.actor,
        reason=f"{ctx.caller.client}_resume_schedule",
    )
    return ScheduleOutput(schedule=schedule_info(s))


class RemovedOutput(BaseModel):
    removed: bool


@operation(
    "remove_schedule",
    "Remove a sync schedule (a per-folder one, or an account's, which stops its sync).",
    permission="mutate",
    input=ScheduleKey,
    output=RemovedOutput,
    mcp="never",
    audit="service",
)
async def remove_schedule(ctx: OpContext, inp: ScheduleKey) -> RemovedOutput:
    done = await ctx.svc.need_sync().remove(inp.account_id, inp.folder, actor=ctx.caller.actor)
    return RemovedOutput(removed=done)


OPERATIONS: tuple[AnyOperation, ...] = (
    list_schedules,
    set_schedule,
    pause_schedule,
    resume_schedule,
    remove_schedule,
)
