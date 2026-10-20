-- Raw mail store (raw.db): the source of truth. Nothing derived lives here. Times as in 0001.
-- accounts.config_json holds non-secret settings only. Credentials belong in the OS keyring.
CREATE TABLE accounts (
    id           TEXT PRIMARY KEY,
    provider     TEXT NOT NULL,
    display_name TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    config_json  TEXT NOT NULL DEFAULT '{}'
);

-- Content-addressed: sha256 is of the raw bytes, so identical messages share one row.
-- codec: zstd (data holds the frame) | zstd-file (data is NULL; the frame is the LFS file at
-- lfs_path, relative to the LFS folder). A large message also keeps a small zstd header stub
-- inline, so listing and parsing headers never has to open the big file.
CREATE TABLE blobs (
    sha256      TEXT PRIMARY KEY,
    size_raw    INTEGER NOT NULL,
    size_stored INTEGER NOT NULL,
    codec       TEXT NOT NULL CHECK (codec IN ('zstd', 'zstd-file')),
    data        BLOB,
    stub        BLOB,
    lfs_path    TEXT
);

CREATE TABLE messages (
    id            TEXT PRIMARY KEY,
    account_id    TEXT NOT NULL REFERENCES accounts (id),
    provider_id   TEXT NOT NULL,
    blob_sha256   TEXT NOT NULL REFERENCES blobs (sha256),
    internal_date TEXT,
    first_seen_at TEXT NOT NULL,
    deleted_at    TEXT,
    UNIQUE (account_id, provider_id)
);

CREATE INDEX messages_seen ON messages (account_id, first_seen_at, id);
CREATE INDEX messages_blob ON messages (blob_sha256);

-- Read-state intent (prompt 12): local_dirty_json is what still has to be written to the server.
CREATE TABLE message_state (
    message_id        TEXT PRIMARY KEY REFERENCES messages (id),
    is_read           INTEGER NOT NULL DEFAULT 0,
    is_starred        INTEGER NOT NULL DEFAULT 0,
    labels_json       TEXT NOT NULL DEFAULT '[]',
    server_updated_at TEXT,
    local_dirty_json  TEXT
);

CREATE TABLE sync_cursors (
    account_id TEXT NOT NULL REFERENCES accounts (id),
    scope      TEXT NOT NULL,
    cursor     TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, scope)
);
