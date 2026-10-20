# SPDX-License-Identifier: Apache-2.0
"""``oaken admin account list|show|add`` and ``oaken auth gmail|smtp|imap``.

The account wizard asks the questions and sends the answers to core, which stores them, so nobody
edits a file by hand. Only non-secret settings are asked for or stored in the account. A credential
is handled by ``auth``: a password is read without echo (or from stdin for scripts), sent once to
core over loopback, and kept by the provider in the OS keyring. The CLI never prints it, never
takes it as an argument, and never writes it to disk. Gmail's consent is core's: the CLI shows the
address, opens a browser if asked, and waits for core to report the result.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from typing import Any

from oaken_cli.client import ApiClient
from oaken_cli.commands.runtime import emit, run
from oaken_cli.exits import CliError, ExitCode

SECRETISH = ("password", "passwd", "secret", "token", "credential", "api_key", "apikey")
PROVIDERS = ("gmail", "smtp", "imap")


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
            print_table(rows, ["account_id", "provider", "display_name"], title="accounts")

        return emit(args, data=result.output, text=listed)
    if args.action == "show":
        result = await client.call("get_account", {"account_id": args.account_id})
        return emit(args, data=result.output, text=lambda: _show(result.output))
    return await _add(client, args)


def _show(out: Any) -> None:
    from oaken_cli.output import print_text

    print_text(f"{out['account_id']}: {out['provider']} ({out['display_name']})")
    for key, value in out.get("config", {}).items():
        print_text(f"  {key} = {json.dumps(value)}")
    if not out.get("config"):
        print_text("  no settings")
    if out.get("problem"):
        print_text(f"problem: {out['problem']}")


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
    result = await client.call(
        "create_account",
        {"account_id": account_id, "provider": provider, "display_name": name, "config": config},
        confirm=True,
    )

    def done() -> None:
        from oaken_cli.output import print_text

        print_text(f"account {account_id} added")
        print_text(f'next: "oaken auth {provider} --account {account_id}" to store its credential')

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


# --- auth -----------------------------------------------------------------------------------


def cmd_auth(args: argparse.Namespace) -> int:
    password = _read_password(args) if _needs_password(args) else None
    args.password_value = password
    return run(lambda client: _auth(client, args), args)


def _needs_password(args: argparse.Namespace) -> bool:
    return args.provider in ("smtp", "imap") and not args.check


def _read_password(args: argparse.Namespace) -> str:
    """Without echo from a terminal, or one line from stdin; never from an argument."""
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    elif sys.stdin.isatty():
        password = getpass.getpass(f"{args.provider} password (not shown): ")
    else:
        raise CliError(
            "no terminal to ask on; pipe the password with --password-stdin", ExitCode.USAGE
        )
    if not password:
        raise CliError("no password given", ExitCode.USAGE)
    return password


async def _account_for(client: ApiClient, args: argparse.Namespace) -> str:
    if args.account:
        return str(args.account)
    result = await client.call("list_accounts", {})
    rows = result.output.get("accounts", []) if isinstance(result.output, dict) else []
    mine = [a["account_id"] for a in rows if a.get("provider") == args.provider]
    if len(mine) == 1:
        return str(mine[0])
    if not mine:
        raise CliError(
            f'no {args.provider} account; add one with "oaken admin account add"', ExitCode.USAGE
        )
    raise CliError(
        f"several {args.provider} accounts ({', '.join(mine)}); use --account", ExitCode.USAGE
    )


async def _auth(client: ApiClient, args: argparse.Namespace) -> int:
    account = await _account_for(client, args)
    if args.check:
        return await _check(client, args, account)
    if args.provider == "gmail":
        return await _consent(client, args, account)
    await client.call(
        "set_account_password",
        {"account_id": account, "password": args.password_value},
        confirm=True,
    )
    args.password_value = None
    return await _check(client, args, account, stored=True)


async def _check(
    client: ApiClient, args: argparse.Namespace, account: str, *, stored: bool = False
) -> int:
    result = await client.call("check_account", {"account_id": account})
    out = result.output if isinstance(result.output, dict) else {}

    def text() -> None:
        from oaken_cli.output import print_text

        if stored:
            print_text(f"password for {account} stored in the OS keyring")
        print_text(
            f"{account}: login works"
            if out.get("ok")
            else f"{account}: {out.get('detail', 'failed')}"
        )

    emit(args, data={"account_id": account, "stored": stored, **out}, text=text)
    return int(ExitCode.OK if out.get("ok") else ExitCode.AUTH)


async def _consent(client: ApiClient, args: argparse.Namespace, account: str) -> int:
    started = await client.call("start_authorization", {"account_id": account}, confirm=True)
    url = str((started.output or {}).get("url", "")) if isinstance(started.output, dict) else ""
    if not args.json:
        from oaken_cli.output import print_text

        print_text(f"open this address to authorize {account}:\n{url}")
        if not args.no_browser and sys.stdout.isatty():
            import webbrowser

            webbrowser.open(url)
        print_text("waiting for Google to redirect back to this machine...")
    state, detail = await _wait_authorized(client, account, args.timeout)

    def text() -> None:
        from oaken_cli.output import print_text

        if state == "done":
            print_text(f"{account} authorized; the refresh token is in the OS keyring")
        else:
            print_text(f"{account}: {detail or 'authorization did not finish'}")

    emit(
        args, data={"account_id": account, "state": state, "detail": detail, "url": url}, text=text
    )
    return int(ExitCode.OK if state == "done" else ExitCode.AUTH)


async def _wait_authorized(client: ApiClient, account: str, timeout: float) -> tuple[str, str]:  # noqa: ASYNC109
    async def poll() -> tuple[str, str]:
        while True:
            result = await client.call("authorization_status", {"account_id": account})
            out = result.output if isinstance(result.output, dict) else {}
            if out.get("state") in ("done", "failed"):
                return str(out["state"]), str(out.get("detail", ""))
            await asyncio.sleep(0.5)

    try:
        return await asyncio.wait_for(poll(), timeout)
    except TimeoutError:
        return "failed", f"timed out after {timeout:.0f}s; nothing was changed"
