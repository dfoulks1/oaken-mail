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

- [x] Ratify ADRs 0015, 0016, 0017, 0018, 0019 (and 0020): done 2026-11-03. The owner verified the
  built implementation works for 0.2.0, so the reality is ratified; statuses flipped to accepted.
- [ ] Prompt overrides: `oaken prompts` and `[prompts.<task>]` (the owner asked; not built).
- [ ] Labels a rule applies, and automations from a label (CORE_51 goals 2, 3, 6).
- [ ] Restart core for the 0018 migration and the `rules` / `spamcheck` entry points (refresh the editable install first).
- [ ] Move the triage rule operations into the `rules` plugin and the level operations into `BaseTriager` (ADR 0016, accepted): plugin `store` and `draft`, a `wizard` declaration the CLI and console render, then drop `oaken rule`/`oaken level`.
- [x] Separate triagers from enrichers (ADR 0017 accepted, `CORE_55` built): `oaken_triagers` package, triagers on `BaseTriager` not `BaseEnricher`, `triage_verdicts` table + migration, `get_triage`.
- [x] Plugin web UI contract (ADR 0018 accepted, `CORE_56` built): `ui()`/`UiSpec`, core renders, no plugin markup or script.
- [x] Attachment handler, `BaseScanner` and the ClamAV scanner (`CORE_57`, ADR 0020): built, tested, checks pass (ruff, mypy, pytest). Was `CORE_52`; renumbered.
- [ ] Run the full checks once more before the tag: ruff, mypy, pytest, the docs check and build, and `uv.lock`. Back up `~/.oaken` first: migrations 0019 to 0021 run on the next start.
- [ ] Next: the plugin capabilities (ADR 0016: `store`, `folders`, `draft`), the rules plugin owning its rules and the wizard page (ADR 0018), then the `attachment` action. Open: the grant mechanism, the result-store helper, the lazy triage chain.


## Two-sided mail: file and local server

Oaken should sit on either side of the MUA: read a Thunderbird/Outlook/mbox-file store, and serve
its own store back to one over IMAP.

- [ ] **Mailbox-file provider.** A provider plugin that reads updates from a mailbox file (`mbox`,
  and ideally Maildir). It pulls like any source: parse the file, yield the new or changed messages
  since the last cursor (file offset or message count for mbox, new/cur scan for Maildir). Read-only
  first, no `send`, no `flags.write`. This is the ROADMAP "Later" `.mbox` import, made concrete.
  Open: how a source that is not a live account picks its datastore and workspace (the same open
  question the roadmap already flags), and whether a file that is rewritten under us (mbox compaction)
  invalidates the cursor.
- [ ] **Local IMAP-server action.** An action plugin that serves core's own store to an MUA over a
  loopback IMAP server, so Thunderbird or Outlook reads Oaken's mail as if it were any IMAP account.
  The inverse of `oaken-imap` (that one is a client pulling from a remote server; this one is the
  server). Binds `127.0.0.1` only, read-first (folders and FETCH before any write-back). Open: how
  it maps core's labels and folders onto IMAP folders, and how flag write-back (`\Seen`) rejoins
  core's durable read-state path.
