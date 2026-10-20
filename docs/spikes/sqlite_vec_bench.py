# SPDX-License-Identifier: Apache-2.0
"""Measurements behind docs/spikes/sqlite-vec.md. Run: uv run python docs/spikes/sqlite_vec_bench.py

Random vectors only (no mail). Brute-force KNN reads every vector, so its latency does not depend
on what the vectors contain; vectors are drawn from a pool of random unit vectors to keep
generation fast without numpy. Recall is measured on a separate small set of fully random vectors.
"""

import math
import os
import random
import sqlite3
import statistics
import struct
import sys
import tempfile
import time
from pathlib import Path

import sqlite_vec

SIZES = [10_000, 100_000, 300_000]
DIMS = [384, 768]
QUERIES = 30
K = 10
POOL = 2000


def connect(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path, isolation_level=None)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA journal_mode=WAL")
    return db


def unit(rng: random.Random, d: int) -> list[float]:
    v = [rng.gauss(0, 1) for _ in range(d)]
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def f32(v: list[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)


def i8(v: list[float]) -> bytes:
    return struct.pack(f"{len(v)}b", *[max(-127, min(127, round(x * 127 * 8))) for x in v])


KINDS = (("float32", f32, "float"), ("int8", i8, "int8"))


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def latency() -> None:
    rng = random.Random(7)
    print(f"{'N':>8} {'dim':>4} {'type':>7} {'insert/s':>9} {'p50 ms':>8} {'p95 ms':>8} {'MB':>7}")
    for d in DIMS:
        pool = [unit(rng, d) for _ in range(POOL)]
        for kind, enc, coltype in KINDS:
            wrap = "vec_int8(?)" if kind == "int8" else "?"
            blobs = [enc(v) for v in pool]
            for n in SIZES:
                with tempfile.TemporaryDirectory() as tmp:
                    path = str(Path(tmp) / "v.db")
                    db = connect(path)
                    db.execute(
                        f"CREATE VIRTUAL TABLE v USING vec0(embedding {coltype}[{d}] "
                        "distance_metric=cosine)"
                    )
                    t = time.perf_counter()
                    db.execute("BEGIN")
                    for i in range(n):
                        db.execute(
                            f"INSERT INTO v(rowid, embedding) VALUES (?, {wrap})",
                            (i + 1, blobs[rng.randrange(POOL)]),
                        )
                    db.execute("COMMIT")
                    ins = n / (time.perf_counter() - t)
                    qs = [enc(unit(rng, d)) for _ in range(QUERIES)]
                    db.execute(
                        f"SELECT rowid FROM v WHERE embedding MATCH {wrap} AND k=?", (qs[0], K)
                    ).fetchall()
                    times = []
                    for q in qs:
                        t = time.perf_counter()
                        db.execute(
                            f"SELECT rowid, distance FROM v WHERE embedding MATCH {wrap} AND k=? "
                            "ORDER BY distance",
                            (q, K),
                        ).fetchall()
                        times.append((time.perf_counter() - t) * 1000)
                    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    size = os.path.getsize(path) / 1e6
                    db.close()
                    print(
                        f"{n:>8} {d:>4} {kind:>7} {ins:>9.0f} {pct(times, 0.5):>8.1f} "
                        f"{pct(times, 0.95):>8.1f} {size:>7.1f}",
                        flush=True,
                    )


def recall() -> None:
    """Recall@10 of int8 against float32 on 20k fully random vectors."""
    rng = random.Random(11)
    for d in DIMS:
        data = [unit(rng, d) for _ in range(20_000)]
        queries = [unit(rng, d) for _ in range(50)]
        res: dict[str, list[set[int]]] = {}
        for kind, enc, coltype in KINDS:
            wrap = "vec_int8(?)" if kind == "int8" else "?"
            db = connect(":memory:")
            db.execute(
                f"CREATE VIRTUAL TABLE v USING vec0("
                f"embedding {coltype}[{d}] distance_metric=cosine)"
            )
            db.execute("BEGIN")
            db.executemany(
                f"INSERT INTO v(rowid, embedding) VALUES (?, {wrap})",
                ((i + 1, enc(v)) for i, v in enumerate(data)),
            )
            db.execute("COMMIT")
            res[kind] = [
                {
                    r[0]
                    for r in db.execute(
                        f"SELECT rowid FROM v WHERE embedding MATCH {wrap} AND k=?", (enc(q), K)
                    )
                }
                for q in queries
            ]
            db.close()
        hit = statistics.mean(
            len(a & b) / K for a, b in zip(res["float32"], res["int8"], strict=True)
        )
        print(f"recall@{K} int8 vs float32, dim {d}: {hit:.3f}", flush=True)


def consistency() -> None:
    """One file holding FTS5 and vec0: does a rollback undo both, and do readers keep working
    while a writer holds a transaction open?"""
    import threading

    d = 384
    rng = random.Random(5)
    blob = f32(unit(rng, d))
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "index.db")
        db = connect(path)
        db.execute("CREATE VIRTUAL TABLE fts USING fts5(text)")
        db.execute(f"CREATE VIRTUAL TABLE v USING vec0(embedding float[{d}])")
        db.execute("BEGIN")
        db.execute("INSERT INTO fts(rowid, text) VALUES (1, 'kept')")
        db.execute("INSERT INTO v(rowid, embedding) VALUES (1, ?)", (blob,))
        db.execute("COMMIT")
        db.execute("BEGIN")
        db.execute("INSERT INTO fts(rowid, text) VALUES (2, 'rolled back')")
        db.execute("INSERT INTO v(rowid, embedding) VALUES (2, ?)", (blob,))
        db.execute("ROLLBACK")
        n_fts = db.execute("SELECT count(*) FROM fts").fetchone()[0]
        n_vec = db.execute("SELECT count(*) FROM v").fetchone()[0]
        print(f"after rollback: fts rows {n_fts}, vec rows {n_vec} (both should be 1)")

        stop = threading.Event()
        errors: list[str] = []
        reads = [0]

        def reader() -> None:
            r = connect(path)
            while not stop.is_set():
                try:
                    r.execute(
                        "SELECT rowid FROM v WHERE embedding MATCH ? AND k=5", (blob,)
                    ).fetchall()
                    r.execute("SELECT rowid FROM fts WHERE fts MATCH 'kept'").fetchall()
                    reads[0] += 1
                except sqlite3.Error as exc:
                    errors.append(str(exc))
            r.close()

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        t0 = time.perf_counter()
        for batch in range(20):
            db.execute("BEGIN")
            for i in range(500):
                rid = 10 + batch * 500 + i
                db.execute("INSERT INTO fts(rowid, text) VALUES (?, 'bulk text')", (rid,))
                db.execute("INSERT INTO v(rowid, embedding) VALUES (?, ?)", (rid, blob))
            db.execute("COMMIT")
        dt = time.perf_counter() - t0
        stop.set()
        for t in threads:
            t.join()
        print(
            f"10000 rows written in {dt:.1f}s while 4 readers ran {reads[0]} queries; "
            f"reader errors: {len(errors)} {sorted(set(errors))[:2]}"
        )
        db.close()


if __name__ == "__main__":
    which = sys.argv[1:] or ["latency", "recall", "consistency"]
    if "consistency" in which:
        consistency()
    if "recall" in which:
        recall()
    if "latency" in which:
        latency()
