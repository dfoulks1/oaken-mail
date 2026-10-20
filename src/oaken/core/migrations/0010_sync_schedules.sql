-- Prompt 12b: when each account syncs. One row per account, and optionally one per folder for a
-- provider with folders (folder = '' is the whole account). Edited live from the UI and CLI, so it
-- is data, not config. spec_kind and spec are as in `schedules` (0002): interval = seconds, cron =
-- five fields, here read in `timezone` (an IANA name). quiet_start and quiet_end are HH:MM in that
-- timezone: inside them a sync still runs, but clients are told not to alert.
-- next_run is persisted so a restart neither repeats nor skips a run. failures counts consecutive
-- failed syncs and drives the backoff; paused_reason says why (an auth failure pauses by itself).
-- last_error is a scrubbed one-liner, never mail content.
CREATE TABLE sync_schedules (
    account_id    TEXT NOT NULL REFERENCES accounts (id),
    folder        TEXT NOT NULL DEFAULT '',
    spec_kind     TEXT NOT NULL CHECK (spec_kind IN ('interval', 'cron')),
    spec          TEXT NOT NULL,
    timezone      TEXT NOT NULL DEFAULT 'UTC',
    quiet_start   TEXT,
    quiet_end     TEXT,
    paused        INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
    paused_reason TEXT NOT NULL DEFAULT '',
    next_run      TEXT NOT NULL,
    last_run      TEXT,
    last_result   TEXT,
    last_error    TEXT,
    last_new      INTEGER,
    failures      INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (account_id, folder)
);

CREATE INDEX sync_schedules_due ON sync_schedules (next_run) WHERE paused = 0;
