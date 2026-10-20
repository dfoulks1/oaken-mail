# SPDX-License-Identifier: Apache-2.0
"""``SyncRunner``: pull from a provider, store, and hand each message to the pipeline.

The order that makes it safe to stop at any moment: a batch's messages are stored first, a job
is queued for each, and only then is the batch's cursor saved. A crash in between means the batch
is pulled again, which is harmless (storing is idempotent and jobs are deduplicated per message).
Core, not the provider, owns all of this: a provider only returns batches.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from oaken.core.clock import Clock
from oaken.core.events import EventBus, SyncFinished, SyncProgress, SyncStarted
from oaken.core.ids import AccountId, MessageId
from oaken.core.logging import get_logger
from oaken.core.rawstore import MessageMeta, RawStore
from oaken.ingest.pipeline import Pipeline
from oaken.plugins.adapt import iter_pull
from oaken.plugins.contracts import FlagChange, Provider

log = get_logger("oaken.sync")

SCOPE = "mail"
BACKFILL_SCOPE = "mail.backfill"  # "active" from the first pull until one reaches the end
SEEN, FLAGGED = "\\Seen", "\\Flagged"

# Told about server-side flag changes, already resolved to message ids (read state is prompt 12).
FlagSink = Callable[[AccountId, list[tuple[MessageId, FlagChange]]], Awaitable[None]]


@dataclass
class SyncResult:
    stored: int = 0  # messages that were new or different
    unchanged: int = 0
    deleted: int = 0
    flag_changes: int = 0
    batches: int = 0
    paused: bool = False  # backfill stopped at the queue cap; the next run continues
    cursor: str | None = None
    errors: list[str] = field(default_factory=list)


class SyncRunner:
    def __init__(
        self,
        raw: RawStore,
        pipeline: Pipeline,
        clock: Clock,
        *,
        bus: EventBus | None = None,
        flag_sink: FlagSink | None = None,
    ) -> None:
        self.raw, self.pipeline, self.clock, self.bus = raw, pipeline, clock, bus
        self.flag_sink = flag_sink
        self._locks: dict[str, asyncio.Lock] = {}

    async def sync(self, account_id: AccountId, provider: Provider, *, who: str = "") -> SyncResult:
        """One pull of one account, to the end or until the backfill cap. Syncs of the same
        account never overlap: a second call waits for the first."""
        lock = self._locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            return await self._sync(account_id, provider, who or type(provider).__name__)

    async def _sync(self, account_id: AccountId, provider: Provider, who: str) -> SyncResult:
        out = SyncResult()
        cursor = await self.raw.get_cursor(account_id, SCOPE)
        out.cursor = cursor
        # The first pull of an account is a backfill, and stays one across pauses until a pull
        # reaches the end; every later pull is new mail.
        active = await self.raw.get_cursor(account_id, BACKFILL_SCOPE) == "active"
        backfill = cursor is None or active
        if backfill and not active:
            await self.raw.set_cursor(account_id, BACKFILL_SCOPE, "active")
        await self._publish(SyncStarted(ts=self.clock.now(), account_id=account_id))
        error: str | None = None
        try:
            async for batch in iter_pull(provider, cursor, who):
                if backfill and await self.pipeline.backfill_paused():
                    out.paused = True
                    break
                await self._apply(account_id, batch, backfill, out)
                out.batches += 1
                if batch.next_cursor and batch.next_cursor != out.cursor:
                    await self.raw.set_cursor(account_id, SCOPE, batch.next_cursor)  # only now
                    out.cursor = batch.next_cursor
                await self._publish(
                    SyncProgress(
                        ts=self.clock.now(), account_id=account_id, done=out.stored + out.unchanged
                    )
                )
            if backfill and not out.paused:
                await self.raw.set_cursor(account_id, BACKFILL_SCOPE, "done")
        except Exception as exc:  # a provider plugin may raise anything; report it, keep going
            error = f"{type(exc).__name__}: {exc}"
            out.errors.append(error)
            log.warning("sync failed", account_id=account_id, error=type(exc).__name__)
        await self._publish(
            SyncFinished(
                ts=self.clock.now(), account_id=account_id, new_messages=out.stored, error=error
            )
        )
        return out

    async def _apply(self, account_id: AccountId, batch, backfill: bool, out: SyncResult) -> None:  # type: ignore[no-untyped-def]
        for m in batch.messages:
            res = await self.raw.put(
                account_id,
                m.provider_id,
                m.raw_bytes,
                MessageMeta(
                    internal_date=m.internal_date,
                    is_read=SEEN in m.provider_flags,
                    is_starred=FLAGGED in m.provider_flags,
                    labels=m.provider_labels,
                ),
            )
            if res.changed:
                out.stored += 1
                await self.pipeline.enqueue_parse(
                    res.message_id, account_id, changed=True, backfill=backfill
                )
            else:
                out.unchanged += 1
        for pid in batch.deleted_provider_ids:
            if await self.raw.mark_deleted_by_provider_id(account_id, pid):
                out.deleted += 1
                mid = await self.raw.message_id_for(account_id, pid)
                if mid:  # the parse stage removes it from the index and the vector store
                    await self.pipeline.enqueue_parse(mid, account_id, backfill=False)
        if batch.flag_changes:
            resolved: list[tuple[MessageId, FlagChange]] = []
            for fc in batch.flag_changes:
                mid = await self.raw.message_id_for(account_id, fc.provider_id)
                if mid:
                    resolved.append((mid, fc))
            out.flag_changes += len(resolved)
            if self.flag_sink and resolved:
                await self.flag_sink(account_id, resolved)

    async def _publish(self, event: SyncStarted | SyncProgress | SyncFinished) -> None:
        if self.bus:
            await self.bus.publish(event)
