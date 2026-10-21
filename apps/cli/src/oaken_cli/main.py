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
    parser = argparse.ArgumentParser(prog=PROG, description=DESCRIPTION, parents=[_globals(False)])
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    # Every subcommand inherits the global flags, so ``--json``/``--url``/``--token-file`` work
    # whether they come before or after the command name (argparse otherwise only accepts a
    # top-level option before the subcommand).
    g = _globals(True)
    _register_daemon(sub, g)
    _register_operation(sub, g)
    _register_status(sub, g)
    _register_mail(sub, g)
    _register_system(sub, g)
    _register_swarm(sub, g)
    _register_mcp(sub, g)
    _register_content(sub)
    _register_accounts(sub)
    _register_completion(sub, g)
    # Any registry operation without its own verb above is still reachable through the generic
    # ``oaken operation NAME --json '{...}'`` fallback, so a registry change never needs a release.
    return parser


def _globals(on_subcommand: bool) -> argparse.ArgumentParser:
    """A parent parser carrying the flags every command shares. ``add_help=False`` so it does not
    fight the real parser's ``-h``.

    The copy on a subcommand has no defaults (``SUPPRESS``): argparse applies a subparser's
    defaults after the top-level parse, which would otherwise overwrite ``oaken --token-file X
    status`` with ``None``. The top-level copy carries the real defaults."""
    parent = argparse.ArgumentParser(add_help=False)
    unset = argparse.SUPPRESS if on_subcommand else None
    g = parent.add_argument_group("connection")
    g.add_argument(
        "--url", metavar="URL", default=unset, help="Base URL of a running core (dev override)."
    )
    g.add_argument(
        "--token-file",
        metavar="PATH",
        default=unset,
        help="Read the API token from this file (dev override).",
    )
    parent.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS if on_subcommand else False,
        help="Emit machine-readable JSON instead of text.",
    )
    parent.add_argument(
        "--verbose",
        "-v",
        action="count",
        default=argparse.SUPPRESS if on_subcommand else 0,
        help="Show core log output on stderr (-v for INFO, -vv for DEBUG).",
    )
    return parent


# --- command registration (each imports its handler lazily) ---------------------------------


def _register_daemon(
    sub: argparse._SubParsersAction[argparse.ArgumentParser], g: argparse.ArgumentParser
) -> None:
    start = sub.add_parser(
        "start", help="Start the core daemon (foreground; --detach for background).", parents=[g]
    )
    start.add_argument("--detach", action="store_true", help="Run in the background.")
    start.set_defaults(func=_lazy("daemon_cmd", "cmd_start"))

    stop = sub.add_parser("stop", help="Stop the core daemon.", parents=[g])
    stop.set_defaults(func=_lazy("daemon_cmd", "cmd_stop"))

    restart = sub.add_parser("restart", help="Restart the core daemon.", parents=[g])
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
        "--describe",
        action="store_true",
        help="Show the operation's summary and input and output fields instead of calling it.",
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
    search = sub.add_parser(
        "search",
        help="Search the mail (keyword and meaning, merged).",
        parents=[g],
        description=(
            "Hybrid search: keyword and vector results merged by rank. Operators in the query: "
            'from:, before:, after:, label:, in:, -term, "phrase", term*. Each hit says which '
            "retriever found it; if the vector side was down the result says so."
        ),
    )
    search.add_argument("query", help="Search terms and operators.")
    search.add_argument("--from", dest="from_", metavar="ADDR", help="Only this sender.")
    search.add_argument("--since", metavar="DATE", help="Only after this date (ISO 8601).")
    search.add_argument("--before", metavar="DATE", help="Only before this date (ISO 8601).")
    search.add_argument("--label", metavar="LABEL", help="Only messages carrying this label.")
    search.add_argument("--account", metavar="ID", help="Restrict to one account.")
    search.add_argument(
        "--disposition", metavar="D", help="inbox, archive or discarded (discarded is explicit)."
    )
    search.add_argument("--category", metavar="C", help="Only this triage category.")
    search.add_argument("--min-priority", type=int, metavar="N", help="Only this priority or more.")
    search.add_argument("--unread", action="store_true", help="Only unread messages.")
    search.add_argument(
        "--keyword", action="store_true", help="Keyword search only (no model, no vectors)."
    )
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
    sync.add_argument("--wait", action="store_true", help="Wait until the sync job finishes.")
    sync.add_argument(
        "--timeout", type=float, default=600.0, metavar="S", help="Give up waiting after S seconds."
    )
    sync.set_defaults(func=_lazy("mail_cmd", "cmd_sync"))


def _register_system(
    sub: argparse._SubParsersAction[argparse.ArgumentParser], g: argparse.ArgumentParser
) -> None:
    """System, inspection and job verbs: pipeline, usage, plugins, accounts, audit, operations,
    reprocess, rebuild-index, ask, watch."""
    ask = sub.add_parser(
        "ask", help="Answer a question from the mail, citing the messages.", parents=[g]
    )
    ask.add_argument("question", help="The question to answer.")
    ask.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        metavar="S",
        help="Give up waiting for the answer after S seconds (a local model can be slow).",
    )
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


def _register_mcp(
    sub: argparse._SubParsersAction[argparse.ArgumentParser], g: argparse.ArgumentParser
) -> None:
    mcp = sub.add_parser(
        "mcp",
        help="Bridge an MCP client that speaks stdio to the MCP server in core.",
        parents=[g],
        description=(
            "Reads MCP messages on stdin and forwards them to the MCP server inside the running "
            "core, writing its answers to stdout. It adds nothing: the tools, the confirmation "
            "step and the limits are core's. If core is not running, tool calls answer with a "
            "core_not_running error. It never starts core. Point a stdio-only client (Claude "
            "Desktop) at 'oaken mcp'."
        ),
    )
    mcp.add_argument(
        "--timeout", type=float, default=60.0, metavar="SECONDS", help="Give up on a call after."
    )
    mcp.add_argument(
        "--agent",
        action="store_true",
        help="Speak to the swarm door as an agent (token from OAKEN_AGENT_TOKEN).",
    )
    mcp.add_argument("--agent-token-file", metavar="PATH", help="Read the agent token from a file.")
    mcp.set_defaults(func=_lazy("mcp_cmd", "cmd_mcp"))


def _register_swarm(
    sub: argparse._SubParsersAction[argparse.ArgumentParser], g: argparse.ArgumentParser
) -> None:
    """Agents (managed by a person) and the job protocol (worked by an agent)."""
    agents = sub.add_parser(
        "agents",
        help="Create, list and revoke external agents (the swarm).",
        parents=[g],
        description="Agents lease jobs from the queue with a token of their own (the swarm page).",
    )
    actions = agents.add_subparsers(dest="action", metavar="ACTION")
    actions.required = True
    create = actions.add_parser(
        "create", parents=[_globals(True)], help="Register an agent and print its token once."
    )
    create.add_argument("label", help="A name for the agent.")
    create.add_argument(
        "--kind", action="append", required=True, metavar="KIND", help="A job kind it may lease."
    )
    create.add_argument(
        "--account", action="append", metavar="ID", help="Limit it to this account (repeatable)."
    )
    create.add_argument("--max-leases", type=int, metavar="N", help="Jobs it may hold at once.")
    create.add_argument("--max-per-hour", type=int, metavar="N", help="Jobs it may claim per hour.")
    lst = actions.add_parser("list", parents=[_globals(True)], help="List agents.")
    lst.add_argument("--all", action="store_true", help="Include revoked agents.")
    revoke = actions.add_parser(
        "revoke", parents=[_globals(True)], help="Revoke an agent; its jobs go back on the queue."
    )
    revoke.add_argument("agent_id", help="The agent id (see 'oaken agents list').")
    agents.set_defaults(func=_lazy("agent_cmd", "cmd_agents"))

    jobs = sub.add_parser(
        "jobs",
        help="Lease and finish jobs as an agent (OAKEN_AGENT_TOKEN).",
        parents=[g],
        description=(
            "The agent side of the queue. Authenticated by the agent's own token "
            "(OAKEN_AGENT_TOKEN or --agent-token-file); the API token is not used."
        ),
    )
    jobs.add_argument("--agent-token-file", metavar="PATH", help="Read the agent token from PATH.")
    verbs = jobs.add_subparsers(dest="action", metavar="ACTION")
    verbs.required = True
    claim = verbs.add_parser("claim", parents=[_globals(True)], help="Lease the next job.")
    claim.add_argument(
        "--kind", action="append", default=[], metavar="KIND", help="Only this kind."
    )
    beat = verbs.add_parser("heartbeat", parents=[_globals(True)], help="Extend a lease.")
    beat.add_argument("job_id")
    done = verbs.add_parser("complete", parents=[_globals(True)], help="Hand back a result.")
    done.add_argument("job_id")
    done.add_argument("--result", metavar="JSON", help="The result object; '-' reads stdin.")
    done.add_argument("--produced-by", metavar="NAME", help="The model or tool that made it.")
    fail = verbs.add_parser("fail", parents=[_globals(True)], help="Report an error.")
    fail.add_argument("job_id")
    fail.add_argument("--error", required=True, metavar="TEXT", help="What went wrong (no mail).")
    fail.add_argument("--permanent", action="store_true", help="Do not retry it.")
    jobs.set_defaults(func=_lazy("agent_cmd", "cmd_jobs"))


def _register_content(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Enrichment results and Sieve scripts."""
    enrich = sub.add_parser(
        "enrich",
        help="Run or show the enrichers (summary, triage, actions, inspector) on a message.",
        parents=[_globals(True)],
    )
    verbs = enrich.add_subparsers(dest="action", metavar="ACTION")
    verbs.required = True
    run_ = verbs.add_parser(
        "run", parents=[_globals(True)], help="Run the enrichers on one message now."
    )
    run_.add_argument("message_id")
    run_.add_argument("enricher", nargs="*", help="Only these enrichers (default: all).")
    run_.add_argument(
        "--force", action="store_true", help="Run even if a current result is stored."
    )
    show = verbs.add_parser("show", parents=[_globals(True)], help="Show the stored results.")
    show.add_argument("message_id")
    show.add_argument("enricher", nargs="*", help="One enricher (default: all stored).")
    enrich.set_defaults(func=_lazy("content_cmd", "cmd_enrich"))

    sieve = sub.add_parser(
        "sieve",
        help="Check, try and list Sieve filter scripts; refilter stored mail.",
        parents=[_globals(True)],
        description="Scripts are files you edit; these commands never change them.",
    )
    acts = sieve.add_subparsers(dest="action", metavar="ACTION")
    acts.required = True
    check = acts.add_parser("check", parents=[_globals(True)], help="Compile a script file.")
    check.add_argument("file")
    test = acts.add_parser(
        "test",
        parents=[_globals(True)],
        help="Run a script on one message and show each effect. Stores nothing.",
    )
    test.add_argument("file")
    which = test.add_mutually_exclusive_group(required=True)
    which.add_argument("--message", metavar="ID", help="A stored message.")
    which.add_argument("--eml", metavar="PATH", help="A message file.")
    acts.add_parser("list", parents=[_globals(True)], help="The scripts core would load.")
    refilter = acts.add_parser(
        "refilter", parents=[_globals(True)], help="Re-run the scripts over stored mail (a job)."
    )
    refilter.add_argument("--account", metavar="ID", help="Restrict to one account.")
    sieve.set_defaults(func=_lazy("content_cmd", "cmd_sieve"))


def _register_accounts(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """The account wizard and credentials."""
    admin = sub.add_parser(
        "admin",
        help="Administration: add and inspect accounts.",
        parents=[_globals(True)],
    )
    area = admin.add_subparsers(dest="area", metavar="AREA")
    area.required = True
    account = area.add_parser("account", parents=[_globals(True)], help="Mail accounts.")
    acts = account.add_subparsers(dest="action", metavar="ACTION")
    acts.required = True
    acts.add_parser("list", parents=[_globals(True)], help="List accounts.")
    show = acts.add_parser("show", parents=[_globals(True)], help="One account and its settings.")
    show.add_argument("account_id")
    add = acts.add_parser(
        "add",
        parents=[_globals(True)],
        help="Add an account (asks questions on a terminal).",
        description=(
            "Asks for the provider, an id, a name and the provider's own non-secret settings, then "
            "stores them. The credential is stored afterwards with 'oaken auth'. Give "
            "--provider, --id and --name (and --set) to skip the questions."
        ),
    )
    add.add_argument("--provider", metavar="NAME", help="gmail, smtp or imap.")
    add.add_argument("--id", dest="account_id", metavar="ID", help="a-z, 0-9, - and _.")
    add.add_argument("--name", metavar="TEXT", help="A display name.")
    add.add_argument(
        "--set", action="append", metavar="KEY=VALUE", help="A provider setting (repeatable)."
    )
    admin.set_defaults(func=_lazy("account_cmd", "cmd_admin"))

    auth = sub.add_parser(
        "auth",
        help="Store an account's credential (Gmail consent, SMTP or IMAP password).",
        parents=[_globals(True)],
        description=(
            "gmail opens Google's consent and waits; smtp and imap read the password without "
            "echo. Nothing is ever printed, taken as an argument, or written to disk: the "
            "credential goes to the OS keyring."
        ),
    )
    auth.add_argument("provider", choices=["gmail", "smtp", "imap"])
    auth.add_argument("--account", metavar="ID", help="The account (default: the only one).")
    auth.add_argument("--check", action="store_true", help="Check the stored credential only.")
    auth.add_argument(
        "--password-stdin", action="store_true", help="Read the password from one line of stdin."
    )
    auth.add_argument("--no-browser", action="store_true", help="Only print the Gmail address.")
    auth.add_argument(
        "--timeout", type=float, default=300.0, metavar="S", help="Gmail: give up after S seconds."
    )
    auth.set_defaults(func=_lazy("account_cmd", "cmd_auth"))


def _register_completion(
    sub: argparse._SubParsersAction[argparse.ArgumentParser], g: argparse.ArgumentParser
) -> None:
    completion = sub.add_parser(
        "completion",
        help="Print a shell completion script (bash, zsh or fish).",
        description='Install with, for example: eval "$(oaken completion bash)".',
    )
    completion.add_argument("shell", choices=["bash", "zsh", "fish"])
    completion.set_defaults(func=_lazy("completion", "cmd_completion"))


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
