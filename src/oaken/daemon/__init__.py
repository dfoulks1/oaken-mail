# SPDX-License-Identifier: Apache-2.0
"""Core as a running process: composition root and daemon lifecycle."""

from oaken.daemon.core import Core
from oaken.daemon.lifecycle import (
    StopResult,
    detach,
    reload,
    restart,
    run,
    serve,
    status,
    stop,
    stop_gracefully,
)

__all__ = [
    "Core",
    "StopResult",
    "detach",
    "reload",
    "restart",
    "run",
    "serve",
    "status",
    "stop",
    "stop_gracefully",
]
