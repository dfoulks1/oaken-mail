# SPDX-License-Identifier: Apache-2.0
"""The five plugin contracts: typing Protocols plus the plain-data types that cross them.

Everything that crosses the boundary is a frozen dataclass of plain data (strings, bytes,
numbers, tuples). A plugin never receives a database handle. Plugins are *not* required to
inherit from anything here: any class with these members satisfies a contract. Most authors will
subclass the optional base classes in ``oaken.plugins.base`` instead, which supply defaults.

Core is lenient in what it receives from plugins (see ``oaken.plugins.adapt``: dicts, lists and
tuples are accepted and coerced into these types) and strict about what it hands on.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from oaken.core.clock import Clock
from oaken.core.errors import PluginError
from oaken.core.events import Event, EventBus, PluginEvent
from oaken.core.settings import Settings
from oaken.core.untrusted import UntrustedText

PLUGIN_API_VERSION = 1

PluginKind = Literal["provider", "embedder", "llm_backend", "enricher", "vector_store"]
PLUGIN_KINDS: tuple[PluginKind, ...] = (
    "provider",
    "embedder",
    "llm_backend",
    "enricher",
    "vector_store",
)

# Python entry-point group for each kind. First-party plugins register the same way.
ENTRY_POINT_GROUPS: dict[str, str] = {
    "provider": "oaken.providers",
    "embedder": "oaken.embedders",
    "llm_backend": "oaken.llm_backends",
    "enricher": "oaken.enrichers",
    "vector_store": "oaken.vector_stores",
}

# Capabilities a kind may declare, and the method that must really exist for each.
CAPABILITIES: dict[str, dict[str, str]] = {
    "provider": {
        "auth": "authenticate",
        "sync.pull": "pull",
        "flags.write": "set_flags",
        "send": "send",
    },
    "embedder": {},
    "llm_backend": {},
    "enricher": {},
    "vector_store": {},
}

# Members a plugin of each kind must have, besides the methods its capabilities require.
REQUIRED_MEMBERS: dict[str, list[str]] = {
    "provider": [],
    "embedder": ["model_id", "dimensions", "embed"],
    "llm_backend": ["model_id", "context_window", "generate"],
    "enricher": ["version", "needs_llm", "enrich"],
    "vector_store": ["upsert", "delete", "query", "stats"],
}


@dataclass(frozen=True)
class PluginInfo:
    """What the host knows about a loaded plugin."""

    name: str
    version: str
    kind: PluginKind
    capabilities: frozenset[str]
    api_version: int


@dataclass(frozen=True)
class PluginContext:
    """Handed to ``start()``. ``config`` is the plugin's own ``[plugins.<name>]`` table."""

    name: str
    kind: PluginKind
    settings: Settings
    clock: Clock
    log: Any
    config: dict[str, Any] = field(default_factory=dict)
    bus: EventBus | None = None
    _subscriptions: list[Callable[[], None]] = field(
        default_factory=list, repr=False, compare=False
    )

    async def publish(self, event: PluginEvent) -> None:
        """Announce a plugin-defined event (a ``PluginEvent`` subclass) on the event bus. Core
        events cannot be forged this way. A context without a bus (tests) drops it quietly."""
        if not isinstance(event, PluginEvent):
            raise PluginError(f"{self.name}: publish() takes a PluginEvent subclass")
        if self.bus is not None:
            await self.bus.publish(replace(event, plugin=self.name))

    def subscribe(
        self, event_type: type[Event], handler: Callable[[Any], Awaitable[None]]
    ) -> Callable[[], None]:
        """Call ``handler(event)`` for every core or plugin event of ``event_type`` (``Event``
        for all). Read-only: events are frozen, and a handler that raises is logged and skipped
        without touching core. Subscriptions end when the plugin stops (the host calls
        ``release``); the returned function ends one earlier. Handlers should be quick: queue
        real work instead of doing it inline. A context without a bus (tests) has no events."""
        if self.bus is None:
            return lambda: None
        unsubscribe = self.bus.subscribe(event_type, handler)
        self._subscriptions.append(unsubscribe)
        return unsubscribe

    def release(self) -> None:
        """End every subscription this plugin made. Called by the host when the plugin stops."""
        subs, self._subscriptions[:] = list(self._subscriptions), []
        for unsubscribe in subs:
            unsubscribe()


@dataclass(frozen=True)
class Health:
    ok: bool
    detail: str = ""


@runtime_checkable
class Plugin(Protocol):
    """Lifecycle, shared by every kind. All three are optional for a plugin to define."""

    async def start(self, ctx: PluginContext) -> None:
        """Acquire resources. Raising marks the plugin unhealthy; core carries on."""
        ...

    async def stop(self) -> None:
        """Release resources. Called once at shutdown."""
        ...

    async def health(self) -> Health:
        """A cheap liveness check, polled periodically by the host."""
        ...


# -- Provider ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class RawMessage:
    """One message exactly as the server holds it."""

    provider_id: str
    raw_bytes: bytes
    provider_labels: tuple[str, ...] = ()
    provider_flags: frozenset[str] = frozenset()
    internal_date: datetime | None = None


@dataclass(frozen=True)
class FlagChange:
    """A flag change made on the server side (for example, read on another device)."""

    provider_id: str
    add: frozenset[str] = frozenset()
    remove: frozenset[str] = frozenset()


@dataclass(frozen=True)
class PullBatch:
    """One page of changes. ``next_cursor`` is opaque: core stores it and hands it back."""

    messages: tuple[RawMessage, ...] = ()
    deleted_provider_ids: tuple[str, ...] = ()
    flag_changes: tuple[FlagChange, ...] = ()
    next_cursor: str | None = None


@runtime_checkable
class Provider(Plugin, Protocol):
    """A mail source or a way to send. Capabilities: ``auth``, ``sync.pull``, ``flags.write``,
    ``send``. A provider declares at least ``sync.pull`` or ``send``: a send-only provider (an SMTP
    server) has no mail to pull."""

    async def authenticate(self, ctx: PluginContext) -> None:
        """Make sure the plugin can talk to the service (capability ``auth``)."""
        ...

    def pull(self, cursor: str | None) -> AsyncIterator[PullBatch]:
        """Yield batches of changes since ``cursor`` (None: from the start). Capability
        ``sync.pull``. Re-pulling from the last cursor returns nothing new."""
        ...

    async def set_flags(
        self, provider_ids: Sequence[str], add: Collection[str], remove: Collection[str]
    ) -> None:
        """Write flags back to the server (capability ``flags.write``)."""
        ...

    async def send(self, raw_bytes: bytes) -> str:
        """Send a message and return its provider id. Declared for the future; unimplemented in
        v1, and the host never routes sending to a plugin (capability ``send``)."""
        ...


# -- Embedder ----------------------------------------------------------------------------------


@runtime_checkable
class Embedder(Plugin, Protocol):
    """Turns text into vectors."""

    model_id: str
    dimensions: int
    max_input_tokens: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector of ``dimensions`` floats per text, in order."""
        ...


# -- LLM backend -------------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMRequest:
    """``system`` is a static instruction. Mail content only ever appears in ``user``, as
    ``UntrustedText`` (plain ``str`` parts are for the application's own words)."""

    system: str
    user: tuple[str | UntrustedText, ...] = ()
    json_schema: dict[str, Any] | None = None
    max_output_tokens: int | None = None
    temperature: float = 0.0


@dataclass(frozen=True)
class LLMResponse:
    text: str = ""
    data: dict[str, Any] | None = None  # parsed JSON when a schema was requested
    input_tokens: int = 0
    output_tokens: int = 0
    model_id: str = ""


@runtime_checkable
class LLMBackend(Plugin, Protocol):
    """Runs a language model."""

    model_id: str
    context_window: int

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Answer ``request``. With a ``json_schema``, ``data`` carries the parsed result."""
        ...


# -- Enricher ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class EnrichInput:
    """A cleaned message. Everything derived from the mail is ``UntrustedText``."""

    message_id: str
    account_id: str
    subject: UntrustedText
    body: UntrustedText
    headers: dict[str, UntrustedText] = field(default_factory=dict)


@dataclass(frozen=True)
class EnrichOutput:
    """A JSON-serializable result, which must match the enricher's ``output_schema``."""

    data: dict[str, Any]


@runtime_checkable
class Enricher(Plugin, Protocol):
    """Derives something from one message. ``version`` is part of the cache key: bump it when
    the output could change."""

    version: str
    needs_llm: bool
    output_schema: dict[str, Any]

    async def enrich(self, msg: EnrichInput) -> EnrichOutput:
        """Derive this enricher's result for ``msg``."""
        ...


# -- Vector store ------------------------------------------------------------------------------


@dataclass(frozen=True)
class VectorItem:
    """One vector. The model and dimensions are recorded per vector; a store never mixes them."""

    message_id: str
    chunk_id: str
    vector: tuple[float, ...]
    model_id: str
    content_hash: str  # of the chunk text: required, so every vector can be traced to its text
    account_id: str | None = None

    def __post_init__(self) -> None:
        if not self.content_hash:
            raise ValueError("a VectorItem needs the content_hash of the chunk it embeds")

    @property
    def dimensions(self) -> int:
        return len(self.vector)


@dataclass(frozen=True)
class Hit:
    message_id: str
    chunk_id: str
    score: float  # higher is closer
    model_id: str = ""


@dataclass(frozen=True)
class VectorStats:
    count: int
    model_id: str | None = None
    dimensions: int | None = None


@runtime_checkable
class VectorStore(Plugin, Protocol):
    """Stores vectors and finds the nearest ones."""

    async def upsert(self, items: Sequence[VectorItem]) -> None:
        """Insert or replace by (message_id, chunk_id). Raises if an item's model or dimensions
        differ from what the store already holds, leaving the store unchanged."""
        ...

    async def delete(self, message_ids: Sequence[str]) -> None:
        """Remove every vector of these messages."""
        ...

    async def query(
        self, vector: Sequence[float], k: int, filters: dict[str, str] | None = None
    ) -> list[Hit]:
        """The ``k`` nearest, best first. ``filters`` may name ``account_id`` and ``model_id``;
        a store that sits beside the index may take more (``thread_key``, ``after``, ``before``,
        ``sender``) and must refuse a filter it does not know rather than ignore it.
        Raises if ``vector`` does not match the stored dimensions."""
        ...

    async def stats(self) -> VectorStats:
        """How many vectors, and which model and dimensions they use."""
        ...
