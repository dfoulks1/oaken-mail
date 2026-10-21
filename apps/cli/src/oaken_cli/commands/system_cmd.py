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

from oaken_cli.client import ApiClient, CallResult
from oaken_cli.commands.runtime import emit, run
from oaken_cli.exits import ExitCode

# --- inspection (read ops) ------------------------------------------------------------------


def cmd_pipeline(args: argparse.Namespace) -> int:
    return run(lambda client: _pipeline(client, args), args)


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
        }
        for name, s in stages.items()
    ]

    def text() -> None:
        from oaken_cli.output import print_table, print_text

        print_table(rows, ["stage", "pending", "running", "failed", "per_second"], title="pipeline")
        print_text(
            f"docs {out.get('docs', 0)}, chunks {out.get('chunks', 0)}, "
            f"vectors {out.get('vectors', 0)}"
            + (", backfill paused" if out.get("backfill_paused") else "")
        )
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

    def text() -> None:
        from oaken_cli.output import print_table, print_text

        if not rows:
            print_text("no usage recorded")
            return
        print_table(
            rows,
            ["workspace", "stage", "model_id", "calls", "tokens_in", "tokens_out", "tokens_saved"],
            title="usage",
        )

    return emit(args, data=result.output, text=text)


def cmd_plugins(args: argparse.Namespace) -> int:
    return run(lambda client: _plugins(client, args), args)


async def _plugins(client: ApiClient, args: argparse.Namespace) -> int:
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
            "permission": o.get("permission", ""),
            "mcp": o.get("mcp_enabled", False),
            "summary": o.get("summary", ""),
        }
        for o in (out.get("operations", []) if isinstance(out, dict) else [])
    ]

    def text() -> None:
        from oaken_cli.output import print_table

        print_table(rows, ["name", "permission", "mcp", "summary"], title="operations")

    return emit(args, data=result.output, text=text)


# --- job starters (mutate ops a person runs) ------------------------------------------------


def cmd_reprocess(args: argparse.Namespace) -> int:
    return run(lambda client: _reprocess(client, args), args)


async def _reprocess(client: ApiClient, args: argparse.Namespace) -> int:
    payload: dict[str, object] = {"stage": args.stage}
    if args.account:
        payload["account_id"] = args.account
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
