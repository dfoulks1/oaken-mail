# SPDX-License-Identifier: Apache-2.0
"""``uvicorn`` served from inside core's own event loop, on a socket core opens itself.

Core binds ``127.0.0.1`` first, so it knows the real port (for the Host allowlist and the runtime
file) before the first request, and a second core cannot take the same fixed port. Signal handling
is core's, not uvicorn's: the daemon decides what a SIGTERM means.
"""

import asyncio
import contextlib
import socket
import sys
from collections.abc import Generator

import uvicorn

from oaken.api.app import Api
from oaken.core.errors import ConfigError
from oaken.core.logging import get_logger

log = get_logger("oaken.api")
LOOPBACK = "127.0.0.1"


class _Server(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield  # the daemon installs the handlers


class ApiServer:
    def __init__(self, api: Api, *, host: str = LOOPBACK, port: int = 0) -> None:
        if host != LOOPBACK:
            raise ConfigError(f"the API binds {LOOPBACK} only, not {host!r}")
        self.api, self.host, self.requested_port = api, host, port
        self.port = 0
        self._server: _Server | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> int:
        """Bind and serve. Returns the port. ``ConfigError`` if the port is taken."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if sys.platform != "win32":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # quick restarts
            sock.bind((self.host, self.requested_port))
            sock.listen(128)
            sock.setblocking(False)
        except OSError as exc:
            sock.close()
            raise ConfigError(f"cannot listen on {self.host}:{self.requested_port}: {exc}") from exc
        self.port = sock.getsockname()[1]
        self.api.bind(self.port)
        self.api.start()
        config = uvicorn.Config(
            self.api,
            log_config=None,
            access_log=False,  # a URL can hold a search the user typed; never log requests
            lifespan="off",
            ws="none",
            server_header=False,
            timeout_keep_alive=5,
            timeout_graceful_shutdown=5,
            h11_max_incomplete_event_size=64 * 1024,
        )
        self._server = _Server(config)
        self._task = asyncio.create_task(self._server.serve(sockets=[sock]))
        for _ in range(500):
            if self._server.started:
                log.info("api listening", host=self.host, port=self.port)
                return self.port
            if self._task.done():
                break
            await asyncio.sleep(0.01)
        if self._task.done():
            self._task.result()  # raises whatever stopped it
        raise ConfigError("the API server did not start")

    async def stop(self) -> None:
        """Stop accepting, close event streams, let requests in flight finish, close the socket."""
        self.api.stop()
        if self._server is not None:
            self._server.should_exit = True
        task, self._task = self._task, None
        if task is not None:
            await task
