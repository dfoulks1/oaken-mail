-- Add two attribute kinds (ADR 0008):
--   location    the folder a message is organized into. Single-valued per (message, source),
--               like disposition; rides with disposition = 'filed'. Distinct values are the
--               folders a client renders. inbox and discarded carry no location.
--   enrichment  a general, multi-valued escape hatch for enrichers and plugins; value is
--               namespaced "<producer>:<key>=<data>" so producers do not collide.
--
-- SQLite cannot alter a CHECK in place, so the table is rebuilt and its rows copied. Forward-only:
-- editing 0005 would change its checksum and the runner would refuse an already-applied database.

CREATE TABLE message_attrs_new (
    message_id TEXT NOT NULL REFERENCES messages (id),
    kind       TEXT NOT NULL CHECK (kind IN (
                   'disposition', 'location', 'label', 'flag', 'intent', 'analysis', 'enrichment'
               )),
    value      TEXT NOT NULL,
    source     TEXT NOT NULL,
    rule       TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (message_id, kind, value, source)
) WITHOUT ROWID;

INSERT INTO message_attrs_new (message_id, kind, value, source, rule)
    SELECT message_id, kind, value, source, rule FROM message_attrs;

DROP TABLE message_attrs;
ALTER TABLE message_attrs_new RENAME TO message_attrs;

CREATE INDEX message_attrs_lookup ON message_attrs (kind, value, message_id);
CREATE UNIQUE INDEX message_attrs_one_disposition
    ON message_attrs (message_id, source) WHERE kind = 'disposition';
-- A message is in at most one folder per source, mirroring the disposition invariant.
CREATE UNIQUE INDEX message_attrs_one_location
    ON message_attrs (message_id, source) WHERE kind = 'location';
