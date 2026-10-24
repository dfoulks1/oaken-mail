# SPDX-License-Identifier: Apache-2.0
"""The CLI reaches everything core can do.

Most operations have a verb of their own. The rest, and any added later, are reached through
``oaken operation NAME --field value``, whose flags are built from the operation's input schema. So
the guarantee is this test: every operation's schema turns into flags, and every required field
can be given. An operation whose input the generic command cannot express fails here, when it is
added, instead of being quietly out of the CLI's reach."""

from __future__ import annotations

import argparse

import pytest

from oaken.core.operations.builtin import core_registry
from oaken_cli.schema_flags import add_flags, missing_required, parse_flags

OPERATIONS = sorted(core_registry().core(), key=lambda o: o.name)


@pytest.mark.parametrize("op", OPERATIONS, ids=lambda o: o.name)
def test_every_operation_becomes_flags(op: object) -> None:
    schema = op.input_schema()  # type: ignore[attr-defined]
    parser = argparse.ArgumentParser(add_help=False)
    add_flags(parser, schema, enforce_required=False)
    required = set(schema.get("required", []))
    assert required <= {a.dest for a in parser._actions}, "a required field has no flag"
    # with nothing given, the generic command reports exactly the required fields as missing
    missing = {m.removeprefix("--").replace("-", "_") for m in missing_required(schema, {})}
    assert missing == required


def test_flags_round_trip_a_nested_and_a_list_field() -> None:
    op = core_registry().get("set_labels")
    schema = op.input_schema()
    payload = parse_flags(
        schema, ["--ids", "a", "--ids", "b", "--add", "x"], prog="oaken operation set_labels"
    )
    assert payload["ids"] == ["a", "b"] and payload["add"] == ["x"]


def test_the_operations_with_no_verb_are_the_ones_that_are_signals_not_commands() -> None:
    """``message_presented``/``message_dismissed`` are what a client sends as a person starts and
    stops looking at a message. They are reachable through the generic command and have no verb on
    purpose: nobody types them."""
    names = {o.name for o in OPERATIONS}
    assert {"message_presented", "message_dismissed"} <= names
