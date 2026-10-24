# SPDX-License-Identifier: Apache-2.0
"""Where the home comes from: ``OAKEN_HOME``, else the last known good one, else the defaults."""

from pathlib import Path

import pytest

from oaken.core import paths


def test_an_explicit_home_wins_and_is_remembered(oaken_home: Path) -> None:
    paths.data_dir()  # the home exists once core has used it
    assert paths.home_source() == ("env", oaken_home.resolve())
    assert paths.remember_home() is True
    assert paths.remember_home() is False  # nothing new to write


def test_without_the_variable_the_last_known_home_is_used(
    oaken_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths.data_dir()  # the home exists once core has used it
    paths.remember_home()
    monkeypatch.delenv(paths.HOME_ENV)
    assert paths.home_source() == ("remembered", oaken_home.resolve())
    assert paths.data_dir() == oaken_home.resolve() / "data"


def test_a_remembered_home_that_is_gone_is_ignored(
    oaken_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths.data_dir()
    paths.remember_home()
    monkeypatch.delenv(paths.HOME_ENV)
    for p in sorted(oaken_home.rglob("*"), reverse=True):
        p.rmdir()
    oaken_home.rmdir()
    assert paths.home_source() == ("default", None)


def test_the_default_is_never_remembered(oaken_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(paths.HOME_ENV)
    assert paths.remember_home() is False
