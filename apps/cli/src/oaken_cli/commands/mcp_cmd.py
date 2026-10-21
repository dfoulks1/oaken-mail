# SPDX-License-Identifier: Apache-2.0
"""``oaken mcp``: a stdio bridge to the MCP server inside core, for clients that only speak stdio.

The bridge adds no behaviour. It reads one JSON-RPC message per line from stdin, posts it to
``/mcp`` on the running core with the MCP token, and writes core's answer to stdout. Everything the
client sees (the tool list, the two-phase confirm, the untrusted envelope, the limits) is core's.
It finds core the way every other command does (the runtime file), never starts it, and never
opens a database: this module imports no core code beyond discovery.

When core cannot answer, the bridge says so in the MCP way and does not hang:

* a tool call gets a normal result with ``isError`` and ``{"code": "core_not_running", ...}``
  (or ``core_timeout``, ``core_auth``, ``mcp_disabled``), with the start hint in the message;
* other requests get a JSON-RPC error with the same code in ``error.data``;
* ``initialize`` is answered locally when core is down, so a client that starts before core
  still connects and reports the problem on the first call instead of failing to start;
* the bridge looks core up again on every message, so it recovers when core is started.

Stdout carries protocol only; messages about the bridge go to stderr, and never include mail text.
With ``--agent`` the bridge speaks to ``/mcp/agent`` with the token in ``OAKEN_AGENT_TOKEN`` (or
``--agent-token-file``), so a stdio-only worker can take jobs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, BinaryIO

from oaken_cli.discovery import Endpoint, discover
from oaken_cli.exits import CORE_DOWN_MESSAGE, CliError, CoreUnavailable, ExitCode

MCP_TOKEN_FILE = "mcp_token"  # noqa: S105  — a filename, not a secret
PATH = "/mcp"
AGENT_PATH = "/mcp/agent"
DEFAULT_TIMEOUT = 60.0
CONNECT_TIMEOUT = 3.0
MAX_LINE = 1 << 20  # a message is one line; a megabyte is far beyond anything a client sends
WORKERS = 8
PROTOCOL = "2025-06-18"

MESSAGES = {
    "core_not_running": CORE_DOWN_MESSAGE,
    "core_timeout": "core did not answer in time; try again, or raise --timeout",
    "core_auth": "core refused the MCP token; check the token file (or restart the bridge)",
    "mcp_disabled": "the MCP server is off in core; set [api] mcp = true and restart core",
    "core_error": "core could not handle the request",
}


class Down(Exception):
    """Core did not give an answer; ``code`` is the one the client is shown."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.message = message or MESSAGES[code]


def _frame(message: dict[str, Any]) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))


def _tool_error(request_id: Any, down: Down) -> str:
    text = json.dumps({"code": down.code, "message": down.message}, separators=(",", ":"))
    result = {"content": [{"type": "text", "text": text}], "isError": True}
    return _frame({"jsonrpc": "2.0", "id": request_id, "result": result})


def _rpc_error(request_id: Any, down: Down) -> str:
    error = {"code": -32000, "message": down.message, "data": {"code": down.code}}
    return _frame({"jsonrpc": "2.0", "id": request_id, "error": error})


def _initialize_locally(request_id: Any, params: Any) -> str:
    asked = params.get("protocolVersion") if isinstance(params, dict) else None
    result = {
        "protocolVersion": asked if isinstance(asked, str) else PROTOCOL,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "oaken-mail-bridge", "version": "0"},
        "instructions": f"Oaken Mail is not reachable yet: {CORE_DOWN_MESSAGE}.",
    }
    return _frame({"jsonrpc": "2.0", "id": request_id, "result": result})


def _sse_data(text: str) -> str | None:
    """The JSON of the first ``data:`` event in a ``text/event-stream`` body, if there is one."""
    data = [line[5:].lstrip() for line in text.splitlines() if line.startswith("data:")]
    return "".join(data) or None


class Bridge:
    """Forward one message at a time. ``resolve`` finds core (and may raise ``CoreUnavailable``);
    ``post`` sends the message and returns ``(status, content_type, body)``. Both are parameters so
    the tests can drive the logic without a socket."""

    def __init__(
        self,
        resolve: Callable[[], Endpoint],
        path: str = PATH,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        post: Callable[[str, str, str, float], tuple[int, str, str]] | None = None,
    ) -> None:
        self.resolve = resolve
        self.path = path
        self.timeout = timeout
        self._post = post or self._http_post
        self._client: Any = None
        self._lock = threading.Lock()

    def _http_post(self, url: str, token: str, body: str, timeout: float) -> tuple[int, str, str]:
        import httpx2

        with self._lock:
            if self._client is None:
                self._client = httpx2.Client()
        try:
            response = self._client.post(
                url,
                content=body.encode(),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
                timeout=httpx2.Timeout(timeout, connect=CONNECT_TIMEOUT),
            )
        except httpx2.TimeoutException:
            raise Down("core_timeout") from None
        except httpx2.TransportError:
            raise Down("core_not_running") from None
        return response.status_code, response.headers.get("content-type", ""), response.text

    def handle(self, line: str) -> str | None:
        """The line to write back, or ``None`` when nothing is owed (a notification)."""
        try:
            message = json.loads(line)
        except ValueError:
            return _frame(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
            )
        if not isinstance(message, dict):
            return _frame(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "bad request"}}
            )
        request_id = message.get("id")
        wants_answer = "id" in message and "method" in message
        method = message.get("method")
        try:
            return self._forward(line, wants_answer)
        except Down as down:
            if not wants_answer:
                print(f"oaken mcp: {down.message}", file=sys.stderr)
                return None
            if method == "initialize" and down.code == "core_not_running":
                return _initialize_locally(request_id, message.get("params"))
            return (_tool_error if method == "tools/call" else _rpc_error)(request_id, down)

    def _forward(self, line: str, wants_answer: bool) -> str | None:
        try:
            endpoint = self.resolve()
        except CoreUnavailable:
            raise Down("core_not_running") from None
        status, content_type, body = self._post(
            endpoint.base_url + self.path, endpoint.token, line, self.timeout
        )
        if status in (202, 204) or (status == 200 and not body.strip()):
            return None
        if status == 401:
            raise Down("core_auth")
        if status == 404:
            raise Down("mcp_disabled")
        if status != 200:
            raise Down("core_error", self._why(status, body))
        if "text/event-stream" in content_type:
            body = _sse_data(body) or ""
        return (body.strip() or None) if wants_answer else None

    @staticmethod
    def _why(status: int, body: str) -> str:
        """Core's own ``{"code","message"}`` (it never holds mail text), else just the status."""
        try:
            found = json.loads(body)
            if isinstance(found, dict) and isinstance(found.get("message"), str):
                return f"core answered {status}: {found['message']}"
        except ValueError:
            pass
        return f"core answered {status}"


def serve(bridge: Bridge, stdin: BinaryIO, stdout: BinaryIO) -> None:
    """Read lines until EOF, forward each on a small thread pool (a slow call must not hold up the
    next), and write answers one at a time."""
    write = threading.Lock()

    def answer(line: str) -> None:
        reply = bridge.handle(line)
        if reply is not None:
            with write:
                stdout.write(reply.encode() + b"\n")
                stdout.flush()

    with ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="oaken-mcp") as pool:
        while True:
            raw = stdin.readline(MAX_LINE + 1)
            if not raw:
                break
            if len(raw) > MAX_LINE:
                while raw and not raw.endswith(b"\n"):  # drop the rest of the oversized line
                    raw = stdin.readline(MAX_LINE)
                with write:
                    stdout.write(
                        _frame(
                            {
                                "jsonrpc": "2.0",
                                "id": None,
                                "error": {"code": -32600, "message": "message too large"},
                            }
                        ).encode()
                        + b"\n"
                    )
                    stdout.flush()
                continue
            text = raw.decode("utf-8", "replace").strip()
            if text:
                pool.submit(answer, text)


def _resolver(args: argparse.Namespace) -> tuple[Callable[[], Endpoint], str]:
    url = getattr(args, "url", None)
    if getattr(args, "agent", False):
        token = os.environ.get("OAKEN_AGENT_TOKEN", "")
        path = getattr(args, "agent_token_file", None)
        if path:
            token = Path(path).expanduser().read_text(encoding="utf-8").strip()
        if not token:
            raise CliError(
                "no agent token: set OAKEN_AGENT_TOKEN or pass --agent-token-file", ExitCode.AUTH
            )
        return (lambda: discover(url=url, token=token)), AGENT_PATH
    token_file = getattr(args, "token_file", None)
    custom = token_file or os.environ.get("OAKEN_TOKEN_FILE")
    from oaken.core import paths

    mcp_token = None if custom else paths.config_dir() / MCP_TOKEN_FILE
    return (lambda: discover(url=url, token_file=token_file, token_path=mcp_token)), PATH


def cmd_mcp(args: argparse.Namespace) -> int:
    resolve, path = _resolver(args)
    bridge = Bridge(resolve, path, timeout=float(args.timeout))
    try:
        serve(bridge, sys.stdin.buffer, sys.stdout.buffer)
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    return int(ExitCode.OK)
