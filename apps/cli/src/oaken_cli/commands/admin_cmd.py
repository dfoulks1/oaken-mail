# SPDX-License-Identifier: Apache-2.0
"""``oaken settings | storage | vacuum | cache | events``: looking after a running core.

Each is a thin call to one or two registry operations (``get/check/set_settings``,
``storage_info``, ``vacuum``, ``cache_stats``/``clear_cache``, ``events_tail``); the operations are
core's, and the generic ``oaken operation NAME`` reaches them too. What lives here is presentation:
sizes in human units, and warnings that are hard to miss (a saved setting that needs a reload, or
that an environment variable will override).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from oaken_cli.client import ApiClient, CallResult
from oaken_cli.commands.mail_cmd import _wait_for_job
from oaken_cli.commands.plugin_cmd import parse_pair
from oaken_cli.commands.runtime import emit, run
from oaken_cli.exits import CliError, ExitCode

ACTIONS = ("get", "check", "set")


def _usage(message: str) -> CliError:
    return CliError(message, ExitCode.USAGE)


def _out(result: CallResult) -> dict[str, Any]:
    return result.output if isinstance(result.output, dict) else {}


def human(n: int | float) -> str:
    """``84 MiB``: bytes in the largest unit that keeps the number readable."""
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"  # pragma: no cover


def _show(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


# --- settings -------------------------------------------------------------------------------


def cmd_settings(args: argparse.Namespace) -> int:
    return run(lambda client: _settings(client, args), args)


async def _settings(client: ApiClient, args: argparse.Namespace) -> int:
    words = list(args.words)
    action = words.pop(0) if words and words[0] in ACTIONS else "get"
    section = words.pop(0) if words else None
    if action == "get":
        if words or args.unset or args.reload:
            raise _usage("settings get takes only a section; change settings with 'settings set'")
        return await _get(client, args, section)
    if not section:
        raise _usage(f"usage: oaken settings {action} SECTION KEY=VALUE ... [--unset KEY]")
    values = dict(parse_pair(w) for w in words)
    if not values and not args.unset:
        raise _usage(f"settings {action} {section} needs KEY=VALUE or --unset KEY")
    payload = {"section": section, "values": values, "unset": list(args.unset or [])}
    return await (_check if action == "check" else _save)(client, args, payload)


async def _get(client: ApiClient, args: argparse.Namespace, section: str | None) -> int:
    result = await client.call("get_settings", {"section": section} if section else {})
    out = _out(result)

    def text() -> None:
        from oaken_cli.output import print_table, print_text

        for sec in out.get("sections", []):
            print_text(f"[{sec['table']}]  {sec['section']}: {sec['summary']}")
            rows = []
            for s in sec["settings"]:
                source = s["source"] + (f" ({s['env_var']})" if s.get("env_var") else "")
                rows.append(
                    {
                        "key": s["key"] + ("" if s["editable"] else "  (by hand)"),
                        "value": _show(s["value"]),
                        "default": _show(s["default"]),
                        "from": source,
                        "applies": {
                            "reload": "reload",
                            "reindex": "reload + rebuild-index",
                            "restart": "restart",
                        }[s["effect"]],  # fmt: skip
                    }
                )
            print_table(rows, ["key", "value", "default", "from", "applies"])
            print_text("")

    return emit(args, data=result.output, text=text)


def _problems(args: argparse.Namespace, out: dict[str, Any]) -> None:
    if out.get("ok", True) and not out.get("error"):
        return
    errors = out.get("errors") or []
    message = "; ".join(f"{e['key']}: {e['message']}" for e in errors) or str(out.get("error"))
    raise _usage(message) if errors else CliError(message, ExitCode.ERROR)


def _notes(out: dict[str, Any], *, saved: bool, reload_sent: bool) -> None:
    """What the person must do next, and the things that will surprise them. Printed whatever else
    was said: a setting that does nothing until a reload is the most common way to be confused."""
    from oaken_cli.output import print_text

    for warning in out.get("warnings", []):
        if warning != "nothing to change":
            print_text(f"WARNING: {warning}")
    if not out.get("changed"):
        return
    if out.get("restart_needed"):
        print_text("restart core to apply: oaken restart")
    elif out.get("reload_needed") and not reload_sent:
        verb = "saved" if saved else "would be saved"
        print_text(f"{verb}; apply it with: oaken reload  (or add --reload to settings set)")


async def _check(client: ApiClient, args: argparse.Namespace, payload: dict[str, Any]) -> int:
    result = await client.call("check_settings", payload)
    out = _out(result)

    def text() -> None:
        from oaken_cli.output import print_text

        if out["ok"]:
            print_text(f"ok: {', '.join(out['changed'])} would change" if out["changed"]
                       else "ok: nothing would change")  # fmt: skip
        _notes(out, saved=False, reload_sent=False)

    code = emit(args, data=result.output, text=text)
    _problems(args, out)
    return code


async def _save(client: ApiClient, args: argparse.Namespace, payload: dict[str, Any]) -> int:
    result = await client.call("set_settings", payload, confirm=True)
    out = _out(result)
    _problems(args, out)
    reloaded: CallResult | None = None
    if args.reload and out.get("changed") and not out.get("restart_needed"):
        reloaded = await client.call("config_reload", {}, confirm=True)

    def text() -> None:
        from oaken_cli.output import print_text

        if out["changed"]:
            print_text(f"saved {', '.join(out['changed'])} to {out['path']}")
            if out.get("backup"):
                print_text(f"previous file kept as {out['backup']}")
        else:
            print_text("nothing to change")
        if reloaded is not None:
            print_text("core is reloading its configuration (see its log for the outcome)")
        _notes(out, saved=True, reload_sent=reloaded is not None)

    data = {"saved": result.output, "reload": reloaded.output if reloaded else None}
    return emit(args, data=data if args.reload else result.output, text=text)


# --- storage and vacuum ---------------------------------------------------------------------


def cmd_storage(args: argparse.Namespace) -> int:
    return run(lambda client: _storage(client, args), args)


async def _storage(client: ApiClient, args: argparse.Namespace) -> int:
    result = await client.call("storage_info", {})
    out = _out(result)

    def text() -> None:
        from oaken_cli.output import print_table

        rows = [
            {"what": "raw.db", "size": human(out["raw_db_bytes"]), "where": out["raw_db"]},
            {"what": "index.db", "size": human(out["index_db_bytes"]), "where": out["index_db"]},
            {
                "what": "write-ahead logs",
                "size": human(out["wal_bytes"]),
                "where": "(in the data dir)",
            },
        ]
        if out.get("quarantine_dir"):
            files = out["quarantined_files"]
            rows.append(
                {
                    "what": "quarantine",
                    "size": f"{human(out['quarantine_bytes'])} in "
                    f"{files} file{'' if files == 1 else 's'}",
                    "where": out["quarantine_dir"],
                }
            )
        print_table(rows, ["what", "size", "where"], title=f"storage in {out['data_dir']}")

    return emit(args, data=result.output, text=text)


def cmd_vacuum(args: argparse.Namespace) -> int:
    return run(lambda client: _vacuum(client, args), args)


async def _vacuum(client: ApiClient, args: argparse.Namespace) -> int:
    before = _out(await client.call("storage_info", {})) if args.wait else {}
    result = await client.call("vacuum", {}, confirm=True)
    out = _out(result)
    job = str(out.get("job_id", ""))
    final = await _wait_for_job(client, job, args.timeout) if args.wait else None
    after = _out(await client.call("storage_info", {})) if args.wait else {}

    def text() -> None:
        from oaken_cli.output import print_text

        said = "queued" if out.get("created", True) else "already queued: joined it"
        print_text(f"vacuum {said} (job {job})" + (f" - {final}" if final else ""))
        if final == "succeeded":
            for name in ("raw_db", "index_db"):
                print_text(
                    f"{name.replace('_', '.')}: {human(before[name + '_bytes'])}"
                    f" -> {human(after[name + '_bytes'])}"
                )
        elif not args.wait:
            print_text("it holds the databases' write lock while it works; --wait follows it")

    data = {"request_id": result.request_id, "output": result.output, "status": final}
    code = emit(args, data=data, text=text)
    return int(ExitCode.ERROR) if final not in (None, "succeeded") else code


# --- the enrichment cache -------------------------------------------------------------------


def cmd_cache(args: argparse.Namespace) -> int:
    return run(lambda client: _cache(client, args), args)


async def _cache(client: ApiClient, args: argparse.Namespace) -> int:
    if args.action == "clear":
        return await _cache_clear(client, args)
    result = await client.call("cache_stats", {})
    out = _out(result)

    def text() -> None:
        from oaken_cli.output import print_table, print_text

        rate = (
            "no model calls yet" if out["hit_rate"] is None else f"{out['hit_rate']:.0%} of calls"
        )
        print_text(
            f"{out['entries']} cached answers, {human(out['bytes'])}; the cache saved "
            f"{out['hits']} of {out['hits'] + out['misses']} model calls ({rate})"
        )
        if out["by_task"]:
            print_table(
                [{"task": t, "answers": n} for t, n in sorted(out["by_task"].items())],
                ["task", "answers"],
            )
        if out["prompt_versions"]:
            print_text("prompt versions: " + ", ".join(out["prompt_versions"]))

    return emit(args, data=result.output, text=text)


async def _cache_clear(client: ApiClient, args: argparse.Namespace) -> int:
    stats = _out(await client.call("cache_stats", {}))
    scope = f"the {args.task} answers" if args.task else "every cached answer"
    count = stats["by_task"].get(args.task, 0) if args.task else stats["entries"]
    if count and not args.yes:
        if not sys.stdin.isatty():
            raise CliError(
                f"this would delete {count} cached answers; re-run with --yes to confirm",
                ExitCode.NEEDS_CONFIRMATION,
            )
        question = f"Delete {scope} ({count})? They are recomputed on demand. [y/N] "
        reply = await asyncio.to_thread(input, question)  # not blocking the event loop
        if reply.strip().lower() not in ("y", "yes"):
            raise CliError("not cleared", ExitCode.NEEDS_CONFIRMATION)
    result = await client.call(
        "clear_cache", {"task": args.task} if args.task else {}, confirm=True
    )
    out = _out(result)

    def text() -> None:
        from oaken_cli.output import print_text

        print_text(f"cleared {out['deleted']} cached answers (mail and the index are untouched)")

    return emit(args, data=result.output, text=text)


# --- events ---------------------------------------------------------------------------------


def cmd_events(args: argparse.Namespace) -> int:
    return run(lambda client: _events(client, args), args)


def _line(event: dict[str, Any]) -> str:
    data = {k: v for k, v in event["data"].items() if k not in ("ts", "event_id")}
    body = " ".join(f"{k}={_show(v)}" for k, v in data.items())
    return f"{event['ts'][11:19]}  {event['kind']}  {body}".rstrip()


async def _events(client: ApiClient, args: argparse.Namespace) -> int:
    from oaken_cli.output import print_json, print_text

    payload: dict[str, Any] = {"limit": args.tail}
    if args.kind:
        payload["kinds"] = list(args.kind)
    if args.since:
        payload["since"] = args.since
    result = await client.call("events_tail", payload)
    events = _out(result).get("events", [])
    as_json = getattr(args, "json", False)
    if as_json and not args.follow:
        print_json(result.output)
        return int(ExitCode.OK)
    for event in events:
        print_json(event) if as_json else print_text(_line(event))
    if not events and not args.follow and not as_json:
        print_text("no events")
    if args.follow:
        try:
            async for live in client.stream_events():
                if args.kind and live.event not in args.kind:
                    continue
                item = {
                    "ts": str(live.data.get("ts", "")),
                    "kind": live.event,
                    "data": dict(live.data),
                }
                print_json(item) if as_json else print_text(_line(item))
        except KeyboardInterrupt:
            pass
    return int(ExitCode.OK)
