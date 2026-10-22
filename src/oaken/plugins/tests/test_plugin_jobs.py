# SPDX-License-Identifier: Apache-2.0
"""A plugin contributes a background job kind without touching core (step 34.5a).

An example plugin defines ``job_handlers()``; core registers each on the real ``JobRunner`` as
``plugin.<name>.<kind>`` and runs it through the durable queue. The handler is given a plugin-scoped
context (no database) and its validated payload, and its result is validated and stored.
"""

from pathlib import Path

import pytest
from pydantic import BaseModel

from oaken.core.clock import FakeClock
from oaken.core.db import Database, migrate
from oaken.core.ids import JobId
from oaken.core.jobs import JobQueue, JobRunner
from oaken.core.settings import JobsSettings, Settings
from oaken.plugins.jobs import namespaced_kind, register_plugin_jobs
from oaken.plugins.tests.conftest import Installer
from oaken.plugins.tests.test_host import make_host

MIGRATIONS = Path(__file__).resolve().parents[2] / "core" / "migrations"

# An embedder that also offers a "recount" job kind: it counts the words in the payload text and
# returns a result. It reads only its context and payload — no database, no queue.
WORDCOUNT = """
from pydantic import BaseModel
from oaken.plugins.base import BaseEmbedder
from oaken.plugins.contracts import PluginJobHandler

class Recount(BaseModel):
    text: str

class Count(BaseModel):
    words: int

async def _recount(ctx, payload):
    ctx.log.info("recount", kind=ctx.kind)
    return Count(words=len(payload.text.split()))

class WordCounter(BaseEmbedder):
    model_id = "wc"
    dimensions = 2
    version = "1"

    async def embed(self, texts):
        return [[0.0, 0.0] for _ in texts]

    def job_handlers(self):
        return [PluginJobHandler("recount", Recount, _recount, result_model=Count)]
"""


class _Recount(BaseModel):
    text: str


@pytest.fixture
async def raw(tmp_path: Path) -> Database:
    db = await Database.open(tmp_path / "raw.db", read_pool_size=2)
    await migrate(db, MIGRATIONS)
    return db


async def test_a_plugin_job_kind_is_registered_namespaced_and_runs(
    install_plugin: Installer, raw: Database
) -> None:
    install_plugin("plug_wc", WORDCOUNT, [("oaken.embedders", "word-count", "WordCounter")])
    host, _, _ = make_host(Settings())
    await host.start()
    clock = FakeClock()
    queue = JobQueue(raw, clock, settings=JobsSettings(workers=1, max_attempts=1))
    runner = JobRunner(queue, workers=1)
    try:
        registered = register_plugin_jobs(host, runner)
        full = namespaced_kind("word-count", "recount")
        assert registered == [full]  # namespaced plugin.<name>.<kind>

        res = await queue.enqueue(full, _Recount(text="one two three four"))
        ran = await runner.drain()
        assert ran == 1
        job = await queue.get(JobId(str(res.job_id)))
        assert job is not None and job.status == "succeeded"
        assert job.result_json is not None and '"words":4' in job.result_json.replace(" ", "")
    finally:
        await host.stop()
        await raw.close()


async def test_a_plugin_that_offers_no_job_handlers_registers_nothing(
    install_plugin: Installer, raw: Database
) -> None:
    # The stock embedder from test_host has no job_handlers(): nothing is registered, no error.
    from oaken.plugins.tests.test_host import EMBEDDER

    install_plugin("plug_nojobs", EMBEDDER, [("oaken.embedders", "plain", "Good")])
    host, _, _ = make_host(Settings())
    await host.start()
    clock = FakeClock()
    runner = JobRunner(JobQueue(raw, clock, settings=JobsSettings()), workers=1)
    try:
        assert register_plugin_jobs(host, runner) == []
    finally:
        await host.stop()
        await raw.close()
