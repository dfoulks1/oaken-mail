# SPDX-License-Identifier: Apache-2.0
"""``oaken rule`` and ``oaken level``: the reader's own deterministic triage rules.

A rule says "when these conditions all hold, rate the mail this level and category". The first rule
in order that matches wins, with no model. ``oaken rule add NAME`` with no conditions walks through
it. Rules live in core's database and apply at once (core reloads that one plugin)."""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

from oaken_cli.client import ApiClient
from oaken_cli.commands.admin_cmd import _out
from oaken_cli.commands.runtime import emit, run
from oaken_cli.exits import CliError, ExitCode

# Kept in step with oaken_enrichers.triage_custom.WHEN_KEYS (the CLI never imports core).
CONDITIONS = {
    "from": "sender contains this text, or matches a glob such as *@boss.com",
    "to": "a recipient contains this text, or matches a glob",
    "subject": "subject matches a glob",
    "subject_contains": "subject contains this text",
    "body_contains": "body contains this text",
    "header_present": "this header exists (for example list-id)",
    "has_attachment": "true or false",
    "is_reply": "true or false",
}
CATEGORIES = (
    "personal work transactional notification newsletter automated other".split()
)
BOOLEAN = {"has_attachment", "is_reply"}


def parse_when(pairs: list[str]) -> dict[str, Any]:
    """``from=*@boss.com subject_contains=urgent`` -> the ``when`` mapping."""
    when: dict[str, Any] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        key = key.strip()
        if not sep or key not in CONDITIONS:
            raise CliError(
                f"{pair!r}: write KEY=VALUE with a KEY from: {', '.join(CONDITIONS)}",
                ExitCode.USAGE,
            )
        if key in BOOLEAN:
            if value.strip().lower() not in ("true", "false"):
                raise CliError(f"{key} is true or false", ExitCode.USAGE)
            when[key] = value.strip().lower() == "true"
        else:
            when[key] = value
    return when


def say(text: str) -> None:
    """What the wizard tells the person goes to stderr: stdout stays the result (--json)."""
    import sys

    print(text, file=sys.stderr)


def interactive() -> bool:
    import sys

    return sys.stdin.isatty()


async def ask(prompt: str) -> str:
    return (await asyncio.to_thread(input, prompt)).strip()


# -- levels -----------------------------------------------------------------------------------
def cmd_level(args: argparse.Namespace) -> int:
    return run(lambda client: _level(client, args), args)


async def _level(client: ApiClient, args: argparse.Namespace) -> int:
    if args.action != "list" and args.value is None:
        raise CliError(f"oaken level {args.action} needs the level's number", ExitCode.USAGE)
    if args.action in ("add", "rename") and not args.name:
        raise CliError(f"oaken level {args.action} NUMBER NAME needs a name", ExitCode.USAGE)
    if args.action == "add":
        result = await client.call(
            "triage_level_set", {"value": args.value, "name": args.name}, confirm=True
        )
    elif args.action == "rename":
        result = await client.call(
            "triage_level_set",
            {"value": args.number or args.value, "name": args.name, "renumber_from": args.value},
            confirm=True,
        )
    elif args.action == "rm":
        result = await client.call("triage_level_delete", {"value": args.value}, confirm=True)
    else:
        result = await client.call("triage_levels", {})

    def text() -> None:
        from oaken_cli.output import print_table

        print_table(
            [
                {"level": lv["value"], "name": lv["name"], "built-in": "yes" if lv["builtin"] else ""}
                for lv in _out(result)["levels"]
            ],
            ["level", "name", "built-in"],
        )

    return emit(args, data=result.output, text=text)


# -- rules ------------------------------------------------------------------------------------
def cmd_rule(args: argparse.Namespace) -> int:
    return run(lambda client: _rule(client, args), args)


async def _rule(client: ApiClient, args: argparse.Namespace) -> int:
    action = args.action
    if action == "test":
        when = parse_when(args.when or [])
        if not when:
            raise CliError("oaken rule test needs at least one --when KEY=VALUE", ExitCode.USAGE)
        from oaken_cli.output import print_text

        out = await _try(client, when, print_text)
        return emit(args, data=out, text=lambda: None)
    if action in ("diff", "ack", "revert", "apply", "discard"):
        return await _drift(client, args)
    if action != "list" and not args.name:
        raise CliError(f"oaken rule {action} needs the rule's NAME", ExitCode.USAGE)
    if action == "add":
        payload = await _gather(client, args)
        result = await client.call("triage_rule_set", payload, confirm=True)
    elif action in ("on", "off"):
        result = await client.call(
            "triage_rule_set", {"name": args.name, "enabled": action == "on"}, confirm=True
        )
    elif action == "rm":
        result = await client.call("triage_rule_delete", {"name": args.name}, confirm=True)
    elif action == "move":
        if args.to is None and not args.before:
            raise CliError("say where: --to PLACE or --before RULE", ExitCode.USAGE)
        payload = {"name": args.name, "to": args.to, "before": args.before}
        result = await client.call(
            "triage_rule_move", {k: v for k, v in payload.items() if v is not None}, confirm=True
        )
    else:
        result = await client.call("triage_rules", {})
    drift = _out(await client.call("triage_drift", {})) if action == "list" else None

    def text() -> None:
        from oaken_cli.output import print_table, print_text

        rules = _out(result)["rules"]
        if drift and drift["staged"]:
            print_text(f"{sum(len(v) for v in drift['staged'].values())} staged edit(s) not yet "
                       "written to config.toml: oaken rule apply (or oaken reload), "
                       "oaken rule discard")
        if drift and drift["changed"]:
            print_text("! config.toml's triage rules were edited by hand and not acknowledged "
                       "(the last known good copy is running): oaken rule diff")
        if not rules:
            print_text("no rules yet: oaken rule add NAME")
            return
        print_table(
            [
                {
                    "#": i,
                    "name": r["name"],
                    "on": "" if r["enabled"] else "off",
                    "level": f"{r['level']} {r['level_name']}",
                    "category": r["category"],
                    "when": " ".join(f"{k}={v}" for k, v in r["when"].items()),
                }
                for i, r in enumerate(rules, 1)
            ],
            ["#", "name", "on", "level", "category", "when"],
        )

    return emit(args, data=result.output, text=text)


async def _gather(client: ApiClient, args: argparse.Namespace) -> dict[str, Any]:
    """The rule from the flags. With all of them it is one command (for scripts); otherwise, in a
    terminal, the wizard asks for what is missing."""
    when = parse_when(args.when or [])
    level, category = args.level, args.category
    if level is not None and category is not None and when:
        return {"name": args.name, "level": level, "category": category, "when": when}
    if not interactive():
        raise CliError(
            "a rule needs --level, --category and at least one --when KEY=VALUE "
            "(or run it in a terminal to be asked)",
            ExitCode.USAGE,
        )
    return await wizard(client, args.name, when, level, category)


def _show_conditions(when: dict[str, Any]) -> None:
    
    say("Conditions so far (all must match):" if when else "No conditions yet.")
    for key, value in when.items():
        say(f"  {key} = {value}")


async def _conditions(when: dict[str, Any]) -> dict[str, Any]:
    """The condition menu: pick one, give its value, repeat until a blank pick. A condition picked
    again is replaced; a pick of ``-KEY`` removes it."""
    
    keys = list(CONDITIONS)
    while True:
        say("\nAdd a condition:")
        for i, key in enumerate(keys, 1):
            say(f"  {i}. {key:17} {CONDITIONS[key]}")
        _show_conditions(when)
        pick = await ask("Pick a number or name, -NAME to remove one, blank when done: ")
        if not pick:
            if when:
                return when
            say("A rule needs at least one condition.")
            continue
        if pick.startswith("-"):
            when.pop(pick[1:].strip(), None)
            continue
        key = keys[int(pick) - 1] if pick.isdigit() and 0 < int(pick) <= len(keys) else pick
        if key not in CONDITIONS:
            say(f"{pick!r} is not one of the conditions.")
            continue
        if key in BOOLEAN:
            answer = (await ask(f"{key}: yes or no? ")).lower()
            if answer not in ("y", "yes", "n", "no"):
                say("Say yes or no.")
                continue
            when[key] = answer in ("y", "yes")
        elif value := await ask(f"{key} = "):
            when[key] = value


async def _pick_level(client: ApiClient, level: int | None) -> int:
    
    levels = {lv["value"]: lv["name"] for lv in _out(await client.call("triage_levels", {}))["levels"]}
    say("\nLevels: " + ", ".join(f"{v} {n}" for v, n in levels.items()))
    while level is None or level not in levels:
        raw = await ask("Level for mail this rule matches [2]: ") or "2"
        level = int(raw) if raw.isdigit() else None
        if level not in levels:
            say(f"{raw!r} is not a defined level (oaken level add NUMBER NAME).")
    return level


async def _category(category: str | None) -> str:
    
    say("\nCategories: " + ", ".join(f"{i}. {c}" for i, c in enumerate(CATEGORIES, 1)))
    while category not in CATEGORIES:
        raw = await ask("Category [other]: ") or "other"
        category = CATEGORIES[int(raw) - 1] if raw.isdigit() and 0 < int(raw) <= len(CATEGORIES) else raw
        if category not in CATEGORIES:
            say(f"{raw!r} is not a category.")
    return category


async def _try(
    client: ApiClient, when: dict[str, Any], tell: Any = None
) -> dict[str, Any]:
    """Run the dry run and say what it found, with a warning when the rule looks wrong."""
    tell = tell or say
    
    try:
        out = _out(await client.call("triage_rule_test", {"when": when}))
    except Exception as exc:  # noqa: BLE001 - the rules plugin is off or missing: say so, go on
        if type(exc).__name__ == "CoreUnavailable":
            raise
        tell(f"\n(could not try it on your mail: {getattr(exc, 'message', None) or exc})")
        return {"tested": 0, "matched": 0, "sample_ids": []}
    if out["tested"] == 0:
        tell("\nThere is no stored mail to try it on yet.")
        return out
    tell(f"\nThis rule matches {out['matched']} of the {out['tested']} most recent messages."
               + (f" Examples: {', '.join(out['sample_ids'])} (oaken show ID)" if out["sample_ids"] else ""))  # fmt: skip
    if out["matched"] == 0:
        tell("! It matches none. Check the conditions (a glob needs *, text is matched whole).")
    elif out["tested"] >= 10 and out["matched"] * 2 > out["tested"]:
        tell("! It matches more than half of your mail. That is a very broad rule.")
    return out


async def wizard(
    client: ApiClient, name: str, when: dict[str, Any], level: int | None, category: str | None
) -> dict[str, Any]:
    """Walk through one rule: conditions, level, category, a test on recent mail, and a confirm.
    The answer is the rule to stage; nothing is written until `oaken rule apply`."""
    
    say(f"Rule {name!r}: mail that matches every condition gets a level and a category.")
    when = await _conditions(dict(when))
    level = await _pick_level(client, level)
    category = await _category(category)
    while True:
        await _try(client, when)
        say(f"\n{name}: when {when} -> level {level}, {category}")
        choice = (await ask("[s]tage it, [c]hange the conditions, [x] cancel: ")).lower()
        if choice in ("s", "stage", "y", "yes"):
            return {"name": name, "level": level, "category": category, "when": when}
        if choice in ("c", "change"):
            when = await _conditions(when)
        elif choice in ("x", "cancel", "n", "no"):
            raise CliError("cancelled: nothing was staged", ExitCode.NEEDS_CONFIRMATION)


DRIFT_WORDS = {
    "rule_added": "rules added",
    "rule_removed": "rules removed",
    "rule_changed": "rules changed",
    "rule_reordered": "rules moved",
    "level_added": "levels added",
    "level_removed": "levels removed",
    "level_changed": "levels changed",
    "level_reordered": "levels moved",
}


def describe_drift(out: dict[str, Any]) -> list[str]:
    if out.get("error"):
        return [f"config.toml's triage section is not valid: {out['error']}"]
    return [f"{DRIFT_WORDS.get(k, k)}: {', '.join(str(x) for x in v)}" for k, v in out["changes"].items()]


async def _drift(client: ApiClient, args: argparse.Namespace) -> int:
    """``oaken rule diff|apply|discard|ack|revert``. The file is trusted; the database holds a
    working copy (staged edits) and the last known good copy (what runs)."""
    op = {"ack": "triage_ack", "revert": "triage_revert", "apply": "triage_apply",
          "discard": "triage_discard"}.get(args.action)  # fmt: skip
    result = await client.call(op, {}, confirm=True) if op else await client.call("triage_drift", {})

    def text() -> None:
        from oaken_cli.output import print_text

        out = _out(result)
        said = False
        if args.action in ("apply", "discard", "ack", "revert") and not out["changed"] and not out["staged"]:
            print_text(
                {
                    "apply": "applied: config.toml written once and the rules reloaded",
                    "discard": "discarded: the staged edits are gone",
                    "ack": "acknowledged: config.toml's rules are now the ones running",
                    "revert": "reverted: config.toml holds the running rules again (backup kept)",
                }[args.action]
            )
            return
        if out["staged"]:
            said = True
            print_text("staged here, not yet in config.toml (oaken rule apply | discard):")
            for key, names in out["staged"].items():
                print_text(f"  {DRIFT_WORDS.get(key, key)}: {', '.join(str(x) for x in names)}")
        if out["changed"]:
            said = True
            print_text("config.toml differs from the last known good copy, which is running:")
            for line in describe_drift(out):
                print_text(f"  {line}")
            print_text("oaken rule ack  to accept the file    oaken rule revert  to undo the edit")
        if not said:
            print_text("config.toml, the database and the running rules all agree")

    return emit(args, data=result.output, text=text)
