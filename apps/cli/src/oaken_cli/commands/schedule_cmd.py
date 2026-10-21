# SPDX-License-Identifier: Apache-2.0
"""``oaken schedule list|set|pause|resume|remove`` — the per-account sync schedule (prompt 12b).

Each subcommand is one registry operation; the CLI holds no scheduling logic. ``list`` is a ``read``
op; the four edits are ``mutate`` ops behind the confirm gate, so a person at a terminal passes
``confirm=True`` (they asked for exactly this) the same way ``reprocess`` and ``rebuild-index`` do.
Editing a schedule is never offered over MCP, so this verb is the human path to it.

``set --run-now`` triggers a sync immediately as well as installing the schedule (the op's own
``run_now``), which is why there is no separate ``run`` subcommand.
"""

from __future__ import annotations

import argparse

from oaken_cli.client import ApiClient, CallResult
from oaken_cli.commands.runtime import emit, run

_COLUMNS = [
    "account_id",
    "folder",
    "schedule",
    "timezone",
    "paused",
    "next_run",
    "last_result",
    "problem",
]


def cmd_schedule(args: argparse.Namespace) -> int:
    return run(lambda client: _dispatch(client, args), args)


async def _dispatch(client: ApiClient, args: argparse.Namespace) -> int:
    if args.action == "list":
        return await _list(client, args)
    if args.action == "set":
        return await _set(client, args)
    if args.action == "pause":
        return await _pause(client, args)
    if args.action == "resume":
        return await _resume(client, args)
    return await _remove(client, args)


def _key(args: argparse.Namespace) -> dict[str, object]:
    payload: dict[str, object] = {"account_id": args.account_id}
    if args.folder:
        payload["folder"] = args.folder
    return payload


async def _list(client: ApiClient, args: argparse.Namespace) -> int:
    result = await client.call("list_schedules", {"crontab": bool(args.crontab)})
    out = result.output if isinstance(result.output, dict) else {}
    rows = out.get("schedules", []) if isinstance(out, dict) else []

    def text() -> None:
        from oaken_cli.output import print_table, print_text

        if not rows:
            print_text("no sync schedules")
            return
        # Render dates to the minute for a readable table; --json keeps the full value.
        display = [
            {
                **r,
                "next_run": str(r.get("next_run", ""))[:16],
                "last_result": r.get("last_result") or "",
            }
            for r in rows
        ]
        print_table(display, _COLUMNS, title="sync schedules")
        if out.get("crontab"):
            print_text("\ncrontab:")
            print_text(str(out["crontab"]))

    return emit(args, data=result.output, text=text)


async def _set(client: ApiClient, args: argparse.Namespace) -> int:
    payload: dict[str, object] = {**_key(args), "schedule": args.schedule}
    if args.timezone:
        payload["timezone"] = args.timezone
    if args.quiet:
        payload["quiet"] = args.quiet
    if args.clear_quiet:
        payload["clear_quiet"] = True
    if args.run_now:
        payload["run_now"] = True
    result = await client.call("set_schedule", payload, confirm=True)
    return _emit_schedule(args, result, "set")


async def _pause(client: ApiClient, args: argparse.Namespace) -> int:
    payload = _key(args)
    if args.why:
        payload["why"] = args.why
    result = await client.call("pause_schedule", payload, confirm=True)
    return _emit_schedule(args, result, "paused")


async def _resume(client: ApiClient, args: argparse.Namespace) -> int:
    result = await client.call("resume_schedule", _key(args), confirm=True)
    return _emit_schedule(args, result, "resumed")


async def _remove(client: ApiClient, args: argparse.Namespace) -> int:
    result = await client.call("remove_schedule", _key(args), confirm=True)
    out = result.output if isinstance(result.output, dict) else {}

    def text() -> None:
        from oaken_cli.output import print_text

        removed = out.get("removed", False) if isinstance(out, dict) else False
        where = args.account_id + (f"/{args.folder}" if args.folder else "")
        print_text(f"removed schedule for {where}" if removed else f"no schedule for {where}")

    return emit(args, data=result.output, text=text)


def _emit_schedule(args: argparse.Namespace, result: CallResult, verb: str) -> int:
    out = result.output if isinstance(result.output, dict) else {}
    sched = out.get("schedule", {}) if isinstance(out, dict) else {}

    def text() -> None:
        from oaken_cli.output import print_text

        where = sched.get("account_id", args.account_id)
        folder = sched.get("folder") or ""
        where = f"{where}/{folder}" if folder else where
        when = str(sched.get("next_run", ""))[:16]
        state = "paused" if sched.get("paused") else f"next {when}"
        print_text(f"{verb} {where}: {sched.get('schedule', '')} ({state})")

    return emit(args, data=result.output, text=text)
