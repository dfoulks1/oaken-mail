# SPDX-License-Identifier: Apache-2.0
"""Everything lives under one ``OAKEN_HOME``, so a daemon in a test touches nothing real."""

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "oaken-home"
    monkeypatch.setenv("OAKEN_HOME", str(root))
    for name in list(os.environ):
        if name.startswith("OAKEN_") and name != "OAKEN_HOME":
            monkeypatch.delenv(name)
    yield root


def child_env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("OAKEN_")}
    env["OAKEN_HOME"] = str(home)
    env["OAKEN_API__PORT"] = "0"
    env["OAKEN_AI__EMBEDDER"] = ""
    env["PYTHONUNBUFFERED"] = "1"
    return env


PYTHON = sys.executable
