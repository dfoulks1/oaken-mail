# SPDX-License-Identifier: Apache-2.0
"""``[api] mcp``: off, the MCP door does not exist; on, it is a real listener with its own token."""

import asyncio
import contextlib
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import httpx2
import pytest

from oaken.api.runtime import RuntimeFile, read_runtime
from oaken.core import paths
from oaken.core.clock import SystemClock
from oaken.core.settings import Settings
from oaken.daemon import Core, serve

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}


@contextlib.asynccontextmanager
async def running(home: Path) -> AsyncIterator[httpx2.AsyncClient]:
    runtime = paths.runtime_file()
    stopper, ready = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(
        serve(
            Core(Settings.load()),
            RuntimeFile(runtime, SystemClock(), "t"),
            stop=stopper,
            ready=ready,
            handle_signals=False,
        )
    )
    await asyncio.wait_for(ready.wait(), 30)
    info = read_runtime(runtime)
    assert info is not None
    try:
        async with httpx2.AsyncClient(base_url=f"http://127.0.0.1:{info['port']}") as http:
            yield http
    finally:
        stopper.set()
        await asyncio.wait_for(task, 30)


async def test_with_mcp_off_there_is_no_mcp_route_and_no_mcp_token(home: Path) -> None:
    async with running(home) as http:
        api = {"Authorization": f"Bearer {(home / 'config' / 'api_token').read_text().strip()}"}
        for headers in (HEADERS, {**HEADERS, **api}):
            assert (await http.post("/mcp", json=LIST, headers=headers)).status_code == 404
    assert not (home / "config" / "mcp_token").exists()


async def test_with_mcp_on_a_real_socket_serves_tools_to_the_mcp_token_only(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OAKEN_API__MCP", "true")
    async with running(home) as http:
        token_file = home / "config" / "mcp_token"
        mcp = {**HEADERS, "Authorization": f"Bearer {token_file.read_text().strip()}"}
        api = {
            **HEADERS,
            "Authorization": f"Bearer {(home / 'config' / 'api_token').read_text().strip()}",
        }
        listed = await http.post("/mcp", json=LIST, headers=mcp)
        assert listed.status_code == 200, listed.text
        names = {t["name"] for t in listed.json()["result"]["tools"]}
        assert {"search", "get_message", "mark_read"} <= names and "send_mail" not in names
        assert (await http.post("/mcp", json=LIST, headers=api)).status_code == 401
        assert (await http.post("/mcp", json=LIST, headers=HEADERS)).status_code == 401
        assert (await http.get("/v1/operations", headers=mcp)).status_code == 401
        assert token_file.read_text().strip() != (home / "config" / "api_token").read_text().strip()
        if sys.platform != "win32":
            assert token_file.stat().st_mode & 0o777 == 0o600
