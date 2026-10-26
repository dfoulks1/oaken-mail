# SPDX-License-Identifier: Apache-2.0
"""The local HTTP API: a thin skin over the operation registry.

There is no route per operation. ``POST /v1/operations/{name}`` looks the name up in the registry
and calls ``CoreClient``, so the gate, the limits and the audit row are the ones prompt 13 built.
The routes here only authenticate (``guard``), pick the caller class, and turn errors into
``{"code", "message"}``. See ADR 0005.
"""

import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import BaseRoute, Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from oaken.api import models
from oaken.api.errors import INTERNAL, ApiError, body, status_for
from oaken.api.guard import Guard, Policy, Surface
from oaken.api.openapi import build_api_openapi
from oaken.api.ratelimit import RateLimiter
from oaken.api.ring import EventRing
from oaken.api.runtime import same_secret
from oaken.api.stats import STATS_JS, STATS_LOGO, StatsCollector, render_stats
from oaken.api.welcome import CONFIG_JS, JOBS_JS, WELCOME_JS, render_welcome, welcome_context
from oaken.core.agents import TOKEN_PREFIX, Agent
from oaken.core.errors import Conflict, InvalidInput, OakenError, PermissionDenied
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
            log.error("request failed", path=request.url.path, error=type(exc).__name__)
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
        self._detach: Callable[[], None] | None = None
        self._port: int | None = None
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
            Route("/v1/agent/jobs/claim", safe(self.claim), methods=["POST"]),
            Route("/v1/agent/jobs/{job_id}/heartbeat", safe(self.heartbeat), methods=["POST"]),
            Route("/v1/agent/jobs/{job_id}/complete", safe(self.complete), methods=["POST"]),
            Route("/v1/agent/jobs/{job_id}/fail", safe(self.fail), methods=["POST"]),
            Route("/stats", safe(self.stats), methods=["GET"]),
            Route("/stats.json", safe(self.stats_json), methods=["GET"]),
            Route("/stats.js", safe(self.stats_js), methods=["GET"]),
            Route("/stats-logo.png", safe(self.stats_logo), methods=["GET"]),
            Route("/config", safe(self.config_editor), methods=["GET"]),
            Route("/getting_started", safe(self.getting_started), methods=["GET"]),
            Route("/config.js", safe(self.config_js), methods=["GET"]),
            Route("/welcome.js", safe(self.welcome_js), methods=["GET"]),
            Route("/config/get", safe(self.config_get), methods=["GET"]),
            Route("/config/test", safe(self.config_test), methods=["POST"]),
            Route("/config/save", safe(self.config_save_route), methods=["POST"]),
            Route("/config/reload", safe(self.config_reload_route), methods=["POST"]),
            Route("/jobs", safe(self.jobs_page), methods=["GET"]),
            Route("/jobs.js", safe(self.jobs_js), methods=["GET"]),
            Route("/jobs/list", safe(self.jobs_list), methods=["GET"]),
            Route("/jobs/pipeline", safe(self.jobs_pipeline), methods=["GET"]),
            Route("/jobs/pause", safe(self.jobs_pause), methods=["POST"]),
            Route("/jobs/resume", safe(self.jobs_resume), methods=["POST"]),
            Route("/jobs/limit", safe(self.jobs_limit), methods=["POST"]),
            Route("/jobs/retry-parked", safe(self.jobs_retry_parked), methods=["POST"]),
            Route("/jobs/cancel", safe(self.jobs_cancel), methods=["POST"]),
            Route("/jobs/retry", safe(self.jobs_retry), methods=["POST"]),
            Route("/plugins/enable", safe(self.plugins_enable), methods=["POST"]),
            Route("/plugins/disable", safe(self.plugins_disable), methods=["POST"]),
            Route("/plugins/reload", safe(self.plugins_reload), methods=["POST"]),
            Route("/stats/reset-usage", safe(self.stats_reset_usage), methods=["POST"]),
            Route("/stats/revoke-agent", safe(self.stats_revoke_agent), methods=["POST"]),
            Route("/stats/expose", safe(self.stats_expose), methods=["POST"]),
            Route("/stats/mcp-workspaces", safe(self.stats_mcp_workspaces), methods=["POST"]),
            Route("/stats/enricher-config", safe(self.stats_enricher_config), methods=["POST"]),
            Route("/stats/model", safe(self.stats_model_save), methods=["POST"]),
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

    async def placeholder(self, request: Request) -> Response:
        """Core's home page (``/``). Renders the friendly ``welcome.html`` with the version, the
        daemon's paths and port, and a short live status. If the template cannot be rendered (a
        trimmed build, say) it falls back to the plain text placeholder so ``/`` always answers.
        Loopback-only and unauthenticated like ``/stats``; version, paths and counts only."""
        try:
            status: dict[str, Any] = {}
            try:
                out = await self.client.call("status", {}, CallerContext.internal())
                if out.output is not None:
                    status = out.output.model_dump(mode="json")
            except OakenError:
                status = {}  # a status hiccup must not take the home page down
            html = render_welcome(
                welcome_context(version=self.client.svc.version, port=self._port, status=status)
            )
        except Exception:  # noqa: BLE001 - the home page must never 500; fall back to plain text
            log.error("welcome render failed; serving placeholder")
            return HTMLResponse(PLACEHOLDER)
        csp = (
            "default-src 'self'; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net "
            "https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com "
            "https://cdn.jsdelivr.net; img-src 'self' data:; "
            "script-src 'self' https://cdn.jsdelivr.net; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        return HTMLResponse(html, headers={"content-security-policy": csp})

    async def config_editor(self, request: Request) -> Response:
        """The core-served config.toml editor (``/config``): a textarea with Test, Save and Reload,
        backed by the ``config_*`` operations. Loopback-only like ``/stats``; the page holds no
        token (the browser calls ``/v1/operations/{name}`` through the same origin)."""
        from oaken.api.welcome import _env  # same templates dir, same autoescaped environment

        html = _env.get_template("config.html").render(
            version=self.client.svc.version,
            active="config",
            docs_url="https://dfoulks1.github.io/oaken-mail/",
        )
        csp = (
            "default-src 'self'; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net "
            "https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com "
            "https://cdn.jsdelivr.net; img-src 'self' data:; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        return HTMLResponse(html, headers={"content-security-policy": csp})

    # The config editor's data endpoints: loopback-only (the Host guard), token-free like
    # /stats.json. Each delegates to the real config_* operation as the internal caller, so the
    # operations stay the single source of truth and every save/reload still lands in the audit log.
    async def _config_op(self, name: str, payload: dict[str, Any]) -> Response:
        # confirm=True: these routes are driven by a person clicking a button on the loopback
        # editor page, the same intent the CLI/UI expresses. Without it a mutate op (save, reload)
        # returns a proposal and changes nothing.
        out = await self.client.call(name, payload, CallerContext.internal(), confirm=True)
        data = out.output.model_dump(mode="json") if out.output is not None else {}
        return _json(data)

    async def config_get(self, request: Request) -> Response:
        return await self._config_op("config_get", {})

    async def config_js(self, request: Request) -> Response:
        """The config editor's one same-origin script, vendored with the package."""
        return Response(
            CONFIG_JS,
            media_type="application/javascript",
            headers={"cache-control": "no-store"},
        )

    async def welcome_js(self, request: Request) -> Response:
        """The welcome page's one same-origin script (theme toggle), vendored with the package."""
        return Response(
            WELCOME_JS,
            media_type="application/javascript",
            headers={"cache-control": "no-store"},
        )

    async def config_test(self, request: Request) -> Response:
        body = await read_json(request)
        return await self._config_op("config_validate", {"text": body.get("text", "")})

    async def config_save_route(self, request: Request) -> Response:
        body = await read_json(request)
        return await self._config_op("config_save", {"text": body.get("text", "")})

    async def config_reload_route(self, request: Request) -> Response:
        return await self._config_op("config_reload", {})

    # -- the jobs & pipeline console (/jobs) --------------------------------------------------
    # A core debugging surface: inspect the durable queue and control the ingest pipeline. Like
    # /config it is loopback-only (the Host guard) and token-free; the mutating routes delegate to
    # the real mutate operations with confirm=True, so every action still lands in the audit log.
    async def jobs_page(self, request: Request) -> Response:
        from oaken.api.welcome import _env

        html = _env.get_template("jobs.html").render(
            version=self.client.svc.version,
            active="jobs",
            docs_url="https://dfoulks1.github.io/oaken-mail/",
        )
        csp = (
            "default-src 'self'; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net "
            "https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com "
            "https://cdn.jsdelivr.net; img-src 'self' data:; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        return HTMLResponse(html, headers={"content-security-policy": csp})

    async def jobs_js(self, request: Request) -> Response:
        return Response(
            JOBS_JS, media_type="application/javascript", headers={"cache-control": "no-store"}
        )

    async def getting_started(self, request: Request) -> Response:
        """A short, in-app walkthrough of the config.toml settings, served by core at
        /getting_started. Loopback-only like the other core pages."""
        from oaken.api.welcome import _env

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
        html = _env.get_template("getting_started.html").render(
            version=self.client.svc.version,
            providers=providers,
            active="",
            docs_url="https://dfoulks1.github.io/oaken-mail/",
        )
        csp = (
            "default-src 'self'; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net "
            "https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com "
            "https://cdn.jsdelivr.net; img-src 'self' data:; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        return HTMLResponse(html, headers={"content-security-policy": csp})

    async def jobs_list(self, request: Request) -> Response:
        status = request.query_params.get("status") or None
        payload: dict[str, Any] = {"limit": 100}
        if status:
            payload["status"] = status
        return await self._config_op("list_jobs", payload)

    async def jobs_pipeline(self, request: Request) -> Response:
        return await self._config_op("pipeline_status", {})

    # -- plugin control on the stats page ----------------------------------------------------
    # The Plugins tab's switch and Reload button. Loopback-only and token-free like the rest of the
    # console; each delegates to core's own plugin_enable / plugin_disable / plugin_reload with
    # confirm=True, so the audit log records it and the CLI (`oaken plugins ...`) does the same.
    async def _plugin_control(self, request: Request, op: str) -> Response:
        body = await read_json(request)
        name = body.get("name")
        if not isinstance(name, str) or not name:
            raise InvalidInput("name the plugin")
        return await self._config_op(op, {"name": name})

    async def plugins_enable(self, request: Request) -> Response:
        return await self._plugin_control(request, "plugin_enable")

    async def plugins_disable(self, request: Request) -> Response:
        return await self._plugin_control(request, "plugin_disable")

    async def plugins_reload(self, request: Request) -> Response:
        return await self._plugin_control(request, "plugin_reload")

    # -- the stats page's mutating buttons (loopback, audited via _config_op) --------------------
    async def stats_reset_usage(self, request: Request) -> Response:
        return await self._config_op("reset_usage", {})

    async def stats_revoke_agent(self, request: Request) -> Response:
        body = await read_json(request)
        return await self._config_op("revoke_agent", {"agent_id": body["agent_id"]})

    async def stats_expose(self, request: Request) -> Response:
        body = await read_json(request)
        return await self._config_op(
            "set_operation_exposure",
            {"scope": "operation", "name": body["name"], "mcp": bool(body["mcp"])},
        )

    async def stats_mcp_workspaces(self, request: Request) -> Response:
        # Save `[mcp] workspaces` through the same settings operation the CLI uses, then reload so
        # the running MCP server applies it (core re-reads settings; no restart).
        body = await read_json(request)
        names = body.get("workspaces")
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise InvalidInput("workspaces must be a list of workspace names")
        saved = await self.client.call(
            "set_settings",
            {"section": "mcp", "values": {"workspaces": names}},
            CallerContext.internal(),
            confirm=True,
        )
        result = saved.output
        if result is None or not getattr(result, "ok", False):
            return _json(result.model_dump(mode="json") if result is not None else {"ok": False})
        return await self._config_op("config_reload", {})

    async def stats_enricher_config(self, request: Request) -> Response:
        body = await read_json(request)
        return await self._config_op(
            "set_plugin_config",
            {"plugin": body["plugin"], "config": body.get("config", {})},
        )

    async def stats_model_save(self, request: Request) -> Response:
        # Save the backend's model, then reload: the backend pins its model at start(), so a plain
        # config write alone would not take effect until the next restart.
        body = await read_json(request)
        saved = await self.client.call(
            "set_plugin_config",
            {"plugin": body["plugin"], "config": {"model": body["model"]}},
            CallerContext.internal(),
            confirm=True,
        )
        result = saved.output
        if result is None or not getattr(result, "ok", False):
            return _json(result.model_dump(mode="json") if result is not None else {"ok": False})
        return await self._config_op("config_reload", {})

    async def jobs_pause(self, request: Request) -> Response:
        body = await read_json(request)
        payload: dict[str, Any] = {"why": "paused from the jobs console"}
        if body.get("stage"):
            payload["stage"] = body["stage"]
        return await self._config_op("pause_backfill", payload)

    async def jobs_resume(self, request: Request) -> Response:
        body = await read_json(request)
        return await self._config_op(
            "resume_backfill", {"stage": body["stage"]} if body.get("stage") else {}
        )

    async def jobs_limit(self, request: Request) -> Response:
        body = await read_json(request)
        return await self._config_op(
            "set_pipeline_limits",
            {"stage": body.get("stage"), "concurrency": body.get("concurrency")},
        )

    async def jobs_retry_parked(self, request: Request) -> Response:
        return await self._config_op("retry_parked", {"all": True})

    async def jobs_cancel(self, request: Request) -> Response:
        body = await read_json(request)
        return await self._config_op("cancel_job", {"job_id": body.get("job_id", "")})

    async def jobs_retry(self, request: Request) -> Response:
        body = await read_json(request)
        return await self._config_op("retry_job", {"job_id": body.get("job_id", "")})

    def _stats_collector(self) -> StatsCollector:
        return StatsCollector(
            self.client.svc,
            clock=self.client.svc.clock,
            started_at=self._started_at,
            client=self.client,
        )

    async def stats(self, request: Request) -> Response:
        """The read-only status page (ADR 0013). Loopback-only (the Host guard) and
        unauthenticated like ``/v1/health``; counts and health only, never mail content."""
        data = await self._stats_collector().collect()
        data["active"] = "stats"
        data["docs_url"] = "https://dfoulks1.github.io/oaken-mail/"
        html = render_stats(data)
        # Bootstrap (CDN) for a look consistent with / and /config, plus the one same-origin
        # script (stats.js) for live in-place updates. The guard only adds a CSP when the
        # response lacks one, so this header stands.
        csp = (
            "default-src 'self'; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net "
            "https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com "
            "https://cdn.jsdelivr.net; img-src 'self' data:; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        return HTMLResponse(html, headers={"content-security-policy": csp})

    async def stats_json(self, request: Request) -> Response:
        """The same stats as the page, as JSON, for the page's in-place refresh (stats.js).
        Loopback-only and unauthenticated like `/stats`; counts and health only."""
        data = await self._stats_collector().collect()
        return _json(data)

    async def stats_js(self, request: Request) -> Response:
        """The one small, same-origin script for the stats page's in-place refresh. Vendored with
        the package, served here so the page's `script-src 'self'` allows exactly it and nothing
        else. The page works fully without it; this only updates numbers so the tab never jumps."""
        return Response(
            STATS_JS,
            media_type="application/javascript",
            headers={"cache-control": "no-store"},
        )

    async def stats_logo(self, request: Request) -> Response:
        """The Oaken Mail logo for the stats page, served same-origin from the packaged asset so
        the page stays offline (no CDN) under `img-src 'self'`."""
        return Response(
            STATS_LOGO,
            media_type="image/png",
            headers={"cache-control": "max-age=86400"},
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

    # -- the agent surface (a separate token; see ``claim`` and the swarm page) --------------------
    async def claim(self, request: Request) -> Response:
        agent: Agent = request.scope["oaken"]["principal"]
        req = parse(models.ClaimRequest, await read_json(request))
        kinds = sorted(set(req.kinds)) if req.kinds else sorted(agent.kinds)
        if not set(kinds) <= agent.kinds:
            raise PermissionDenied("an agent may claim only the job kinds it was given")
        queue = self.client.svc.queue
        job = await queue.claim(agent.worker, kinds, queue.settings.kind_limits, agent=agent)
        lease = None
        if job is not None:
            lease = models.JobLease(
                id=str(job.id),
                kind=job.kind,
                account_id=str(job.account_id) if job.account_id else None,
                payload=json.loads(job.payload_json),
                attempts=job.attempts,
                max_attempts=job.max_attempts,
                lease_expires_at=job.locked_until.isoformat() if job.locked_until else None,
            )
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
        done = await self.client.svc.queue.complete(
            JobId(request.path_params["job_id"]),
            agent.worker,
            req.result,
            produced_by=req.produced_by,
        )
        if not done:
            raise Conflict("lease_lost: this agent no longer holds the job")
        return _json(models.Ok().model_dump())

    async def fail(self, request: Request) -> Response:
        agent: Agent = request.scope["oaken"]["principal"]
        req = parse(models.FailRequest, await read_json(request))
        status = await self.client.svc.queue.fail(
            JobId(request.path_params["job_id"]), agent.worker, req.error, permanent=req.permanent
        )
        if status == "lost":
            raise Conflict("lease_lost: this agent no longer holds the job")
        return _json(models.FailResponse(status=status).model_dump())  # type: ignore[arg-type]
