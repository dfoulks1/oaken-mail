-- The staged copy of a plugin's own config table (ADR 0016, the `draft` capability). Core validates
-- it against the plugin's config_model and writes config.toml; the plugin only edits this copy.
CREATE TABLE plugin_drafts (
    kind        TEXT NOT NULL,
    name        TEXT NOT NULL,
    draft_json  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (kind, name)
);
