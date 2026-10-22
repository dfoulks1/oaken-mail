# SPDX-License-Identifier: Apache-2.0
"""A plugin schedules recurring work without touching core (step 34.5c).

An example plugin offers scheduled_tasks(); core seeds each into the scheduler as
``plugin.<name>.<task>`` only if absent, so a user who later edits or removes it keeps control.
A fake-clock tick fires the task and enqueues its job.
"""

from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel

from oaken.core.clock import FakeClock
from oaken.core.db import Database, migrate
from oaken.core.jobs import JobQueue
from oaken.core.scheduler import Scheduler
from oaken.core.settings import JobsSettings, Settings
from oaken.plugins.jobs import scheduled_task_name, seed_plugin_schedules
from oaken.plugins.tests.conftest import Installer
from oaken.plugins.tests.test_host import make_host

MIGRATIONS = Path(__file__).resolve().parents[2] / "core" / "migrations"

# An embedder that schedules its own "recount" job every 15 minutes.
SCHEDULER_PLUGIN = """
from pydantic import BaseModel
from oaken.plugins.base import BaseEmbedder
from oaken.plugins.contracts import PluginJobHandler, PluginScheduledTask

class Recount(BaseModel):
    text: str = ""

async def _recount(ctx, payload):
    return None

class Scheduled(BaseEmbedder):
    model_id = "sch"
    dimensions = 2
    version = "1"

    async def embed(self, texts):
        return [[0.0, 0.0] for _ in texts]

    def job_handlers(self):
        return [PluginJobHandler("recount", Recount, _recount)]

    def scheduled_tasks(self):
        return [PluginScheduledTask(
            "nightly", "plugin.sch-plugin.recount", Recount(text="go"), every=900.0
        )]
"""


@pytest.fixture
async def raw(tmp_path: Path) -> Database:
    db = await Database.open(tmp_path / "raw.db", read_pool_size=2)
    await migrate(db, MIGRATIONS)
    return db


async def test_a_plugin_task_is_seeded_fires_and_is_left_alone_once_edited(
    install_plugin: Installer, raw: Database
) -> None:
    install_plugin("plug_sch", SCHEDULER_PLUGIN, [("oaken.embedders", "sch-plugin", "Scheduled")])
    host, _, _ = make_host(Settings())
    await host.start()
    clock = FakeClock()
    queue = JobQueue(raw, clock, settings=JobsSettings(lease_seconds=60))
    scheduler = Scheduler(raw, queue, clock)
    name = scheduled_task_name("sch-plugin", "nightly")
    try:
        seeded = await seed_plugin_schedules(host, scheduler)
        assert seeded == [name]  # plugin.<name>.<task>
        assert await scheduler.next_run(name) is not None

        # It fires on the clock like any schedule: nothing yet, then one job at 15 min.
        assert await scheduler.tick() == 0
        clock.advance(minutes=15)
        assert await scheduler.tick() == 1
        row = await raw.fetchone(
            "SELECT COUNT(*) AS n FROM jobs WHERE kind = ?", ("plugin.sch-plugin.recount",)
        )
        assert row is not None and row["n"] == 1

        # The user edits it (here: a different interval). Re-seeding must NOT overwrite it.
        await scheduler.add(name, "plugin.sch-plugin.recount", _payload(), every=timedelta(hours=6))
        before = await _spec(raw, name)
        seeded_again = await seed_plugin_schedules(host, scheduler)
        assert seeded_again == []  # absent-only: the user's edit stands
        assert await _spec(raw, name) == before
    finally:
        await host.stop()
        await raw.close()


def _payload() -> BaseModel:
    class P(BaseModel):
        text: str = "edited"

    return P()


async def _spec(raw: Database, name: str) -> tuple[str, str]:
    row = await raw.fetchone("SELECT spec_kind, spec FROM schedules WHERE name = ?", (name,))
    assert row is not None
    return (row["spec_kind"], row["spec"])
