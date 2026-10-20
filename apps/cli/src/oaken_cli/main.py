# SPDX-License-Identifier: Apache-2.0
"""The ``oaken`` entry point: an argparse front end over the registry.

Design choices (prompt 17 asks us to pick and justify a framework):

* **argparse, from the standard library.** No third-party parser is added to the core runtime, so
  ``oaken --help`` starts fast (the <300 ms budget) and the dependency surface stays small — the
  context rules discourage dependencies a prompt does not call for. ``rich`` *is* called for and is
  used only for human output, imported lazily (see :mod:`oaken_cli.output`).
* **Lazy imports.** This module imports almost nothing at module load: building the parser and
  printing ``--help`` must not import core, pydantic models, httpx or rich. The heavy work happens
  inside command functions, which are imported only when their command runs.
* **Thin commands.** Every command resolves to one or more :class:`~oaken_cli.client.ApiClient`
  calls. No command contains business logic; new registry operations are reachable immediately via
  the generic ``oaken operation NAME --json '{...}'`` fallback, so a registry change never needs a
  CLI release.

Exit codes are the contract in :mod:`oaken_cli.exits`. The dispatcher turns a raised
:class:`~oaken_cli.exits.CliError` into a clean one-line message and that exit code; an operation
that failed on the core side is turned into the right code by the command via
:func:`oaken_cli.exits.exit_for_error_code`.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

from oaken import __version__

# A command handler takes the parsed args and returns an exit code (int). Handlers are set as the
# ``func`` default on each subparser and imported lazily inside ``_register_*`` helpers.
Handler = Callable[[argparse.Namespace], int]

PROG = "oaken"
DESCRIPTION = "Oaken Mail — a local-first, AI-native mail client. Thin CLI over the local core."


def build_parser() -> argparse.ArgumentParser:
    """Construct the full parser. Kept import-light so ``--help`` is cheap."""
    parser = argparse.ArgumentParser(prog=PROG, description=DESCRIPTION, parents=[_globals()])
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    # Every subcommand inherits the global flags, so ``--json``/``--url``/``--token-file`` work
    # whether they come before or after the command name (argparse otherwise only accepts a
    # top-level option before the subcommand).
    g = _globals()
    _register_daemon(sub, g)
    _register_operation(sub, g)
    _register_status(sub, g)
    _register_mail(sub, g)
    _register_system(sub, g)
    # Any registry operation without its own verb above is still reachable through the generic
    # ``oaken operation NAME --json '{...}'`` fallback, so a registry change never needs a release.
    return parser


def _globals() -> argparse.ArgumentParser:
    """A parent parser carrying the flags every command shares. ``add_help=False`` so it does not
    fight the real parser's ``-h``."""
    parent = argparse.ArgumentParser(add_help=False)
    g = parent.add_argument_group("connection")
    g.add_argument("--url", metavar="URL", help="Base URL of a running core (dev override).")
    g.add_argument(
        "--token-file", metavar="PATH", help="Read the API token from this file (dev override)."
    )
    parent.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON instead of text."
    )
    parent.add_argument(
        "--verbose",
        "-v",
        action="count",
        default=0,
        help="Show core log output on stderr (-v for INFO, -vv for DEBUG).",
    )
    return parent


# --- command registration (each imports its handler lazily) ---------------------------------


def _register_daemon(
    sub: argparse._SubParsersAction[argparse.ArgumentParser], g: argparse.ArgumentParser
) -> None:
    start = sub.add_parser("start", help="Start the core daemon (prompt 16).", parents=[g])
    start.add_argument("--detach", action="store_true", help="Run in the background.")
    start.set_defaults(func=_lazy("daemon_cmd", "cmd_start"))

    stop = sub.add_parser("stop", help="Stop the core daemon (prompt 16).", parents=[g])
    stop.set_defaults(func=_lazy("daemon_cmd", "cmd_stop"))

    restart = sub.add_parser("restart", help="Restart the core daemon (prompt 16).", parents=[g])
    restart.add_argument("--detach", action="store_true", help="Run in the background.")
    restart.set_defaults(func=_lazy("daemon_cmd", "cmd_restart"))


def _register_status(
    sub: argparse._SubParsersAction[argparse.ArgumentParser], g: argparse.ArgumentParser
) -> None:
    status = sub.add_parser("status", help="Is core running? (does not start it)", parents=[g])
    status.set_defaults(func=_lazy("daemon_cmd", "cmd_status"))


def _register_operation(
    sub: argparse._SubParsersAction[argparse.ArgumentParser], g: argparse.ArgumentParser
) -> None:
    op = sub.add_parser(
        "operation",
        help="Call any registry operation by name (generic fallback).",
        parents=[g],
        description=(
            "Call a registry operation directly. The input is JSON on --json-input or stdin. "
            "This reaches every operation the daemon offers, including ones added after this CLI "
            "was released. 'oaken operation' with no name lists the operations."
        ),
    )
    op.add_argument("name", nargs="?", help="Operation name; omit to list operations.")
    op.add_argument(
        "--json-input",
        metavar="JSON",
        help="The operation input as a JSON object. Use '-' or omit to read stdin when piped.",
    )
    op.add_argument(
        "--yes",
        action="store_true",
        help="Confirm a mutating operation (the person asked for exactly this).",
    )
    op.set_defaults(func=_lazy("operation_cmd", "cmd_operation"))


def _register_mail(
    sub: argparse._SubParsersAction[argparse.ArgumentParser], g: argparse.ArgumentParser
) -> None:
    """Mail and read-state verbs: search, show, summarize, read/unread, undo, sync."""
    search = sub.add_parser("search", help="Keyword search over the index.", parents=[g])
    search.add_argument("query", help='Search terms: words, "phrases", -exclude, term*.')
    search.add_argument("--from", dest="from_", metavar="ADDR", help="Only this sender.")
    search.add_argument("--since", metavar="DATE", help="Only after this date (ISO 8601).")
    search.add_argument("--label", metavar="LABEL", help="Only messages carrying this label.")
    search.add_argument("--account", metavar="ID", help="Restrict to one account.")
    search.add_argument("--limit", type=int, metavar="N", help="Maximum results.")
    search.set_defaults(func=_lazy("mail_cmd", "cmd_search"))

    show = sub.add_parser(
        "show", help="Show one message (cleaned text, or --raw/--headers).", parents=[g]
    )
    show.add_argument("message_id", help="The message id.")
    grp = show.add_mutually_exclusive_group()
    grp.add_argument("--raw", action="store_true", help="Include the raw bytes (base64 in --json).")
    grp.add_argument("--headers", action="store_true", help="Show only the raw headers.")
    show.set_defaults(func=_lazy("mail_cmd", "cmd_show"))

    summarize = sub.add_parser(
        "summarize", help="Summarize messages with the local model.", parents=[g]
    )
    summarize.add_argument("ids", nargs="+", metavar="ID", help="One or more message ids.")
    summarize.add_argument(
        "--mark-read", action="store_true", help="Also mark the summarized messages read."
    )
    summarize.set_defaults(func=_lazy("mail_cmd", "cmd_summarize"))

    read = sub.add_parser("read", help="Mark messages read.", parents=[g])
    read.add_argument("ids", nargs="+", metavar="ID", help="One or more message ids.")
    read.add_argument("--yes", action="store_true", help="Confirm a bulk change (>25 messages).")
    read.set_defaults(func=_lazy("mail_cmd", "cmd_mark"), op="mark_read")

    unread = sub.add_parser("unread", help="Mark messages unread.", parents=[g])
    unread.add_argument("ids", nargs="+", metavar="ID", help="One or more message ids.")
    unread.add_argument("--yes", action="store_true", help="Confirm a bulk change (>25 messages).")
    unread.set_defaults(func=_lazy("mail_cmd", "cmd_mark"), op="mark_unread")

    undo = sub.add_parser("undo", help="Undo a read-state change from the audit log.", parents=[g])
    undo.add_argument("audit_id", help="The audit id to undo (see 'oaken audit').")
    undo.set_defaults(func=_lazy("mail_cmd", "cmd_undo"))

    sync = sub.add_parser("sync", help="Sync an account (or one folder) now.", parents=[g])
    sync.add_argument("--account", metavar="ID", help="Account to sync; default is all.")
    sync.add_argument("--folder", metavar="NAME", help="One folder; default is the whole account.")
    sync.add_argument("--wait", action="store_true", help="Stream job events until it finishes.")
    sync.set_defaults(func=_lazy("mail_cmd", "cmd_sync"))


def _register_system(
    sub: argparse._SubParsersAction[argparse.ArgumentParser], g: argparse.ArgumentParser
) -> None:
    """System, inspection and job verbs: pipeline, usage, plugins, accounts, audit, operations,
    reprocess, rebuild-index, ask, watch."""
    ask = sub.add_parser("ask", help="Answer a question from the mail (prompt 15).", parents=[g])
    ask.add_argument("question", help="The question to answer.")
    ask.set_defaults(func=_lazy("system_cmd", "cmd_ask"))

    pipeline = sub.add_parser(
        "pipeline", help="Pipeline status: stages, throughput, parked jobs.", parents=[g]
    )
    pipeline.add_argument(
        "subcommand", nargs="?", default="status", choices=["status"], help="status (default)."
    )
    pipeline.set_defaults(func=_lazy("system_cmd", "cmd_pipeline"))

    usage = sub.add_parser(
        "usage", help="Model calls and tokens per workspace, stage and model.", parents=[g]
    )
    usage.add_argument(
        "--since-hours", type=float, metavar="H", help="Only the last H hours of usage."
    )
    usage.add_argument("--workspace", metavar="NAME", help="Only this workspace.")
    usage.set_defaults(func=_lazy("system_cmd", "cmd_usage"))

    plugins = sub.add_parser(
        "plugins", help="List plugins; --check runs the health probe now.", parents=[g]
    )
    plugins.add_argument("--check", action="store_true", help="Run each plugin's health probe.")
    plugins.set_defaults(func=_lazy("system_cmd", "cmd_plugins"))

    accounts = sub.add_parser(
        "accounts", help="The configured accounts (never credentials).", parents=[g]
    )
    accounts.set_defaults(func=_lazy("system_cmd", "cmd_accounts"))

    audit = sub.add_parser("audit", help="The newest audit-log rows.", parents=[g])
    audit.add_argument(
        "--limit", type=int, default=50, metavar="N", help="How many rows (max 500)."
    )
    audit.add_argument("--op", metavar="OP", help="Only rows for this operation.")
    audit.set_defaults(func=_lazy("system_cmd", "cmd_audit"))

    operations = sub.add_parser(
        "operations", help="Every operation with its permission and MCP exposure.", parents=[g]
    )
    operations.set_defaults(func=_lazy("system_cmd", "cmd_operations"))

    reprocess = sub.add_parser(
        "reprocess", help="Bring one pipeline stage up to date.", parents=[g]
    )
    reprocess.add_argument(
        "stage", choices=["parse", "filter", "embed", "enrich"], help="The stage to reprocess."
    )
    reprocess.add_argument("--account", metavar="ID", help="Restrict to one account.")
    reprocess.set_defaults(func=_lazy("system_cmd", "cmd_reprocess"))

    rebuild = sub.add_parser(
        "rebuild-index", help="Rebuild the search index from the raw store.", parents=[g]
    )
    rebuild.add_argument("--account", metavar="ID", help="Restrict to one account.")
    rebuild.add_argument(
        "--force", action="store_true", help="Re-index even where the content hash is unchanged."
    )
    rebuild.set_defaults(func=_lazy("system_cmd", "cmd_rebuild_index"))

    watch = sub.add_parser("watch", help="Stream core events as lines (or --json).", parents=[g])
    watch.set_defaults(func=_lazy("system_cmd", "cmd_watch"))


def _lazy(module: str, func: str) -> Handler:
    """Return a handler that imports ``oaken_cli.commands.<module>.<func>`` only when called.

    This keeps ``--help`` and argument parsing free of command imports (core, httpx, rich), which
    is what holds the startup budget.
    """

    def run(args: argparse.Namespace) -> int:
        import importlib

        mod = importlib.import_module(f"oaken_cli.commands.{module}")
        handler: Handler = getattr(mod, func)
        return handler(args)

    return run


# --- dispatch --------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """Parse ``argv`` and run a command. Returns the process exit code.

    All expected failures are turned into a clean message on stderr and a stable exit code; an
    unexpected exception is allowed to propagate so it is visible in development.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(getattr(args, "verbose", 0))
    from oaken_cli.exits import CliError, ExitCode

    try:
        return int(args.func(args))
    except CliError as exc:
        print(f"{PROG}: {exc.message}", file=sys.stderr)
        return int(exc.code)
    except KeyboardInterrupt:
        return int(ExitCode.ERROR)


def _configure_logging(verbose: int) -> None:
    """Send core's structured logs to **stderr**, so stdout stays the machine channel ``--json``
    relies on. Quiet by default (only warnings and errors); ``-v`` raises it to INFO, ``-vv`` to
    DEBUG. Without this the CLI inherits structlog's unconfigured default, which prints to stdout
    and corrupts JSON output."""
    import sys

    from oaken.core.logging import configure_logging
    from oaken.core.settings import LogSettings

    settings = (
        LogSettings(level="DEBUG")
        if verbose >= 2
        else LogSettings(level="INFO")
        if verbose == 1
        else LogSettings(level="WARNING")
    )
    configure_logging(settings, stream=sys.stderr)


def _console_main() -> None:
    """Console-script shim: run :func:`main` and exit with its code."""
    raise SystemExit(main())


if __name__ == "__main__":  # pragma: no cover
    _console_main()
