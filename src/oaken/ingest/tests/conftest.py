# SPDX-License-Identifier: Apache-2.0
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

from oaken.core.attrs import AttrStore
from oaken.core.clock import FakeClock
from oaken.core.db import Database, migrate
from oaken.core.events import EventBus
from oaken.core.ids import AccountId, MessageId
from oaken.core.jobs import JobQueue, JobRunner
from oaken.core.rawstore import RawStore
from oaken.core.settings import (
    AISettings,
    AttachmentSettings,
    IngestSettings,
    JobsSettings,
    MailSettings,
    Settings,
)
from oaken.index import Indexer, IndexStore
from oaken.ingest import Backends, Pipeline, Quarantine, SyncRunner, UsageStore
from oaken.mail.filtering import FilterEngine
from oaken.plugins.contracts import Embedder, Enricher
from oaken.testing.fakes import (
    FakeEmbedder,
    FakeEnricher,
    FakeLLM,
    FakeProvider,
    InMemoryVectorStore,
)

MIGRATIONS = Path(__file__).resolve().parents[2] / "core" / "migrations"
ACCT = AccountId("main")


def mail(n: int, body: str | None = None, extra: str = "") -> bytes:
    text = body if body is not None else f"Message number {n} about topic{n} and quokkas."
    return (
        f"From: Alex <alex{n}@example.com>\r\nTo: Sam <sam@example.org>\r\n"
        f"Subject: Subject {n}\r\nDate: Mon, 01 Jan 2024 10:00:00 +0000\r\n"
        f"Message-ID: <m{n}@example.com>\r\n{extra}\r\n{text}\r\n"
    ).encode()


@dataclass
class Rig:
    tmp: Path
    clock: FakeClock
    bus: EventBus
    raw_db: Database
    raw: RawStore
    attrs: AttrStore
    index: IndexStore
    indexer: Indexer
    queue: JobQueue
    runner: JobRunner
    usage: UsageStore
    pipeline: Pipeline
    sync: SyncRunner
    provider: FakeProvider
    embedder: FakeEmbedder
    enricher: FakeEnricher
    llm: FakeLLM
    store: InMemoryVectorStore
    quarantine: Quarantine
    engine: FilterEngine
    sieve_dir: Path
    events: list[Any] = field(default_factory=list)

    async def run_sync(self) -> Any:
        return await self.sync.sync(ACCT, self.provider)

    async def drain(self, rounds: int = 1) -> int:
        """Run everything runnable, ``rounds`` times, moving the clock past any retry backoff
        between rounds."""
        total = 0
        for _ in range(rounds):
            total += await self.runner.drain()
            self.clock.advance(hours=2)
        return total

    async def mid(self, provider_id: str) -> MessageId:
        found = await self.raw.message_id_for(ACCT, provider_id)
        assert found is not None, f"{provider_id} was never stored"
        return found

    def names(self, kind: type) -> list[Any]:
        return [e for e in self.events if isinstance(e, kind)]


@dataclass
class Options:
    embedder: bool = True
    enrich: bool = True
    quarantine: bool = True
    ingest: IngestSettings = field(default_factory=IngestSettings)
    provider_batch: int = 100
    extra: dict[str, Any] = field(default_factory=dict)


async def build(tmp: Path, opts: Options, *, with_filters: bool = True) -> AsyncIterator[Rig]:
    clock = FakeClock()
    bus = EventBus()
    settings = Settings(
        ai=AISettings(embedder="fake-embed" if opts.embedder else ""),
        ingest=opts.ingest,
        mail=MailSettings(attachments=AttachmentSettings(quarantine=opts.quarantine)),
        jobs=JobsSettings(workers=1, max_attempts=3),
    )
    async with await Database.open(tmp / "raw.db", read_pool_size=2) as raw_db:
        await migrate(raw_db, MIGRATIONS)
        raw = RawStore(raw_db, clock, tmp / "lfs", bus=bus)
        await raw.create_account(ACCT, "fake", "Main")
        attrs = AttrStore(raw_db, clock)
        index = await IndexStore.open(tmp / "index.db", clock, raw_store_id=await raw.store_id())
        indexer = Indexer(raw, index, attrs, settings.mail, clock, bus=bus)
        indexer.follow_filtering()
        queue = JobQueue(raw_db, clock, bus, settings=settings.jobs)
        runner = JobRunner(queue, workers=1)
        usage = UsageStore(raw_db, clock)
        embedder, enricher, llm = FakeEmbedder(), FakeEnricher(), FakeLLM()
        store = InMemoryVectorStore()
        sieve_dir = tmp / "sieve"
        sieve_dir.mkdir()
        engine = FilterEngine(raw, attrs, settings.mail, clock, directory=sieve_dir, bus=bus)
        quarantine = Quarantine(tmp / "quarantine")
        backends = Backends(
            embedder=lambda: embedder if opts.embedder else None,
            llm=lambda: llm,
            enrichers=lambda: {"words": cast(Enricher, enricher)} if opts.enrich else {},
            vector_store=lambda: store,
        )
        pipeline = Pipeline(
            raw,
            index,
            attrs,
            indexer,
            queue,
            usage,
            backends,
            settings,
            clock,
            filters=engine if with_filters else None,
            quarantine=quarantine,
            bus=bus,
        )
        pipeline.register(runner)
        sync = SyncRunner(raw, pipeline, clock, bus=bus)
        rig = Rig(
            tmp, clock, bus, raw_db, raw, attrs, index, indexer, queue, runner, usage, pipeline,
            sync, FakeProvider(batch_size=opts.provider_batch), embedder, enricher, llm, store,
            quarantine, engine, sieve_dir,
        )  # fmt: skip

        async def record(event: Any) -> None:
            rig.events.append(event)

        from oaken.core.events import Event

        bus.subscribe(Event, record)
        try:
            yield rig
        finally:
            await index.close()


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    async for r in build(tmp_path, Options()):
        yield r


@pytest.fixture
def make_rig(tmp_path: Path) -> Any:
    """``make_rig(Options(...))`` for a test that needs other settings; use as an async context."""
    import contextlib
    import itertools

    counter = itertools.count()

    @contextlib.asynccontextmanager
    async def make(opts: Options | None = None) -> AsyncIterator[Rig]:
        where = tmp_path / f"rig{next(counter)}"
        where.mkdir()
        async for r in build(where, opts or Options()):
            yield r

    return make


__all__ = ["ACCT", "Embedder", "Enricher", "Options", "Rig", "mail"]
