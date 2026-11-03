# SPDX-License-Identifier: Apache-2.0
import pytest

from oaken.core.attrs import Attr, filepath, scoped_labels
from oaken.core.selector import has_label, selected

LABELS = {"sieve": ("vip",), "triage": ("invoice",), "user": ("a:b",)}


def test_a_label_is_scoped_or_bare() -> None:
    assert has_label(LABELS, "sieve:vip") and has_label(LABELS, "VIP")  # bare: any scope
    assert not has_label(LABELS, "triage:vip")  # the wrong scope
    assert has_label(LABELS, "a:b")  # a colon in a bare label is not a scope


def test_when_is_any_of_and_unless_always_wins() -> None:
    assert selected({}, LABELS)
    assert selected({"when": ["sieve:vip", "x"]}, LABELS)
    assert not selected({"when": ["triage:vip"]}, LABELS)
    assert not selected({"when": ["sieve:vip"], "unless": ["invoice"]}, LABELS)
    with pytest.raises(ValueError):
        selected({"when": "sieve:vip"}, LABELS)


def test_scopes_and_the_one_filepath() -> None:
    attrs = [
        Attr("label", "vip", "sieve"),
        Attr("label", "invoice", "triage"),
        Attr("location", "Work", "sieve"),
        Attr("location", "Mine", "user"),
    ]
    assert scoped_labels(attrs) == {"sieve": ("vip",), "triage": ("invoice",)}
    assert filepath(attrs) == "Mine"  # the user beats Sieve
    assert filepath(attrs[:3]) == "Work" and filepath([]) is None
