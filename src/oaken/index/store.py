# SPDX-License-Identifier: Apache-2.0
"""``index.db``: parsed fields, cleaned text, chunks and the FTS5 keyword index.

Everything here is derived from ``raw.db`` and safe to delete. ``IndexStore.open`` ties the file
to one raw store (a random id kept in ``raw.db``) and refuses any other. Indexing is idempotent:
a message whose content hash did not change writes nothing, and a changed one rewrites only the
chunks whose own text changed.
"""

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from oaken.core.clock import Clock, fmt_ts, parse_ts
from oaken.core.db import Database, Tx, migrate
from oaken.core.errors import IndexMismatch, StorageError
from oaken.core.ids import AccountId, MessageId
from oaken.core.logging import get_logger
from oaken.core.settings import AISettings, IndexSettings
from oaken.index.analyze import Analysis
from oaken.index.query import DISPOSITIONS, ParsedQuery, parse_query
from oaken.mail.chunk import Chunk, chunk_text
from oaken.mail.parse import Address, ParsedMessage
from oaken.mail.tokens import EstimateTokenizer, Tokenizer

log = get_logger("oaken.index")

# Bump when parsing, cleaning or chunking changes what a message turns into. A different value in
# an existing index marks it stale and a rebuild replaces it.
PIPELINE_VERSION: Final = 1
MIGRATIONS: Final = Path(__file__).resolve().parent / "migrations"
HL_OPEN: Final = "\x02"  # marks the start and end of a match in a snippet; the cleaner strips
HL_CLOSE: Final = "\x03"  # control characters, so mail text cannot contain them
SNIPPET_TOKENS: Final = 16
SNIPPET_FALLBACK_CHARS: Final = 160
# bm25 column weights: a match in the subject counts most, then the sender, then the text.
_WEIGHTS: Final = "6.0, 3.0, 1.0"


@dataclass(frozen=True, slots=True)
class IndexResult:
    message_id: MessageId
    changed: bool  # False: the content hash matched and nothing but, maybe, organization changed
    created: bool
    chunks_written: int
    chunks_deleted: int
    chunks_kept: int
    thread_key: str
    joined: bool  # the thread key came from the subject join, not from the headers


@dataclass(slots=True)
class Counters:
    docs_written: int = 0
    chunks_written: int = 0
    chunks_deleted: int = 0


@dataclass(frozen=True, slots=True)
class SearchFilters:
    account_id: AccountId | None = None
    from_: tuple[str, ...] = ()
    after: datetime | None = None
    before: datetime | None = None
    thread_key: str | None = None
    has_attachments: bool | None = None
    list_id: str | None = None
    labels: tuple[str, ...] = ()
    disposition: str | None = None  # None hides discarded mail; "any" shows everything
    include_discarded: bool = False


@dataclass(frozen=True, slots=True)
class SearchHit:
    message_id: MessageId
    account_id: AccountId
    thread_key: str
    subject: str
    from_addr: str
    from_name: str
    date: datetime | None
    score: float  # higher is better; 0 for a listing with no words to match
    snippet: str  # matches wrapped in HL_OPEN / HL_CLOSE
    labels: tuple[str, ...]
    disposition: str
    has_attachments: bool


@dataclass(frozen=True, slots=True)
class SearchResult:
    hits: tuple[SearchHit, ...]
    query: ParsedQuery
    warnings: tuple[str, ...] = field(default=())


@dataclass(frozen=True, slots=True)
class IndexStats:
    docs: int
    chunks: int
    bytes: int
    pipeline_version: int
    raw_store_id: str
    created_at: datetime
    stale: bool


def render_snippet(snippet: str, open_: str = "**", close: str = "**") -> str:
    """The snippet with its match markers replaced (for a terminal or for HTML after escaping)."""
    return snippet.replace(HL_OPEN, open_).replace(HL_CLOSE, close)


def _sender(addrs: Sequence[Address]) -> tuple[str, str, str]:
    """``(address, name, "Name <address>")`` of the first From."""
    if not addrs:
        return "", "", ""
    a = addrs[0]
    return a.address, a.name, (f"{a.name} <{a.address}>" if a.name else a.address)


def _like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class IndexStore:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        *,
        raw_store_id: str,
        ai: AISettings | None = None,
        index: IndexSettings | None = None,
        tokenizer: Tokenizer | None = None,
        stale: bool = False,
    ) -> None:
        self.db = db
        self.clock = clock
        self.raw_store_id = raw_store_id
        self.ai = ai or AISettings()
        self.cfg = index or IndexSettings()
        self.tokenizer = tokenizer or EstimateTokenizer()
        self.stale = stale
        self.counters = Counters()

    # -- lifecycle --------------------------------------------------------------------------
    @classmethod
    async def open(
        cls,
        path: Path,
        clock: Clock,
        *,
        raw_store_id: str,
        ai: AISettings | None = None,
        index: IndexSettings | None = None,
        tokenizer: Tokenizer | None = None,
        read_pool_size: int = 4,
    ) -> "IndexStore":
        """Open or create ``index.db`` for the raw store ``raw_store_id``. Raises IndexMismatch if
        the file was built from another raw store. An older ``pipeline_version`` opens fine but
        sets ``stale``: ``rebuild_index`` then starts it over."""
        db = await Database.open(path, read_pool_size=read_pool_size)
        try:
            await migrate(db, MIGRATIONS)
            stale = await cls._claim(db, clock, raw_store_id)
        except BaseException:
            await db.close()
            raise
        return cls(
            db,
            clock,
            raw_store_id=raw_store_id,
            ai=ai,
            index=index,
            tokenizer=tokenizer,
            stale=stale,
        )

    @staticmethod
    async def _claim(db: Database, clock: Clock, raw_store_id: str) -> bool:
        async with db.transaction() as tx:
            rows = await tx.fetchall("SELECT key, value FROM index_meta")
            meta = {r["key"]: r["value"] for r in rows}
            if not meta:
                await tx.executemany(
                    "INSERT INTO index_meta (key, value) VALUES (?, ?)",
                    [
                        ("raw_store_id", raw_store_id),
                        ("pipeline_version", str(PIPELINE_VERSION)),
                        ("created_at", fmt_ts(clock.now())),
                    ],
                )
                return False
            if meta.get("raw_store_id") != raw_store_id:
                raise IndexMismatch(
                    "index.db was built from a different raw store; delete it and rebuild"
                    " (it is derived, nothing is lost)"
                )
            return int(meta.get("pipeline_version", "0")) != PIPELINE_VERSION

    async def close(self) -> None:
        await self.db.close()

    async def reset(self) -> None:
        """Forget every document (a stale index is replaced this way) and record the current
        ``pipeline_version``. The raw store link is kept."""
        async with self.db.transaction() as tx:
            await tx.execute("DELETE FROM docs")  # chunks, labels and FTS rows follow
            await tx.execute(
                "UPDATE index_meta SET value = ? WHERE key = 'pipeline_version'",
                (str(PIPELINE_VERSION),),
            )
            await tx.execute("DELETE FROM index_meta WHERE key LIKE 'rebuild_cursor:%'")
        self.stale = False

    # -- indexing ---------------------------------------------------------------------------
    def _chunks(self, a: Analysis) -> list[tuple[str, str, str, Chunk]]:
        """``(source, subject, sender, chunk)`` for the message and each message forwarded in it,
        numbered in one run."""
        out: list[tuple[str, str, str, Chunk]] = []
        sources: list[tuple[str, ParsedMessage, str]] = [("main", a.parsed, a.cleaned.cleaned)]
        sources += [(f"fwd:{i}", p, c.cleaned) for i, (p, c) in enumerate(a.forwarded, start=1)]
        for source, parsed, text in sources:
            _, _, sender = _sender(parsed.from_)
            pieces = chunk_text(
                text,
                target=self.ai.chunk_tokens,
                overlap=self.ai.chunk_overlap,
                tokenizer=self.tokenizer,
            )
            for c in pieces:
                out.append((source, parsed.subject, sender, c))
        if not out:
            # No body text: one empty chunk still carries the subject and sender, so the message
            # can be found by them.
            _, _, sender = _sender(a.parsed.from_)
            out.append(
                ("main", a.parsed.subject, sender, Chunk(0, "", 0, hashlib.sha256(b"").hexdigest()))
            )
        return [
            (src, subj, snd, Chunk(i, c.text, c.tokens, c.content_hash))
            for i, (src, subj, snd, c) in enumerate(out)
        ]

    async def _join(
        self, tx: Tx, message_id: str, a: Analysis, when: datetime | None
    ) -> tuple[str, bool]:
        """The thread key, after joining a reference-less reply to a recent same-subject thread."""
        info = a.thread
        days = self.cfg.subject_join_days
        if not (info.basis == "subject" and info.is_reply and days > 0 and when is not None):
            return info.key, False
        floor = fmt_ts(when - timedelta(days=days))
        row = await tx.fetchone(
            "SELECT thread_key FROM docs WHERE subject_key = ? AND message_id != ?"
            " AND date_utc IS NOT NULL AND date_utc >= ? AND date_utc < ?"
            " ORDER BY date_utc DESC, message_id LIMIT 1",
            (info.subject_key, message_id, floor, fmt_ts(when)),
        )
        if row is None or row["thread_key"] == info.key:
            return info.key, False
        return str(row["thread_key"]), True

    async def index_message(
        self,
        message_id: MessageId,
        account_id: AccountId,
        analysis: Analysis,
        *,
        disposition: str = "inbox",
        labels: Sequence[str] = (),
        force: bool = False,
    ) -> IndexResult:
        """Index one message. Idempotent: with the same content hash nothing but the organization
        (disposition and labels) is touched, and no chunk is written."""
        if disposition not in DISPOSITIONS:
            raise ValueError(f"unknown disposition {disposition!r}")
        a = analysis
        p = a.parsed
        when = p.date or a.fallback_date
        from_addr, from_name, _ = _sender(p.from_)
        chunks = self._chunks(a)
        attachments = [
            {
                "filename": x.filename,
                "content_type": x.content_type,
                "size": x.size,
                "inline": x.inline,
                "quarantine": x.quarantine,
            }
            for x in p.attachments
        ]
        to = [{"name": x.name, "address": x.address} for x in p.to]
        warnings = [*p.warnings, *a.cleaned.warnings]
        label_set = sorted(set(labels))
        async with self.db.transaction() as tx:
            thread_key, joined = await self._join(tx, message_id, a, when)
            digest = _doc_hash(
                self.ai,
                account_id,
                thread_key,
                a.thread.subject_key,
                p.subject,
                from_addr,
                from_name,
                to,
                when,
                p.list_id,
                attachments,
                a.cleaned.cleaned,
                [(s, subj, snd, c.content_hash) for s, subj, snd, c in chunks],
                warnings,
            )
            row = await tx.fetchone(
                "SELECT content_hash FROM docs WHERE message_id = ?", (message_id,)
            )
            created = row is None
            if row is not None and row["content_hash"] == digest and not force:
                await self._set_organization(tx, message_id, disposition, label_set)
                return IndexResult(message_id, False, False, 0, 0, len(chunks), thread_key, joined)
            await tx.execute(
                "INSERT INTO docs (message_id, account_id, thread_key, subject_key, subject,"
                " from_addr, from_name, to_json, date_utc, list_id, has_attachments,"
                " attachments_json, cleaned_text, content_hash, parse_warnings_json,"
                " disposition, indexed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT (message_id) DO UPDATE SET account_id = excluded.account_id,"
                " thread_key = excluded.thread_key, subject_key = excluded.subject_key,"
                " subject = excluded.subject, from_addr = excluded.from_addr,"
                " from_name = excluded.from_name, to_json = excluded.to_json,"
                " date_utc = excluded.date_utc, list_id = excluded.list_id,"
                " has_attachments = excluded.has_attachments,"
                " attachments_json = excluded.attachments_json,"
                " cleaned_text = excluded.cleaned_text, content_hash = excluded.content_hash,"
                " parse_warnings_json = excluded.parse_warnings_json,"
                " disposition = excluded.disposition, indexed_at = excluded.indexed_at",
                (
                    message_id,
                    account_id,
                    thread_key,
                    a.thread.subject_key,
                    p.subject,
                    from_addr,
                    from_name,
                    json.dumps(to),
                    fmt_ts(when) if when else None,
                    p.list_id,
                    int(bool(p.attachments)),
                    json.dumps(attachments),
                    a.cleaned.cleaned,
                    digest,
                    json.dumps(warnings),
                    disposition,
                    fmt_ts(self.clock.now()),
                ),
            )
            await self._set_labels(tx, message_id, label_set)
            written, deleted, kept = await self._sync_chunks(tx, message_id, chunks)
        self.counters.docs_written += 1
        self.counters.chunks_written += written
        self.counters.chunks_deleted += deleted
        return IndexResult(message_id, True, created, written, deleted, kept, thread_key, joined)

    async def _sync_chunks(
        self, tx: Tx, message_id: str, chunks: list[tuple[str, str, str, Chunk]]
    ) -> tuple[int, int, int]:
        rows = await tx.fetchall(
            "SELECT id, ordinal, source, subject, sender, content_hash FROM chunks"
            " WHERE message_id = ?",
            (message_id,),
        )
        have = {r["ordinal"]: r for r in rows}
        written = kept = 0
        for source, subject, sender, c in chunks:
            old = have.pop(c.ordinal, None)
            if old is not None and (
                old["content_hash"],
                old["source"],
                old["subject"],
                old["sender"],
            ) == (c.content_hash, source, subject, sender):
                kept += 1
                continue
            if old is None:
                await tx.execute(
                    "INSERT INTO chunks (message_id, ordinal, source, subject, sender, text,"
                    " token_estimate, content_hash) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        message_id,
                        c.ordinal,
                        source,
                        subject,
                        sender,
                        c.text,
                        c.tokens,
                        c.content_hash,
                    ),
                )
            else:
                await tx.execute(
                    "UPDATE chunks SET source = ?, subject = ?, sender = ?, text = ?,"
                    " token_estimate = ?, content_hash = ? WHERE id = ?",
                    (source, subject, sender, c.text, c.tokens, c.content_hash, old["id"]),
                )
            written += 1
        for old in have.values():
            await tx.execute("DELETE FROM chunks WHERE id = ?", (old["id"],))
        return written, len(have), kept

    # -- organization (from raw.db's message_attrs) -----------------------------------------
    @staticmethod
    async def _set_labels(tx: Tx, message_id: str, labels: list[str]) -> None:
        await tx.execute("DELETE FROM doc_labels WHERE message_id = ?", (message_id,))
        await tx.executemany(
            "INSERT INTO doc_labels (message_id, label) VALUES (?, ?)",
            [(message_id, x) for x in labels],
        )

    async def _set_organization(
        self, tx: Tx, message_id: str, disposition: str, labels: list[str]
    ) -> bool:
        changed = await tx.execute(
            "UPDATE docs SET disposition = ? WHERE message_id = ? AND disposition != ?",
            (disposition, message_id, disposition),
        )
        current = [
            r["label"]
            for r in await tx.fetchall(
                "SELECT label FROM doc_labels WHERE message_id = ? ORDER BY label", (message_id,)
            )
        ]
        if current != labels:
            await self._set_labels(tx, message_id, labels)
            changed = 1
        return changed > 0

    async def set_organization(
        self, message_id: MessageId, disposition: str, labels: Sequence[str]
    ) -> bool:
        """Refresh a message's disposition and labels (after Sieve ran). False if the message is
        not indexed (yet); the indexer passes the current values when it gets there."""
        if disposition not in DISPOSITIONS:
            raise ValueError(f"unknown disposition {disposition!r}")
        async with self.db.transaction() as tx:
            if not await tx.fetchone("SELECT 1 FROM docs WHERE message_id = ?", (message_id,)):
                return False
            await self._set_organization(tx, message_id, disposition, sorted(set(labels)))
            return True

    async def delete_message(self, message_id: MessageId) -> bool:
        async with self.db.transaction() as tx:
            n = await tx.execute("DELETE FROM docs WHERE message_id = ?", (message_id,))
        return n > 0

    # -- reading ----------------------------------------------------------------------------
    async def has(self, message_id: MessageId) -> bool:
        return (
            await self.db.fetchone("SELECT 1 FROM docs WHERE message_id = ?", (message_id,))
        ) is not None

    async def doc_ids(
        self, *, after: str = "", limit: int = 500, account_id: AccountId | None = None
    ) -> list[str]:
        rows = await self.db.fetchall(
            "SELECT message_id FROM docs WHERE message_id > ? AND (? IS NULL OR account_id = ?)"
            " ORDER BY message_id LIMIT ?",
            (after, account_id, account_id, limit),
        )
        return [r["message_id"] for r in rows]

    async def chunk_rows(self, message_id: MessageId) -> list[sqlite3.Row]:
        return await self.db.fetchall(
            "SELECT id, ordinal, source, subject, sender, text, token_estimate, content_hash"
            " FROM chunks WHERE message_id = ? ORDER BY ordinal",
            (message_id,),
        )

    async def get_meta(self, key: str) -> str | None:
        row = await self.db.fetchone("SELECT value FROM index_meta WHERE key = ?", (key,))
        return None if row is None else str(row["value"])

    async def set_meta(self, key: str, value: str | None) -> None:
        if value is None:
            await self.db.execute("DELETE FROM index_meta WHERE key = ?", (key,))
        else:
            await self.db.execute(
                "INSERT INTO index_meta (key, value) VALUES (?, ?)"
                " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    async def stats(self) -> IndexStats:
        row = await self.db.fetchone(
            "SELECT (SELECT COUNT(*) FROM docs) AS docs, (SELECT COUNT(*) FROM chunks) AS chunks,"
            " (SELECT page_count * page_size FROM pragma_page_count, pragma_page_size) AS bytes"
        )
        assert row is not None  # noqa: S101 (an aggregate always yields a row)
        created = await self.get_meta("created_at")
        return IndexStats(
            docs=row["docs"],
            chunks=row["chunks"],
            bytes=row["bytes"],
            pipeline_version=int(await self.get_meta("pipeline_version") or 0),
            raw_store_id=self.raw_store_id,
            created_at=parse_ts(created) if created else self.clock.now(),
            stale=self.stale,
        )

    # -- search -----------------------------------------------------------------------------
    def _where(self, q: ParsedQuery, f: SearchFilters) -> tuple[str, list[object], bool]:
        """``(sql, args, explicit)``; ``explicit`` is False when only the default (hide discarded)
        applies, so a query with no words and no filters can tell it has nothing to list."""
        clauses: list[str] = []
        args: list[object] = []
        if f.account_id:
            clauses.append("d.account_id = ?")
            args.append(f.account_id)
        for v in (*q.from_, *f.from_):
            clauses.append("(d.from_addr LIKE ? ESCAPE '\\' OR d.from_name LIKE ? ESCAPE '\\')")
            args += [_like(v), _like(v)]
        after = max((x for x in (q.after, f.after) if x), default=None)
        before = min((x for x in (q.before, f.before) if x), default=None)
        if after:
            clauses.append("d.date_utc >= ?")
            args.append(fmt_ts(after))
        if before:
            clauses.append("d.date_utc < ?")
            args.append(fmt_ts(before))
        if f.thread_key:
            clauses.append("d.thread_key = ?")
            args.append(f.thread_key)
        if f.has_attachments is not None:
            clauses.append("d.has_attachments = ?")
            args.append(int(f.has_attachments))
        if f.list_id:
            clauses.append("d.list_id = ?")
            args.append(f.list_id)
        for label in (*q.labels, *f.labels):
            clauses.append(
                "EXISTS (SELECT 1 FROM doc_labels l WHERE l.message_id = d.message_id"
                " AND l.label = ?)"
            )
            args.append(label)
        explicit = bool(clauses) or bool(q.disposition or f.disposition or f.include_discarded)
        disposition = q.disposition or f.disposition
        if disposition in DISPOSITIONS:
            clauses.append("d.disposition = ?")
            args.append(disposition)
        elif disposition != "any" and not f.include_discarded:
            clauses.append("d.disposition != 'discarded'")
        return (" AND " + " AND ".join(clauses)) if clauses else "", args, explicit

    async def keyword_search(
        self, query: str, filters: SearchFilters | None = None, k: int = 20
    ) -> SearchResult:
        """BM25-ranked search, one hit per message (its best chunk) with a highlighted snippet.
        With no words in the query but some filters it lists the newest matching messages."""
        f = filters or SearchFilters()
        q = parse_query(query, self.cfg)
        k = max(1, min(k, 1000))
        where, args, explicit = self._where(q, f)
        if q.match is None:
            if not explicit:
                return SearchResult((), q, q.warnings)
            listing_sql = (
                f"SELECT d.* FROM docs d WHERE 1 = 1{where}"  # noqa: S608 (placeholders only)
                " ORDER BY d.date_utc DESC, d.message_id LIMIT ?"
            )
            rows = await self.db.fetchall(listing_sql, [*args, k])
            hits = [
                await self._hit(r, 0.0, r["cleaned_text"][:SNIPPET_FALLBACK_CHARS]) for r in rows
            ]
            return SearchResult(tuple(hits), q, q.warnings)
        try:
            ranked_sql = (
                "WITH hits AS (SELECT c.id AS chunk_id, c.message_id AS message_id,"  # noqa: S608
                f" bm25(fts, {_WEIGHTS}) AS score FROM fts JOIN chunks c ON c.id = fts.rowid"
                " JOIN docs d ON d.message_id = c.message_id WHERE fts MATCH ?"
                f"{where}),"
                " ranked AS (SELECT chunk_id, message_id, score, ROW_NUMBER() OVER ("
                " PARTITION BY message_id ORDER BY score, chunk_id) AS rn FROM hits)"
                " SELECT chunk_id, message_id, score FROM ranked WHERE rn = 1"
                " ORDER BY score, message_id LIMIT ?"
            )
            best = await self.db.fetchall(ranked_sql, [q.match, *args, k])
        except StorageError as exc:  # the parser should make this impossible; never leak it
            log.warning("search expression rejected", error=exc.message[:120])
            return SearchResult((), q, (*q.warnings, "the search could not be run"))
        if not best:
            return SearchResult((), q, q.warnings)
        marks = ",".join("?" * len(best))
        snippets = {
            r["rowid"]: r["s"]
            for r in await self.db.fetchall(
                f"SELECT rowid, snippet(fts, 2, ?, ?, '…', {SNIPPET_TOKENS}) AS s FROM fts"  # noqa: S608
                f" WHERE fts MATCH ? AND rowid IN ({marks})",
                [HL_OPEN, HL_CLOSE, q.match, *[b["chunk_id"] for b in best]],
            )
        }
        docs = {
            r["message_id"]: r
            for r in await self.db.fetchall(
                f"SELECT d.* FROM docs d WHERE d.message_id IN ({','.join('?' * len(best))})",  # noqa: S608
                [b["message_id"] for b in best],
            )
        }
        texts = {
            r["id"]: r["text"]
            for r in await self.db.fetchall(
                f"SELECT id, text FROM chunks WHERE id IN ({marks})",  # noqa: S608
                [b["chunk_id"] for b in best],
            )
        }
        hits = []
        for b in best:
            snip = snippets.get(b["chunk_id"]) or ""
            if HL_OPEN not in snip:  # matched in the subject or sender only
                snip = (texts.get(b["chunk_id"]) or "")[:SNIPPET_FALLBACK_CHARS]
            hits.append(await self._hit(docs[b["message_id"]], -float(b["score"]), snip))
        return SearchResult(tuple(hits), q, q.warnings)

    async def _hit(self, d: sqlite3.Row, score: float, snippet: str) -> SearchHit:
        labels = await self.db.fetchall(
            "SELECT label FROM doc_labels WHERE message_id = ? ORDER BY label", (d["message_id"],)
        )
        return SearchHit(
            message_id=MessageId(d["message_id"]),
            account_id=AccountId(d["account_id"]),
            thread_key=d["thread_key"],
            subject=d["subject"],
            from_addr=d["from_addr"],
            from_name=d["from_name"],
            date=parse_ts(d["date_utc"]) if d["date_utc"] else None,
            score=score,
            snippet=snippet,
            labels=tuple(r["label"] for r in labels),
            disposition=d["disposition"],
            has_attachments=bool(d["has_attachments"]),
        )

    # -- integrity --------------------------------------------------------------------------
    async def check(self) -> list[str]:
        """Problems found in the FTS index (empty means consistent). Needs no raw store."""
        problems: list[str] = []
        try:
            async with self.db.transaction() as tx:
                await tx.execute("INSERT INTO fts (fts, rank) VALUES ('integrity-check', 1)")
        except StorageError as exc:
            problems.append(f"fts integrity check failed: {exc.message}")
        return problems

    async def rebuild_fts(self) -> None:
        """Rebuild the FTS index from ``chunks`` (cheap, and needs no mail)."""
        async with self.db.transaction() as tx:
            await tx.execute("INSERT INTO fts (fts) VALUES ('rebuild')")


def _doc_hash(ai: AISettings, *parts: object) -> str:
    payload = json.dumps(
        [PIPELINE_VERSION, ai.chunk_tokens, ai.chunk_overlap, *parts],
        default=lambda o: fmt_ts(o) if isinstance(o, datetime) else str(o),
        sort_keys=True,
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()
