# SPDX-License-Identifier: Apache-2.0
"""Token accounting: every model call, and every call a cache made unnecessary, in ``raw.db``.

This is how the token-thrift claim is proved. A row is one call to a model, or (``cached``) one
call that was skipped because the same ``(content_hash, model)`` already had a result; for a
cached row the tokens are what the call would have cost. Rows never hold message text.
"""

from dataclasses import dataclass
from datetime import datetime

from oaken.core.clock import Clock, fmt_ts
from oaken.core.db import Database
from oaken.core.ids import AccountId, MessageId


@dataclass(frozen=True)
class UsageRow:
    stage: str
    model_id: str
    calls: int  # calls actually made
    cached_calls: int  # calls skipped
    tokens_in: int  # spent
    tokens_out: int
    tokens_saved: int  # what the skipped calls would have cost (in + out)


class UsageStore:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    async def record(
        self,
        stage: str,
        model_id: str,
        *,
        tokens_in: int,
        tokens_out: int = 0,
        cached: bool = False,
        account_id: AccountId | None = None,
        message_id: MessageId | None = None,
    ) -> None:
        await self.db.execute(
            "INSERT INTO ai_usage (ts, stage, model_id, tokens_in, tokens_out, cached, account_id,"
            " message_id) VALUES (?,?,?,?,?,?,?,?)",
            (
                fmt_ts(self.clock.now()),
                stage,
                model_id,
                max(tokens_in, 0),
                max(tokens_out, 0),
                int(cached),
                account_id,
                message_id,
            ),
        )

    async def usage_summary(self, since: datetime | None = None) -> list[UsageRow]:
        """Per stage and model: calls made, calls saved by the cache, and the tokens of each."""
        rows = await self.db.fetchall(
            "SELECT stage, model_id,"
            " SUM(cached = 0) AS calls, SUM(cached = 1) AS cached_calls,"
            " COALESCE(SUM(CASE WHEN cached = 0 THEN tokens_in END), 0) AS tin,"
            " COALESCE(SUM(CASE WHEN cached = 0 THEN tokens_out END), 0) AS tout,"
            " COALESCE(SUM(CASE WHEN cached = 1 THEN tokens_in + tokens_out END), 0) AS saved"
            " FROM ai_usage WHERE ts >= ? GROUP BY stage, model_id ORDER BY stage, model_id",
            (fmt_ts(since) if since else "",),
        )
        return [
            UsageRow(
                r["stage"],
                r["model_id"],
                int(r["calls"]),
                int(r["cached_calls"]),
                int(r["tin"]),
                int(r["tout"]),
                int(r["saved"]),
            )
            for r in rows
        ]
