-- Recurring enqueues (raw.db). next_run is persisted so a restart neither repeats nor skips.
-- spec_kind: interval (spec = seconds) | cron (spec = five-field expression, UTC)
CREATE TABLE schedules (
    name         TEXT PRIMARY KEY,
    account_id   TEXT,
    kind         TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    spec_kind    TEXT NOT NULL CHECK (spec_kind IN ('interval', 'cron')),
    spec         TEXT NOT NULL,
    priority     INTEGER NOT NULL DEFAULT 50,
    next_run     TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE INDEX schedules_due ON schedules (next_run);
