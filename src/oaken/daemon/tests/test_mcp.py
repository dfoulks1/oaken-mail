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


async def test_with_the_swarm_on_an_agent_works_through_its_own_door(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OAKEN_API__MCP", "true")
    monkeypatch.setenv("OAKEN_SWARM__ENABLED", "true")
    monkeypatch.setenv("OAKEN_SWARM__ALLOWED_KINDS", '["enrich"]')
    async with running(home) as http:
        api_token = (home / "config" / "api_token").read_text().strip()
        made = await http.post(
            "/v1/operations/create_agent",
            json={"input": {"label": "big", "kinds": ["enrich"]}, "confirm": True},
            headers={"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"},
        )
        agent = {**HEADERS, "Authorization": f"Bearer {made.json()['output']['token']}"}
        listed = await http.post("/mcp/agent", json=LIST, headers=agent)
        assert listed.status_code == 200, listed.text
        names = {t["name"] for t in listed.json()["result"]["tools"]}
        assert names == {"claim_job", "heartbeat_job", "complete_job", "fail_job"}
        claim = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "claim_job", "arguments": {}},
        }
        nothing = await http.post("/mcp/agent", json=claim, headers=agent)
        assert nothing.json()["result"]["isError"] is False, nothing.text
        assert (await http.post("/mcp", json=LIST, headers=agent)).status_code == 401
        # the same queue answers the HTTP agent routes: core hands it the [swarm] settings
        over_http = await http.post(
            "/v1/agent/jobs/claim",
            json={},
            headers={"Authorization": agent["Authorization"], "Content-Type": "application/json"},
        )
        assert over_http.status_code == 200 and over_http.json() == {"job": None}


async def test_with_the_swarm_off_there_is_no_agent_door(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OAKEN_API__MCP", "true")
    async with running(home) as http:
        token = (home / "config" / "mcp_token").read_text().strip()
        headers = {**HEADERS, "Authorization": f"Bearer {token}"}
        listed = await http.post("/mcp/agent", json=LIST, headers=headers)
        assert listed.status_code == 404, "/mcp is exactly /mcp, and there is no agent door"
