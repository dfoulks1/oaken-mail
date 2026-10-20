# SPDX-License-Identifier: Apache-2.0
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from oaken.ai import LLMCache, LLMService, PromptBuilder, SafeOutput, Workspaces
from oaken.core.clock import FakeClock
from oaken.core.db import Database, migrate
from oaken.core.settings import Settings, WorkspaceSettings
from oaken.ingest.usage import UsageStore
from oaken.plugins.contracts import LLMBackend
from oaken.testing.fakes import FakeLLM

MIGRATIONS = Path(__file__).resolve().parents[2] / "core" / "migrations"


class Summary(SafeOutput):
    summary: str


SUMMARIZE: PromptBuilder[Summary] = PromptBuilder(
    "summarize", "1", "Summarize the message in one sentence.", Summary
)


class RemoteLLM(FakeLLM):
    locality = "remote"


@dataclass
class Rig:
    db: Database
    clock: FakeClock
    cache: LLMCache
    usage: UsageStore
    service: LLMService
    llm: FakeLLM
    remote: RemoteLLM
    settings: Settings


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    clock = FakeClock()
    settings = Settings(
        workspaces={
            "private": WorkspaceSettings(trust="secure", llm_backend="local"),
            "cloud": WorkspaceSettings(trust="insecure", llm_backend="remote"),
            "oops": WorkspaceSettings(trust="secure", llm_backend="remote"),
        }
    )
    async with await Database.open(tmp_path / "raw.db", read_pool_size=1) as db:
        await migrate(db, MIGRATIONS)
        llm, remote = FakeLLM(model_id="local-model"), RemoteLLM(model_id="cloud-model")
        backends: dict[str, LLMBackend] = {"local": llm, "remote": remote, "ollama": llm}
        cache, usage = LLMCache(db, clock), UsageStore(db, clock)
        service = LLMService(cache, usage, Workspaces(settings), backends.__getitem__, settings)
        yield Rig(db, clock, cache, usage, service, llm, remote, settings)
