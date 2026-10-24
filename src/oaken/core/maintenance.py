# SPDX-License-Identifier: Apache-2.0
"""Housekeeping jobs: today, ``maintenance.vacuum``.

A vacuum rewrites ``raw.db`` and ``index.db`` to give back the space deleted rows left. It runs as a
job (one at a time, with the same lease, retry and audit trail as any other), and it holds each
database's write lock while it works, so ingest waits and reads carry on. It needs roughly the
database's own size free on the disk, which is checked before it starts.
"""

import shutil
from pathlib import Path

from pydantic import BaseModel

from oaken.core.actors import SYSTEM, Actor
from oaken.core.db import Database
from oaken.core.errors import StorageError
from oaken.core.jobs import PRIORITY_BACKFILL, Enqueued, Job, JobQueue, JobRunner

VACUUM_KIND = "maintenance.vacuum"
ROOM = 1.1  # free space wanted, as a multiple of the database being rewritten


class VacuumPayload(BaseModel):
    pass


class VacuumResult(BaseModel):
    raw_before: int
    raw_after: int
    index_before: int
    index_after: int


def file_bytes(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def check_room(*dbs: Database) -> None:
    """Raise ``StorageError`` if the disk cannot take a rewrite of the largest of ``dbs``."""
    biggest = max(dbs, key=lambda d: file_bytes(d.path))
    need = int(file_bytes(biggest.path) * ROOM)
    free = shutil.disk_usage(biggest.path.parent).free
    if free < need:
        raise StorageError(
            f"not enough free disk to vacuum: {biggest.path.name} needs about {need:,} bytes free "
            f"and {free:,} are"
        )


def register_maintenance_jobs(runner: JobRunner, raw: Database, index: Database) -> None:
    async def handle(_payload: VacuumPayload, _job: Job) -> VacuumResult:
        check_room(raw, index)
        raw_before, index_before = file_bytes(raw.path), file_bytes(index.path)
        await raw.vacuum()
        await index.vacuum()
        return VacuumResult(
            raw_before=raw_before,
            raw_after=file_bytes(raw.path),
            index_before=index_before,
            index_after=file_bytes(index.path),
        )

    runner.register(VACUUM_KIND, VacuumPayload, handle, concurrency=1, result_model=VacuumResult)


async def enqueue_vacuum(queue: JobQueue, *, actor: Actor = SYSTEM) -> Enqueued:
    """Queue a vacuum. One at a time: asking while one is queued or running returns that job."""
    return await queue.enqueue(
        VACUUM_KIND,
        VacuumPayload(),
        priority=PRIORITY_BACKFILL,
        dedupe_key=VACUUM_KIND,
        actor=actor,
    )
