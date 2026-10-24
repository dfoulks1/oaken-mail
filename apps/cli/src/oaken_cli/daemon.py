# SPDX-License-Identifier: Apache-2.0
"""Daemon lifecycle: ``oaken start | stop | status | restart``.

The process management itself lives in core (``oaken.daemon``): claiming the runtime file, wiring
the services, signal handling, detaching. This module is the client's thin layer over it, and
turns core's errors into the CLI's exit codes. ``oaken.daemon`` imports the whole server stack, so
it is imported only inside the functions that need it: ``oaken --help`` and every ordinary command
never pay for it.

``status`` needs none of that. It reads the runtime file and checks the process is alive, and never
starts anything.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from oaken_cli.discovery import runtime_info
from oaken_cli.exits import CliError, ExitCode


def status(*, url: str | None = None, token_file: str | None = None) -> dict[str, Any]:
    """Describe the running core, or report that it is down. Never starts anything.

    Returns a dict (so ``--json`` can print it directly): ``{"running": bool, ...}`` and, when core
    is up, its ``pid``, ``url``, ``version`` and ``started_at`` from the runtime file. A core that
    has claimed the file but not yet bound its port reports ``"starting": true``.
    """
    if url:
        return {"running": True, "url": url.rstrip("/")}
    info = runtime_info()
    if info is None:
        return {"running": False}
    out: dict[str, Any] = {
        "running": True,
        "pid": info["pid"],
        "version": info.get("version", ""),
        "started_at": info.get("started_at", ""),
    }
    port = info.get("port")
    if isinstance(port, int) and port > 0:
        out["url"] = f"http://{info.get('host', '127.0.0.1')}:{port}"
    else:
        out["starting"] = True
    return out


def start(*, detach: bool = True) -> dict[str, Any] | None:
    """Start core in the background and return where it listens, or with ``detach=False`` run it in
    this process until it is stopped. Exit 1 if one is already running."""
    from oaken.core.errors import Conflict, OakenError

    try:
        if detach:
            from oaken.daemon import detach as detach_core

            return detach_core()
        from oaken.daemon import run

        code = run()
    except Conflict as exc:
        raise CliError(
            exc.message if hasattr(exc, "message") else str(exc), ExitCode.ERROR
        ) from None
    except OakenError as exc:
        raise CliError(str(exc), ExitCode.ERROR) from None
    if code != 0:
        raise CliError(
            "core exited with an error", ExitCode(code) if code in (1, 2) else ExitCode.ERROR
        )
    return None


def stop(
    *,
    grace: float | None = None,
    kill: bool = True,
    say: Callable[[str], None] | None = None,
) -> Any:
    """Ask the running core to stop, give it ``grace`` seconds, and kill it if it is still there
    (unless ``kill`` is False). Returns a ``StopResult``; ``running`` is False if none was."""
    from oaken.core.errors import OakenError
    from oaken.daemon import stop_gracefully
    from oaken.daemon.lifecycle import DEFAULT_GRACE

    try:
        return stop_gracefully(
            grace=DEFAULT_GRACE if grace is None else grace, kill=kill, notify=say
        )
    except OakenError as exc:
        raise CliError(str(exc), ExitCode.ERROR) from None


def restart(
    *,
    detach: bool = True,
    grace: float | None = None,
    kill: bool = True,
    say: Callable[[str], None] | None = None,
) -> dict[str, Any] | None:
    """Stop (if running) then start. In the background by default; ``detach=False`` runs core in
    this process until it is stopped."""
    stop(grace=grace, kill=kill, say=say)
    return start(detach=detach)


def reload() -> bool:
    """Ask the running core to re-read its configuration in place. ``False`` if none is running."""
    from oaken.core.errors import OakenError
    from oaken.daemon import reload as reload_core

    try:
        return reload_core()
    except OakenError as exc:
        raise CliError(str(exc), ExitCode.ERROR) from None
