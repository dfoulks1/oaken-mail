# SPDX-License-Identifier: Apache-2.0
"""Flags built from an operation's input schema, and the shorthand that uses them."""

from __future__ import annotations

import json
from typing import Any

import pytest

from oaken.core.operations.builtin import core_registry
from oaken_cli.exits import ExitCode
from oaken_cli.schema_flags import parse_flags
from oaken_cli.tests.conftest import Cli

SCHEMA: dict[str, Any] = {
    "properties": {
        "account_id": {"type": "string"},
        "limit": {"type": "integer", "default": 10},
        "ratio": {"type": "number"},
        "dry_run": {"type": "boolean"},
        "ids": {"type": "array", "items": {"type": "string"}},
        "mode": {"enum": ["a", "b"], "type": "string"},
        "since": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "extra": {"type": "object"},
        "inner": {"$ref": "#/$defs/Inner"},
    },
    "required": ["account_id"],
    "$defs": {"Inner": {"type": "object", "properties": {"x": {"type": "integer"}}}},
}


def test_flags_map_to_typed_fields_and_only_given_ones_are_sent() -> None:
    got = parse_flags(
        SCHEMA,
        ["--account-id", "w", "--limit", "5", "--dry-run", "--ids", "a", "--ids", "b",
         "--mode", "b", "--since", "x", "--extra", '{"k": 1}', "--inner", '{"x": 2}'],
        prog="t",
    )  # fmt: skip
    assert got == {
        "account_id": "w", "limit": 5, "dry_run": True, "ids": ["a", "b"], "mode": "b",
        "since": "x", "extra": {"k": 1}, "inner": {"x": 2},
    }  # fmt: skip
    assert parse_flags(SCHEMA, ["--account-id", "w", "--no-dry-run"], prog="t") == {
        "account_id": "w",
        "dry_run": False,
    }
    assert parse_flags(SCHEMA, ["--account-id", "w"], prog="t") == {"account_id": "w"}


@pytest.mark.parametrize(
    "argv",
    [[], ["--account-id", "w", "--limit", "many"], ["--account-id", "w", "--mode", "z"],
     ["--account-id", "w", "--extra", "{bad"], ["--account-id", "w", "--nope", "1"]],
)  # fmt: skip
def test_bad_flags_exit_with_a_usage_error(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        parse_flags(SCHEMA, argv, prog="t")
    assert exc.value.code == int(ExitCode.USAGE)


def test_every_core_operation_input_schema_builds_a_parser() -> None:
    """The architectural rule: any operation is reachable from the CLI, so the generator must be
    able to express every input we ship."""
    for op in core_registry().all():
        schema = op.input.model_json_schema()
        parse_flags(schema, [], prog=op.name) if not schema.get("required") else None
        import argparse

        from oaken_cli.schema_flags import add_flags

        add_flags(argparse.ArgumentParser(), schema)


async def test_an_operation_takes_its_fields_as_flags(cli: Cli) -> None:
    await cli.ops.add(3)
    run = await cli("operation", "list_messages", "--limit", "2", "--json")
    assert run.code == int(ExitCode.OK), run.err
    assert len(json.loads(run.out)["output"]["items"]) == 2


async def test_a_flag_the_operation_lacks_is_a_usage_error(cli: Cli) -> None:
    with pytest.raises(SystemExit) as exc:
        await cli("operation", "status", "--nope", "1")
    assert exc.value.code == int(ExitCode.USAGE)


def test_plugin_verb_shorthand_means_operation_plugin_dot_verb() -> None:
    from oaken_cli.main import _parse, build_parser

    parser = build_parser()
    args = _parse(parser, ["--json", "gmail", "login", "--client-secret-file", "f.json"])
    assert (args.command, args.name, args.flags) == (
        "operation",
        "gmail.login",
        ["--client-secret-file", "f.json"],
    )
    again = _parse(parser, ["--token-file", "t", "operations"])  # a real command is left alone
    assert again.command == "operations"
    with pytest.raises(SystemExit):  # an unknown flag on a normal verb is still an error
        _parse(parser, ["status", "--nope"])
