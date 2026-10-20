-- The ingest pipeline (prompt 09).
--
-- ai_usage: one row per model call or per call that a cache made unnecessary. This is the evidence
-- for the token-thrift claim. For a cached row, tokens_in / tokens_out are what the call WOULD have
-- cost, so a summary can say how much was saved. Never any message text.
CREATE TABLE ai_usage (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    stage      TEXT NOT NULL,            -- embed | enrich | ...
    model_id   TEXT NOT NULL,
    tokens_in  INTEGER NOT NULL,
    tokens_out INTEGER NOT NULL,
    cached     INTEGER NOT NULL CHECK (cached IN (0, 1)),
    account_id TEXT,
    message_id TEXT
);
CREATE INDEX ai_usage_ts ON ai_usage (ts);

-- enrichments: one enricher's result for one message. The cache key is
-- (content_hash, enricher, version, model_id): the same key anywhere (even another message with
-- identical content) is reused, so a model is never asked the same question twice. These live in
-- raw.db, not index.db, because they cost model time to make and index.db is disposable.
CREATE TABLE enrichments (
    message_id   TEXT NOT NULL,
    enricher     TEXT NOT NULL,
    version      TEXT NOT NULL,
    model_id     TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    data_json    TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (message_id, enricher)
);
CREATE INDEX enrichments_key ON enrichments (content_hash, enricher, version, model_id);
