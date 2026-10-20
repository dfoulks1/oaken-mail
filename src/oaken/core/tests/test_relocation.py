# SPDX-License-Identifier: Apache-2.0
"""OAKEN_HOME relocates everything: nothing is written outside it."""

import os
from pathlib import Path

import pytest

from oaken.core import paths
from oaken.core.db import Database, migrate
from oaken.core.logging import configure_logging, get_logger
from oaken.core.settings import Settings


def snapshot(root: Path) -> set[Path]:
    return {p for p in root.rglob("*")}


async def test_nothing_is_written_outside_oaken_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = tmp_path / "sandbox"
    home = sandbox / "oaken-home"
    elsewhere = sandbox / "fake-home"
    elsewhere.mkdir(parents=True)
    # Point every platform default somewhere we can watch; OAKEN_HOME must make them unused.
    for var in ("HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "XDG_CACHE_HOME"):
        monkeypatch.setenv(var, str(elsewhere))
    monkeypatch.setenv("OAKEN_HOME", str(home))
    before = snapshot(elsewhere)

    settings = Settings.load()
    configure_logging(settings.log)
    get_logger().info("started")
    migrations = tmp_path / "migrations"  # outside the sandbox on purpose: it is an input
    migrations.mkdir()
    (migrations / "0001_a.sql").write_text("CREATE TABLE a (x);", encoding="utf-8")
    async with await Database.open(paths.data_dir() / settings.storage.raw_db) as db:
        await migrate(db, migrations)
        await db.execute("INSERT INTO a (x) VALUES (1)")
    paths.runtime_file().write_text("{}", encoding="utf-8")

    assert snapshot(elsewhere) == before
    assert snapshot(home), "expected files under OAKEN_HOME"
    for p in snapshot(home):
        assert home.resolve() in p.resolve().parents
    assert os.environ["OAKEN_HOME"] == str(home)
