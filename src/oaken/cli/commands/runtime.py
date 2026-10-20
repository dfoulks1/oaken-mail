# SPDX-License-Identifier: Apache-2.0
"""Shared plumbing for the command handlers: get an :class:`ApiClient`, and run an async body.

A command does not care whether it is talking to a daemon over HTTP or to an in-process
``CoreClient``; it asks :func:`make_client` for an :class:`~oaken.cli.client.ApiClient` and uses the
protocol. By default :func:`make_client` builds the HTTP client from discovery (the normal path,
talking to the running daemon). Tests — and a future embedded mode — install a different factory
with :func:`set_client_factory`, so the same command code runs against the in-process client with
the fakes, no socket involved.

:func:`run` wraps ``asyncio.run`` with the one piece of error translation every command shares:
turning a core-side :class:`~oaken.core.errors.OakenError` (reached as an HTTP error body or, in
process, a real exception) into a :class:`~oaken.cli.exits.CliError` with the right exit code.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from oaken.cli.client import ApiClient
from oaken.cli.exits import CliError, CoreUnavailable, ExitCode, exit_for_error_code

# A factory the process can override (tests, embedded mode). Takes the parsed args so it can read
# --url / --token-file; returns an ApiClient (an async context manager).
ClientFactory = Callable[[argparse.Namespace], ApiClient]
_factory: ClientFactory | None = None


def set_client_factory(factory: ClientFactory | None) -> None:
    """Install (or, with ``None``, clear) the :class:`ApiClient` factory. Tests use this to inject
    the in-process client; the default builds the HTTP client from discovery."""
    global _factory
    _factory = factory


def make_client(args: argparse.Namespace) -> ApiClient:
    """Build the :class:`ApiClient` for this invocation.

    With a factory installed, defer to it. Otherwise discover the daemon and build the HTTP client
    (a stub until prompt 16). Discovery raises :class:`CoreUnavailable` (exit 3) when core is down.
    """
    if _factory is not None:
        return _factory(args)
    from oaken.cli.discovery import discover
    from oaken.cli.http import HttpApiClient

    endpoint = discover(
        url=getattr(args, "url", None), token_file=getattr(args, "token_file", None)
    )
    return HttpApiClient(endpoint)


def run(body: Callable[[ApiClient], Awaitable[int]], args: argparse.Namespace) -> int:
    """Open a client, run ``body`` in an event loop, translate core errors to exit codes."""

    async def _main() -> int:
        async with make_client(args) as client:
            return await body(client)

    try:
        return asyncio.run(_main())
    except CoreUnavailable:
        raise  # already a CliError with exit 3 and the exact message
    except CliError:
        raise
    except Exception as exc:  # noqa: BLE001 — the CLI's top-of-stack translation
        raise _translate(exc) from exc


def _translate(exc: Exception) -> CliError:
    """Map a core-side error to a :class:`CliError`. Recognizes ``OakenError`` by its ``code``
    attribute without importing the whole taxonomy at module load."""
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None) or str(exc) or type(exc).__name__
    if isinstance(code, str):
        return CliError(message, exit_for_error_code(code))
    if isinstance(exc, NotImplementedError):
        return CliError(str(exc), ExitCode.ERROR)
    return CliError(message, ExitCode.ERROR)


def emit(args: argparse.Namespace, *, data: Any, text: Callable[[], None]) -> int:
    """Print either JSON (``--json``) or human text, and return exit 0.

    ``data`` is the machine form (printed verbatim as JSON); ``text`` is a callable that prints the
    human form (so its cost is paid only when not in JSON mode).
    """
    if getattr(args, "json", False):
        from oaken.cli.output import print_json

        print_json(data)
    else:
        text()
    return int(ExitCode.OK)
