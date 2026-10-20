-- index.db (prompt 07): everything here is derived from raw.db and can be deleted and rebuilt.
-- Times are UTC, fixed-width ISO text (as in raw.db), so they sort as text.

-- pipeline_version: bumped when the parse/clean/chunk output changes; a mismatch marks the index stale.
-- raw_store_id: the random id of the raw.db this was built from; another raw.db is refused.
CREATE TABLE index_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per message. disposition and labels are copied from raw.db's message_attrs (they are
-- what Sieve decided) so a search can filter on them without opening raw.db.
CREATE TABLE docs (
    message_id          TEXT PRIMARY KEY,
    account_id          TEXT NOT NULL,
    thread_key          TEXT NOT NULL,
    subject_key         TEXT NOT NULL,
    subject             TEXT NOT NULL,
    from_addr           TEXT NOT NULL,
    from_name           TEXT NOT NULL,
    to_json             TEXT NOT NULL,
    date_utc            TEXT,
    list_id             TEXT,
    has_attachments     INTEGER NOT NULL,
    attachments_json    TEXT NOT NULL,
    cleaned_text        TEXT NOT NULL,
    content_hash        TEXT NOT NULL,
    parse_warnings_json TEXT NOT NULL,
    disposition         TEXT NOT NULL DEFAULT 'inbox' CHECK (disposition IN ('inbox', 'filed', 'discarded')),
    indexed_at          TEXT NOT NULL
);

CREATE INDEX docs_account_date ON docs (account_id, date_utc);
CREATE INDEX docs_thread ON docs (thread_key, date_utc);
CREATE INDEX docs_subject ON docs (subject_key, date_utc);
CREATE INDEX docs_list ON docs (list_id) WHERE list_id IS NOT NULL;
CREATE INDEX docs_disposition ON docs (disposition, date_utc);

CREATE TABLE doc_labels (
    message_id TEXT NOT NULL REFERENCES docs (message_id) ON DELETE CASCADE,
    label      TEXT NOT NULL,
    PRIMARY KEY (message_id, label)
) WITHOUT ROWID;

CREATE INDEX doc_labels_label ON doc_labels (label, message_id);

-- A chunk of cleaned text. subject and sender are the context for a result, kept apart from the
-- text. content_hash is of text alone, so an embedding or summary of an unchanged chunk is reused.
-- source is 'main' or 'fwd:N' (the Nth message forwarded inside this one).
CREATE TABLE chunks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id     TEXT NOT NULL REFERENCES docs (message_id) ON DELETE CASCADE,
    ordinal        INTEGER NOT NULL,
    source         TEXT NOT NULL,
    subject        TEXT NOT NULL,
    sender         TEXT NOT NULL,
    text           TEXT NOT NULL,
    token_estimate INTEGER NOT NULL,
    content_hash   TEXT NOT NULL,
    UNIQUE (message_id, ordinal)
);

-- External content: the text lives once, in chunks, and the triggers keep the index in step. (A
-- contentless table could not give snippets.) Prefix indexes make typeahead and ``term*`` cheap.
CREATE VIRTUAL TABLE fts USING fts5(
    subject, sender, text,
    content = 'chunks', content_rowid = 'id',
    tokenize = 'unicode61 remove_diacritics 2',
    prefix = '2 3 4'
);

CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO fts (rowid, subject, sender, text) VALUES (new.id, new.subject, new.sender, new.text);
END;

CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO fts (fts, rowid, subject, sender, text)
    VALUES ('delete', old.id, old.subject, old.sender, old.text);
END;

CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO fts (fts, rowid, subject, sender, text)
    VALUES ('delete', old.id, old.subject, old.sender, old.text);
    INSERT INTO fts (rowid, subject, sender, text) VALUES (new.id, new.subject, new.sender, new.text);
END;
