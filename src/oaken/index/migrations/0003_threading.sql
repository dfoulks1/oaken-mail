-- What a message answers, so a thread can be shown as a tree (who replied to whom).
--   msgid     the message's own Message-ID header, without angle brackets
--   refs_json the Message-IDs it answers, nearest first (In-Reply-To, then References newest back)
-- Both are NULL on rows indexed before this migration; the thread operation fills them in from the
-- raw message the first time it needs them. index.db is rebuildable from raw.db.
ALTER TABLE docs ADD COLUMN msgid TEXT;
ALTER TABLE docs ADD COLUMN refs_json TEXT;

CREATE INDEX docs_msgid ON docs (msgid) WHERE msgid IS NOT NULL;
