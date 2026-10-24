-- Prompt 30: manage the queue per stage. A stage can be paused on its own (the single
-- ``pipeline_control.llm_paused`` row still pauses every model-bound stage at once), and the
-- number of jobs a stage runs at once can be changed while core runs. Both are stored here so
-- they survive a restart, and the job runner reads them on every claim.
CREATE TABLE pipeline_stage_pause (
    kind       TEXT PRIMARY KEY,
    reason     TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT ''
);

CREATE TABLE pipeline_limits (
    kind        TEXT PRIMARY KEY,
    concurrency INTEGER NOT NULL CHECK (concurrency >= 1),
    updated_at  TEXT NOT NULL DEFAULT '',
    updated_by  TEXT NOT NULL DEFAULT ''
);
