# SPDX-License-Identifier: Apache-2.0
"""The data behind the read-only ``/general`` page (ADR 0013).

Gathers the same counts and health the ``status``, ``pipeline_status``, ``usage_summary``,
``list_accounts`` and ``list_plugins`` operations expose, into one plain dict the console templates
render. **Counts and health only**: no message text, no addresses, no tokens, no log lines. The
page is served on loopback without a token (like ``/v1/health``), so the discipline that keeps
mail content out of it lives here, in what this collector is allowed to read.
"""

import asyncio
import contextlib
import time
from collections.abc import Collection
from dataclasses import asdict
from datetime import datetime
from typing import Any, ClassVar

from oaken.core.clock import Clock
from oaken.core.operations.services import Services


class StatsCollector:
    _MODELS: ClassVar[dict[str, tuple[float, list[str]]]] = {}  # the models a backend listed, 30 s
    """Builds the ``/general`` template context from the running services. Every field is a count,
    a flag, a version, a name or a timestamp — never mail content."""

    def __init__(
        self,
        svc: Services,
        *,
        clock: Clock,
        started_at: datetime | None = None,
        client: Any = None,
        audit_limit: int = 50,
    ) -> None:
        self._svc = svc
        self._clock = clock
        self._started_at = started_at
        self._client = client
        self._audit_limit = audit_limit

    async def collect(
        self,
        parts: Collection[str] | None = None,
        stages: Collection[str] | None = None,
        counts: bool = True,
    ) -> dict[str, Any]:
        """The page's data. ``parts`` names what the page shows (None: all of it): mail, pipeline,
        usage, accounts, plugins, audit, operations, mcp, agents, models. A page that needs one
        part does not pay for the others (the pipeline numbers scan the jobs table, ``models``
        asks the model server). ``stages`` narrows ``pipeline`` to those stages; ``counts=False``
        leaves the queue numbers for a later request, so the page paints at once."""

        def want(name: str) -> bool:
            return parts is None or name in parts

        now = self._clock.now()
        data: dict[str, Any] = {"generated_at": now.isoformat(), "core": self._core(now)}
        # The parts do not depend on one another (``usage`` needs the model list, so that pair
        # stays in order), so they run together: the page waits for the slowest, not the sum.
        async def with_models() -> tuple[dict[str, Any] | None, list[Any], Any]:
            llm = await self._llm()
            models = await self._models(llm)
            return llm, models, (await self._usage() if want("usage") else None)

        jobs: dict[str, Any] = {}
        if want("mail"):
            jobs["mail"] = self._mail()
        if want("pipeline"):
            jobs["pipeline"] = self._pipeline(stages, counts)
        if want("usage") or want("models"):
            jobs["models"] = with_models()
        if want("models"):
            jobs["enrichers"] = self._enrichers()
        for key, part, make in (
            ("accounts", "accounts", self._accounts), ("audit", "audit", self._audit),
            ("mcp", "mcp", self._mcp), ("agents", "agents", self._agents),
            ("schedules", "schedules", self._schedules),
            ("settings_rows", "settings", self._settings_rows),
            ("operations", "operations", self._operations_via_op),
        ):  # fmt: skip
            if want(part):
                jobs[key] = make()
        got = dict(zip(jobs, await asyncio.gather(*jobs.values()), strict=True))
        for key in ("mail", "pipeline", "accounts", "audit", "operations", "mcp", "agents",
                    "schedules", "settings_rows"):  # fmt: skip
            if key in got:
                data[key] = got[key]
        if want("plugins"):
            data["plugins"] = self._plugins()
        if want("stage_settings"):
            s = self._svc.settings
            data["stage_settings"] = {
                "triage_gate": s.ai.triage_gate,
                "embed_min_priority": s.ingest.embed_min_priority,
                "quarantine_dir": s.mail.attachments.quarantine_dir,
            }
        if "models" in got:
            llm, models, usage = got["models"]
            if want("usage"):
                data["usage"] = usage
                data["usage_rows"] = self._usage_view(usage, models)
            if want("models"):
                data["enrichers"] = got["enrichers"]
                data["llm"] = llm
                data["models"] = models
        return data

    async def front(self) -> dict[str, Any]:
        """What the front page (``/``) shows beside the daemon's environment: counts and the
        accounts, no mail text."""
        return {
            "core": self._core(self._clock.now()),
            "mail": await self._mail(),
            "accounts": await self._accounts(),
            "sync": await self._sync(),
        }

    def _core(self, now: datetime) -> dict[str, Any]:
        started = self._started_at
        uptime = (now - started).total_seconds() if started is not None else None
        return {
            "version": self._svc.version,
            "started_at": started.isoformat() if started is not None else None,
            "uptime_seconds": uptime,
            "uptime_minutes": int(uptime // 60) if uptime is not None else None,
        }

    async def _mail(self) -> dict[str, Any]:
        accounts, messages = await self._svc.raw.message_counts()
        index = await self._svc.index.stats()
        mail: dict[str, Any] = {
            "accounts": accounts,
            "messages": messages,
            "indexed": index.docs,
            "index_stale": index.stale,
            "queue_depth": None,
        }
        if self._svc.pipeline is not None:
            mail["queue_depth"] = await self._svc.pipeline.depth()
        return mail

    async def _pipeline(
        self, stages: Collection[str] | None = None, counts: bool = True
    ) -> dict[str, Any] | None:
        if self._svc.pipeline is None:
            return None
        # Through the operation, so each stage carries its pause and concurrency and what runs now.
        ask: dict[str, Any] = {"counts": counts}
        if stages:
            ask["stages"] = list(stages)
        shown = await self._run_op("pipeline_status", ask)
        if shown is not None:
            return dict(shown.model_dump())
        status = await self._svc.pipeline.pipeline_status(stages, counts=counts)
        out = asdict(status)
        if self._svc.pipeline_control is not None:
            out["llm_paused"] = (await self._svc.pipeline_control.state()).llm_paused
        return out

    async def _llm(self) -> dict[str, Any] | None:
        """The language-model backend's current model and the models it can run, so the AI tab can
        offer a picker of only what is available. None when there is no started LLM backend."""
        if self._svc.plugins is None:
            return None
        try:
            backend = self._svc.plugins.llm_backend()
        except Exception:  # noqa: BLE001 - no started backend: no picker, not an error
            return None
        try:  # a busy model server must not stall a page: a timeout, and a recent answer stands
            cached = StatsCollector._MODELS.get(backend.model_id)
            if cached is not None and time.monotonic() - cached[0] < 30:
                models = cached[1]
            else:
                models = await asyncio.wait_for(backend.available_models(), timeout=2.0)
                StatsCollector._MODELS[backend.model_id] = (time.monotonic(), models)
        except Exception:  # noqa: BLE001 - degrade to the current model
            models = [backend.model_id] if backend.model_id else []
        ctx = getattr(backend, "ctx", None)
        return {
            "name": ctx.name if ctx is not None else None,
            "model_id": backend.model_id,
            "available": models,
        }

    async def _models(self, llm: dict[str, Any] | None) -> list[dict[str, Any]]:
        """One row per component that runs a model, for the compact AI table: the language model,
        the embedder, and each enricher that can pick its own model. ``choices`` are only models
        the backend can run (None: shown, not editable). ``config`` and ``config_schema`` feed the
        plugin's settings dialog, through the real ``get_plugin_config`` operation."""
        rows: list[dict[str, Any]] = []
        plugins = self._svc.plugins
        if plugins is None:
            return rows

        async def settings(name: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
            cfg = await self._run_op("get_plugin_config", {"plugin": name})
            return (cfg.config, cfg.config_schema) if cfg else ({}, None)

        if llm and llm.get("name"):
            config, schema = await settings(llm["name"])
            rows.append(
                {
                    "kind": "llm_backend",
                    "label": "Language model",
                    "name": llm["name"],
                    "model": llm["model_id"],
                    "choices": llm["available"],
                    "default": "",
                    "config": config,
                    "config_schema": schema,
                    "enabled": True,
                }  # fmt: skip
            )
        try:
            embedder = plugins.embedder()
        except Exception:  # noqa: BLE001 - no started embedder: no row
            embedder = None
        ectx = getattr(embedder, "ctx", None)
        if embedder is not None and ectx is not None:
            config, schema = await settings(ectx.name)
            choices: list[str] | None = llm["available"] if llm else None  # the same server
            lister = getattr(embedder, "available_models", None)
            if callable(lister):
                with contextlib.suppress(Exception):  # keep the language backend's list
                    choices = list(await asyncio.wait_for(lister(), timeout=2.0))
            rows.append(
                {
                    "kind": "embedder",
                    "label": "Embedder",
                    "name": ectx.name,
                    "model": config.get("model") or "",
                    "choices": choices,
                    "default": self._svc.settings.ai.embed_model or embedder.model_id,
                    "config": config,
                    "config_schema": schema,
                    "enabled": True,
                }  # fmt: skip
            )
        for e in await self._enrichers():
            props = (e["config_schema"] or {}).get("properties", {})
            if not props:  # no settings at all: nothing to configure, nothing to list
                continue
            takes_model = "model" in props  # a plugin with no model setting gets no dropdown
            rows.append(
                {
                    "kind": e["kind"],
                    "label": e["name"],
                    "name": e["name"],
                    "model": e["config"].get("model") or "",
                    "choices": llm["available"] if llm and takes_model else None,
                    "default": llm["model_id"] if llm else "",
                    "config": e["config"],
                    "config_schema": e["config_schema"],
                    "enabled": e["enabled"],
                    "steps": e.get("steps", []),
                    "ui": e.get("ui", {"pages": [], "buttons": []}),
                }  # fmt: skip
            )
        return rows

    @staticmethod
    def _usage_view(
        usage: list[dict[str, Any]], models: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Group token usage by stage (not stage+model), summing calls and tokens across all
        models for each stage. The component controls (model dropdown, settings) appear on each
        stage's row. Stages with no recorded usage still get a row of zeros."""
        by_name = {m["name"]: m for m in models if m["kind"] in ("enricher", "triager")}
        triager = next((m for m in models if m["kind"] == "triager"), None)
        backend = next((m for m in models if m["kind"] == "llm_backend"), None)
        embedder = next((m for m in models if m["kind"] == "embedder"), None)

        def owner(stage: str) -> dict[str, Any] | None:
            if stage in ("llm.triage", "llm.enrich.triage"):  # the old name is in stored usage
                return triager  # whichever triager ran it
            if stage.startswith("llm.enrich."):
                return by_name.get(stage.removeprefix("llm.enrich."))
            if stage.startswith("llm."):
                return backend
            return embedder if stage == "embed" else None

        # aggregate by stage: sum tokens and calls
        by_stage: dict[tuple[str, str], dict[str, Any]] = {}
        for u in usage:
            ws = u["workspace"]
            stage = u["stage"]
            key = (ws, stage)
            if key not in by_stage:
                by_stage[key] = {
                    "workspace": ws,
                    "stage": stage,
                    "calls": 0,
                    "cached_calls": 0,
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "tokens_saved": 0,
                    "component": owner(stage),
                    "model_text": "",  # leave blank; this is aggregated
                }
            by_stage[key]["calls"] += u["calls"]
            by_stage[key]["cached_calls"] += u["cached_calls"]
            by_stage[key]["tokens_in"] += u["tokens_in"]
            by_stage[key]["tokens_out"] += u["tokens_out"]
            by_stage[key]["tokens_saved"] += u["tokens_saved"]

        rows = list(by_stage.values())
        seen = {
            (r["component"]["kind"], r["component"]["name"])
            for r in rows
            if r["component"] is not None
        }

        # add stages with no usage yet
        for comp in models:
            if (comp["kind"], comp["name"]) in seen:
                continue
            stage = {
                "enricher": f"llm.enrich.{comp['name']}",
                "embedder": "embed",
            }.get(comp["kind"], "llm")
            rows.append(
                {"workspace": "—", "stage": stage, "calls": 0,
                 "cached_calls": 0, "tokens_in": 0, "tokens_out": 0, "tokens_saved": 0,
                 "component": comp, "model_text": comp["model"]}
            )  # fmt: skip
        return rows

    async def _usage(self) -> list[dict[str, Any]]:
        if self._svc.usage is None:
            return []
        rows = await self._svc.usage.usage_summary(None, None)
        return [asdict(r) for r in rows]

    async def _settings_rows(self) -> list[dict[str, Any]]:
        """Every plugin that has settings, active or not, for its configure window: its table in
        ``config.toml`` and the schema it declares (through ``get_plugin_config``). A plugin that is
        installed but not selected (a second LLM backend) is configured here too. A provider's
        per-account instances share its settings, so only the plugin itself is listed."""
        if self._svc.plugins is None:
            return []
        rows: list[dict[str, Any]] = []
        for s in self._svc.plugins.statuses():
            if "@" in s.name:
                continue
            cfg = await self._run_op("get_plugin_config", {"plugin": s.name, "kind": s.kind})
            schema = cfg.config_schema if cfg else None
            if not (schema or {}).get("properties"):
                continue
            steps: list[dict[str, Any]] = []
            if s.kind == "triager":
                with contextlib.suppress(Exception):  # a plugin's own listing must not break a page
                    lister = getattr(
                        self._svc.plugins.named_triagers().get(s.name), "describe", None
                    )
                    steps = list(lister()) if callable(lister) else []
            plug = {
                "triager": self._svc.plugins.named_triagers,
                "enricher": self._svc.plugins.named_enrichers,
            }.get(s.kind, dict)().get(s.name)
            built = getattr(plug, "prompt", None)
            rows.append(
                {
                    "prompts": {"guard": built.guard, "prompt": built.instructions} if built else None,
                    "kind": s.kind,
                    "name": s.name,
                    "label": s.name,
                    "config": cfg.config if cfg else {},
                    "config_schema": schema,
                    "steps": steps,
                    "ui": self._ui(s.kind, s.name),
                }
            )
        return rows

    async def _schedules(self) -> list[dict[str, Any]]:
        """Every sync schedule, for the Providers tab's schedule manager."""
        if self._svc.sync is None:
            return []
        rows = []
        for s in await self._svc.sync.schedules():
            rows.append(
                {
                    "account_id": s.account_id,
                    "folder": s.folder,
                    "schedule": s.schedule,
                    "timezone": s.timezone,
                    "quiet": "-".join(s.quiet) if s.quiet else "",
                    "paused": s.paused,
                    "paused_reason": s.paused_reason,
                    "next_run": s.next_run.isoformat(),
                    "last_run": s.last_run.isoformat() if s.last_run else None,
                    "last_result": s.last_result,
                    "last_error": s.last_error,
                    "last_new": s.last_new,
                    "failures": s.failures,
                    "problem": s.problem,
                }
            )
        return rows

    async def _sync(self) -> dict[str, Any]:
        """The state of syncing and of the pipeline's pauses, for the front page: whether a
        backfill is held, whether the model stages are paused, and each account's next run."""
        out: dict[str, Any] = {"backfill_paused": False, "llm_paused": False, "schedules": []}
        if self._svc.pipeline is not None:
            out["backfill_paused"] = await self._svc.pipeline.backfill_paused()
        if self._svc.pipeline_control is not None:
            out["llm_paused"] = (await self._svc.pipeline_control.state()).llm_paused
        out["schedules"] = await self._schedules()
        return out

    async def _accounts(self) -> list[dict[str, Any]]:
        # id, provider, display name and a sync summary — never credentials.
        rows: list[dict[str, Any]] = []
        schedules = {}
        if self._svc.sync is not None:
            for s in await self._svc.sync.schedules():
                schedules[s.account_id] = s
        for a in await self._svc.raw.list_accounts():
            sched = schedules.get(a.id)
            rows.append(
                {
                    "account_id": a.id,
                    "provider": a.provider,
                    "display_name": a.display_name,
                    "paused": bool(getattr(sched, "paused", False)) if sched else None,
                    "failures": int(getattr(sched, "failures", 0)) if sched else None,
                }
            )
        return rows

    def _plugins(self) -> list[dict[str, Any]]:
        if self._svc.plugins is None:
            return []
        return [
            {
                "name": s.name,
                "kind": s.kind,
                "version": s.version,
                "started": s.started,
                "healthy": s.healthy,
                "detail": s.detail,
                "enabled": s.enabled,
                "capabilities": sorted(s.capabilities),
            }
            for s in self._svc.plugins.statuses()
        ]

    async def _audit(self) -> list[dict[str, Any]]:
        """Recent audit rows, via the real ``list_audit`` operation (ids and metadata only — the
        audit log never holds message text). Surfaced so a person can see what happened, and what
        went wrong, without reading the source or the shell."""
        out = await self._run_op("list_audit", {"limit": self._audit_limit})
        if out is None:
            return []
        return [
            {
                "audit_id": e.audit_id,
                "ts": e.ts.isoformat(),
                "actor": f"{e.actor_kind}:{e.actor_label}" if e.actor_label else e.actor_kind,
                "op": e.op,
                "reason": e.reason,
                "count": e.count,
                "undone_by": e.undone_by,
            }
            for e in out.entries
        ]

    async def _operations_via_op(self) -> list[dict[str, Any]]:
        """The operation reference, via the real ``list_operations`` operation — so the page shows
        exactly what that op reports, including the ceiling-aware MCP exposure, not a re-derivation.
        """
        out = await self._run_op("list_operations", {})
        if out is None:
            return []
        return [
            {
                "name": o.name,
                "summary": o.summary,
                "permission": o.permission,
                "plugin": o.plugin,
                "mcp_allowed": o.mcp_allowed,
                "mcp_default": o.mcp_default,
                "mcp_enabled": o.mcp_enabled,
                "mcp_locked_reason": o.mcp_locked_reason,
            }
            for o in out.operations
        ]

    async def _mcp(self) -> dict[str, Any]:
        """Which workspaces an MCP client may reach, each with its trust, for the Reference tab.
        ``listed`` is ``[mcp] workspaces`` as saved (empty means every workspace)."""
        listed: list[str] = []
        mounted = enabled = False
        if self._client is not None:
            cfg = self._client.svc.settings
            listed, mounted, enabled = list(cfg.mcp.workspaces), cfg.api.mcp, cfg.mcp.enabled
        out = await self._run_op("list_workspaces", {})
        known = [] if out is None else [{"name": w.name, "trust": w.trust} for w in out.workspaces]
        return {
            "mounted": mounted,
            "enabled": enabled,
            "listed": listed,
            "all": not listed,
            "workspaces": [{**w, "linked": not listed or w["name"] in listed} for w in known],
        }

    async def _agents(self) -> list[dict[str, Any]]:
        """The registered external agents, via the real ``list_agents`` operation (never tokens).
        Empty when the swarm is off, so the poll never fails on a disabled registry."""
        if getattr(self._svc, "agents", None) is None:
            return []
        out = await self._run_op("list_agents", {"include_revoked": True})
        if out is None:
            return []
        return [
            {
                "id": a.id,
                "label": a.label,
                "kinds": a.kinds,
                "last_seen_at": a.last_seen_at.isoformat() if a.last_seen_at else None,
                "revoked": a.revoked,
            }
            for a in out.agents
        ]

    async def _enrichers(self) -> list[dict[str, Any]]:
        """Each enricher with its settings and their JSON Schema, via the real ``get_plugin_config``
        op (CORE_23) — so the AI tab can render a config form from the schema the plugin declares.
        No schema (no ``config_model``) means no form, just the on/off switch on the Plugins tab."""
        if self._svc.plugins is None:
            return []
        out: list[dict[str, Any]] = []
        for s in self._svc.plugins.statuses():
            if s.kind not in ("enricher", "triager"):
                continue
            cfg = await self._run_op("get_plugin_config", {"plugin": s.name, "kind": s.kind})
            steps: list[dict[str, Any]] = []
            if s.kind == "triager":  # a triager made of several checks lists them, in order
                try:
                    lister = getattr(
                        self._svc.plugins.named_triagers().get(s.name), "describe", None
                    )
                    steps = list(lister()) if callable(lister) else []
                except Exception:  # noqa: BLE001 - a plugin's own listing must not take the page down
                    steps = []
            out.append(
                {
                    "kind": s.kind,
                    "name": s.name,
                    "enabled": s.enabled,
                    "config": cfg.config if cfg else {},
                    "config_schema": cfg.config_schema if cfg else None,
                    "steps": steps,
                    "ui": self._ui(s.kind, s.name),
                }
            )
        return out

    def _ui(self, kind: str, name: str) -> dict[str, Any]:
        """What the plugin's declared UI offers its settings dialog: page links and buttons."""
        host = self._svc.plugins
        spec = host.plugin_ui(kind, name) if host is not None else None  # type: ignore[arg-type]
        if spec is None:
            return {"pages": [], "buttons": []}
        return {
            "pages": [{"name": p.name, "title": p.title} for p in spec.pages],
            "buttons": [{"label": b.label, "operation": b.operation, "confirm": b.confirm}
                        for b in spec.buttons],
        }  # fmt: skip

    async def _run_op(self, name: str, payload: dict[str, Any]) -> Any:
        """Invoke a read operation through the CoreClient as the internal caller, returning its
        typed output (or None if there is no client, e.g. a bare collector in a unit test)."""
        if self._client is None:
            return None
        from oaken.core.operations.caller import CallerContext

        result = await self._client.call(name, payload, CallerContext.internal())
        return result.output
