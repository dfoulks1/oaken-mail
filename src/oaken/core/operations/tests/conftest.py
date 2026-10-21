# SPDX-License-Identifier: Apache-2.0
"""A rig for the operation registry: the real stores and pipeline (fake provider, embedder and
LLM), read state, the sync scheduler, a plugin host with two test plugins, and a ``CoreClient``."""

import random
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from dataclasses import dataclass
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from oaken.ai import LLMCache, LLMService, Workspaces
from oaken.core.agents import AgentRegistry
from oaken.core.clock import FakeClock
from oaken.core.ids import MessageId
from oaken.core.operations import (
    CallerContext,
    CallResult,
    CoreClient,
    OperationRegistry,
    Services,
)
from oaken.core.operations.builtin import core_registry
from oaken.core.operations.plugin_ops import plugin_operations
from oaken.core.operations.registry import AnyOperation
from oaken.core.settings import OperationsSettings, Settings, SwarmSettings, SyncSettings
from oaken.ingest.pipeline_control import PipelineControl
from oaken.ingest.schedule import SyncService
from oaken.plugins.accounts import AccountProviders
from oaken.plugins.base import BaseEnricher, BaseProvider
from oaken.plugins.contracts import (
    EnrichInput,
    EnrichOutput,
    PluginChange,
    PluginOpContext,
    PluginOperation,
    PluginPreview,
)
from oaken.plugins.host import PluginHost
from oaken.readstate.tests.conftest import RS, make
from oaken.testing.fakes import FakeProvider

CLI, UI = CallerContext.cli(), CallerContext.ui()


def agent(label: str = "claude") -> CallerContext:
    return CallerContext.mcp(label)


class Name(BaseModel):
    name: str = "world"


class Greeting(BaseModel):
    text: str


class Renamed(BaseModel):
    ok: bool


class Greeter(BaseEnricher):
    """A plugin that offers operations: one read, one mutation (previewed), one that breaks."""

    version = "1"
    calls: ClassVar[list[str]] = []

    async def enrich(self, msg: EnrichInput) -> EnrichOutput:
        return EnrichOutput({})

    def operations(self) -> list[PluginOperation]:
        async def hello(ctx: PluginOpContext, inp: Name) -> Greeting:
            return Greeting(text=f"hello {inp.name} from {ctx.plugin} via {ctx.client}")

        async def rename(ctx: PluginOpContext, inp: Name) -> Renamed:
            Greeter.calls.append(inp.name)
            return Renamed(ok=True)

        async def preview(ctx: PluginOpContext, inp: Name) -> PluginPreview:
            return PluginPreview(
                "Rename the greeter", (PluginChange("greeter", "name", "old", inp.name),), 1
            )

        async def boom(ctx: PluginOpContext, inp: Name) -> Greeting:
            raise RuntimeError("secret mail text in an error")

        return [
            PluginOperation("hello", "Say hello.", "read", Name, Greeting, hello),
            PluginOperation("rename", "Rename it.", "mutate", Name, Renamed, rename, preview),
            PluginOperation("boom", "Always fails.", "read", Name, Greeting, boom),
            PluginOperation(
                "secret", "Never over MCP.", "read", Name, Greeting, hello, mcp="never"
            ),
        ]


class Mailbox(FakeProvider):
    supports_folders = False


def discover(group: str) -> Iterable[EntryPoint]:
    here = __name__
    if group == "oaken.providers":
        return [EntryPoint("fake", f"{here}:Mailbox", group)]
    if group == "oaken.enrichers":
        return [EntryPoint("greeter", f"{here}:Greeter", group)]
    return []


def _backend_for(llm: Any) -> Callable[[str], Any]:
    return lambda _name: llm


def _plugin_source(host: PluginHost) -> Callable[[], Sequence[AnyOperation]]:
    return lambda: plugin_operations(host)


@dataclass
class Ops:
    rs: RS
    client: CoreClient
    svc: Services
    host: PluginHost
    registry: OperationRegistry

    @property
    def rig(self) -> Any:
        return self.rs.rig

    async def call(
        self,
        name: str,
        payload: BaseModel | dict[str, Any] | None = None,
        caller: CallerContext = CLI,
        *,
        confirm: bool = True,
        token: str | None = None,
    ) -> CallResult:
        """Call as a person at the CLI would: confirmed, unless told otherwise."""
        return await self.client.call(
            name, payload, caller, confirm=confirm, confirmation_token=token
        )

    async def out(self, name: str, payload: Any = None, caller: CallerContext = CLI) -> Any:
        result = await self.call(name, payload, caller)
        assert result.output is not None, f"{name} returned a proposal, not a result"
        return result.output

    async def add(self, count: int = 1, *, start: int = 0) -> list[MessageId]:
        return await self.rs.add(count, start=start)

    async def read_count(self) -> int:
        row = await self.rig.raw_db.fetchone(
            "SELECT COUNT(*) AS n FROM message_state WHERE is_read = 1"
        )
        return int(row["n"])

    async def audit_ops(self) -> list[str]:
        return [e.op for e in await self.svc.audit.recent(500)]


async def build_ops(
    tmp: Path, *, operations: OperationsSettings | None = None, with_plugins: bool = True
) -> AsyncIterator[Ops]:
    async for rs in make(tmp):
        rig = rs.rig
        settings = Settings(
            operations=operations or OperationsSettings(),
            sync=SyncSettings(backoff_jitter=0.0),
            swarm=SwarmSettings(enabled=True, allowed_kinds=["enrich"]),
        )
        host = PluginHost(settings, rig.clock, rig.bus, discover=discover, health_interval=3600)
        if with_plugins:
            await host.start()
        accounts = AccountProviders(host, rig.raw)
        if with_plugins:
            await accounts.start()
        sync = SyncService(
            rig.raw, rig.sync, accounts, rs.audit, rig.clock, settings.sync, bus=rig.bus,
            rng=random.Random(0),
        )  # fmt: skip
        sync.register(rig.runner)
        llm = LLMService(
            LLMCache(rig.raw_db, rig.clock),
            rig.usage,
            Workspaces(settings),
            _backend_for(rig.llm),
            settings,
        )
        svc = Services(
            raw=rig.raw, index=rig.index, attrs=rig.attrs, queue=rig.queue, audit=rs.audit,
            settings=settings, clock=rig.clock, bus=rig.bus, readstate=rs.rs,
            pipeline=rig.pipeline, indexer=rig.indexer, usage=rig.usage, sync=sync,
            plugins=host, llm=llm, version="test",
            pipeline_control=PipelineControl(rig.raw_db, rig.clock, rs.audit),
            agents=AgentRegistry(rig.raw_db, rig.clock, settings.swarm, rig.bus),
        )  # fmt: skip
        registry = core_registry()
        registry.attach_plugins(_plugin_source(host))
        Greeter.calls.clear()
        try:
            yield Ops(rs, CoreClient(registry, svc), svc, host, registry)
        finally:
            await sync.stop()
            await host.stop()


@pytest.fixture
async def ops(tmp_path: Path) -> AsyncIterator[Ops]:
    async for o in build_ops(tmp_path):
        yield o


@pytest.fixture
def make_ops(tmp_path: Path) -> Any:
    import contextlib
    import itertools

    counter = itertools.count()

    @contextlib.asynccontextmanager
    async def build_one(**kwargs: Any) -> AsyncIterator[Ops]:
        where = tmp_path / f"ops{next(counter)}"
        where.mkdir()
        async for o in build_ops(where, **kwargs):
            yield o

    return build_one


__all__ = ["CLI", "UI", "BaseProvider", "FakeClock", "Ops", "agent"]
