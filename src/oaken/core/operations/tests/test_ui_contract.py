# SPDX-License-Identifier: Apache-2.0
"""The web UI's contract with core: every operation its pages call exists and is reachable by a
thin client, and every output field its render code reads is in that operation's schema.

The list lives in ``apps/webui/ui-contract.json``, beside the UI, so adding a UI feature means
adding it there; a capability with no backing operation fails here, not in a browser.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from oaken.api.app import operation_info
from oaken.core.operations.builtin import core_operations

CONTRACT = json.loads(
    (Path(__file__).parents[5] / "apps" / "webui" / "ui-contract.json").read_text("utf-8")
)
OPS = {op.name: op for op in core_operations()}
REQUIRED = ("name", "summary", "permission", "marks_read", "streaming", "mcp")


def _resolve(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in schema:
        schema = root["$defs"][schema["$ref"].rsplit("/", 1)[-1]]
    for key in ("anyOf", "oneOf"):  # an optional field: look at the non-null branch
        branches = [b for b in schema.get(key, []) if b.get("type") != "null"]
        if len(branches) == 1:
            return _resolve(branches[0], root)
    return schema


def has_field(root: dict[str, Any], path: str) -> bool:
    """Does the output schema have ``a.b`` / ``items[].x``? ``[]`` steps into a list's items."""
    node = _resolve(root, root)
    for part in path.split("."):
        each = part.endswith("[]")
        name = part[:-2] if each else part
        props = node.get("properties", {})
        if name not in props:
            return False
        node = _resolve(props[name], root)
        if each:
            node = _resolve(node.get("items", {}), root)
    return True


def test_every_ui_capability_is_a_registered_operation_a_client_can_reach() -> None:
    for name in CONTRACT["capabilities"]:
        assert name in OPS, f"the UI calls {name!r} but core has no such operation"
        op = OPS[name]
        # reachable by the general (non-MCP) surface: nothing the UI needs is MCP-only
        assert op.mcp in ("never", "off", "on"), name


def test_the_ui_never_depends_on_a_swarm_agent_operation() -> None:
    from oaken.core.operations.builtin.jobs import AGENT_OPERATIONS

    agent_only = {op.name for op in AGENT_OPERATIONS}
    assert not agent_only & set(CONTRACT["capabilities"])


def test_the_field_table_only_names_declared_capabilities() -> None:
    assert set(CONTRACT["fields"]) <= set(CONTRACT["capabilities"])


@pytest.mark.parametrize("name", sorted(CONTRACT["fields"]))
def test_every_field_the_ui_reads_is_in_the_output_schema(name: str) -> None:
    schema = OPS[name].output_schema()
    missing = [f for f in CONTRACT["fields"][name] if not has_field(schema, f)]
    assert not missing, f"{name}: the UI reads {missing}, which its output schema lacks"


def test_a_field_that_is_not_there_is_caught() -> None:
    schema = OPS["list_messages"].output_schema()
    assert has_field(schema, "items[].starred") and not has_field(schema, "items[].nonesuch")


def test_the_operation_listing_carries_what_a_client_builds_itself_from() -> None:
    for name, op in OPS.items():
        info = operation_info(op).model_dump()
        assert all(k in info for k in REQUIRED), name
        assert isinstance(info["input_schema"], dict) and info["input_schema"], name
        assert isinstance(info["output_schema"], dict) and info["output_schema"], name
