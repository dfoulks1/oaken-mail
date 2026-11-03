# SPDX-License-Identifier: Apache-2.0
"""``Retrieval``: the one object the operations (and later the API) call to search.

It owns no state of its own. It is the index, ``raw.db``, the vector seam and the embedder the
pipeline already uses, plus a language model service for query rewriting. Search marks nothing
read: a result is a *reference* a client can open, and opening is what marks read.
"""

from collections.abc import Callable
from typing import TYPE_CHECKING

from oaken.core.rawstore import RawStore
from oaken.core.search.hybrid import HybridFilters, HybridResult, HybridSearch
from oaken.core.search.rewrite import rewriter
from oaken.core.settings import Settings
from oaken.index.store import IndexStore
from oaken.ingest.backends import Vectors
from oaken.plugins.contracts import Embedder, LLMAccess

if TYPE_CHECKING:
    from oaken.ingest.pipeline import Pipeline


class Retrieval:
    def __init__(
        self,
        index: IndexStore,
        raw: RawStore,
        vectors: Vectors,
        embedder: Callable[[], Embedder | None],
        settings: Settings,
        llm: LLMAccess | None = None,
    ) -> None:
        self.settings, self.llm = settings, llm
        self.hybrid = HybridSearch(
            index,
            raw,
            vectors,
            embedder,
            settings.search,
            settings.index,
            rewrite=rewriter(llm) if llm is not None and settings.search.llm_rewrite else None,
        )

    @classmethod
    def from_pipeline(cls, pipeline: "Pipeline", llm: LLMAccess | None = None) -> "Retrieval":
        """Retrieval over what an ingest pipeline has built: its index, vectors and embedder."""
        return cls(
            pipeline.index,
            pipeline.raw,
            pipeline.vectors,
            pipeline.backends.embedder,
            pipeline.settings,
            llm,
        )

    async def search(
        self, text: str, filters: HybridFilters | None = None, k: int = 20, *, offset: int = 0
    ) -> HybridResult:
        return await self.hybrid.search(text, filters, k, offset=offset)
