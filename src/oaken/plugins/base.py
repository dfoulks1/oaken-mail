# SPDX-License-Identifier: Apache-2.0
"""Optional base classes that make writing a plugin short.

Subclass the one for your kind, set a few attributes and override only what your plugin does.
Everything else has a harmless default, and the capabilities of a provider are worked out from
the methods you override, so there is no second list to keep in step with the code.

    class MyEmbedder(BaseEmbedder):
        model_id = "my-model"
        dimensions = 384

        async def embed(self, texts):
            return [my_model(t) for t in texts]

Nothing requires these classes: any object with the members of a contract works, and the host
checks the same rules either way (``oaken.plugins.validate``).
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Sequence
from typing import Any, ClassVar, TypeVar

from pydantic import BaseModel

from oaken.plugins.adapt import to_enrich_output
from oaken.plugins.contracts import (
    CAPABILITIES,
    PLUGIN_API_VERSION,
    EnrichInput,
    EnrichOutput,
    Health,
    Hit,
    LLMRequest,
    LLMResponse,
    PluginContext,
    PluginKind,
    PullBatch,
    VectorItem,
    VectorStats,
)

F = TypeVar("F", bound=Callable[..., Any])


def _default(fn: F) -> F:
    """Mark a base-class method as a placeholder: overriding it is what implements it."""
    fn._oaken_default = True  # type: ignore[attr-defined]
    return fn


def is_implemented(obj: object, method: str) -> bool:
    """True if ``obj`` has a callable ``method`` that is not a base-class placeholder."""
    fn = getattr(obj, method, None)
    return callable(fn) and not getattr(fn, "_oaken_default", False)


def inferred_capabilities(obj: object, kind: str) -> frozenset[str]:
    """The capabilities of ``kind`` whose method ``obj`` really implements."""
    return frozenset(c for c, m in CAPABILITIES[kind].items() if is_implemented(obj, m))


class _Base:
    kind: ClassVar[PluginKind]
    api_version: ClassVar[int] = PLUGIN_API_VERSION
    version: ClassVar[str] = ""
    ctx: PluginContext  # set by start()

    async def start(self, ctx: PluginContext) -> None:
        self.ctx = ctx

    async def stop(self) -> None:
        return None

    async def health(self) -> Health:
        return Health(True)


class BaseProvider(_Base):
    """Override ``pull`` at least. Add ``authenticate`` and ``set_flags`` as the service allows.

    Capabilities come from what you override (``pull`` is ``sync.pull``, ``set_flags`` is
    ``flags.write``). To state them yourself, set ``capabilities = frozenset({...})``; the host
    rejects a capability whose method you did not write.
    """

    kind: ClassVar[PluginKind] = "provider"

    @_default
    async def authenticate(self, ctx: PluginContext) -> None:
        raise NotImplementedError

    @_default
    def pull(self, cursor: str | None) -> AsyncIterator[PullBatch]:
        raise NotImplementedError

    @_default
    async def set_flags(
        self, provider_ids: Sequence[str], add: Collection[str], remove: Collection[str]
    ) -> None:
        raise NotImplementedError

    @_default
    async def send(self, raw_bytes: bytes) -> str:
        raise NotImplementedError


class BaseEmbedder(_Base):
    kind: ClassVar[PluginKind] = "embedder"
    model_id: str = ""
    dimensions: int = 0
    max_input_tokens: int = 8192

    @_default
    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise NotImplementedError


class BaseLLMBackend(_Base):
    kind: ClassVar[PluginKind] = "llm_backend"
    model_id: str = ""
    context_window: int = 0

    @_default
    async def generate(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError


class BaseEnricher(_Base):
    """Set ``version`` and, ideally, ``output_model`` (a pydantic model): the host then checks the
    result against it and publishes its JSON schema. Override ``enrich``."""

    kind: ClassVar[PluginKind] = "enricher"
    version: ClassVar[str] = "1"
    needs_llm: bool = False
    output_model: ClassVar[type[BaseModel] | None] = None

    @property
    def output_schema(self) -> dict[str, Any]:
        return self.output_model.model_json_schema() if self.output_model else {}

    @_default
    async def enrich(self, msg: EnrichInput) -> EnrichOutput:
        raise NotImplementedError


class BaseVectorStore(_Base):
    kind: ClassVar[PluginKind] = "vector_store"

    @_default
    async def upsert(self, items: Sequence[VectorItem]) -> None:
        raise NotImplementedError

    @_default
    async def delete(self, message_ids: Sequence[str]) -> None:
        raise NotImplementedError

    @_default
    async def query(
        self, vector: Sequence[float], k: int, filters: dict[str, str] | None = None
    ) -> list[Hit]:
        raise NotImplementedError

    @_default
    async def stats(self) -> VectorStats:
        raise NotImplementedError


def enricher(
    name: str,
    *,
    version: str = "1",
    needs_llm: bool = False,
    output: type[BaseModel] | None = None,
) -> Callable[[Callable[[EnrichInput], Awaitable[object]]], type[BaseEnricher]]:
    """Turn one async function into an enricher class, ready to register as an entry point:

        class Result(BaseModel):
            words: int

        @enricher("word-count", output=Result)
        async def word_count(msg: EnrichInput) -> Result:
            return Result(words=len(msg.body.unwrap().split()))

    The function may return the model, a dict or an ``EnrichOutput``; the host coerces it.
    """

    def wrap(fn: Callable[[EnrichInput], Awaitable[object]]) -> type[BaseEnricher]:
        async def enrich(self: BaseEnricher, msg: EnrichInput) -> EnrichOutput:
            return to_enrich_output(await fn(msg), name, output)

        return type(
            fn.__name__,
            (BaseEnricher,),
            {
                "name": name,
                "version": version,
                "needs_llm": needs_llm,
                "output_model": output,
                "enrich": enrich,
                "__doc__": fn.__doc__,
                "__module__": fn.__module__,
            },
        )

    return wrap
