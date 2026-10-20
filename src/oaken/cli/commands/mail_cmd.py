# SPDX-License-Identifier: Apache-2.0
"""The mail-facing commands, each a thin call to one registry operation.

Every handler here resolves to a single :meth:`ApiClient.call`; the operation names and their input
shapes are the contract (see ``oaken.core.operations.builtin.messages`` and ``...mail``). No command
holds business logic — a change to an operation is picked up with no CLI release, and the generic
``oaken operation NAME`` reaches anything not given its own verb here.

Two rules from prompt 17 live in this module because they are presentation, not core policy:

* **Bulk confirm.** A read-state change touching more than :data:`BULK_LIMIT` ids needs ``--yes``.
  The core gate already guards the mutation; this is the extra "you asked for a lot" check the CLI
  owes a person at a terminal.
* **The summarize mark-read note.** ``oaken summarize --mark-read`` must say, in plain text, that
  the messages were marked read and show the audit id so the person can ``oaken undo`` it.
"""

from __future__ import annotations

import argparse

from oaken.cli.client import ApiClient, CallResult
from oaken.cli.commands.runtime import emit, run
from oaken.cli.exits import CliError, ExitCode

# More than this many ids in one read-state change needs an explicit --yes (prompt 17, req 4).
BULK_LIMIT = 25


# --- search / show --------------------------------------------------------------------------


def cmd_search(args: argparse.Namespace) -> int:
    return run(lambda client: _search(client, args), args)


async def _search(client: ApiClient, args: argparse.Namespace) -> int:
    payload: dict[str, object] = {"query": args.query}
    if args.account:
        payload["account_id"] = args.account
    if args.from_:
        payload["from"] = args.from_
    if args.since:
        payload["after"] = args.since
    if args.label:
        payload["label"] = args.label
    if args.limit is not None:
        payload["limit"] = args.limit
    result = await client.call("search_keyword", payload)
    return _render_messages(args, result)


def cmd_show(args: argparse.Namespace) -> int:
    return run(lambda client: _show(client, args), args)


async def _show(client: ApiClient, args: argparse.Namespace) -> int:
    if args.raw or args.headers:
        # get_raw gives the headers always and the raw bytes only when asked (never over MCP).
        result = await client.call(
            "get_raw", {"message_id": args.message_id, "include_raw": bool(args.raw)}
        )
        return _render_raw(args, result)
    result = await client.call("get_message", {"message_id": args.message_id})
    return _render_message(args, result)


# --- read state: summarize / read / unread / undo -------------------------------------------


def cmd_summarize(args: argparse.Namespace) -> int:
    if args.mark_read and len(args.ids) > BULK_LIMIT:
        _require_yes(args, len(args.ids), "mark read")
    return run(lambda client: _summarize(client, args), args)


async def _summarize(client: ApiClient, args: argparse.Namespace) -> int:
    payload = {"message_ids": list(args.ids), "mark_read": bool(args.mark_read)}
    # mark_read inside summarize is a confirmed read-state change; --yes pre-confirms it.
    result = await client.call("summarize_message", payload, confirm=bool(args.mark_read))
    return _render_summary(args, result)


def cmd_mark(args: argparse.Namespace) -> int:
    """Shared handler for ``oaken read`` / ``oaken unread`` (op set on the subparser)."""
    if len(args.ids) > BULK_LIMIT and not args.yes:
        _require_yes(args, len(args.ids), args.op.replace("mark_", "mark "))
    return run(lambda client: _mark(client, args), args)


async def _mark(client: ApiClient, args: argparse.Namespace) -> int:
    # Read-state ops are an explicit user command; a human caller executes directly (confirm=True).
    result = await client.call(args.op, {"ids": list(args.ids)}, confirm=True)
    return _render_mark(args, result)


def cmd_undo(args: argparse.Namespace) -> int:
    return run(lambda client: _undo(client, args), args)


async def _undo(client: ApiClient, args: argparse.Namespace) -> int:
    result = await client.call("undo", {"audit_id": args.audit_id}, confirm=True)
    return _render_mark(args, result)


# --- sync -----------------------------------------------------------------------------------


def cmd_sync(args: argparse.Namespace) -> int:
    return run(lambda client: _sync(client, args), args)


async def _sync(client: ApiClient, args: argparse.Namespace) -> int:
    payload: dict[str, object] = {"account_id": args.account or ""}
    if args.folder:
        payload["folder"] = args.folder
    result = await client.call("sync_now", payload, confirm=True)
    data = {"request_id": result.request_id, "output": result.output}

    def text() -> None:
        from oaken.cli.output import print_text

        out = result.output or {}
        job = out.get("job_id", "?") if isinstance(out, dict) else "?"
        created = out.get("created", True) if isinstance(out, dict) else True
        print_text(f"sync queued (job {job})" if created else f"joined running sync (job {job})")
        if args.wait:
            print_text(
                "(--wait: streaming job events is enabled once the daemon lands — prompt 16)"
            )

    return emit(args, data=data, text=text)


# --- rendering ------------------------------------------------------------------------------


def _render_messages(args: argparse.Namespace, result: CallResult) -> int:
    out = result.output if isinstance(result.output, dict) else {}
    items = out.get("items", []) if isinstance(out, dict) else []
    rows = [
        {
            "date": _short_date(it.get("date")),
            "from": it.get("from_name") or it.get("from_addr", ""),
            "subject": it.get("subject", ""),
            "message_id": it.get("message_id", ""),
        }
        for it in items
    ]

    def text() -> None:
        from oaken.cli.output import print_table, print_text

        if not rows:
            print_text("no matches")
            return
        print_table(rows, ["date", "from", "subject", "message_id"])
        if out.get("truncated"):
            print_text("(results were cut to the limit; narrow the query or raise --limit)")
        for w in out.get("warnings", []) or []:
            print_text(f"warning: {w}")

    return emit(args, data=result.output, text=text)


def _render_message(args: argparse.Namespace, result: CallResult) -> int:
    """One message: the cleaned body as sanitized text, or the whole object as JSON."""
    out = result.output if isinstance(result.output, dict) else {}

    def text() -> None:
        from oaken.cli.output import print_text

        subject = out.get("subject", "") if isinstance(out, dict) else ""
        sender = out.get("from_name") or out.get("from_addr", "") if isinstance(out, dict) else ""
        print_text(f"From: {sender}")
        print_text(f"Subject: {subject}")
        if isinstance(out, dict) and out.get("truncated"):
            print_text("(text was cut to the limit)")
        print_text("")
        print_text(out.get("text", "") if isinstance(out, dict) else "")

    return emit(args, data=result.output, text=text)


def _render_raw(args: argparse.Namespace, result: CallResult) -> int:
    out = result.output if isinstance(result.output, dict) else {}

    def text() -> None:
        from oaken.cli.output import print_text

        print_text(out.get("headers", "") if isinstance(out, dict) else "")
        if isinstance(out, dict) and out.get("raw_base64"):
            # Raw bytes are base64 in the machine form; on a terminal we only note they are there.
            print_text(f"\n[{out.get('size_raw', 0)} raw bytes; use --json to get base64]")

    return emit(args, data=result.output, text=text)


def _render_summary(args: argparse.Namespace, result: CallResult) -> int:
    out = result.output if isinstance(result.output, dict) else {}

    def text() -> None:
        from oaken.cli.output import print_text

        for item in out.get("items", []) if isinstance(out, dict) else []:
            mid = item.get("message_id", "?")
            if item.get("error"):
                print_text(f"{mid}: (no summary — {item['error']})")
                continue
            print_text(f"{mid}:")
            print_text(item.get("summary") or "")
            if item.get("injection_suspected"):
                print_text("  ⚠ the model saw instruction-like text inside this message.")
        marked = out.get("marked_read", []) if isinstance(out, dict) else []
        if marked:
            # The required note: say read state changed, and how to reverse it.
            print_text(
                f"\nmarked {len(marked)} message(s) read. "
                'undo with "oaken undo AUDIT_ID" (see "oaken audit").'
            )

    return emit(args, data=result.output, text=text)


def _render_mark(args: argparse.Namespace, result: CallResult) -> int:
    out = result.output if isinstance(result.output, dict) else {}

    def text() -> None:
        from oaken.cli.output import print_text

        changed = out.get("changed", []) if isinstance(out, dict) else []
        unchanged = out.get("unchanged", []) if isinstance(out, dict) else []
        missing = out.get("missing", []) if isinstance(out, dict) else []
        local = out.get("local_only", []) if isinstance(out, dict) else []
        audit = out.get("audit_ids", []) if isinstance(out, dict) else []
        print_text(f"changed {len(changed)}, unchanged {len(unchanged)}, missing {len(missing)}")
        if local:
            print_text(f"{len(local)} changed only locally (no provider could write them back)")
        if audit:
            print_text(f'audit id(s): {", ".join(audit)}  (undo with "oaken undo ID")')

    return emit(args, data=result.output, text=text)


def _require_yes(args: argparse.Namespace, count: int, what: str) -> None:
    raise CliError(
        f"this would {what} {count} messages (over {BULK_LIMIT}); re-run with --yes to confirm",
        ExitCode.NEEDS_CONFIRMATION,
    )


def _short_date(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    return value[:10]  # the date, dropping the time; JSON output keeps the full value
