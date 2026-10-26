# SPDX-License-Identifier: Apache-2.0
"""The API over the real registry and stores, driven in-process through ``ASGITransport``.

The Host header is ``127.0.0.1:PORT`` with a fixed pretend port; the one real-socket test lives in
``oaken.daemon.tests``.
"""

import contextlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pydantic import BaseModel

from oaken.api import Api
from oaken.core.agents import AgentRegistry
from oaken.core.errors import OakenError
from oaken.core.eventlog import EventLog
from oaken.core.operations.tests.conftest import Ops, build_ops
from oaken.core.settings import APISettings, SwarmSettings

PORT = 47123
TOKEN = "test-token-" + "x" * 32  # noqa: S105 (a fixture, not a secret)
ORIGIN = f"http://127.0.0.1:{PORT}"
SWARM = SwarmSettings(enabled=True, allowed_kinds=["enrich"], max_agents=3)


class Result(BaseModel):
    text: str
    score: int = 0


@dataclass
class Rig:
    ops: Ops
    api: Api
    http: httpx2.AsyncClient
    agents: AgentRegistry

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {TOKEN}"}

    async def get(self, path: str, **kw: Any) -> httpx2.Response:
        headers = {**self.auth, **kw.pop("headers", {})}
        return await self.http.get(path, headers=headers, **kw)

    async def post(
        self, path: str, body: Any = None, *, client: str | None = None, **kw: Any
    ) -> httpx2.Response:
        headers = {**self.auth, **kw.pop("headers", {})}
        if client:
            headers["X-Oaken-Client"] = client
        return await self.http.post(path, json={} if body is None else body, headers=headers, **kw)

    async def ui(
        self, action: str, back: str = "/stats", *, token: str | None = None, **fields: str
    ) -> httpx2.Response:
        """A console form post, as the browser sends it: urlencoded, with the page's token. The
        reply is the 303 back to the page; ``seen`` follows it to read the message."""
        from oaken.api.console import csrf_token

        data = {"csrf": csrf_token() if token is None else token, "back": back, **fields}
        return await self.http.post(f"/ui/{action}", data=data)

    async def seen(self, action: str, back: str = "/stats", **fields: str) -> str:
        """The page text after a form post, with its one-time message on it."""
        resp = await self.ui(action, back, **fields)
        assert resp.status_code == 303, resp.text
        page = await self.http.get(resp.headers["location"])
        assert page.status_code == 200
        return page.text

    async def call(self, name: str, input_: dict[str, Any] | None = None, **kw: Any) -> Any:
        """A confirmed call as the CLI; returns the parsed body."""
        r = await self.post(
            f"/v1/operations/{name}", {"input": input_ or {}, "confirm": True}, **kw
        )
        assert r.status_code == 200, r.text
        return r.json()


@contextlib.asynccontextmanager
async def build_rig(tmp: Path, **api_settings: Any) -> AsyncIterator[Rig]:
    async for ops in build_ops(tmp):
        svc = ops.svc
        svc.settings = svc.settings.model_copy(
            update={"api": APISettings(**api_settings), "swarm": SWARM}
        )
        ops.rig.queue.swarm = SWARM
        ops.rig.queue.register_result("enrich", Result)
        agents = AgentRegistry(ops.rig.raw_db, ops.rig.clock, SWARM, ops.rig.bus)
        svc.agents = agents
        svc.eventlog = EventLog(tmp / "events.jsonl", max_bytes=1_000_000, backups=1)
        svc.eventlog.attach(ops.rig.bus)  # so events_tail has a log to read, as a real core does
        api = Api(ops.client, token=TOKEN)
        api.bind(PORT)
        api.start()
        transport = httpx2.ASGITransport(app=api)
        async with httpx2.AsyncClient(transport=transport, base_url=ORIGIN) as http:
            try:
                yield Rig(ops, api, http, agents)
            finally:
                api.stop()


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    async with build_rig(tmp_path) as r:
        yield r


@pytest.fixture
def make_rig(tmp_path: Path) -> Callable[..., Any]:
    import itertools

    counter = itertools.count()

    def make(**api_settings: Any) -> Any:
        where = tmp_path / f"api{next(counter)}"
        where.mkdir()
        return build_rig(where, **api_settings)

    return make


__all__ = ["ORIGIN", "PORT", "TOKEN", "OakenError", "Rig"]


@pytest.fixture(autouse=True)
def reloads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """``config_reload`` signals this very process (SIGHUP), which would end the test run. Record
    that signal instead of sending it; any other signal goes through."""
    import os
    import signal

    sent: list[int] = []
    real_kill = os.kill

    def kill(pid: int, sig: int) -> None:
        if sig == signal.SIGHUP:
            sent.append(sig)
        else:
            real_kill(pid, sig)

    monkeypatch.setattr("oaken.core.operations.builtin.config.os.kill", kill)
    return sent
