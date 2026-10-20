-- Identity of this raw store (prompt 07). index.db records it when it is built, so an index made
-- from a different raw.db is detected and refused. Random, created once; an upgraded store gets
-- one here. Never derived from content, so it is not a secret and not a fingerprint of the mail.
CREATE TABLE raw_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT INTO raw_meta (key, value) VALUES ('store_id', lower(hex(randomblob(16))));
