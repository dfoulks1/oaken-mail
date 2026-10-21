-- The mailbox structure a client shows in its sidebar: folders (nested, rooted at Inbox) and labels
-- (flat tags). A row exists whether or not any message is in it, so an empty folder or a label the
-- user just made persists. Counts are never stored here: they come from message_attrs.
--
-- kind 'folder': path is relative to Inbox and uses "/" between levels ("Lists/Dev" is Inbox/Lists/Dev).
--   It equals the `location` attr value (ADR 0008) of the messages filed there.
-- kind 'label': path is the label's name, which equals the `label` attr value.
-- source says how the row came to be: 'user' (made in a client), 'sieve' (seen in a script's result).
CREATE TABLE mailbox_nodes (
    node_id    TEXT PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('folder', 'label')),
    path       TEXT NOT NULL,
    color      TEXT NOT NULL DEFAULT '',
    position   INTEGER NOT NULL DEFAULT 0,
    source     TEXT NOT NULL DEFAULT 'user',
    created_at TEXT NOT NULL,
    UNIQUE (kind, path)
);
