-- Prompt 17 (CLI feature-completeness): a durable switch to pause the LLM-using ingest stages
-- (ingest.embed, ingest.enrich) without stopping mail from landing. One row, id = 0: when
-- llm_paused is 1 the job runner stops claiming those two kinds; parse and sync keep flowing. The
-- flag survives a restart (the runner loads it at start), so "pause the model" stays paused until
-- the person resumes it. reason/by/at record who paused it and why, for the audit trail and the UI.
CREATE TABLE pipeline_control (
    id         INTEGER PRIMARY KEY CHECK (id = 0),
    llm_paused INTEGER NOT NULL DEFAULT 0 CHECK (llm_paused IN (0, 1)),
    reason     TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT ''
);

INSERT INTO pipeline_control (id, llm_paused) VALUES (0, 0);
