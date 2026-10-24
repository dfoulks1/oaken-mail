# SPDX-License-Identifier: Apache-2.0
"""``oaken plugins config | check | set NAME``: a plugin's settings, through core.

A plugin does not control itself: starting, stopping and configuring one is core's, so it is all
under ``oaken plugins`` (``enable``, ``disable`` and ``reload`` are in ``system_cmd``). These three
are thin calls to ``get_plugin_config``, ``check_plugin_config`` and ``set_plugin_config``.

    oaken plugins config gmail
    oaken plugins set gmail page_size=200 'include_labels=["INBOX"]' --unset timeout
    oaken plugins set imap --file imap.toml          # the plugin's own settings, comments kept
    oaken plugins check imap host=imap.example.com   # a throwaway instance; writes nothing
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from typing import Any

from oaken_cli.client import ApiClient, CallResult
from oaken_cli.commands.runtime import emit
from oaken_cli.exits import CliError, ExitCode

SUBCOMMANDS = ("config", "check", "set")


def _usage(message: str) -> CliError:
    return CliError(message, ExitCode.USAGE)


def parse_pair(pair: str) -> tuple[str, Any]:
    """``KEY=VALUE`` with VALUE as TOML (``200``, ``true``, ``["a"]``), or text if it is not."""
    key, sep, raw = pair.partition("=")
    if not sep or not key.strip():
        raise _usage(f"expected KEY=VALUE, got {pair!r}")
    try:
        return key.strip(), tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        return key.strip(), raw


def _payload(args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {"plugin": args.name}
    if args.pairs:
        payload["config"] = dict(parse_pair(p) for p in args.pairs)
    if args.unset:
        payload["unset"] = list(args.unset)
    if args.replace:
        payload["replace"] = True
    if args.file:
        payload["text"] = sys.stdin.read() if args.file == "-" else _read(args.file)
    return payload


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise CliError(f"cannot read {path}: {exc.strerror}", ExitCode.USAGE) from None


def _out(result: CallResult) -> dict[str, Any]:
    return result.output if isinstance(result.output, dict) else {}


async def settings(client: ApiClient, args: argparse.Namespace) -> int:
    sub = args.subcommand
    if not args.name:
        raise _usage(f"plugins {sub} needs a plugin name")
    if sub != "config" and not (
        args.pairs or args.unset or args.file or args.replace or sub == "check"
    ):
        raise _usage(f"plugins set {args.name} needs KEY=VALUE, --unset KEY or --file FILE")
    if sub == "config" and (args.pairs or args.unset or args.file or args.replace):
        raise _usage("plugins config only shows; change settings with 'plugins set'")
    if sub == "config":
        return await _show(client, args)
    payload = _payload(args)
    if sub == "check":
        return await _check(client, args, payload)
    return await _save(client, args, payload)


async def _show(client: ApiClient, args: argparse.Namespace) -> int:
    result = await client.call("get_plugin_config", {"plugin": args.name})
    out = _out(result)

    def text() -> None:
        from oaken_cli.output import print_text

        if out.get("text"):
            print_text(out["text"].rstrip("\n"))
        extra = {k: v for k, v in out.get("config", {}).items() if k in ("enabled", "capabilities")}
        for key, value in extra.items():
            print_text(f"# {key} = {value!r}  (set by the host)")
        if not out.get("exists"):
            print_text(f"no [plugins.{args.name}] settings yet")
            if out.get("config_example"):
                print_text("\nan example to start from:\n" + out["config_example"].rstrip("\n"))

    return emit(args, data=result.output, text=text)


async def _check(client: ApiClient, args: argparse.Namespace, payload: dict[str, Any]) -> int:
    result = await client.call("check_plugin_config", payload)
    out = _out(result)

    def text() -> None:
        from oaken_cli.output import print_text

        for problem in out.get("problems", []):
            print_text(f"problem: {problem}")
        for probe in out.get("probes", []):
            say = "ok" if probe["ok"] else "not ok"
            print_text(
                f"{probe['kind']} {args.name}: {say}"
                + (f" ({probe['detail']})" if probe["detail"] else "")
            )
        if out.get("ok"):
            print_text("settings look good")

    code = emit(args, data=result.output, text=text)
    return code if out.get("ok") else int(ExitCode.ERROR)


async def _save(client: ApiClient, args: argparse.Namespace, payload: dict[str, Any]) -> int:
    result = await client.call("set_plugin_config", payload, confirm=True)
    out = _out(result)
    if not out.get("ok"):
        raise CliError(str(out.get("error") or "not saved"), ExitCode.ERROR)

    def text() -> None:
        from oaken_cli.output import print_text

        print_text(
            f"saved to {out['path']}" + (f" (backup: {out['backup']})" if out.get("backup") else "")
        )
        print_text(f"apply it with: oaken plugins reload {args.name}")

    return emit(args, data=result.output, text=text)
