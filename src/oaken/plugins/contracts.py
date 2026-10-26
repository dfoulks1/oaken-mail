# SPDX-License-Identifier: Apache-2.0
"""The six plugin contracts: typing Protocols plus the plain-data types that cross them.

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
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from oaken.core.clock import Clock
from oaken.core.errors import PluginError
from oaken.core.events import Event, EventBus, PluginEvent
from oaken.core.settings import Settings
from oaken.core.untrusted import UntrustedText

if TYPE_CHECKING:  # ``oaken.ai.prompts`` imports this module
    from oaken.ai.prompts import PromptBuilder

# 2: LLMBackend.generate gained the keyword-only `model` (per-call model selection), CORE_42.
PLUGIN_API_VERSION = 2

PluginKind = Literal[
    "provider", "embedder", "llm_backend", "enricher", "vector_store", "cache_store"
]
PLUGIN_KINDS: tuple[PluginKind, ...] = (
    "provider",
    "embedder",
    "llm_backend",
    "enricher",
    "vector_store",
    "cache_store",
)

# Python entry-point group for each kind. First-party plugins register the same way.
ENTRY_POINT_GROUPS: dict[str, str] = {
    "provider": "oaken.providers",
    "embedder": "oaken.embedders",
    "llm_backend": "oaken.llm_backends",
    "enricher": "oaken.enrichers",
    "vector_store": "oaken.vector_stores",
    "cache_store": "oaken.cache_stores",
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
    "cache_store": {},
}

# Members a plugin of each kind must have, besides the methods its capabilities require.
REQUIRED_MEMBERS: dict[str, list[str]] = {
    "provider": [],
    "embedder": ["model_id", "dimensions", "embed"],
    "llm_backend": ["model_id", "context_window", "generate"],
    "enricher": ["version", "needs_llm", "enrich"],
    "vector_store": ["upsert", "delete", "query", "stats"],
    "cache_store": ["key", "get", "put", "stats", "clear"],
}


@dataclass(frozen=True)
class PluginInfo:
    """What the host knows about a loaded plugin."""

    name: str
    version: str
    kind: PluginKind
    capabilities: frozenset[str]
    api_version: int


class LLMAccess(Protocol):
    """The one way a plugin reaches a language model: a ``PromptBuilder`` task about mail, as
    ``UntrustedText`` parts. Core picks the workspace from the account, enforces what it allows,
    answers a repeated question from its cache and validates the answer against the task's schema.
    A plugin never sees a backend, a database or a workspace."""

    async def generate_cached(
        self,
        builder: "PromptBuilder[Any]",
        *parts: UntrustedText,
        content_hash: str,
        account_id: str | None = None,
        message_id: str | None = None,
        model: str | None = None,
    ) -> Any: ...


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
    # Set when the host started this instance for one account (a provider serving several
    # accounts runs one instance each). ``config`` is then the plugin's table with that account's
    # own settings on top. None for the plugin's shared instance.
    account_id: str | None = None
    # Core's language-model access (see ``LLMAccess``), for a plugin that ``needs_llm``. None in a
    # context built without it (most tests); calling it before core has one raises PluginError.
    llm: LLMAccess | None = None
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


# -- Operations a plugin offers ----------------------------------------------------------------
# Any plugin, of any kind, may offer operations by defining ``operations()``. Core adds them to the
# operation registry as ``<plugin>.<name>``, so the CLI, the UI and (if the user allows it) MCP get
# them with the same permission classes, confirm gate and audit as core's own.


@dataclass(frozen=True)
class PluginOpContext:
    """What an operation handler of a plugin receives besides its input. No database handle: a
    plugin reaches its own service, its ``config`` and the clock, nothing else."""

    plugin: str
    client: str  # which door the call came through: cli, ui, mcp or internal
    actor: str  # who: a label, never a secret
    config: dict[str, Any]
    clock: Clock
    log: Any


@dataclass(frozen=True)
class PluginChange:
    """One line of a proposal: what would change. ``target`` is an id, never mail text."""

    target: str
    field: str = ""
    before: Any = None
    after: Any = None


@dataclass(frozen=True)
class PluginPreview:
    """What a mutating operation would do, shown to the person before it does it."""

    summary: str
    changes: tuple[PluginChange, ...] = ()
    total: int = 0


PluginOpHandler = Callable[[PluginOpContext, Any], Awaitable[Any]]
PluginOpPreview = Callable[[PluginOpContext, Any], Awaitable[PluginPreview]]


@dataclass(frozen=True)
class PluginOperation:
    """One operation a plugin offers.

    ``permission`` is ``read`` or ``mutate`` (a plugin cannot send). A ``mutate`` is confirmed by
    the person at the CLI or UI, and by proposal and token over MCP; give it a ``preview`` to say
    what it would change. ``mcp`` is ``off`` (the user may turn it on) or ``never`` (they may not):
    a plugin's operations are never on by default. ``input`` and ``output`` are pydantic models.
    """

    name: str
    summary: str
    permission: Literal["read", "mutate"]
    input: type[BaseModel]
    output: type[BaseModel]
    handler: PluginOpHandler
    preview: PluginOpPreview | None = None
    mcp: Literal["off", "never"] = "off"
    # True: the input has an ``account_id`` and core runs the handler on that account's own
    # instance of the plugin (its host, its login), not on the shared one. An empty ``account_id``
    # means the only account this plugin has. Offered even while the plugin is unhealthy or not
    # set up, because that is when you need it (signing in).
    per_account: bool = False


# -- Job kinds a plugin offers -----------------------------------------------------------------
# A plugin may run its own background work by defining ``job_handlers()``. Core registers each on
# the job runner as ``plugin.<name>.<kind>`` (namespaced so two plugins cannot collide), with the
# same durable queue, retries, leases and per-kind concurrency as core's own kinds. The handler is
# given a plugin-scoped context (no database handle) and its validated payload; it never touches
# the queue, the stores or another plugin.


@dataclass(frozen=True)
class PluginJobContext:
    """What a plugin's job handler receives besides its payload. Like ``PluginOpContext``: the
    plugin's own ``config``, the clock and a logger, and the registered ``kind`` it is running —
    no database handle, no queue, no job internals."""

    plugin: str
    kind: str  # the namespaced kind, e.g. "plugin.word-count.recount"
    config: dict[str, Any]
    clock: Clock
    log: Any


# ``handler(ctx, payload) -> result | None``. ``payload`` is an instance of the handler's
# ``payload_model``; a returned value must match ``result_model`` when one is declared.
PluginJobHandlerFn = Callable[["PluginJobContext", Any], Awaitable[Any]]


@dataclass(frozen=True)
class PluginJobHandler:
    """One job kind a plugin offers.

    ``kind`` is the plugin's local name (``recount``); core namespaces it to
    ``plugin.<name>.<kind>``. ``payload_model`` and ``result_model`` are pydantic models: the
    payload is validated before the handler runs, and a result (if ``result_model`` is set) is
    validated and stored, exactly as for a core job kind. ``concurrency`` caps how many run at
    once (``[jobs] kind_limits`` still wins). A plugin enqueues its kind with ``ctx`` helpers added
    later; for now core and other plugins can enqueue it by its namespaced name.
    """

    kind: str
    payload_model: type[BaseModel]
    handler: PluginJobHandlerFn
    concurrency: int | None = None
    result_model: type[BaseModel] | None = None


# -- Pipeline stages a plugin offers -----------------------------------------------------------
# A plugin may add a derivation stage that runs **after the enrichers** for a message, by defining
# ``pipeline_stages()``. It sees the message and the enrichment results just computed, and returns
# JSON-serializable data core stores with the same content-hash idempotency as an enricher — so a
# rerun or a reprocess is a cache hit, never recomputed. A stage never touches the databases, runs
# a model directly, or sees another plugin's state; it is for cheap, deterministic post-processing
# (a tag derived from triage, a cross-enricher roll-up).


@dataclass(frozen=True)
class PluginStageContext:
    """What a plugin's pipeline stage receives besides its input: the plugin's own ``config``, the
    clock and a logger. No database handle, no queue."""

    plugin: str
    stage: str
    config: dict[str, Any]
    clock: Clock
    log: Any


@dataclass(frozen=True)
class PluginStageInput:
    """The message a stage runs on, after its enrichers. Everything derived from the mail is
    ``UntrustedText``. ``enrichments`` is the message's stored enrichment results by enricher name
    (``summary``, ``triage``, ...), so a stage can roll up or tag from them without re-reading the
    body."""

    message_id: str
    account_id: str
    subject: UntrustedText
    body: UntrustedText
    enrichments: dict[str, dict[str, Any]] = field(default_factory=dict)


# ``handler(ctx, msg) -> dict``. The returned dict must be JSON-serializable; core stores it under
# the stage's name with content-hash idempotency.
PluginStageFn = Callable[["PluginStageContext", "PluginStageInput"], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class PluginStage:
    """One pipeline stage a plugin offers, run after the enrichers for each message.

    ``name`` is the plugin's local stage name; core stores its result under
    ``plugin.<plugin>.<name>``. ``version`` is part of the idempotency key: bump it when the output
    could change, and a rerun recomputes; leave it and a rerun is a cache hit.
    """

    name: str
    handler: PluginStageFn
    version: str = "1"


# -- Scheduled tasks a plugin offers -----------------------------------------------------------
# A plugin may schedule recurring work by defining ``scheduled_tasks()``. Core seeds each into the
# scheduler as ``plugin.<name>.<task>`` **only if it is absent**, so a schedule the user has since
# edited (interval, cron, or removed) is left alone — the same rule as the per-account sync
# schedules. The task fires a ``job_kind`` (usually one the plugin also registers via
# ``job_handlers()``) with a fixed ``payload``.


@dataclass(frozen=True)
class PluginScheduledTask:
    """One recurring task a plugin offers. Give exactly one of ``every`` (seconds) or ``cron`` (a
    five-field expression). ``job_kind`` is the kind enqueued when it fires — commonly the plugin's
    own ``plugin.<name>.<kind>`` — and ``payload`` is the fixed input for that job.
    """

    name: str
    job_kind: str
    payload: BaseModel
    every: float | None = None
    cron: str | None = None


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

    def pull(self, cursor: str | None, folder: str | None = None) -> AsyncIterator[PullBatch]:
        """Yield batches of changes since ``cursor`` (None: from the start). Capability
        ``sync.pull``. Re-pulling from the last cursor returns nothing new.

        ``folder`` limits the pull to one folder, for a provider with folders (set
        ``supports_folders = True``); the cursor is still the account's whole cursor, and a
        folder pull only moves that folder's part of it. Core passes ``folder`` only to a provider
        that supports it, and only when the user scheduled that folder on its own."""
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

    async def generate(self, request: LLMRequest, *, model: str | None = None) -> LLMResponse:
        """Answer ``request``. With a ``json_schema``, ``data`` carries the parsed result. ``model``
        names which of the backend's models to run; ``None`` means the started default
        (``model_id``). The response's ``model_id`` is the model that actually ran."""
        ...

    async def available_models(self) -> list[str]:
        """The model ids this backend can run right now, for a picker to offer."""
        ...


# -- Enricher ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class EnrichAttachment:
    """What the message says about one attachment. The bytes are never passed to an enricher."""

    filename: UntrustedText | None
    content_type: str
    size: int
    inline: bool = False
    quarantine: bool = False


@dataclass(frozen=True)
class EnrichInput:
    """A cleaned message. Everything derived from the mail is ``UntrustedText``.

    ``headers`` holds a few display values (``from``). ``raw_headers`` has every header, keys in
    lower case and every value kept, for a deterministic enricher such as the inspector.
    ``parent_summary`` is the stored summary of the message this one replies to, when core has
    one: a reply is summarized from its own new text plus that, never from the parent's body.
    """

    message_id: str
    account_id: str
    subject: UntrustedText
    body: UntrustedText
    headers: dict[str, UntrustedText] = field(default_factory=dict)
    raw_headers: dict[str, tuple[UntrustedText, ...]] = field(default_factory=dict)
    attachments: tuple[EnrichAttachment, ...] = ()
    date: datetime | None = None
    is_reply: bool = False
    parent_summary: UntrustedText | None = None


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


@dataclass(frozen=True)
class CacheEntry:
    """A validated model answer and what it cost to get, as the LLM answer cache stores it."""

    result_json: str
    tokens_in: int
    tokens_out: int


@dataclass(frozen=True)
class CacheStats:
    entries: int
    bytes: int = 0
    by_task: dict[str, int] = field(default_factory=dict)
    prompt_versions: tuple[str, ...] = ()
    oldest: datetime | None = None


class CacheOps(Protocol):
    """The LLM answer cache's members. Core's built-in SQLite cache has them too (it is not a
    plugin). Core hands over the facts of a question; the cache owns the key."""

    def key(
        self, *, workspace: str, task: str, content_hash: str, model: str, prompt_version: str
    ) -> tuple[str, ...]:
        """The key for this question. ``model`` is the model that will answer it, never a
        backend's default: two models must not share a key."""
        ...

    async def get(self, key: tuple[str, ...]) -> CacheEntry | None:
        """The stored answer, or None. A miss costs one model call; raising counts as a miss."""
        ...

    async def put(self, key: tuple[str, ...], entry: CacheEntry) -> None:
        """Store ``entry``. Raising loses the cache write, never the answer."""
        ...

    async def stats(self, workspace: str | None = None) -> CacheStats:
        """What the cache holds (one workspace's, or all)."""
        ...

    async def clear(self, workspace: str | None = None, task: str | None = None) -> int:
        """Forget answers (one workspace's and/or one task's, or all). How many were removed."""
        ...


@runtime_checkable
class Cache(Plugin, CacheOps, Protocol):
    """An LLM answer cache plugin."""
