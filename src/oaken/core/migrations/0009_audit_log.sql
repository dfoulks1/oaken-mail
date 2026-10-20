-- Prompt 12: the audit log. Every state-changing action that can be undone writes a row here, in
-- the same transaction as the change. before_json and after_json map message id -> the value
-- before and after (for read state: true or false), so undo restores the exact prior state.
-- A change to more than [read_state] audit_chunk messages is several rows sharing one batch_id.
-- message ids are internal ids; no mail content is ever stored here.
CREATE TABLE audit_log (
    id               TEXT PRIMARY KEY,
    ts               TEXT NOT NULL,
    actor_kind       TEXT NOT NULL CHECK (actor_kind IN ('user', 'agent', 'system')),
    actor_label      TEXT NOT NULL DEFAULT '',
    reason           TEXT NOT NULL,
    op               TEXT NOT NULL,
    batch_id         TEXT NOT NULL,
    message_ids_json TEXT NOT NULL,
    before_json      TEXT NOT NULL,
    after_json       TEXT NOT NULL,
    undone_by        TEXT REFERENCES audit_log (id)
);

CREATE INDEX audit_log_ts ON audit_log (ts, id);
CREATE INDEX audit_log_batch ON audit_log (batch_id);
