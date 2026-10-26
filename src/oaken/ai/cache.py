# SPDX-License-Identifier: Apache-2.0
"""``llm_cache`` in ``raw.db``: a validated model answer for a (workspace, task, content, model,
prompt version). The same question is never asked twice. Results can hold text derived from mail,
so the table is as sensitive as the rest of ``raw.db``."""

from oaken.core.clock import Clock, fmt_ts, parse_ts
from oaken.core.db import Database
from oaken.plugins.contracts import CacheEntry, CacheStats

__all__ = ["CacheEntry", "LLMCache"]


class LLMCache:
    """Core's own cache, the default: it works on ``raw.db``, which a plugin never sees."""

    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    def key(
        self, *, workspace: str, task: str, content_hash: str, model: str, prompt_version: str
    ) -> tuple[str, ...]:
        return (workspace, task, content_hash, model, prompt_version)

    async def get(self, key: tuple[str, ...]) -> CacheEntry | None:
        workspace, task, content_hash, model_id, prompt_version = key
        row = await self.db.fetchone(
            "SELECT result_json, tokens_in, tokens_out FROM llm_cache WHERE workspace = ? AND"
            " task = ? AND content_hash = ? AND model_id = ? AND prompt_version = ?",
            (workspace, task, content_hash, model_id, prompt_version),
        )
        if row is None:
            return None
        return CacheEntry(row["result_json"], int(row["tokens_in"]), int(row["tokens_out"]))

    async def put(self, key: tuple[str, ...], entry: CacheEntry) -> None:
        workspace, task, content_hash, model_id, prompt_version = key
        await self.db.execute(
            "INSERT OR REPLACE INTO llm_cache (workspace, task, content_hash, model_id,"
            " prompt_version, result_json, tokens_in, tokens_out, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                workspace,
                task,
                content_hash,
                model_id,
                prompt_version,
                entry.result_json,
                entry.tokens_in,
                entry.tokens_out,
                fmt_ts(self.clock.now()),
            ),
        )

    async def stats(self, workspace: str | None = None) -> CacheStats:
        args = (workspace, workspace)
        total = await self.db.fetchone(
            "SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(result_json)), 0) AS b,"
            " MIN(created_at) AS oldest FROM llm_cache WHERE (? IS NULL OR workspace = ?)",
            args,
        )
        by_task = {
            r["task"]: int(r["n"])
            for r in await self.db.fetchall(
                "SELECT task, COUNT(*) AS n FROM llm_cache WHERE (? IS NULL OR workspace = ?)"
                " GROUP BY task",
                args,
            )
        }
        versions = tuple(
            r["prompt_version"]
            for r in await self.db.fetchall(
                "SELECT DISTINCT prompt_version FROM llm_cache WHERE (? IS NULL OR workspace = ?)"
                " ORDER BY prompt_version",
                args,
            )
        )
        oldest = total["oldest"] if total else None
        return CacheStats(
            entries=int(total["n"]),  # type: ignore[index]
            bytes=int(total["b"]),  # type: ignore[index]
            by_task=by_task,
            prompt_versions=versions,
            oldest=parse_ts(oldest) if oldest else None,
        )

    async def clear(self, workspace: str | None = None, task: str | None = None) -> int:
        """Forget cached answers (one workspace's and/or one task's, or all). Returns how many."""
        return await self.db.execute(
            "DELETE FROM llm_cache WHERE (? IS NULL OR workspace = ?) AND (? IS NULL OR task = ?)",
            (workspace, workspace, task, task),
        )
