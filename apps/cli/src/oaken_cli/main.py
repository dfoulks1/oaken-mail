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
DESCRIPTION = """\
Oaken Mail — a local-first, AI-native mail client.

Mail is pulled to your machine, filtered by your Sieve rules, and processed by a local model
at ingest time, so search and question-answering are cheap. Nothing leaves your machine unless
you configure a remote endpoint. This CLI is a thin client: every command is one call to the
local core daemon over 127.0.0.1 — it holds no logic of its own, and never starts core for you.
"""

EPILOG = """\
getting started:
  oaken help                  show this overview (also: oaken --help)
  oaken help COMMAND          help for one command (e.g. oaken help sync) — same as COMMAND --help
  oaken start                 run the core daemon in the background (--fg to stay attached)
  oaken admin account add     add a mail account (a wizard, or --provider/--id/--set)
  oaken gmail auth            sign an account in (also imap, smtp: any plugin's functions are
                              'oaken PLUGIN VERB'; 'oaken operation --describe NAME' lists flags)
  oaken sync --wait           pull mail now and wait for it to finish
  oaken search "quokkas"      search; oaken ask "when is the board meeting?" to ask
  oaken show MESSAGE_ID       read one message (adds --mark-read via 'oaken read')

command groups:
  daemon        start · stop · restart · reload · status
  mail          search · ask · show · thread · summarize · read · unread · undo · sync
  organize      mailbox · label · folder · star · sieve · schedule · reprocess · rebuild-index
  queue         jobs · pipeline · watch
  accounts      admin · auth · accounts
  system        status · usage · plugins · config · settings · audit · operations
  storage       storage · vacuum · cache · events
  agents        agents · jobs (claim/heartbeat/complete/fail)
  tools         operation · mcp · completion · help

anything else:
  Every capability the daemon offers is reachable without a dedicated verb via the generic
  fallback:  oaken operation NAME --json '{...}'   (and 'oaken operation' lists them all).
  Add --json to any command for machine-readable output.

exit codes:
  0 ok · 1 error · 2 usage · 3 core not running · 4 auth · 5 needs confirmation (re-run with --yes)

docs: https://dfoulks1.github.io/oaken-mail/   ·   'oaken help COMMAND' or 'oaken COMMAND --help'.
"""


def build_parser() -> argparse.ArgumentParser:
    """Construct the full parser. Kept import-light so ``--help`` is cheap."""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[_globals(False)],
    )
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
    _register_organize(sub, g)
    _register_system(sub, g)
    _register_admin(sub, g)
    _register_swarm(sub, g)
    _register_mcp(sub, g)
    _register_content(sub)
    _register_accounts(sub)
    _register_schedule(sub)
    _register_completion(sub, g)

    # `oaken help [COMMAND]` — the same as --help, and 'oaken help sync' == 'oaken sync --help'.
    # Handled in main() because it needs the parser itself; the marker is picked up there.
    helpp = sub.add_parser("help", help="Show this help, or help for one COMMAND.")
    helpp.add_argument("topic", nargs="?", metavar="COMMAND", help="A command to show help for.")
    helpp.set_defaults(_help=True)
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
    def where(p: argparse.ArgumentParser) -> None:
        """The background is the default; ``--fg`` stays attached. ``--detach`` is the old spelling
        of the default and still accepted, so scripts written for it keep working."""
        group = p.add_mutually_exclusive_group()
        group.add_argument(
            "--fg",
            action="store_true",
            help="Run in the foreground, logging to this terminal (default: the background).",
        )
        group.add_argument("--detach", action="store_true", help=argparse.SUPPRESS)

    def patience(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--grace",
            type=float,
            default=None,
            metavar="SECONDS",
            help="How long core gets to finish what it is doing before it is killed (default 40; "
            "0 kills it at once).",
        )
        p.add_argument(
            "--no-kill",
            action="store_true",
            help="Report an error if core does not stop in time, instead of killing it.",
        )

    start = sub.add_parser(
        "start",
        help="Start the core daemon (in the background; --fg to stay attached).",
        parents=[g],
    )
    where(start)
    start.set_defaults(func=_lazy("daemon_cmd", "cmd_start"))

    stop = sub.add_parser(
        "stop",
        help="Stop the core daemon, killing it if it will not stop in time.",
        parents=[g],
        description=(
            "Asks core to stop, waits for it to finish what it is doing (up to --grace seconds), "
            "and kills it if it is still running. Core's work is a durable queue, so a killed core "
            "loses nothing: unfinished jobs are retried when it starts again."
        ),
    )
    patience(stop)
    stop.set_defaults(func=_lazy("daemon_cmd", "cmd_stop"))

    restart = sub.add_parser(
        "restart",
        help="Restart the core daemon (in the background; --fg to stay attached).",
        parents=[g],
    )
    where(restart)
    patience(restart)
    restart.set_defaults(func=_lazy("daemon_cmd", "cmd_restart"))

    reload = sub.add_parser(
        "reload",
        help="Make the running core re-read config.toml, without a restart.",
        parents=[g],
        description=(
            "Signals the running core (SIGHUP) to re-read its configuration and rebuild itself "
            "in the same process. An invalid file changes nothing. The web UI follows by itself."
        ),
    )
    reload.set_defaults(func=_lazy("daemon_cmd", "cmd_reload"))


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
            "was released. 'oaken operation' with no name lists the operations. The operation's "
            "own fields can be given as flags built from its input schema (see --describe), e.g. "
            "'oaken operation list_messages --limit 5'; 'oaken PLUGIN VERB' is a shorthand for "
            "'oaken operation PLUGIN.VERB'."
        ),
    )
    op.add_argument("name", nargs="?", help="Operation name; omit to list operations.")
    op.add_argument(
        "--json-input",
        metavar="JSON",
        help="The operation input as a JSON object. Use '-' or omit to read stdin when piped.",
    )
    op.add_argument("--no-browser", action="store_true", help="Print an address, never open it.")
    op.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        metavar="S",
        help="When an operation waits on the person (a sign-in), give up after S seconds.",
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
    search.add_argument(
        "--offset", type=int, default=0, metavar="N", help="Skip this many (paging)."
    )
    search.add_argument("--starred", action="store_true", help="Only starred messages.")
    search.add_argument(
        "--has-attachment", action="store_true", help="Only messages with an attachment."
    )
    search.set_defaults(func=_lazy("mail_cmd", "cmd_search"))

    show = sub.add_parser(
        "show", help="Show one message (cleaned text, or --raw/--headers).", parents=[g]
    )
    show.add_argument("message_id", help="The message id.")
    grp = show.add_mutually_exclusive_group()
    grp.add_argument("--raw", action="store_true", help="Include the raw bytes (base64 in --json).")
    grp.add_argument("--headers", action="store_true", help="Show only the raw headers.")
    show.set_defaults(func=_lazy("mail_cmd", "cmd_show"))

    thread = sub.add_parser(
        "thread",
        help="Show a conversation as a tree: who replied to whom (* marks unread).",
        parents=[g],
    )
    thread.add_argument("message_id", help="Any message in the conversation.")
    thread.add_argument("--limit", type=int, metavar="N", help="Most messages to show.")
    thread.set_defaults(func=_lazy("mail_cmd", "cmd_thread"))

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

    sync = sub.add_parser(
        "sync",
        help="Sync every account on every provider now (or one account, or one folder).",
        parents=[g],
    )
    sync.add_argument("--account", metavar="ID", help="Account to sync; default is all.")
    sync.add_argument("--folder", metavar="NAME", help="One folder; default is the whole account.")
    sync.add_argument("--wait", action="store_true", help="Wait until the sync job finishes.")
    sync.add_argument(
        "--timeout", type=float, default=600.0, metavar="S", help="Give up waiting after S seconds."
    )
    sync.set_defaults(func=_lazy("mail_cmd", "cmd_sync"))


def _register_organize(
    sub: argparse._SubParsersAction[argparse.ArgumentParser], g: argparse.ArgumentParser
) -> None:
    """How mail is organized. Labels, folders and stars share one grammar; a mailbox is read."""
    kinds = {
        "label": (
            "Label messages (tags for finding mail).",
            "oaken label list | create NAME | add NAME ID... | remove NAME ID...",
        ),
        "folder": (
            "File messages into folders under Inbox.",
            "oaken folder list | create PATH | add PATH ID... (or move) | remove ID... "
            "(back to the Inbox)",
        ),
        "star": (
            "Star messages (written back to the mailbox where it can be).",
            "oaken star list | add ID... | remove ID...   (oaken unstar ID... is remove)",
        ),
    }
    for noun, (helptext, usage) in kinds.items():
        p = sub.add_parser(noun, help=helptext, parents=[g], description=f"{helptext}\n\n{usage}")
        p.add_argument("action", choices=["list", "create", "add", "remove", "move"], help=usage)
        p.add_argument("rest", nargs="*", metavar="ARG", help="A name or path, then message ids.")
        p.add_argument("--yes", action="store_true", help="Confirm a change to over 25 messages.")
        if noun == "star":
            p.add_argument("--limit", type=int, metavar="N", help="With list: most to show.")
        p.set_defaults(func=_lazy("organize_cmd", "cmd_mark"), noun=noun)

    unstar = sub.add_parser("unstar", help="Remove the star from messages.", parents=[g])
    unstar.add_argument("ids", nargs="+", metavar="ID", help="One or more message ids.")
    unstar.add_argument("--yes", action="store_true", help="Confirm a change to over 25 messages.")
    unstar.set_defaults(func=_lazy("organize_cmd", "cmd_unstar"))

    mailbox = sub.add_parser(
        "mailbox",
        help="Look at a mailbox: the folder tree, counts, a listing, or why a message is filed.",
        parents=[g],
        description=(
            "tree (default): Inbox, folders, labels and Discarded with counts. counts: the "
            "sidebar's numbers. messages: list what is in a folder or under a label. rules ID: "
            "the labels and flags on a message and the Sieve rule behind each. --account limits "
            "counts and messages to one account."
        ),
    )
    mailbox.add_argument(
        "action",
        nargs="?",
        default="tree",
        choices=["tree", "counts", "messages", "rules"],
        help="tree (default), counts, messages or rules.",
    )
    mailbox.add_argument("id", nargs="?", help="With rules: the message id.")
    mailbox.add_argument("--account", metavar="ID", help="Only this account.")
    mailbox.add_argument("--folder", metavar="PATH", help="With messages: only this folder.")
    mailbox.add_argument("--label", metavar="NAME", help="With messages: only this label.")
    mailbox.add_argument(
        "--disposition",
        choices=["inbox", "filed", "discarded", "any"],
        help="With messages: where to look (default: filed for --folder, any for --label, "
        "else inbox).",
    )
    mailbox.add_argument("--unread", action="store_true", help="With messages: only unread.")
    mailbox.add_argument("--starred", action="store_true", help="With messages: only starred.")
    mailbox.add_argument("--limit", type=int, metavar="N", help="With messages: most to show.")
    mailbox.add_argument("--offset", type=int, default=0, metavar="N", help="Skip this many.")
    mailbox.set_defaults(func=_lazy("organize_cmd", "cmd_mailbox"))


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
        "pipeline",
        help="Pipeline status, and pausing the model-bound stages.",
        parents=[g],
        description=(
            "Default shows status. 'pause'/'resume' stop and restart the model-bound ingest "
            "stages (embed, enrich), or just one (oaken pipeline pause enrich) — mail keeps "
            "landing and being filtered while paused. 'limits enrich 2' changes how many jobs a "
            "stage runs at once, live. 'parked' re-queues failed ingest jobs."
        ),
    )
    pipeline.add_argument(
        "subcommand",
        nargs="?",
        default="status",
        choices=["status", "pause", "resume", "limits", "parked"],
        help="status (default), pause, resume, limits, or parked.",
    )
    pipeline.add_argument(
        "words",
        nargs="*",
        metavar="STAGE",
        help="With pause/resume: one stage (embed or enrich), or none for all. "
        "With limits: a stage and a number.",
    )
    pipeline.add_argument("--why", default="", metavar="TEXT", help="A note recorded with a pause.")
    pipeline.add_argument(
        "--retry", metavar="JOB_ID", help="With 'parked': re-queue this one parked job."
    )
    pipeline.add_argument(
        "--all", action="store_true", help="With 'parked': re-queue every parked ingest job."
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
        "plugins",
        help="List plugins, and control one: enable, disable, reload, config, check, set.",
        parents=[g],
        description=(
            "Plugin control is core's, all here. Default lists plugins; --check runs the health "
            "probe now. 'disable' stops a plugin until 'enable' (or a restart; "
            "`enabled = false` in [plugins.NAME] makes it last). 'reload' restarts one on the "
            "current config.toml. 'config NAME' shows its settings. 'set NAME KEY=VALUE ...' "
            "changes them (--unset KEY, --replace, or --file FILE for TOML; comments in "
            "config.toml are kept, secrets are refused). 'check NAME [KEY=VALUE ...]' tries "
            "settings on a throwaway instance and writes nothing."
        ),
    )
    plugins.add_argument(
        "subcommand",
        nargs="?",
        default="list",
        choices=["list", "enable", "disable", "reload", "config", "check", "set"],
        help="list (default), enable, disable, reload, config, check or set.",
    )
    plugins.add_argument("name", nargs="?", help="The plugin (gmail) or one account (gmail@work).")
    plugins.add_argument("pairs", nargs="*", metavar="KEY=VALUE", help="With set/check: settings.")
    plugins.add_argument("--check", action="store_true", help="Run each plugin's health probe.")
    plugins.add_argument(
        "--unset", action="append", metavar="KEY", help="With set/check: remove a setting."
    )
    plugins.add_argument(
        "--replace", action="store_true", help="With set/check: KEY=VALUE is the whole table."
    )
    plugins.add_argument(
        "--file", metavar="FILE", help="With set/check: the plugin's settings as TOML (- is stdin)."
    )
    plugins.set_defaults(func=_lazy("system_cmd", "cmd_plugins"))

    config = sub.add_parser(
        "config",
        help="Show, test, save or reload config.toml through core.",
        parents=[g],
        description=(
            "'show' prints config.toml. 'test FILE' checks an edit without saving it, 'save FILE' "
            "validates and saves it (the old file is kept as a timestamped .bak), 'reload' applies "
            "the saved file to the running core. FILE may be - for standard input."
        ),
    )
    config.add_argument(
        "subcommand", nargs="?", default="show", choices=["show", "test", "save", "reload"]
    )
    config.add_argument("file", nargs="?", help="With test/save: the TOML to use, or - for stdin.")
    config.set_defaults(func=_lazy("system_cmd", "cmd_config"))

    accounts = sub.add_parser(
        "accounts", help="The configured accounts (never credentials).", parents=[g]
    )
    accounts.set_defaults(func=_lazy("system_cmd", "cmd_accounts"))

    audit = sub.add_parser("audit", help="The newest audit-log rows.", parents=[g])
    audit.add_argument(
        "--limit", type=int, default=50, metavar="N", help="How many rows (max 500)."
    )
    audit.add_argument("--op", metavar="OP", help="Only rows for this operation.")
    audit.add_argument("--since", metavar="DATE", help="Nothing older than this (ISO 8601).")
    audit.add_argument("--cursor", metavar="C", help="The next page: the cursor the last one gave.")
    audit.add_argument("--all", action="store_true", help="Page through every row, not just one.")
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
    reprocess.add_argument(
        "--since", metavar="YYYY-MM-DD", help="embed and enrich: mail dated on or after this day."
    )
    reprocess.add_argument(
        "--limit", type=int, metavar="N", help="embed and enrich: just the newest N messages."
    )
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


def _register_admin(
    sub: argparse._SubParsersAction[argparse.ArgumentParser], g: argparse.ArgumentParser
) -> None:
    """Looking after a running core: settings, storage, vacuum, the cache, the event history."""
    settings = sub.add_parser(
        "settings",
        help="Show, check or change core's own settings (index, retrieval, ai, sync, ...).",
        parents=[g],
        description=(
            "'settings' lists every section; 'settings SECTION' one. 'settings set SECTION "
            "KEY=VALUE ...' saves a change (--unset KEY goes back to the default) to config.toml, "
            "keeping its comments; 'settings check ...' tests it without saving. Saved settings "
            "reach core on 'oaken reload' (or add --reload); the answer says which need more. "
            "An environment variable that overrides a key is reported."
        ),
    )
    settings.add_argument(
        "words", nargs="*", metavar="[get|check|set] [SECTION] [KEY=VALUE ...]",
        help="The action (default get), the section, then settings.",
    )  # fmt: skip
    settings.add_argument(
        "--unset", action="append", metavar="KEY", help="Remove a setting: back to its default."
    )
    settings.add_argument(
        "--reload", action="store_true", help="With set: reload core on the change."
    )
    settings.set_defaults(func=_lazy("admin_cmd", "cmd_settings"))

    storage = sub.add_parser("storage", help="Where the data is and how big it is.", parents=[g])
    storage.set_defaults(func=_lazy("admin_cmd", "cmd_storage"))

    vacuum = sub.add_parser(
        "vacuum",
        help="Give back the space deleted mail left in the databases.",
        parents=[g],
        description=(
            "Rewrites raw.db and index.db. One at a time, and not while an index rebuild runs. "
            "It holds each database's write lock while it works (ingest waits, reads go on) and "
            "needs about the database's own size free on the disk."
        ),
    )
    vacuum.add_argument("--wait", action="store_true", help="Wait for it, and show the sizes.")
    vacuum.add_argument(
        "--timeout",
        type=float,
        default=3600.0,
        metavar="S",
        help="Give up waiting after S seconds.",
    )
    vacuum.set_defaults(func=_lazy("admin_cmd", "cmd_vacuum"))

    cache = sub.add_parser(
        "cache",
        help="The enrichment cache: how it is doing, or clear it.",
        parents=[g],
        description=(
            "'cache' (or 'cache stats') shows what it holds and how often it saved a model call. "
            "'cache clear' deletes cached answers (--task TASK for one task): mail and the index "
            "are untouched, and cleared answers are recomputed on demand."
        ),
    )
    cache.add_argument("action", nargs="?", default="stats", choices=["stats", "clear"])
    cache.add_argument("--task", metavar="TASK", help="With clear: only this task's answers.")
    cache.add_argument("--yes", action="store_true", help="With clear: do not ask.")
    cache.set_defaults(func=_lazy("admin_cmd", "cmd_cache"))

    events = sub.add_parser(
        "events",
        help="Recent events from core's event log (--follow keeps watching).",
        parents=[g],
        description=(
            "What core published recently, including before it was last restarted: ids, states "
            "and numbers, never mail text. --follow prints the history and then the live stream."
        ),
    )
    events.add_argument(
        "--tail", "-n", type=int, default=50, metavar="N", help="How many (default 50, max 1000)."
    )
    events.add_argument("--kind", action="append", metavar="KIND", help="Only this kind of event.")
    events.add_argument("--since", metavar="DATE", help="Nothing older than this (ISO 8601).")
    events.add_argument("--follow", "-f", action="store_true", help="Then keep watching.")
    events.set_defaults(func=_lazy("admin_cmd", "cmd_events"))


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
            "Desktop) at 'oaken mcp'. 'oaken mcp expose' lists which operations MCP clients may "
            "call; 'oaken mcp expose NAME on|off|default' (--plugin for a plugin's operations) "
            "changes it."
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
    mcp.add_argument(
        "action",
        nargs="?",
        choices=["expose"],
        help="expose: choose which operations MCP clients may call (bare 'oaken mcp' is the "
        "bridge).",
    )
    mcp.add_argument("target", nargs="?", metavar="NAME", help="With expose: an operation name.")
    mcp.add_argument(
        "state",
        nargs="?",
        choices=["on", "off", "default"],
        help="With expose NAME: on, off, or default (the operation's own).",
    )
    mcp.add_argument(
        "--plugin",
        action="store_true",
        help="With expose: NAME is a plugin (all of its operations).",
    )
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
        help="The durable job queue: list, show, cancel, retry; or work it as an agent.",
        parents=[g],
        description=(
            "Inspect and steer the durable queue (list, show, cancel, retry) with your API token. "
            "The agent protocol (claim, heartbeat, complete, fail) is authenticated by the agent's "
            "own token (OAKEN_AGENT_TOKEN or --agent-token-file) instead."
        ),
    )
    jobs.add_argument("--agent-token-file", metavar="PATH", help="Read the agent token from PATH.")
    verbs = jobs.add_subparsers(dest="action", metavar="ACTION")
    verbs.required = True

    # The person's view of the queue (API token).
    jlist = verbs.add_parser("list", parents=[_globals(True)], help="List jobs in the queue.")
    jlist.add_argument(
        "--status",
        choices=["queued", "running", "succeeded", "failed", "cancelled"],
        help="Only jobs in this state.",
    )
    jlist.add_argument("--kind", metavar="KIND", help="Only this kind, e.g. ingest.enrich.")
    jlist.add_argument("--account", metavar="ID", help="Only this account.")
    jlist.add_argument("--limit", type=int, default=50, help="How many (newest first, max 500).")
    jshow = verbs.add_parser("show", parents=[_globals(True)], help="Show one job in full.")
    jshow.add_argument("job_id")
    jcancel = verbs.add_parser(
        "cancel",
        parents=[_globals(True)],
        help="Cancel a queued job (or ask a running one to stop).",
    )
    jcancel.add_argument("job_id")
    jretry = verbs.add_parser(
        "retry", parents=[_globals(True)], help="Re-queue a failed or cancelled job to run now."
    )
    jretry.add_argument("job_id")

    # The agent protocol (agent token).
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
    get = acts.add_parser(
        "get", parents=[_globals(True)], help="Print one script's text (to edit it)."
    )
    get.add_argument("name", help="The script file name, e.g. 10-lists.sieve.")
    save = acts.add_parser(
        "save",
        parents=[_globals(True)],
        help="Save a script so it runs on the next filter pass (refused unless it compiles).",
        description=(
            "Compiles the script first and writes it only if it compiles, atomically and "
            "owner-only. Run order is by name, so a new script is best named NN-name.sieve; "
            "'oaken sieve save --suggest-name' prints the next free NN."
        ),
    )
    save.add_argument("name", nargs="?", help="The script file name (e.g. 20-work.sieve).")
    save.add_argument("file", nargs="?", help="The script file to read ('-' reads stdin).")
    save.add_argument(
        "--suggest-name",
        action="store_true",
        help="Print the next free NN-prefixed name and exit (give a base with --base).",
    )
    save.add_argument(
        "--base", metavar="NAME", default="filter", help="Base for --suggest-name (default filter)."
    )
    delete = acts.add_parser("delete", parents=[_globals(True)], help="Remove one script.")
    delete.add_argument("name", help="The script file name.")
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
            "stores them. The credential is stored afterwards with 'oaken PROVIDER auth'. Give "
            "--provider, --id and --name (and --set) to skip the questions."
        ),
    )
    add.add_argument("--provider", metavar="NAME", help="gmail, smtp or imap.")
    add.add_argument("--id", dest="account_id", metavar="ID", help="a-z, 0-9, - and _.")
    add.add_argument("--name", metavar="TEXT", help="A display name.")
    add.add_argument(
        "--set", action="append", metavar="KEY=VALUE", help="A provider setting (repeatable)."
    )
    add.add_argument(
        "--receive-only",
        dest="receive_only",
        action="store_true",
        default=None,
        help="Listen but never send (the default for a new Gmail account until send is turned on).",
    )
    set_ = acts.add_parser(
        "set",
        parents=[_globals(True)],
        help="Change an account's non-secret settings, or its receive-only switch.",
        description=(
            "Merges --set KEY=VALUE into the account's config and/or flips the receive-only "
            "switch. Credentials are never set here (use 'oaken PROVIDER auth')."
        ),
    )
    set_.add_argument("account_id")
    set_.add_argument(
        "--set", action="append", metavar="KEY=VALUE", help="A setting to change (repeatable)."
    )
    ro = set_.add_mutually_exclusive_group()
    ro.add_argument(
        "--receive-only",
        dest="receive_only",
        action="store_true",
        default=None,
        help="Mark the account receive-only (listen but never send).",
    )
    ro.add_argument(
        "--can-send",
        dest="receive_only",
        action="store_false",
        help="Allow this account to be a send route (undo receive-only).",
    )
    set_.set_defaults(func=_lazy("account_cmd", "cmd_set"))
    remove = acts.add_parser(
        "remove",
        parents=[_globals(True)],
        help="Remove an account and its schedules (stored mail is kept).",
    )
    remove.add_argument("account_id")
    remove.set_defaults(func=_lazy("account_cmd", "cmd_remove"))
    admin.set_defaults(func=_lazy("account_cmd", "cmd_admin"))


def _register_schedule(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Per-account sync schedules (prompt 12b): list and edit. Edits are never over MCP."""
    schedule = sub.add_parser(
        "schedule",
        help="The per-account sync schedule: list, set, pause, resume, remove.",
        parents=[_globals(True)],
        description=(
            "When each account syncs. 'set' takes 'every 15m' or a five-field cron expression; "
            "edits are audited and take effect live. Editing a schedule is never offered over MCP."
        ),
    )
    acts = schedule.add_subparsers(dest="action", metavar="ACTION")
    acts.required = True

    lst = acts.add_parser("list", parents=[_globals(True)], help="Every schedule and its state.")
    lst.add_argument(
        "--crontab", action="store_true", help="Also print a read-only crontab export."
    )

    def _key_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("account_id", help="The account to schedule.")
        p.add_argument(
            "--folder", default="", metavar="NAME", help="A per-folder schedule (default: account)."
        )

    setp = acts.add_parser("set", parents=[_globals(True)], help="Create or change a schedule.")
    _key_args(setp)
    setp.add_argument("schedule", help="'every 15m' or a five-field cron expression (UTC-aware).")
    setp.add_argument(
        "--timezone", metavar="IANA", help="e.g. America/New_York (unchanged if omitted)."
    )
    setp.add_argument(
        "--quiet", metavar="HH:MM-HH:MM", help="Mute alerts in this window; sync still runs."
    )
    setp.add_argument("--clear-quiet", action="store_true", help="Remove the quiet window.")
    setp.add_argument("--run-now", action="store_true", help="Also sync immediately.")

    pause = acts.add_parser("pause", parents=[_globals(True)], help="Pause a schedule.")
    _key_args(pause)
    pause.add_argument("--why", default="", metavar="TEXT", help="A note recorded with the pause.")

    resume = acts.add_parser("resume", parents=[_globals(True)], help="Resume a paused schedule.")
    _key_args(resume)

    remove = acts.add_parser("remove", parents=[_globals(True)], help="Remove a schedule.")
    _key_args(remove)

    schedule.set_defaults(func=_lazy("schedule_cmd", "cmd_schedule"))


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
    args = _parse(parser, list(sys.argv[1:] if argv is None else argv))
    _configure_logging(getattr(args, "verbose", 0))
    from oaken_cli.exits import CliError, ExitCode

    # `oaken help [COMMAND]`: print top-level help, or defer to the command's own --help.
    if getattr(args, "_help", False):
        topic = getattr(args, "topic", None)
        if topic is None:
            parser.print_help()
            return int(ExitCode.OK)
        # Re-parse as 'oaken TOPIC --help', which argparse prints and exits (SystemExit 0).
        try:
            parser.parse_args([topic, "--help"])
        except SystemExit as exit_:
            return int(exit_.code or 0)
        return int(ExitCode.OK)

    try:
        return int(args.func(args))
    except CliError as exc:
        print(f"{PROG}: {exc.message}", file=sys.stderr)
        return int(exc.code)
    except KeyboardInterrupt:
        return int(ExitCode.ERROR)


def _parse(parser: argparse.ArgumentParser, argv: list[str]) -> argparse.Namespace:
    """Parse ``argv``. Flags no verb declares are an error, except after ``operation NAME`` where
    they are the operation's own fields (see ``schema_flags``). ``oaken PLUGIN VERB ...`` that
    names no command is tried as ``operation PLUGIN.VERB ...``."""
    commands = _command_names(parser)
    original = list(argv)
    first = _command_index(argv)
    if first is not None and argv[first] not in commands and first + 1 < len(argv):
        verb = argv[first + 1]
        if not verb.startswith("-"):
            argv = [*argv[:first], "operation", f"{argv[first]}.{verb.replace('-', '_')}"] + argv[
                first + 2 :
            ]
    rewritten = first is not None and argv[first] == "operation" and argv != original
    args, extra = parser.parse_known_args(argv)
    if rewritten:
        args.yes = True  # typing ``oaken PLUGIN VERB`` is the person asking for exactly this
    if extra:
        if getattr(args, "command", None) == "operation" and getattr(args, "name", None):
            args.flags = extra
        else:
            parser.error(f"unrecognized arguments: {' '.join(extra)}")
    return args


def _command_index(argv: list[str]) -> int | None:
    """Where the command word is: the first token that is neither a flag nor a flag's value."""
    takes_value = {"--url", "--token-file"}
    i = 0
    while i < len(argv):
        if argv[i] in takes_value:
            i += 2
        elif argv[i].startswith("-"):
            i += 1
        else:
            return i
    return None


def _command_names(parser: argparse.ArgumentParser) -> set[str]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    return set()


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
