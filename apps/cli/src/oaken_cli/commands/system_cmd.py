# SPDX-License-Identifier: Apache-2.0
"""System and inspection commands, plus the event stream.

Each read command is one :meth:`ApiClient.call` to a ``read`` operation and a table on the way out;
``--json`` prints the operation's own output verbatim. The two job starters (``reprocess``,
``rebuild-index``) are ``mutate`` operations a person runs, so they execute directly. ``watch``
is the one streaming command: it iterates :meth:`ApiClient.stream_events` and prints a line (or a
JSON object) per event until interrupted.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any

from oaken_cli.client import ApiClient, CallResult
from oaken_cli.commands.runtime import emit, run
from oaken_cli.exits import CliError, ExitCode

# --- inspection (read ops) ------------------------------------------------------------------


def cmd_pipeline(args: argparse.Namespace) -> int:
    return run(lambda client: _pipeline_dispatch(client, args), args)


async def _pipeline_dispatch(client: ApiClient, args: argparse.Namespace) -> int:
    sub = getattr(args, "subcommand", "status")
    words = list(getattr(args, "words", []) or [])
    if sub == "pause":
        payload: dict[str, object] = {"why": args.why}
        if words:
            payload["stage"] = words[0]
        result = await client.call("pause_backfill", payload, confirm=True)
        return _emit_pause(args, result)
    if sub == "resume":
        result = await client.call(
            "resume_backfill", {"stage": words[0]} if words else {}, confirm=True
        )
        return _emit_pause(args, result)
    if sub == "limits":
        if len(words) != 2 or not words[1].isdigit():
            raise CliError("pipeline limits needs a stage and a number: embed 2", ExitCode.USAGE)
        result = await client.call(
            "set_pipeline_limits", {"stage": words[0], "concurrency": int(words[1])}, confirm=True
        )
        out = result.output if isinstance(result.output, dict) else {}
        return emit(
            args,
            data=result.output,
            text=lambda: _say(f"{out.get('stage')} now runs {out.get('concurrency')} at a time"),
        )
    if sub == "parked":
        payload = {}
        if args.retry:
            payload["job_id"] = args.retry
        elif args.all:
            payload["all"] = True
        else:
            raise CliError("give --retry JOB_ID or --all", ExitCode.USAGE)
        result = await client.call("retry_parked", payload, confirm=True)
        out = result.output if isinstance(result.output, dict) else {}

        def retried() -> None:
            from oaken_cli.output import print_text

            ids = out.get("retried", []) if isinstance(out, dict) else []
            print_text(f"re-queued {len(ids)} parked job(s)")

        return emit(args, data=result.output, text=retried)
    return await _pipeline(client, args)


def _emit_pause(args: argparse.Namespace, result: CallResult) -> int:
    out = result.output if isinstance(result.output, dict) else {}

    def text() -> None:
        from oaken_cli.output import print_text

        stages = out.get("paused_stages") or []
        if out.get("llm_paused"):
            reason = out.get("reason") or ""
            print_text(f"model stages paused{f' ({reason})' if reason else ''}")
        elif stages:
            print_text(f"paused: {', '.join(stages)}")
        else:
            print_text("model stages resumed")

    return emit(args, data=result.output, text=text)


async def _pipeline(client: ApiClient, args: argparse.Namespace) -> int:
    result = await client.call("pipeline_status", {})
    out = result.output if isinstance(result.output, dict) else {}
    stages = out.get("stages", {}) if isinstance(out, dict) else {}
    rows = [
        {
            "stage": name,
            "pending": s.get("pending", 0),
            "running": s.get("running", 0),
            "failed": s.get("failed", 0),
            "per_second": round(float(s.get("per_second", 0.0)), 2),
            "state": "PAUSED" if s.get("paused") else "",
            "limit": s.get("concurrency") if s.get("concurrency") else "",
        }
        for name, s in stages.items()
    ]

    def text() -> None:
        from oaken_cli.output import print_table, print_text

        print_table(
            rows,
            ["stage", "pending", "running", "failed", "per_second", "state", "limit"],
            title="pipeline",
        )
        print_text(
            f"docs {out.get('docs', 0)}, chunks {out.get('chunks', 0)}, "
            f"vectors {out.get('vectors', 0)}"
            + (", backfill paused" if out.get("backfill_paused") else "")
            + (", model stages PAUSED" if out.get("llm_paused") else "")
        )
        for name, s in stages.items():
            for job in s.get("current", []) or []:
                print_text(f"running {job.get('job_id')}: {name} {job.get('message_id') or ''}")
        for p in out.get("parked", []) or []:
            print_text(f"parked {p.get('job_id')}: {p.get('stage')} — {p.get('error')}")

    return emit(args, data=result.output, text=text)


def cmd_usage(args: argparse.Namespace) -> int:
    return run(lambda client: _usage(client, args), args)


async def _usage(client: ApiClient, args: argparse.Namespace) -> int:
    payload: dict[str, object] = {}
    if args.since_hours is not None:
        payload["since_hours"] = args.since_hours
    if args.workspace:
        payload["workspace"] = args.workspace
    result = await client.call("usage_summary", payload)
    out = result.output if isinstance(result.output, dict) else {}
    rows = out.get("rows", []) if isinstance(out, dict) else []
    try:
        saved: CallResult | None = await client.call(
            "savings_summary", {k: v for k, v in payload.items() if k == "since_hours"}
        )
    except CliError:  # a core from before savings were tracked: show the usage table alone
        saved = None
    sav = saved.output if saved is not None and isinstance(saved.output, dict) else {}

    def text() -> None:
        from oaken_cli.output import print_text

        if not rows:
            print_text("no usage recorded")
        else:
            _table(
                rows,
                [
                    "workspace",
                    "stage",
                    "model_id",
                    "calls",
                    "tokens_in",
                    "tokens_out",
                    "tokens_saved",
                ],
                "usage",
                sums=["calls", "tokens_in", "tokens_out", "tokens_saved"],
            )
        totals = [
            {**t, "context_saved_pct": _pct(t["context_tokens_saved"], t["context_tokens_full"])}
            for t in sav.get("totals", [])
        ]
        if totals:
            full = sum(x["context_tokens_full"] for x in totals)
            _table(
                totals,
                ["caller", "cache_tokens_saved", "context_tokens_saved", "context_saved_pct"],
                "saved, by caller",
                sums=["cache_tokens_saved", "context_tokens_saved"],
                extra=lambda t: {"context_saved_pct": _pct(t["context_tokens_saved"], full)},
            )
        if sav.get("context"):
            _table(
                sav["context"],
                [
                    "caller",
                    "op",
                    "calls",
                    "messages",
                    "tokens_returned",
                    "tokens_full",
                    "tokens_saved",
                ],
                "context kept out of the client, by operation",
                sums=["calls", "messages", "tokens_returned", "tokens_full", "tokens_saved"],
            )
        if totals:
            print_text(sav.get("note", ""))

    return emit(args, data={**out, "savings": saved.output if saved else None}, text=text)


def _table(
    rows: list[dict[str, Any]],
    columns: list[str],
    title: str,
    *,
    sums: list[str],
    extra: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> None:
    """Print ``rows`` with a totals row under a rule (text view only; the JSON keeps plain rows)."""
    from oaken_cli.output import print_table

    if len(rows) < 2:
        print_table(rows, columns, title=title)
        return
    total = {columns[0]: "total", **{c: sum(int(r.get(c, 0)) for r in rows) for c in sums}}
    if extra:
        total.update(extra(total))
    print_table([*rows, total], columns, title=title, footer_rows=1)


def _pct(saved: int, full: int) -> str:
    return f"{100 * saved / full:.0f}%" if full else "-"


def cmd_plugins(args: argparse.Namespace) -> int:
    return run(lambda client: _plugins(client, args), args)


async def _plugins(client: ApiClient, args: argparse.Namespace) -> int:
    sub = getattr(args, "subcommand", "list")
    if sub != "list":
        if not args.name:
            raise CliError(f"plugins {sub} needs a plugin name", ExitCode.USAGE)
        changed = await client.call(f"plugin_{sub}", {"name": args.name}, confirm=True)

        def said() -> None:
            from oaken_cli.output import print_text

            names = changed.output.get("changed", []) if isinstance(changed.output, dict) else []
            print_text(f"{sub}: {', '.join(names)}")

        return emit(args, data=changed.output, text=said)
    op = "plugin_health" if args.check else "list_plugins"
    result = await client.call(op, {})
    out = result.output if isinstance(result.output, dict) else {}
    plugins = out.get("plugins", []) if isinstance(out, dict) else []
    rows = [
        {
            "name": p.get("name", ""),
            "kind": p.get("kind", ""),
            "version": p.get("version", ""),
            "started": p.get("started", False),
            "healthy": p.get("healthy", False),
            "detail": p.get("detail", ""),
        }
        for p in plugins
    ]

    def text() -> None:
        from oaken_cli.output import print_table, print_text

        if not rows:
            print_text("no plugins")
            return
        print_table(
            rows, ["name", "kind", "version", "started", "healthy", "detail"], title="plugins"
        )

    return emit(args, data=result.output, text=text)


def cmd_accounts(args: argparse.Namespace) -> int:
    return run(lambda client: _accounts(client, args), args)


async def _accounts(client: ApiClient, args: argparse.Namespace) -> int:
    result = await client.call("list_accounts", {})
    out = result.output if isinstance(result.output, dict) else {}
    rows = out.get("accounts", []) if isinstance(out, dict) else []

    def text() -> None:
        from oaken_cli.output import print_table, print_text

        if not rows:
            print_text("no accounts configured")
            return
        print_table(rows, ["account_id", "provider", "display_name"], title="accounts")

    return emit(args, data=result.output, text=text)


def cmd_audit(args: argparse.Namespace) -> int:
    return run(lambda client: _audit(client, args), args)


async def _audit(client: ApiClient, args: argparse.Namespace) -> int:
    payload: dict[str, object] = {"limit": args.limit}
    if args.op:
        payload["op"] = args.op
    result = await client.call("list_audit", payload)
    out = result.output if isinstance(result.output, dict) else {}
    rows = [
        {
            "audit_id": e.get("audit_id", ""),
            "op": e.get("op", ""),
            "actor": e.get("actor_label", ""),
            "count": e.get("count", 0),
            "undoable": e.get("undoable", False),
        }
        for e in (out.get("entries", []) if isinstance(out, dict) else [])
    ]

    def text() -> None:
        from oaken_cli.output import print_table, print_text

        if not rows:
            print_text("no audit entries")
            return
        print_table(rows, ["audit_id", "op", "actor", "count", "undoable"], title="audit")

    return emit(args, data=result.output, text=text)


def cmd_operations(args: argparse.Namespace) -> int:
    return run(lambda client: _operations(client, args), args)


async def _operations(client: ApiClient, args: argparse.Namespace) -> int:
    result = await client.call("list_operations", {})
    out = result.output if isinstance(result.output, dict) else {}
    rows = [
        {
            "name": o.get("name", ""),
            "provider": o.get("plugin") or "core",
            "permission": o.get("permission", ""),
            "mcp": o.get("mcp_enabled", False),
            "summary": o.get("summary", ""),
        }
        for o in (out.get("operations", []) if isinstance(out, dict) else [])
    ]

    def text() -> None:
        from oaken_cli.output import print_table

        print_table(rows, ["name", "provider", "permission", "mcp", "summary"], title="operations")

    return emit(args, data=result.output, text=text)


# --- job starters (mutate ops a person runs) ------------------------------------------------


def cmd_reprocess(args: argparse.Namespace) -> int:
    return run(lambda client: _reprocess(client, args), args)


async def _reprocess(client: ApiClient, args: argparse.Namespace) -> int:
    payload: dict[str, object] = {"stage": args.stage}
    if args.account:
        payload["account_id"] = args.account
    if args.since:
        payload["since"] = args.since
    if args.limit:
        payload["limit"] = args.limit
    result = await client.call("reprocess", payload, confirm=True)
    return _render_queued(args, result, f"reprocess {args.stage}")


def cmd_rebuild_index(args: argparse.Namespace) -> int:
    return run(lambda client: _rebuild(client, args), args)


async def _rebuild(client: ApiClient, args: argparse.Namespace) -> int:
    payload: dict[str, object] = {"force": bool(args.force)}
    if args.account:
        payload["account_id"] = args.account
    result = await client.call("rebuild_index", payload, confirm=True)
    return _render_queued(args, result, "rebuild-index")


def cmd_ask(args: argparse.Namespace) -> int:
    return run(lambda client: _ask(client, args), args)


async def _ask(client: ApiClient, args: argparse.Namespace) -> int:
    result = await client.call("ask", {"question": args.question}, timeout=args.timeout)
    out = result.output if isinstance(result.output, dict) else {}

    def text() -> None:
        from oaken_cli.output import print_text

        print_text(out.get("answer", ""))
        if out.get("insufficient_evidence"):
            print_text("(not enough in the mail to answer this)")
        cites = out.get("citations", []) or []
        if cites:
            print_text("\nsources:")
        for c in cites:
            print_text(
                f"  [{c.get('claim')}] {c.get('subject', '')} ({c.get('sender', '')}, "
                f"{str(c.get('date') or '')[:10]}) {c.get('message_id', '')}"
            )
            print_text(f'      "{c.get("quote", "")}"')
        if out.get("degraded"):
            print_text(f"note: keyword evidence only ({out.get('degraded_reason', '')})")
        for w in out.get("warnings", []) or []:
            print_text(f"warning: {w}")

    return emit(args, data=result.output, text=text)


# --- watch (SSE) ----------------------------------------------------------------------------


def cmd_watch(args: argparse.Namespace) -> int:
    return run(lambda client: _watch(client, args), args)


async def _watch(client: ApiClient, args: argparse.Namespace) -> int:
    from oaken_cli.output import print_json, print_text

    try:
        async for event in client.stream_events():
            if getattr(args, "json", False):
                print_json({"id": event.id, "event": event.event, "data": dict(event.data)})
            else:
                print_text(f"{event.event}  {dict(event.data)}")
    except KeyboardInterrupt:
        pass
    return int(ExitCode.OK)


# --- shared ---------------------------------------------------------------------------------


def _render_queued(args: argparse.Namespace, result: CallResult, what: str) -> int:
    out = result.output if isinstance(result.output, dict) else {}
    data = {"request_id": result.request_id, "output": result.output}

    def text() -> None:
        from oaken_cli.output import print_text

        job = out.get("job_id", "?") if isinstance(out, dict) else "?"
        created = out.get("created", True) if isinstance(out, dict) else True
        print_text(f"{what} queued (job {job})" if created else f"{what} joined (job {job})")

    return emit(args, data=data, text=text)


# --- config.toml through core ---------------------------------------------------------------


def cmd_config(args: argparse.Namespace) -> int:
    return run(lambda client: _config(client, args), args)


def _read_candidate(args: argparse.Namespace) -> str:
    import sys

    if not args.file:
        raise CliError(f"config {args.subcommand} needs FILE (or - for stdin)", ExitCode.USAGE)
    if args.file == "-":
        return sys.stdin.read()
    try:
        with open(args.file, encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        raise CliError(f"cannot read {args.file}: {exc}", ExitCode.USAGE) from exc


async def _config(client: ApiClient, args: argparse.Namespace) -> int:
    from oaken_cli.output import print_text

    sub = args.subcommand
    if sub == "show":
        result = await client.call("config_get", {})
        out = result.output if isinstance(result.output, dict) else {}
        return emit(args, data=result.output, text=lambda: print_text(str(out.get("text", ""))))
    if sub == "reload":
        result = await client.call("config_reload", {}, confirm=True)
        out = result.output if isinstance(result.output, dict) else {}
        code = emit(
            args,
            data=result.output,
            text=lambda: print_text(
                "reload requested" if out.get("ok") else f"not reloaded: {out.get('error', '')}"
            ),
        )
        return code if out.get("ok") else int(ExitCode.USAGE)
    text = _read_candidate(args)
    if sub == "test":
        result = await client.call("config_validate", {"text": text})
    else:
        result = await client.call("config_save", {"text": text}, confirm=True)
    out = result.output if isinstance(result.output, dict) else {}

    def said() -> None:
        if out.get("ok"):
            print_text("saved" if sub == "save" else "valid")
            if out.get("backup"):
                print_text(f"previous file kept as {out['backup']}")
        else:
            print_text(f"invalid: {out.get('error', '')}")

    code = emit(args, data=result.output, text=said)
    return code if out.get("ok") else int(ExitCode.USAGE)


def _say(line: str) -> None:
    from oaken_cli.output import print_text

    print_text(line)
