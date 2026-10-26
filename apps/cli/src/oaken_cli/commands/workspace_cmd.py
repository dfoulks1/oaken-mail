# SPDX-License-Identifier: Apache-2.0
"""``oaken workspaces list|get|check|set|remove``: workspace management through core.

Workspaces are named confidentiality levels (prompt 10). Each has a trust level and an LLM
backend. A ``secure`` workspace only talks to a local backend.

    oaken workspaces                      # list all (same as 'list')
    oaken workspaces list
    oaken workspaces get private
    oaken workspaces check private trust=insecure
    oaken workspaces set private --file private.toml
    oaken workspaces set cloud 'trust = "insecure"' 'llm_backend = "ollama-remote"'
    oaken workspaces remove cloud
"""

from __future__ import annotations

import sys
from typing import Any

from oaken_cli.client import ApiClient, CallResult
from oaken_cli.commands.runtime import emit
from oaken_cli.exits import CliError, ExitCode

SUBCOMMANDS = ("list", "get", "check", "set", "remove")


def _usage(message: str) -> CliError:
    return CliError(message, ExitCode.USAGE)


def _out(result: CallResult) -> dict[str, Any]:
    return result.output if isinstance(result.output, dict) else {}


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        raise CliError(f"cannot read {path}: {exc.strerror}", ExitCode.USAGE) from None


async def cmd_workspaces(client: ApiClient, args: Any) -> int:
    sub = getattr(args, "subcommand", "list") or "list"

    if sub == "list":
        return await _list(client, args)
    if sub == "get":
        if not args.name:
            raise _usage("workspaces get needs a workspace name")
        return await _get(client, args)
    if sub == "check":
        if not args.name:
            raise _usage("workspaces check needs a workspace name")
        return await _check(client, args)
    if sub == "set":
        if not args.name:
            raise _usage("workspaces set needs a workspace name")
        return await _set(client, args)
    if sub == "remove":
        if not args.name:
            raise _usage("workspaces remove needs a workspace name")
        return await _remove(client, args)
    raise _usage(f"unknown workspaces subcommand {sub!r}")


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


async def _list(client: ApiClient, args: Any) -> int:
    result = await client.call("list_workspaces", {})
    out = _out(result)

    def text() -> None:
        from oaken_cli.output import print_text

        workspaces = out.get("workspaces", [])
        if not workspaces:
            print_text("no workspaces configured (only 'default' exists)")
            return
        for ws in workspaces:
            default_mark = " (default)" if ws.get("is_default") else ""
            trust = ws.get("trust", "secure")
            backend = ws.get("llm_backend", "")
            usage = ws.get("usage", {})
            calls = usage.get("calls", 0)
            print_text(
                f"{ws['name']}{default_mark}  trust={trust}  backend={backend}  calls={calls}"
            )

    return emit(args, data=result.output, text=text)


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


async def _get(client: ApiClient, args: Any) -> int:
    result = await client.call("get_workspace", {"name": args.name})
    out = _out(result)

    def text() -> None:
        from oaken_cli.output import print_text

        print_text(f"[workspaces.{out.get('name', args.name)}]")
        print_text(out.get("toml", "").rstrip("\n"))
        print_text(f"# effective backend: {out.get('llm_backend', '')}")
        usage = out.get("usage", {})
        print_text(
            f"# usage: calls={usage.get('calls', 0)}  "
            f"tokens_in={usage.get('tokens_in', 0)}  "
            f"tokens_out={usage.get('tokens_out', 0)}  "
            f"cache_entries={'?' if usage.get('cache_entries') is None else usage['cache_entries']}"
        )

    return emit(args, data=result.output, text=text)


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


async def _check(client: ApiClient, args: Any) -> int:
    toml = _toml_from_args(args)
    payload = {"name": args.name, "toml": toml}
    result = await client.call("check_workspace", payload)
    out = _out(result)

    def text() -> None:
        from oaken_cli.output import print_text

        for err in out.get("errors", []):
            print_text(f"error: {err['key']}: {err['message']}")
        for warn in out.get("warnings", []):
            print_text(f"warning: {warn}")
        if out.get("valid"):
            print_text(f"ok  effective_backend={out.get('effective_backend', '')}")

    code = emit(args, data=result.output, text=text)
    return code if out.get("valid") else int(ExitCode.ERROR)


# ---------------------------------------------------------------------------
# set
# ---------------------------------------------------------------------------


async def _set(client: ApiClient, args: Any) -> int:
    toml = _toml_from_args(args)
    if not toml.strip() and not getattr(args, "file", None):
        raise _usage(
            "workspaces set needs settings: pass TOML lines as arguments or use --file FILE"
        )
    payload = {"name": args.name, "toml": toml}
    result = await client.call("set_workspace", payload, confirm=True)
    out = _out(result)
    if not out.get("ok"):
        raise CliError(str(out.get("error") or "not saved"), ExitCode.ERROR)

    def text() -> None:
        from oaken_cli.output import print_text

        print_text(
            f"saved [workspaces.{args.name}] to {out['path']}"
            + (f" (backup: {out['backup']})" if out.get("backup") else "")
        )

    return emit(args, data=result.output, text=text)


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


async def _remove(client: ApiClient, args: Any) -> int:
    result = await client.call("remove_workspace", {"name": args.name}, confirm=True)
    out = _out(result)
    if not out.get("removed"):
        raise CliError(str(out.get("error") or "not removed"), ExitCode.ERROR)

    def text() -> None:
        from oaken_cli.output import print_text

        print_text(
            f"removed [workspaces.{args.name}]"
            + (f" (backup: {out['backup']})" if out.get("backup") else "")
        )
        print_text("LLM cache and usage rows kept; clear with: oaken cache clear")

    return emit(args, data=result.output, text=text)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _toml_from_args(args: Any) -> str:
    """Build a TOML snippet from --file or positional TOML lines."""
    if getattr(args, "file", None):
        return sys.stdin.read() if args.file == "-" else _read(args.file)
    lines = getattr(args, "lines", []) or []
    return "\n".join(lines) + ("\n" if lines else "")
