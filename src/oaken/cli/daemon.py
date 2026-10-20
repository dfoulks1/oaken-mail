# SPDX-License-Identifier: Apache-2.0
"""Daemon lifecycle: ``oaken start | stop | status | restart``.

Starting and stopping the long-running core process is **prompt 16's** work (the API daemon:
binding loopback, writing the runtime file, graceful shutdown on SIGTERM). Until then, ``start``,
``stop`` and ``restart`` are stubs that say so, with the exit code a script would expect.

``status`` already works as far as it can from the client side: it reports whether a live runtime
file names a reachable core, without starting anything. That is the one piece of lifecycle a thin
client owns on its own, so it is real now and prompt 16 only has to make the process it describes
actually exist.
"""

from __future__ import annotations

from oaken.cli.discovery import discover
from oaken.cli.exits import CliError, CoreUnavailable, ExitCode

_PROMPT16 = "building and supervising the core process is not implemented yet (prompt 16)"


def status(*, url: str | None = None, token_file: str | None = None) -> dict[str, object]:
    """Describe the running core, or report that it is down. Never starts anything.

    Returns a dict (so ``--json`` can print it directly): ``{"running": bool, ...}``. When core is
    up it includes the base url; the daemon's ``pid``/``version`` come from the runtime file once
    prompt 16 writes it, and from ``/v1/health`` once the HTTP client exists.
    """
    try:
        endpoint = discover(url=url, token_file=token_file)
    except CoreUnavailable:
        return {"running": False}
    return {"running": True, "url": endpoint.base_url}


def start(*, detach: bool = False) -> None:
    """Start core in the foreground (or ``--detach`` in the background). **Stub: prompt 16.**"""
    raise CliError(_PROMPT16, ExitCode.ERROR)


def stop() -> None:
    """Ask the running core to shut down gracefully. **Stub: prompt 16.**"""
    raise CliError(_PROMPT16, ExitCode.ERROR)


def restart(*, detach: bool = False) -> None:
    """Stop then start. **Stub: prompt 16.**"""
    raise CliError(_PROMPT16, ExitCode.ERROR)
