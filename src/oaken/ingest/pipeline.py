# SPDX-License-Identifier: Apache-2.0
"""The stages that run for every new or changed message, as jobs.

    stored (SyncRunner) -> ingest.parse -> ingest.embed
                                        \\-> ingest.enrich

``ingest.parse`` is the cheap, CPU-only stage: Sieve, then parse/clean/chunk/FTS, then the
quarantine of flagged attachments. When it is done the message is keyword-searchable, whatever
the models are doing. ``ingest.embed`` and ``ingest.enrich`` are queued after it, one priority
step (and two) below it, so a backlog of parsing always runs ahead of a backlog of embedding.

Every stage is idempotent and skips work whose result already exists: a chunk whose
``(content_hash, model_id)`` has a vector is not embedded again, an enrichment whose
``(content_hash, enricher, version, model_id)`` is cached is not asked for again. A crash at any
point leaves a job leased until its lease runs out; it then runs again and converges.
Message text never reaches a log.
"""

import asyncio
import json
import sqlite3
import time
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel

from oaken.ai.fresh import FRESH
from oaken.core.actors import SYSTEM, Actor
from oaken.core.attrs import AttrStore
from oaken.core.clock import Clock, fmt_ts, parse_ts
from oaken.core.errors import Conflict, PluginError
from oaken.core.events import (
    AttachmentQuarantined,
    EventBus,
    MessageEmbedded,
    MessageEnriched,
)
from oaken.core.ids import AccountId, MessageId
from oaken.core.jobs import (
    PRIORITY_BACKFILL,
    PRIORITY_INGEST,
    Enqueued,
    Job,
    JobQueue,
    JobRunner,
)
from oaken.core.logging import get_logger
from oaken.core.rawstore import RawStore
from oaken.core.settings import Settings
from oaken.core.untrusted import UntrustedText
from oaken.index import Indexer, IndexStore, RebuildPayload, enqueue_rebuild
from oaken.ingest.backends import Backends, Vectors
from oaken.ingest.quarantine import Quarantine
from oaken.ingest.usage import UsageStore
from oaken.mail.filtering import FilterEngine
from oaken.mail.parse import parse, read_attachment
from oaken.mail.threading import normalize_subject
from oaken.mail.tokens import Tokenizer, load_tokenizer
from oaken.plugins.adapt import to_enrich_output, to_vectors
from oaken.plugins.contracts import EnrichAttachment, EnrichInput, PluginStageInput, VectorItem

log = get_logger("oaken.ingest")

PARSE = "ingest.parse"
EMBED = "ingest.embed"
ENRICH = "ingest.enrich"
REPROCESS = "ingest.reprocess"
STAGE_KINDS = (PARSE, EMBED, ENRICH, REPROCESS)

Stage = Literal["parse", "filter", "embed", "enrich"]
STAGES: tuple[Stage, ...] = ("parse", "filter", "embed", "enrich")
# Embedding and enrichment queue one and two steps below parsing at the same urgency.
EMBED_STEP, ENRICH_STEP = 1, 2
PAGE = 200


# -- payloads and results ------------------------------------------------------------------
class ParsePayload(BaseModel):
    message_id: str
    changed: bool = True  # the bytes are new or different: filter again
    backfill: bool = False


class MessagePayload(BaseModel):
    message_id: str
    backfill: bool = False


class ReprocessPayload(BaseModel):
    stage: Stage
    account_id: str | None = None


class StageJobResult(BaseModel):
    done: int = 0
    skipped: int = 0
    note: str = ""


@dataclass
class EmbedResult:
    embedded: int = 0  # chunks sent to the embedder
    skipped: int = 0  # chunks that already had a vector
    stale: bool = False  # the store holds another model: nothing was done (run reprocess)


@dataclass
class EnrichResult:
    ran: int = 0
    cached: int = 0


@dataclass(frozen=True)
class StageStatus:
    pending: int = 0  # queued, waiting to run
    retrying: int = 0  # of those, ones that already failed once and are waiting out a backoff
    running: int = 0
    failed: int = 0  # parked: out of attempts
    oldest_pending_seconds: float | None = None
    per_second: float = 0.0  # recent throughput
    eta_seconds: float | None = None


@dataclass(frozen=True)
class ParkedJob:
    job_id: str
    stage: str
    message_id: str | None
    attempts: int
    error: str


@dataclass(frozen=True)
class PipelineStatus:
    stages: dict[str, StageStatus]
    docs: int
    chunks: int
    index_stale: bool
    vector_model: str | None
    vectors: int
    embedder_model: str | None
    embeddings_stale: bool  # the store holds another model than the embedder: reprocess("embed")
    backfill_paused: bool  # queue depth is at the cap
    quarantined_files: int
    parked: list[ParkedJob] = field(default_factory=list)


class Pipeline:
    def __init__(
        self,
        raw: RawStore,
        index: IndexStore,
        attrs: AttrStore,
        indexer: Indexer,
        queue: JobQueue,
        usage: UsageStore,
        backends: Backends,
        settings: Settings,
        clock: Clock,
        *,
        filters: FilterEngine | None = None,
        quarantine: Quarantine | None = None,
        bus: EventBus | None = None,
        tokenizer: Tokenizer | None = None,
    ) -> None:
        self.raw, self.index, self.attrs, self.indexer = raw, index, attrs, indexer
        self.queue, self.usage, self.backends = queue, usage, backends
        self.settings, self.clock, self.bus = settings, clock, bus
        self.filters, self.quarantine = filters, quarantine
        self.tokenizer = tokenizer or indexer.tokenizer or load_tokenizer(settings.mail)
        self.vectors = Vectors(backends.vector_store)
        self.cfg = settings.ingest

    # -- registering ------------------------------------------------------------------------
    def register(self, runner: JobRunner) -> None:
        """Handle the pipeline's job kinds. The model-bound ones are limited to the concurrency
        in ``[ingest]`` (one, by default: a single local GPU)."""
        runner.register(PARSE, ParsePayload, self._parse_job, result_model=StageJobResult)
        runner.register(
            EMBED,
            MessagePayload,
            self._embed_job,
            concurrency=self.cfg.embed_concurrency,
            result_model=StageJobResult,
        )
        runner.register(
            ENRICH,
            MessagePayload,
            self._enrich_job,
            concurrency=self.cfg.enrich_concurrency,
            result_model=StageJobResult,
        )
        runner.register(
            REPROCESS,
            ReprocessPayload,
            self._reprocess_job,
            concurrency=1,
            result_model=StageJobResult,
        )

    # -- enqueueing -------------------------------------------------------------------------
    async def enqueue_parse(
        self,
        message_id: MessageId,
        account_id: AccountId | None = None,
        *,
        changed: bool = True,
        backfill: bool = False,
        actor: Actor = SYSTEM,
    ) -> Enqueued:
        return await self.queue.enqueue(
            PARSE,
            ParsePayload(message_id=message_id, changed=changed, backfill=backfill),
            priority=PRIORITY_BACKFILL if backfill else PRIORITY_INGEST,
            account_id=account_id,
            dedupe_key=f"{PARSE}:{message_id}",
            actor=actor,
        )

    async def _enqueue_stage(
        self, kind: str, step: int, message_id: MessageId, account_id: AccountId, backfill: bool
    ) -> None:
        base = PRIORITY_BACKFILL if backfill else PRIORITY_INGEST
        await self.queue.enqueue(
            kind,
            MessagePayload(message_id=message_id, backfill=backfill),
            priority=base - step,
            account_id=account_id,
            dedupe_key=f"{kind}:{message_id}",
            max_attempts=self.cfg.stage_max_attempts,
        )

    def embedding_enabled(self) -> bool:
        return bool(self.settings.ai.embedder)

    # -- ingest.parse -----------------------------------------------------------------------
    async def _parse_job(self, p: ParsePayload, _job: Job) -> StageJobResult:
        mid = MessageId(p.message_id)
        msg = await self.raw.get_message(mid)
        if msg.deleted_at is not None:
            await self.indexer.index_one(mid)  # removes it from the index
            if self.vectors.available:
                await self.vectors.delete([mid])
            return StageJobResult(note="deleted")
        await self._filter(mid, p.changed)
        res = await self.indexer.index_one(mid)
        await self._quarantine(mid, msg.account_id)
        if self.embedding_enabled():
            await self._enqueue_stage(EMBED, EMBED_STEP, mid, msg.account_id, p.backfill)
        if self.backends.enrichers():
            await self._enqueue_stage(ENRICH, ENRICH_STEP, mid, msg.account_id, p.backfill)
        changed = res is not None and res.changed
        return StageJobResult(done=int(changed), skipped=int(not changed))

    async def _filter(self, mid: MessageId, changed: bool) -> None:
        engine = self.filters
        if engine is None or not self.settings.mail.sieve.enabled:
            return
        run = await self.attrs.filter_run(mid)
        if not changed and run is not None and run.scripts_sha256 == engine.report.sha256:
            return
        await engine.filter_message(mid)

    async def _quarantine(self, mid: MessageId, account_id: AccountId) -> int:
        """Write flagged attachments to the quarantine folder. The bytes are copied there and
        never read back, parsed or sent to a model."""
        qt = self.quarantine
        if qt is None or not self.settings.mail.attachments.quarantine:
            return 0
        doc = await self.index.get_doc(mid)
        if doc is None or not any(a.get("quarantine") for a in json.loads(doc["attachments_json"])):
            return 0
        raw = await self.raw.get_raw(mid)
        parsed = await asyncio.to_thread(parse, raw, self.settings.mail)
        count = 0
        for att in parsed.attachments:
            if not att.quarantine:
                continue
            try:
                data = await asyncio.to_thread(read_attachment, raw, att.part_path)
            except Exception as exc:  # noqa: BLE001 - an unreadable part must not fail the message
                log.warning("attachment unreadable", message_id=mid, error=type(exc).__name__)
                continue
            if await asyncio.to_thread(qt.put, mid, att.filename, att.part_path, data):
                count += 1
        if count and self.bus:
            await self.bus.publish(
                AttachmentQuarantined(
                    ts=self.clock.now(), account_id=account_id, message_id=mid, count=count
                )
            )
        return count

    # -- ingest.embed -----------------------------------------------------------------------
    async def _embed_job(self, p: MessagePayload, _job: Job) -> StageJobResult:
        r = await self.embed_message(MessageId(p.message_id))
        note = "stale: run reprocess('embed')" if r.stale else ""
        return StageJobResult(done=r.embedded, skipped=r.skipped, note=note)

    async def _skip_ai(self, mid: MessageId) -> bool:
        return any(
            a.kind == "analysis" and a.value == "skip_enrich" for a in await self.attrs.get(mid)
        )

    async def embed_message(self, message_id: MessageId, *, swapping: bool = False) -> EmbedResult:
        """Embed the chunks of one message that have no vector for the embedder's model yet.
        With ``swapping`` the vectors go to a model the store is building beside its active one."""
        out = EmbedResult()
        embedder = self.backends.embedder()
        if embedder is None or not self.vectors.available:
            raise PluginError("no embedder or vector store is available (will retry)")
        msg = await self.raw.get_message(message_id)
        if msg.deleted_at is not None:
            await self.vectors.delete([message_id])
            return out
        if await self._skip_ai(message_id):
            return out
        model = embedder.model_id
        held = (await self.vectors.stats()).model_id
        if not swapping and held not in (None, model):
            log.warning("embedder model differs from the stored vectors", embedder_model=model)
            out.stale = True
            return out
        rows = await self.index.chunk_rows(message_id)
        have = await self.vectors.stored_hashes(model, message_id)
        current = {str(r["id"]) for r in rows}
        todo = [r for r in rows if have.get(str(r["id"])) != r["content_hash"]]
        for r in rows:
            if r not in todo:
                out.skipped += 1
                await self.usage.record(
                    "embed",
                    model,
                    tokens_in=int(r["token_estimate"]),
                    cached=True,
                    account_id=msg.account_id,
                    message_id=message_id,
                )
        for start in range(0, len(todo), self.cfg.embed_batch):
            batch = todo[start : start + self.cfg.embed_batch]
            began = time.monotonic()
            vecs = to_vectors(
                await embedder.embed([r["text"] for r in batch]),
                len(batch),
                embedder.dimensions,
                f"embedder {model}",
            )
            elapsed = time.monotonic() - began
            await self.vectors.upsert(
                [
                    VectorItem(
                        message_id,
                        str(r["id"]),
                        tuple(v),
                        model,
                        r["content_hash"],
                        msg.account_id,
                    )
                    for r, v in zip(batch, vecs, strict=True)
                ]
            )
            await self.vectors.record_throughput(len(batch), max(elapsed, 1e-6))
            await self.usage.record(
                "embed",
                model,
                tokens_in=sum(int(r["token_estimate"]) for r in batch),
                account_id=msg.account_id,
                message_id=message_id,
            )
            out.embedded += len(batch)
        if set(have) - current:  # chunks that were replaced: their old vectors are orphans
            await self.vectors.prune_orphans()
        if out.embedded and self.bus:
            await self.bus.publish(
                MessageEmbedded(
                    ts=self.clock.now(),
                    account_id=msg.account_id,
                    message_id=message_id,
                    model=model,
                )
            )
        return out

    # -- ingest.enrich ----------------------------------------------------------------------
    async def _enrich_job(self, p: MessagePayload, _job: Job) -> StageJobResult:
        r = await self.enrich_message(MessageId(p.message_id))
        return StageJobResult(done=r.ran, skipped=r.cached)

    async def enrich_message(
        self,
        message_id: MessageId,
        *,
        only: Collection[str] | None = None,
        force: bool = False,
    ) -> EnrichResult:
        """Run every enricher whose result for this content is not already stored. A result
        another message with the same content produced is copied, not asked for again.

        ``only`` limits it to the named enrichers. ``force`` runs them even when a current result
        is stored and asks the model afresh (the new answer replaces the cached one)."""
        token = FRESH.set(True) if force else None
        try:
            return await self._enrich_message(message_id, only, force)
        finally:
            if token is not None:
                FRESH.reset(token)

    async def _enrich_message(
        self, message_id: MessageId, only: Collection[str] | None, force: bool
    ) -> EnrichResult:
        out = EnrichResult()
        if await self._skip_ai(message_id):
            return out
        doc = await self.index.get_doc(message_id)
        if doc is None:
            return out
        llm = self.backends.llm()
        account = AccountId(doc["account_id"])
        msg_input: EnrichInput | None = None
        for name, enricher in self.backends.enrichers().items():
            if only is not None and name not in only:
                continue
            if self.settings.plugins.get(name, {}).get("enabled") is False:
                continue  # switched off with [plugins.<name>] enabled = false
            if enricher.needs_llm and llm is None:
                raise PluginError(f"enricher {name} needs a language model, none is available")
            model = llm.model_id if enricher.needs_llm and llm else ""
            key = (doc["content_hash"], name, enricher.version, model)
            tokens = self.tokenizer.count(doc["cleaned_text"])
            if not force and await self._cached_enrichment(message_id, key):
                out.cached += 1
                await self.usage.record(
                    "enrich", model or name, tokens_in=tokens, cached=True,
                    account_id=account, message_id=message_id,
                )  # fmt: skip
                continue
            if msg_input is None:
                msg_input = await self._enrich_input(message_id, doc)
            result = to_enrich_output(
                await enricher.enrich(msg_input),
                f"enricher {name}",
                getattr(enricher, "output_model", None),
            )
            data = json.dumps(result.data, sort_keys=True)
            await self._store_enrichment(message_id, key, data)
            out.ran += 1
            await self.usage.record(
                "enrich",
                model or name,
                tokens_in=tokens,
                tokens_out=self.tokenizer.count(data),
                account_id=account,
                message_id=message_id,
            )
            if self.bus:
                await self.bus.publish(
                    MessageEnriched(
                        ts=self.clock.now(),
                        account_id=account,
                        message_id=message_id,
                        enricher=name,
                    )
                )
        await self._run_plugin_stages(message_id, doc, account, force)
        return out

    async def _run_plugin_stages(
        self, message_id: MessageId, doc: sqlite3.Row, account: AccountId, force: bool
    ) -> None:
        """Run the plugin pipeline stages (after the enrichers) for this message. Each is stored
        under ``plugin.<plugin>.<stage>`` with the same content-hash idempotency as an enricher, so
        a rerun or reprocess is a cache hit. A stage sees the message and its stored enrichments,
        never the databases; a stage failure is logged and never fails the enrich job."""
        stages = self.backends.pipeline_stages()
        make_ctx = self.backends.stage_context
        if not stages or make_ctx is None:
            return
        stage_input: PluginStageInput | None = None
        for offered in stages:
            plugin, stage = offered.plugin, offered.declaration
            full = f"plugin.{plugin}.{stage.name}"
            key = (doc["content_hash"], full, stage.version, "")
            if not force and await self._cached_enrichment(message_id, key):
                continue
            if stage_input is None:
                stage_input = await self._stage_input(message_id, doc)
            try:
                data = await stage.handler(make_ctx(plugin, stage.name), stage_input)
                payload = json.dumps(dict(data), sort_keys=True)
            except Exception as exc:  # a stage must never fail the message's enrich job
                log.error("plugin stage failed", stage=full, error=type(exc).__name__)
                continue
            await self._store_enrichment(message_id, key, payload)

    async def _stage_input(self, message_id: MessageId, doc: sqlite3.Row) -> PluginStageInput:
        """What a plugin stage is handed: the message and its stored enrichment results."""
        rows = await self.raw.db.fetchall(
            "SELECT enricher, data_json FROM enrichments WHERE message_id = ?", (message_id,)
        )
        enrichments: dict[str, dict[str, Any]] = {}
        for r in rows:
            try:
                enrichments[r["enricher"]] = json.loads(r["data_json"])
            except (TypeError, ValueError):
                continue
        return PluginStageInput(
            message_id=message_id,
            account_id=doc["account_id"],
            subject=UntrustedText(doc["subject"]),
            body=UntrustedText(doc["cleaned_text"]),
            enrichments=enrichments,
        )

    async def _enrich_input(self, message_id: MessageId, doc: sqlite3.Row) -> EnrichInput:
        """What an enricher is handed: the cleaned text, every header, the attachments' metadata
        (never their bytes) and, for a reply, the stored summary of the message before it."""
        raw = await self.raw.get_raw(message_id)
        parsed = await asyncio.to_thread(parse, raw, self.settings.mail)
        parent = await self.index.previous_in_thread(message_id)
        summary = await self.enrichment(parent, "summary") if parent else None
        parent_summary = summary.get("summary") if summary else None
        return EnrichInput(
            message_id=message_id,
            account_id=doc["account_id"],
            subject=UntrustedText(doc["subject"]),
            body=UntrustedText(doc["cleaned_text"]),
            headers={"from": UntrustedText(f"{doc['from_name']} <{doc['from_addr']}>")},
            raw_headers={
                k: tuple(UntrustedText(v) for v in vals) for k, vals in parsed.headers.items()
            },
            attachments=tuple(
                EnrichAttachment(
                    filename=UntrustedText(a.filename) if a.filename else None,
                    content_type=a.content_type,
                    size=a.size,
                    inline=a.inline,
                    quarantine=a.quarantine,
                )
                for a in parsed.attachments
            ),
            date=parsed.date,
            is_reply=bool(parsed.in_reply_to) or normalize_subject(parsed.subject)[1],
            parent_summary=UntrustedText(parent_summary)
            if isinstance(parent_summary, str)
            else None,
        )

    async def _cached_enrichment(
        self, message_id: MessageId, key: tuple[str, str, str, str]
    ) -> bool:
        """True if the result is stored for this message; if only another message has it, copy
        it here (and say True)."""
        content_hash, name, version, model = key
        mine = await self.raw.db.fetchone(
            "SELECT 1 FROM enrichments WHERE message_id = ? AND enricher = ? AND version = ?"
            " AND model_id = ? AND content_hash = ?",
            (message_id, name, version, model, content_hash),
        )
        if mine:
            return True
        other = await self.raw.db.fetchone(
            "SELECT data_json FROM enrichments WHERE content_hash = ? AND enricher = ?"
            " AND version = ? AND model_id = ? LIMIT 1",
            (content_hash, name, version, model),
        )
        if other:
            await self._store_enrichment(message_id, key, other["data_json"])
            return True
        return False

    async def _store_enrichment(
        self, message_id: MessageId, key: tuple[str, str, str, str], data: str
    ) -> None:
        content_hash, name, version, model = key
        await self.raw.db.execute(
            "INSERT INTO enrichments (message_id, enricher, version, model_id, content_hash,"
            " data_json, created_at) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT (message_id, enricher) DO UPDATE SET version = excluded.version,"
            " model_id = excluded.model_id, content_hash = excluded.content_hash,"
            " data_json = excluded.data_json, created_at = excluded.created_at",
            (message_id, name, version, model, content_hash, data, fmt_ts(self.clock.now())),
        )

    async def enrichments(self, message_id: MessageId) -> dict[str, dict[str, object]]:
        """Every stored enricher result for a message, by enricher name."""
        rows = await self.raw.db.fetchall(
            "SELECT enricher, data_json FROM enrichments WHERE message_id = ? ORDER BY enricher",
            (message_id,),
        )
        return {r["enricher"]: json.loads(r["data_json"]) for r in rows}

    async def enrichment(self, message_id: MessageId, enricher: str) -> dict[str, object] | None:
        """The stored result of one enricher for a message (for operations and the web UI)."""
        row = await self.raw.db.fetchone(
            "SELECT data_json FROM enrichments WHERE message_id = ? AND enricher = ?",
            (message_id, enricher),
        )
        return json.loads(row["data_json"]) if row else None

    # -- reprocess --------------------------------------------------------------------------
    async def reprocess(
        self, stage: Stage, account_id: AccountId | None = None, *, actor: Actor = SYSTEM
    ) -> Enqueued:
        """Queue the work to bring one stage up to date for old mail, at backfill priority.
        ``parse`` rebuilds the index, ``filter`` runs the Sieve scripts again, ``embed`` embeds
        whatever lacks a vector for the current model (swapping models without removing the old
        vectors first), ``enrich`` runs whichever enrichers have no current result."""
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage!r}; one of {', '.join(STAGES)}")
        return await self.queue.enqueue(
            REPROCESS,
            ReprocessPayload(stage=stage, account_id=account_id),
            priority=PRIORITY_BACKFILL,
            account_id=account_id,
            dedupe_key=f"{REPROCESS}:{stage}:{account_id or '*'}",
            actor=actor,
        )

    async def _reprocess_job(self, p: ReprocessPayload, _job: Job) -> StageJobResult:
        acct = AccountId(p.account_id) if p.account_id else None
        if p.stage == "parse":
            await enqueue_rebuild(self.queue, RebuildPayload(account_id=acct))
            return StageJobResult(note="index.rebuild queued")
        if p.stage == "filter":
            if self.filters is None:
                return StageJobResult(note="no filter engine")
            self.filters.reload()  # the scripts may have been edited since core started
            r = await self.filters.refilter(acct)
            return StageJobResult(done=r.filtered, skipped=r.failed)
        if p.stage == "embed":
            return await self._reembed(acct)
        return await self._reenrich(acct)

    async def _doc_ids(self, account_id: AccountId | None) -> list[str]:
        out: list[str] = []
        after = ""
        while ids := await self.index.doc_ids(after=after, limit=PAGE, account_id=account_id):
            out += ids
            after = ids[-1]
        return out

    async def _reembed(self, account_id: AccountId | None) -> StageJobResult:
        embedder = self.backends.embedder()
        if embedder is None or not self.vectors.available:
            raise PluginError("no embedder or vector store is available (will retry)")
        model = embedder.model_id
        old = (await self.vectors.stats()).model_id
        swapping = old is not None and old != model
        if swapping and not await self.vectors.begin_model(model, embedder.dimensions):
            raise Conflict(
                f"the vector store cannot hold two models; clear it to switch to {model}"
            )
        total = EmbedResult()
        for _ in range(3):  # a second pass catches mail that arrived during the first
            embedded = 0
            for mid in await self._doc_ids(account_id):
                r = await self.embed_message(MessageId(mid), swapping=swapping)
                embedded += r.embedded
                total.embedded += r.embedded
                total.skipped += r.skipped
            if embedded == 0:
                break
        if swapping and old is not None:
            await self.vectors.activate_model(model)
            await self.vectors.drop_model(old)
        await self.vectors.prune_orphans()
        return StageJobResult(
            done=total.embedded,
            skipped=total.skipped,
            note=f"swapped from {old}" if swapping else "",
        )

    async def _reenrich(self, account_id: AccountId | None) -> StageJobResult:
        ran = cached = 0
        for mid in await self._doc_ids(account_id):
            r = await self.enrich_message(MessageId(mid))
            ran += r.ran
            cached += r.cached
        return StageJobResult(done=ran, skipped=cached)

    # -- status -----------------------------------------------------------------------------
    async def depth(self) -> int:
        """Ingest jobs queued or running: what the backfill cap compares against."""
        row = await self.queue.db.fetchone(
            "SELECT COUNT(*) AS n FROM jobs WHERE kind LIKE 'ingest.%' AND kind != ?"
            " AND status IN ('queued', 'running')",
            (REPROCESS,),
        )
        return int(row["n"]) if row else 0

    async def backfill_paused(self) -> bool:
        return await self.depth() >= self.cfg.backfill_queue_cap

    async def pipeline_status(self) -> PipelineStatus:
        now = self.clock.now()
        db = self.queue.db
        window = timedelta(seconds=self.cfg.throughput_window_seconds)
        stages: dict[str, StageStatus] = {}
        for kind in STAGE_KINDS:
            counts = {
                r["status"]: r
                for r in await db.fetchall(
                    "SELECT status, COUNT(*) AS n, MIN(created_at) AS oldest,"
                    " SUM(attempts > 0) AS retried FROM jobs WHERE kind = ? GROUP BY status",
                    (kind,),
                )
            }
            queued = counts.get("queued")
            pending = int(queued["n"]) if queued else 0
            running = int(counts["running"]["n"]) if "running" in counts else 0
            done = await db.fetchone(
                "SELECT COUNT(*) AS n, MIN(updated_at) AS first FROM jobs"
                " WHERE kind = ? AND status = 'succeeded' AND updated_at >= ?",
                (kind, fmt_ts(now - window)),
            )
            rate = 0.0
            if done and done["n"]:
                span = max((now - parse_ts(done["first"])).total_seconds(), 1.0)
                rate = float(done["n"]) / min(span, window.total_seconds())
            stages[kind.removeprefix("ingest.")] = StageStatus(
                pending=pending,
                retrying=int(queued["retried"] or 0) if queued else 0,
                running=running,
                failed=int(counts["failed"]["n"]) if "failed" in counts else 0,
                oldest_pending_seconds=(
                    (now - parse_ts(queued["oldest"])).total_seconds() if queued else None
                ),
                per_second=rate,
                eta_seconds=((pending + running) / rate)
                if rate > 0 and pending + running
                else None,
            )
        parked = [
            ParkedJob(
                r["id"],
                r["kind"].removeprefix("ingest."),
                _message_of(r["payload_json"]),
                int(r["attempts"]),
                r["last_error"] or "",
            )
            for r in await db.fetchall(
                "SELECT id, kind, payload_json, attempts, last_error FROM jobs"
                " WHERE kind LIKE 'ingest.%' AND status = 'failed'"
                " ORDER BY updated_at DESC LIMIT 20"
            )
        ]
        istats = await self.index.stats()
        vstats = await self.vectors.stats() if self.vectors.available else None
        embedder = self.backends.embedder()
        held = vstats.model_id if vstats else None
        return PipelineStatus(
            stages=stages,
            docs=istats.docs,
            chunks=istats.chunks,
            index_stale=istats.stale,
            vector_model=held,
            vectors=vstats.count if vstats else 0,
            embedder_model=embedder.model_id if embedder else None,
            embeddings_stale=bool(embedder and held and held != embedder.model_id),
            backfill_paused=await self.backfill_paused(),
            quarantined_files=self.quarantine.stats().files if self.quarantine else 0,
            parked=parked,
        )


def _message_of(payload_json: str) -> str | None:
    try:
        value = json.loads(payload_json).get("message_id")
    except (ValueError, AttributeError):
        return None
    return value if isinstance(value, str) else None


__all__ = [
    "EMBED",
    "ENRICH",
    "PARSE",
    "REPROCESS",
    "STAGES",
    "EmbedResult",
    "EnrichResult",
    "ParkedJob",
    "Pipeline",
    "PipelineStatus",
    "Stage",
    "StageStatus",
]
