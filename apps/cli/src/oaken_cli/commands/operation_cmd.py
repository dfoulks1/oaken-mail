# SPDX-License-Identifier: Apache-2.0
"""``oaken operation [NAME] [--json-input JSON] [--yes]`` — the generic registry fallback.

With no ``NAME``, lists every operation the daemon offers (name, permission, summary). With a
``NAME``, calls it: the input is a JSON object from ``--json-input`` or stdin. This is what lets a
new registry operation be used the moment it exists, with no CLI change (prompt 17, requirement 2).

Mutations follow the confirm gate (prompt 13): the first call without ``--yes`` comes back as a
proposal — what would change — and nothing happens; ``--yes`` means *the person asked for exactly
this* and the call executes (for a human-class caller this is a single pre-confirmed call, not a
two-phase token dance, which is reserved for agents).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from oaken_cli.client import ApiClient, CallResult
from oaken_cli.commands.runtime import emit, run
from oaken_cli.exits import CliError, ExitCode


def cmd_operation(args: argparse.Namespace) -> int:
    if not args.name:
        return run(lambda client: _list(client, args), args)
    if args.describe:
        return run(lambda client: _describe(client, args), args)
    return run(lambda client: _call(client, args), args)


async def _list(client: ApiClient, args: argparse.Namespace) -> int:
    ops = await client.list_ops()
    data = [
        {
            "name": o.name,
            "permission": o.permission,
            "marks_read": o.marks_read,
            "summary": o.summary,
        }
        for o in ops
    ]

    def text() -> None:
        from oaken_cli.output import print_table

        print_table(data, ["name", "permission", "summary"], title="operations")

    return emit(args, data=data, text=text)


def _fields(schema: object) -> list[dict[str, object]]:
    """The fields of a JSON Schema object as rows: name, type, whether required."""
    if not isinstance(schema, dict):
        return []
    required = set(schema.get("required", []))
    return [
        {
            "field": name,
            "type": str(
                p.get("type") or ("/".join(str(x.get("type")) for x in p.get("anyOf", []))) or "any"
            ),
            "required": name in required,
            "description": p.get("description", ""),
        }
        for name, p in schema.get("properties", {}).items()
    ]


async def _describe(client: ApiClient, args: argparse.Namespace) -> int:
    ops = {o.name: o for o in await client.list_ops()}
    op = ops.get(args.name)
    if op is None:
        raise CliError(f"no such operation: {args.name}", ExitCode.USAGE)
    data = {
        "name": op.name,
        "summary": op.summary,
        "permission": op.permission,
        "marks_read": op.marks_read,
        "input": _fields(op.input_schema),
        "output": _fields(op.output_schema),
    }

    def text() -> None:
        from oaken_cli.output import print_table, print_text

        print_text(f"{op.name}: {op.summary}")
        mark = "; may change read state" if op.marks_read else ""
        print_text(f"permission: {op.permission}{mark}")
        cols = ["field", "type", "required", "description"]
        print_table(_fields(op.input_schema), cols, title="input")
        print_table(_fields(op.output_schema), cols, title="output")

    return emit(args, data=data, text=text)


async def _call(client: ApiClient, args: argparse.Namespace) -> int:
    from oaken_cli.schema_flags import missing_required, parse_flags, secret_fields

    payload = _read_input(args)
    rest = list(getattr(args, "flags", None) or [])
    op = next((o for o in await client.list_ops() if o.name == args.name), None)
    if op is None and rest:
        raise CliError(f"no such operation: {args.name}", ExitCode.USAGE)
    if op is not None:
        schema = dict(op.input_schema or {})
        prog = f"oaken operation {args.name}"
        payload.update(parse_flags(schema, rest, prog=prog, enforce_required=False))
        if missing := missing_required(schema, payload):
            raise CliError(f"{args.name} needs {', '.join(missing)}", ExitCode.USAGE)
        for name in secret_fields(schema):
            if not payload.get(name) and not payload.get("check"):
                payload[name] = _ask_secret(args.name, name)
    result = await client.call(args.name, payload, confirm=bool(args.yes))
    if isinstance(result.output, dict) and "state" in result.output and result.proposal is None:
        return await _stateful(client, args, result)
    return render_result(args, result)


def _ask_secret(operation: str, field: str) -> str:
    """A secret field, without echo, from a terminal. Never from an argument."""
    import getpass

    if not sys.stdin.isatty():
        raise CliError(
            f"no terminal to ask for {field} on; pipe it with --{field.replace('_', '-')}-stdin",
            ExitCode.USAGE,
        )
    value = getpass.getpass(f"{operation} {field} (not shown): ")
    if not value:
        raise CliError(f"no {field} given", ExitCode.USAGE)
    return value


async def _stateful(client: ApiClient, args: argparse.Namespace, result: CallResult) -> int:
    """An output that says ``state`` (done, failed or pending) follows a small convention: it may
    carry an ``open_url`` to show, and a ``poll`` operation to call until it is done or failed.
    The client acts on that without knowing which plugin or provider it is."""
    import asyncio
    import time

    from oaken_cli.output import print_text

    out: dict[str, Any] = dict(result.output) if isinstance(result.output, dict) else {}
    quiet = bool(getattr(args, "json", False))
    if out.get("open_url") and not quiet:
        print_text(f"open this address:\n{out['open_url']}")
        if not getattr(args, "no_browser", False) and sys.stdout.isatty():
            import webbrowser

            webbrowser.open(str(out["open_url"]))
    if out.get("state") == "pending" and out.get("poll"):
        if not quiet:
            print_text(str(out.get("detail") or "waiting..."))
        deadline = time.monotonic() + float(getattr(args, "timeout", 300.0))
        while time.monotonic() < deadline and out.get("state") == "pending":
            await asyncio.sleep(1.0)
            polled = await client.call(str(out["poll"]), dict(out.get("poll_input") or {}))
            if isinstance(polled.output, dict):
                out = {**out, **polled.output}
        if out.get("state") == "pending":
            out = {**out, "state": "failed", "detail": "gave up waiting"}

    def text() -> None:
        print_text(str(out.get("detail") or out.get("state")))

    emit(args, data={"request_id": result.request_id, "output": out}, text=text)
    return int(ExitCode.OK if out.get("state") == "done" else ExitCode.AUTH)


def _read_input(args: argparse.Namespace) -> dict[str, object]:
    raw = args.json_input
    # Only an explicit ``-`` reads stdin. Omitting --json-input means "no input" (an empty object),
    # so an operation that takes no arguments works in a script or pipeline without hanging on, or
    # accidentally consuming, stdin.
    if raw == "-":
        raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CliError(f"--json-input is not valid JSON: {exc}", ExitCode.USAGE) from None
    if not isinstance(value, dict):
        raise CliError("--json-input must be a JSON object", ExitCode.USAGE)
    return value


def render_result(args: argparse.Namespace, result: CallResult) -> int:
    """Render a :class:`CallResult`: an executed output, or a proposal (exit 5). Reused by the
    concrete commands that call a single operation (e.g. ``oaken show``)."""
    if result.proposal is not None:
        return _render_proposal(args, result)
    data = {"request_id": result.request_id, "output": result.output}

    def text() -> None:
        from oaken_cli.output import print_json, print_text

        if result.output is None:
            print_text("ok")
        else:
            print_json(result.output)

    return emit(args, data=data, text=text)


def _render_proposal(args: argparse.Namespace, result: CallResult) -> int:
    p = result.proposal
    assert p is not None  # noqa: S101 — render path is only reached when a proposal is present
    data = {
        "request_id": result.request_id,
        "status": "confirmation_required",
        "proposal": {
            "operation": p.operation,
            "summary": p.summary,
            "changes": p.changes,
            "total_changes": p.total_changes,
        },
    }

    def text() -> None:
        from oaken_cli.output import print_text

        print_text(f"would {p.summary}")
        print_text(f"{p.total_changes} change(s); re-run with --yes to confirm.")

    emit(args, data=data, text=text)
    # A proposal is "not done": exit 5 so a script can tell execution from a confirm prompt.
    return int(ExitCode.NEEDS_CONFIRMATION)
