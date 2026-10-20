# SPDX-License-Identifier: Apache-2.0
"""Build a mailbox from the synthetic corpus, run the retrievers over its questions, score them.

Everything goes through the real ingest pipeline (sync, parse, index, embed) and the real
``Retrieval`` object; only the models are stand-ins offline (``ConceptEmbedder`` and the extractive
``reader``). With ``live`` models the same run measures a real embedder and language model.
"""

import contextlib
import re
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from oaken.ai import LLMCache, LLMService, Workspaces
from oaken.ai.access import WorkspaceLLM
from oaken.core.attrs import AttrStore
from oaken.core.clock import FakeClock, fmt_ts
from oaken.core.db import Database, migrate
from oaken.core.events import EventBus
from oaken.core.ids import AccountId, MessageId
from oaken.core.jobs import JobQueue, JobRunner
from oaken.core.rawstore import RawStore
from oaken.core.search import Evidence, HybridFilters, HybridSearch, Retrieval
from oaken.core.search.ask import ASK, AskResult, content_hash, parts, resolve
from oaken.core.search.context import EvidenceMessage, Excerpt
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
TOP_FOR_ASK = 10


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
    summaries: bool = True,
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
                if summaries:
                    await _store_summaries(setup, clock)
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


_FIRST_SENTENCE = re.compile(r"^(.{20,200}?[.!?])(?:\s|$)", re.S)


async def _store_summaries(setup: Setup, clock: FakeClock) -> None:
    """Stand-in for the summary enricher: the first sentence of each message's cleaned text, kept
    where the real enricher keeps its result. The report says so."""
    import json

    stamp = fmt_ts(clock.now())
    for mid in setup.logical:
        doc = await setup.index.get_doc(MessageId(mid))
        if doc is None:
            continue
        text = " ".join(str(doc["cleaned_text"]).split())
        m = _FIRST_SENTENCE.match(text)
        summary = (m.group(1) if m else text[:160]).strip()
        await setup.raw.db.execute(
            "INSERT OR REPLACE INTO enrichments (message_id, enricher, version, model_id,"
            " content_hash, data_json, created_at) VALUES (?, 'summary', '1', 'eval', ?, ?, ?)",
            (mid, str(doc["content_hash"]), json.dumps({"summary": summary}), stamp),
        )


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


# -- ask ----------------------------------------------------------------------------------------
async def full_bodies(setup: Setup, ids: list[str]) -> Evidence:
    """The naive context: the whole cleaned text of each message, every chunk, no summaries, no
    budget. Built from the same pieces so it can be answered and verified the same way."""
    messages: list[EvidenceMessage] = []
    used = 0
    for rank, mid in enumerate(ids, 1):
        doc = await setup.index.get_doc(MessageId(mid))
        if doc is None:
            continue
        who = f"{doc['from_name']} <{doc['from_addr']}>"
        msg = EvidenceMessage(f"M{rank}", MessageId(mid), None, who, str(doc["subject"]))
        for row in await setup.index.chunk_rows(MessageId(mid)):
            text = str(row["text"])
            ref = f"M{rank}.{row['ordinal']}"
            msg.excerpts.append(Excerpt(ref, int(row["id"]), int(row["ordinal"]), text, text, True))
        messages.append(msg)
    count = setup.pipeline.tokenizer.count
    used = count("\n\n".join(m.render() for m in messages))
    return Evidence(tuple(messages), 10**9, used, used, used, len(messages), len(messages), 0)


@dataclass
class AskScores:
    questions: int = 0
    answered: int = 0  # said something (did not answer "not enough evidence")
    correct: int = 0  # answerable, answered, and a citation points at the planted fact
    citations: int = 0
    precise: int = 0  # citations to a relevant message whose quote holds the fact
    false_answers: int = 0  # unanswerable but answered
    abstained_right: int = 0  # unanswerable and said so
    dropped_claims: int = 0
    by_kind: dict[str, list[int]] = field(default_factory=dict)  # kind -> [questions, correct]
    evidence_tokens: float = 0.0  # mean per question that reached the reader
    reached_reader: int = 0

    @property
    def citation_precision(self) -> float:
        return self.precise / self.citations if self.citations else 0.0


def _precise(q: Question, result: AskResult, logical: dict[str, str]) -> tuple[int, int]:
    fact = {w for w in re.findall(r"\w+", q.fact.lower())}
    n = good = 0
    for c in result.citations:
        n += 1
        words = set(re.findall(r"\w+", c.quote.lower()))
        if (
            logical.get(c.message_id) in q.relevant
            and fact
            and len(fact & words) / len(fact) >= 0.7
        ):
            good += 1
    return n, good


def _tally(scores: AskScores, q: Question, result: AskResult, logical: dict[str, str]) -> None:
    scores.questions += 1
    scores.dropped_claims += result.dropped_claims
    if result.evidence_tokens:
        scores.reached_reader += 1
        scores.evidence_tokens += result.evidence_tokens
    answered = not result.insufficient_evidence
    scores.answered += answered
    n, good = _precise(q, result, logical)
    scores.citations += n
    scores.precise += good
    tally = scores.by_kind.setdefault(q.kind, [0, 0])
    tally[0] += 1
    if q.relevant:
        scores.correct += answered and good > 0
        tally[1] += answered and good > 0
    elif answered:
        scores.false_answers += 1
    else:
        scores.abstained_right += 1
        tally[1] += 1


async def ask_all(setup: Setup, questions: list[Question]) -> tuple[AskScores, AskScores, float]:
    """The context builder against the naive baseline over every question, and the mean share of
    full-body tokens the builder saved. Both ask the same reader through ``ask``'s own path."""
    built, naive = AskScores(), AskScores()
    saved: list[float] = []
    llm = setup.retrieval.llm
    assert llm is not None  # noqa: S101
    for q in questions:
        result = await setup.retrieval.ask(q.text)
        _tally(built, q, result, setup.logical)
        found, evidence = await setup.retrieval.evidence(q.text)
        if evidence.tokens_full_bodies:
            saved.append(evidence.saved_fraction)
        top = [h.message_id for h in found.hits[:TOP_FOR_ASK]]
        stuffed = await full_bodies(setup, top)
        if not stuffed.messages:
            continue
        answer = await llm.generate_cached(
            ASK, *parts(q.text, stuffed), content_hash="naive" + content_hash(q.text, stuffed),
            account_id=ACCOUNT,
        )  # fmt: skip
        _tally(naive, q, resolve(answer, stuffed), setup.logical)
    return built, naive, metrics.mean(saved)


@dataclass
class Results:
    seed: int
    messages: int
    chunks: int
    questions: dict[str, int]
    mode: str
    embedder: str
    reader: str
    summaries: str
    retrieval: dict[str, dict[str, RetrievalScores]]
    context: AskScores
    naive: AskScores
    saved_fraction: float
    budget: int
    date: str = field(default_factory=lambda: datetime.now(UTC).date().isoformat())


async def run(
    corpus: Corpus,
    embedder: Embedder,
    llm: LLMBackend,
    *,
    mode: str = "offline",
    reader: str = "extractive stand-in (not a model)",
    summaries: bool = True,
    settings: Settings | None = None,
    date: str | None = None,
) -> Results:
    if isinstance(embedder, ConceptEmbedder):
        embedder.fit(m.raw.decode(errors="replace") for m in corpus.messages)
    async with open_setup(corpus, embedder, llm, summaries=summaries, settings=settings) as setup:
        questions = corpus.questions
        ranked = {q.id: await rankings(setup, q) for q in questions if q.relevant}
        scores = score_rankings(questions, ranked)
        built, naive, saved = await ask_all(setup, questions)
        stats = await setup.index.stats()
        counts = {k: sum(q.kind == k for q in questions) for k in KINDS}
        res = Results(
            seed=corpus.seed,
            messages=stats.docs,
            chunks=stats.chunks,
            questions=counts,
            mode=mode,
            embedder=f"{embedder.model_id} ({'offline stand-in' if mode == 'offline' else 'live'})",
            reader=reader,
            summaries="first sentence (stand-in for the summary enricher)" if summaries else "none",
            retrieval=scores,
            context=built,
            naive=naive,
            saved_fraction=saved,
            budget=setup.pipeline.settings.ai.ask_context_tokens,
        )
        if date:
            res.date = date
        return res
