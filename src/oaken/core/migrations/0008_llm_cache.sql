-- Prompt 10: the LLM result cache and per-workspace accounting.
--
-- llm_cache: one validated model answer, keyed by everything that could change it. ``workspace`` is
-- part of the key so a result made in one workspace is never served to another (a ``secure``
-- workspace's results never reach an ``insecure`` one). The result is the model's JSON output after
-- validation; it can contain text derived from mail, so this table is as sensitive as raw.db.
CREATE TABLE llm_cache (
    workspace      TEXT NOT NULL,
    task           TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    model_id       TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    result_json    TEXT NOT NULL,
    tokens_in      INTEGER NOT NULL,
    tokens_out     INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (workspace, task, content_hash, model_id, prompt_version)
);

ALTER TABLE ai_usage ADD COLUMN workspace TEXT NOT NULL DEFAULT 'default';
