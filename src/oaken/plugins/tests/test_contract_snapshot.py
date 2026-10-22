# SPDX-License-Identifier: Apache-2.0
"""Snapshot of the plugin contract surface (ADR 0010).

Pins the things an out-of-tree plugin depends on: ``PLUGIN_API_VERSION``, the kind and capability
tables, the field names of every frozen dataclass that crosses the boundary, and the public members
of every contract ``Protocol`` and base class. A renamed field, a removed method or a bumped API
version fails this test, so a break to a published contract is deliberate and reviewed (and, per
ADR 0010, a breaking change bumps ``PLUGIN_API_VERSION``).

To update the baseline intentionally::

    uv run python -m oaken.plugins.tests.test_contract_snapshot --update
"""

import contextlib
import dataclasses
import json
from pathlib import Path
from typing import Any, get_type_hints

from oaken.plugins import base, contracts

SNAPSHOT = Path(__file__).with_name("contract_snapshot.json")

# The frozen dataclasses that cross the core/plugin boundary.
_DATACLASSES = [
    "PluginInfo",
    "PluginContext",
    "PluginOpContext",
    "PluginChange",
    "PluginPreview",
    "PluginOperation",
    "PluginJobContext",
    "PluginJobHandler",
    "Health",
    "RawMessage",
    "FlagChange",
    "PullBatch",
    "LLMRequest",
    "LLMResponse",
    "EnrichAttachment",
    "EnrichInput",
    "EnrichOutput",
    "VectorItem",
    "Hit",
    "VectorStats",
]

# The contract Protocols (lifecycle + the five kinds) and LLMAccess.
_PROTOCOLS = [
    "Plugin",
    "Provider",
    "Embedder",
    "LLMBackend",
    "Enricher",
    "VectorStore",
    "LLMAccess",
]

# The optional base classes a plugin author subclasses.
_BASES = [
    "BaseProvider",
    "BaseEmbedder",
    "BaseLLMBackend",
    "BaseEnricher",
    "BaseVectorStore",
]


def _public_members(obj: type) -> list[str]:
    """Public attribute and method names declared on a class (not inherited from object)."""
    names: set[str] = set()
    for klass in obj.__mro__:
        if klass is object:
            continue
        names.update(k for k in vars(klass) if not k.startswith("_"))
    # Annotated-but-unassigned members (Protocol attributes like ``model_id``) show in hints.
    with contextlib.suppress(Exception):  # forward refs we cannot resolve: use vars() alone
        names.update(k for k in get_type_hints(obj) if not k.startswith("_"))
    return sorted(names)


def _surface() -> dict[str, Any]:
    out: dict[str, Any] = {
        "PLUGIN_API_VERSION": contracts.PLUGIN_API_VERSION,
        "PLUGIN_KINDS": list(contracts.PLUGIN_KINDS),
        "ENTRY_POINT_GROUPS": dict(sorted(contracts.ENTRY_POINT_GROUPS.items())),
        "CAPABILITIES": {
            k: dict(sorted(v.items())) for k, v in sorted(contracts.CAPABILITIES.items())
        },
        "REQUIRED_MEMBERS": {k: sorted(v) for k, v in sorted(contracts.REQUIRED_MEMBERS.items())},
        "dataclasses": {},
        "protocols": {},
        "bases": {},
    }
    for name in _DATACLASSES:
        cls = getattr(contracts, name)
        out["dataclasses"][name] = [f.name for f in dataclasses.fields(cls)]
    for name in _PROTOCOLS:
        out["protocols"][name] = _public_members(getattr(contracts, name))
    for name in _BASES:
        out["bases"][name] = _public_members(getattr(base, name))
    return out


def _dump(surface: dict[str, Any]) -> str:
    return json.dumps(surface, indent=2, sort_keys=True) + "\n"


def test_plugin_contract_surface_matches_the_snapshot() -> None:
    assert SNAPSHOT.exists(), (
        f"{SNAPSHOT.name} is missing; generate it with "
        "`uv run python -m oaken.plugins.tests.test_contract_snapshot --update`"
    )
    expected = json.loads(SNAPSHOT.read_text())
    actual = _surface()
    assert actual == expected, (
        "the plugin contract surface changed (ADR 0010). If intentional, regenerate with "
        "`uv run python -m oaken.plugins.tests.test_contract_snapshot --update` and review the "
        "diff; a removed or renamed member needs a PLUGIN_API_VERSION bump."
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
