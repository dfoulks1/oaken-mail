-- The reader's triage rules and levels moved into the `rules` plugin (ADR 0016): rules live in
-- [plugins.triager.rules] in config.toml, levels in the triager kind's own table. Core no longer
-- holds them. Migrations 0018 and 0021 were never released, so this drops tables nothing in a
-- release wrote. A reader who ran an unreleased build re-adds their rules with `oaken rules add`.
DROP TABLE IF EXISTS triage_ack;
DROP TABLE IF EXISTS triage_rules;
DROP TABLE IF EXISTS triage_levels;
