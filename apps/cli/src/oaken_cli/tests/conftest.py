# SPDX-License-Identifier: Apache-2.0
"""Fixtures for the CLI tests.

The CLI is tested the way prompt 17 asks, against two transports that must behave the same:

* ``inprocess``: the operations rig's ``CoreClient`` behind :class:`InProcessApiClient`;
* ``http``: the real :class:`HttpApiClient` talking to core's real ASGI app through
  ``httpx2.ASGITransport``, so the wire format, the guard, the token and the error mapping are all
  exercised with no socket.

Every command test in ``test_cli_commands.py`` runs against both. One more test boots a real
daemon on a free port (``test_cli_e2e.py``).
"""

from __future__ import annotations

import asyncio
import io
import os
from collections.abc import AsyncIterator
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path

import httpx2
import pytest

from oaken.api.tests.conftest import ORIGIN, TOKEN, build_rig
from oaken.core.operations.tests.conftest import Ops
from oaken_cli.commands.runtime import set_agent_client_factory, set_client_factory
from oaken_cli.discovery import Endpoint
from oaken_cli.http import AgentClient, HttpApiClient
from oaken_cli.inprocess import InProcessApiClient
from oaken_cli.main import main


@dataclass
class Run:
    """What a CLI invocation produced: its exit code and captured streams."""

    code: int
    out: str
    err: str


@dataclass
class Cli:
    """Run the real ``main`` against the chosen transport.

    ``main`` calls :func:`asyncio.run`, which cannot nest inside the test's own event loop, so the
    invocation runs in a worker thread with its own loop, exactly as a real process would. The
    transport shares the ``ops`` stores, which are safe to touch from the worker because the test
    awaits the thread and does not touch them concurrently.
    """

    ops: Ops
    transport: str
    api: object | None = None
    token: str = TOKEN

    async def __call__(self, *argv: str) -> Run:
        return await asyncio.to_thread(self._run, argv)

    def _client(self, _args: object) -> InProcessApiClient | HttpApiClient:
        if self.transport == "inprocess":
            return InProcessApiClient(self.ops.client)
        asgi = httpx2.ASGITransport(app=self.api)  # type: ignore[arg-type]
        return HttpApiClient(Endpoint(ORIGIN, self.token), transport=asgi)

    def _agent(self, _args: object) -> AgentClient:
        asgi = httpx2.ASGITransport(app=self.api)  # type: ignore[arg-type]
        return AgentClient(Endpoint(ORIGIN, os.environ["OAKEN_AGENT_TOKEN"]), transport=asgi)

    def _run(self, argv: tuple[str, ...]) -> Run:
        set_client_factory(self._client)
        set_agent_client_factory(self._agent)
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = main(list(argv))
        finally:
            set_client_factory(None)
            set_agent_client_factory(None)
        return Run(code=code, out=out.getvalue(), err=err.getvalue())


@pytest.fixture(params=["inprocess", "http"])
async def cli(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Cli]:
    async with build_rig(tmp_path) as rig:
        yield Cli(rig.ops, request.param, rig.api)


@pytest.fixture
async def http_cli(tmp_path: Path) -> AsyncIterator[Cli]:
    """Only the HTTP transport, for what only HTTP has (tokens, agents, the event stream)."""
    async with build_rig(tmp_path) as rig:
        yield Cli(rig.ops, "http", rig.api)
