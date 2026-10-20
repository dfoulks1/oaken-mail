-- Durable job queue (raw.db). Times are UTC ISO-8601 with microseconds, so they sort as text.
-- status: queued | running | succeeded | failed | cancelled
-- priority: higher runs first (user 100, ingest 50, backfill 10).
CREATE TABLE jobs (
    id               TEXT PRIMARY KEY,
    account_id       TEXT,
    kind             TEXT NOT NULL,
    payload_json     TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    priority         INTEGER NOT NULL DEFAULT 50,
    attempts         INTEGER NOT NULL DEFAULT 0,
    max_attempts     INTEGER NOT NULL DEFAULT 5,
    run_after        TEXT NOT NULL,
    locked_by        TEXT,
    locked_until     TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    dedupe_key       TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE INDEX jobs_claim ON jobs (status, priority DESC, run_after, id);
CREATE INDEX jobs_kind_status ON jobs (kind, status);

-- A dedupe key is unique only while the job is live; finished jobs release it.
CREATE UNIQUE INDEX jobs_dedupe_live ON jobs (dedupe_key)
    WHERE dedupe_key IS NOT NULL AND status IN ('queued', 'running');
