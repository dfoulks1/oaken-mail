# SPDX-License-Identifier: Apache-2.0
"""Housekeeping a person can see and ask for: storage figures, vacuum, the enrichment cache, and
the recent event history.

Sizes come from the filesystem, never from a scan of mail. The cache is derived data (model answers
keyed by what produced them), so clearing it costs only the model calls it saved; it never touches
mail or the index. None of these is offered over MCP.
"""

from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from oaken.core.errors import Conflict, StorageError
from oaken.core.maintenance import check_room, enqueue_vacuum, file_bytes
from oaken.core.operations.builtin.mail import QueuedOutput
from oaken.core.operations.context import OpContext
from oaken.core.operations.registry import AnyOperation, operation


class Empty(BaseModel):
    pass


# -- storage ----------------------------------------------------------------------------------
class StorageInfo(BaseModel):
    data_dir: str
    raw_db: str
    raw_db_bytes: int
    index_db: str
    index_db_bytes: int
    wal_bytes: int  # both databases' write-ahead logs: space that vacuum or a checkpoint returns
    quarantine_dir: str | None = None
    quarantined_files: int = 0
    quarantine_bytes: int = 0


@operation(
    "storage_info",
    "Where Oaken keeps its data and how big it is: the two databases, their write-ahead logs and "
    "the quarantine folder. Read from the filesystem, not by scanning mail.",
    permission="read",
    input=Empty,
    output=StorageInfo,
    mcp="never",
)
async def storage_info(ctx: OpContext, inp: Empty) -> StorageInfo:
    raw, index = ctx.svc.raw.db.path, ctx.svc.index.db.path
    quarantine = ctx.svc.pipeline.quarantine if ctx.svc.pipeline is not None else None
    stats = quarantine.stats() if quarantine is not None else None
    wal = sum(file_bytes(Path(f"{p}-wal")) for p in (raw, index))
    return StorageInfo(
        data_dir=str(raw.parent),
        raw_db=str(raw),
        raw_db_bytes=file_bytes(raw),
        index_db=str(index),
        index_db_bytes=file_bytes(index),
        wal_bytes=wal,
        quarantine_dir=str(quarantine.root) if quarantine is not None else None,
        quarantined_files=stats.files if stats else 0,
        quarantine_bytes=stats.bytes if stats else 0,
    )


@operation(
    "vacuum",
    "Rewrite raw.db and index.db to give back the space deleted rows left. One at a time, and not "
    "while a rebuild runs. It holds each database's write lock while it works (ingest waits, "
    "reads go on) and needs about the database's own size free on the disk.",
    permission="mutate",
    input=Empty,
    output=QueuedOutput,
    mcp="never",
    streaming=True,
)
async def vacuum(ctx: OpContext, inp: Empty) -> QueuedOutput:
    for status in ("queued", "running"):
        if await ctx.svc.queue.query(status=status, kind="index.rebuild", limit=1):
            raise Conflict("an index rebuild is running; vacuum when it has finished")
    try:
        check_room(ctx.svc.raw.db, ctx.svc.index.db)
    except StorageError as exc:
        raise Conflict(str(exc)) from None
    queued = await enqueue_vacuum(ctx.svc.queue, actor=ctx.caller.actor)
    return QueuedOutput(job_id=queued.job_id, created=queued.created)


# -- the enrichment cache ----------------------------------------------------------------------
class CacheStats(BaseModel):
    entries: int
    bytes: int  # the stored answers, not the file around them
    by_task: dict[str, int] = Field(default_factory=dict)
    prompt_versions: list[str] = Field(default_factory=list)
    oldest: datetime | None = None
    hits: int  # model calls the cache made unnecessary, all time
    misses: int  # model calls made
    hit_rate: float | None = None  # hits / (hits + misses); None before any call


@operation(
    "cache_stats",
    "The enrichment cache: how many answers it holds, how big, from which prompt versions, and how "
    "often it saved a model call.",
    permission="read",
    input=Empty,
    output=CacheStats,
    mcp="never",
)
async def cache_stats(ctx: OpContext, inp: Empty) -> CacheStats:
    stats = await ctx.svc.cache().stats()
    calls = await ctx.svc.raw.db.fetchone(
        "SELECT COALESCE(SUM(cached = 1), 0) AS hits, COALESCE(SUM(cached = 0), 0) AS misses"
        " FROM ai_usage"
    )
    hits, misses = int(calls["hits"]), int(calls["misses"])  # type: ignore[index]
    return CacheStats(
        entries=stats.entries,
        bytes=stats.bytes,
        by_task=stats.by_task,
        prompt_versions=list(stats.prompt_versions),
        oldest=stats.oldest,
        hits=hits,
        misses=misses,
        hit_rate=(hits / (hits + misses)) if hits + misses else None,
    )


class ClearCacheInput(BaseModel):
    task: str | None = None  # only this task's answers (summary, triage, ...); default: all


class ClearCacheOutput(BaseModel):
    deleted: int


@operation(
    "clear_cache",
    "Delete cached model answers (all, or one task's). Only derived results go: mail, the index "
    "and the usage record are untouched, and anything cleared is recomputed the next time it is "
    "asked for, at the cost of the model calls the cache had saved.",
    permission="mutate",
    input=ClearCacheInput,
    output=ClearCacheOutput,
    mcp="never",
)
async def clear_cache(ctx: OpContext, inp: ClearCacheInput) -> ClearCacheOutput:
    return ClearCacheOutput(deleted=await ctx.svc.cache().clear(task=inp.task))


# -- the usage record --------------------------------------------------------------------------
class ResetUsageOutput(BaseModel):
    deleted: int  # rows cleared across the usage tables


@operation(
    "reset_usage",
    "Clear the token-usage record (the figures the stats page shows). Model calls and the context "
    "savings both go; mail, the index and the enrichment cache are untouched. The accounting starts "
    "fresh from the next call.",
    permission="mutate",
    input=Empty,
    output=ResetUsageOutput,
    mcp="never",
)
async def reset_usage(ctx: OpContext, inp: Empty) -> ResetUsageOutput:
    db = ctx.svc.raw.db
    deleted = await db.execute("DELETE FROM ai_usage")
    deleted += await db.execute("DELETE FROM context_savings")
    return ResetUsageOutput(deleted=deleted)


# -- the event history -------------------------------------------------------------------------
class EventsInput(BaseModel):
    limit: int = Field(default=100, ge=1, le=1000)
    since: datetime | None = None  # nothing older than this
    kinds: list[str] | None = None  # only these kinds (JobStateChanged, MessageStored, ...)


class EventRow(BaseModel):
    ts: datetime
    kind: str
    data: dict[str, Any]  # the event's fields: ids, states and numbers, never mail text


class EventsOutput(BaseModel):
    events: list[EventRow]  # oldest first


@operation(
    "events_tail",
    "The most recent events core published, from its event log: what happened before this page "
    "was opened, or before core was last restarted. Ids, states and numbers, never mail text.",
    permission="read",
    input=EventsInput,
    output=EventsOutput,
    mcp="never",
)
async def events_tail(ctx: OpContext, inp: EventsInput) -> EventsOutput:
    rows = ctx.svc.need_eventlog().tail(
        inp.limit, since=inp.since, kinds=set(inp.kinds) if inp.kinds else None
    )
    return EventsOutput(events=[EventRow.model_validate(r) for r in rows])


OPERATIONS: tuple[AnyOperation, ...] = (
    storage_info,
    vacuum,
    cache_stats,
    clear_cache,
    reset_usage,
    events_tail,
)
