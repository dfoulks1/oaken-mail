# SPDX-License-Identifier: Apache-2.0
"""Migration 0019 (ADR 0017): a triage verdict stops being an enrichment named ``triage``. Every old
row moves to ``triage_verdicts`` with its data unchanged, other enrichments stay, and a migration
that fails leaves the old rows where they were."""

import json
import shutil
from pathlib import Path

import pytest

from oaken.core.db import Database, migrate
from oaken.core.errors import StorageError
from oaken.core.tests.conftest import MIGRATIONS

OLD = [  # (message, source, expected triager)
    ("m1", "llm", "ai"),
    ("m2", "custom", "rules"),
    ("m3", "rules", "spamcheck"),
]


def upto_18(tmp_path: Path) -> Path:
    """A copy of the migrations without 0019 and anything after it."""
    out = tmp_path / "migrations"
    out.mkdir(parents=True)
    for f in MIGRATIONS.glob("*.sql"):
        if int(f.name[:4]) < 19:
            shutil.copy(f, out / f.name)
    return out


async def seed(db: Database) -> None:
    for mid, source, _ in OLD:
        await db.execute(
            "INSERT INTO enrichments (message_id, enricher, version, model_id, content_hash,"
            " data_json, created_at) VALUES (?, 'triage', '1', 'qwen', ?, ?, 't')",
            (mid, f"h-{mid}", json.dumps({"priority": 2, "source": source})),
        )
    await db.execute(
        "INSERT INTO enrichments (message_id, enricher, version, model_id, content_hash,"
        " data_json, created_at) VALUES ('m1', 'summary', '1', 'qwen', 'h-m1', '{}', 't')"
    )


async def test_old_verdicts_move_and_nothing_else_is_touched(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "raw.db", read_pool_size=1) as db:
        await migrate(db, upto_18(tmp_path))
        await seed(db)
        assert 19 in await migrate(db, MIGRATIONS)
        rows = await db.fetchall(
            "SELECT message_id, triager, version, model_id, content_hash, data_json"
            " FROM triage_verdicts ORDER BY message_id"
        )
        assert [(r["message_id"], r["triager"]) for r in rows] == [(m, t) for m, _, t in OLD]
        assert all(r["version"] == "1" and r["model_id"] == "qwen" for r in rows)
        assert [json.loads(r["data_json"])["source"] for r in rows] == [s for _, s, _ in OLD]
        left = await db.fetchall("SELECT enricher FROM enrichments")
        assert [r["enricher"] for r in left] == ["summary"]  # the verdicts left; the summary stayed


async def test_a_failed_migration_leaves_the_old_rows(tmp_path: Path) -> None:
    mig = upto_18(tmp_path / "failing")
    shutil.copy(MIGRATIONS / "0019_triage_verdicts.sql", mig / "0019_triage_verdicts.sql")
    async with await Database.open(tmp_path / "raw.db", read_pool_size=1) as db:
        await migrate(db, upto_18(tmp_path / "plain"))
        await seed(db)
        with (mig / "0019_triage_verdicts.sql").open("a") as f:
            f.write("\nSELECT * FROM no_such_table;\n")  # fails after the copy and the delete
        with pytest.raises(StorageError):
            await migrate(db, mig)
        kept = await db.fetchall("SELECT enricher FROM enrichments WHERE enricher = 'triage'")
        assert len(kept) == len(OLD)  # rolled back: the old rows are still there
        assert await db.fetchone("SELECT 1 FROM sqlite_master WHERE name = 'triage_verdicts'") is None
