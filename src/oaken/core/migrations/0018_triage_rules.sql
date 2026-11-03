-- User-defined triage: levels and deterministic rules. Each has a uid that never changes and is
-- never shown, so a level can be renumbered or a rule renamed or moved without losing what points
-- at it. A rule's place in the order is ``position`` (lowest first; the first match wins).
CREATE TABLE triage_levels (
    uid     TEXT PRIMARY KEY,
    value   INTEGER NOT NULL UNIQUE CHECK (value BETWEEN 0 AND 99),
    name    TEXT NOT NULL,
    builtin INTEGER NOT NULL DEFAULT 0
);
INSERT INTO triage_levels (uid, value, name, builtin) VALUES
    ('level-0', 0, 'ignore', 1), ('level-1', 1, 'low', 1),
    ('level-2', 2, 'normal', 1), ('level-3', 3, 'soon', 1);

CREATE TABLE triage_rules (
    uid        TEXT PRIMARY KEY,
    position   INTEGER NOT NULL,
    name       TEXT NOT NULL UNIQUE,
    enabled    INTEGER NOT NULL DEFAULT 1,
    level_uid  TEXT NOT NULL REFERENCES triage_levels (uid),
    category   TEXT NOT NULL,
    when_json  TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT ''
);

-- The two tables above are the *working copy*: what the CLI and the console edit. ``snapshot_json``
-- is the last copy written to, or accepted from, ``config.toml`` (the last known good); it is what
-- runs, and what the file is compared with. With no row, nothing has been acknowledged yet and the
-- file's rules are adopted at first start instead of being reported as drift.
CREATE TABLE triage_ack (
    id            INTEGER PRIMARY KEY CHECK (id = 0),
    acked_at      TEXT NOT NULL,
    snapshot_json TEXT NOT NULL
);
