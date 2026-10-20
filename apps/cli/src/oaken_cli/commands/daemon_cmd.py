# SPDX-License-Identifier: Apache-2.0
"""``oaken start | stop | status | restart`` handlers.

``status`` is real now: it reports whether a live core is reachable, without starting anything.
``start``, ``stop`` and ``restart`` defer to :mod:`oaken_cli.daemon`, whose process management is a
stub until prompt 16.
"""

from __future__ import annotations

import argparse

from oaken_cli import daemon
from oaken_cli.commands.runtime import emit
from oaken_cli.exits import ExitCode


def cmd_status(args: argparse.Namespace) -> int:
    info = daemon.status(
        url=getattr(args, "url", None), token_file=getattr(args, "token_file", None)
    )

    def text() -> None:
        from oaken_cli.output import print_text

        if info.get("running"):
            print_text(f"core is running at {info.get('url', '')}")
        else:
            print_text("core is not running")

    emit(args, data=info, text=text)
    # Exit 0 whether up or down: ``status`` answers a question, it does not fail because core is
    # down. (``--json`` scripts read the ``running`` field.)
    return int(ExitCode.OK)


def cmd_start(args: argparse.Namespace) -> int:
    daemon.start(detach=getattr(args, "detach", False))
    return int(ExitCode.OK)  # unreachable until prompt 16; start() raises today


def cmd_stop(args: argparse.Namespace) -> int:
    daemon.stop()
    return int(ExitCode.OK)


def cmd_restart(args: argparse.Namespace) -> int:
    daemon.restart(detach=getattr(args, "detach", False))
    return int(ExitCode.OK)
