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

from pydantic import ValidationError

from oaken.core.clock import Clock
from oaken.core.errors import NotFound, PluginError
from oaken.core.events import EventBus, PluginHealthChanged
from oaken.core.logging import get_logger
from oaken.core.settings import Settings
from oaken.plugins.base import ALLOWED_ATTR
from oaken.plugins.contracts import (
    CAPABILITIES,
    ENTRY_POINT_GROUPS,
    PLUGIN_KINDS,
    Embedder,
    Enricher,
    Health,
    LLMAccess,
    LLMBackend,
    PluginContext,
    PluginInfo,
    PluginJobContext,
    PluginJobHandler,
    PluginKind,
    PluginOpContext,
    PluginOperation,
    PluginScheduledTask,
    PluginStage,
    PluginStageContext,
    Provider,
    VectorStore,
)
from oaken.plugins.validate import (
    check_plugin,
    check_started,
    declared_job_handlers,
    declared_operations,
    declared_pipeline_stages,
    declared_scheduled_tasks,
)

log = get_logger("oaken.plugins")

# Sending is a declared capability but unimplemented in v1: the host never routes it.
SEND_ROUTING_ENABLED = False

# An instance for one account is named ``<plugin>@<account id>``; account ids never hold "@".
ACCOUNT_SEP = "@"

Discover = Callable[[str], Iterable[EntryPoint]]


def discover_entry_points(group: str) -> Iterable[EntryPoint]:
    return entry_points(group=group)


class _HostLLM:
    """``ctx.llm``: forwards to whichever service core has attached to the host at call time, so a
    plugin started before the service exists (or after it is replaced) still works."""

    def __init__(self, host: "PluginHost") -> None:
        self._host = host

    async def generate_cached(self, builder: Any, *parts: Any, **kwargs: Any) -> Any:
        access = self._host.llm_access
        if access is None:
            raise PluginError("no language model service is attached to the plugin host")
        return await access.generate_cached(builder, *parts, **kwargs)


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
    disabled: bool = False  # turned off at runtime (``disable``); ``enabled = false`` also counts
    forced_on: bool = False  # ``enable`` was asked for: beats the plugin's own off-by-default
    allowed: frozenset[str] | None = None  # the user's ``capabilities`` allow-list, if any


@dataclass(frozen=True)
class OfferedOperation:
    """An operation a started, healthy plugin offers (the registry namespaces it)."""

    plugin: str
    declaration: PluginOperation


@dataclass(frozen=True)
class OfferedJobHandler:
    """A job kind a started, healthy plugin offers (core namespaces it ``plugin.<name>.<kind>``)."""

    plugin: str
    declaration: PluginJobHandler


@dataclass(frozen=True)
class OfferedPipelineStage:
    """A pipeline stage a started, healthy plugin offers (run after the enrichers)."""

    plugin: str
    declaration: PluginStage


@dataclass(frozen=True)
class OfferedScheduledTask:
    """A recurring task a started, healthy plugin offers (seeded if absent as plugin.<name>.<t>)."""

    plugin: str
    declaration: PluginScheduledTask


@dataclass(frozen=True)
class PluginStatus:
    kind: PluginKind
    name: str
    version: str
    capabilities: frozenset[str]  # what the plugin may do now: its own, narrowed by the user
    started: bool
    healthy: bool
    detail: str
    enabled: bool = True  # on, by runtime switch and by its table (or its own default)
    enabled_default: bool = True  # what the plugin does with no ``enabled`` in its table
    allowed: frozenset[str] | None = None  # the user's allow-list, None when there is none
    declared: frozenset[str] = frozenset()  # what the plugin implements, before any allow-list


@dataclass(frozen=True)
class Probe:
    """What starting a throwaway instance of a plugin on a candidate config showed."""

    kind: PluginKind
    ok: bool
    detail: str = ""


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
        # Set by core once it has an LLM service; plugins reach it through ``ctx.llm``.
        self.llm_access: LLMAccess | None = None

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
            info, problems = check_plugin(kind, ep.name, rec.instance, defer_started=True)
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
        config = {**self.settings.plugins.get(base_name, {}), **(rec.overlay or {})}
        # ``enabled`` and ``capabilities`` are the host's, not the plugin's settings: a plugin never
        # sees them.
        if not self._is_enabled(rec, config) or rec.disabled:
            if rec.detail != "disabled":  # the health tick retries; say it once
                log.info("plugin disabled", kind=rec.kind, name=rec.name)
            rec.started, rec.healthy, rec.detail = False, False, "disabled"
            await self._announce(rec)
            return
        try:
            rec.allowed = self._allowlist(rec.kind, rec.info, config.pop("capabilities", None))
            self._apply_allowlist(rec)
        except PluginError as exc:
            rec.started, rec.healthy, rec.detail = False, False, f"start() failed: {exc}"
            log.error("plugin failed to start", kind=rec.kind, name=rec.name, error=str(exc))
            await self._announce(rec)
            return
        config.pop("enabled", None)
        ctx = PluginContext(
            name=rec.name,
            kind=rec.kind,
            settings=self.settings,
            clock=self.clock,
            log=get_logger(f"oaken.plugin.{rec.kind}.{rec.name}"),
            config=config,
            bus=self.bus,
            account_id=rec.account_id,
            llm=_HostLLM(self),
        )
        rec.ctx = ctx
        try:
            self._validate_config(rec.instance, ctx.config)
            await self._call(rec.instance, "start", ctx)
            problems = check_started(rec.kind, rec.instance)
            if problems:
                await self._call(rec.instance, "stop")
                raise PluginError("; ".join(problems))
            rec.started, rec.healthy, rec.detail = True, True, ""
        except Exception as exc:
            rec.started, rec.healthy = False, False
            if rec.account_id is None and not ctx.config:
                # Installed but never set up (no [plugins.<name>] table): not an error.
                rec.detail = f"not configured ({exc})"
                log.info("plugin not configured", kind=rec.kind, name=rec.name, detail=str(exc))
            else:
                rec.detail = f"start() failed: {exc}"
                log.error("plugin failed to start", kind=rec.kind, name=rec.name, error=str(exc))
        await self._announce(rec)

    @staticmethod
    def _enabled_default(instance: Any) -> bool:
        return getattr(instance, "enabled_default", True) is not False

    def _is_enabled(self, rec: _Record, config: dict[str, Any]) -> bool:
        """The table's ``enabled`` wins. With none, a plugin runs unless it ships switched off
        (``enabled_default``) and nobody has asked for it with ``enable``. ``config`` is read, not
        changed."""
        explicit = config.get("enabled")
        if explicit is not None:
            return explicit is not False
        return rec.forced_on or self._enabled_default(rec.instance)

    @staticmethod
    def _allowlist(kind: PluginKind, info: PluginInfo | None, value: Any) -> frozenset[str] | None:
        """Read a ``capabilities = [...]`` setting. It only narrows: a name the plugin does not
        implement does nothing, one that does not exist at all is a typo and an error, and an
        allow-list that leaves only ``auth`` leaves nothing to sign in for."""
        if value is None:
            return None
        known = CAPABILITIES[kind]
        if not known:
            raise PluginError(f"a {kind} has no capabilities to narrow; remove `capabilities`")
        if not isinstance(value, list) or not all(isinstance(c, str) for c in value):
            raise PluginError("`capabilities` must be a list of names")
        unknown = sorted(set(value) - set(known))
        if unknown:
            raise PluginError(
                f"unknown capability {unknown[0]!r} (a {kind} has: {', '.join(sorted(known))})"
            )
        allowed = frozenset(value)
        if not (allowed & (info.capabilities if info else frozenset())) - {"auth"}:
            raise PluginError(
                "`capabilities` leaves the plugin nothing to do (signing in alone is not enough)"
            )
        return allowed

    @staticmethod
    def _apply_allowlist_to(instance: Any, allowed: frozenset[str] | None) -> None:
        try:
            if allowed is None:
                vars(instance).pop(ALLOWED_ATTR, None)
            else:
                setattr(instance, ALLOWED_ATTR, allowed)
        except (AttributeError, TypeError):
            raise PluginError("this plugin cannot be narrowed with `capabilities`") from None

    def _apply_allowlist(self, rec: _Record) -> None:
        self._apply_allowlist_to(rec.instance, rec.allowed)

    def _caps(self, rec: _Record) -> frozenset[str]:
        caps = rec.info.capabilities if rec.info else frozenset()
        return caps if rec.allowed is None else caps & rec.allowed

    async def probe(
        self, name: str, config: dict[str, Any], kind: PluginKind | None = None
    ) -> list[Probe]:
        """Start a throwaway instance of plugin ``name`` on a candidate ``config`` and ask its
        health, so a form can say "this works" before anything is saved. The running plugin and
        its accounts are not touched, nothing is announced, and the instance is stopped again.
        ``kind`` narrows it for a name shared by two kinds (``ollama`` embeds and generates)."""
        recs = [
            r
            for (k, n), r in sorted(self._records.items())
            if n == name and (kind is None or k == kind)
        ]
        if not recs:
            raise NotFound(f"no plugin named {name!r}")
        return [await self._probe_one(r, config) for r in recs]

    async def _probe_one(self, rec: _Record, candidate: dict[str, Any]) -> Probe:
        if rec.instance is None or rec.factory is None:
            return Probe(rec.kind, False, rec.detail or "it did not load")
        if not (isinstance(rec.factory, type) or inspect.isfunction(rec.factory)):
            return Probe(
                rec.kind, False, "a single object: it cannot be tried apart from the live one"
            )
        config = dict(candidate)
        config.pop("enabled", None)
        try:
            allowed = self._allowlist(rec.kind, rec.info, config.pop("capabilities", None))
        except PluginError as exc:
            return Probe(rec.kind, False, str(exc))
        instance = self._instantiate(rec.factory)
        ctx = PluginContext(
            name=rec.name,
            kind=rec.kind,
            settings=self.settings,
            clock=self.clock,
            log=get_logger(f"oaken.plugin.{rec.kind}.{rec.name}"),
            config=config,
            bus=None,
            account_id=None,
            llm=_HostLLM(self),
        )
        started = False
        try:
            self._apply_allowlist_to(instance, allowed)
            self._validate_config(instance, config)
            await self._call(instance, "start", ctx)
            started = True
            problems = check_started(rec.kind, instance)
            if problems:
                raise PluginError("; ".join(problems))
            result = await self._call(instance, "health")
            health = result if isinstance(result, Health) else Health(True)
            return Probe(rec.kind, health.ok, health.detail)
        except Exception as exc:  # noqa: BLE001 - whatever it was is the answer
            return Probe(rec.kind, False, str(exc))
        finally:
            if started:
                try:
                    await self._call(instance, "stop")
                except Exception as exc:  # noqa: BLE001
                    log.error("probe instance failed to stop", name=rec.name, error=str(exc))
            ctx.release()

    def check_config(self, name: str, config: dict[str, Any]) -> list[str]:
        """Problems with ``config`` as the ``[plugins.<name>]`` table, found without starting
        anything: its ``config_model`` and its ``capabilities``. Empty when it is sound."""
        recs = [r for (_k, n), r in sorted(self._records.items()) if n == name]
        if not recs:
            raise NotFound(f"no plugin named {name!r}")
        problems: list[str] = []
        for rec in recs:
            table = dict(config)
            table.pop("enabled", None)
            try:
                self._allowlist(rec.kind, rec.info, table.pop("capabilities", None))
                if rec.instance is not None:
                    self._validate_config(rec.instance, table)
            except PluginError as exc:
                problems.append(f"{rec.kind} {name}: {exc}")
        return problems

    def kinds_of(self, name: str) -> list[PluginKind]:
        return [k for (k, n) in sorted(self._records) if n == name]

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task:
            await task
        for rec in self._records.values():
            await self._stop_one(rec)

    async def _stop_one(self, rec: _Record) -> None:
        if rec.started:
            try:
                await self._call(rec.instance, "stop")
            except Exception as exc:
                log.error("plugin failed to stop", kind=rec.kind, name=rec.name, error=str(exc))
            rec.started = False
        if rec.ctx is not None:
            rec.ctx.release()
            rec.ctx = None

    # -- control of one plugin, while core keeps running -------------------------------------
    def _family(self, name: str) -> list[_Record]:
        """The plugin called ``name`` (any kind) and every instance started for an account of it,
        or just the one instance if ``name`` is one (``gmail@work``)."""
        if ACCOUNT_SEP in name:
            found = [r for r in self._records.values() if r.name == name]
        else:
            prefix = name + ACCOUNT_SEP
            found = [
                r for r in self._records.values() if r.name == name or r.name.startswith(prefix)
            ]
        if not found:
            known = ", ".join(sorted({r.name for r in self._records.values()})) or "none installed"
            raise NotFound(f"no plugin named {name!r} (found: {known})")
        return found

    async def disable(self, name: str) -> list[str]:
        """Stop a plugin and keep it stopped until ``enable`` (or a restart: ``enabled = false`` in
        ``[plugins.<name>]`` is the lasting form). Returns the names affected."""
        recs = self._family(name)
        for rec in recs:
            rec.disabled = True
            await self._stop_one(rec)
            rec.healthy, rec.detail = False, "disabled"
            await self._announce(rec)
        return [r.name for r in recs]

    async def enable(self, name: str) -> list[str]:
        """Start a plugin that was disabled at runtime. It still stays off if its table says
        ``enabled = false``: that is a setting, and ``reload`` picks up a change to it."""
        recs = self._family(name)
        for rec in recs:
            rec.disabled = False
            rec.forced_on = True
            if rec.instance is not None and not rec.started:
                await self._start_one(rec)
        return [r.name for r in recs]

    async def reload(self, name: str, settings: Settings | None = None) -> list[str]:
        """Stop a plugin, take a fresh instance of it (re-discovering its entry point, so a plugin
        installed or upgraded since boot is found) and start it on ``settings``' ``[plugins]``.
        Only that plugin is touched; core keeps running. Job handlers and scheduled tasks a plugin
        offered are registered when core starts, so a change to those needs a core reload."""
        recs = self._family(name)
        if settings is not None:
            self.settings = settings
        found = {
            (kind, ep.name): ep
            for kind in PLUGIN_KINDS
            for ep in self._discover(ENTRY_POINT_GROUPS[kind])
        }
        for rec in recs:
            await self._stop_one(rec)
            base = rec.name.partition(ACCOUNT_SEP)[0]
            ep = found.get((rec.kind, base))
            if rec.account_id is None:
                if ep is None:
                    rec.instance, rec.healthy, rec.detail = None, False, "no longer installed"
                    await self._announce(rec)
                    continue
                fresh = self._load_one(rec.kind, ep)
                fresh.disabled, fresh.forced_on = rec.disabled, rec.forced_on
                self._records[(rec.kind, rec.name)] = fresh
                rec = fresh
            else:
                try:
                    rec.instance = self._instantiate(rec.factory)
                except Exception as exc:
                    rec.instance, rec.detail = None, f"failed to load: {exc}"
                    continue
            if rec.instance is not None:
                await self._start_one(rec)
            else:
                await self._announce(rec)
        return [r.name for r in recs]

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

    def catalog(self) -> list[tuple[PluginKind, str, str, list[PluginOperation]]]:
        """Every loaded plugin as ``(kind, name, version, operations it declares)``, started or
        not. For listings (the docs, a tool that wants to show what is installed); a plugin whose
        ``operations()`` fails declares none."""
        rows: list[tuple[PluginKind, str, str, list[PluginOperation]]] = []
        for (kind, name), rec in sorted(self._records.items()):
            if rec.instance is None or ACCOUNT_SEP in name:
                continue
            try:
                ops = declared_operations(rec.instance)
            except Exception:
                ops = []
            rows.append((kind, name, rec.info.version if rec.info else "", ops))
        return rows

    def offered_operations(self) -> list[OfferedOperation]:
        """What the plugins offer. A healthy plugin offers all its operations. One that is loaded
        but not healthy (not set up yet, or not signed in) offers only its ``per_account`` ones,
        because signing in is how it becomes healthy. An operation that cannot be read is skipped
        (it was already reported as a problem when the plugin loaded)."""
        out: list[OfferedOperation] = []
        for rec in sorted(self._records.values(), key=lambda r: (r.kind, r.name)):
            if rec.account_id is not None or rec.instance is None or rec.disabled:
                continue
            healthy = rec.started and rec.healthy
            try:
                decls = declared_operations(rec.instance)
            except Exception as exc:
                log.error("plugin operations failed", name=rec.name, error=type(exc).__name__)
                continue
            out.extend(
                OfferedOperation(rec.name, d)
                for d in decls
                if isinstance(d, PluginOperation) and (healthy or d.per_account)
            )
        return out

    def account_instance(self, plugin: str, account_id: str) -> Any:
        """The instance of ``plugin`` that serves ``account_id``, healthy or not (a command to
        sign in must reach an account that cannot sign in yet)."""
        name = f"{plugin}{ACCOUNT_SEP}{account_id}"
        for (_kind, rec_name), rec in self._records.items():
            if rec_name == name and rec.instance is not None:
                return rec.instance
        raise NotFound(f"no account {account_id!r} of plugin {plugin!r}")

    def account_context(
        self, plugin: str, account_id: str, client: str, actor: str
    ) -> PluginOpContext:
        """Like ``operation_context``, with that account's own settings."""
        name = f"{plugin}{ACCOUNT_SEP}{account_id}"
        for rec in self._records.values():
            if rec.name == name and rec.ctx is not None:
                return PluginOpContext(
                    plugin=plugin,
                    client=client,
                    actor=actor,
                    config=dict(rec.ctx.config),
                    clock=self.clock,
                    log=rec.ctx.log,
                )
        raise NotFound(f"no account {account_id!r} of plugin {plugin!r}")

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

    def offered_job_handlers(self) -> list[OfferedJobHandler]:
        """The job kinds the healthy plugins offer via ``job_handlers()``. A plugin that cannot
        say what it offers is skipped (the problem was logged when it loaded)."""
        out: list[OfferedJobHandler] = []
        for rec in self._shared():
            try:
                decls = declared_job_handlers(rec.instance)
            except Exception as exc:
                log.error("plugin job_handlers failed", name=rec.name, error=type(exc).__name__)
                continue
            out.extend(
                OfferedJobHandler(rec.name, d) for d in decls if isinstance(d, PluginJobHandler)
            )
        return out

    def job_context(self, plugin: str, kind: str) -> PluginJobContext:
        """The context for one run of ``plugin``'s job ``kind`` (the namespaced kind). No database
        handle: the plugin's own ``config``, the clock and a logger, like an operation handler."""
        for rec in self._shared():
            if rec.name == plugin and rec.ctx is not None:
                return PluginJobContext(
                    plugin=plugin,
                    kind=kind,
                    config=dict(rec.ctx.config),
                    clock=self.clock,
                    log=rec.ctx.log,
                )
        raise NotFound(f"plugin {plugin!r} is not running")

    def offered_pipeline_stages(self) -> list[OfferedPipelineStage]:
        """The pipeline stages the healthy plugins offer via ``pipeline_stages()``. A plugin that
        cannot say what it offers is skipped (the problem was logged when it loaded)."""
        out: list[OfferedPipelineStage] = []
        for rec in self._shared():
            try:
                decls = declared_pipeline_stages(rec.instance)
            except Exception as exc:
                log.error("plugin pipeline_stages failed", name=rec.name, error=type(exc).__name__)
                continue
            out.extend(
                OfferedPipelineStage(rec.name, d) for d in decls if isinstance(d, PluginStage)
            )
        return out

    def stage_context(self, plugin: str, stage: str) -> PluginStageContext:
        """The context for one run of ``plugin``'s pipeline ``stage``. No database handle."""
        for rec in self._shared():
            if rec.name == plugin and rec.ctx is not None:
                return PluginStageContext(
                    plugin=plugin,
                    stage=stage,
                    config=dict(rec.ctx.config),
                    clock=self.clock,
                    log=rec.ctx.log,
                )
        raise NotFound(f"plugin {plugin!r} is not running")

    def offered_scheduled_tasks(self) -> list[OfferedScheduledTask]:
        """The recurring tasks the healthy plugins offer via ``scheduled_tasks()``. A plugin that
        cannot say what it offers is skipped (the problem was logged when it loaded)."""
        out: list[OfferedScheduledTask] = []
        for rec in self._shared():
            try:
                decls = declared_scheduled_tasks(rec.instance)
            except Exception as exc:
                log.error("plugin scheduled_tasks failed", name=rec.name, error=type(exc).__name__)
                continue
            out.extend(
                OfferedScheduledTask(rec.name, d)
                for d in decls
                if isinstance(d, PluginScheduledTask)
            )
        return out

    def config_schema(self, kind: PluginKind, name: str) -> dict[str, Any] | None:
        """JSON Schema of a plugin's settings, if it declares ``config_model`` (pydantic)."""
        rec = self._records.get((kind, name))
        model = getattr(rec.instance, "config_model", None) if rec else None
        schema = getattr(model, "model_json_schema", None)
        return schema() if callable(schema) else None

    def declared_text(self, kind: PluginKind, name: str) -> tuple[str, str, dict[str, Any]]:
        """What a plugin says about itself for people setting it up: ``(setup_help,
        config_example, account_defaults)``, empty for one that says nothing."""
        rec = self._records.get((kind, name))
        obj = rec.instance if rec else None
        defaults = getattr(obj, "account_defaults", None)
        return (
            str(getattr(obj, "setup_help", "") or ""),
            str(getattr(obj, "config_example", "") or ""),
            dict(defaults) if isinstance(defaults, dict) else {},
        )

    @staticmethod
    def _validate_config(instance: Any, config: dict[str, Any]) -> None:
        """Validate ``config`` against a plugin's ``config_model`` (a pydantic model) before it
        starts, so a bad ``[plugins.<name>]`` table is a clear, field-level unhealthy message
        instead of a late failure inside the plugin. A plugin that declares no ``config_model`` is
        unaffected: its ``ctx.config`` stays the free-form dict. Raises ``PluginError`` on a
        validation error (the caller records it as a start failure). The validated values are not
        written back onto ``config`` — the plugin still reads its own table — this only checks."""
        model = getattr(instance, "config_model", None)
        validate = getattr(model, "model_validate", None)
        if not callable(validate):
            return
        try:
            validate(config)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'config'}: {e['msg']}"
                for e in exc.errors(include_input=False, include_url=False)[:5]
            )
            raise PluginError(f"invalid [plugins] config ({problems})") from None

    # -- lookup ------------------------------------------------------------------------------
    def _table(self, rec: _Record) -> dict[str, Any]:
        base = rec.name.partition(ACCOUNT_SEP)[0]
        return {**self.settings.plugins.get(base, {}), **(rec.overlay or {})}

    def statuses(self) -> list[PluginStatus]:
        return [
            PluginStatus(
                kind=r.kind,
                name=r.name,
                version=r.info.version if r.info else "",
                capabilities=self._caps(r),
                started=r.started,
                healthy=r.healthy,
                detail=r.detail,
                enabled=not r.disabled and self._is_enabled(r, self._table(r)),
                enabled_default=self._enabled_default(r.instance),
                allowed=r.allowed,
                declared=r.info.capabilities if r.info else frozenset(),
            )
            for r in sorted(self._records.values(), key=lambda r: (r.kind, r.name))
        ]

    def active_name(self, kind: PluginKind) -> str | None:
        """The name config selects for ``kind`` (None for enrichers: every one is used)."""
        s = self.settings
        return {
            "provider": s.sync.provider or None,
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

    def get_started(self, kind: PluginKind, name: str) -> Any:
        """The plugin called ``name`` if it started, healthy or not. For the few things that must
        work on an unhealthy plugin, such as authorizing an account whose credential is missing."""
        rec = self._records.get((kind, name))
        if rec is None or rec.instance is None:
            raise NotFound(f"no {kind} named {name!r}")
        if not rec.started:
            raise PluginError(f"{kind} {name!r} did not start: {rec.detail or 'not started'}")
        return rec.instance

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
            if capability in self._caps(usable[n])
        ]
