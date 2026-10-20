# SPDX-License-Identifier: Apache-2.0
"""Hybrid search: BM25 keyword and vector nearest-neighbour results, fused by rank.

Both retrievers run at once. Their results are cut to one entry per message (a message's best
chunk), held to the same filters, ranked inside each retriever, and merged with Reciprocal Rank
Fusion: a message scores ``sum(weight / (rrf_k + rank))`` over the retrievers that found it, so a
message both agree on beats one only a single retriever liked, and the scores of two unrelated
scales never have to be compared.

The vector side is optional. With no embedder, no vector store, vectors built by another model, a
timeout or any error, the result is the keyword result with ``degraded`` set and a reason. Search
never raises because a model is away, and never marks anything read.
"""

import asyncio
import dataclasses
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Final

from oaken.core.ids import AccountId
from oaken.core.logging import get_logger
from oaken.core.rawstore import RawStore
from oaken.core.search.signals import eligible
from oaken.core.settings import IndexSettings, SearchSettings
from oaken.index.query import ParsedQuery, parse_query
from oaken.index.store import SNIPPET_FALLBACK_CHARS, IndexStore, SearchFilters, SearchHit
from oaken.ingest.backends import Vectors
from oaken.plugins.contracts import Embedder, Hit

log = get_logger("oaken.search")

MAX_RESULTS: Final = 200
MATCHED_CHUNKS_PER_MESSAGE: Final = 3
_DIGIT = re.compile(r"\d")
Rewriter = Callable[[str, str | None], Awaitable[str | None]]


@dataclass(frozen=True, slots=True)
class HybridFilters:
    """What narrows a search. ``index`` is applied by the index itself; the rest only ``raw.db``
    knows (read state, the triage enrichment). Operators in the query text add to these."""

    index: SearchFilters = field(default_factory=SearchFilters)
    unread_only: bool = False
    categories: tuple[str, ...] = ()
    min_priority: int | None = None


@dataclass(frozen=True, slots=True)
class ChunkMatch:
    chunk_id: int
    ordinal: int
    by: tuple[str, ...]  # "keyword", "vector"


@dataclass(frozen=True, slots=True)
class HybridHit:
    hit: SearchHit  # score is the fused score; snippet marks keyword matches
    why: tuple[str, ...]  # which retrievers found it: "keyword", "vector", or "filter"
    matched_chunks: tuple[ChunkMatch, ...] = ()
    keyword_rank: int | None = None
    vector_rank: int | None = None

    @property
    def message_id(self) -> str:
        return self.hit.message_id


@dataclass(frozen=True, slots=True)
class HybridResult:
    hits: tuple[HybridHit, ...]
    query: ParsedQuery
    degraded: bool = False
    degraded_reason: str = ""
    warnings: tuple[str, ...] = ()
    retrievers: tuple[str, ...] = ()  # those that contributed


@dataclass(slots=True)
class _Vec:
    hits: list[Hit]
    ran: bool
    reason: str = ""


class HybridSearch:
    def __init__(
        self,
        index: IndexStore,
        raw: RawStore,
        vectors: Vectors,
        embedder: Callable[[], Embedder | None],
        cfg: SearchSettings,
        index_cfg: IndexSettings | None = None,
        *,
        rewrite: Rewriter | None = None,
    ) -> None:
        self.index, self.raw, self.vectors, self.embedder = index, raw, vectors, embedder
        self.cfg = cfg
        self.index_cfg = index_cfg or IndexSettings()
        self.rewrite = rewrite

    # -- the two retrievers -----------------------------------------------------------------
    async def _vector(self, text: str, n: int, account_id: AccountId | None) -> _Vec:
        embedder = self.embedder()
        if embedder is None:
            return _Vec([], False, "no embedder is available")
        if not self.vectors.available:
            return _Vec([], False, "no vector store is available")
        try:
            stats = await self.vectors.stats()
            if stats.count == 0:
                return _Vec([], False, "no vectors have been built yet")
            if stats.model_id not in (None, embedder.model_id):
                return _Vec(
                    [], False, "the vectors were built by another model (run reprocess embed)"
                )
            filters = {"model_id": embedder.model_id}
            if account_id:
                filters["account_id"] = account_id
            async with asyncio.timeout(self.cfg.vector_timeout_seconds):
                vecs = await embedder.embed([text])
                hits = await self.vectors.query(vecs[0], n, filters)
        except Exception as exc:  # noqa: BLE001 - a plugin failing must never fail a search
            log.warning("vector search failed", error_type=type(exc).__name__)
            return _Vec([], False, f"vector search failed ({type(exc).__name__})")
        return _Vec(hits, True)

    # -- search -----------------------------------------------------------------------------
    async def search(
        self,
        text: str,
        filters: HybridFilters | None = None,
        k: int = 20,
        *,
        keyword_text: str | None = None,
        keyword_any: bool = False,
    ) -> HybridResult:
        """The ``k`` best messages for ``text``. ``keyword_text`` is what the keyword side matches
        when it should differ from what is embedded (a question with its stop words removed), and
        ``keyword_any`` matches a message with any of those words instead of all."""
        f = filters or HybridFilters()
        q = parse_query(text, self.index_cfg)
        k = max(1, min(k, MAX_RESULTS))
        unread = q.unread if q.unread is not None else (True if f.unread_only else None)
        categories = tuple(dict.fromkeys((*f.categories, *q.categories)))
        priorities = [p for p in (f.min_priority, q.min_priority) if p is not None]
        min_priority = max(priorities) if priorities else None
        index_filters = dataclasses.replace(f.index, list_all=True)

        if not q.has_text:  # nothing to rank by: the newest messages that pass the filters
            return await self._listing(
                text, q, f, index_filters, k, unread, categories, min_priority
            )

        semantic = " ".join(q.terms)
        if self.rewrite and self.cfg.llm_rewrite:
            rewritten = await self._rewritten(semantic, f.index.account_id)
            semantic = rewritten or semantic

        exact = '"' in text or any(_DIGIT.search(t) for t in q.terms)
        weights = (self.cfg.keyword_weight * (self.cfg.exact_boost if exact else 1.0),
                   self.cfg.vector_weight)  # fmt: skip
        keyword_terms = parse_query(keyword_text or text, self.index_cfg).terms
        n = min(k * self.cfg.overfetch, self.cfg.max_candidates)
        while True:
            keyword_res, vec = await asyncio.gather(
                self.index.keyword_search(
                    keyword_text or text, index_filters, n, any_term=keyword_any
                ),
                self._vector(semantic, n, f.index.account_id),
            )
            keyword_hits = keyword_res.hits
            if keyword_any:
                keyword_hits = await self._enough_terms(keyword_hits, keyword_terms)
            built = await self._fuse(
                q, f, keyword_hits, vec, k, unread, categories, min_priority, weights
            )
            exhausted = len(keyword_res.hits) < n and len(vec.hits) < n
            if len(built) >= k or exhausted or n >= self.cfg.max_candidates:
                break
            n = min(n * 4, self.cfg.max_candidates)
        retrievers = (
            *(("keyword",) if keyword_res.hits else ()),
            *(("vector",) if vec.hits else ()),
        )
        degraded = not vec.ran
        return HybridResult(
            hits=tuple(built[:k]),
            query=q,
            degraded=degraded,
            degraded_reason=vec.reason if degraded else "",
            warnings=tuple(dict.fromkeys((*q.warnings, *keyword_res.warnings))),
            retrievers=retrievers,
        )

    async def _enough_terms(
        self, hits: tuple[SearchHit, ...], terms: tuple[str, ...]
    ) -> tuple[SearchHit, ...]:
        """Of ``hits`` (which matched any word), those holding at least ``keyword_min_match`` of
        ``terms`` in their best chunk, subject or sender."""
        need = math.ceil(len(terms) * self.cfg.keyword_min_match)
        if need <= 1 or not hits:
            return hits
        chunks = await self.index.chunks_by_ids(
            [h.chunk_id for h in hits if h.chunk_id is not None]
        )
        kept: list[SearchHit] = []
        for h in hits:
            row = chunks.get(h.chunk_id) if h.chunk_id is not None else None
            hay = f"{h.subject} {h.from_name} {h.from_addr} {row['text'] if row else ''}".lower()
            if sum(t.lower() in hay for t in terms) >= need:
                kept.append(h)
        return tuple(kept)

    async def _rewritten(self, semantic: str, account_id: AccountId | None) -> str | None:
        assert self.rewrite is not None  # noqa: S101
        try:
            return await self.rewrite(semantic, account_id)
        except Exception as exc:  # noqa: BLE001 - rewriting is a nicety; search must not fail on it
            log.warning("query rewrite failed", error_type=type(exc).__name__)
            return None

    async def _fuse(
        self,
        q: ParsedQuery,
        f: HybridFilters,
        keyword_hits: tuple[SearchHit, ...],
        vec: _Vec,
        k: int,
        unread: bool | None,
        categories: tuple[str, ...],
        min_priority: int | None,
        weights: tuple[float, float],
    ) -> list[HybridHit]:
        # Collapse vector chunks to messages: first appearance is the message's best chunk.
        vec_order: list[str] = []
        vec_chunks: dict[str, list[Hit]] = {}
        for h in vec.hits:
            if h.message_id not in vec_chunks:
                vec_order.append(h.message_id)
                vec_chunks[h.message_id] = []
            vec_chunks[h.message_id].append(h)
        # Vector hits were not filtered by the index; hold them to the same rules.
        allowed_vec = await self.index.filtered_hits(vec_order, q, f.index)
        candidates = [h.message_id for h in keyword_hits] + [
            m for m in vec_order if m in allowed_vec
        ]
        passing = await eligible(
            self.raw, candidates, unread=unread, categories=categories, min_priority=min_priority
        )
        kw = [h for h in keyword_hits if h.message_id in passing]
        vo = [m for m in vec_order if m in allowed_vec and m in passing]

        kw_rank: dict[str, int] = {h.message_id: i + 1 for i, h in enumerate(kw)}
        vec_rank: dict[str, int] = {m: i + 1 for i, m in enumerate(vo)}
        kw_by_id: dict[str, SearchHit] = {h.message_id: h for h in kw}
        fused: dict[str, float] = {}
        for m in (*kw_rank, *vec_rank):
            score = 0.0
            if m in kw_rank:
                score += weights[0] / (self.cfg.rrf_k + kw_rank[m])
            if m in vec_rank:
                score += weights[1] / (self.cfg.rrf_k + vec_rank[m])
            fused[m] = score
        # Equal scores break by rank in each list, never by id, so a rerun orders them the same.
        far = len(fused) + 1
        order = sorted(
            fused, key=lambda m: (-fused[m], kw_rank.get(m, far), vec_rank.get(m, far), m)
        )[: max(k, 1) * 2]

        chunk_ids = [
            cid for m in order if m in kw_by_id and (cid := kw_by_id[m].chunk_id) is not None
        ]
        chunk_ids += [int(h.chunk_id) for m in order if m in vec_chunks
                      for h in vec_chunks[m][:MATCHED_CHUNKS_PER_MESSAGE]]  # fmt: skip
        chunks = await self.index.chunks_by_ids(chunk_ids)

        out: list[HybridHit] = []
        for m in order:
            matched: dict[int, set[str]] = {}
            kh = kw_by_id.get(m)
            if kh is not None and kh.chunk_id is not None:
                matched.setdefault(kh.chunk_id, set()).add("keyword")
            for h in vec_chunks.get(m, [])[:MATCHED_CHUNKS_PER_MESSAGE]:
                matched.setdefault(int(h.chunk_id), set()).add("vector")
            matches = tuple(
                ChunkMatch(cid, int(chunks[cid]["ordinal"]), tuple(sorted(by)))
                for cid, by in matched.items()
                if cid in chunks
            )
            matches = tuple(sorted(matches, key=lambda c: (len(c.by) * -1, c.ordinal)))
            base = kh if kh is not None else allowed_vec[m]
            snippet = base.snippet
            if not snippet and matches:
                snippet = chunks[matches[0].chunk_id]["text"][:SNIPPET_FALLBACK_CHARS]
            why = tuple(
                r for r, hit in (("keyword", m in kw_rank), ("vector", m in vec_rank)) if hit
            )
            out.append(
                HybridHit(
                    hit=dataclasses.replace(base, score=fused[m], snippet=snippet),
                    why=why,
                    matched_chunks=matches,
                    keyword_rank=kw_rank.get(m),
                    vector_rank=vec_rank.get(m),
                )
            )
        return out

    async def _listing(
        self,
        text: str,
        q: ParsedQuery,
        f: HybridFilters,
        index_filters: SearchFilters,
        k: int,
        unread: bool | None,
        categories: tuple[str, ...],
        min_priority: int | None,
    ) -> HybridResult:
        n = min(k * self.cfg.overfetch, self.cfg.max_candidates)
        while True:
            # the query's operators (from:, after:, label:) are applied by the index itself
            res = await self.index.keyword_search(text, index_filters, n)
            passing = await eligible(
                self.raw,
                [h.message_id for h in res.hits],
                unread=unread,
                categories=categories,
                min_priority=min_priority,
            )
            hits = [h for h in res.hits if h.message_id in passing]
            if len(hits) >= k or len(res.hits) < n or n >= self.cfg.max_candidates:
                break
            n = min(n * 4, self.cfg.max_candidates)
        return HybridResult(
            hits=tuple(HybridHit(hit=h, why=("filter",)) for h in hits[:k]),
            query=q,
            warnings=q.warnings,
            retrievers=("filter",),
        )
