# SPDX-License-Identifier: Apache-2.0
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def oaken_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test runs with all paths relocated under tmp_path."""
    import os

    for key in [k for k in os.environ if k.startswith("OAKEN_")]:
        monkeypatch.delenv(key)
    home = tmp_path / "home"
    monkeypatch.setenv("OAKEN_HOME", str(home))
    monkeypatch.setenv("OAKEN_STATE_DIR", str(tmp_path / "state"))
    return home
