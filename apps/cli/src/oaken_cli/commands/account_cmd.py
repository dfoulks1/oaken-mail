# SPDX-License-Identifier: Apache-2.0
"""``oaken admin account list|show|add|set|remove``.

The account wizard asks the questions and sends the answers to core, which stores them, so nobody
edits a file by hand. Only non-secret settings are asked for or stored in the account. A credential
is the provider's own ``auth`` command (``oaken gmail auth``, ``oaken imap auth``): a password is
read without echo (or from stdin for scripts), sent once to core over loopback, and kept by the
provider in the OS keyring. The CLI never prints it, never takes it as an argument, and never
writes it to disk.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from oaken_cli.client import ApiClient
from oaken_cli.commands.runtime import emit, run
from oaken_cli.exits import CliError, ExitCode

SECRETISH = ("password", "passwd", "secret", "token", "credential", "api_key", "apikey")


# --- admin account --------------------------------------------------------------------------


def cmd_admin(args: argparse.Namespace) -> int:
    if args.action == "add":
        _require_answers_or_tty(args)
    return run(lambda client: _admin(client, args), args)


def _require_answers_or_tty(args: argparse.Namespace) -> None:
    if (args.provider and args.account_id and args.name) or sys.stdin.isatty():
        return
    raise CliError(
        "no terminal to ask on: give --provider, --id and --name (and --set KEY=VALUE)",
        ExitCode.USAGE,
    )


async def _admin(client: ApiClient, args: argparse.Namespace) -> int:
    if args.action == "list":
        result = await client.call("list_accounts", {})
        rows = result.output.get("accounts", []) if isinstance(result.output, dict) else []

        def listed() -> None:
            from oaken_cli.output import print_table, print_text

            if not rows:
                print_text('no accounts; add one with "oaken admin account add"')
                return
            for r in rows:
                r["send"] = "no (receive-only)" if r.get("receive_only") else "yes"
            print_table(rows, ["account_id", "provider", "display_name", "send"], title="accounts")

        return emit(args, data=result.output, text=listed)
    if args.action == "show":
        result = await client.call("get_account", {"account_id": args.account_id})
        return emit(args, data=result.output, text=lambda: _show(result.output))
    return await _add(client, args)


def _show(out: Any) -> None:
    from oaken_cli.output import print_text

    print_text(f"{out['account_id']}: {out['provider']} ({out['display_name']})")
    if out.get("receive_only"):
        print_text("  receive-only (listen but not send)")
    caps = out.get("capabilities") or []
    if caps:
        print_text(f"  capabilities: {', '.join(caps)}")
    for key, value in out.get("config", {}).items():
        print_text(f"  {key} = {json.dumps(value)}")
    if not out.get("config"):
        print_text("  no settings")
    if out.get("problem"):
        print_text(f"problem: {out['problem']}")


def cmd_set(args: argparse.Namespace) -> int:
    return run(lambda client: _set(client, args), args)


async def _set(client: ApiClient, args: argparse.Namespace) -> int:
    # Read the current config, merge the changes, and write the whole thing back (the op replaces).
    current = await client.call("get_account", {"account_id": args.account_id})
    out = current.output if isinstance(current.output, dict) else {}
    config = dict(out.get("config", {}))
    config.update(_parse_settings(args.set))
    receive_only = getattr(args, "receive_only", None)
    if receive_only is True:
        config["receive_only"] = True
    elif receive_only is False:
        config.pop("receive_only", None)
    result = await client.call(
        "set_account_config", {"account_id": args.account_id, "config": config}, confirm=True
    )
    body = result.output if isinstance(result.output, dict) else {}

    def done() -> None:
        from oaken_cli.output import print_text

        account = body.get("account", {})
        _show(account)
        if body.get("restart_needed"):
            print_text("(the running provider keeps its old settings until core restarts)")

    return emit(args, data=result.output, text=done)


def cmd_remove(args: argparse.Namespace) -> int:
    return run(lambda client: _remove(client, args), args)


async def _remove(client: ApiClient, args: argparse.Namespace) -> int:
    result = await client.call("remove_account", {"account_id": args.account_id}, confirm=True)
    out = result.output if isinstance(result.output, dict) else {}

    def done() -> None:
        from oaken_cli.output import print_text

        print_text(
            f"account {args.account_id} removed ({out.get('schedules_removed', 0)} schedules)"
        )
        if out.get("mail_kept"):
            print_text("stored mail is kept; purge is a separate operation")
        if out.get("restart_needed"):
            print_text("(a started provider keeps serving until core restarts)")

    return emit(args, data=result.output, text=done)


async def _add(client: ApiClient, args: argparse.Namespace) -> int:
    providers = await _providers(client)
    interactive = sys.stdin.isatty() and not (args.provider and args.account_id and args.name)
    provider = args.provider or _ask(f"provider ({', '.join(sorted(providers))})", "", interactive)
    if provider not in providers:
        raise CliError(
            f"no provider named {provider!r}; installed: {', '.join(sorted(providers))}",
            ExitCode.USAGE,
        )
    account_id = args.account_id or _ask("account id (a-z, 0-9, - and _)", provider, interactive)
    name = args.name or _ask("display name", account_id, interactive)
    config = _parse_settings(args.set)
    schema = providers[provider].get("config_schema") or {}
    if interactive:
        config = _wizard(schema, config)
    # What a new account of this provider starts with is the provider's own word (its
    # ``account_defaults``, from list_plugins): core and this client name no provider. Receive-only
    # is the "listen but not send" switch; a provider that defaults to it asks first.
    defaults = dict(providers[provider].get("account_defaults") or {})
    receive_only = getattr(args, "receive_only", None)
    if receive_only is None and "receive_only" in defaults:
        receive_only = bool(defaults.pop("receive_only"))
        if interactive:
            reply = _ask(
                "receive only (listen but not send)? [Y/n]", "y" if receive_only else "n", True
            )
            receive_only = reply.lower() in ("y", "yes")
    defaults.pop("receive_only", None)
    for key, value in defaults.items():
        config.setdefault(key, value)
    if receive_only:
        config["receive_only"] = True
    result = await client.call(
        "create_account",
        {"account_id": account_id, "provider": provider, "display_name": name, "config": config},
        confirm=True,
    )

    def done() -> None:
        from oaken_cli.output import print_text

        print_text(f"account {account_id} added")
        print_text(
            f'next: "oaken {provider} auth --account-id {account_id}" to store its credential'
        )

    return emit(args, data=result.output, text=done)


async def _providers(client: ApiClient) -> dict[str, dict[str, Any]]:
    result = await client.call("list_plugins", {})
    plugins = result.output.get("plugins", []) if isinstance(result.output, dict) else []
    return {p["name"]: p for p in plugins if p.get("kind") == "provider" and "@" not in p["name"]}


def _ask(question: str, default: str, interactive: bool) -> str:
    if not interactive:
        raise CliError(f"missing {question}", ExitCode.USAGE)
    shown = f" [{default}]" if default else ""
    answer = input(f"{question}{shown}: ").strip()
    return answer or default


def _parse_settings(pairs: list[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise CliError(f"--set takes KEY=VALUE, not {pair!r}", ExitCode.USAGE)
        out[key] = _coerce(value)
    return out


def _coerce(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def _wizard(schema: dict[str, Any], given: dict[str, Any]) -> dict[str, Any]:
    """Ask for each non-secret setting the provider declares, showing its default."""
    config = dict(given)
    for key, spec in schema.get("properties", {}).items():
        if key in config or any(s in key.lower() for s in SECRETISH):
            continue
        default = spec.get("default")
        hint = spec.get("description") or spec.get("title") or ""
        shown = "" if default is None else f" [{json.dumps(default)}]"
        answer = input(f"{key}{shown}{' - ' + hint if hint else ''}: ").strip()
        if answer:
            config[key] = _coerce(answer)
    return config
