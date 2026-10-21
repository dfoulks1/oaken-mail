# SPDX-License-Identifier: Apache-2.0
"""The web UI ASGI app: static files at ``/`` and a reverse proxy for ``/v1/*``.

Design
------
The browser only ever talks to this server. Two concerns, nothing else:

* **Static UI.** The HTML/CSS/JS live next to this package (packaged as ``oaken_webui/ui``) or, in
  a source checkout, in ``apps/webui/oakenmail-templates``. HTML is served with the
  ``data-oaken-demo`` marker stripped, so the live UI calls the API instead of the demo fixtures
  (the marker is only for the published mockups under ``docs/``).

* **API proxy.** ``/v1/*`` is forwarded verbatim to the core daemon on loopback. This server holds
  the API token and adds it (and ``X-Oaken-Client: ui``) on the way through, so the token never
  reaches the browser. Server-sent events (``/v1/events``) are streamed, not buffered.

Core is not contacted at import time: the proxy resolves the endpoint per request (so the UI
server can start before core, and survive a core restart that changes the port). Everything is
loopback; this server never binds off-host.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
import httpx2
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import BaseRoute, Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from oaken_cli.discovery import Endpoint, discover
from oaken_cli.exits import CoreUnavailable

UI_CLIENT = "ui"
# The demo marker: only the published mockups use it. Served live, the UI must be in API mode.
_DEMO_ATTR = re.compile(rb"\s+data-oaken-demo\b")
# Local asset references in served HTML (src=/href="assets/...js|css"), for cache-busting. Only
# relative ``assets/`` paths are stamped; absolute URLs and CDN links are left alone.
_ASSET_REF = re.compile(rb'(src|href)="(assets/[^"?]+\.(?:js|css))"')


def _asset_version() -> bytes:
    from oaken import __version__

    return __version__.encode()


def _version_assets(html: bytes) -> bytes:
    """Append ``?v=<version>`` to local ``assets/*.js``/``*.css`` references so a browser fetches
    the current file after an upgrade instead of a cached one (21b D2)."""
    ver = _asset_version()
    return _ASSET_REF.sub(rb'\1="\2?v=' + ver + b'"', html)


# The Content-Security-Policy for served HTML (21b D3). The templates load Bootstrap and Bootstrap
# Icons from jsDelivr and fonts from Google Fonts; everything else is same-origin. No remote images
# can load (``img-src`` is self + data: only), which is the enforcement behind "Load remote images"
# being hidden in the UI. ``connect-src 'self'`` keeps XHR/EventSource on the proxy; the page cannot
# be framed. If the CDN assets are later vendored under assets/ (21b Q7), tighten this to 'self'.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


def _html_headers() -> dict[str, str]:
    return {"cache-control": "no-store", "content-security-policy": _CSP}


# Hop-by-hop headers are not forwarded across a proxy (RFC 7230 §6.1).
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)


# What only a browser sends. The proxy is a trusted, server-side client of core, so these are not
# forwarded: core's own guard would take the page's Origin for a cross-origin caller and refuse
# every POST. The same checks are made here instead, against the UI's own origin (``_same_origin``).
_BROWSER_ONLY = frozenset(
    {
        "origin",
        "referer",
        "cookie",
        "sec-fetch-site",
        "sec-fetch-mode",
        "sec-fetch-dest",
        "sec-fetch-user",
    }
)
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "[::1]"})


def _bad_request(request: Request) -> JSONResponse | None:
    """Refuse a request that is not from a page this server served. The proxy holds the API token,
    so a page on another site (or a rebinding host name) must not be able to use it."""
    host = request.headers.get("host", "").lower()
    if host.rpartition(":")[0] not in _LOOPBACK_NAMES and host not in _LOOPBACK_NAMES:
        return JSONResponse({"code": "bad_host", "message": "this host name is not allowed"}, 400)
    if request.method in _SAFE_METHODS:
        return None
    origin = request.headers.get("origin")
    if (origin is not None and origin != f"http://{host}") or (
        request.headers.get("sec-fetch-site") == "cross-site"
    ):
        return JSONResponse(
            {"code": "bad_origin", "message": "cross-origin requests are not allowed"}, 403
        )
    return None


def ui_dir() -> Path:
    """Where the browser UI lives: the packaged copy if present, else the source templates."""
    packaged = Path(__file__).parent / "ui"
    if packaged.is_dir():
        return packaged
    # Source checkout: apps/webui/oakenmail-templates, three parents up from this file
    # (src/oaken_webui/app.py -> src/oaken_webui -> src -> apps/webui).
    return Path(__file__).resolve().parents[2] / "oakenmail-templates"


class _UiFiles(StaticFiles):
    """StaticFiles that strips ``data-oaken-demo`` from HTML as it is served, so the live UI runs
    against the API instead of the demo fixtures. It also sends HTML with ``Cache-Control:
    no-store`` and stamps local ``assets/*.js``/``*.css`` references with a ``?v=<version>`` query,
    so a browser never runs a stale shell or a stale script after an upgrade (21b D2). Non-HTML
    files and misses fall through to the normal StaticFiles handling (range requests, 404s)."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        # Resolve "." (a directory request such as "/") to its index before deciding.
        lookup = "index.html" if path == "." else path
        full, stat = self.lookup_path(lookup)
        is_html = bool(full) and lookup.endswith(".html") and stat is not None
        if is_html:
            raw = await anyio.to_thread.run_sync(Path(full).read_bytes)
            body = _version_assets(_DEMO_ATTR.sub(b"", raw))
            return Response(body, media_type="text/html", headers=_html_headers())
        return await super().get_response(path, scope)


class Proxy:
    """A reverse proxy for ``/v1/*`` to the core daemon, resolved per request.

    ``endpoint`` fixes the target (a dev override); without it the core is discovered from the
    runtime file on each request, so the proxy follows a core that restarts on a new port.
    """

    def __init__(
        self,
        *,
        endpoint: Endpoint | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._fixed = endpoint
        self._transport = transport

    def _endpoint(self) -> Endpoint:
        return self._fixed or discover()

    def _client(self, base_url: str) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            base_url=base_url, transport=self._transport, timeout=httpx2.Timeout(60.0, read=None)
        )

    def _headers(self, request: Request, token: str) -> dict[str, str]:
        skip = _HOP_BY_HOP | _BROWSER_ONLY
        out = {k: v for k, v in request.headers.items() if k.lower() not in skip}
        out["authorization"] = f"Bearer {token}"
        out["x-oaken-client"] = UI_CLIENT  # the UI caller class, never raised to mcp
        return out

    async def handle(self, request: Request) -> Response:
        if (refused := _bad_request(request)) is not None:
            return refused
        try:
            endpoint = self._endpoint()
        except CoreUnavailable as exc:
            return JSONResponse({"code": "core_unavailable", "message": str(exc)}, status_code=503)
        path = request.url.path
        if request.url.query:
            path = f"{path}?{request.url.query}"
        body = await request.body()
        headers = self._headers(request, endpoint.token)
        client = self._client(endpoint.base_url)
        req = client.build_request(request.method, path, headers=headers, content=body)
        try:
            upstream = await client.send(req, stream=True)
        except (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.RemoteProtocolError) as exc:
            await client.aclose()
            return JSONResponse({"code": "core_unavailable", "message": str(exc)}, status_code=503)

        async def stream() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP}
        return StreamingResponse(
            stream(),
            status_code=upstream.status_code,
            headers=resp_headers,
            media_type=upstream.headers.get("content-type"),
        )


def build_app(
    *,
    endpoint: Endpoint | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
    static_dir: Path | None = None,
) -> Starlette:
    """The web UI ASGI app. ``endpoint``/``transport`` are for a dev override and for tests
    (an ASGI transport pointed at a fake core); ``static_dir`` overrides where the UI is read."""
    proxy = Proxy(endpoint=endpoint, transport=transport)
    root = static_dir or ui_dir()

    async def _login_redirect(_request: Request) -> Response:
        # There is no login in a loopback single-user app (21b A2/D1): the proxy holds the token.
        return RedirectResponse("/", status_code=302)

    routes: list[BaseRoute] = [
        Route("/v1/{path:path}", proxy.handle, methods=["GET", "POST", "PUT", "DELETE", "PATCH"]),
        Route("/login.html", _login_redirect, methods=["GET"]),
    ]
    if root.is_dir():
        routes.append(Mount("/", app=_UiFiles(directory=str(root), html=True), name="ui"))
    return Starlette(routes=routes)
