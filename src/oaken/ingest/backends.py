# SPDX-License-Identifier: Apache-2.0
"""How the pipeline reaches the configured plugins, and the one seam in front of the vector store.

``Backends`` holds callables, not plugins, so the pipeline asks for the embedder (or the store)
when it needs it: a plugin that restarts, becomes unhealthy or is replaced by a setting is picked
up on the next job, and a pipeline built for tests passes fakes the same way.

``Vectors`` is the only code in core that talks to a vector store. Several stores (see the plan
in prompt 09's notes) can later live behind it with no change to the stages; today it forwards to
the one that is configured. The model-swap methods are optional capabilities of a store: one that
lacks them can still be used for a single model.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from oaken.core.errors import OakenError, PluginError
from oaken.plugins.contracts import (
    Embedder,
    Enricher,
    Hit,
    LLMBackend,
    PluginStageContext,
    VectorItem,
    VectorStats,
    VectorStore,
)
from oaken.plugins.host import OfferedPipelineStage, PluginHost


def _none() -> None:
    return None


def _no_stages() -> list["OfferedPipelineStage"]:
    return []


@dataclass
class Backends:
    embedder: Callable[[], Embedder | None] = _none
    llm: Callable[[], LLMBackend | None] = _none
    enrichers: Callable[[], dict[str, Enricher]] = field(default=dict)
    vector_store: Callable[[], VectorStore | None] = _none
    # Plugin pipeline stages (run after the enrichers) and a factory for a stage's context.
    pipeline_stages: Callable[[], list["OfferedPipelineStage"]] = _no_stages
    stage_context: Callable[[str, str], "PluginStageContext"] | None = None

    @classmethod
    def from_host(cls, host: PluginHost) -> "Backends":
        """Whatever the host has started and healthy; nothing (not an error) when a kind is
        missing or down, so keyword indexing carries on while a model is away."""

        def quietly(get: Callable[[], Any]) -> Callable[[], Any]:
            def call() -> Any:
                try:
                    return get()
                except OakenError:  # not installed, not started, or unhealthy
                    return None

            return call

        return cls(
            embedder=quietly(host.embedder),
            llm=quietly(host.llm_backend),
            enrichers=host.named_enrichers,
            vector_store=quietly(host.vector_store),
            pipeline_stages=host.offered_pipeline_stages,
            stage_context=host.stage_context,
        )


class Vectors:
    """The vector store, behind one seam."""

    def __init__(self, get: Callable[[], VectorStore | None]) -> None:
        self._get = get

    def store(self) -> VectorStore | None:
        return self._get()

    def _need(self) -> VectorStore:
        store = self._get()
        if store is None:
            raise PluginError("no vector store is available")
        return store

    @property
    def available(self) -> bool:
        return self._get() is not None

    async def upsert(self, items: Sequence[VectorItem]) -> None:
        await self._need().upsert(items)

    async def delete(self, message_ids: Sequence[str]) -> None:
        store = self._get()
        if store is not None:
            await store.delete(message_ids)

    async def query(
        self, vector: Sequence[float], k: int, filters: dict[str, str] | None = None
    ) -> list[Hit]:
        return await self._need().query(vector, k, filters)

    async def stats(self) -> VectorStats:
        store = self._get()
        return await store.stats() if store is not None else VectorStats(0)

    # -- optional capabilities --------------------------------------------------------------
    async def stored_hashes(self, model_id: str, message_id: str) -> dict[str, str]:
        """``{chunk_id: content_hash}`` already embedded. A store that cannot say reports none,
        which costs a re-embed but never a wrong result (an upsert of equal content is skipped
        by stores that can tell)."""
        fn = getattr(self._need(), "stored_hashes", None)
        return dict(await fn(model_id, message_id)) if fn else {}

    async def begin_model(self, model_id: str, dimensions: int) -> bool:
        """Make room for a new model beside the active one. False if the store cannot swap."""
        fn = getattr(self._need(), "begin_model", None)
        if fn is None:
            return False
        await fn(model_id, dimensions)
        return True

    async def activate_model(self, model_id: str) -> None:
        fn = getattr(self._need(), "activate_model", None)
        if fn is not None:
            await fn(model_id)

    async def drop_model(self, model_id: str) -> int:
        fn = getattr(self._need(), "drop_model", None)
        return int(await fn(model_id)) if fn else 0

    async def prune_orphans(self) -> int:
        fn = getattr(self._need(), "prune_orphans", None)
        return int(await fn()) if fn else 0

    async def record_throughput(self, chunks: int, seconds: float) -> None:
        fn = getattr(self._need(), "record_throughput", None)
        if fn is not None:
            await fn(chunks, seconds)
