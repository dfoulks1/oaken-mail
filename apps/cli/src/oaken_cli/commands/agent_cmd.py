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


# --- jobs: one group, two audiences --------------------------------------------------------
# The person inspects and steers the durable queue (list/show/cancel/retry) with the API token;
# an external agent works it (claim/heartbeat/complete/fail) with its own token. Same `jobs` group,
# routed to the right client by the subcommand.

_AGENT_ACTIONS = frozenset({"claim", "heartbeat", "complete", "fail"})


def cmd_jobs(args: argparse.Namespace) -> int:
    if args.action in _AGENT_ACTIONS:
        return run_agent(lambda client: _agent_jobs(client, args), args)
    return run(lambda client: _queue_jobs(client, args), args)


async def _queue_jobs(client: ApiClient, args: argparse.Namespace) -> int:
    """The person's view of the durable queue: list, show, cancel, retry (API token)."""
    if args.action == "list":
        payload: dict[str, Any] = {"limit": args.limit}
        if args.status:
            payload["status"] = args.status
        if args.kind:
            payload["kind"] = args.kind
        if args.account:
            payload["account_id"] = args.account
        result = await client.call("list_jobs", payload)
        out: dict[str, Any] = result.output if isinstance(result.output, dict) else {}
        rows = out.get("jobs", [])

        def listed() -> None:
            from oaken_cli.output import print_table, print_text

            if not rows:
                print_text("no jobs")
                return
            print_table(
                rows,
                ["id", "kind", "status", "attempts", "max_attempts", "account_id", "last_error"],
                title="jobs",
            )

        return emit(args, data=result.output, text=listed)
    if args.action == "show":
        # A single job is a filtered list by id, so the person gets the same fields without a
        # dedicated op (the id is unique).
        result = await client.call("list_jobs", {"limit": 500})
        out = result.output if isinstance(result.output, dict) else {}
        match = next((j for j in out.get("jobs", []) if j.get("id") == args.job_id), None)
        if match is None:
            raise CliError(f"no such job: {args.job_id}", ExitCode.ERROR)

        def shown() -> None:
            from oaken_cli.output import print_text

            print_text(json.dumps(match, indent=2, ensure_ascii=False))

        return emit(args, data=match, text=shown)
    op = "cancel_job" if args.action == "cancel" else "retry_job"
    result = await client.call(op, {"job_id": args.job_id}, confirm=True)
    out = result.output if isinstance(result.output, dict) else {}

    def changed() -> None:
        from oaken_cli.output import print_text

        print_text(f"job {args.job_id}: {out.get('status', 'ok')}")

    return emit(args, data=result.output, text=changed)


async def _agent_jobs(client: Any, args: argparse.Namespace) -> int:
    if args.action == "claim":
        data = await client.claim(list(args.kind) or None, args.max_chars)
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
        owed = data.get("still_owed", 0)
        note = f"; {owed} enrichment(s) still owed, job requeued" if owed else ""
        return emit(args, data=data, text=lambda: _say(f"job completed{note}"))
    data = await client.fail(
        args.job_id, args.error, permanent=bool(args.permanent), retry=bool(args.retry)
    )
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
