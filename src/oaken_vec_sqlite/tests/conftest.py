# SPDX-License-Identifier: Apache-2.0
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from oaken.testing.contracts import make_context
from oaken_vec_sqlite import SqliteVecStore


@pytest.fixture
async def started(tmp_path: Path) -> AsyncIterator[SqliteVecStore]:
    store = SqliteVecStore(tmp_path / "index.db")
    await store.start(make_context("vector_store"))
    try:
        yield store
    finally:
        await store.stop()
