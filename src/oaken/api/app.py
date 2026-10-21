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
from oaken.core.agents import TOKEN_PREFIX, Agent
from oaken.core.errors import Conflict, OakenError, PermissionDenied
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
        self, client: CoreClient, *, token: str, rate_now: Callable[[], float] | None = None
    ) -> None:
        self.client = client
        self._token = token
        svc = client.svc
        cfg = svc.settings.api
        self.cfg = cfg
        limiter = RateLimiter(cfg.rate_limit_per_minute, now=rate_now or time.monotonic)
        self.policy = Policy(dev_origin=cfg.dev_origin, limiter=limiter, public_csp=PUBLIC_CSP)
        agent_body = svc.settings.jobs.max_result_bytes + 64 * 1024
        self.policy.surfaces = [
            Surface("general", "/v1/", self._general, cfg.max_body_bytes, max_concurrent=64),
            Surface("agent", "/v1/agent/", self._agent, agent_body, max_concurrent=16),
        ]
        self.ring = EventRing(cfg.event_ring_size)
        self._detach: Callable[[], None] | None = None
        self.routes: list[BaseRoute] = self._routes()
        self.starlette = Starlette(
            routes=self.routes,
            exception_handlers={HTTPException: self._http_error},
        )
        self.app: ASGIApp = Guard(self.starlette, self.policy)

    # -- lifecycle ---------------------------------------------------------------------------
    def bind(self, port: int) -> None:
        """Allow the Host and Origin values for the port core really listens on."""
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
        The MCP server (prompt 18) mounts here; the surface, not a header, sets its class."""
        self.policy.surfaces.append(surface)
        base = surface.prefix.rstrip("/")
        # Both spellings, not a ``Mount``: a Mount does not match its own bare path, and a client
        # that posts to ``/mcp`` must not be redirected.
        self.starlette.router.routes[0:0] = [Route(base, app), Route(base + "/{rest:path}", app)]

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
        return HTMLResponse(PLACEHOLDER)

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
