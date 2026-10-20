# SPDX-License-Identifier: Apache-2.0
"""``oaken agents ...`` (a person managing agents) and ``oaken jobs ...`` (an agent working).

``agents create|list|revoke`` are registry operations (never offered over MCP), called with the
person's API token like any other command. ``jobs claim|heartbeat|complete|fail`` are the agent job
protocol and are authenticated by the *agent's* token alone, so a scripted worker needs nothing but
that token. See the swarm page in the docs.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from oaken_cli.client import ApiClient
from oaken_cli.commands.runtime import emit, run, run_agent
from oaken_cli.exits import CliError, ExitCode

SHOWN_ONCE = (
    "this token is shown once and cannot be shown again; store it now "
    "(an agent reads it from OAKEN_AGENT_TOKEN)."
)


# --- agents (registry operations) -----------------------------------------------------------


def cmd_agents(args: argparse.Namespace) -> int:
    return run(lambda client: _agents(client, args), args)


async def _agents(client: ApiClient, args: argparse.Namespace) -> int:
    if args.action == "create":
        return await _create(client, args)
    if args.action == "revoke":
        result = await client.call("revoke_agent", {"agent_id": args.agent_id}, confirm=True)
        out: dict[str, Any] = result.output if isinstance(result.output, dict) else {}

        def revoked() -> None:
            from oaken_cli.output import print_text

            print_text(
                "agent revoked; its jobs are back on the queue"
                if out.get("revoked")
                else "agent was already revoked"
            )

        return emit(args, data=result.output, text=revoked)
    result = await client.call("list_agents", {"include_revoked": bool(args.all)})
    listing: dict[str, Any] = result.output if isinstance(result.output, dict) else {}
    rows = listing.get("agents", [])

    def listed() -> None:
        from oaken_cli.output import print_table, print_text

        if not rows:
            print_text("no agents")
            return
        print_table(
            rows, ["id", "label", "kinds", "max_leases", "max_per_hour", "revoked"], title="agents"
        )

    return emit(args, data=result.output, text=listed)


async def _create(client: ApiClient, args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {"label": args.label, "kinds": list(args.kind)}
    if args.account:
        payload["account_ids"] = list(args.account)
    if args.max_leases is not None:
        payload["max_leases"] = args.max_leases
    if args.max_per_hour is not None:
        payload["max_per_hour"] = args.max_per_hour
    result = await client.call("create_agent", payload, confirm=True)
    out: dict[str, Any] = result.output if isinstance(result.output, dict) else {}

    def text() -> None:
        from oaken_cli.output import print_text

        agent = out.get("agent", {})
        print_text(f"agent {agent.get('id', '?')} ({agent.get('label', '')}) created")
        print_text(f"token: {out.get('token', '')}")
        print_text(f"warning: {SHOWN_ONCE}")

    return emit(args, data=result.output, text=text)


# --- jobs (the agent protocol) --------------------------------------------------------------


def cmd_jobs(args: argparse.Namespace) -> int:
    return run_agent(lambda client: _jobs(client, args), args)


async def _jobs(client: Any, args: argparse.Namespace) -> int:
    if args.action == "claim":
        data = await client.claim(list(args.kind) or None)
        job = data.get("job")

        def claimed() -> None:
            from oaken_cli.output import print_text

            if job is None:
                print_text("no job available")
                return
            print_text(f"job {job['id']} ({job['kind']}), attempt {job['attempts']}")
            print_text(json.dumps(job["payload"], ensure_ascii=False))

        return emit(args, data=data, text=claimed)
    if args.action == "heartbeat":
        data = await client.heartbeat(args.job_id)

        def beat() -> None:
            from oaken_cli.output import print_text

            print_text("cancel requested" if data.get("cancel_requested") else "lease extended")

        return emit(args, data=data, text=beat)
    if args.action == "complete":
        data = await client.complete(args.job_id, _read_result(args), args.produced_by)
        return emit(args, data=data, text=lambda: _say("job completed"))
    data = await client.fail(args.job_id, args.error, permanent=bool(args.permanent))
    return emit(args, data=data, text=lambda: _say(f"job {data.get('status', 'failed')}"))


def _say(message: str) -> None:
    from oaken_cli.output import print_text

    print_text(message)


def _read_result(args: argparse.Namespace) -> dict[str, Any] | None:
    raw = args.result
    if raw == "-":
        raw = sys.stdin.read().strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CliError(f"--result is not valid JSON: {exc}", ExitCode.USAGE) from None
    if not isinstance(value, dict):
        raise CliError("--result must be a JSON object", ExitCode.USAGE)
    return value
