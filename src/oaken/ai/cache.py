# SPDX-License-Identifier: Apache-2.0
"""``llm_cache`` in ``raw.db``: a validated model answer for a (workspace, task, content, model,
prompt version). The same question is never asked twice. Results can hold text derived from mail,
so the table is as sensitive as the rest of ``raw.db``."""

from dataclasses import dataclass

from oaken.core.clock import Clock, fmt_ts
from oaken.core.db import Database


@dataclass(frozen=True)
class CacheEntry:
    result_json: str
    tokens_in: int
    tokens_out: int


class LLMCache:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    async def get(
        self, workspace: str, task: str, content_hash: str, model_id: str, prompt_version: str
    ) -> CacheEntry | None:
        row = await self.db.fetchone(
            "SELECT result_json, tokens_in, tokens_out FROM llm_cache WHERE workspace = ? AND"
            " task = ? AND content_hash = ? AND model_id = ? AND prompt_version = ?",
            (workspace, task, content_hash, model_id, prompt_version),
        )
        if row is None:
            return None
        return CacheEntry(row["result_json"], int(row["tokens_in"]), int(row["tokens_out"]))

    async def put(
        self,
        workspace: str,
        task: str,
        content_hash: str,
        model_id: str,
        prompt_version: str,
        entry: CacheEntry,
    ) -> None:
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

    async def clear(self, workspace: str | None = None) -> int:
        """Forget cached answers (one workspace's, or all). Returns how many."""
        return await self.db.execute(
            "DELETE FROM llm_cache WHERE (? IS NULL OR workspace = ?)", (workspace, workspace)
        )
