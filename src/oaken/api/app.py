# SPDX-License-Identifier: Apache-2.0
"""The local HTTP API: a thin skin over the operation registry.

There is no route per operation. ``POST /v1/operations/{name}`` looks the name up in the registry
and calls ``CoreClient``, so the gate, the limits and the audit row are the ones prompt 13 built.
The routes here only authenticate (``guard``), pick the caller class, and turn errors into
``{"code", "message"}``. See ADR 0005.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, ClassVar, TypeVar

from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import BaseRoute, Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from oaken.api import models
from oaken.api.console import CHART_UMD_JS, CSS, JOBS_CHART_JS, LOGO, REFRESH_JS, Console
from oaken.api.errors import INTERNAL, ApiError, body, status_for
from oaken.api.guard import Guard, Policy, Surface
from oaken.api.openapi import build_api_openapi
from oaken.api.ratelimit import RateLimiter
from oaken.api.ring import EventRing
from oaken.api.runtime import same_secret
from oaken.api.stats import StatsCollector
from oaken.api.welcome import welcome_context
from oaken.core.agents import TOKEN_PREFIX, Agent
from oaken.core.errors import Conflict, OakenError, PermissionDenied, Unavailable
from oaken.core.ids import JobId
from oaken.core.logging import get_logger
from oaken.core.operations.caller import CallerContext
from oaken.core.operations.client import CallResult, CoreClient
from oaken.core.operations.registry import AnyOperation

log = get_logger("oaken.api")

PUBLIC_CSP = "default-src 'self'; frame-ancestors 'none'; base-uri 'none'"
PLACEHOLDER = (
    "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
    "<title>Oaken Mail</title><body style='font:16px system-ui;margin:3rem auto;max-width:36rem'>"
    "<h1>Oaken Mail</h1><p>Core is running. The web interface is not installed yet.</p>"
)

Handler = Callable[[Request], Awaitable[Response]]


def _json(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return _json(body(code, message), status)


def safe(fn: Handler) -> Handler:
    """Turn what a handler raises into the error body. Core's own errors keep their code; anything
    else is ``internal`` and logged by type only, because its text could quote mail."""

    async def wrapped(request: Request) -> Response:
        try:
            return await fn(request)
        except ApiError as exc:
            return _error(exc.status, exc.code, exc.message)
        except OakenError as exc:
            return _error(status_for(exc), exc.code, exc.message)
        except Exception as exc:  # reduced to its type: the message may hold mail text
            log.exception("request failed", path=request.url.path, error=type(exc).__name__)
            return _json(INTERNAL, 500)

    return wrapped


async def read_json(request: Request) -> dict[str, Any]:
    raw = await request.body()
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        raise ApiError(400, "bad_json", "the body is not valid JSON") from None
    if not isinstance(data, dict):
        raise ApiError(400, "bad_json", "the body must be a JSON object")
    return data


M = TypeVar("M", bound=BaseModel)


def parse(model: type[M], data: dict[str, Any]) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as exc:  # where and what, never the value
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'body'}: {e['msg']}"
            for e in exc.errors(include_input=False, include_url=False)[:5]
        )
        raise ApiError(422, "invalid_input", problems) from None


def caller_of(request: Request) -> CallerContext:
    """The registry's caller class. The surface the path is on can fix it (the MCP mount does);
    otherwise a person's token may *lower* it with ``X-Oaken-Client``. ``internal`` can never be
    asked for, and a header can never raise ``mcp`` to a person."""
    info = request.scope["oaken"]
    if info["client"] == "mcp":
        return CallerContext.mcp("mcp")
    claim = request.headers.get("x-oaken-client", "cli").strip().lower()
    if claim == "cli":
        return CallerContext.cli()
    if claim == "ui":
        return CallerContext.ui()
    if claim == "mcp":
        return CallerContext.mcp("api")
    raise ApiError(400, "bad_client", "X-Oaken-Client must be cli, ui or mcp")


def operation_info(op: AnyOperation) -> models.OperationInfo:
    return models.OperationInfo(
        name=op.name,
        summary=op.summary,
        permission=op.permission,
        marks_read=op.marks_read,
        streaming=op.streaming,
        mcp=op.mcp,
        input_schema=op.input_schema(),
        output_schema=op.output_schema(),
    )


def call_body(op: AnyOperation, result: CallResult) -> dict[str, Any]:
    out: dict[str, Any] = {
        "request_id": result.request_id,
        "status": "ok" if result.executed else "confirmation_required",
        "output": result.output.model_dump(mode="json") if result.output is not None else None,
    }
    if op.permission == "mutate":
        out["proposal"] = result.proposal.model_dump(mode="json") if result.proposal else None
    return out


def _moved(to: str) -> Handler:
    """A permanent redirect to ``to``, keeping the query string (``/stats?tab=queue``)."""

    async def go(request: Request) -> Response:
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(to + query, status_code=301)

    return go


class Api:
    """The ASGI application. ``bind`` it to the port once known; ``mount`` another door."""

    def __init__(
        self,
        client: CoreClient,
        *,
        token: str,
        rate_now: Callable[[], float] | None = None,
        started_at: Any = None,
    ) -> None:
        self.client = client
        self._token = token
        self._started_at = started_at
        svc = client.svc
        cfg = svc.settings.api
        self.cfg = cfg
        limiter = RateLimiter(cfg.rate_limit_per_minute, now=rate_now or time.monotonic)
        self.policy = Policy(
            dev_origin=cfg.dev_origin,
            limiter=limiter,
            public_csp=PUBLIC_CSP,
            extra_hosts=[*cfg.allowed_hosts, cfg.host],
        )
        agent_body = svc.settings.jobs.max_result_bytes + 64 * 1024
        self.policy.surfaces = [
            Surface("general", "/v1/", self._general, cfg.max_body_bytes, max_concurrent=64),
            Surface("agent", "/v1/agent/", self._agent, agent_body, max_concurrent=16),
        ]
        self.ring = EventRing(cfg.event_ring_size)
        self._closing = False
        self._detach: Callable[[], None] | None = None
        self._port: int | None = None
        self.console = Console(client, started_at=started_at)
        self.routes: list[BaseRoute] = self._routes()
        self.starlette = Starlette(
            routes=self.routes,
            exception_handlers={HTTPException: self._http_error},
        )
        self.app: ASGIApp = Guard(self.starlette, self.policy)

    # -- lifecycle ---------------------------------------------------------------------------
    def bind(self, port: int) -> None:
        """Allow the Host and Origin values for the port core really listens on."""
        self._port = port
        self.policy.bind(port)

    def start(self) -> None:
        if self.client.svc.bus is not None and self._detach is None:
            self._detach = self.ring.attach(self.client.svc.bus)

    def stop(self) -> None:
        """Close the event streams, so shutting the server down does not wait on them."""
        if self._detach is not None:
            self._detach()
            self._detach = None
        self.ring.close()
        self._closing = True  # open trace streams end at their next beat

    def mount(self, surface: Surface, app: ASGIApp) -> None:
        """Serve ``app`` at ``surface.prefix`` behind the same guard, with its own token and limits.
        The MCP doors mount here (the person's at ``/mcp``, an agent's at ``/mcp/agent``); the
        surface, not a header, sets the caller's class."""
        self.policy.surfaces.append(surface)
        base = surface.prefix.rstrip("/")
        # A ``Route`` for exactly this path, not a ``Mount``: a Mount does not match its own bare
        # path (a client that posts to ``/mcp`` would be redirected), and a door has no subpaths.
        self.starlette.router.routes.insert(0, Route(base, app))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)

    # -- authentication ----------------------------------------------------------------------
    async def _general(self, token: str) -> str | None:
        return "user" if same_secret(token, self._token) else None

    async def _agent(self, token: str) -> Agent | None:
        agents = self.client.svc.agents
        if agents is None or not token.startswith(TOKEN_PREFIX):
            return None
        try:
            return await agents.authenticate(token)
        except PermissionDenied:
            return None

    # -- routes ------------------------------------------------------------------------------
    def _routes(self) -> list[BaseRoute]:
        routes: list[BaseRoute] = [
            Route("/v1/health", safe(self.health), methods=["GET"]),
            Route("/v1/operations", safe(self.list_operations), methods=["GET"]),
            Route("/v1/operations/{name}", safe(self.call_operation), methods=["POST"]),
            Route("/v1/openapi.json", safe(self.openapi), methods=["GET"]),
            Route("/v1/events", self.events, methods=["GET"]),
            Route("/v1/trace/stream", self.trace_stream, methods=["GET"]),
            Route("/v1/agent/jobs/claim", safe(self.claim), methods=["POST"]),
            Route("/v1/agent/jobs/{job_id}/heartbeat", safe(self.heartbeat), methods=["POST"]),
            Route("/v1/agent/jobs/{job_id}/complete", safe(self.complete), methods=["POST"]),
            Route("/v1/agent/jobs/{job_id}/fail", safe(self.fail), methods=["POST"]),
            Route("/general", safe(self.general), methods=["GET"]),
            Route("/general.json", safe(self.general_json), methods=["GET"]),
            Route("/config", safe(self.config_editor), methods=["GET"]),
            Route("/getting-started", safe(self.getting_started), methods=["GET"]),
            # Older paths still lead somewhere: the page moved, or got a tidier name.
            Route("/jobs", safe(self.jobs_page), methods=["GET"]),
            Route("/stats", _moved("/general"), methods=["GET"]),
            Route("/stats.json", _moved("/general.json"), methods=["GET"]),
            Route("/getting_started", _moved("/getting-started"), methods=["GET"]),
            Route("/pipeline", safe(self.pipeline_page), methods=["GET"]),
            Route("/triage", safe(self.triage_page), methods=["GET"]),
            Route("/plugins/{kind}/{name}/{page}", safe(self.plugin_page), methods=["GET"]),
            Route("/embed", safe(self.embed_page), methods=["GET"]),
            Route("/enrich", safe(self.enrich_page), methods=["GET"]),
            Route("/action", safe(self.action_page), methods=["GET"]),
            Route("/capabilities", safe(self.capabilities_page), methods=["GET"]),
            Route("/pipeline.json", safe(self.pipeline_json), methods=["GET"]),
            Route("/oaken.css", safe(self.asset_css), methods=["GET"]),
            Route("/oaken-logo.png", safe(self.asset_logo), methods=["GET"]),
            Route("/refresh.js", safe(self.asset_refresh), methods=["GET"]),
            Route("/throughput-chart.js", safe(self.asset_throughput_chart), methods=["GET"]),
            Route("/chart.umd.js", safe(self.asset_chart), methods=["GET"]),
            Route("/ui/config-test", safe(self.ui_config_test), methods=["POST"]),
            Route("/ui/{action}", safe(self.ui_action), methods=["POST"]),
        ]
        static = Path(self.cfg.static_dir).expanduser() if self.cfg.static_dir else None
        if static is not None and static.is_dir():
            routes.append(Mount("/", app=StaticFiles(directory=static, html=True), name="ui"))
        else:
            routes.append(Route("/", self.placeholder, methods=["GET"]))
        return routes

    @staticmethod
    async def _http_error(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, HTTPException)  # noqa: S101
        codes = {404: "not_found", 405: "method_not_allowed"}
        return _error(exc.status_code, codes.get(exc.status_code, "error"), "no such route")

    async def health(self, request: Request) -> Response:
        return _json(models.Health().model_dump())

    # -- the console (prompt 50) ---------------------------------------------------------------
    # Core's own pages. Each is server-rendered HTML with one stylesheet and no script (the one
    # optional exception, /refresh.js, only updates numbers). Every control is a form posting to
    # /ui/<action>; see ``oaken.api.console``. Loopback-only and token-free like /general, and the
    # actions run the real operations as the internal caller with confirm=True, so each still lands
    # in the audit log.
    async def _op(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        out = await self.client.call(name, payload, CallerContext.internal(), confirm=True)
        return out.output.model_dump(mode="json") if out.output is not None else {}

    async def placeholder(self, request: Request) -> Response:
        """Core's home page (``/``): the version, the daemon's paths and port, and a short live
        status. If it cannot be rendered it falls back to plain text so ``/`` always answers."""
        try:
            context = welcome_context(version=self.client.svc.version, port=self._port)
            try:
                context.update(await self._stats_collector().front())
            except Exception:  # noqa: BLE001 - counts are a nicety; the home page still answers
                log.error("front page counts failed")
            return self.console.render(request, "welcome.html", "home", context)
        except Exception:  # noqa: BLE001 - the home page must never 500; fall back to plain text
            log.error("welcome render failed; serving placeholder")
            return HTMLResponse(PLACEHOLDER)

    async def _config_page(self, request: Request, **extra: Any) -> Response:
        info = await self._op("config_get", {})
        context = {"text": info.get("text", ""), "path": info.get("path", "config.toml"),
                   "exists": info.get("exists", False), **extra}  # fmt: skip
        return self.console.render(request, "config.html", "config", context)

    async def config_editor(self, request: Request) -> Response:
        """The config.toml editor: a textarea with Test, Save and Reload, over the ``config_*``
        operations."""
        return await self._config_page(request)

    async def ui_config_test(self, request: Request) -> Response:
        """Test the text in the editor and answer on the same page with the text intact."""

        async def render(text: str, flash: Any) -> Response:
            return await self._config_page(request, text=text, flash=flash)

        form_text = await request.body()
        return await self.console.config_test(request, form_text, render)

    async def jobs_page(self, request: Request) -> Response:
        """The jobs page moved into General (its Queue and Ingest tabs); old links still work."""
        return RedirectResponse("/general?tab=queue", status_code=303)

    # Which stage a model call or an agent belongs to, so each stage's page shows its own.
    @staticmethod
    def _usage_stage(stage: str) -> str:
        if stage in ("llm.triage", "llm.enrich.triage"):  # the old name is in stored usage
            return "triage"
        if stage == "llm.action" or stage.startswith("llm.action."):
            return "action"
        if stage == "embed" or stage.startswith(("embed.", "llm.embed")):
            return "embed"
        if stage == "enrich" or stage.startswith("llm.enrich."):
            return "enrich"
        return "pipeline"  # the language model itself, ask and routing, anything else

    @staticmethod
    def _agent_stages(kinds: list[str]) -> set[str]:
        out: set[str] = set()
        for k in kinds:
            if k == "ingest.triage":
                out.add("triage")
            elif k == "ingest.embed":
                out.add("embed")
            elif k == "ingest.enrich" or k.startswith("ingest.enrich."):
                out.add("enrich")
        return out

    async def _stage_page(
        self, request: Request, kind: str, stage: str, label: str, plural: str
    ) -> Response:
        data = await self._stats_collector().collect(
            {"pipeline", "plugins", "models", "usage", "agents", "stage_settings", "settings"},
            stages={stage},
            counts=False,
        )
        data["usage_rows"] = [
            r for r in data.get("usage_rows", []) if self._usage_stage(r["stage"]) == stage
        ]
        data["agents"] = [
            a for a in data.get("agents", []) if stage in self._agent_stages(a["kinds"])
        ]
        have = data.get("stage_settings", {})
        fields: list[dict[str, Any]] = []
        if stage == "triage":
            fields.append(
                {
                    "section": "ai",
                    "key": "triage_gate",
                    "label": "Triage gate",
                    "choices": ["strict", "promiscuous"],
                    "value": have.get("triage_gate"),
                    "hint": "strict: an enricher with a min_priority waits until triage rates the "
                    "message. promiscuous: it runs on unrated mail.",
                }
            )
        if stage == "embed":
            fields.append(
                {
                    "section": "ingest",
                    "key": "embed_min_priority",
                    "label": "Embed mail rated at least",
                    "choices": None,
                    "value": have.get("embed_min_priority"),
                    "hint": "0 embeds everything; 1 skips mail triage rated 0 (junk). Blank "
                    "restores the default.",
                }
            )
        if stage == "action":
            listed = await self.console.op("list_actions", {})
            data["action_waiting"], data["action_recent"] = listed.waiting, listed.recent
        data.update(
            stage_kind=kind,
            stage_name=stage,
            stage_label=label,
            stage_plural=plural,
            stage_fields=fields,
        )
        return self.console.render(request, "stage.html", stage, data)

    async def triage_page(self, request: Request) -> Response:
        return await self._stage_page(request, "triager", "triage", "Triage", "Triagers")

    async def plugin_page(self, request: Request) -> Response:
        return await self.console.plugin_page(request)

    async def embed_page(self, request: Request) -> Response:
        return await self._stage_page(request, "embedder", "embed", "Embed", "Embedders")

    async def enrich_page(self, request: Request) -> Response:
        return await self._stage_page(request, "enricher", "enrich", "Enrich", "Enrichers")

    async def action_page(self, request: Request) -> Response:
        return await self._stage_page(request, "action", "action", "Action", "Actions")

    async def capabilities_page(self, request: Request) -> Response:
        """What this core can do, by plugin kind, and what MCP clients may call."""
        data = await self._stats_collector().collect({"operations", "plugins", "mcp"})
        return self.console.render(request, "capabilities.html", "capabilities", data)

    async def getting_started(self, request: Request) -> Response:
        """A short in-app walkthrough of the config.toml settings."""
        host = self.client.svc.plugins
        providers = (
            [
                {"name": name, "setup_help": text[0], "config_example": text[1]}
                for kind, name, _version, _ops in host.catalog()
                if kind == "provider"
                for text in [host.declared_text(kind, name)]
            ]
            if host is not None
            else []
        )
        return self.console.render(request, "getting_started.html", "", {"providers": providers})

    async def pipeline_json(self, request: Request) -> Response:
        """The pipeline numbers as JSON, for the optional refresh script."""
        return _json(await self._op("pipeline_status", {}))

    async def ui_action(self, request: Request) -> Response:
        return await self.console.act(request)

    def _stats_collector(self) -> StatsCollector:
        return StatsCollector(
            self.client.svc,
            clock=self.client.svc.clock,
            started_at=self._started_at,
            client=self.client,
        )

    # What each General tab needs. A tab loads only its own data: the pipeline numbers scan the
    # jobs table and ``models`` asks the model server, so a page that does not show them skips them.
    GENERAL_PARTS: ClassVar[dict[str, frozenset[str]]] = {
        "ingest": frozenset({"pipeline"}),
        "queue": frozenset({"agents"}),
        "audit": frozenset({"audit"}),
    }

    # The Pipeline page: the plugins behind the stages, one kind per tab.
    PIPELINE_PARTS: ClassVar[dict[str, frozenset[str]]] = {
        "provider": frozenset({"plugins", "schedules", "accounts", "settings"}),
        "sieve": frozenset(),
        "llm_backend": frozenset({"plugins", "models", "usage", "settings"}),
        "vector_store": frozenset({"plugins", "settings"}),
        "cache_store": frozenset({"plugins", "settings"}),
    }

    async def pipeline_page(self, request: Request) -> Response:
        tab = request.query_params.get("tab", "provider")
        tab = tab if tab in self.PIPELINE_PARTS else "provider"
        data = await self._stats_collector().collect(self.PIPELINE_PARTS[tab])
        data["usage_rows"] = [
            r for r in data.get("usage_rows", []) if self._usage_stage(r["stage"]) == "pipeline"
        ]
        if tab == "sieve":
            data["sieve"] = await self._sieve_view(request.query_params.get("script", ""))
        return self.console.render(request, "pipeline.html", "pipeline", data)

    async def _sieve_view(self, chosen: str) -> dict[str, Any]:
        """The Sieve scripts on disk (names and problems, never mail) and, for ``?script=NAME``,
        that script's text for the editor."""
        op = self.console.op
        listed = await op("sieve_list", {})
        view: dict[str, Any] = {"list": listed, "open": None}
        if chosen and any(s.name == chosen for s in listed.scripts):
            view["open"] = await op("sieve_get", {"name": chosen})
        return view

    async def general(self, request: Request) -> Response:
        """General (ADR 0013). Loopback-only (the Host guard) and unauthenticated like
        ``/v1/health``; counts and health only, never mail content."""
        tab = request.query_params.get("tab", "ingest")
        tab = tab if tab in self.GENERAL_PARTS else "ingest"
        data = await self._stats_collector().collect(
            self.GENERAL_PARTS[tab],
            counts=tab != "ingest",  # Ingest paints first, then fills
        )
        if tab == "queue":
            # The default view is the jobs that matter (running, queued, failed): a status filter
            # is an index lookup, while "all" sorts every job ever run. "all" is still one click.
            status = request.query_params.get("status", "")
            known = ("queued", "running", "failed", "cancelled", "succeeded")
            wanted = (
                (status,)
                if status in known
                else (() if status == "all" else ("running", "queued", "failed"))
            )
            jobs: list[dict[str, Any]] = []
            if status == "all":
                jobs = (await self._op("list_jobs", {"limit": 100})).get("jobs", [])
            for st in wanted:
                jobs += (await self._op("list_jobs", {"status": st, "limit": 50})).get("jobs", [])
            if len(wanted) > 1:
                jobs.sort(key=lambda j: str(j["created_at"]), reverse=True)
            data["jobs"] = jobs
            failed = [j for j in jobs if j["status"] == "failed"] if status != "all" else []
            if status in known and status != "failed":
                failed = (await self._op("list_jobs", {"status": "failed", "limit": 20})).get(
                    "jobs", []
                )
            data["parked"] = [
                {
                    "job_id": j["id"],
                    "stage": str(j["kind"]).removeprefix("ingest."),
                    "message_id": None,
                    "attempts": j["attempts"],
                    "error": j.get("last_error") or "",
                }
                for j in failed[:20]
                if str(j["kind"]).startswith("ingest.")
            ]
            data.update(status=status, statuses=known)
        return self.console.render(request, "general.html", "general", data)

    async def general_json(self, request: Request) -> Response:
        """The same stats as the page, as JSON, for the optional refresh script. Loopback-only and
        unauthenticated like `/general`; counts and health only."""
        stage = request.query_params.get("stage", "")
        tab = request.query_params.get("tab", "")
        if stage:  # a stage page's live numbers
            data = await self._stats_collector().collect({"pipeline"}, stages={stage})
        elif tab in self.GENERAL_PARTS:
            data = await self._stats_collector().collect(self.GENERAL_PARTS[tab])
        else:
            data = await self._stats_collector().collect()
        return _json(data)

    # The stylesheet, the logo and the optional script are served from the package, so the pages
    # load nothing from another origin.
    async def asset_css(self, request: Request) -> Response:
        return Response(
            CSS,
            media_type="text/css",
            headers={"cache-control": "public, max-age=31536000, immutable"},
        )

    async def asset_logo(self, request: Request) -> Response:
        return Response(LOGO, media_type="image/png", headers={"cache-control": "max-age=86400"})

    async def asset_refresh(self, request: Request) -> Response:
        return Response(
            REFRESH_JS, media_type="text/javascript", headers={"cache-control": "no-store"}
        )

    async def asset_throughput_chart(self, request: Request) -> Response:
        return Response(
            JOBS_CHART_JS, media_type="text/javascript", headers={"cache-control": "no-store"}
        )

    async def asset_chart(self, request: Request) -> Response:
        return Response(
            CHART_UMD_JS,
            media_type="text/javascript",
            headers={"cache-control": "public, max-age=31536000, immutable"},
        )

    async def list_operations(self, request: Request) -> Response:
        ops = await self.client.visible(caller_of(request))
        listing = models.OperationList(operations=[operation_info(o) for o in ops])
        return _json(listing.model_dump(mode="json"))

    async def openapi(self, request: Request) -> Response:
        return _json(build_api_openapi(self.client.registry.all(), version=self.client.svc.version))

    async def call_operation(self, request: Request) -> Response:
        name = request.path_params["name"]
        caller = caller_of(request)
        req = parse(models.CallRequest, await read_json(request))
        op = self.client.registry.get(name)
        result = await self.client.call(
            name, req.input, caller, confirm=req.confirm, confirmation_token=req.confirmation_token
        )
        return JSONResponse(call_body(op, result), headers={"x-request-id": result.request_id})

    async def events(self, request: Request) -> Response:
        raw = request.headers.get("last-event-id", "")
        last = int(raw) if raw.isdigit() else None
        stream = self.ring.stream(last, self.cfg.heartbeat_seconds)
        return StreamingResponse(
            stream,
            media_type="text/event-stream",
            headers={"cache-control": "no-store", "x-accel-buffering": "no"},
        )

    async def trace_stream(self, request: Request) -> Response:
        """The trace as it happens (``oaken trace -f``): server-sent events, filtered by plugin, job
        or message, pushed when an event is emitted. Authenticated like every ``/v1`` route; the
        trace holds mail text, so it is never on a token-free page."""
        from oaken.core.events import TraceEvent
        from oaken.core.operations.builtin.trace import matches, want_from

        params = request.query_params
        want = want_from(params.get("plugin"), params.get("job_id"), params.get("message_id"))
        kinds = set(params.getlist("kind"))
        self.client.svc.need_tracer()
        bus = self.client.svc.bus
        if bus is None:
            raise Unavailable("there is no event bus in this core")
        stream = bus.stream(
            lambda e: (
                isinstance(e, TraceEvent)
                and matches(e.data, want)
                and (not kinds or e.kind in kinds)
            )
        )

        async def frames() -> AsyncIterator[str]:
            yield ": connected\n\n"
            try:
                while not self._closing:
                    try:
                        event = await asyncio.wait_for(
                            stream.__anext__(), self.cfg.heartbeat_seconds
                        )
                    except TimeoutError:
                        yield ": heartbeat\n\n"
                        continue
                    except StopAsyncIteration:
                        return
                    if not isinstance(event, TraceEvent):
                        continue
                    row = {"ts": event.ts.isoformat(), "kind": event.kind, "data": event.data}
                    yield f"event: trace\ndata: {json.dumps(row, default=str)}\n\n"
            finally:
                await stream.aclose()

        return StreamingResponse(
            frames(),
            media_type="text/event-stream",
            headers={"cache-control": "no-store", "x-accel-buffering": "no"},
        )

    # -- the agent surface (a separate token; see ``claim`` and the swarm page) --------------------
    def _agent_call(self, agent: Agent, name: str, body: dict[str, Any]) -> Any:
        """Run an agent operation as this agent: the same operations (and rules) as the MCP agent
        door, so the HTTP routes and the CLI are not a second implementation."""
        from oaken.core.operations.builtin import agent_registry

        client = CoreClient(
            agent_registry(),
            self.client.svc,
            proposals=self.client.proposals,
            exposure=self.client.exposure,
        )
        return client.call(name, body, CallerContext.mcp(agent.label, agent_id=str(agent.id)))

    async def claim(self, request: Request) -> Response:
        agent: Agent = request.scope["oaken"]["principal"]
        req = parse(models.ClaimRequest, await read_json(request))
        result = await self._agent_call(agent, "claim_job", req.model_dump(exclude_none=True))
        job = result.output.job
        lease = None if job is None else models.JobLease(**job.model_dump())
        return _json(models.ClaimResponse(job=lease).model_dump(mode="json"))

    async def heartbeat(self, request: Request) -> Response:
        agent: Agent = request.scope["oaken"]["principal"]
        owner, cancel = await self.client.svc.queue.heartbeat(
            JobId(request.path_params["job_id"]), agent.worker
        )
        if not owner:
            raise Conflict("lease_lost: this agent no longer holds the job")
        return _json(models.HeartbeatResponse(cancel_requested=cancel).model_dump())

    async def complete(self, request: Request) -> Response:
        agent: Agent = request.scope["oaken"]["principal"]
        req = parse(models.CompleteRequest, await read_json(request))
        body = {"job_id": request.path_params["job_id"], **req.model_dump()}
        result = await self._agent_call(agent, "complete_job", body)
        return _json(models.CompleteResponse(**result.output.model_dump()).model_dump())

    async def fail(self, request: Request) -> Response:
        agent: Agent = request.scope["oaken"]["principal"]
        req = parse(models.FailRequest, await read_json(request))
        body = {"job_id": request.path_params["job_id"], **req.model_dump()}
        result = await self._agent_call(agent, "fail_job", body)
        return _json(models.FailResponse(status=result.output.status).model_dump())
