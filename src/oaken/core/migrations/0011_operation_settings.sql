-- Prompt 13: which operations an MCP client may call, as the user set it. A row is one choice:
-- scope 'op:<name>' for an operation, 'plugin:<name>' for every operation a plugin offers. No row
-- means the operation's own default. This never widens what code allows: an operation that can
-- never be exposed over MCP (sending, raw access, configuration) ignores its row.
CREATE TABLE operation_settings (
    scope       TEXT PRIMARY KEY,
    mcp_enabled INTEGER NOT NULL CHECK (mcp_enabled IN (0, 1)),
    updated_at  TEXT NOT NULL,
    updated_by  TEXT NOT NULL
);
