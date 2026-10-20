# SPDX-License-Identifier: Apache-2.0
"""In-memory typed event bus.

Two kinds of listener, with different promises:

* **Handlers** (``subscribe``) are internal components. Every published event is delivered to
  every matching handler; none is ever dropped. A handler that raises is logged and skipped, and
  never affects the publisher or the other handlers. ``publish`` returns once all handlers have
  finished, so handlers should be quick (queue work as a job instead of doing it inline).
* **Streams** (``stream``) feed clients such as SSE. Each has a bounded queue. When a consumer is
  too slow and its queue is full, the **oldest** event is dropped to make room, and
  ``EventStream.dropped`` counts them. A slow stream never blocks ``publish``.
"""

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from types import TracebackType
from typing import Self

from oaken.core.ids import AccountId, JobId, MessageId, uuid7
from oaken.core.logging import get_logger

log = get_logger("oaken.events")

DEFAULT_STREAM_QUEUE = 256


def _new_event_id() -> str:
    return str(uuid7())


@dataclass(frozen=True, kw_only=True)
class Event:
    ts: datetime
    event_id: str = field(default_factory=_new_event_id)


@dataclass(frozen=True, kw_only=True)
class SyncStarted(Event):
    account_id: AccountId


@dataclass(frozen=True, kw_only=True)
class SyncProgress(Event):
    account_id: AccountId
    done: int
    total: int | None = None


@dataclass(frozen=True, kw_only=True)
class SyncFinished(Event):
    account_id: AccountId
    new_messages: int = 0
    error: str | None = None
    # The account is inside its quiet hours: the sync ran as usual, but a client should not alert.
    quiet: bool = False
    folder: str = ""


@dataclass(frozen=True, kw_only=True)
class SyncPaused(Event):
    """An account's scheduled sync stopped until the user acts (for example, the password was
    refused). ``quiet`` is as on ``SyncFinished``."""

    account_id: AccountId
    reason: str
    quiet: bool = False


@dataclass(frozen=True, kw_only=True)
class SyncScheduleChanged(Event):
    """A sync schedule was added, edited, paused, resumed or removed (``change`` says which)."""

    account_id: AccountId
    folder: str = ""
    change: str


@dataclass(frozen=True, kw_only=True)
class MessageStored(Event):
    """``is_new``: first time seen. ``changed``: an existing message got different bytes (or came
    back after a delete), so anything derived from it is stale. A no-op put publishes nothing."""

    account_id: AccountId
    message_id: MessageId
    is_new: bool = True
    changed: bool = False


@dataclass(frozen=True, kw_only=True)
class LargeMessageStored(Event):
    """A message was too big for ``raw.db`` and went to the LFS folder, whole. ``oversize`` is set
    when it is also past ``storage.max_message_bytes`` (worth a warning in a UI)."""

    account_id: AccountId
    message_id: MessageId
    size_bytes: int
    oversize: bool = False


@dataclass(frozen=True, kw_only=True)
class MessageParsed(Event):
    account_id: AccountId
    message_id: MessageId


@dataclass(frozen=True, kw_only=True)
class MessageFiltered(Event):
    """Sieve ran on a message and its organization attributes were written."""

    account_id: AccountId
    message_id: MessageId
    disposition: str


@dataclass(frozen=True, kw_only=True)
class MessageIndexed(Event):
    account_id: AccountId
    message_id: MessageId


@dataclass(frozen=True, kw_only=True)
class IndexRebuildProgress(Event):
    """A rebuild of ``index.db`` is under way. ``total`` is the live messages in scope when it
    started; ``done`` counts the ones looked at (changed or not). ``finished`` is the last event."""

    account_id: AccountId
    done: int
    total: int
    indexed: int
    failed: int
    finished: bool = False


@dataclass(frozen=True, kw_only=True)
class MessageEmbedded(Event):
    account_id: AccountId
    message_id: MessageId
    model: str


@dataclass(frozen=True, kw_only=True)
class MessageEnriched(Event):
    account_id: AccountId
    message_id: MessageId
    enricher: str


@dataclass(frozen=True, kw_only=True)
class AttachmentQuarantined(Event):
    """Flagged attachments of a message were written to the quarantine folder (never opened)."""

    account_id: AccountId
    message_id: MessageId
    count: int


@dataclass(frozen=True, kw_only=True)
class MessageReadChanged(Event):
    account_id: AccountId
    message_id: MessageId
    read: bool
    # Why, and the audit row a client can offer to undo. ``server_sync`` changes have no audit row.
    reason: str = ""
    audit_id: str | None = None
    # True while the change exists only here: no provider can write it back yet. ``error`` says why.
    local_only: bool = False
    error: str | None = None


@dataclass(frozen=True, kw_only=True)
class JobStateChanged(Event):
    job_id: JobId
    kind: str
    status: str
    account_id: AccountId | None = None


@dataclass(frozen=True, kw_only=True)
class PluginHealthChanged(Event):
    plugin: str
    healthy: bool
    detail: str = ""


@dataclass(frozen=True, kw_only=True)
class PluginEvent(Event):
    """Base for events a plugin defines itself: subclass it, add plain-data fields, and publish
    with ``ctx.publish(...)``. ``plugin`` names the sender (the host fills it in)."""

    plugin: str = ""


Handler = Callable[[Event], Awaitable[None]]
EventFilter = Callable[[Event], bool]


class EventStream:
    """An async iterator of events with a bounded, drop-oldest queue. Close it when done."""

    def __init__(self, bus: "EventBus", accept: EventFilter, maxsize: int) -> None:
        self._bus = bus
        self._accept = accept
        self._maxsize = maxsize
        self._items: deque[Event] = deque()
        self._ready = asyncio.Event()
        self._closed = False
        self.dropped = 0

    def _offer(self, event: Event) -> None:
        if self._closed or not self._accept(event):
            return
        if len(self._items) >= self._maxsize:
            self._items.popleft()
            self.dropped += 1
        self._items.append(event)
        self._ready.set()

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> Event:
        while True:
            if self._items:
                return self._items.popleft()
            if self._closed:
                raise StopAsyncIteration
            self._ready.clear()
            await self._ready.wait()

    async def aclose(self) -> None:
        self._closed = True
        self._bus._streams.discard(self)
        self._ready.set()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()


class EventBus:
    def __init__(self) -> None:
        self._handlers: list[tuple[type[Event], Handler]] = []
        self._streams: set[EventStream] = set()

    def subscribe(self, event_type: type[Event], handler: Handler) -> Callable[[], None]:
        """Call ``handler`` for every event that is an instance of ``event_type`` (``Event``
        matches all). Returns a function that removes the subscription."""
        entry = (event_type, handler)
        self._handlers.append(entry)

        def unsubscribe() -> None:
            if entry in self._handlers:
                self._handlers.remove(entry)

        return unsubscribe

    def stream(
        self,
        event_filter: EventFilter | type[Event] | tuple[type[Event], ...] | None = None,
        *,
        maxsize: int = DEFAULT_STREAM_QUEUE,
    ) -> EventStream:
        """Start receiving matching events now; iterate with ``async for``, then ``aclose()``."""
        if maxsize < 1:
            raise ValueError("maxsize must be at least 1")
        accept: EventFilter
        if event_filter is None:

            def accept(_e: Event) -> bool:
                return True
        elif isinstance(event_filter, type | tuple):
            types = event_filter

            def accept(e: Event) -> bool:
                return isinstance(e, types)
        else:
            accept = event_filter
        stream = EventStream(self, accept, maxsize)
        self._streams.add(stream)
        return stream

    async def publish(self, event: Event) -> None:
        for stream in list(self._streams):
            try:
                stream._offer(event)
            except Exception:  # a broken filter must not reach the publisher
                log.exception("event stream filter failed", event_type=type(event).__name__)
        matching = [h for t, h in self._handlers if isinstance(event, t)]
        if matching:
            await asyncio.gather(*(self._call(h, event) for h in matching))

    @staticmethod
    async def _call(handler: Handler, event: Event) -> None:
        try:
            await handler(event)
        except Exception:
            log.exception("event handler failed", event_type=type(event).__name__)
