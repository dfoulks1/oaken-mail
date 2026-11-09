# SPDX-License-Identifier: LicenseRef-Proprietary
"""The WebUI Quart app: the static UI, ``/api/action/<name>`` (the action map), and, while the
extracted UI still calls core directly, a ``/v1/*`` reverse proxy.

The browser only talks to this server. It holds the API token and adds it (and
``X-Oaken-Client: ui``) on the way to core, so the token never reaches the browser. Core is
resolved per request, so the server can start before core and follow a core that restarts.
Everything is loopback.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx2
from quart import Quart, Response, abort, redirect, request, send_from_directory

from oaken_core.cli.client import ApiClient
from oaken_core.cli.discovery import Endpoint, discover
from oaken_core.cli.exits import CliError, CoreUnavailable
from oaken_core.cli.http import HttpApiClient
from oaken_webui import __version__
from oaken_webui_core import ui_ops
from oaken_webui_core.actions import UnknownAction, dispatch

UI_CLIENT = "ui"
_DEMO_ATTR = re.compile(rb"\s+data-oaken-demo\b")
_ASSET_REF = re.compile(rb'(src|href)="(assets/[^"?]+\.(?:js|css))"')

# Bootstrap and icons come from jsDelivr, fonts from Google; no remote images can load. Tighten to
# 'self' once those are vendored under assets/.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com https://cdn.jsdelivr.net; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

_HOP_BY_HOP = frozenset(
    {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
     "transfer-encoding", "upgrade", "host", "content-length"}
)  # fmt: skip
# What only a browser sends: core would take the page's Origin for a cross-origin caller. The same
# checks are made here instead (``_refusal``).
_BROWSER_ONLY = frozenset(
    {"origin", "referer", "cookie", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest",
     "sec-fetch-user"}
)  # fmt: skip
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "[::1]"})

ClientFactory = Callable[[], ApiClient]


def ui_dir() -> Path:
    """The packaged UI if present, else the source checkout's ``oakenmail-templates``."""
    packaged = Path(__file__).parent / "ui"
    return packaged if packaged.is_dir() else Path(__file__).resolve().parents[2] / "oakenmail-templates"


def _json(body: dict[str, object], status: int) -> Response:
    return Response(__import__("json").dumps(body), status=status, content_type="application/json")


def _refusal() -> Response | None:
    """Refuse a request from a page this server did not serve: the server holds the API token."""
    host = request.headers.get("host", "").lower()
    if host.rpartition(":")[0] not in _LOOPBACK_NAMES and host not in _LOOPBACK_NAMES:
        return _json({"code": "bad_host", "message": "this host name is not allowed"}, 400)
    if request.method in _SAFE_METHODS:
        return None
    origin = request.headers.get("origin")
    if (origin is not None and origin != f"http://{host}") or (
        request.headers.get("sec-fetch-site") == "cross-site"
    ):
        return _json({"code": "bad_origin", "message": "cross-origin requests are not allowed"}, 403)
    return None


def build_app(
    *,
    endpoint: Endpoint | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
    static_dir: Path | None = None,
    client_factory: ClientFactory | None = None,
) -> Quart:
    """``endpoint`` fixes the core target (dev override); ``transport`` and ``client_factory`` let
    tests stand in for core; ``static_dir`` overrides where the UI is read."""
    app = Quart(__name__, static_folder=None)
    root = static_dir or ui_dir()

    def endpoint_now() -> Endpoint:
        return endpoint or discover()

    def make_client() -> ApiClient:
        return client_factory() if client_factory else HttpApiClient(endpoint_now(), transport=transport)

    @app.before_request
    async def guard() -> Response | None:
        return _refusal()

    @app.errorhandler(CoreUnavailable)
    async def _core_down(exc: CoreUnavailable) -> Response:
        return _json({"code": "core_unavailable", "message": str(exc)}, 503)

    @app.errorhandler(CliError)
    async def _cli_error(exc: CliError) -> Response:
        return _json({"code": "core_error", "message": str(exc)}, 502)

    @app.post("/api/action/<name>")
    async def action(name: str) -> Response:
        body = await request.get_json(silent=True) or {}
        try:
            async with make_client() as client:
                result = await dispatch(
                    client, name, body.get("input"), confirmation_token=body.get("confirmation_token")
                )
        except UnknownAction:
            return _json({"code": "unknown_action", "message": name}, 404)
        if result.proposal is not None:
            p = result.proposal
            return _json(
                {"status": "confirmation_required", "request_id": result.request_id,
                 "proposal": {"operation": p.operation, "summary": p.summary, "changes": p.changes,
                              "total_changes": p.total_changes, "confirmation_token": p.confirmation_token}},
                202,
            )  # fmt: skip
        return _json({"status": "ok", "request_id": result.request_id, "output": result.output}, 200)

    @app.post("/v1/operations/<name>")
    async def ui_operation(name: str) -> Response:
        body = await request.get_json(silent=True) or {}
        try:
            async with make_client() as client:
                output = await ui_ops.run(client, name, body.get("input"))
        except ui_ops.NotAvailable:
            return _json({"code": "not_available", "message": f"{name} is not available in this build yet"}, 501)
        return _json({"status": "ok", "request_id": None, "output": output, "proposal": None}, 200)

    @app.route("/v1/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def proxy(path: str) -> Response:
        ep = endpoint_now()
        client = httpx2.AsyncClient(
            base_url=ep.base_url, transport=transport, timeout=httpx2.Timeout(60.0, read=None)
        )
        headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in _HOP_BY_HOP | _BROWSER_ONLY | {"authorization", "x-oaken-client"}  # set below; a copy under another casing would send both
        }
        headers["authorization"] = f"Bearer {ep.token}"
        headers["x-oaken-client"] = UI_CLIENT  # the UI caller class, never raised to mcp
        target = f"/v1/{path}" + (f"?{request.query_string.decode()}" if request.query_string else "")
        req = client.build_request(
            request.method, target, headers=headers, content=await request.get_data()
        )
        try:
            upstream = await client.send(req, stream=True)
        except (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.RemoteProtocolError) as exc:
            await client.aclose()
            return _json({"code": "core_unavailable", "message": str(exc)}, 503)

        async def stream() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        out = {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP}
        return Response(stream(), status=upstream.status_code, headers=out)

    @app.get("/login.html")
    async def login() -> Response:
        return redirect("/", 302)  # loopback, single user: the server holds the token

    @app.get("/", defaults={"path": "index.html"})
    @app.get("/<path:path>")
    async def ui(path: str) -> Response:
        if not root.is_dir():
            abort(404)
        if path.endswith("/"):
            path += "index.html"
        if path.endswith(".html"):
            file = (root / path).resolve()
            if not file.is_relative_to(root.resolve()) or not file.is_file():
                abort(404)
            html = _DEMO_ATTR.sub(b"", file.read_bytes())  # live UI calls the API, not demo fixtures
            html = _ASSET_REF.sub(rb'\1="\2?v=' + __version__.encode() + b'"', html)
            return Response(
                html, content_type="text/html",
                headers={"cache-control": "no-store", "content-security-policy": _CSP},
            )  # fmt: skip
        resp = await send_from_directory(root, path)
        resp.headers["cache-control"] = "no-cache"  # revalidate: never run a stale script
        return resp

    return app
