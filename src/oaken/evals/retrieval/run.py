# SPDX-License-Identifier: Apache-2.0
"""Build a mailbox from the synthetic corpus, run the retrievers over its questions, score them.

Everything goes through the real ingest pipeline (sync, parse, index, embed) and the real
``Retrieval`` object; only the models are stand-ins offline (``ConceptEmbedder``). With ``live`` models the same run measures a real embedder.
"""

import contextlib
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from oaken.ai import LLMCache, LLMService, Workspaces
from oaken.ai.access import WorkspaceLLM
from oaken.core.attrs import AttrStore
from oaken.core.clock import FakeClock
from oaken.core.db import Database, migrate
from oaken.core.events import EventBus
from oaken.core.ids import AccountId
from oaken.core.jobs import JobQueue, JobRunner
from oaken.core.rawstore import RawStore
from oaken.core.search import HybridFilters, HybridSearch, Retrieval
from oaken.core.search.query import keyword_text
from oaken.core.settings import (
    AISettings,
    AttachmentSettings,
    JobsSettings,
    MailSettings,
    Settings,
)
from oaken.evals.retrieval import metrics
from oaken.evals.retrieval.corpus import KINDS, Corpus, Question
from oaken.evals.retrieval.embedder import ConceptEmbedder
from oaken.index import Indexer, IndexStore
from oaken.index.store import SearchFilters
from oaken.ingest import Backends, Pipeline, SyncRunner, UsageStore
from oaken.mail.filtering import FilterEngine
from oaken.plugins.contracts import Embedder, LLMBackend
from oaken.testing.fakes import FakeProvider, InMemoryVectorStore

MIGRATIONS = Path(__file__).resolve().parents[2] / "core" / "migrations"
ACCOUNT = AccountId("eval")
RETRIEVERS = ("keyword", "vector", "hybrid", "hybrid-plain")
DEPTH = 50  # how far down each ranking is kept


@dataclass
class Setup:
    raw: RawStore
    index: IndexStore
    pipeline: Pipeline
    retrieval: Retrieval
    embedder: Embedder
    store: InMemoryVectorStore
    logical: dict[str, str] = field(default_factory=dict)  # message id -> corpus id ("e12")
    message_of: dict[str, str] = field(default_factory=dict)  # corpus id -> message id


@contextlib.asynccontextmanager
async def open_setup(
    corpus: Corpus,
    embedder: Embedder,
    llm: LLMBackend,
    *,
    settings: Settings | None = None,
    where: Path | None = None,
) -> AsyncIterator[Setup]:
    """A throwaway mailbox holding ``corpus``, fully ingested and embedded."""
    settings = settings or Settings(
        ai=AISettings(embedder=embedder.model_id),
        jobs=JobsSettings(workers=1, max_attempts=3),
        mail=MailSettings(attachments=AttachmentSettings(quarantine=False)),
    )
    with tempfile.TemporaryDirectory() as scratch:
        tmp = where or Path(scratch)
        clock, bus = FakeClock(), EventBus()
        async with await Database.open(tmp / "raw.db", read_pool_size=2) as raw_db:
            await migrate(raw_db, MIGRATIONS)
            raw = RawStore(raw_db, clock, tmp / "lfs", bus=bus)
            await raw.create_account(ACCOUNT, "fake", "Eval")
            attrs = AttrStore(raw_db, clock)
            index = await IndexStore.open(
                tmp / "index.db", clock, raw_store_id=await raw.store_id()
            )
            try:
                indexer = Indexer(raw, index, attrs, settings.mail, clock, bus=bus)
                indexer.follow_filtering()
                queue = JobQueue(raw_db, clock, bus, settings=settings.jobs)
                runner = JobRunner(queue, workers=1)
                usage = UsageStore(raw_db, clock)
                store = InMemoryVectorStore()
                (tmp / "sieve").mkdir(exist_ok=True)
                engine = FilterEngine(
                    raw, attrs, settings.mail, clock, directory=tmp / "sieve", bus=bus
                )
                backends = Backends(
                    embedder=lambda: embedder, llm=lambda: llm, vector_store=lambda: store
                )
                pipeline = Pipeline(
                    raw, index, attrs, indexer, queue, usage, backends, settings, clock,
                    filters=engine, bus=bus,
                )  # fmt: skip
                pipeline.register(runner)
                provider = FakeProvider(batch_size=200)
                pids = {m.provider_id: provider.add_message(m.raw) for m in corpus.messages}
                await SyncRunner(raw, pipeline, clock, bus=bus).sync(ACCOUNT, provider)
                await _drain(runner, clock)
                retrieval = _retrieval(pipeline, raw, usage, llm, settings, clock, raw_db)
                setup = Setup(raw, index, pipeline, retrieval, embedder, store)
                for logical, pid in pids.items():
                    mid = await raw.message_id_for(ACCOUNT, pid)
                    if mid is not None:
                        setup.logical[mid] = logical
                        setup.message_of[logical] = mid
                yield setup
            finally:
                await index.close()


def _retrieval(
    pipeline: Pipeline, raw: RawStore, usage: UsageStore, llm: LLMBackend, settings: Settings,
    clock: FakeClock, raw_db: Database,
) -> Retrieval:  # fmt: skip
    service = LLMService(
        LLMCache(raw_db, clock), usage, Workspaces(settings), lambda _n: llm, settings
    )
    return Retrieval.from_pipeline(pipeline, WorkspaceLLM(service, raw))


async def _drain(runner: JobRunner, clock: FakeClock) -> None:
    for _ in range(3):
        await runner.drain()
        clock.advance(hours=2)





# -- retrieval ----------------------------------------------------------------------------------
async def rankings(setup: Setup, q: Question) -> dict[str, list[str]]:
    """Corpus ids, best first, from each retriever alone and from the two fused."""
    words = keyword_text(q.text)
    kw = await setup.index.keyword_search(words, SearchFilters(list_all=True), DEPTH, any_term=True)
    vec = await setup.embedder.embed([q.text])
    chunk_hits = await setup.store.query(vec[0], DEPTH * 4, {"model_id": setup.embedder.model_id})
    by_vector = list(dict.fromkeys(h.message_id for h in chunk_hits))[:DEPTH]
    fused = await setup.retrieval.hybrid.search(
        q.text, HybridFilters(), DEPTH, keyword_text=words, keyword_any=True
    )
    flat = setup.pipeline.settings.search.model_copy(
        update={"exact_boost": 1.0, "keyword_min_match": 0.0}
    )
    plain = await HybridSearch(
        setup.index, setup.raw, setup.pipeline.vectors, setup.pipeline.backends.embedder, flat,
        setup.pipeline.settings.index,
    ).search(q.text, HybridFilters(), DEPTH, keyword_text=words, keyword_any=True)  # fmt: skip
    name = setup.logical.get
    return {
        "keyword": [str(name(h.message_id)) for h in kw.hits],
        "vector": [str(name(m)) for m in by_vector],
        "hybrid": [str(name(h.message_id)) for h in fused.hits],
        "hybrid-plain": [str(name(h.message_id)) for h in plain.hits],
    }


@dataclass
class RetrievalScores:
    recall_5: float
    recall_10: float
    mrr: float
    ndcg_10: float


def score_rankings(
    questions: list[Question], ranked: dict[str, dict[str, list[str]]]
) -> dict[str, dict[str, RetrievalScores]]:
    """``{retriever: {"all" or a kind: scores}}`` over the answerable questions."""
    out: dict[str, dict[str, RetrievalScores]] = {}
    answerable = [q for q in questions if q.relevant]
    for retriever in RETRIEVERS:
        out[retriever] = {}
        for label in ("all", *(k for k in KINDS if k != "unanswerable")):
            qs = [q for q in answerable if label in ("all", q.kind)]
            if qs:
                out[retriever][label] = _scores([ranked[q.id][retriever] for q in qs], qs)
    return out


def _scores(rankings_: list[list[str]], qs: list[Question]) -> RetrievalScores:
    rel = [set(q.relevant) for q in qs]
    pairs = list(zip(rankings_, rel, strict=True))
    return RetrievalScores(
        recall_5=metrics.mean([metrics.recall_at(r, s, 5) for r, s in pairs]),
        recall_10=metrics.mean([metrics.recall_at(r, s, 10) for r, s in pairs]),
        mrr=metrics.mean([metrics.reciprocal_rank(r, s) for r, s in pairs]),
        ndcg_10=metrics.mean([metrics.ndcg_at(r, s, 10) for r, s in pairs]),
    )


@dataclass
class Results:
    seed: int
    messages: int
    chunks: int
    questions: dict[str, int]
    mode: str
    embedder: str
    retrieval: dict[str, dict[str, RetrievalScores]]
    date: str = field(default_factory=lambda: datetime.now(UTC).date().isoformat())


async def run(
    corpus: Corpus,
    embedder: Embedder,
    llm: LLMBackend,
    *,
    mode: str = "offline",
    settings: Settings | None = None,
    date: str | None = None,
) -> Results:
    if isinstance(embedder, ConceptEmbedder):
        embedder.fit(m.raw.decode(errors="replace") for m in corpus.messages)
    async with open_setup(corpus, embedder, llm, settings=settings) as setup:
        questions = corpus.questions
        ranked = {q.id: await rankings(setup, q) for q in questions if q.relevant}
        scores = score_rankings(questions, ranked)
        stats = await setup.index.stats()
        counts = {k: sum(q.kind == k for q in questions) for k in KINDS}
        res = Results(
            seed=corpus.seed,
            messages=stats.docs,
            chunks=stats.chunks,
            questions=counts,
            mode=mode,
            embedder=f"{embedder.model_id} ({'offline stand-in' if mode == 'offline' else 'live'})",
            retrieval=scores,
        )
        if date:
            res.date = date
        return res
