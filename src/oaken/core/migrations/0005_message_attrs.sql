-- Organization attributes for a message (prompt 06c): labels, flags, a disposition and notes, set by
-- the user's Sieve scripts and, later, by the user directly. They are what a UI uses to build a
-- mailbox. They are not the message: the bytes live in blobs, and a script change re-derives the
-- 'sieve' rows (message_filters says which scripts last ran). Rows of any other source are never
-- touched by a re-run.
--   disposition  inbox | filed | discarded   (exactly one per source; discarded is hidden, never deleted)
--   label        a mailbox or folder name
--   flag         \Seen, \Flagged or a keyword
--   intent       a send the script asked for and Oaken suppressed (redirect, reject, vacation)
--   analysis     skip_enrich: never send this message to a model
CREATE TABLE message_attrs (
    message_id TEXT NOT NULL REFERENCES messages (id),
    kind       TEXT NOT NULL CHECK (kind IN ('disposition', 'label', 'flag', 'intent', 'analysis')),
    value      TEXT NOT NULL,
    source     TEXT NOT NULL,
    rule       TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (message_id, kind, value, source)
) WITHOUT ROWID;

CREATE INDEX message_attrs_lookup ON message_attrs (kind, value, message_id);
CREATE UNIQUE INDEX message_attrs_one_disposition
    ON message_attrs (message_id, source) WHERE kind = 'disposition';

CREATE TABLE message_filters (
    message_id     TEXT PRIMARY KEY REFERENCES messages (id),
    scripts_sha256 TEXT NOT NULL,
    ran_at         TEXT NOT NULL,
    errors_json    TEXT NOT NULL DEFAULT '[]'
);
