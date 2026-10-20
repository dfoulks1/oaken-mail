# SPDX-License-Identifier: Apache-2.0
"""The plugin host: discover, validate, start, watch and stop plugins.

A plugin that fails anywhere (import, validation, ``start()``, a health check) is recorded as
unhealthy, logged, announced with ``PluginHealthChanged`` and left out of lookups. Core goes on
running. A plugin whose ``start()`` failed is tried again at each health tick.

Plugins run in-process with full access and are not sandboxed in v1.
"""

import asyncio
import contextlib
import inspect
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib.metadata import EntryPoint, entry_points
from typing import Any, cast

from oaken.core.clock import Clock
from oaken.core.errors import NotFound, PluginError
from oaken.core.events import EventBus, PluginHealthChanged
from oaken.core.logging import get_logger
from oaken.core.settings import Settings
from oaken.plugins.contracts import (
    ENTRY_POINT_GROUPS,
    PLUGIN_KINDS,
    Embedder,
    Enricher,
    Health,
    LLMBackend,
    PluginContext,
    PluginInfo,
    PluginKind,
    PluginOpContext,
    PluginOperation,
    Provider,
    VectorStore,
)
from oaken.plugins.validate import check_plugin, declared_operations

log = get_logger("oaken.plugins")

# Sending is a declared capability but unimplemented in v1: the host never routes it.
SEND_ROUTING_ENABLED = False

# An instance for one account is named ``<plugin>@<account id>``; account ids never hold "@".
ACCOUNT_SEP = "@"

Discover = Callable[[str], Iterable[EntryPoint]]


def discover_entry_points(group: str) -> Iterable[EntryPoint]:
    return entry_points(group=group)


@dataclass
class _Record:
    kind: PluginKind
    name: str
    info: PluginInfo | None = None
    instance: Any = None
    started: bool = False
    healthy: bool = False
    detail: str = ""
    announced: bool | None = None  # the last health state published
    ctx: PluginContext | None = None
    factory: Any = None  # what the entry point loaded: a class or function that makes instances
    account_id: str | None = None  # set on an instance started for one account
    overlay: dict[str, Any] | None = None  # that account's own (non-secret) settings


@dataclass(frozen=True)
class OfferedOperation:
    """An operation a started, healthy plugin offers (the registry namespaces it)."""

    plugin: str
    declaration: PluginOperation


@dataclass(frozen=True)
class PluginStatus:
    kind: PluginKind
    name: str
    version: str
    capabilities: frozenset[str]
    started: bool
    healthy: bool
    detail: str


class PluginHost:
    def __init__(
        self,
        settings: Settings,
        clock: Clock,
        bus: EventBus | None = None,
        *,
        discover: Discover = discover_entry_points,
        health_interval: float = 30.0,
        call_timeout: float = 30.0,
    ) -> None:
        self.settings = settings
        self.clock = clock
        self.bus = bus
        self._discover = discover
        self.health_interval = health_interval
        self.call_timeout = call_timeout
        self._records: dict[tuple[PluginKind, str], _Record] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    # -- lifecycle ---------------------------------------------------------------------------
    async def load(self) -> None:
        """Find every installed plugin and validate it. Nothing is started yet."""
        for kind in PLUGIN_KINDS:
            for ep in sorted(self._discover(ENTRY_POINT_GROUPS[kind]), key=lambda e: e.name):
                key = (kind, ep.name)
                if key in self._records:
                    log.error("duplicate plugin name ignored", kind=kind, name=ep.name)
                    continue
                self._records[key] = self._load_one(kind, ep)

    def _load_one(self, kind: PluginKind, ep: EntryPoint) -> _Record:
        rec = _Record(kind, ep.name)
        try:
            obj = ep.load()
            rec.factory = obj
            rec.instance = self._instantiate(obj)
            declared = getattr(rec.instance, "name", None)
            if isinstance(declared, str) and declared and declared != ep.name:
                log.warning(
                    "plugin name differs from its entry point; using the entry point",
                    kind=kind,
                    entry_point=ep.name,
                    declared=declared,
                )
            info, problems = check_plugin(kind, ep.name, rec.instance)
            if info is not None and not info.version and ep.dist is not None:
                info = PluginInfo(
                    info.name, ep.dist.version, kind, info.capabilities, info.api_version
                )
            if problems:
                raise PluginError("; ".join(problems))
            rec.info = info
        except Exception as exc:  # a broken plugin must never take core down
            rec.instance = None
            rec.healthy = False
            rec.detail = f"failed to load: {exc}"
            log.error("plugin failed to load", kind=kind, name=ep.name, error=str(exc))
        return rec

    @staticmethod
    def _instantiate(obj: Any) -> Any:
        if isinstance(obj, type) or inspect.isfunction(obj):
            return obj()
        return obj

    async def start(self) -> None:
        """Load (if not yet), start every valid plugin, and begin health checks."""
        if not self._records:
            await self.load()
        for rec in self._records.values():
            if rec.instance is not None:
                await self._start_one(rec)
            else:
                await self._announce(rec)
        self._stop.clear()
        self._task = asyncio.create_task(self._health_loop())

    async def start_account_instance(
        self, kind: PluginKind, name: str, account_id: str, config: dict[str, Any] | None = None
    ) -> str:
        """Start a separate instance of plugin ``name`` for one account, so a provider can serve
        several accounts (three Gmail accounts, a Fastmail one). Its settings are the plugin's own
        ``[plugins.<name>]`` table with ``config`` (the account's non-secret settings) on top.
        Returns the instance's name (``gmail@work``), which ``get`` accepts. Starting the same
        account twice returns the running instance. A failure to start is recorded, not raised,
        exactly as for any plugin: the instance is unhealthy and retried at the health tick."""
        instance_name = f"{name}{ACCOUNT_SEP}{account_id}"
        if (kind, instance_name) in self._records:
            return instance_name
        base = self._records.get((kind, name))
        if base is None or base.factory is None:
            known = ", ".join(sorted(n for k, n in self._records if k == kind)) or "none installed"
            raise NotFound(f"no {kind} named {name!r} (found: {known})")
        rec = _Record(kind, instance_name, factory=base.factory, account_id=account_id)
        rec.overlay = dict(config or {})
        if isinstance(base.factory, type) or inspect.isfunction(base.factory):
            try:
                rec.instance = self._instantiate(base.factory)
                rec.info = base.info
            except Exception as exc:
                rec.detail = f"failed to load: {exc}"
        else:
            rec.detail = f"{name} is a single object, so it cannot serve more than one account"
        self._records[(kind, instance_name)] = rec
        if rec.instance is not None and self._task is not None:
            await self._start_one(rec)
        else:
            await self._announce(rec)
        return instance_name

    async def _start_one(self, rec: _Record) -> None:
        base_name = rec.name.partition(ACCOUNT_SEP)[0]
        ctx = PluginContext(
            name=rec.name,
            kind=rec.kind,
            settings=self.settings,
            clock=self.clock,
            log=get_logger(f"oaken.plugin.{rec.kind}.{rec.name}"),
            config={**self.settings.plugins.get(base_name, {}), **(rec.overlay or {})},
            bus=self.bus,
            account_id=rec.account_id,
        )
        rec.ctx = ctx
        try:
            await self._call(rec.instance, "start", ctx)
            rec.started, rec.healthy, rec.detail = True, True, ""
        except Exception as exc:
            rec.started, rec.healthy = False, False
            rec.detail = f"start() failed: {exc}"
            log.error("plugin failed to start", kind=rec.kind, name=rec.name, error=str(exc))
        await self._announce(rec)

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task:
            await task
        for rec in self._records.values():
            if rec.started:
                try:
                    await self._call(rec.instance, "stop")
                except Exception as exc:
                    log.error("plugin failed to stop", kind=rec.kind, name=rec.name, error=str(exc))
                rec.started = False
            if rec.ctx is not None:
                rec.ctx.release()

    # -- health ------------------------------------------------------------------------------
    async def check_health(self) -> None:
        """One health pass: poll started plugins, retry the ones whose start failed."""
        for rec in list(self._records.values()):
            if rec.instance is None:
                continue
            if not rec.started:
                await self._start_one(rec)
                continue
            try:
                result = await self._call(rec.instance, "health")
                health = result if isinstance(result, Health) else Health(True)
                rec.healthy, rec.detail = health.ok, health.detail
            except Exception as exc:
                rec.healthy, rec.detail = False, f"health check failed: {exc}"
                log.error(
                    "plugin health check failed", kind=rec.kind, name=rec.name, error=str(exc)
                )
            await self._announce(rec)

    async def _health_loop(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), self.health_interval)
            if not self._stop.is_set():
                await self.check_health()

    async def _call(self, obj: Any, method: str, *args: Any) -> Any:
        """Call an optional lifecycle method (a plugin need not define it), with a timeout."""
        fn = getattr(obj, method, None)
        if fn is None:
            return None
        result = fn(*args)
        if inspect.isawaitable(result):
            return await asyncio.wait_for(result, self.call_timeout)
        return result

    async def _announce(self, rec: _Record) -> None:
        if rec.announced == rec.healthy:
            return
        rec.announced = rec.healthy
        if self.bus:
            await self.bus.publish(
                PluginHealthChanged(
                    ts=self.clock.now(),
                    plugin=f"{rec.kind}:{rec.name}",
                    healthy=rec.healthy,
                    detail=rec.detail,
                )
            )

    # -- operations a plugin offers ----------------------------------------------------------
    def _shared(self) -> list[_Record]:
        """The plugin's own instance (not one started for an account) of each healthy plugin."""
        return [
            r
            for r in sorted(self._records.values(), key=lambda r: (r.kind, r.name))
            if r.account_id is None and r.started and r.healthy and r.instance is not None
        ]

    def offered_operations(self) -> list[OfferedOperation]:
        """What the healthy plugins offer. An operation that cannot be read is skipped (it was
        already reported as a problem when the plugin loaded)."""
        out: list[OfferedOperation] = []
        for rec in self._shared():
            try:
                decls = declared_operations(rec.instance)
            except Exception as exc:
                log.error("plugin operations failed", name=rec.name, error=type(exc).__name__)
                continue
            out.extend(
                OfferedOperation(rec.name, d) for d in decls if isinstance(d, PluginOperation)
            )
        return out

    def operation_context(self, plugin: str, client: str, actor: str) -> PluginOpContext:
        """The context for one call of ``plugin``'s operation."""
        for rec in self._shared():
            if rec.name == plugin and rec.ctx is not None:
                return PluginOpContext(
                    plugin=plugin,
                    client=client,
                    actor=actor,
                    config=dict(rec.ctx.config),
                    clock=self.clock,
                    log=rec.ctx.log,
                )
        raise NotFound(f"plugin {plugin!r} is not running")

    def config_schema(self, kind: PluginKind, name: str) -> dict[str, Any] | None:
        """JSON Schema of a plugin's settings, if it declares ``config_model`` (pydantic)."""
        rec = self._records.get((kind, name))
        model = getattr(rec.instance, "config_model", None) if rec else None
        schema = getattr(model, "model_json_schema", None)
        return schema() if callable(schema) else None

    # -- lookup ------------------------------------------------------------------------------
    def statuses(self) -> list[PluginStatus]:
        return [
            PluginStatus(
                kind=r.kind,
                name=r.name,
                version=r.info.version if r.info else "",
                capabilities=r.info.capabilities if r.info else frozenset(),
                started=r.started,
                healthy=r.healthy,
                detail=r.detail,
            )
            for r in sorted(self._records.values(), key=lambda r: (r.kind, r.name))
        ]

    def active_name(self, kind: PluginKind) -> str | None:
        """The name config selects for ``kind`` (None for enrichers: every one is used)."""
        s = self.settings
        return {
            "provider": s.sync.provider,
            "embedder": s.ai.embedder,
            "llm_backend": s.ai.llm_backend,
            "vector_store": s.ai.vector_store,
        }.get(kind)

    def _usable(self, kind: PluginKind) -> dict[str, _Record]:
        return {
            r.name: r
            for r in self._records.values()
            if r.kind == kind and r.started and r.healthy and r.instance is not None
        }

    def _get(self, kind: PluginKind, name: str | None) -> Any:
        name = name or self.active_name(kind)
        if name is None:
            raise PluginError(f"give the name of the {kind} to use")
        rec = self._records.get((kind, name))
        if rec is None:
            known = ", ".join(sorted(n for k, n in self._records if k == kind)) or "none installed"
            raise NotFound(f"no {kind} named {name!r} (found: {known})")
        if not (rec.started and rec.healthy):
            raise PluginError(f"{kind} {name!r} is unhealthy: {rec.detail or 'not started'}")
        return rec.instance

    def provider(self, name: str | None = None) -> Provider:
        return cast(Provider, self._get("provider", name))

    def embedder(self, name: str | None = None) -> Embedder:
        return cast(Embedder, self._get("embedder", name))

    def llm_backend(self, name: str | None = None) -> LLMBackend:
        return cast(LLMBackend, self._get("llm_backend", name))

    def vector_store(self, name: str | None = None) -> VectorStore:
        return cast(VectorStore, self._get("vector_store", name))

    def enrichers(self) -> list[Enricher]:
        """Every healthy enricher, in name order."""
        usable = self._usable("enricher")
        return [cast(Enricher, usable[n].instance) for n in sorted(usable)]

    def named_enrichers(self) -> dict[str, Enricher]:
        """Every healthy enricher by its entry-point name (the name results are cached under)."""
        usable = self._usable("enricher")
        return {n: cast(Enricher, usable[n].instance) for n in sorted(usable)}

    def get(self, kind: PluginKind, name: str | None = None) -> Any:
        """The started, healthy plugin of ``kind`` called ``name`` (default: the configured one)."""
        return self._get(kind, name)

    def providers_with(self, capability: str) -> list[Provider]:
        """Healthy providers that implement ``capability``. Sending is never routed (v1)."""
        if capability == "send" and not SEND_ROUTING_ENABLED:
            return []
        usable = self._usable("provider")
        return [
            cast(Provider, usable[n].instance)
            for n in sorted(usable)
            if (info := usable[n].info) and capability in info.capabilities
        ]
