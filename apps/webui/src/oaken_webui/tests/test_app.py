# SPDX-License-Identifier: Apache-2.0
"""Tests for the web UI server: static UI and the ``/v1`` reverse proxy.

No sockets and no real core. A *fake core* ASGI app records what the proxy forwards, and the whole
web UI app is driven through an in-memory ``httpx2.ASGITransport``. The proxy reaches the fake core
through a second ASGITransport, so nothing leaves the process.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import httpx2
import pytest

from oaken_cli.discovery import Endpoint
from oaken_webui.app import build_app


class FakeCore:
    """A minimal ASGI app standing in for the core daemon. Records the last request it saw and
    answers a few ``/v1`` routes, including a streaming one."""

    def __init__(self) -> None:
        self.seen: dict[str, Any] = {}

    async def __call__(self, scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        assert scope["type"] == "http"  # noqa: S101
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        headers = {k.decode(): v.decode() for k, v in scope["headers"]}
        self.seen = {
            "method": scope["method"],
            "path": scope["path"],
            "query": scope["query_string"].decode(),
            "headers": headers,
            "body": body,
        }
        path = scope["path"]
        if path == "/v1/events":
            await _send(send, 200, b"data: hello\n\n", content_type="text/event-stream")
        elif path == "/v1/operations/search":
            await _send(send, 200, b'{"status":"ok","output":{"items":[]}}')
        else:
            await _send(send, 200, b'{"status":"ok"}')


async def _send(
    send: Any, status: int, body: bytes, content_type: str = "application/json"
) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", content_type.encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _webui_client(core: FakeCore | None, *, static_dir: Path | None = None) -> httpx2.AsyncClient:
    """A client wired to the web UI app. The proxy's upstream transport points at ``core``
    (an ASGITransport), and the endpoint is fixed so discovery is not used in tests."""
    transport = httpx2.ASGITransport(app=core) if core is not None else None
    endpoint = Endpoint(base_url="http://127.0.0.1:0", token="test-token")
    app = build_app(endpoint=endpoint, transport=transport, static_dir=static_dir)
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1:47813"
    )


@pytest.fixture
def ui_tree(tmp_path: Path) -> Path:
    root = tmp_path / "ui"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(
        '<!doctype html><html data-oaken-demo data-oaken-theme="oak-light">'
        "<body>inbox</body></html>"
    )
    (root / "assets" / "api.js").write_text("// app js\n")
    return root


async def test_static_index_is_served_with_the_demo_marker_stripped(ui_tree: Path) -> None:
    async with _webui_client(FakeCore(), static_dir=ui_tree) as c:
        r = await c.get("/")
        assert r.status_code == 200
        assert "inbox" in r.text
        # The live UI must run against the API, not the demo fixtures.
        assert "data-oaken-demo" not in r.text
        # Other attributes on the same tag survive.
        assert "data-oaken-theme" in r.text


async def test_html_is_no_store_and_carries_a_csp(ui_tree: Path) -> None:
    async with _webui_client(FakeCore(), static_dir=ui_tree) as c:
        r = await c.get("/")
        assert r.headers["cache-control"] == "no-store"
        csp = r.headers["content-security-policy"]
        assert "default-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "img-src 'self' data:" in csp  # no remote images
        assert "connect-src 'self'" in csp
        # Bootstrap Icons is a webfont served from jsDelivr: without this every icon is blank.
        font_src = next(d for d in csp.split("; ") if d.startswith("font-src"))
        assert "https://cdn.jsdelivr.net" in font_src and "https://fonts.gstatic.com" in font_src


async def test_local_asset_references_are_version_stamped(ui_tree: Path) -> None:
    # Give the index a real local asset reference and a CDN one.
    (ui_tree / "index.html").write_text(
        "<!doctype html><html data-oaken-demo><head>"
        '<link href="https://cdn.jsdelivr.net/npm/bootstrap/x.css" rel="stylesheet">'
        '</head><body>inbox<script src="assets/oaken.js"></script></body></html>'
    )
    from oaken import __version__

    async with _webui_client(FakeCore(), static_dir=ui_tree) as c:
        r = await c.get("/")
        assert f"assets/oaken.js?v={__version__}" in r.text  # local asset stamped
        assert "cdn.jsdelivr.net/npm/bootstrap/x.css?v=" not in r.text


async def test_assets_are_revalidated_not_served_from_a_stale_browser_cache(
    ui_tree: Path,
) -> None:
    (ui_tree / "assets").mkdir(exist_ok=True)
    (ui_tree / "assets" / "oaken.js").write_text("// js")
    async with _webui_client(FakeCore(), static_dir=ui_tree) as c:
        first = await c.get("/assets/oaken.js")
        assert first.status_code == 200 and first.headers["cache-control"] == "no-cache"
        again = await c.get("/assets/oaken.js", headers={"if-none-match": first.headers["etag"]})
        assert again.status_code == 304  # revalidating is still cheap

        missing = await c.get("/assets/nope.js")
        assert missing.status_code == 404 and "cache-control" not in missing.headers


async def test_login_html_redirects_to_root(ui_tree: Path) -> None:
    async with _webui_client(FakeCore(), static_dir=ui_tree) as c:
        r = await c.get("/login.html")
        assert r.status_code == 302 and r.headers["location"] == "/"


async def test_non_html_assets_are_served_unchanged(ui_tree: Path) -> None:
    async with _webui_client(FakeCore(), static_dir=ui_tree) as c:
        r = await c.get("/assets/api.js")
        assert r.status_code == 200 and r.text == "// app js\n"


async def test_v1_is_proxied_with_the_token_and_the_ui_caller_class(ui_tree: Path) -> None:
    core = FakeCore()
    async with _webui_client(core, static_dir=ui_tree) as c:
        r = await c.post("/v1/operations/search", json={"input": {"query": "x"}})
        assert r.status_code == 200
        assert r.json()["output"] == {"items": []}
    # The browser never sent a token; the proxy added it and fixed the caller class to "ui".
    assert core.seen["headers"]["authorization"] == "Bearer test-token"
    assert core.seen["headers"]["x-oaken-client"] == "ui"
    assert core.seen["method"] == "POST"
    assert b'"query"' in core.seen["body"]


async def test_a_same_origin_post_from_the_browser_works_and_page_headers_are_not_forwarded(
    ui_tree: Path,
) -> None:
    core = FakeCore()
    page = {
        "origin": "http://127.0.0.1:47813",
        "referer": "http://127.0.0.1:47813/",
        "sec-fetch-site": "same-origin",
        "cookie": "a=b",
    }
    async with _webui_client(core, static_dir=ui_tree) as c:
        r = await c.post("/v1/operations/search", json={"input": {}}, headers=page)
        assert r.status_code == 200
    # Core must not see the page's origin: it would take the proxy for a cross-origin caller.
    assert not {"origin", "referer", "cookie", "sec-fetch-site"} & set(core.seen["headers"])


async def test_a_cross_origin_post_is_refused_before_it_reaches_core(ui_tree: Path) -> None:
    core = FakeCore()
    async with _webui_client(core, static_dir=ui_tree) as c:
        for headers in (
            {"origin": "https://evil.example"},
            {"sec-fetch-site": "cross-site"},
        ):
            r = await c.post("/v1/operations/search", json={"input": {}}, headers=headers)
            assert r.status_code == 403 and r.json()["code"] == "bad_origin"
    assert core.seen == {}


async def test_a_non_loopback_host_name_is_refused(ui_tree: Path) -> None:
    core = FakeCore()
    async with _webui_client(core, static_dir=ui_tree) as c:
        r = await c.get("/v1/operations/search", headers={"host": "rebind.example"})
        assert r.status_code == 400 and r.json()["code"] == "bad_host"
    assert core.seen == {}


async def test_query_string_is_forwarded(ui_tree: Path) -> None:
    core = FakeCore()
    async with _webui_client(core, static_dir=ui_tree) as c:
        await c.get("/v1/operations/search?limit=5")
    assert core.seen["query"] == "limit=5"


async def test_sse_stream_is_proxied(ui_tree: Path) -> None:
    core = FakeCore()
    async with _webui_client(core, static_dir=ui_tree) as c:
        r = await c.get("/v1/events")
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        assert "data: hello" in r.text


async def test_core_down_is_reported_as_503(ui_tree: Path) -> None:
    # A fixed endpoint at a closed loopback port: the proxy's connection attempt fails and must be
    # turned into 503 core_unavailable rather than a crash. Deterministic, no reliance on whether a
    # real core happens to be running in the environment.
    endpoint = Endpoint(base_url="http://127.0.0.1:1", token="test-token")
    app = build_app(endpoint=endpoint, static_dir=ui_tree)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1:47813"
    ) as c:
        r = await c.post("/v1/operations/search", json={"input": {}})
        assert r.status_code == 503
        assert r.json()["code"] == "core_unavailable"
