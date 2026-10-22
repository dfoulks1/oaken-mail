# SPDX-License-Identifier: Apache-2.0
"""Snapshot of the event surface (ADR 0010).

Every ``Event`` subclass can reach a client stream (the SSE endpoint, an MCP client, an in-process
consumer), so its field names are part of the public surface. This pins them against
``event_snapshot.json`` beside this file; a renamed or removed field on a streamed event fails here.

``PluginEvent`` subclasses defined by plugins are each that plugin's own contract and are not pinned
here — only the ``Event`` types core defines.

To update the baseline intentionally::

    uv run python -m oaken.core.tests.test_event_snapshot --update
"""

import dataclasses
import json
from pathlib import Path
from typing import Any

from oaken.core import events

SNAPSHOT = Path(__file__).with_name("event_snapshot.json")


def _event_types() -> list[type]:
    """Every dataclass in ``oaken.core.events`` that subclasses ``Event`` (``Event`` included)."""
    found = []
    for obj in vars(events).values():
        if (
            isinstance(obj, type)
            and dataclasses.is_dataclass(obj)
            and issubclass(obj, events.Event)
        ):
            found.append(obj)
    return sorted(found, key=lambda c: c.__name__)


def _surface() -> dict[str, Any]:
    return {cls.__name__: [f.name for f in dataclasses.fields(cls)] for cls in _event_types()}


def _dump(surface: dict[str, Any]) -> str:
    return json.dumps(surface, indent=2, sort_keys=True) + "\n"


def test_event_shapes_match_the_snapshot() -> None:
    assert SNAPSHOT.exists(), (
        f"{SNAPSHOT.name} is missing; generate it with "
        "`uv run python -m oaken.core.tests.test_event_snapshot --update`"
    )
    expected = json.loads(SNAPSHOT.read_text())
    actual = _surface()
    assert actual == expected, (
        "a core event's shape changed (ADR 0010). If intentional, regenerate with "
        "`uv run python -m oaken.core.tests.test_event_snapshot --update` and review the diff; "
        "a removed or renamed field is a breaking change (PLUGIN_API_VERSION bump)."
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
