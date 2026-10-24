# SPDX-License-Identifier: Apache-2.0
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import structlog

from oaken.core.clock import FakeClock
from oaken.core.db import Database, migrate


@pytest.fixture(autouse=True)
def oaken_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every core test runs with all paths relocated under tmp_path; env is otherwise clean."""
    import os

    for key in [k for k in os.environ if k.startswith("OAKEN_")]:
        monkeypatch.delenv(key)
    home = tmp_path / "home"
    monkeypatch.setenv("OAKEN_HOME", str(home))
    monkeypatch.setenv("OAKEN_STATE_DIR", str(tmp_path / "state"))  # the last-known-home pointer
    return home


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    async with await Database.open(tmp_path / "t.db", read_pool_size=3) as database:
        yield database


MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def raw(tmp_path: Path) -> AsyncIterator[Database]:
    """A raw.db with the core migrations applied."""
    async with await Database.open(tmp_path / "raw.db", read_pool_size=2) as database:
        await migrate(database, MIGRATIONS)
        yield database


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    """A test that configures logging onto a captured stream must not leave it behind."""
    yield
    structlog.reset_defaults()
