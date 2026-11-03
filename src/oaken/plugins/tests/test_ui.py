# SPDX-License-Identifier: Apache-2.0
"""The plugin UI contract: a malformed spec is named, positions downshift (ADR 0018)."""

import pytest

from oaken.core.errors import InvalidInput
from oaken.plugins.contracts import UiField, UiPage, UiSpec
from oaken.plugins.ui import check_ui, reposition

IDS = [str(i) for i in range(1, 11)]


def test_moving_10_to_3_shifts_3_to_9_down() -> None:
    assert reposition(IDS, {"10": 3}) == ["1", "2", "10", "3", "4", "5", "6", "7", "8", "9"]


def test_two_rows_asking_for_one_place_the_earlier_keeps_it() -> None:
    assert reposition(IDS, {"3": 1, "4": 1})[:4] == ["3", "4", "1", "2"]


def test_nothing_typed_or_unchanged_moves_nothing() -> None:
    assert reposition(IDS, {"4": "4", "5": ""}) == IDS


@pytest.mark.parametrize("bad", ["0", "11", "x", "2.5", "-1"])
def test_a_bad_position_refuses_the_whole_update(bad: str) -> None:
    with pytest.raises(InvalidInput):
        reposition(IDS, {"2": bad})


def test_a_malformed_spec_is_reported_not_raised() -> None:
    assert check_ui(None) == [] and check_ui("x")
    page = UiPage("new", "New", "load", fields=(UiField("a", kind="enum"),))
    assert any("no choices" in p for p in check_ui(UiSpec(pages=(page,))))
    assert check_ui(UiSpec(pages=(UiPage("Bad Name", "T", "load"),)))
    assert check_ui(UiSpec(pages=(UiPage("new", "New", "load", submit="add"),))) == []
