# SPDX-License-Identifier: Apache-2.0
"""Index a stored message, keep its organization in step, and rebuild the whole index.

``Indexer`` is the one place that reads ``raw.db`` and writes ``index.db``. Prompt 09 calls
``index_one`` as a pipeline stage; ``rebuild_index`` walks every message again (resumable, as a
job) and is how a change in the cleaning rules, the chunk size or the pipeline reaches old mail.
Nothing here calls a model, and message text never reaches a log.
"""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel

from oaken.core.actors import SYSTEM, Actor
from oaken.core.attrs import AttrStore
from oaken.core.clock import Clock, fmt_ts, parse_ts
from oaken.core.events import (
    EventBus,
    IndexRebuildProgress,
    MessageFiltered,
    MessageIndexed,
    MessageParsed,
)
from oaken.core.ids import AccountId, MessageId
from oaken.core.jobs import PRIORITY_BACKFILL, Enqueued, Job, JobQueue, JobRunner
from oaken.core.logging import get_logger
from oaken.core.rawstore import RawStore
from oaken.core.settings import MailSettings
from oaken.index.analyze import analyze
from oaken.index.query import DISPOSITIONS
from oaken.index.store import IndexResult, IndexStore
from oaken.mail.tokens import Tokenizer, load_tokenizer

log = get_logger("oaken.index")

REBUILD_KIND = "index.rebuild"
PROGRESS_EVERY = 100
CHECKPOINT_EVERY = 25


@dataclass
class RebuildResult:
    examined: int = 0
    indexed: int = 0  # new or changed
    unchanged: int = 0
    failed: int = 0
    pruned: int = 0  # indexed messages that no longer exist or were deleted
    resumed: bool = False
    errors: list[str] = field(default_factory=list)


class Indexer:
    def __init__(
        self,
        raw: RawStore,
        index: IndexStore,
        attrs: AttrStore,
        settings: MailSettings,
        clock: Clock,
        *,
        tokenizer: Tokenizer | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self.raw, self.index, self.attrs, self.settings, self.clock, self.bus = (
            raw,
            index,
            attrs,
            settings,
            clock,
            bus,
        )
        self.tokenizer = tokenizer or load_tokenizer(settings)

    # -- one message ------------------------------------------------------------------------
    async def organization(self, message_id: MessageId) -> tuple[str, list[str]]:
        """``(disposition, labels)`` from the message's attributes in ``raw.db``. A disposition
        set by anything other than a script (the user, later) wins over the script's."""
        dispositions: dict[str, str] = {}
        labels: set[str] = set()
        for a in await self.attrs.get(message_id):
            if a.kind == "label":
                labels.add(a.value)
            elif a.kind == "disposition" and a.value in DISPOSITIONS:
                dispositions[a.source] = a.value
        chosen = next(
            (v for s, v in sorted(dispositions.items()) if s != "sieve"),
            dispositions.get("sieve", "inbox"),
        )
        return chosen, sorted(labels)

    async def index_one(self, message_id: MessageId, *, force: bool = False) -> IndexResult | None:
        """Parse, clean, chunk and index one message. A deleted message is removed from the index
        instead (and None is returned). Idempotent."""
        msg = await self.raw.get_message(message_id)
        if msg.deleted_at is not None:
            await self.index.delete_message(message_id)
            return None
        raw = await self.raw.get_raw(message_id)
        analysis = await asyncio.to_thread(
            analyze,
            raw,
            self.settings,
            tokenizer=self.tokenizer,
            fallback_date=msg.internal_date,
        )
        await self._publish(
            MessageParsed(ts=self.clock.now(), account_id=msg.account_id, message_id=message_id)
        )
        disposition, labels = await self.organization(message_id)
        result = await self.index.index_message(
            message_id,
            msg.account_id,
            analysis,
            disposition=disposition,
            labels=labels,
            force=force,
        )
        if result.changed:
            await self._publish(
                MessageIndexed(
                    ts=self.clock.now(), account_id=msg.account_id, message_id=message_id
                )
            )
        log.debug(
            "message indexed",
            message_id=message_id,
            changed=result.changed,
            chunks_written=result.chunks_written,
            joined=result.joined,
        )
        return result

    async def _publish(self, event: MessageParsed | MessageIndexed) -> None:
        if self.bus:
            await self.bus.publish(event)

    # -- keeping organization in step -------------------------------------------------------
    def follow_filtering(self) -> Callable[[], None]:
        """Refresh the index's copy of the disposition and labels whenever Sieve runs. Returns
        the function that stops following."""
        if self.bus is None:
            raise ValueError("an event bus is needed to follow filtering")

        async def on_filtered(event: MessageFiltered) -> None:
            disposition, labels = await self.organization(event.message_id)
            await self.index.set_organization(event.message_id, disposition, labels)

        return self.bus.subscribe(MessageFiltered, on_filtered)  # type: ignore[arg-type]

    # -- rebuild ----------------------------------------------------------------------------
    async def rebuild(
        self,
        *,
        account_id: AccountId | None = None,
        since: datetime | None = None,
        force: bool = False,
    ) -> RebuildResult:
        """Walk the raw store oldest first and index every message again. Messages whose content
        hash did not change cost a parse and no write. Resumable: progress is checkpointed in
        ``index_meta``, so a run that dies continues where it stopped; it ends by pruning index
        entries whose message is gone (only when ``since`` is not given). A stale index (older
        ``pipeline_version``) is emptied first."""
        out = RebuildResult()
        if self.index.stale:
            await self.index.reset()
        accounts = [account_id] if account_id else [a.id for a in await self.raw.list_accounts()]
        for acct in accounts:
            await self._rebuild_account(AccountId(acct), since, force, out)
        if since is None:
            out.pruned = await self._prune(account_id)
        return out

    async def _rebuild_account(
        self, acct: AccountId, since: datetime | None, force: bool, out: RebuildResult
    ) -> None:
        key = f"rebuild_cursor:{acct}"
        start = since
        after: MessageId | None = None
        saved = await self.index.get_meta(key)
        if saved:
            cur = json.loads(saved)
            if cur.get("since") == (fmt_ts(since) if since else ""):
                start, after = parse_ts(cur["at"]), MessageId(cur["id"])
                out.resumed = True
        total = await self.raw.count_messages(acct, since=start)
        done = indexed = failed = 0
        last: datetime | None = None
        async for msg in self.raw.iter_messages(acct, since=start, after_id=after):
            try:
                res = await self.index_one(msg.id, force=force)
                if res is not None and res.changed:
                    indexed += 1
                    out.indexed += 1
                else:
                    out.unchanged += 1
            except Exception as exc:  # noqa: BLE001 - one bad message must not stop the rebuild
                failed += 1
                out.failed += 1
                out.errors.append(f"{msg.id}: {type(exc).__name__}")
                log.warning("indexing failed", message_id=msg.id, error=type(exc).__name__)
            done += 1
            out.examined += 1
            last = msg.first_seen_at
            if done % CHECKPOINT_EVERY == 0:
                await self._checkpoint(key, since, last, msg.id)
            if self.bus and done % PROGRESS_EVERY == 0:
                await self.bus.publish(
                    IndexRebuildProgress(
                        ts=self.clock.now(),
                        account_id=acct,
                        done=done,
                        total=total,
                        indexed=indexed,
                        failed=failed,
                    )
                )
        await self.index.set_meta(key, None)
        if self.bus:
            await self.bus.publish(
                IndexRebuildProgress(
                    ts=self.clock.now(),
                    account_id=acct,
                    done=done,
                    total=total,
                    indexed=indexed,
                    failed=failed,
                    finished=True,
                )
            )

    async def _checkpoint(
        self, key: str, since: datetime | None, at: datetime, message_id: MessageId
    ) -> None:
        await self.index.set_meta(
            key,
            json.dumps(
                {"since": fmt_ts(since) if since else "", "at": fmt_ts(at), "id": message_id}
            ),
        )

    async def _prune(self, account_id: AccountId | None) -> int:
        removed = 0
        after = ""
        while True:
            ids = await self.index.doc_ids(after=after, limit=500, account_id=account_id)
            if not ids:
                return removed
            after = ids[-1]
            live = await self.raw.live_message_ids(ids)
            for mid in ids:
                if mid not in live and await self.index.delete_message(MessageId(mid)):
                    removed += 1


async def rebuild_index(
    raw_store: RawStore,
    index_store: IndexStore,
    *,
    attrs: AttrStore,
    settings: MailSettings,
    clock: Clock,
    bus: EventBus | None = None,
    tokenizer: Tokenizer | None = None,
    account_id: AccountId | None = None,
    since: datetime | None = None,
    force: bool = False,
) -> RebuildResult:
    """See ``Indexer.rebuild``."""
    indexer = Indexer(raw_store, index_store, attrs, settings, clock, tokenizer=tokenizer, bus=bus)
    return await indexer.rebuild(account_id=account_id, since=since, force=force)


# -- as a job ------------------------------------------------------------------------------
class RebuildPayload(BaseModel):
    account_id: str | None = None
    since: datetime | None = None
    force: bool = False


class RebuildJobResult(BaseModel):
    examined: int
    indexed: int
    unchanged: int
    failed: int
    pruned: int
    resumed: bool


def register_index_jobs(runner: JobRunner, indexer: Indexer) -> None:
    """Handle ``index.rebuild`` jobs. A job that dies is run again and resumes at its checkpoint."""

    async def handle(payload: RebuildPayload, _job: Job) -> RebuildJobResult:
        r = await indexer.rebuild(
            account_id=AccountId(payload.account_id) if payload.account_id else None,
            since=payload.since,
            force=payload.force,
        )
        return RebuildJobResult(
            examined=r.examined,
            indexed=r.indexed,
            unchanged=r.unchanged,
            failed=r.failed,
            pruned=r.pruned,
            resumed=r.resumed,
        )

    runner.register(
        REBUILD_KIND, RebuildPayload, handle, concurrency=1, result_model=RebuildJobResult
    )


async def enqueue_rebuild(
    queue: JobQueue, payload: RebuildPayload | None = None, *, actor: Actor = SYSTEM
) -> Enqueued:
    """Queue a rebuild. One is enough per account: asking again while one is queued or running
    returns that job."""
    p = payload or RebuildPayload()
    return await queue.enqueue(
        REBUILD_KIND,
        p,
        priority=PRIORITY_BACKFILL,
        dedupe_key=f"{REBUILD_KIND}:{p.account_id or '*'}",
        actor=actor,
    )
