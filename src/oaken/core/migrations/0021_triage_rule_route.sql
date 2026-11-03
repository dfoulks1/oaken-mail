-- A rule can route a message to actions (ADR 0019): the names, as a JSON list; empty routes nowhere.
ALTER TABLE triage_rules ADD COLUMN route_json TEXT NOT NULL DEFAULT '[]';
