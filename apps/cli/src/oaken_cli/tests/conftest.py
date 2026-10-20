# SPDX-License-Identifier: Apache-2.0
"""Fixtures for the CLI tests.

The CLI is tested exactly as prompt 17 asks: against the in-process ``CoreClient`` the operations
rig already builds over the fakes — no daemon, no socket, no network. We install an
:class:`~oaken_cli.inprocess.InProcessApiClient` through :func:`set_client_factory`, run the real
argparse ``main`` with an argv, and capture what a user would see (stdout, stderr, exit code).

A single end-to-end test against a real booted daemon on a free port is the other half of the
acceptance criteria; it is blocked until the HTTP server exists (prompt 16) and is marked skipped
in ``test_cli_e2e.py``.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path

import pytest

# Reuse the operations rig verbatim: it yields an ``Ops`` holding a CoreClient over the fakes.
from oaken.core.operations.tests.conftest import Ops, build_ops
from oaken_cli.commands.runtime import set_client_factory
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
    """Run the real ``main`` against the in-process client built from ``ops``.

    ``main`` calls :func:`asyncio.run`, which cannot nest inside the test's own event loop, so the
    invocation runs in a worker thread with its own loop — exactly as a real process would. The
    in-process client shares the ``ops`` stores, which are safe to touch from the worker because the
    test awaits the thread and does not touch them concurrently.
    """

    ops: Ops

    async def __call__(self, *argv: str) -> Run:
        return await asyncio.to_thread(self._run, argv)

    def _run(self, argv: tuple[str, ...]) -> Run:
        set_client_factory(lambda _args: InProcessApiClient(self.ops.client))
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = main(list(argv))
        finally:
            set_client_factory(None)
        return Run(code=code, out=out.getvalue(), err=err.getvalue())


@pytest.fixture
async def cli(tmp_path: Path) -> AsyncIterator[Cli]:
    async for o in build_ops(tmp_path):
        yield Cli(o)
