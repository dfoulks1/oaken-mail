-- Token savings across the board.
--
-- ai_usage.caller: which door the call came in through (cli | ui | mcp | internal), so the cache's
-- savings can be split by caller. Rows from before this column read ''.
ALTER TABLE ai_usage ADD COLUMN caller TEXT NOT NULL DEFAULT '';

-- context_savings: one row per read operation that handed a client mail content. tokens_returned
-- is what the reply cost the client; tokens_full is what reading every message it names in full
-- would have cost. The difference is what Oaken kept out of the client's context. Estimates, the
-- same chars/4 the rest of Oaken uses. Never any message text.
CREATE TABLE context_savings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    op              TEXT NOT NULL,
    caller          TEXT NOT NULL,
    messages        INTEGER NOT NULL,
    tokens_returned INTEGER NOT NULL,
    tokens_full     INTEGER NOT NULL
);
CREATE INDEX context_savings_ts ON context_savings (ts);
