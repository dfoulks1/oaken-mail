-- Add the folder a message is organized into (ADR 0008). Single-valued, copied from raw.db's
-- message_attrs (kind='location'); a filed message carries one, inbox/discarded carry none.
-- index.db is rebuildable from raw.db, so no data is lost if this is rebuilt instead of migrated.
ALTER TABLE docs ADD COLUMN location TEXT;

CREATE INDEX docs_location ON docs (location, date_utc) WHERE location IS NOT NULL;
