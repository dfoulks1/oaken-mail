# SPDX-License-Identifier: Apache-2.0
"""``raw.db``: the source of truth for mail. Compressed, content-addressed RFC 822 messages plus
the bookkeeping a provider needs to sync. Nothing derived lives here.

* A message is stored as a zstd frame keyed by the sha256 of its raw bytes, so identical bytes
  share one blob. Small messages live in the database; messages over ``storage.inline_max_bytes``
  go whole to the **LFS folder** (one file per blob) and ``raw.db`` keeps a link plus a small
  header stub. Nothing is ever truncated.
* ``put_message`` is idempotent and transactional. Changed bytes under the same provider id add
  a new blob and move the pointer; the old blob stays until ``gc_blobs()``.
* Deleting is soft (``deleted_at``). ``gc_blobs()`` removes only blobs no message points at.
* Message content is never logged: log lines carry ids, sizes and hashes only.
* ``accounts.config_json`` holds non-secret settings. Credentials belong in the OS keyring, and
  account creation refuses keys that look like secrets.
"""

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from sqlite3 import Row
from typing import Any

import zstandard

from oaken.core.clock import Clock, fmt_ts, parse_ts
from oaken.core.db import Database
from oaken.core.errors import ConfigError, Conflict, NotFound, StorageError
from oaken.core.events import EventBus, LargeMessageStored, MessageStored
from oaken.core.ids import AccountId, MessageId, new_message_id
from oaken.core.logging import get_logger
from oaken.core.settings import StorageSettings

log = get_logger("oaken.rawstore")

COMPRESSION_LEVEL = 3
CHUNK = 1024 * 1024
HEADER_STUB_MAX = 1024 * 1024
OFF_THREAD_BYTES = 256 * 1024  # compress/hash bigger inputs off the event loop
ORPHAN_GRACE_SECONDS = (
    3600.0  # gc leaves fresh unreferenced LFS files alone (a put may be mid-flight)
)
PAGE = 500

_ITER_SQL_ALL = (
    "SELECT m.*, b.size_raw, b.codec FROM messages m JOIN blobs b ON b.sha256 = m.blob_sha256"
    " WHERE m.account_id = ? AND (m.first_seen_at > ? OR (m.first_seen_at = ? AND m.id > ?))"
    " ORDER BY m.first_seen_at, m.id LIMIT ?"
)
_ITER_SQL_LIVE = _ITER_SQL_ALL.replace("ORDER BY", "AND m.deleted_at IS NULL ORDER BY")

_ACCOUNT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_SECRETISH = re.compile(r"token|secret|passw|credential|api[_-]?key|private[_-]?key", re.IGNORECASE)


@dataclass(frozen=True)
class MessageMeta:
    """What a provider knows about a message besides its bytes."""

    internal_date: datetime | None = None
    is_read: bool = False
    is_starred: bool = False
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class PutResult:
    message_id: MessageId
    is_new: bool
    changed: bool


@dataclass(frozen=True)
class Account:
    id: AccountId
    provider: str
    display_name: str
    created_at: datetime
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StoredMessage:
    id: MessageId
    account_id: AccountId
    provider_id: str
    blob_sha256: str
    internal_date: datetime | None
    first_seen_at: datetime
    deleted_at: datetime | None
    size_raw: int
    in_lfs: bool


@dataclass(frozen=True)
class StoreStats:
    accounts: int
    messages: int
    deleted_messages: int
    blobs: int
    lfs_blobs: int
    bytes_raw: int
    bytes_stored: int

    @property
    def ratio(self) -> float:
        """Raw bytes per stored byte (above 1 means the store is smaller than the mail)."""
        return self.bytes_raw / self.bytes_stored if self.bytes_stored else 0.0


@dataclass(frozen=True)
class GcResult:
    blobs_removed: int
    bytes_freed: int
    files_removed: int


# -- byte helpers (synchronous; the store runs the big ones in a thread) ---------------------
def header_block(raw: bytes, limit: int = HEADER_STUB_MAX) -> bytes:
    """The message up to the blank line ending its headers, capped at ``limit`` bytes."""
    ends = [
        i + n for sep, n in ((b"\r\n\r\n", 4), (b"\n\n", 2)) if (i := raw.find(sep, 0, limit)) >= 0
    ]
    return raw[: min(ends) if ends else limit]


def compress(raw: bytes) -> bytes:
    """One zstd frame with a checksum and the content size recorded, built in chunks."""
    obj = zstandard.ZstdCompressor(
        level=COMPRESSION_LEVEL, write_checksum=True, write_content_size=True
    ).compressobj(size=len(raw))
    parts = [obj.compress(raw[i : i + CHUNK]) for i in range(0, len(raw), CHUNK)]
    parts.append(obj.flush())
    return b"".join(parts)


def decompress(frame: bytes, size_raw: int) -> bytes:
    """Inverse of ``compress``, streamed, and never producing more than ``size_raw`` bytes."""
    obj = zstandard.ZstdDecompressor().decompressobj()
    out: list[bytes] = []
    total = 0
    try:
        for i in range(0, len(frame), CHUNK):
            piece = obj.decompress(frame[i : i + CHUNK])
            total += len(piece)
            if total > size_raw:
                raise StorageError("stored message is larger than recorded (corrupt)")
            out.append(piece)
    except zstandard.ZstdError as exc:
        raise StorageError(f"stored message is corrupt: {exc}") from exc
    if not obj.eof or total != size_raw:
        raise StorageError("stored message is truncated or its size is wrong (corrupt)")
    return b"".join(out)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _write_frame(path: Path, frame: bytes) -> None:
    """Atomically place ``frame`` at ``path`` (owner-only)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            for i in range(0, len(frame), CHUNK):
                f.write(frame[i : i + CHUNK])
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _prepare(raw: bytes, lfs: bool) -> tuple[str, bytes, bytes | None]:
    """sha256, the compressed frame, and (for LFS) the compressed header stub."""
    return _sha256(raw), compress(raw), compress(header_block(raw)) if lfs else None


def _check_config(config: dict[str, Any], where: str = "config") -> None:
    for key, value in config.items():
        if _SECRETISH.search(str(key)):
            raise ConfigError(
                f"{where}.{key} looks like a secret; credentials belong in the OS keyring"
            )
        if isinstance(value, dict):
            _check_config(value, f"{where}.{key}")


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class RawStore:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        lfs_dir: Path,
        settings: StorageSettings | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self.db = db
        self.clock = clock
        self.lfs_dir = lfs_dir
        self.settings = settings or StorageSettings()
        self.bus = bus

    # -- accounts ---------------------------------------------------------------------------
    async def create_account(
        self,
        account_id: str,
        provider: str,
        display_name: str,
        config: dict[str, Any] | None = None,
    ) -> Account:
        """Register an account. ``config`` is stored as given (non-secret settings only)."""
        if not _ACCOUNT_ID.match(account_id):
            raise ConfigError("an account id is 1-32 chars of a-z, 0-9, '-' and '_'")
        if not provider.strip() or not display_name.strip():
            raise ConfigError("an account needs a provider and a display name")
        config = config or {}
        _check_config(config)
        try:
            blob = json.dumps(config)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"account config must be JSON-serializable: {exc}") from exc
        now = self.clock.now()
        async with self.db.transaction() as tx:
            existing = await tx.fetchone(
                "SELECT removed_at FROM accounts WHERE id = ?", (account_id,)
            )
            if existing is not None and existing["removed_at"] is None:
                raise Conflict(f"account already exists: {account_id}")
            if existing is not None:  # a soft-removed id: revive it, keeping its stored mail
                await tx.execute(
                    "UPDATE accounts SET provider = ?, display_name = ?, created_at = ?,"
                    " config_json = ?, removed_at = NULL WHERE id = ?",
                    (provider.strip(), display_name.strip(), fmt_ts(now), blob, account_id),
                )
            else:
                await tx.execute(
                    "INSERT INTO accounts (id, provider, display_name, created_at, config_json)"
                    " VALUES (?,?,?,?,?)",
                    (account_id, provider.strip(), display_name.strip(), fmt_ts(now), blob),
                )
        return Account(AccountId(account_id), provider.strip(), display_name.strip(), now, config)

    async def get_account(self, account_id: AccountId) -> Account:
        row = await self.db.fetchone(
            "SELECT * FROM accounts WHERE id = ? AND removed_at IS NULL", (account_id,)
        )
        if row is None:
            raise NotFound(f"no such account: {account_id}")
        return _account(row)

    async def list_accounts(self) -> list[Account]:
        rows = await self.db.fetchall(
            "SELECT * FROM accounts WHERE removed_at IS NULL ORDER BY created_at, id"
        )
        return [_account(r) for r in rows]

    async def set_account_config(self, account_id: AccountId, config: dict[str, Any]) -> Account:
        """Replace an account's config (the CLI wizard's write path)."""
        _check_config(config)
        try:
            blob = json.dumps(config)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"account config must be JSON-serializable: {exc}") from exc
        changed = await self.db.execute(
            "UPDATE accounts SET config_json = ? WHERE id = ?", (blob, account_id)
        )
        if not changed:
            raise NotFound(f"no such account: {account_id}")
        return await self.get_account(account_id)

    async def delete_account(self, account_id: AccountId) -> bool:
        """Soft-remove the account: its stored mail is kept (prompt 24 Q2) and still owns its
        ``account_id`` foreign key, so the row stays but is marked ``removed_at`` and hidden from
        ``get_account``/``list_accounts``. Sync cursors are cleared (a re-added account re-syncs
        from scratch). Purging the mail itself is a separate confirmed operation. Returns False if
        there was no such live account."""
        async with self.db.transaction() as tx:
            row = await tx.fetchone(
                "SELECT 1 FROM accounts WHERE id = ? AND removed_at IS NULL", (account_id,)
            )
            if row is None:
                return False
            await tx.execute(
                "UPDATE accounts SET removed_at = ? WHERE id = ?",
                (fmt_ts(self.clock.now()), account_id),
            )
            await tx.execute("DELETE FROM sync_cursors WHERE account_id = ?", (account_id,))
        return True

    # -- messages ---------------------------------------------------------------------------
    async def put_message(
        self,
        account_id: AccountId,
        provider_id: str,
        raw: bytes,
        meta: MessageMeta | None = None,
    ) -> tuple[MessageId, bool]:
        """Store ``raw``. Returns ``(message_id, is_new)``; ``is_new`` is False when the provider
        id was already stored (whether or not its bytes changed)."""
        res = await self.put(account_id, provider_id, raw, meta)
        return res.message_id, res.is_new

    async def put(
        self,
        account_id: AccountId,
        provider_id: str,
        raw: bytes,
        meta: MessageMeta | None = None,
    ) -> PutResult:
        """``put_message`` that also says whether anything changed: ``changed`` is True for a new
        message, new bytes, or a deleted message that came back, and False for a no-op."""
        meta = meta or MessageMeta()
        size = len(raw)
        to_lfs = size > self.settings.inline_max_bytes
        if size > OFF_THREAD_BYTES:
            sha, frame, stub = await asyncio.to_thread(_prepare, raw, to_lfs)
        else:
            sha, frame, stub = _prepare(raw, to_lfs)
        rel = f"{sha[:2]}/{sha}.zst" if to_lfs else None
        if rel is not None:
            await asyncio.to_thread(_write_frame, self.lfs_dir / rel, frame)

        now = self.clock.now()
        stamp = fmt_ts(now)
        async with self.db.transaction() as tx:
            if not await tx.fetchone("SELECT 1 FROM accounts WHERE id = ?", (account_id,)):
                raise NotFound(f"no such account: {account_id}")
            await tx.execute(
                "INSERT OR IGNORE INTO blobs (sha256, size_raw, size_stored, codec, data, stub,"
                " lfs_path) VALUES (?,?,?,?,?,?,?)",
                (
                    sha,
                    size,
                    len(frame),
                    "zstd-file" if to_lfs else "zstd",
                    None if to_lfs else frame,
                    stub,
                    rel,
                ),
            )
            row = await tx.fetchone(
                "SELECT id, blob_sha256, deleted_at FROM messages"
                " WHERE account_id = ? AND provider_id = ?",
                (account_id, provider_id),
            )
            if row is None:
                message_id = new_message_id()
                is_new = changed = True
                await tx.execute(
                    "INSERT INTO messages (id, account_id, provider_id, blob_sha256,"
                    " internal_date, first_seen_at) VALUES (?,?,?,?,?,?)",
                    (
                        message_id,
                        account_id,
                        provider_id,
                        sha,
                        fmt_ts(_aware(meta.internal_date)) if meta.internal_date else None,
                        stamp,
                    ),
                )
                await tx.execute(
                    "INSERT INTO message_state (message_id, is_read, is_starred, labels_json)"
                    " VALUES (?,?,?,?)",
                    (message_id, int(meta.is_read), int(meta.is_starred), json.dumps(meta.labels)),
                )
            else:
                message_id = MessageId(row["id"])
                is_new = False
                changed = row["blob_sha256"] != sha or row["deleted_at"] is not None
                if changed:
                    await tx.execute(
                        "UPDATE messages SET blob_sha256 = ?, deleted_at = NULL WHERE id = ?",
                        (sha, message_id),
                    )
        log.info(
            "message stored",
            message_id=message_id,
            account_id=account_id,
            sha256=sha,
            size=size,
            new=is_new,
            changed=changed,
            lfs=to_lfs,
        )
        if self.bus and changed:
            await self.bus.publish(
                MessageStored(
                    ts=now,
                    account_id=account_id,
                    message_id=message_id,
                    is_new=is_new,
                    changed=changed,
                )
            )
        if self.bus and to_lfs and changed:
            oversize = size > self.settings.max_message_bytes
            log.warning(
                "large message stored in LFS",
                message_id=message_id,
                size=size,
                oversize=oversize,
            )
            await self.bus.publish(
                LargeMessageStored(
                    ts=now,
                    account_id=account_id,
                    message_id=message_id,
                    size_bytes=size,
                    oversize=oversize,
                )
            )
        return PutResult(message_id, is_new, changed)

    async def get_message(self, message_id: MessageId) -> StoredMessage:
        row = await self.db.fetchone(
            "SELECT m.*, b.size_raw, b.codec FROM messages m"
            " JOIN blobs b ON b.sha256 = m.blob_sha256 WHERE m.id = ?",
            (message_id,),
        )
        if row is None:
            raise NotFound(f"no such message: {message_id}")
        return _stored(row)

    async def get_raw(self, message_id: MessageId, *, verify: bool = False) -> bytes:
        """The exact bytes that were stored. ``verify=True`` also re-hashes them against the
        blob's sha256 and raises ``StorageError`` on any mismatch."""
        row = await self._blob_for(message_id)
        raw = await self._read_blob(row)
        if verify and await _hash(raw) != row["sha256"]:
            raise StorageError(f"message {message_id} failed its sha256 check (corrupt)")
        return raw

    async def get_stub(self, message_id: MessageId) -> bytes:
        """The message's headers (up to the blank line, at most 1 MiB). For a large message this
        comes from the inline stub, without opening the LFS file."""
        row = await self._blob_for(message_id)
        if row["stub"] is not None:
            return decompress(row["stub"], _stub_size(row["stub"]))
        return header_block(await self._read_blob(row))

    async def mark_deleted(self, message_id: MessageId) -> bool:
        """Soft delete. False if it was already deleted."""
        stamp = fmt_ts(self.clock.now())
        async with self.db.transaction() as tx:
            if not await tx.fetchone("SELECT 1 FROM messages WHERE id = ?", (message_id,)):
                raise NotFound(f"no such message: {message_id}")
            return (
                await tx.execute(
                    "UPDATE messages SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
                    (stamp, message_id),
                )
                > 0
            )

    async def message_id_for(self, account_id: AccountId, provider_id: str) -> MessageId | None:
        """The message with this provider id (deleted or not), or None."""
        row = await self.db.fetchone(
            "SELECT id FROM messages WHERE account_id = ? AND provider_id = ?",
            (account_id, provider_id),
        )
        return MessageId(row["id"]) if row else None

    async def mark_deleted_by_provider_id(self, account_id: AccountId, provider_id: str) -> bool:
        """Soft delete by the provider's id. False if unknown or already deleted."""
        stamp = fmt_ts(self.clock.now())
        changed = await self.db.execute(
            "UPDATE messages SET deleted_at = ? WHERE account_id = ? AND provider_id = ?"
            " AND deleted_at IS NULL",
            (stamp, account_id, provider_id),
        )
        return changed > 0

    async def iter_messages(
        self,
        account_id: AccountId,
        *,
        since: datetime | None = None,
        include_deleted: bool = False,
        after_id: MessageId | None = None,
    ) -> AsyncIterator[StoredMessage]:
        """Messages first seen at or after ``since`` (all if None), oldest first, in pages. With
        ``after_id`` the walk resumes strictly after that message among those first seen at the
        same instant (a checkpoint is ``(first_seen_at, id)``)."""
        after_ts = fmt_ts(_aware(since)) if since else ""
        after_id = after_id or MessageId("")
        while True:
            rows = await self.db.fetchall(
                _ITER_SQL_ALL if include_deleted else _ITER_SQL_LIVE,
                (account_id, after_ts, after_ts, after_id, PAGE),
            )
            for r in rows:
                yield _stored(r)
            if len(rows) < PAGE:
                return
            after_ts, after_id = rows[-1]["first_seen_at"], rows[-1]["id"]

    async def count_messages(self, account_id: AccountId, *, since: datetime | None = None) -> int:
        """Live messages of an account (first seen at or after ``since``, if given)."""
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM messages WHERE account_id = ? AND deleted_at IS NULL"
            " AND first_seen_at >= ?",
            (account_id, fmt_ts(_aware(since)) if since else ""),
        )
        return int(row["n"]) if row else 0

    async def live_message_ids(self, ids: Sequence[str]) -> set[str]:
        """Which of ``ids`` are messages that exist and are not deleted (for pruning an index)."""
        found: set[str] = set()
        for i in range(0, len(ids), 500):
            part = list(ids[i : i + 500])
            marks = ",".join("?" * len(part))
            rows = await self.db.fetchall(
                f"SELECT id FROM messages WHERE deleted_at IS NULL AND id IN ({marks})",  # noqa: S608 (placeholders only)
                part,
            )
            found.update(r["id"] for r in rows)
        return found

    # -- sync cursors -----------------------------------------------------------------------
    async def get_cursor(self, account_id: AccountId, scope: str) -> str | None:
        row = await self.db.fetchone(
            "SELECT cursor FROM sync_cursors WHERE account_id = ? AND scope = ?",
            (account_id, scope),
        )
        return row["cursor"] if row else None

    async def set_cursor(self, account_id: AccountId, scope: str, cursor: str) -> None:
        async with self.db.transaction() as tx:
            if not await tx.fetchone("SELECT 1 FROM accounts WHERE id = ?", (account_id,)):
                raise NotFound(f"no such account: {account_id}")
            await tx.execute(
                "INSERT INTO sync_cursors (account_id, scope, cursor, updated_at) VALUES (?,?,?,?)"
                " ON CONFLICT(account_id, scope) DO UPDATE SET cursor = excluded.cursor,"
                " updated_at = excluded.updated_at",
                (account_id, scope, cursor, fmt_ts(self.clock.now())),
            )

    # -- housekeeping -----------------------------------------------------------------------
    async def store_id(self) -> str:
        """The random id of this raw store (migration 0006). A derived index records it so it is
        never attached to a different store."""
        row = await self.db.fetchone("SELECT value FROM raw_meta WHERE key = 'store_id'")
        if row is None:
            raise StorageError("raw.db has no store id; run the migrations")
        return str(row["value"])

    async def stats(self) -> StoreStats:
        row = await self.db.fetchone(
            "SELECT (SELECT COUNT(*) FROM accounts) AS accounts,"
            " (SELECT COUNT(*) FROM messages) AS messages,"
            " (SELECT COUNT(*) FROM messages WHERE deleted_at IS NOT NULL) AS deleted,"
            " COUNT(*) AS blobs, COALESCE(SUM(codec = 'zstd-file'), 0) AS lfs,"
            " COALESCE(SUM(size_raw), 0) AS raw, COALESCE(SUM(size_stored), 0) AS stored"
            " FROM blobs"
        )
        assert row is not None  # noqa: S101 (an aggregate always yields a row)
        return StoreStats(
            accounts=row["accounts"],
            messages=row["messages"],
            deleted_messages=row["deleted"],
            blobs=row["blobs"],
            lfs_blobs=row["lfs"],
            bytes_raw=row["raw"],
            bytes_stored=row["stored"],
        )

    async def gc_blobs(self) -> GcResult:
        """Remove blobs no message points at (including soft-deleted messages' blobs, which stay
        referenced until the message row itself is gone), their LFS files, and stray LFS files
        older than an hour. A referenced blob is never removed."""
        async with self.db.transaction() as tx:
            dead = await tx.fetchall(
                "SELECT sha256, size_stored, lfs_path FROM blobs WHERE sha256 NOT IN"
                " (SELECT blob_sha256 FROM messages)"
            )
            if dead:
                await tx.executemany(
                    "DELETE FROM blobs WHERE sha256 = ?", [(r["sha256"],) for r in dead]
                )
        freed = sum(r["size_stored"] for r in dead)
        paths = [self.lfs_dir / r["lfs_path"] for r in dead if r["lfs_path"]]
        removed = await asyncio.to_thread(_unlink_all, paths)
        known = {
            r["lfs_path"]
            for r in await self.db.fetchall("SELECT lfs_path FROM blobs WHERE lfs_path IS NOT NULL")
        }
        removed += await asyncio.to_thread(_sweep_orphans, self.lfs_dir, known)
        result = GcResult(len(dead), freed, removed)
        log.info("blob gc", **result.__dict__)
        return result

    # -- internals --------------------------------------------------------------------------
    async def _blob_for(self, message_id: MessageId) -> Row:
        row = await self.db.fetchone(
            "SELECT b.* FROM messages m JOIN blobs b ON b.sha256 = m.blob_sha256 WHERE m.id = ?",
            (message_id,),
        )
        if row is None:
            raise NotFound(f"no such message: {message_id}")
        return row

    async def _read_blob(self, row: Row) -> bytes:
        if row["codec"] == "zstd":
            frame: bytes = row["data"]
        else:
            path = self.lfs_dir / row["lfs_path"]
            try:
                frame = await asyncio.to_thread(path.read_bytes)
            except OSError as exc:
                raise StorageError(f"LFS file missing or unreadable for {row['sha256']}") from exc
        if len(frame) > OFF_THREAD_BYTES:
            return await asyncio.to_thread(decompress, frame, row["size_raw"])
        return decompress(frame, row["size_raw"])


async def _hash(raw: bytes) -> str:
    return await asyncio.to_thread(_sha256, raw) if len(raw) > OFF_THREAD_BYTES else _sha256(raw)


def _stub_size(frame: bytes) -> int:
    """The content size recorded in a stub frame (stubs are written with it)."""
    params = zstandard.get_frame_parameters(frame)
    return int(params.content_size)


def _unlink_all(paths: list[Path]) -> int:
    n = 0
    for p in paths:
        try:
            p.unlink()
            n += 1
        except FileNotFoundError:
            pass
    return n


def _sweep_orphans(lfs_dir: Path, known: set[str]) -> int:
    """Delete LFS files no blob row names (and stale temp files), once they are old enough."""
    if not lfs_dir.is_dir():
        return 0
    cutoff = time.time() - ORPHAN_GRACE_SECONDS
    n = 0
    for f in lfs_dir.rglob("*"):
        if not f.is_file() or f.stat().st_mtime > cutoff:
            continue
        if f.relative_to(lfs_dir).as_posix() not in known:
            f.unlink(missing_ok=True)
            n += 1
    return n


def _account(r: Row) -> Account:
    return Account(
        AccountId(r["id"]),
        r["provider"],
        r["display_name"],
        parse_ts(r["created_at"]),
        json.loads(r["config_json"]),
    )


def _stored(r: Row) -> StoredMessage:
    return StoredMessage(
        id=MessageId(r["id"]),
        account_id=AccountId(r["account_id"]),
        provider_id=r["provider_id"],
        blob_sha256=r["blob_sha256"],
        internal_date=parse_ts(r["internal_date"]) if r["internal_date"] else None,
        first_seen_at=parse_ts(r["first_seen_at"]),
        deleted_at=parse_ts(r["deleted_at"]) if r["deleted_at"] else None,
        size_raw=r["size_raw"],
        in_lfs=r["codec"] == "zstd-file",
    )
