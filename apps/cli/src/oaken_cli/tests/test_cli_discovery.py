# SPDX-License-Identifier: Apache-2.0
"""Finding core: the runtime file first, then the default port."""

import json
import os
from pathlib import Path

import pytest

from oaken.core.settings import DEFAULT_PORT, APISettings
from oaken_cli import discovery
from oaken_cli.exits import CoreUnavailable


def test_the_default_port_is_fixed_and_zero_still_asks_for_a_free_one() -> None:
    assert APISettings().port == DEFAULT_PORT == 62536
    assert APISettings(port=0).port == 0


def test_no_runtime_file_and_nothing_on_the_default_port_is_core_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discovery, "_answers_on_default_port", lambda: False)
    with pytest.raises(CoreUnavailable):
        discovery.discover(runtime_path=tmp_path / "none.json", token="t")


def test_no_runtime_file_falls_back_to_the_default_port_when_core_answers_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discovery, "_answers_on_default_port", lambda: True)
    found = discovery.discover(runtime_path=tmp_path / "none.json", token="t")
    assert found.base_url == f"http://127.0.0.1:{DEFAULT_PORT}" and found.token == "t"


def test_a_stale_runtime_file_also_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"pid": 2**22 + 12345, "host": "127.0.0.1", "port": 40000}))
    monkeypatch.setattr(discovery, "_answers_on_default_port", lambda: True)
    assert discovery.discover(runtime_path=runtime, token="t").base_url.endswith(str(DEFAULT_PORT))


def test_a_good_runtime_file_wins_and_the_default_is_not_tried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"pid": os.getpid(), "host": "127.0.0.1", "port": 40001}))

    def boom() -> bool:
        raise AssertionError("the default port must not be probed when the file is good")

    monkeypatch.setattr(discovery, "_answers_on_default_port", boom)
    assert discovery.discover(runtime_path=runtime, token="t").base_url == "http://127.0.0.1:40001"
