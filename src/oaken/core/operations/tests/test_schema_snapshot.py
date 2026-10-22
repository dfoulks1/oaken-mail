# SPDX-License-Identifier: Apache-2.0
"""Snapshot of the core operation surface (ADR 0010).

Every core operation's public shape — its name, permission class, MCP ceiling, ``marks_read`` and
``streaming`` flags, and its input/output JSON Schemas — is pinned against
``operation_schemas.json`` beside this file. A renamed, removed or retyped field fails this test
with a readable diff, so a change to the surface is a deliberate, reviewed act, not silent drift.

Plugin operations are **not** here: they belong to whatever plugins are installed, and each plugin
owns its own contract. This snapshot is the *core* registry only (``OperationRegistry.core``).

To update the baseline intentionally (after a reviewed surface change)::

    uv run python -m oaken.core.operations.tests.test_schema_snapshot --update

The diff of ``operation_schemas.json`` in the pull request is then the record of the change; if it
removes or renames a field, ADR 0010 requires a ``PLUGIN_API_VERSION`` bump and a changelog note.
"""

import json
from pathlib import Path
from typing import Any

from oaken.core.operations.builtin import core_registry

SNAPSHOT = Path(__file__).with_name("operation_schemas.json")


def _surface() -> dict[str, Any]:
    """The pinned public shape of every core operation, name-sorted and JSON-ready."""
    out: dict[str, Any] = {}
    for op in core_registry().core():
        out[op.name] = {
            "permission": op.permission,
            "mcp": op.mcp,
            "marks_read": op.marks_read,
            "streaming": op.streaming,
            "input_schema": op.input_schema(),
            "output_schema": op.output_schema(),
        }
    return out


def _dump(surface: dict[str, Any]) -> str:
    return json.dumps(surface, indent=2, sort_keys=True) + "\n"


def test_core_operation_schemas_match_the_snapshot() -> None:
    assert SNAPSHOT.exists(), (
        f"{SNAPSHOT.name} is missing; generate it with "
        "`uv run python -m oaken.core.operations.tests.test_schema_snapshot --update`"
    )
    expected = json.loads(SNAPSHOT.read_text())
    actual = _surface()
    assert actual == expected, (
        "the core operation surface changed (ADR 0010). If this is intentional, regenerate the "
        "snapshot with `uv run python -m oaken.core.operations.tests.test_schema_snapshot "
        "--update` and review the diff; a removed or renamed field needs a PLUGIN_API_VERSION bump."
    )


def _update() -> None:
    SNAPSHOT.write_text(_dump(_surface()))
    print(f"wrote {SNAPSHOT}")


if __name__ == "__main__":
    import sys

    if "--update" in sys.argv:
        _update()
    else:
        print("pass --update to regenerate the snapshot")
