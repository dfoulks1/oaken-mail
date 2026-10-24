# SPDX-License-Identifier: Apache-2.0
"""What an operation's handler may reach: the services the daemon (or a test rig) has built.

The core ones are required. The rest are optional because a process may run without them (no
model, no plugins, a test rig with a few parts); a handler that needs one asks for it with ``need``
and the caller gets ``unavailable`` rather than a crash.
"""

from dataclasses import dataclass
from typing import TypeVar

from oaken.ai.generate import LLMService
from oaken.core.agents import AgentRegistry
from oaken.core.attrs import AttrStore
from oaken.core.clock import Clock
from oaken.core.errors import Unavailable
from oaken.core.eventlog import EventLog
from oaken.core.events import EventBus
from oaken.core.jobs import JobQueue
from oaken.core.mailbox import MailboxStore
from oaken.core.rawstore import RawStore
from oaken.core.search import Retrieval
from oaken.core.settings import Settings
from oaken.index import Indexer, IndexStore
from oaken.ingest import Pipeline, UsageStore
from oaken.ingest.pipeline_control import PipelineControl
from oaken.ingest.schedule import SyncService
from oaken.plugins.host import PluginHost
from oaken.readstate import AuditLog, ReadState

T = TypeVar("T")


def need(value: T | None, what: str) -> T:
    if value is None:
        raise Unavailable(f"{what} is not running")
    return value


@dataclass
class Services:
    raw: RawStore
    index: IndexStore
    attrs: AttrStore
    queue: JobQueue
    audit: AuditLog
    settings: Settings
    clock: Clock
    bus: EventBus | None = None
    readstate: ReadState | None = None
    pipeline: Pipeline | None = None
    indexer: Indexer | None = None
    usage: UsageStore | None = None
    sync: SyncService | None = None
    plugins: PluginHost | None = None
    llm: LLMService | None = None
    pipeline_control: PipelineControl | None = None
    retrieval: Retrieval | None = None  # built from the pipeline when not given
    agents: AgentRegistry | None = None
    mailbox: MailboxStore | None = None  # built on first use from raw, attrs and the indexer
    eventlog: EventLog | None = None  # None when ``[log] events_file`` is empty
    version: str = ""

    def need_readstate(self) -> ReadState:
        return need(self.readstate, "read state")

    def need_pipeline(self) -> Pipeline:
        return need(self.pipeline, "the ingest pipeline")

    def need_indexer(self) -> Indexer:
        return need(self.indexer, "the indexer")

    def need_usage(self) -> UsageStore:
        return need(self.usage, "usage accounting")

    def need_sync(self) -> SyncService:
        return need(self.sync, "the sync scheduler")

    def need_pipeline_control(self) -> PipelineControl:
        return need(self.pipeline_control, "the pipeline control")

    def need_mailbox(self) -> MailboxStore:
        if self.mailbox is None:
            refresh = self.indexer.refresh_organization if self.indexer is not None else None
            self.mailbox = MailboxStore(self.raw.db, self.attrs, self.clock, refresh)
        return self.mailbox

    def need_plugins(self) -> PluginHost:
        return need(self.plugins, "the plugin host")

    def need_eventlog(self) -> EventLog:
        if self.eventlog is None:
            raise Unavailable("the event log is off: set [log] events_file to turn it on")
        return self.eventlog

    def need_agents(self) -> AgentRegistry:
        return need(self.agents, "the agent registry")

    def need_llm(self) -> LLMService:
        return need(self.llm, "the language model service")

    def need_retrieval(self) -> Retrieval:
        """Search and ask. Built on demand from the pipeline's index, vectors and embedder, and
        the language model service (workspace-checked per account) when there is one."""
        if self.retrieval is None:
            from oaken.ai.access import WorkspaceLLM

            llm = WorkspaceLLM(self.llm, self.raw) if self.llm is not None else None
            self.retrieval = Retrieval.from_pipeline(self.need_pipeline(), llm)
        return self.retrieval
