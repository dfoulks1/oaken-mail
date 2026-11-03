# TODO

Working items. ROADMAP.md holds the why; this file holds the next concrete steps.

- [x] **CORE_47** (one job per enricher): done.
- [x] **Expose triage for tuning.** Done: `try_triage` (CLI and MCP) and, for rules, `triage_rule_test`. Was: `enrich_message` is `mcp="never"` (`src/oaken/core/operations/builtin/enrichment.py`), so the triager can only run from the CLI. Add a read-only dry-run operation that runs triage on one message (or pasted text) and returns the result without storing it, and expose that over MCP.
- [x] **Fix the header-rule priority.** `rule_for` in `src/oaken_enrichers/triage.py` settles `precedence` and `list-headers` mail at priority 0 without the model. A human reply sent through a list gets priority 0, so the strict gate skips its summary and actions. Let the model rate priority for list mail, or narrow the rule to bulk senders.
- [ ] **Check the live triage model.** The live config runs `qwen2.5:0.5b` for triage and the Ollama default. Score it on a hand-labelled sample against `qwen3.5:4b` and `qwen2.5:3b` before trusting its priorities.
- [ ] **Test `triage_gate`.** Committed in `c25257c` (`src/oaken/core/settings.py`, `src/oaken/ingest/pipeline.py`, `config.example.toml`) but no test in `tests/` mentions it. Cover strict and promiscuous.
- [x] **One pause/resume button on the enrichment pipeline.** Done: `general.html` has one "Model stages" switch, and the old labels are gone from `src/`.

## Release 0.2.0

- [x] Prompts naming local projects: decided 2026-11-02. Libra removed, `mailmind-unified` stays (basis of oaken-mail), `ollmcp` is third-party and stays.
- [ ] Review the audit report and the docs beyond the software map and console UI, or leave them labelled AI-generated.
- [ ] Optional second blind tastetest to confirm block 1 is fixed.
- [ ] Date the CHANGELOG entry, tag `v0.2.0`, publish (`docs/releasing.md`).

## Triage rules (ADR 0015, proposed; CORE_54 built)

- [ ] Ratify ADR 0015 (only the owner can).
- [ ] Prompt overrides: `oaken prompts` and `[prompts.<task>]` (the owner asked; not built).
- [ ] Labels a rule applies, and automations from a label (CORE_51 goals 2, 3, 6).
- [ ] Restart core for the 0018 migration and the `rules` / `spamcheck` entry points (refresh the editable install first).
- [ ] Move the triage rule operations into the `rules` plugin and the level operations into `BaseTriager` (ADR 0016, proposed): plugin `store` and `draft`, a `wizard` declaration the CLI and console render, then drop `oaken rule`/`oaken level`.
- [ ] Separate triagers from enrichers (ADR 0017 proposed, `CORE_55` draft): review the prompt, ratify, then build; it blocks ADR 0016 and the 0.2.0 tag.
- [ ] Plugin web UI contract (ADR 0018 proposed, `CORE_56` draft): review, ratify, build after CORE_55 and before ADR 0016; in 0.2.0.
- [ ] Run the checks on what was built 2026-11-03 (ADR 0017 and 0019, `ask` removed): ruff, mypy, pytest, the docs check and build, and `uv.lock`. Back up `~/.oaken` first: migrations 0019 to 0021 run on the next start.
- [ ] Next: the plugin capabilities (ADR 0016: `store`, `folders`, `draft`), the rules plugin owning its rules and the wizard page (ADR 0018), then the `attachment` action. Open: the grant mechanism, the result-store helper, the lazy triage chain.

