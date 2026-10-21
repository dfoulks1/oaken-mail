# SPDX-License-Identifier: Apache-2.0
"""The ``oaken-webui`` entry point: serve the browser UI and proxy to core.

Thin by design. This module builds the ASGI app (:mod:`oaken_webui.app`) and runs it under
``uvicorn`` on loopback. Core is reached through the same discovery the CLI uses
(:mod:`oaken_cli.discovery`): the runtime file for the port, the token file for the credential,
or the ``--url`` / ``--token-file`` dev overrides.

Binding is ``127.0.0.1`` only, and cannot be configured otherwise — the UI server is as local as
core. It does not start core; if core is down the proxy answers ``503 core_unavailable`` and the
UI shows it, exactly as the CLI reports a core-down.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import socket
import subprocess  # noqa: S404 (starts our own interpreter, never a shell)
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import uvicorn

from oaken import __version__
from oaken.api.runtime import pid_alive
from oaken.core import paths
from oaken_cli.discovery import Endpoint, discover
from oaken_cli.exits import CoreUnavailable, ExitCode
from oaken_webui.app import build_app

PROG = "oaken-webui"
LOOPBACK = "127.0.0.1"
DEFAULT_PORT = 47813  # obscure, high-numbered, well clear of common dev ports
COMMANDS = ("start", "stop", "restart", "status")
PID_FILE = "webui.json"  # beside core's runtime file; says which process serves the UI, and where


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Serve the Oaken Mail web UI and proxy /v1/* to the local core daemon.",
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    parser.add_argument(
        "command",
        nargs="?",
        choices=COMMANDS,
        default="start",
        help="start (the default; foreground unless --detach), stop, restart, or status.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"port to listen on (default {DEFAULT_PORT}; restart keeps the running one's)",
    )
    parser.add_argument(
        "--detach",
        action="store_true",
        help="with start or restart: run in the background and return once it is serving.",
    )
    parser.add_argument("--stop", action="store_true", help=argparse.SUPPRESS)  # old spelling
    parser.add_argument(
        "--url",
        metavar="URL",
        help="dev override: a hand-started core's base URL (loopback only), e.g. "
        "http://127.0.0.1:8123. Default: discover the running daemon.",
    )
    parser.add_argument(
        "--token-file", metavar="PATH", help="dev override: read the API token from this file."
    )
    parser.add_argument(
        "--verbose", "-v", action="count", default=0, help="-v for INFO, -vv for DEBUG on stderr."
    )
    return parser


def _endpoint(args: argparse.Namespace) -> Endpoint | None:
    """A fixed endpoint when a dev override is given, else ``None`` so the proxy discovers core
    per request (and tolerates a core that is not up yet)."""
    if args.url or args.token_file:
        return discover(url=args.url, token_file=args.token_file)
    return None


def _configure_logging(verbose: int) -> None:
    """Core's structured logs to stderr; stdout stays clean. Quiet by default."""
    from oaken.core.logging import configure_logging
    from oaken.core.settings import LogSettings

    if verbose >= 2:
        settings = LogSettings(level="DEBUG")
    elif verbose == 1:
        settings = LogSettings(level="INFO")
    else:
        settings = LogSettings(level="WARNING")
    configure_logging(settings, stream=sys.stderr)


def _pid_path() -> Path:
    return paths.runtime_file(PID_FILE)


def _running() -> dict[str, int] | None:
    """The pid file's contents if that server is alive, else ``None``."""
    try:
        data = json.loads(_pid_path().read_text())
        pid, port = int(data["pid"]), int(data["port"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return {"pid": pid, "port": port} if pid_alive(pid) else None


def _listening(port: int) -> bool:
    with contextlib.suppress(OSError), socket.create_connection((LOOPBACK, port), timeout=0.2):
        return True
    return False


def _child_args(args: argparse.Namespace) -> list[str]:
    """The arguments that start the same server in the foreground."""
    out = ["start", "--port", str(args.port)]
    if args.url:
        out += ["--url", args.url]
    if args.token_file:
        out += ["--token-file", args.token_file]
    return out + ["-v"] * args.verbose


def _detach(args: argparse.Namespace) -> int:
    """Start ourselves again in the background (a new session on POSIX, so closing the terminal
    does not stop it) and wait until the port answers."""
    found = _running()
    if found is not None:
        print(
            f"{PROG}: already running (pid {found['pid']}) on http://{LOOPBACK}:{found['port']}",
            file=sys.stderr,
        )
        return int(ExitCode.ERROR)
    flags: dict[str, Any] = {}
    if sys.platform == "win32":  # pragma: no cover (Windows only)
        flags["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        flags["start_new_session"] = True
    child = subprocess.Popen(  # noqa: S603 (our own interpreter, fixed arguments)
        [sys.executable, "-m", "oaken_webui.main", *_child_args(args)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        **flags,
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        found = _running()
        if _listening(args.port) and found is not None and found["pid"] == child.pid:
            # Something else may be holding the port, and the child writes its pid file just before
            # it tries to bind: only believe it if it is still alive a moment later.
            time.sleep(0.4)
            if child.poll() is not None:
                continue  # the next pass reports that it exited
            print(
                f"{PROG}: serving the Oaken Mail UI on http://{LOOPBACK}:{args.port} "
                f"(pid {child.pid}); stop it with {PROG} stop",
                file=sys.stderr,
            )
            return int(ExitCode.OK)
        if child.poll() is not None:
            print(
                f"{PROG}: exited while starting (code {child.returncode}); is port "
                f"{args.port} in use? Run without --detach to see why.",
                file=sys.stderr,
            )
            return int(ExitCode.ERROR)
        time.sleep(0.05)
    print(f"{PROG}: did not start listening within 15s", file=sys.stderr)
    return int(ExitCode.ERROR)


def _stop(*, quiet: bool = False) -> int:
    found = _running()
    if found is None:
        if not quiet:
            print(f"{PROG}: not running", file=sys.stderr)
        return int(ExitCode.OK if quiet else ExitCode.ERROR)
    sig = signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGTERM  # type: ignore[attr-defined,unused-ignore]
    with contextlib.suppress(ProcessLookupError):
        os.kill(found["pid"], sig)
    deadline = time.monotonic() + 10
    while pid_alive(found["pid"]) and time.monotonic() < deadline:
        time.sleep(0.05)
    if pid_alive(found["pid"]):
        print(f"{PROG}: pid {found['pid']} did not stop within 10s", file=sys.stderr)
        return int(ExitCode.ERROR)
    with contextlib.suppress(OSError):
        _pid_path().unlink()  # uvicorn re-raises SIGTERM, so the server's own cleanup is skipped
    print(f"{PROG}: stopped", file=sys.stderr)
    return int(ExitCode.OK)


def _status() -> int:
    found = _running()
    if found is None:
        print(f"{PROG}: not running", file=sys.stderr)
        return int(ExitCode.ERROR)
    print(f"{PROG}: running (pid {found['pid']}) on http://{LOOPBACK}:{found['port']}")
    return int(ExitCode.OK)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    command = "stop" if args.stop else args.command
    if command == "status":
        return _status()
    if command == "stop":
        return _stop()
    if command == "restart":
        before = _running()
        if _stop(quiet=True) != int(ExitCode.OK):
            return int(ExitCode.ERROR)
        if args.port is None and before is not None:
            args.port = before["port"]  # the UI comes back where the browser tab expects it
    if args.port is None:
        args.port = DEFAULT_PORT
    if args.detach:
        return _detach(args)
    _configure_logging(args.verbose)
    try:
        endpoint = _endpoint(args)
    except CoreUnavailable as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return int(ExitCode.ERROR)

    app = build_app(endpoint=endpoint)
    config = uvicorn.Config(
        app,
        host=LOOPBACK,
        port=args.port,
        log_config=None,
        access_log=False,  # a URL can hold a search the user typed; never log requests
        server_header=False,
    )
    server = uvicorn.Server(config)
    print(f"{PROG}: serving the Oaken Mail UI on http://{LOOPBACK}:{args.port}", file=sys.stderr)
    found = _running()
    if found is not None:
        print(
            f"{PROG}: already running (pid {found['pid']}) on http://{LOOPBACK}:{found['port']}",
            file=sys.stderr,
        )
        return int(ExitCode.ERROR)
    pid_file = _pid_path()
    pid_file.write_text(json.dumps({"pid": os.getpid(), "port": args.port}))
    try:
        server.run()
    except KeyboardInterrupt:
        return int(ExitCode.ERROR)
    finally:
        with contextlib.suppress(OSError):
            pid_file.unlink()
    return int(ExitCode.OK)


def _console_main() -> None:
    raise SystemExit(main())


if __name__ == "__main__":  # pragma: no cover
    _console_main()
