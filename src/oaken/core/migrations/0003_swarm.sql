-- Agents (external workers) and the columns jobs need for swarms. Times as in 0001.
-- token_hash is the SHA-256 of the agent token. The token itself is never stored.
-- kinds_json lists the job kinds the agent may lease. account_ids_json is NULL for all accounts.
CREATE TABLE agents (
    id                TEXT PRIMARY KEY,
    label             TEXT NOT NULL,
    token_hash        TEXT NOT NULL UNIQUE,
    kinds_json        TEXT NOT NULL,
    account_ids_json  TEXT,
    max_leases        INTEGER NOT NULL,
    max_per_hour      INTEGER NOT NULL,
    created_at        TEXT NOT NULL,
    last_seen_at      TEXT,
    revoked_at        TEXT
);

-- Fan-out and join: a job may name its parent.
ALTER TABLE jobs ADD COLUMN parent_job_id TEXT;
-- Who enqueued the job.
ALTER TABLE jobs ADD COLUMN actor_kind TEXT NOT NULL DEFAULT 'system'
    CHECK (actor_kind IN ('user', 'agent', 'system'));
ALTER TABLE jobs ADD COLUMN actor_label TEXT NOT NULL DEFAULT '';
-- The last agent to lease the job, and when (the per-hour quota counts these).
ALTER TABLE jobs ADD COLUMN agent_id TEXT;
ALTER TABLE jobs ADD COLUMN claimed_at TEXT;
-- Output of a succeeded job, who finished it, and the model the finisher says produced it.
ALTER TABLE jobs ADD COLUMN result_json TEXT;
ALTER TABLE jobs ADD COLUMN completed_by TEXT;
ALTER TABLE jobs ADD COLUMN produced_by TEXT;

CREATE INDEX jobs_parent ON jobs (parent_job_id) WHERE parent_job_id IS NOT NULL;
CREATE INDEX jobs_agent ON jobs (agent_id, claimed_at) WHERE agent_id IS NOT NULL;
