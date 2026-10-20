# SPDX-License-Identifier: Apache-2.0
import asyncio
import re
import subprocess
import sys
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from oaken.core.db import Database, migrate
from oaken.core.errors import Conflict, PluginError
from oaken.index.store import MIGRATIONS
from oaken.plugins.contracts import VectorItem
from oaken.testing.contracts import make_context
from oaken_vec_sqlite import SqliteVecStore

X, Y, Z = (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)


def item(
    mid: str,
    vec: tuple[float, ...],
    *,
    chunk: str = "c0",
    model: str = "m-a",
    acct: str = "a1",
    h: str = "",
) -> VectorItem:
    return VectorItem(mid, chunk, vec, model, acct, h)


# -- basics --------------------------------------------------------------------------------
async def test_an_empty_store_answers_with_nothing(started: SqliteVecStore) -> None:
    assert await started.query(X, 5) == []
    s = await started.stats()
    assert (s.count, s.model_id, s.dimensions) == (0, None, None)
    await started.delete(["nothing-here"])
    assert (await started.health()).ok


async def test_k_larger_than_the_store_returns_everything(started: SqliteVecStore) -> None:
    await started.upsert([item("x", X), item("y", Y)])
    hits = await started.query(X, 50)
    assert [h.message_id for h in hits] == ["x", "y"]
    assert hits[0].score == pytest.approx(1.0)
    assert hits[1].score == pytest.approx(0.0)
    assert len(await started.query(X, 10_000)) == 2  # k past the extension's limit is clamped


async def test_vectors_survive_reopening_the_file(tmp_path: Path) -> None:
    path = tmp_path / "index.db"
    first = SqliteVecStore(path)
    await first.start(make_context("vector_store"))
    await first.upsert([item("x", X), item("y", Y)])
    await first.stop()
    second = SqliteVecStore(path)
    await second.start(make_context("vector_store"))
    try:
        assert (await second.stats()).count == 2
        assert [h.message_id for h in await second.query(Y, 1)] == ["y"]
    finally:
        await second.stop()


async def test_an_unstarted_store_says_so() -> None:
    with pytest.raises(PluginError, match="not started"):
        await SqliteVecStore().stats()


# -- duplicates and replacement ------------------------------------------------------------
async def test_the_same_vector_twice_is_one_vector(started: SqliteVecStore) -> None:
    await started.upsert([item("x", X)])
    await started.upsert([item("x", X)])
    await started.upsert([item("x", X), item("x", X)])
    assert (await started.stats()).count == 1


async def test_a_chunk_with_the_same_hash_and_model_is_not_written_again(
    started: SqliteVecStore,
) -> None:
    await started.upsert([item("x", X, h="h1")])
    assert (started.last_upsert.written, started.last_upsert.skipped) == (1, 0)
    await started.upsert([item("x", X, h="h1")])
    assert (started.last_upsert.written, started.last_upsert.skipped) == (0, 1)
    await started.upsert([item("x", Y, h="h2")])  # the text changed: replaced
    assert (started.last_upsert.written, started.last_upsert.skipped) == (1, 0)
    assert (await started.stats()).count == 1
    assert [h.message_id for h in await started.query(Y, 1)] == ["x"]
    assert await started.stored_hashes("m-a", "x") == {"c0": "h2"}


async def test_an_empty_hash_is_never_a_reason_to_skip(started: SqliteVecStore) -> None:
    await started.upsert([item("x", X)])
    await started.upsert([item("x", Y)])
    assert started.last_upsert.written == 1
    assert [h.message_id for h in await started.query(Y, 1)] == ["x"]


# -- models and dimensions -----------------------------------------------------------------
async def test_a_second_model_is_refused_with_a_clear_error(started: SqliteVecStore) -> None:
    await started.upsert([item("x", X)])
    with pytest.raises(Conflict, match="begin_model"):
        await started.upsert([item("y", X, model="m-b")])
    with pytest.raises(Conflict, match="dimensions"):
        await started.upsert([item("y", (1.0, 0.0))])
    assert (await started.stats()).count == 1


async def test_a_bad_batch_changes_nothing_even_for_a_new_store(started: SqliteVecStore) -> None:
    with pytest.raises(Conflict):
        await started.upsert([item("x", X), item("y", (1.0, 0.0))])
    assert (await started.stats()).count == 0
    assert await started.models() == []  # the model registered by the first item was rolled back
    await started.upsert([item("x", (1.0, 0.0))])  # so a different size is accepted afterwards
    assert (await started.stats()).dimensions == 2


@pytest.mark.parametrize(
    "bad", [(0.0, 0.0, 0.0), (float("nan"), 1.0, 0.0), (float("inf"), 0.0, 0.0), ()]
)
async def test_vectors_with_no_usable_direction_are_refused(
    started: SqliteVecStore, bad: tuple[float, ...]
) -> None:
    with pytest.raises(PluginError):
        await started.upsert([item("x", bad)])
    with pytest.raises(PluginError):
        await started.query(bad, 1)
    assert (await started.stats()).count == 0


async def test_a_new_model_is_built_beside_the_old_one_and_swapped_in(
    started: SqliteVecStore,
) -> None:
    await started.upsert([item("x", X), item("y", Y)])
    await started.begin_model("m-b", 2)
    await started.upsert([item("x", (1.0, 0.0), model="m-b")])
    # nothing about the active model changed while the new one is being built
    assert (await started.stats()).model_id == "m-a"
    assert [h.message_id for h in await started.query(Y, 1)] == ["y"]
    assert [h.message_id for h in await started.query((1.0, 0.0), 5, {"model_id": "m-b"})] == ["x"]
    infos = {m.model_id: (m.state, m.vectors) for m in await started.models()}
    assert infos == {"m-a": ("active", 2), "m-b": ("building", 1)}

    with pytest.raises(Conflict, match="active"):
        await started.drop_model("m-a")
    await started.activate_model("m-b")
    s = await started.stats()
    assert (s.model_id, s.dimensions, s.count) == ("m-b", 2, 1)
    assert await started.drop_model("m-a") == 2
    assert [m.model_id for m in await started.models()] == ["m-b"]
    assert await started.drop_model("m-a") == 0


async def test_unknown_models_and_dimension_clashes_are_conflicts(started: SqliteVecStore) -> None:
    await started.upsert([item("x", X)])
    with pytest.raises(Conflict):
        await started.activate_model("never-seen")
    with pytest.raises(Conflict):
        await started.query(X, 1, {"model_id": "never-seen"})
    with pytest.raises(Conflict):
        await started.begin_model("m-a", 9)
    with pytest.raises(PluginError):
        await started.begin_model("m-c", 0)


async def test_delete_removes_every_models_vectors(started: SqliteVecStore) -> None:
    await started.upsert([item("x", X), item("y", Y)])
    await started.begin_model("m-b", 3)
    await started.upsert([item("x", Z, model="m-b")])
    await started.delete(["x"])
    assert {m.model_id: m.vectors for m in await started.models()} == {"m-a": 1, "m-b": 0}
    assert [h.message_id for h in await started.query(X, 5)] == ["y"]


# -- filters -------------------------------------------------------------------------------
async def test_an_unknown_filter_is_refused_not_ignored(started: SqliteVecStore) -> None:
    await started.upsert([item("x", X)])
    with pytest.raises(PluginError, match="unknown filter"):
        await started.query(X, 1, {"colour": "red"})


async def test_index_filters_need_the_index_tables(started: SqliteVecStore) -> None:
    await started.upsert([item("x", X)])
    with pytest.raises(PluginError, match="index tables"):
        await started.query(X, 1, {"sender": "alex"})


DOC_COLUMNS = (
    "message_id, account_id, thread_key, subject_key, subject, from_addr, from_name, to_json,"
    " date_utc, has_attachments, attachments_json, cleaned_text, content_hash,"
    " parse_warnings_json, disposition, indexed_at"
)


async def add_doc(
    db: Database, mid: str, *, thread: str, date: str, addr: str, name: str, acct: str = "a1"
) -> int:
    async with db.transaction() as tx:
        await tx.execute(
            f"INSERT INTO docs ({DOC_COLUMNS}) VALUES (?, ?, ?, 's', 'subject', ?, ?, '[]', ?, 0,"  # noqa: S608
            " '[]', 'text', 'h', '[]', 'inbox', '2024-01-01T00:00:00Z')",
            (mid, acct, thread, addr, name, date),
        )
        await tx.execute(
            "INSERT INTO chunks (message_id, ordinal, source, subject, sender, text,"
            " token_estimate, content_hash) VALUES (?, 0, 'main', 's', 'n', 't', 1, 'ch')",
            (mid,),
        )
        row = await tx.fetchone("SELECT last_insert_rowid() AS id")
        return int(row["id"]) if row else 0


@pytest.fixture
async def indexed(tmp_path: Path) -> SqliteVecStore:
    """A store in a file that also has the real index tables, with three documents."""
    path = tmp_path / "index.db"
    async with await Database.open(path) as db:
        await migrate(db, MIGRATIONS)
        c1 = await add_doc(
            db,
            "m1",
            thread="t1",
            date="2024-01-10T09:00:00Z",
            addr="alex@example.com",
            name="Alex Rivera",
        )
        c2 = await add_doc(
            db,
            "m2",
            thread="t1",
            date="2024-02-10T09:00:00Z",
            addr="sam@example.org",
            name="Sam Lee",
        )
        c3 = await add_doc(
            db,
            "m3",
            thread="t2",
            date="2024-03-10T09:00:00Z",
            addr="alex@example.com",
            name="Alex Rivera",
            acct="a2",
        )
    store = SqliteVecStore(path)
    await store.start(make_context("vector_store"))
    await store.upsert(
        [
            item("m1", X, chunk=str(c1)),
            item("m2", X, chunk=str(c2)),
            item("m3", X, chunk=str(c3), acct="a2"),
        ]
    )
    return store


async def ids(store: SqliteVecStore, filters: dict[str, str]) -> list[str]:
    return sorted(h.message_id for h in await store.query(X, 10, filters))


async def test_filters_by_thread_date_sender_and_account(indexed: SqliteVecStore) -> None:
    try:
        assert await ids(indexed, {}) == ["m1", "m2", "m3"]
        assert await ids(indexed, {"thread_key": "t1"}) == ["m1", "m2"]
        assert await ids(indexed, {"after": "2024-02-01"}) == ["m2", "m3"]
        assert await ids(indexed, {"before": "2024-02-01"}) == ["m1"]
        assert await ids(indexed, {"after": "2024-02-01", "before": "2024-03-01"}) == ["m2"]
        assert await ids(indexed, {"sender": "ALEX"}) == ["m1", "m3"]
        assert await ids(indexed, {"sender": "sam@example.org"}) == ["m2"]
        assert await ids(indexed, {"sender": "alex", "account_id": "a2"}) == ["m3"]
        assert await ids(indexed, {"thread_key": "t1", "sender": "alex"}) == ["m1"]
        assert await ids(indexed, {"thread_key": "nope"}) == []
        with pytest.raises(PluginError, match="ISO date"):
            await indexed.query(X, 1, {"after": "last tuesday"})
    finally:
        await indexed.stop()


async def test_a_narrow_filter_still_finds_a_far_away_match(tmp_path: Path) -> None:
    """The reason for pre-filtering: the one allowed vector is the worst match of many."""
    store = SqliteVecStore(tmp_path / "index.db")
    await store.start(make_context("vector_store"))
    try:
        await store.upsert([item(f"n{i}", X, acct="other") for i in range(300)])
        await store.upsert([item("wanted", (0.0, 1.0, 0.1), acct="mine")])
        hits = await store.query(X, 3, {"account_id": "mine"})
        assert [h.message_id for h in hits] == ["wanted"]
    finally:
        await store.stop()


# -- orphans and planning ------------------------------------------------------------------
async def test_vectors_of_vanished_chunks_and_messages_are_pruned(indexed: SqliteVecStore) -> None:
    try:
        db = indexed._open()
        await db.execute("DELETE FROM chunks WHERE message_id = 'm1'")
        await db.execute("DELETE FROM docs WHERE message_id = 'm3'")
        assert await indexed.prune_orphans() == 2
        assert await ids(indexed, {}) == ["m2"]
        assert await indexed.prune_orphans() == 0
    finally:
        await indexed.stop()


async def test_pruning_a_file_without_index_tables_does_nothing(started: SqliteVecStore) -> None:
    await started.upsert([item("x", X)])
    assert await started.prune_orphans() == 0
    assert (await started.stats()).count == 1


async def test_reembed_plan_counts_what_is_missing_and_estimates_time(
    started: SqliteVecStore,
) -> None:
    await started.upsert([item("a", X), item("b", Y), item("c", Z)])
    plan = await started.reembed_plan("m-b")
    assert (plan.chunks_total, plan.chunks_done, plan.chunks_needed) == (3, 0, 3)
    assert plan.seconds is None  # no throughput known yet
    await started.record_throughput(100, 10)  # 10 per second
    await started.begin_model("m-b", 3)
    await started.upsert([item("a", X, model="m-b")])
    plan = await started.reembed_plan("m-b")
    assert (plan.chunks_done, plan.chunks_needed) == (1, 2)
    assert plan.seconds == pytest.approx(0.2)
    await started.record_throughput(30, 1)  # smoothed with the earlier rate: (30 + 10) / 2
    assert (await started.reembed_plan("m-b")).seconds == pytest.approx(0.1)


# -- concurrency and wiring ----------------------------------------------------------------
async def test_reads_keep_working_while_writes_happen(started: SqliteVecStore) -> None:
    await started.upsert([item("seed", X)])

    async def writer() -> None:
        for i in range(30):
            await started.upsert([item(f"w{i}", (1.0, float(i + 1), 0.0))])

    async def reader() -> int:
        n = 0
        for _ in range(60):
            assert await started.query(X, 3)
            n += 1
        return n

    results = await asyncio.gather(writer(), reader(), reader())
    assert (results[1], results[2]) == (60, 60)
    assert (await started.stats()).count == 31


def test_it_is_registered_as_an_entry_point() -> None:
    found = {ep.name: ep.value for ep in entry_points(group="oaken.vector_stores")}
    assert found["sqlite-vec"] == "oaken_vec_sqlite:SqliteVecStore"


def test_core_does_not_import_the_plugin() -> None:
    core = Path(__file__).parents[2] / "oaken"
    offenders = [
        str(p.relative_to(core))
        for p in core.rglob("*.py")
        if re.search(r"^\s*(from|import)\s+oaken_vec_sqlite", p.read_text(), re.M)
    ]
    assert offenders == []


def test_the_package_imports_without_the_extension_installed() -> None:
    """sqlite-vec is optional: importing the plugin must not need it, only starting it."""
    code = "import sys; sys.modules['sqlite_vec'] = None\nimport oaken_vec_sqlite; print('ok')"
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)  # noqa: S603
    assert out.stdout.strip() == "ok", out.stderr


async def test_starting_without_the_extension_explains_what_to_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "sqlite_vec", None)
    with pytest.raises(PluginError, match=r"oaken\[vec\]"):
        await SqliteVecStore(tmp_path / "i.db").start(make_context("vector_store"))


async def test_the_loader_works_here(started: SqliteVecStore) -> None:
    """The smoke test CI runs on Linux, macOS and Windows: the extension loads and answers."""
    health = await started.health()
    assert health.ok and health.detail.startswith("sqlite-vec v")
