# SPDX-License-Identifier: Apache-2.0
"""Pick messages by label, for an enricher or an action: ``when`` and ``unless`` in its table.

A label is ``scope:label`` (scope is ``sieve``, ``triage`` or ``user``) or a bare ``label``, which
matches in any scope. ``when`` is any-of (empty: everything); ``unless`` removes a message that has
any of its labels.
"""

from collections.abc import Collection, Mapping
from typing import Any

from oaken.core.attrs import SCOPES


def has_label(labels: Mapping[str, Collection[str]], spec: str) -> bool:
    scope, sep, name = spec.partition(":")
    if sep and scope.casefold() in SCOPES:
        return name.casefold() in {x.casefold() for x in labels.get(scope.casefold(), ())}
    want = spec.casefold()  # bare, or a label that happens to contain a colon
    return any(want in {x.casefold() for x in v} for v in labels.values())


def _specs(table: Mapping[str, Any], key: str) -> list[str]:
    value = table.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
        raise ValueError(f"{key} must be a list of labels, such as [\"sieve:vip\"]")
    return value


def selected(table: Mapping[str, Any], labels: Mapping[str, Collection[str]]) -> bool:
    when, unless = _specs(table, "when"), _specs(table, "unless")
    return (not when or any(has_label(labels, s) for s in when)) and not any(
        has_label(labels, s) for s in unless
    )
