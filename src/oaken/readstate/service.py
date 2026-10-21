# SPDX-License-Identifier: Apache-2.0
"""Read state: the one place a message becomes read or unread (prompt 12).

Read state follows what the user has seen. A message is marked read only when the user opens it
(``message_presented``, after a delay and only if it is still presented) or asks for a summary of
it. Sync, ingest, embedding, enrichment, search and bulk reads never call this module, and a
system actor is refused outright, so background work cannot mark mail read even by mistake.

Every change is local first. ``message_state.is_read`` flips at once; ``local_dirty_json`` records
what the server still has to be told; one audit row records who did it and why; and a durable
``readstate.write`` job (one per message, deduplicated) writes it through the provider's
``flags.write``. Success clears the marker; a failure retries with the queue's backoff. While a
marker is set, a flag change that arrives from the server for that message is ignored, because
the write about to go out will overwrite it.

When no provider can write flags, the change stays local, the marker stays, nothing is retried on
a timer, and the change is reported as ``local_only`` with the reason, until someone fixes the
provider and calls ``resume_pending``.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from sqlite3 import Row
from typing import Any

from pydantic import BaseModel

from oaken.core.actors import USER, Actor
from oaken.core.clock import Clock, fmt_ts
from oaken.core.db import Tx
from oaken.core.errors import Conflict, NotFound, OakenError, PermissionDenied
from oaken.core.events import Event, EventBus, MessageReadChanged, MessageStarChanged
from oaken.core.ids import AccountId, MessageId
from oaken.core.jobs import PRIORITY_USER, Job, JobQueue, JobRunner, PermanentJobError
from oaken.core.logging import get_logger
from oaken.core.rawstore import RawStore
from oaken.core.settings import Settings
from oaken.plugins.base import inferred_capabilities
from oaken.plugins.contracts import FlagChange, Provider
from oaken.readstate.audit import AuditLog, new_audit_id

log = get_logger("oaken.readstate")

WRITE = "readstate.write"
SEEN, FLAGGED = "\\Seen", "\\Flagged"
NO_PROVIDER = "no_flags_write_provider"
MAX_ROUNDS = 5  # writes in one job when the user keeps changing their mind
IN_LIMIT = 500  # ids per ``IN (...)`` query

# Reasons that are an automatic consequence of something the user did (opening a message).
# ``auto_mark = false`` turns these off; an explicit mark read or unread (any other reason, a
# summary the person chose to mark read included) is never affected by it.
AUTOMATIC = frozenset({"user_opened"})
READ_OPS = frozenset({"mark_read", "mark_unread", "undo", "set_flagged"})

# The two flag states this service owns. Each maps a message_state column to the IMAP-style flag
# written back through the provider, and to the audit field name clients read.
FLAG_SEEN = "is_read"
FLAG_STARRED = "is_starred"
_FLAG_OF = {FLAG_SEEN: SEEN, FLAG_STARRED: FLAGGED}

# Asked with an account id: the provider instance that serves that account, if it can write flags.
ProviderFor = Callable[[str], Provider | None]


class ConfirmRequired(PermissionDenied):
    """The change needs ``confirm=True``, which the operation registry's gate passes once a person
    (or a token from a proposal) has approved it."""

    code = "confirm_required"


class WritePayload(BaseModel):
    message_id: str


@dataclass(frozen=True)
class ReadResult:
    changed: tuple[MessageId, ...] = ()
    unchanged: tuple[MessageId, ...] = ()
    missing: tuple[MessageId, ...] = ()  # unknown, or deleted
    local_only: tuple[MessageId, ...] = ()  # changed here, but no provider can write it back
    audit_ids: tuple[str, ...] = ()
    batch_id: str | None = None
    skipped: str | None = None  # why nothing was tried (``auto_mark_off``)


@dataclass
class _Pending:
    deadline: datetime
    actor: Actor
    task: "asyncio.Task[None] | None" = None
    token: int = 0


@dataclass
class _Outcome:
    changed: list[MessageId] = field(default_factory=list)
    unchanged: list[MessageId] = field(default_factory=list)
    missing: list[MessageId] = field(default_factory=list)
    local_only: list[MessageId] = field(default_factory=list)
    audit_ids: list[str] = field(default_factory=list)


def providers_from_accounts(accounts: Any) -> ProviderFor:
    """A ``provider_for`` that asks ``AccountProviders``: the account's own provider instance, if it
    is healthy and can write flags."""

    def lookup(account_id: str) -> Provider | None:
        try:
            provider = accounts.get(account_id)
        except OakenError:
            return None
        return provider if "flags.write" in inferred_capabilities(provider, "provider") else None

    return lookup


_STATE_SQL = (
    "SELECT s.message_id AS id, s.is_read, s.is_starred, s.local_dirty_json AS dirty,"
    " m.account_id, m.provider_id, m.deleted_at, a.provider"
    " FROM message_state s JOIN messages m ON m.id = s.message_id"
    " JOIN accounts a ON a.id = m.account_id"
)


class ReadState:
    def __init__(
        self,
        raw: RawStore,
        queue: JobQueue,
        audit: AuditLog,
        clock: Clock,
        settings: Settings,
        *,
        bus: EventBus | None = None,
        provider_for: ProviderFor | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.raw, self.queue, self.audit, self.clock = raw, queue, audit, clock
        self.cfg, self.ui, self.bus = settings.read_state, settings.ui, bus
        self.provider_for: ProviderFor = provider_for or (lambda name: None)
        self._sleep = sleep  # replaceable in tests; the deadline itself is judged by the clock
        self._pending: dict[MessageId, _Pending] = {}
        self._tokens = 0

    def register(self, runner: JobRunner) -> None:
        runner.register(WRITE, WritePayload, self._write_job, concurrency=2)

    async def close(self) -> None:
        for mid in list(self._pending):
            self._drop(mid)

    # -- explicit operations ----------------------------------------------------------------
    async def mark_read(
        self,
        ids: Iterable[MessageId],
        reason: str,
        *,
        actor: Actor = USER,
        confirm: bool = False,
    ) -> ReadResult:
        return await self._mark(ids, True, reason, actor, confirm, "mark_read")

    async def mark_unread(
        self,
        ids: Iterable[MessageId],
        reason: str,
        *,
        actor: Actor = USER,
        confirm: bool = False,
    ) -> ReadResult:
        return await self._mark(ids, False, reason, actor, confirm, "mark_unread")

    async def set_flagged(
        self,
        ids: Iterable[MessageId],
        flagged: bool,
        reason: str,
        *,
        actor: Actor = USER,
        confirm: bool = False,
    ) -> ReadResult:
        """Star or unstar messages (the ``\\Flagged`` flag). Local first, written back through the
        provider's ``flags.write`` exactly as read state is, audited and undoable. This is the only
        way a star reaches ``set_flags``: no other caller may (prompt 25)."""
        self._gate(actor, reason, confirm)
        wanted = {MessageId(i): flagged for i in dict.fromkeys(ids)}
        return await self._apply(
            wanted, reason=reason, op="set_flagged", actor=actor, field=FLAG_STARRED
        )

    async def _mark(
        self,
        ids: Iterable[MessageId],
        read: bool,
        reason: str,
        actor: Actor,
        confirm: bool,
        op: str,
    ) -> ReadResult:
        self._gate(actor, reason, confirm)
        if reason in AUTOMATIC and not self.cfg.auto_mark:
            return ReadResult(skipped="auto_mark_off")
        wanted = {MessageId(i): read for i in dict.fromkeys(ids)}
        return await self._apply(wanted, reason=reason, op=op, actor=actor, field=FLAG_SEEN)

    @staticmethod
    def _gate(actor: Actor, reason: str, confirm: bool) -> None:
        """Defence in depth behind the operation registry's confirm gate (prompt 13), where a
        change is really approved and which passes ``confirm=True`` once it has. Background work
        is refused outright, whatever any caller says."""
        if actor.kind == "system":
            raise PermissionDenied("background work never changes read state")
        if not reason:
            raise PermissionDenied("a change of read state needs a reason")
        preconfirmed = actor.kind == "user" and reason.startswith("user_")
        if not preconfirmed and not confirm:
            raise ConfirmRequired(f"changing read state for {reason!r} needs confirm=True")

    async def undo(
        self, audit_id: str, *, actor: Actor = USER, confirm: bool = False
    ) -> ReadResult:
        """Restore the exact prior state of everything the audit row (and the rest of its batch)
        changed. The undo is itself audited, and can itself be undone."""
        self._gate(actor, "user_undo", confirm)
        entry = await self.audit.get(audit_id)
        if entry is None:
            raise NotFound(f"no such audit entry: {audit_id}")
        if entry.op not in READ_OPS:
            raise Conflict(f"audit entry {audit_id} is a {entry.op!r}, which cannot be undone here")
        if entry.undone_by:
            raise Conflict(f"audit entry {audit_id} was already undone by {entry.undone_by}")
        entries = [e for e in await self.audit.batch(entry.batch_id) if e.undone_by is None]
        field = FLAG_STARRED if entry.op == "set_flagged" else FLAG_SEEN
        wanted = {MessageId(m): bool(v) for e in entries for m, v in e.before.items()}
        result = await self._apply(
            wanted, reason="user_undo", op="undo", actor=actor, field=field, always_record=True
        )
        await self.audit.mark_undone([e.id for e in entries], result.audit_ids[0])
        return result

    # -- presented and dismissed ------------------------------------------------------------
    async def message_presented(self, message_id: MessageId, *, actor: Actor = USER) -> bool:
        """The user is looking at this message. Core marks it read after ``ui.mark_read_delay_ms``
        unless it is dismissed first. Presenting it again starts a fresh timer. False when
        ``auto_mark`` is off."""
        self._gate(actor, "user_opened", False)
        if not self.cfg.auto_mark:
            return False
        if not await self.raw.db.fetchone("SELECT 1 FROM messages WHERE id = ?", (message_id,)):
            raise NotFound(f"no such message: {message_id}")
        self._drop(message_id)
        delay = self.ui.mark_read_delay_ms / 1000
        self._tokens += 1
        pending = _Pending(self.clock.now() + timedelta(seconds=delay), actor, token=self._tokens)
        pending.task = asyncio.create_task(self._timer(message_id, pending.token, delay))
        self._pending[message_id] = pending
        return True

    async def message_dismissed(self, message_id: MessageId) -> bool:
        """The user stopped looking. True if a mark was still waiting and is now cancelled."""
        return self._drop(message_id)

    async def due(self) -> int:
        """Mark every presented message whose delay has passed. The timers call this; tests with a
        fake clock call it after advancing the clock."""
        now = self.clock.now()
        done = 0
        for mid, p in list(self._pending.items()):
            if p.deadline <= now and await self._fire(mid, p.token):
                done += 1
        return done

    async def _timer(self, mid: MessageId, token: int, delay: float) -> None:
        await self._sleep(delay)
        await self._fire(mid, token)

    async def _fire(self, mid: MessageId, token: int) -> bool:
        p = self._pending.get(mid)
        if p is None or p.token != token or p.deadline > self.clock.now():
            return False
        self._drop(mid)
        try:
            await self._mark([mid], True, "user_opened", p.actor, False, "mark_read")
        except OakenError as exc:
            log.warning("could not mark opened message read", message_id=mid, code=exc.code)
            return False
        return True

    def _drop(self, mid: MessageId) -> bool:
        p = self._pending.pop(mid, None)
        if p is None:
            return False
        if p.task is not None and p.task is not asyncio.current_task():
            p.task.cancel()
        return True

    # -- the state change -------------------------------------------------------------------
    async def _rows(self, tx: Tx, ids: Sequence[str]) -> dict[str, Row]:
        out: dict[str, Row] = {}
        for i in range(0, len(ids), IN_LIMIT):
            part = ids[i : i + IN_LIMIT]
            marks = ",".join("?" * len(part))
            for r in await tx.fetchall(f"{_STATE_SQL} WHERE s.message_id IN ({marks})", part):
                out[r["id"]] = r
        return out

    async def _apply(
        self,
        wanted: Mapping[MessageId, bool],
        *,
        reason: str,
        op: str,
        actor: Actor,
        field: str = FLAG_SEEN,
        always_record: bool = False,
    ) -> ReadResult:
        batch_id = new_audit_id()
        out = _Outcome()
        events: list[Event] = []
        announce = []
        ids = list(wanted)
        step = self.cfg.audit_chunk
        for start in range(0, max(len(ids), 1), step):
            part = ids[start : start + step]
            audit_id = new_audit_id()
            async with self.raw.db.transaction() as tx:
                rows = await self._rows(tx, part)
                before: dict[MessageId, bool] = {}
                after: dict[MessageId, bool] = {}
                for mid in part:
                    r = rows.get(mid)
                    if r is None or r["deleted_at"]:
                        out.missing.append(mid)
                    elif bool(r[field]) == wanted[mid]:
                        out.unchanged.append(mid)
                    else:
                        before[mid], after[mid] = bool(r[field]), wanted[mid]
                if not after and not (always_record and not out.audit_ids):
                    continue
                await self.audit.record(
                    tx, audit_id=audit_id, actor=actor, reason=reason, op=op, batch_id=batch_id,
                    before=before, after=after,
                )  # fmt: skip
                out.audit_ids.append(audit_id)
                now = fmt_ts(self.clock.now())
                for mid, value in after.items():
                    r = rows[mid]
                    marker: dict[str, Any] = {
                        "field": field, field: value, "audit_id": audit_id, "ts": now
                    }  # fmt: skip
                    error: str | None = None
                    if self.provider_for(r["account_id"]) is None:
                        error = f"{NO_PROVIDER}: account {r['account_id']!r} uses {r['provider']!r}"
                        marker["error"] = error
                    await tx.execute(
                        f"UPDATE message_state SET {field} = ?, local_dirty_json = ?"  # noqa: S608
                        " WHERE message_id = ?",
                        (int(value), json.dumps(marker), mid),
                    )
                    if error is None:
                        _, change = await self.queue.enqueue_in(
                            tx, WRITE, WritePayload(message_id=mid), priority=PRIORITY_USER,
                            account_id=AccountId(r["account_id"]), dedupe_key=f"{WRITE}:{mid}",
                            actor=actor,
                        )  # fmt: skip
                        announce.append(change)
                    else:
                        out.local_only.append(MessageId(mid))
                    out.changed.append(MessageId(mid))
                    events.append(self._state_event(field, r, value, reason, audit_id, error))
        await self.queue.announce(*announce)
        await self._publish(events)
        log.info(
            "flag state changed", op=op, field=field, reason=reason, actor=actor.kind,
            changed=len(out.changed), local_only=len(out.local_only),
        )  # fmt: skip
        return ReadResult(
            changed=tuple(out.changed),
            unchanged=tuple(out.unchanged),
            missing=tuple(out.missing),
            local_only=tuple(out.local_only),
            audit_ids=tuple(out.audit_ids),
            batch_id=batch_id if out.audit_ids else None,
        )

    def _state_event(
        self, field: str, r: Row, value: bool, reason: str, audit_id: str, error: str | None
    ) -> Event:
        """The right event for the field that changed: read state or star."""
        common = dict(
            ts=self.clock.now(),
            account_id=AccountId(r["account_id"]),
            message_id=MessageId(r["id"]),
            reason=reason,
            audit_id=audit_id,
            local_only=error is not None,
            error=error,
        )
        if field == FLAG_STARRED:
            return MessageStarChanged(starred=value, **common)  # type: ignore[arg-type]
        return MessageReadChanged(read=value, **common)  # type: ignore[arg-type]

    async def _publish(self, events: Iterable[Event]) -> None:
        if self.bus is not None:
            for e in events:
                await self.bus.publish(e)

    # -- inbound: what the server says -------------------------------------------------------
    async def apply_server_flags(
        self, account_id: AccountId, changes: list[tuple[MessageId, FlagChange]]
    ) -> None:
        """A ``SyncRunner`` flag sink. A change made on the server updates the local state unless
        a local change for that message is still waiting to be written (local wins until the
        server has acknowledged it; after that, the server's word is the latest). Not audited:
        nothing here was done by a user of this client."""
        events: list[MessageReadChanged] = []
        async with self.raw.db.transaction() as tx:
            rows = await self._rows(tx, [m for m, _ in changes])
            for mid, fc in changes:
                r = rows.get(mid)
                if r is None:
                    continue
                read = bool(r["is_read"])
                if r["dirty"] is None:
                    if SEEN in fc.add:
                        read = True
                    elif SEEN in fc.remove:
                        read = False
                starred = bool(r["is_starred"])
                if FLAGGED in fc.add:
                    starred = True
                elif FLAGGED in fc.remove:
                    starred = False
                if read == bool(r["is_read"]) and starred == bool(r["is_starred"]):
                    continue
                await tx.execute(
                    "UPDATE message_state SET is_read = ?, is_starred = ?, server_updated_at = ?"
                    " WHERE message_id = ?",
                    (int(read), int(starred), fmt_ts(self.clock.now()), mid),
                )
                if read != bool(r["is_read"]):
                    events.append(
                        MessageReadChanged(
                            ts=self.clock.now(), account_id=account_id, message_id=mid,
                            read=read, reason="server_sync",
                        )
                    )  # fmt: skip
        await self._publish(events)

    # -- outbound: the write-back job ---------------------------------------------------------
    async def _write_job(self, payload: WritePayload, job: Job) -> None:
        mid = MessageId(payload.message_id)
        for _ in range(MAX_ROUNDS):
            rows = await self.raw.db.fetchall(f"{_STATE_SQL} WHERE s.message_id = ?", (mid,))
            if not rows or rows[0]["dirty"] is None:
                return
            r = rows[0]
            text = r["dirty"]
            if r["deleted_at"]:  # nothing to write to: drop the intent
                await self._clear(mid, text)
                return
            marker = json.loads(text)
            provider = self.provider_for(r["account_id"])
            if provider is None:
                error = f"{NO_PROVIDER}: account {r['account_id']!r} uses {r['provider']!r}"
                await self._park(r, marker, error)
                raise PermanentJobError(error)
            field = marker.get("field", FLAG_SEEN)  # legacy markers held only is_read
            flag = _FLAG_OF[field]
            add, remove = ({flag}, set()) if marker[field] else (set(), {flag})
            try:
                await provider.set_flags([r["provider_id"]], add, remove)
            except Exception as exc:
                if getattr(exc, "transient", True) is False:
                    code = getattr(exc, "code", type(exc).__name__)
                    error = f"write_refused: the provider refused the change ({code})"
                    await self._park(r, marker, error)
                    raise PermanentJobError(error) from None
                raise
            if await self._clear(mid, text):
                return
        raise RuntimeError("read state kept changing during the write")  # retried with backoff

    async def _clear(self, mid: MessageId, text: str) -> bool:
        """Clear the marker if it is still the one that was written."""
        return bool(
            await self.raw.db.execute(
                "UPDATE message_state SET local_dirty_json = NULL, server_updated_at = ?"
                " WHERE message_id = ? AND local_dirty_json = ?",
                (fmt_ts(self.clock.now()), mid, text),
            )
        )

    async def _park(self, r: Row, marker: dict[str, Any], error: str) -> None:
        """Keep the change local, remember why, and tell clients."""
        marker["error"] = error
        await self.raw.db.execute(
            "UPDATE message_state SET local_dirty_json = ? WHERE message_id = ?",
            (json.dumps(marker), r["id"]),
        )
        field = marker.get("field", FLAG_SEEN)
        await self._publish(
            [
                self._state_event(
                    field, r, bool(marker[field]), "write_back_failed",
                    marker.get("audit_id", ""), error,
                )
            ]
        )  # fmt: skip

    async def resume_pending(self, account_id: AccountId | None = None) -> int:
        """Queue the write-back of every change that still has a marker and no job (after a
        crash, or once a provider that can write flags is available). Returns how many were
        queued. Changes whose provider still cannot write stay local."""
        sql = f"{_STATE_SQL} WHERE s.local_dirty_json IS NOT NULL AND m.deleted_at IS NULL"
        args: tuple[Any, ...] = ()
        if account_id:
            sql, args = sql + " AND m.account_id = ?", (account_id,)
        queued = 0
        announce = []
        async with self.raw.db.transaction() as tx:
            for r in await tx.fetchall(sql, args):
                if self.provider_for(r["account_id"]) is None:
                    continue
                marker = json.loads(r["dirty"])
                if "error" in marker:
                    marker.pop("error")
                    await tx.execute(
                        "UPDATE message_state SET local_dirty_json = ? WHERE message_id = ?",
                        (json.dumps(marker), r["id"]),
                    )
                made, change = await self.queue.enqueue_in(
                    tx, WRITE, WritePayload(message_id=r["id"]), priority=PRIORITY_USER,
                    account_id=AccountId(r["account_id"]), dedupe_key=f"{WRITE}:{r['id']}",
                )  # fmt: skip
                queued += made.created
                announce.append(change)
        await self.queue.announce(*announce)
        return queued
