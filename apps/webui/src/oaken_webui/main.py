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
import sys
from collections.abc import Sequence

import uvicorn

from oaken import __version__
from oaken_cli.discovery import Endpoint, discover
from oaken_cli.exits import CoreUnavailable, ExitCode
from oaken_webui.app import build_app

PROG = "oaken-webui"
LOOPBACK = "127.0.0.1"
DEFAULT_PORT = 47813  # obscure, high-numbered, well clear of common dev ports


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Serve the Oaken Mail web UI and proxy /v1/* to the local core daemon.",
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"port to listen on (default {DEFAULT_PORT})"
    )
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


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
    try:
        server.run()
    except KeyboardInterrupt:
        return int(ExitCode.ERROR)
    return int(ExitCode.OK)


def _console_main() -> None:
    raise SystemExit(main())


if __name__ == "__main__":  # pragma: no cover
    _console_main()
