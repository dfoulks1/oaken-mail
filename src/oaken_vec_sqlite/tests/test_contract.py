# SPDX-License-Identifier: Apache-2.0
from pathlib import Path

import pytest

from oaken.testing.contracts import VectorStoreContract, verify_vector_store
from oaken_vec_sqlite import SqliteVecStore


class TestSqliteVecContract(VectorStoreContract):
    @pytest.fixture
    def store(self, tmp_path: Path) -> SqliteVecStore:
        return SqliteVecStore(tmp_path / "index.db")


async def test_the_one_call_verifier_passes(tmp_path: Path) -> None:
    counter = iter(range(100))
    await verify_vector_store(lambda: SqliteVecStore(tmp_path / f"v{next(counter)}.db"))
