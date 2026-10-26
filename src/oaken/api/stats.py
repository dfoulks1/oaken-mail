# SPDX-License-Identifier: Apache-2.0
"""The data behind the read-only ``/stats`` page (ADR 0013).

Gathers the same counts and health the ``status``, ``pipeline_status``, ``usage_summary``,
``list_accounts`` and ``list_plugins`` operations expose, into one plain dict the console templates
render. **Counts and health only**: no message text, no addresses, no tokens, no log lines. The
page is served on loopback without a token (like ``/v1/health``), so the discipline that keeps
mail content out of it lives here, in what this collector is allowed to read.
"""

from dataclasses import asdict
from datetime import datetime
from typing import Any

from oaken.core.clock import Clock
from oaken.core.operations.services import Services


class StatsCollector:
    """Builds the ``/stats`` template context from the running services. Every field is a count,
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

    async def collect(self) -> dict[str, Any]:
        now = self._clock.now()
        llm = await self._llm()
        usage = await self._usage()
        models = await self._models(llm)
        data: dict[str, Any] = {
            "generated_at": now.isoformat(),
            "core": self._core(now),
            "mail": await self._mail(),
            "pipeline": await self._pipeline(),
            "usage": usage,
            "usage_rows": self._usage_view(usage, models),
            "accounts": await self._accounts(),
            "plugins": self._plugins(),
            "audit": await self._audit(),
            "operations": await self._operations_via_op(),
            "mcp": await self._mcp(),
            "agents": await self._agents(),
            "enrichers": await self._enrichers(),
            "llm": llm,
            "models": models,
        }
        return data

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
        stats = await self._svc.raw.stats()
        index = await self._svc.index.stats()
        mail: dict[str, Any] = {
            "accounts": stats.accounts,
            "messages": stats.messages - stats.deleted_messages,
            "indexed": index.docs,
            "index_stale": index.stale,
            "queue_depth": None,
        }
        if self._svc.pipeline is not None:
            mail["queue_depth"] = await self._svc.pipeline.depth()
        return mail

    async def _pipeline(self) -> dict[str, Any] | None:
        if self._svc.pipeline is None:
            return None
        status = await self._svc.pipeline.pipeline_status()
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
        try:
            models = await backend.available_models()
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
            rows.append(
                {
                    "kind": "embedder",
                    "label": "Embedder",
                    "name": ectx.name,
                    "model": embedder.model_id,
                    "choices": None,
                    "default": "",
                    "config": config,
                    "config_schema": schema,
                    "enabled": True,
                }  # fmt: skip
            )
        if llm and self._client is not None:  # ask has its own model: a core setting, not a plugin
            rows.append(
                {
                    "kind": "ask",
                    "label": "ask",
                    "name": "ask",
                    "model": self._client.svc.settings.ai.ask_model,
                    "choices": llm["available"],
                    "default": llm["model_id"],
                    "config": {},
                    "config_schema": None,
                    "enabled": True,
                }  # fmt: skip
            )
        for e in await self._enrichers():
            props = (e["config_schema"] or {}).get("properties", {})
            if "model" not in props:
                continue
            rows.append(
                {
                    "kind": "enricher",
                    "label": e["name"],
                    "name": e["name"],
                    "model": e["config"].get("model") or "",
                    "choices": llm["available"] if llm else None,
                    "default": llm["model_id"] if llm else "",
                    "config": e["config"],
                    "config_schema": e["config_schema"],
                    "enabled": e["enabled"],
                }  # fmt: skip
            )
        return rows

    @staticmethod
    def _usage_view(
        usage: list[dict[str, Any]], models: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """The usage rows with the component that owns each one, so the page can show one table:
        the model dropdown and the settings menu sit on the row that reports its tokens. Only the
        first row of a component carries the controls. A component that has recorded nothing yet
        gets a row of zeros, so it can still be edited."""
        by_name = {m["name"]: m for m in models if m["kind"] == "enricher"}
        backend = next((m for m in models if m["kind"] == "llm_backend"), None)
        embedder = next((m for m in models if m["kind"] == "embedder"), None)
        ask = next((m for m in models if m["kind"] == "ask"), None)

        def owner(stage: str) -> dict[str, Any] | None:
            if stage == "llm.ask" or stage.startswith("llm.ask."):
                return ask
            if stage.startswith("llm.enrich."):
                return by_name.get(stage.removeprefix("llm.enrich."))
            if stage.startswith("llm."):
                return backend
            return embedder if stage == "embed" else None

        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for u in usage:
            comp = owner(u["stage"])
            first = comp is not None and (comp["kind"], comp["name"]) not in seen
            if comp is not None:
                seen.add((comp["kind"], comp["name"]))
            rows.append({**u, "component": comp if first else None, "model_text": u["model_id"]})
        for comp in models:
            if (comp["kind"], comp["name"]) in seen:
                continue
            stage = {
                "enricher": f"llm.enrich.{comp['name']}",
                "embedder": "embed",
                "ask": "llm.ask",
            }.get(comp["kind"], "llm")
            rows.append(
                {"workspace": "—", "stage": stage, "model_id": comp["model"], "calls": 0,
                 "cached_calls": 0, "tokens_in": 0, "tokens_out": 0, "tokens_saved": 0,
                 "component": comp, "model_text": comp["model"]}
            )  # fmt: skip
        return rows

    async def _usage(self) -> list[dict[str, Any]]:
        if self._svc.usage is None:
            return []
        rows = await self._svc.usage.usage_summary(None, None)
        return [asdict(r) for r in rows]

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
        mounted = False
        if self._client is not None:
            cfg = self._client.svc.settings
            listed, mounted = list(cfg.mcp.workspaces), cfg.api.mcp
        out = await self._run_op("list_workspaces", {})
        known = [] if out is None else [{"name": w.name, "trust": w.trust} for w in out.workspaces]
        return {
            "mounted": mounted,
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
            if s.kind != "enricher":
                continue
            cfg = await self._run_op("get_plugin_config", {"plugin": s.name})
            out.append(
                {
                    "name": s.name,
                    "enabled": s.enabled,
                    "config": cfg.config if cfg else {},
                    "config_schema": cfg.config_schema if cfg else None,
                }
            )
        return out

    async def _run_op(self, name: str, payload: dict[str, Any]) -> Any:
        """Invoke a read operation through the CoreClient as the internal caller, returning its
        typed output (or None if there is no client, e.g. a bare collector in a unit test)."""
        if self._client is None:
            return None
        from oaken.core.operations.caller import CallerContext

        result = await self._client.call(name, payload, CallerContext.internal())
        return result.output
