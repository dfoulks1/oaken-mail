# SPDX-License-Identifier: Apache-2.0
"""Every test runs in its own Oaken home, whatever the shell it was started from.

Oaken finds its home from ``OAKEN_HOME``, and failing that from the last home it ran under, which
is the developer's real one. A test that saves a config, or starts a daemon, without setting its
own home would write into that real home. So no test is left to remember: each one gets a throwaway
home and state directory, and any other ``OAKEN_*`` setting in the developer's shell is dropped so
it cannot change what a test sees. A test that wants a different home just sets it again.
"""

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_oaken_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    base = tmp_path_factory.mktemp("oaken_isolated")
    for name in [n for n in os.environ if n.startswith("OAKEN_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("OAKEN_HOME", str(base / "home"))
    monkeypatch.setenv("OAKEN_STATE_DIR", str(base / "state"))  # the last-known-home pointer
    return base
