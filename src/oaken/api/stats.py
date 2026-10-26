# SPDX-License-Identifier: Apache-2.0
"""The data behind the read-only ``/stats`` page (ADR 0013).

Gathers the same counts and health the ``status``, ``pipeline_status``, ``usage_summary``,
``list_accounts`` and ``list_plugins`` operations expose, into one plain dict a Jinja2 template
renders. **Counts and health only**: no message text, no addresses, no tokens, no log lines. The
page is served on loopback without a token (like ``/v1/health``), so the discipline that keeps
mail content out of it lives here, in what this collector is allowed to read.
"""

from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from oaken.core.clock import Clock
from oaken.core.operations.services import Services

_TEMPLATES = Path(__file__).resolve().parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES)),
    autoescape=select_autoescape(["html", "xml"]),  # escape everything: data may hold odd chars
)

# The one small, same-origin script for the stats page's live refresh (served at /stats.js).
STATS_JS = (_TEMPLATES / "stats.js").read_text(encoding="utf-8")

# The Oaken Mail logo, packaged with core and served at /stats-logo.png (same-origin, offline).
STATS_LOGO = (_TEMPLATES / "oaken-logo.png").read_bytes()


def render_stats(data: dict[str, Any]) -> str:
    """Render ``stats.html`` with the collected context. Autoescaped, so nothing in the data can
    inject markup even though the collector already keeps mail content out."""
    return _env.get_template("stats.html").render(**data)


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
        data: dict[str, Any] = {
            "generated_at": now.isoformat(),
            "core": self._core(now),
            "mail": await self._mail(),
            "pipeline": await self._pipeline(),
            "usage": await self._usage(),
            "accounts": await self._accounts(),
            "plugins": self._plugins(),
            "audit": await self._audit(),
            "operations": await self._operations_via_op(),
            "agents": await self._agents(),
            "enrichers": await self._enrichers(),
            "llm": await self._llm(),
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
            }
            for o in out.operations
        ]

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
