# SPDX-License-Identifier: Apache-2.0
"""``Retrieval``: the one object the operations (and later the API) call to search and to ask.

It owns no state of its own. It is the index, ``raw.db``, the vector seam and the embedder the
pipeline already uses, plus a language model service for ``ask``. Neither search nor ask marks
anything read: a result is a *reference* a client can open, and opening is what marks read.
"""

import dataclasses
from collections.abc import Callable
from typing import TYPE_CHECKING

from oaken.core.errors import InvalidInput, Unavailable
from oaken.core.rawstore import RawStore
from oaken.core.search.ask import ASK, NOT_ENOUGH, AskResult, content_hash, parts, resolve
from oaken.core.search.context import ContextBuilder, Evidence
from oaken.core.search.hybrid import HybridFilters, HybridResult, HybridSearch
from oaken.core.search.query import keyword_text
from oaken.core.search.rewrite import rewriter
from oaken.core.settings import Settings
from oaken.index.store import IndexStore
from oaken.ingest.backends import Vectors
from oaken.mail.tokens import Tokenizer
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
        tokenizer: Tokenizer,
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
        self.context = ContextBuilder(index, raw, tokenizer)

    @classmethod
    def from_pipeline(cls, pipeline: "Pipeline", llm: LLMAccess | None = None) -> "Retrieval":
        """Retrieval over what an ingest pipeline has built: its index, vectors and embedder."""
        return cls(
            pipeline.index,
            pipeline.raw,
            pipeline.vectors,
            pipeline.backends.embedder,
            pipeline.settings,
            pipeline.tokenizer,
            llm,
        )

    async def search(
        self, text: str, filters: HybridFilters | None = None, k: int = 20
    ) -> HybridResult:
        return await self.hybrid.search(text, filters, k)

    async def evidence(
        self, question: str, filters: HybridFilters | None = None
    ) -> tuple[HybridResult, Evidence]:
        """What ``ask`` would send for ``question``: the hits and the context built from them."""
        found = await self.hybrid.search(
            question,
            filters,
            self.settings.search.ask_hits,
            keyword_text=keyword_text(question),
            keyword_any=True,
        )
        return found, await self.context.build(found.hits, self.settings.ai.ask_context_tokens)

    async def ask(self, question: str, filters: HybridFilters | None = None) -> AskResult:
        """A cited answer from the mail, or ``insufficient_evidence``. Only mail from one account
        is ever put in one prompt (an account decides its workspace, and so which models may see
        it); a question that finds mail in several asks for an account."""
        if self.llm is None:
            raise Unavailable("the language model service is not running")
        found, evidence = await self.evidence(question, filters)
        accounts = {h.hit.account_id for h in found.hits}
        if len(accounts) > 1:
            raise InvalidInput("that question matches mail in more than one account: name one")
        if not any(m.excerpts for m in evidence.messages):  # nothing to quote: do not ask
            return AskResult(
                NOT_ENOUGH,
                (),
                "low",
                True,
                degraded=found.degraded,
                degraded_reason=found.degraded_reason,
                warnings=found.warnings,
                hits=evidence.hits_included,
                evidence_tokens=evidence.tokens_used,
                full_body_tokens=evidence.tokens_full_bodies,
            )
        answer = await self.llm.generate_cached(
            ASK,
            *parts(question, evidence),
            content_hash=content_hash(question, evidence),
            account_id=next(iter(accounts)),
        )
        return dataclasses.replace(
            resolve(answer, evidence),
            degraded=found.degraded,
            degraded_reason=found.degraded_reason,
            warnings=found.warnings,
        )
