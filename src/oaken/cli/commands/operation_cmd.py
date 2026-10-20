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

from oaken.cli.client import ApiClient, CallResult
from oaken.cli.commands.runtime import emit, run
from oaken.cli.exits import CliError, ExitCode


def cmd_operation(args: argparse.Namespace) -> int:
    if not args.name:
        return run(lambda client: _list(client, args), args)
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
        from oaken.cli.output import print_table

        print_table(data, ["name", "permission", "summary"], title="operations")

    return emit(args, data=data, text=text)


async def _call(client: ApiClient, args: argparse.Namespace) -> int:
    payload = _read_input(args)
    result = await client.call(args.name, payload, confirm=bool(args.yes))
    return render_result(args, result)


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
        from oaken.cli.output import print_json, print_text

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
        from oaken.cli.output import print_text

        print_text(f"would {p.summary}")
        print_text(f"{p.total_changes} change(s); re-run with --yes to confirm.")

    emit(args, data=data, text=text)
    # A proposal is "not done": exit 5 so a script can tell execution from a confirm prompt.
    return int(ExitCode.NEEDS_CONFIRMATION)
