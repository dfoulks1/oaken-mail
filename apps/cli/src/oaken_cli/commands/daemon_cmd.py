# SPDX-License-Identifier: Apache-2.0
"""``oaken start | stop | status | restart | reload`` handlers.

``status`` reports whether a live core is running, without starting anything. ``start``, ``stop``
and ``restart`` defer to :mod:`oaken_cli.daemon`, which drives core's own daemon module.
"""

from __future__ import annotations

import argparse
from typing import Any

from oaken_cli import daemon
from oaken_cli.commands.runtime import emit
from oaken_cli.exits import ExitCode


def cmd_status(args: argparse.Namespace) -> int:
    info = daemon.status(
        url=getattr(args, "url", None), token_file=getattr(args, "token_file", None)
    )

    def text() -> None:
        from oaken_cli.output import print_text

        if not info.get("running"):
            print_text("core is not running")
        elif info.get("starting"):
            print_text(f"core is starting (pid {info.get('pid')})")
        else:
            version = f" {info['version']}" if info.get("version") else ""
            pid = f", pid {info['pid']}" if info.get("pid") else ""
            print_text(f"core{version} is running at {info.get('url', '')}{pid}")

    emit(args, data=info, text=text)
    # Exit 0 whether up or down: ``status`` answers a question, it does not fail because core is
    # down. (``--json`` scripts read the ``running`` field.)
    return int(ExitCode.OK)


def _started(args: argparse.Namespace, info: dict[str, Any] | None, verb: str) -> int:
    if info is None:  # a foreground run that has ended
        return int(ExitCode.OK)
    data = {"running": True, "url": f"http://{info['host']}:{info['port']}", **info}

    def text() -> None:
        from oaken_cli.output import print_text

        print_text(f"core {verb}: {data['url']} (pid {info['pid']})")

    return emit(args, data=data, text=text)


def cmd_start(args: argparse.Namespace) -> int:
    if not getattr(args, "detach", False) and getattr(args, "verbose", 0) == 0:
        _log_info()  # a foreground daemon should say what it is doing
    return _started(args, daemon.start(detach=getattr(args, "detach", False)), "started")


def cmd_stop(args: argparse.Namespace) -> int:
    stopped = daemon.stop()

    def text() -> None:
        from oaken_cli.output import print_text

        print_text("core stopped" if stopped else "core is not running")

    return emit(args, data={"stopped": stopped}, text=text)


def cmd_restart(args: argparse.Namespace) -> int:
    detach = getattr(args, "detach", False)
    if not detach and getattr(args, "verbose", 0) == 0:
        _log_info()
    return _started(args, daemon.restart(detach=detach), "restarted")


def cmd_reload(args: argparse.Namespace) -> int:
    signalled = daemon.reload()

    def text() -> None:
        from oaken_cli.output import print_text

        print_text(
            "core is reloading its configuration (see its log for the outcome)"
            if signalled
            else "core is not running"
        )

    return emit(args, data={"reloading": signalled}, text=text)


def _log_info() -> None:
    import sys

    from oaken.core.logging import configure_logging
    from oaken.core.settings import LogSettings

    configure_logging(LogSettings(level="INFO"), stream=sys.stderr)
