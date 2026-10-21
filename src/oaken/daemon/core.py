# SPDX-License-Identifier: Apache-2.0
"""The composition root: every store and service opened and wired once, in a fixed order.

Order is settings → databases and migrations → plugin host → job runner and sync schedule → API.
Stopping is the reverse, so nothing is closed while something that uses it still runs.
"""

from pathlib import Path

import oaken
from oaken.ai import LLMCache, LLMService, Workspaces
from oaken.ai.access import WorkspaceLLM
from oaken.api import Api, ApiServer
from oaken.api.mcp_server import McpServer
from oaken.api.runtime import MCP_TOKEN_FILE, TOKEN_FILE, load_or_create_token
from oaken.core import paths
from oaken.core.agents import AgentRegistry
from oaken.core.attrs import AttrStore
from oaken.core.clock import Clock, SystemClock
from oaken.core.db import Database, migrate
from oaken.core.errors import OakenError
from oaken.core.events import EventBus
from oaken.core.jobs import JobQueue, JobRunner
from oaken.core.logging import get_logger
from oaken.core.operations import CoreClient, Services
from oaken.core.operations.builtin import core_registry
from oaken.core.operations.plugin_ops import plugin_operations
from oaken.core.rawstore import RawStore
from oaken.core.scheduler import Scheduler
from oaken.core.settings import Settings
from oaken.index import Indexer, IndexStore
from oaken.ingest import Backends, Pipeline, Quarantine, SyncRunner, UsageStore
from oaken.ingest.quarantine import quarantine_dir
from oaken.ingest.schedule import SyncService
from oaken.mail.filtering import FilterEngine, scripts_dir
from oaken.plugins.accounts import AccountProviders
from oaken.plugins.host import PluginHost
from oaken.readstate import AuditLog, ReadState
from oaken.readstate.service import providers_from_accounts

log = get_logger("oaken.daemon")
MIGRATIONS = Path(__file__).resolve().parents[1] / "core" / "migrations"


def _under_data(name: str) -> Path:
    p = Path(name).expanduser()
    return p if p.is_absolute() else paths.data_dir() / p


class Core:
    """Everything core is. ``open`` builds and migrates, ``start`` runs, ``stop`` unwinds."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        clock: Clock | None = None,
        token_path: Path | None = None,
        mcp_token_path: Path | None = None,
        discover: object | None = None,
    ) -> None:
        self.settings = settings or Settings.load()
        self.clock = clock or SystemClock()
        self.bus = EventBus()
        self.token_path = token_path or paths.config_dir() / TOKEN_FILE
        self.mcp_token_path = mcp_token_path or self.token_path.with_name(MCP_TOKEN_FILE)
        self._discover = discover
        self._open = False
        self._started: list[str] = []

    # -- building -------------------------------------------------------------------------
    async def open(self) -> None:
        s, clock, bus = self.settings, self.clock, self.bus
        st = s.storage
        self.raw_db = await Database.open(
            _under_data(st.raw_db), read_pool_size=st.read_pool_size,
            busy_timeout_ms=st.busy_timeout_ms,
        )  # fmt: skip
        try:
            await migrate(self.raw_db, MIGRATIONS)
            self.raw = RawStore(self.raw_db, clock, _under_data(st.lfs_dir), st, bus)
            self.attrs = AttrStore(self.raw_db, clock)
            self.index = await IndexStore.open(
                _under_data(st.index_db), clock, raw_store_id=await self.raw.store_id(),
                ai=s.ai, index=s.index, read_pool_size=st.read_pool_size,
            )  # fmt: skip
        except BaseException:
            await self.raw_db.close()
            raise
        self.indexer = Indexer(self.raw, self.index, self.attrs, s.mail, clock, bus=bus)
        self.indexer.follow_filtering()
        self.queue = JobQueue(self.raw_db, clock, bus, settings=s.jobs)
        self.runner = JobRunner(self.queue)
        self.scheduler = Scheduler(self.raw_db, self.queue, clock)
        self.usage = UsageStore(self.raw_db, clock)
        self.audit = AuditLog(self.raw_db, clock)
        kwargs = {} if self._discover is None else {"discover": self._discover}
        self.host = PluginHost(s, clock, bus, **kwargs)  # type: ignore[arg-type]
        self.llm = LLMService(
            LLMCache(self.raw_db, clock), self.usage, Workspaces(s),
            lambda name: self.host.llm_backend(name), s,
        )  # fmt: skip
        self.host.llm_access = WorkspaceLLM(self.llm, self.raw)
        engine = FilterEngine(
            self.raw, self.attrs, s.mail, clock, directory=scripts_dir(s.mail), bus=bus
        )
        quarantine = Quarantine(
            quarantine_dir(s.mail.attachments), max_bytes=s.mail.attachments.quarantine_max_bytes
        )
        self.pipeline = Pipeline(
            self.raw, self.index, self.attrs, self.indexer, self.queue, self.usage,
            Backends.from_host(self.host), s, clock, filters=engine, quarantine=quarantine,
            bus=bus,
        )  # fmt: skip
        self.pipeline.register(self.runner)
        self.accounts = AccountProviders(self.host, self.raw)
        self.readstate = ReadState(
            self.raw, self.queue, self.audit, clock, s, bus=bus,
            provider_for=providers_from_accounts(self.accounts),
        )  # fmt: skip
        self.readstate.register(self.runner)
        sync_runner = SyncRunner(self.raw, self.pipeline, clock, bus=bus)
        sync_runner.flag_sink = self.readstate.apply_server_flags
        self.sync = SyncService(
            self.raw, sync_runner, self.accounts, self.audit, clock, s.sync, bus=bus,
            after_providers=[self.readstate.resume_pending],
        )  # fmt: skip
        self.sync.register(self.runner)
        self.agents = AgentRegistry(self.raw_db, clock, s.swarm, bus)
        self.services = Services(
            raw=self.raw, index=self.index, attrs=self.attrs, queue=self.queue, audit=self.audit,
            settings=s, clock=clock, bus=bus, readstate=self.readstate, pipeline=self.pipeline,
            indexer=self.indexer, usage=self.usage, sync=self.sync, plugins=self.host,
            llm=self.llm, agents=self.agents, version=oaken.__version__,
        )  # fmt: skip
        registry = core_registry()
        registry.attach_plugins(lambda: plugin_operations(self.host))
        self.registry = registry
        self.client = CoreClient(registry, self.services)
        self.api = Api(self.client, token=load_or_create_token(self.token_path))
        self.mcp: McpServer | None = None
        if s.api.mcp:
            self.mcp = McpServer(self.client, token=load_or_create_token(self.mcp_token_path))
            self.api.mount(self.mcp.surface, self.mcp)
        self.server = ApiServer(self.api, host=s.api.host, port=s.api.port)
        self._open = True

    # -- running --------------------------------------------------------------------------
    async def start(self) -> int:
        """Plugins, then jobs and the sync schedule, then the API. Returns the API's port."""
        try:
            await self.host.start()
            self._started.append("host")
            await self.sync.start()
            self._started.append("sync")
            self.runner.start()
            self.scheduler.start()
            self._started.append("jobs")
            if self.mcp is not None:
                await self.mcp.start()
                self._started.append("mcp")
            port = await self.server.start()
            self._started.append("api")
        except BaseException:
            await self.stop()
            raise
        return port

    async def stop(self) -> None:
        """Stop accepting, let jobs finish or give their leases back, close the databases."""
        grace = self.settings.api.shutdown_grace_seconds
        steps = {
            "api": self.server.stop,
            "mcp": self._stop_mcp,
            "jobs": lambda: self._stop_jobs(grace),
            "sync": self.sync.stop,
            "host": self._stop_host,
        }
        for name in reversed(self._started):
            try:
                await steps[name]()
            except (OakenError, OSError, RuntimeError):
                log.exception("stopping failed", step=name)
        self._started.clear()
        if self._open:
            self._open = False
            await self.readstate.close()
            await self.index.close()
            await self.raw_db.close()

    async def _stop_mcp(self) -> None:
        if self.mcp is not None:
            await self.mcp.stop()

    async def _stop_host(self) -> None:
        await self.accounts.authorizations.close()
        await self.host.stop()

    async def _stop_jobs(self, grace: float) -> None:
        await self.scheduler.stop()
        await self.runner.stop(grace)
