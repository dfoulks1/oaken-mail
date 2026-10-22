# SPDX-License-Identifier: Apache-2.0
"""The extension-hook conformance suites pass a well-formed plugin and fail a malformed one."""

import pytest
from pydantic import BaseModel

from oaken.plugins.base import BaseEmbedder
from oaken.plugins.contracts import (
    PluginJobHandler,
    PluginScheduledTask,
    PluginStage,
    PluginStageContext,
    PluginStageInput,
)
from oaken.testing.contracts import (
    JobHandlerContract,
    PipelineStageContract,
    ScheduledTaskContract,
    verify_job_handlers,
    verify_pipeline_stages,
    verify_scheduled_tasks,
)


class _Payload(BaseModel):
    text: str = ""


async def _job(ctx: object, payload: _Payload) -> None:
    return None


async def _stage(ctx: PluginStageContext, msg: PluginStageInput) -> dict[str, object]:
    return {"ok": True}


class Extending(BaseEmbedder):
    """An embedder that also offers all three extension hooks, well-formed."""

    model_id = "ext"
    dimensions = 2
    version = "1"

    async def embed(self, texts: object) -> list[list[float]]:
        return []

    def job_handlers(self) -> list[PluginJobHandler]:
        return [PluginJobHandler("recount", _Payload, _job)]

    def pipeline_stages(self) -> list[PluginStage]:
        return [PluginStage("tag", _stage)]

    def scheduled_tasks(self) -> list[PluginScheduledTask]:
        return [PluginScheduledTask("nightly", "plugin.ext.recount", _Payload(), every=900.0)]


class Broken(BaseEmbedder):
    """Malformed declarations: a job kind with a bad name, a task with both every and cron."""

    model_id = "broken"
    dimensions = 2
    version = "1"

    async def embed(self, texts: object) -> list[list[float]]:
        return []

    def job_handlers(self) -> list[PluginJobHandler]:
        return [PluginJobHandler("Bad Kind", _Payload, _job)]  # space + capital: invalid name

    def scheduled_tasks(self) -> list[PluginScheduledTask]:
        return [
            PluginScheduledTask("t", "k", _Payload(), every=900.0, cron="* * * * *")  # both set
        ]


# -- the one-call forms pass the good plugin and fail the broken one ---------------------------
async def test_verify_job_handlers_passes_and_fails() -> None:
    await verify_job_handlers(Extending)
    with pytest.raises(AssertionError):
        await verify_job_handlers(Broken)


async def test_verify_pipeline_stages_passes() -> None:
    await verify_pipeline_stages(Extending)


async def test_verify_scheduled_tasks_passes_and_fails() -> None:
    await verify_scheduled_tasks(Extending)
    with pytest.raises(AssertionError):
        await verify_scheduled_tasks(Broken)


# -- the pytest mixins against the good plugin -------------------------------------------------
class TestExtendingJobs(JobHandlerContract):
    @pytest.fixture
    def plugin(self) -> Extending:
        return Extending()


class TestExtendingStages(PipelineStageContract):
    @pytest.fixture
    def plugin(self) -> Extending:
        return Extending()


class TestExtendingSchedules(ScheduledTaskContract):
    @pytest.fixture
    def plugin(self) -> Extending:
        return Extending()
