# SPDX-License-Identifier: Apache-2.0
"""A vector store inside ``index.db``, using the sqlite-vec extension (brute-force KNN).

Layout, all in one file with the FTS5 index so a vector and its chunk share a transaction:

* ``vec_items``  one row per (model, message, chunk): its account, the hash of the chunk text it
  was made from, and the row id used in the model's vector table.
* ``vec_models`` the models the store holds. One is *active* (what ``query`` and ``stats`` see);
  others may be *building* while a new model is embedded, so the old vectors stay until the
  replacement is complete (``begin_model`` / ``activate_model`` / ``drop_model``).
* ``vec_N``      one ``vec0`` table per model (float32, cosine). A model never shares a table.

Metadata filters pre-select candidate rows (``rowid IN (...)``) and then run the KNN over only
those. That is exact for any selectivity, and costs a scan of the candidates: cheap when the
filter is narrow, a little slower than no filter when it keeps nearly everything. Filtering after
the KNN would lose results whenever the filter is narrow.

Triggers are deliberately not used to keep vectors in step with ``chunks``: a connection that
did not load the extension (the index writer) would fail on them. ``prune_orphans`` does it.
"""

import asyncio
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Final

from oaken.core import paths
from oaken.core.clock import fmt_ts
from oaken.core.db import Database, Tx
from oaken.core.errors import Conflict, PluginError, StorageError
from oaken.plugins.base import BaseVectorStore
from oaken.plugins.contracts import Health, Hit, PluginContext, VectorItem, VectorStats

SCHEMA_VERSION: Final = "1"
MAX_K: Final = 4096  # sqlite-vec refuses a larger k
_BATCH: Final = 500
# Filters with no meaning outside index.db: they need its ``docs`` table.
_DOC_FILTERS: Final = ("thread_key", "after", "before", "sender")
_FILTERS: Final = ("account_id", "model_id", *_DOC_FILTERS)

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS vec_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS vec_models (
        model_id   TEXT PRIMARY KEY,
        dimensions INTEGER NOT NULL,
        dtype      TEXT NOT NULL DEFAULT 'float32' CHECK (dtype IN ('float32', 'int8')),
        tbl        TEXT NOT NULL UNIQUE,
        state      TEXT NOT NULL CHECK (state IN ('active', 'building')),
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS vec_items (
        id           INTEGER PRIMARY KEY,
        model_id     TEXT NOT NULL REFERENCES vec_models (model_id),
        message_id   TEXT NOT NULL,
        chunk_id     TEXT NOT NULL,
        account_id   TEXT,
        content_hash TEXT NOT NULL DEFAULT '',
        UNIQUE (model_id, message_id, chunk_id)
    )""",
    "CREATE INDEX IF NOT EXISTS vec_items_message ON vec_items (message_id)",
    "CREATE INDEX IF NOT EXISTS vec_items_account ON vec_items (model_id, account_id)",
)


@dataclass(frozen=True)
class UpsertResult:
    written: int
    skipped: int  # same chunk text and model already stored


@dataclass(frozen=True)
class ModelInfo:
    model_id: str
    dimensions: int
    state: str  # "active" or "building"
    vectors: int
    dtype: str = "float32"


@dataclass(frozen=True)
class ReembedPlan:
    """What switching to ``model_id`` costs. ``seconds`` is None until a throughput is known."""

    model_id: str
    chunks_total: int
    chunks_done: int
    chunks_needed: int
    seconds: float | None


def _check_vector(vector: Sequence[float]) -> None:
    if not vector:
        raise PluginError("sqlite-vec: a vector cannot be empty")
    if not all(math.isfinite(x) for x in vector):
        raise PluginError("sqlite-vec: a vector contains NaN or infinity")
    if not any(vector):
        raise PluginError("sqlite-vec: a zero vector has no direction (cosine is undefined)")


def _iso(value: str, name: str) -> str:
    try:
        when = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PluginError(f"sqlite-vec: filter {name} needs an ISO date, not {value!r}") from exc
    return fmt_ts(when if when.tzinfo else when.replace(tzinfo=UTC))


def _dtype(value: str) -> str:
    if value not in ("float32", "int8"):
        raise PluginError(f"sqlite-vec: dtype must be float32 or int8, not {value!r}")
    return value


def _wrap(dtype: str) -> str:
    return "vec_int8(?)" if dtype == "int8" else "?"


def _blob(sqlite_vec: Any, vector: Sequence[float], dtype: str) -> bytes:
    """The vector as sqlite-vec wants it. int8 scales by the largest component first (cosine
    ignores scale), so the whole -127..127 range is used."""
    if dtype != "int8":
        return bytes(sqlite_vec.serialize_float32(list(vector)))
    peak = max(abs(x) for x in vector)
    return bytes(sqlite_vec.serialize_int8([round(x / peak * 127) for x in vector]))


def _marks(n: int) -> str:
    return ",".join("?" * n)


class SqliteVecStore(BaseVectorStore):
    """``path`` defaults to ``index.db`` in the data folder (or ``config["path"]``)."""

    version: ClassVar[str] = "1"

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._db: Database | None = None
        self._lock = asyncio.Lock()  # one writer at a time inside this plugin
        self._dtype = "float32"  # what a new model uses; set by ``config["dtype"]``
        self.last_upsert = UpsertResult(0, 0)

    # -- lifecycle --------------------------------------------------------------------------
    async def start(self, ctx: PluginContext) -> None:
        await super().start(ctx)
        self._dtype = _dtype(str(ctx.config.get("dtype") or "float32"))
        path = self._path or Path(
            ctx.config.get("path") or paths.data_dir() / ctx.settings.storage.index_db
        )
        try:
            import sqlite_vec  # noqa: PLC0415 - imported here so a missing backend is a plugin error
        except ImportError as exc:
            raise PluginError(
                "sqlite-vec is missing; it normally ships with Oaken. Reinstall the core package, "
                "e.g. `uv tool install oaken-cli --with 'oaken[vec]'` (uv tool) or "
                "`pip install 'oaken[vec]'` (pip)."
            ) from exc
        db = await Database.open(
            Path(path),
            read_pool_size=ctx.settings.storage.read_pool_size,
            busy_timeout_ms=ctx.settings.storage.busy_timeout_ms,
        )
        try:
            if not await db.load_extension(sqlite_vec.loadable_path()):
                raise PluginError(
                    "this Python's SQLite cannot load extensions, so sqlite-vec cannot run; "
                    "use a Python build that allows it (the uv-managed ones do)"
                )
            async with db.transaction() as tx:
                for statement in _SCHEMA:
                    await tx.execute(statement)
                columns = {r["name"] for r in await tx.fetchall("PRAGMA table_info(vec_models)")}
                if "dtype" not in columns:  # a store made before int8 was a choice
                    await tx.execute(
                        "ALTER TABLE vec_models ADD COLUMN dtype TEXT NOT NULL DEFAULT 'float32'"
                    )
                await tx.execute(
                    "INSERT OR IGNORE INTO vec_meta (key, value) VALUES ('schema_version', ?)",
                    (SCHEMA_VERSION,),
                )
        except BaseException:
            await db.close()
            raise
        self._db = db

    async def stop(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def health(self) -> Health:
        try:
            row = await self._open().fetchone("SELECT vec_version() AS v")
        except (StorageError, PluginError) as exc:
            return Health(False, str(exc))
        return Health(True, f"sqlite-vec {row['v'] if row else '?'}")

    def _open(self) -> Database:
        if self._db is None:
            raise PluginError("sqlite-vec: the store is not started")
        return self._db

    # -- models -----------------------------------------------------------------------------
    @staticmethod
    async def _models(tx: Tx | Database) -> dict[str, dict[str, Any]]:
        rows = await tx.fetchall("SELECT model_id, dimensions, dtype, tbl, state FROM vec_models")
        return {r["model_id"]: dict(r) for r in rows}

    @staticmethod
    async def _new_model(tx: Tx, model_id: str, dimensions: int, dtype: str, state: str) -> str:
        row = await tx.fetchone("SELECT COALESCE(MAX(rowid), 0) + 1 AS n FROM vec_models")
        table = f"vec_{row['n'] if row else 1}"
        await tx.execute(
            f"CREATE VIRTUAL TABLE {table} USING vec0("  # noqa: S608 - table is vec_<int>
            f"embedding {'int8' if dtype == 'int8' else 'float'}[{int(dimensions)}]"
            " distance_metric=cosine)"
        )
        await tx.execute(
            "INSERT INTO vec_models (model_id, dimensions, dtype, tbl, state, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (model_id, dimensions, dtype, table, state, fmt_ts(datetime.now(UTC))),
        )
        return table

    async def begin_model(self, model_id: str, dimensions: int, dtype: str | None = None) -> None:
        """Make room for vectors of a new model, next to the active one, without touching it.
        ``dtype`` (``float32`` or ``int8``) defaults to the plugin's ``dtype`` setting."""
        if dimensions < 1:
            raise PluginError("sqlite-vec: dimensions must be positive")
        dtype = _dtype(dtype or self._dtype)
        async with self._lock, self._open().transaction() as tx:
            models = await self._models(tx)
            if model_id in models:
                if models[model_id]["dimensions"] != dimensions:
                    raise Conflict(
                        f"{model_id} is already held with {models[model_id]['dimensions']} dims"
                    )
                return
            await self._new_model(
                tx, model_id, dimensions, dtype, "building" if models else "active"
            )

    async def activate_model(self, model_id: str) -> None:
        """Make ``model_id`` what queries and stats see. The previous one is kept (as building)
        until ``drop_model``."""
        async with self._lock, self._open().transaction() as tx:
            if model_id not in await self._models(tx):
                raise Conflict(f"the store holds no vectors of {model_id}")
            await tx.execute("UPDATE vec_models SET state = 'building' WHERE state = 'active'")
            await tx.execute(
                "UPDATE vec_models SET state = 'active' WHERE model_id = ?", (model_id,)
            )

    async def drop_model(self, model_id: str) -> int:
        """Delete every vector of a model that is not the active one. Returns how many."""
        async with self._lock, self._open().transaction() as tx:
            models = await self._models(tx)
            if model_id not in models:
                return 0
            if models[model_id]["state"] == "active":
                raise Conflict(f"{model_id} is the active model; activate another first")
            count = await tx.fetchone(
                "SELECT COUNT(*) AS n FROM vec_items WHERE model_id = ?", (model_id,)
            )
            await tx.execute(f"DROP TABLE {models[model_id]['tbl']}")
            await tx.execute("DELETE FROM vec_items WHERE model_id = ?", (model_id,))
            await tx.execute("DELETE FROM vec_models WHERE model_id = ?", (model_id,))
            return int(count["n"]) if count else 0

    async def models(self) -> list[ModelInfo]:
        db = self._open()
        rows = await db.fetchall(
            "SELECT m.model_id, m.dimensions, m.state, m.dtype, COUNT(i.id) AS n FROM vec_models m "
            "LEFT JOIN vec_items i ON i.model_id = m.model_id GROUP BY m.model_id "
            "ORDER BY m.rowid"
        )
        return [
            ModelInfo(r["model_id"], r["dimensions"], r["state"], r["n"], r["dtype"]) for r in rows
        ]

    # -- writes -----------------------------------------------------------------------------
    async def upsert(self, items: Sequence[VectorItem]) -> None:
        """Insert or replace by (model, message, chunk). A chunk already stored for the same
        model with the same ``content_hash`` is left alone. All or nothing."""
        for item in items:
            _check_vector(item.vector)
        import sqlite_vec  # noqa: PLC0415

        # Last one wins when a batch names the same chunk twice.
        unique = {(i.model_id, i.message_id, i.chunk_id): i for i in items}.values()
        written = skipped = 0
        async with self._lock, self._open().transaction() as tx:
            models = await self._models(tx)
            by_model: dict[str, list[VectorItem]] = {}
            for item in unique:
                known = models.get(item.model_id)
                if known is None:
                    if models:
                        held = next((m for m in models.values() if m["state"] == "active"), None)
                        raise Conflict(
                            f"store holds {held['model_id'] if held else 'other models'}; "
                            f"refusing {item.model_id}. Call begin_model() to add a model"
                            " next to it, or clear the store."
                        )
                    table = await self._new_model(
                        tx, item.model_id, item.dimensions, self._dtype, "active"
                    )
                    known = models[item.model_id] = {
                        "model_id": item.model_id,
                        "dimensions": item.dimensions,
                        "dtype": self._dtype,
                        "tbl": table,
                        "state": "active",
                    }
                if known["dimensions"] != item.dimensions:
                    raise Conflict(
                        f"{item.model_id} has {known['dimensions']} dimensions;"
                        f" refusing a vector of {item.dimensions}"
                    )
                by_model.setdefault(item.model_id, []).append(item)
            top = await tx.fetchone("SELECT COALESCE(MAX(id), 0) AS n FROM vec_items")
            next_id = int(top["n"]) + 1 if top else 1
            for model_id, group in by_model.items():
                table = models[model_id]["tbl"]
                have = await self._existing(tx, model_id, group)
                replaced: list[tuple[int]] = []
                updated: list[tuple[str | None, str, int]] = []
                created: list[tuple[int, str, str, str, str | None, str]] = []
                vectors: list[tuple[int, bytes]] = []
                for item in group:
                    blob = _blob(sqlite_vec, item.vector, models[model_id]["dtype"])
                    row = have.get((item.message_id, item.chunk_id))
                    if row is None:
                        created.append(
                            (
                                next_id,
                                model_id,
                                item.message_id,
                                item.chunk_id,
                                item.account_id,
                                item.content_hash,
                            )
                        )
                        vectors.append((next_id, blob))
                        next_id += 1
                    elif row[1] == item.content_hash:
                        skipped += 1
                        continue
                    else:
                        replaced.append((row[0],))
                        updated.append((item.account_id, item.content_hash, row[0]))
                        vectors.append((row[0], blob))
                    written += 1
                await tx.executemany(f"DELETE FROM {table} WHERE rowid = ?", replaced)  # noqa: S608
                await tx.executemany(
                    "UPDATE vec_items SET account_id = ?, content_hash = ? WHERE id = ?", updated
                )
                await tx.executemany(
                    "INSERT INTO vec_items (id, model_id, message_id, chunk_id, account_id,"
                    " content_hash) VALUES (?, ?, ?, ?, ?, ?)",
                    created,
                )
                await tx.executemany(
                    f"INSERT INTO {table} (rowid, embedding) VALUES (?, {_wrap(models[model_id]['dtype'])})",  # noqa: S608,E501
                    vectors,
                )
        self.last_upsert = UpsertResult(written, skipped)

    @staticmethod
    async def _existing(
        tx: Tx, model_id: str, group: Sequence[VectorItem]
    ) -> dict[tuple[str, str], tuple[int, str]]:
        """``{(message_id, chunk_id): (row id, content_hash)}`` for the chunks already stored."""
        wanted = sorted({i.message_id for i in group})
        have: dict[tuple[str, str], tuple[int, str]] = {}
        for start in range(0, len(wanted), _BATCH):
            part = wanted[start : start + _BATCH]
            rows = await tx.fetchall(
                "SELECT id, message_id, chunk_id, content_hash FROM vec_items "  # noqa: S608
                f"WHERE model_id = ? AND message_id IN ({_marks(len(part))})",
                (model_id, *part),
            )
            for r in rows:
                have[(r["message_id"], r["chunk_id"])] = (r["id"], r["content_hash"])
        return have

    async def delete(self, message_ids: Sequence[str]) -> None:
        """Remove every vector of these messages, for every model."""
        ids = list(dict.fromkeys(message_ids))
        async with self._lock, self._open().transaction() as tx:
            models = await self._models(tx)
            for start in range(0, len(ids), _BATCH):
                part = ids[start : start + _BATCH]
                for m in models.values():
                    await tx.execute(
                        f"DELETE FROM {m['tbl']} WHERE rowid IN "  # noqa: S608 - vec_<int>
                        f"(SELECT id FROM vec_items WHERE model_id = ? "
                        f"AND message_id IN ({_marks(len(part))}))",
                        (m["model_id"], *part),
                    )
                await tx.execute(
                    f"DELETE FROM vec_items WHERE message_id IN ({_marks(len(part))})",  # noqa: S608
                    part,
                )

    async def prune_orphans(self) -> int:
        """Remove vectors whose message or chunk is gone from ``index.db`` (a chunk is replaced
        when its text changes). Does nothing when the file has no index tables. Returns the count.
        """
        async with self._lock, self._open().transaction() as tx:
            tables = {
                r["name"]
                for r in await tx.fetchall(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name IN ('docs', 'chunks')"
                )
            }
            if tables != {"docs", "chunks"}:
                return 0
            rows = await tx.fetchall(
                "SELECT i.id, i.model_id FROM vec_items i "
                "WHERE NOT EXISTS (SELECT 1 FROM docs d WHERE d.message_id = i.message_id) "
                "OR (i.chunk_id GLOB '[0-9]*' AND NOT EXISTS "
                "(SELECT 1 FROM chunks c WHERE c.id = CAST(i.chunk_id AS INTEGER)))"
            )
            if not rows:
                return 0
            models = await self._models(tx)
            for r in rows:
                table = models[r["model_id"]]["tbl"]
                await tx.execute(f"DELETE FROM {table} WHERE rowid = ?", (r["id"],))  # noqa: S608
                await tx.execute("DELETE FROM vec_items WHERE id = ?", (r["id"],))
            return len(rows)

    # -- reads ------------------------------------------------------------------------------
    async def query(
        self, vector: Sequence[float], k: int, filters: dict[str, str] | None = None
    ) -> list[Hit]:
        """The ``k`` nearest chunks of the active model (or of ``filters["model_id"]``), best
        first, with score ``1 - cosine distance``. Filters: ``account_id``, ``model_id`` and,
        in ``index.db``, ``thread_key``, ``after``, ``before`` (ISO dates) and ``sender``."""
        filters = dict(filters or {})
        unknown = sorted(set(filters) - set(_FILTERS))
        if unknown:
            raise PluginError(f"sqlite-vec: unknown filter {unknown[0]!r}; known: {_FILTERS}")
        if k <= 0:
            return []
        _check_vector(vector)
        import sqlite_vec  # noqa: PLC0415

        db = self._open()
        models = await self._models(db)
        wanted = filters.pop("model_id", None)
        model = (
            models.get(wanted)
            if wanted
            else next((m for m in models.values() if m["state"] == "active"), None)
        )
        if wanted and model is None and models:
            raise Conflict(f"the store holds no vectors of {wanted}")
        if model is None:
            return []
        if len(vector) != model["dimensions"]:
            raise Conflict(
                f"the query has {len(vector)} dimensions; {model['model_id']} has "
                f"{model['dimensions']}"
            )
        where, params, join = ["i.model_id = ?"], [model["model_id"]], ""
        if "account_id" in filters:
            where.append("i.account_id = ?")
            params.append(filters.pop("account_id"))
        if filters:  # what is left needs the docs table
            if not await db.fetchone("SELECT 1 FROM sqlite_master WHERE name = 'docs'"):
                raise PluginError(
                    f"sqlite-vec: filter {sorted(filters)[0]!r} needs the index tables in this file"
                )
            join = " JOIN docs d ON d.message_id = i.message_id"
            if "thread_key" in filters:
                where.append("d.thread_key = ?")
                params.append(filters["thread_key"])
            if "after" in filters:
                where.append("d.date_utc >= ?")
                params.append(_iso(filters["after"], "after"))
            if "before" in filters:
                where.append("d.date_utc < ?")
                params.append(_iso(filters["before"], "before"))
            if "sender" in filters:
                where.append(
                    "(instr(lower(d.from_addr), lower(?)) > 0"
                    " OR instr(lower(d.from_name), lower(?)) > 0)"
                )
                params += [filters["sender"], filters["sender"]]
        k = min(k, MAX_K)
        rows = await db.fetchall(
            f"SELECT i.message_id, i.chunk_id, m.distance FROM "  # noqa: S608 - vec_<int>
            f"(SELECT rowid, distance FROM {model['tbl']} "
            f"WHERE embedding MATCH {_wrap(model['dtype'])} AND k = ? "
            f"AND rowid IN (SELECT i.id FROM vec_items i{join} WHERE {' AND '.join(where)})) m "
            f"JOIN vec_items i ON i.id = m.rowid ORDER BY m.distance, i.message_id, i.chunk_id",
            (_blob(sqlite_vec, vector, model["dtype"]), k, *params),
        )
        return [
            Hit(r["message_id"], r["chunk_id"], 1.0 - float(r["distance"]), model["model_id"])
            for r in rows
        ]

    async def stats(self) -> VectorStats:
        db = self._open()
        models = await self._models(db)
        active = next((m for m in models.values() if m["state"] == "active"), None)
        if active is None:
            return VectorStats(0)
        row = await db.fetchone(
            "SELECT COUNT(*) AS n FROM vec_items WHERE model_id = ?", (active["model_id"],)
        )
        return VectorStats(int(row["n"]) if row else 0, active["model_id"], active["dimensions"])

    async def stored_hashes(self, model_id: str, message_id: str) -> dict[str, str]:
        """``{chunk_id: content_hash}`` already embedded for one message, so the pipeline can
        skip chunks whose text did not change."""
        rows = await self._open().fetchall(
            "SELECT chunk_id, content_hash FROM vec_items WHERE model_id = ? AND message_id = ?",
            (model_id, message_id),
        )
        return {r["chunk_id"]: r["content_hash"] for r in rows}

    # -- planning a model change -------------------------------------------------------------
    async def record_throughput(self, chunks: int, seconds: float) -> None:
        """Note how fast the embedder went (chunks per second), smoothed with earlier runs. It
        is what ``reembed_plan`` uses for its time estimate."""
        if chunks <= 0 or seconds <= 0:
            return
        rate = chunks / seconds
        async with self._lock, self._open().transaction() as tx:
            row = await tx.fetchone("SELECT value FROM vec_meta WHERE key = 'embed_rate'")
            if row:
                rate = 0.5 * rate + 0.5 * float(row["value"])
            await tx.execute(
                "INSERT INTO vec_meta (key, value) VALUES ('embed_rate', ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (repr(rate),),
            )

    async def reembed_plan(self, new_model_id: str) -> ReembedPlan:
        """How many chunks still need a vector from ``new_model_id`` and about how long that
        takes. The chunk total is the index's chunk count (or, in a file with no index tables,
        the distinct chunks any model holds)."""
        db = self._open()
        has_chunks = await db.fetchone("SELECT 1 FROM sqlite_master WHERE name = 'chunks'")
        if has_chunks:
            row = await db.fetchone("SELECT COUNT(*) AS n FROM chunks")
        else:
            row = await db.fetchone(
                "SELECT COUNT(*) AS n FROM (SELECT DISTINCT message_id, chunk_id FROM vec_items)"
            )
        total = int(row["n"]) if row else 0
        done_row = await db.fetchone(
            "SELECT COUNT(*) AS n FROM vec_items WHERE model_id = ?", (new_model_id,)
        )
        done = int(done_row["n"]) if done_row else 0
        needed = max(0, total - done)
        rate_row = await db.fetchone("SELECT value FROM vec_meta WHERE key = 'embed_rate'")
        rate = float(rate_row["value"]) if rate_row else 0.0
        return ReembedPlan(new_model_id, total, done, needed, needed / rate if rate > 0 else None)
