# SPDX-License-Identifier: Apache-2.0
"""``oaken enrich run|show`` and ``oaken sieve check|test|list|get|save|delete|refilter``.

Each is one registry operation. The Sieve commands read the script file here (it lives on the
person's disk) and send its text; core compiles and runs it, so the CLI has no Sieve logic. A script
that does not compile exits 1 with core's message, which names the line. ``save`` compiles before it
persists (core refuses an uncompilable script), and ``--suggest-name`` prints the next free
``NN-name.sieve`` since run order is by name. Writing and deleting scripts is confirm-gated and
never offered over MCP.
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

from oaken_cli.client import ApiClient
from oaken_cli.commands.runtime import emit, run
from oaken_cli.exits import CliError, ExitCode

MAX_SCRIPT = 1_000_000
MAX_EML = 8 * 1024 * 1024


# --- enrich ---------------------------------------------------------------------------------


def cmd_enrich(args: argparse.Namespace) -> int:
    return run(lambda client: _enrich(client, args), args)


async def _enrich(client: ApiClient, args: argparse.Namespace) -> int:
    names = list(args.enricher) or None
    if args.action == "show":
        payload: dict[str, object] = {"message_id": args.message_id}
        if names:
            if len(names) > 1:
                raise CliError("enrich show takes at most one ENRICHER", ExitCode.USAGE)
            payload["enricher"] = names[0]
        result = await client.call("get_enrichment", payload)
        out = result.output if isinstance(result.output, dict) else {}

        def shown() -> None:
            from oaken_cli.output import print_text

            for name, data in out.get("results", {}).items():
                print_text(f"{name}:")
                print_text(json.dumps(data, indent=2, ensure_ascii=False))

        return emit(args, data=result.output, text=shown)
    payload = {"message_id": args.message_id, "force": bool(args.force)}
    if names:
        payload["enrichers"] = names
    result = await client.call("enrich_message", payload, confirm=True)
    out = result.output if isinstance(result.output, dict) else {}

    def ran() -> None:
        from oaken_cli.output import print_text

        print_text(
            f"ran {out.get('ran', 0)}, already current {out.get('cached', 0)}; "
            f"results: {', '.join(out.get('results', [])) or 'none'}"
        )

    return emit(args, data=result.output, text=ran)


# --- sieve ----------------------------------------------------------------------------------


def _read_script(path: str) -> tuple[str, str]:
    if path == "-":
        import sys

        text = sys.stdin.read()
        if len(text.encode("utf-8", "surrogatepass")) > MAX_SCRIPT:
            raise CliError(f"stdin is larger than {MAX_SCRIPT} bytes", ExitCode.USAGE)
        return "script.sieve", text
    p = Path(path).expanduser()
    try:
        if p.stat().st_size > MAX_SCRIPT:
            raise CliError(f"{path} is larger than {MAX_SCRIPT} bytes", ExitCode.USAGE)
        return p.name, p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CliError(f"cannot read {path}: {exc}", ExitCode.USAGE) from None


def _read_eml(path: str) -> str:
    eml = Path(path).expanduser()
    try:
        if eml.stat().st_size > MAX_EML:
            raise CliError(f"{path} is larger than {MAX_EML} bytes", ExitCode.USAGE)
        return base64.b64encode(eml.read_bytes()).decode()
    except OSError as exc:
        raise CliError(f"cannot read {path}: {exc}", ExitCode.USAGE) from None


def cmd_sieve(args: argparse.Namespace) -> int:
    # Files are read here, before the event loop starts, so a bad path fails fast and cheaply.
    if args.action in ("check", "test"):
        args.script_name, args.script_text = _read_script(args.file)
    if args.action == "test" and args.eml:
        args.eml_b64 = _read_eml(args.eml)
    if args.action == "save" and not args.suggest_name:
        if not args.name or not args.file:
            raise CliError("save takes a script NAME and a FILE", ExitCode.USAGE)
        _, args.script_text = _read_script(args.file)
    return run(lambda client: _sieve(client, args), args)


def _next_name(existing: list[str], base: str) -> str:
    """The next free ``NN-base.sieve``: scan the two-digit prefixes in use and pick the next slot.
    Run order is by file name (prompt 26 Q2), so a fresh script sorts after the current ones."""
    import re

    used = set()
    for name in existing:
        m = re.match(r"^(\d{2})-", name)
        if m:
            used.add(int(m.group(1)))
    nn = 10
    while nn in used and nn < 100:
        nn += 10
    while nn in used and nn < 100:
        nn += 1
    return f"{nn:02d}-{base}.sieve"


async def _sieve(client: ApiClient, args: argparse.Namespace) -> int:
    if args.action == "check":
        return await _check(client, args)
    if args.action == "test":
        return await _test(client, args)
    if args.action == "get":
        return await _get(client, args)
    if args.action == "save":
        return await _save(client, args)
    if args.action == "delete":
        result = await client.call("sieve_delete", {"name": args.name}, confirm=True)
        out = result.output if isinstance(result.output, dict) else {}

        def deleted() -> None:
            from oaken_cli.output import print_text

            print_text(f"{args.name}: {'deleted' if out.get('deleted') else 'was not there'}")

        emit(args, data=result.output, text=deleted)
        return int(ExitCode.OK if out.get("deleted") else ExitCode.ERROR)
    if args.action == "refilter":
        payload: dict[str, object] = {"stage": "filter"}
        if args.account:
            payload["account_id"] = args.account
        result = await client.call("reprocess", payload, confirm=True)
        out = result.output if isinstance(result.output, dict) else {}

        def queued() -> None:
            from oaken_cli.output import print_text

            print_text(f"refilter queued (job {out.get('job_id', '?')})")

        return emit(args, data=result.output, text=queued)
    result = await client.call("sieve_list", {})
    out = result.output if isinstance(result.output, dict) else {}

    def listed() -> None:
        from oaken_cli.output import print_table, print_text

        print_text(f"{out.get('directory', '')}" + ("" if out.get("enabled") else " (disabled)"))
        print_table(out.get("scripts", []), ["name", "sha256"], title="scripts")
        for p in out.get("problems", []):
            print_text(f"skipped {p['name']}: {p['problem']}")

    return emit(args, data=result.output, text=listed)


async def _check(client: ApiClient, args: argparse.Namespace) -> int:
    name, text = args.script_name, args.script_text
    result = await client.call("sieve_check", {"script": text, "name": name})
    out = result.output if isinstance(result.output, dict) else {}

    def shown() -> None:
        from oaken_cli.output import print_text

        if out.get("ok"):
            needs = ", ".join(out.get("requires", [])) or "no extensions"
            print_text(f"{name}: ok ({needs})")
        else:
            print_text(f"{name}: {out.get('error')}")

    emit(args, data=result.output, text=shown)
    return int(ExitCode.OK if out.get("ok") else ExitCode.ERROR)


async def _test(client: ApiClient, args: argparse.Namespace) -> int:
    name, text = args.script_name, args.script_text
    payload: dict[str, object] = {"script": text, "name": name}
    if args.message:
        payload["message_id"] = args.message
    else:
        payload["eml_base64"] = args.eml_b64
    result = await client.call("sieve_test", payload)
    out = result.output if isinstance(result.output, dict) else {}
    check = out.get("check", {})

    def shown() -> None:
        from oaken_cli.output import print_text

        if not check.get("ok"):
            print_text(f"{name}: {check.get('error')}")
            return
        print_text(f"disposition: {out.get('disposition')}  ({out.get('rule')})")
        for e in out.get("effects", []):
            print_text(f"  {e['kind']} {e['value']}  [{e['rule']}]")
        if not out.get("effects"):
            print_text("  no actions")
        for err in out.get("errors", []):
            print_text(f"error: {err}")

    emit(args, data=result.output, text=shown)
    return int(ExitCode.OK if check.get("ok") and not out.get("errors") else ExitCode.ERROR)


async def _get(client: ApiClient, args: argparse.Namespace) -> int:
    result = await client.call("sieve_get", {"name": args.name})
    out = result.output if isinstance(result.output, dict) else {}

    def shown() -> None:
        from oaken_cli.output import print_text

        print_text(out.get("text", ""))

    return emit(args, data=result.output, text=shown)


async def _save(client: ApiClient, args: argparse.Namespace) -> int:
    if args.suggest_name:
        listing = await client.call("sieve_list", {})
        body = listing.output if isinstance(listing.output, dict) else {}
        names = [s["name"] for s in body.get("scripts", [])]
        suggestion = _next_name(names, args.base)

        def named() -> None:
            from oaken_cli.output import print_text

            print_text(suggestion)

        return emit(args, data={"name": suggestion}, text=named)
    result = await client.call(
        "sieve_save", {"name": args.name, "text": args.script_text}, confirm=True
    )
    out = result.output if isinstance(result.output, dict) else {}
    check = out.get("check", {})

    def shown() -> None:
        from oaken_cli.output import print_text

        if out.get("saved"):
            print_text(f"{args.name}: saved (sha256 {out.get('sha256', '')[:12]})")
            print_text("takes effect on the next filter run; 'oaken sieve refilter' re-runs now")
        else:
            print_text(f"{args.name}: not saved — {check.get('error', 'does not compile')}")

    emit(args, data=result.output, text=shown)
    return int(ExitCode.OK if out.get("saved") else ExitCode.ERROR)
