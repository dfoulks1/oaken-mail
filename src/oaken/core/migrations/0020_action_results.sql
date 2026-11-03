-- action_results: what an action did about a message routed to it (ADR 0019). One row for each
-- message, action and route; a retry updates it. status is done, failed or skipped. There is no
-- REFERENCES here: an action's rows point at a message by id and outlive nothing, so dropping an
-- action plugin, or a message, breaks no other table.
CREATE TABLE action_results (
    message_id TEXT NOT NULL,
    action     TEXT NOT NULL,
    route      TEXT NOT NULL,
    status     TEXT NOT NULL,
    version    TEXT NOT NULL,
    result_json TEXT NOT NULL,
    error      TEXT NOT NULL,
    attempts   INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (message_id, action, route)
);
CREATE INDEX action_results_status ON action_results (action, status);
