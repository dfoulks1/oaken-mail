# Oaken Mail prompt series

Self-contained prompts for building Oaken Mail with Claude Code, one per session,
in order. Copy this directory into the `oaken-mail` checkout (`docs/prompts/`) first. Start every session with: *"Read `docs/prompts/00-context.md`, then
execute `docs/prompts/NN-….md`."* Each prompt ends with a report-back checkpoint;
review it before starting the next.

| # | Prompt | Delivers | Depends on |
|---|---|---|---|
| 01 | [bootstrap](01-bootstrap.md) | Clean repo, Apache-2.0, CI, secret scanning, legacy-data safeguard | — |
| 01a | [docs-site-scaffold](01a-docs-site-scaffold.md) | Generated GitHub Pages site (Libra look), build/check tooling, Pages deploy | 01 |
| 02 | [core-foundation](02-core-foundation.md) | Settings, paths, logging, errors, IDs, clock, DB access layer | 01 |
| 03 | [events-and-jobs](03-events-and-jobs.md) | Event bus, durable job queue, scheduler | 02 |
| 03b | [swarm-foundations](03b-swarm-foundations.md) | Optional external agents: scoped tokens, quotas, validated results, fan-out and attribution | 03 |
| 04 | [plugin-host](04-plugin-host.md) | Plugin contracts (5), host, fakes, contract tests | 03 |
| 05 | [raw-store](05-raw-store.md) | `raw.db`, blob store, migrations, accounts/messages/cursors | 02 |
| 06 | [parsing-and-cleaning](06-parsing-and-cleaning.md) | RFC 822 → structured, cleaned text; threading keys | 05 |
| 06c | [sieve-filtering](06c-sieve-filtering.md) | Sieve scripts organize mail into labels, flags and a disposition, stored in `raw.db` | 05, 06 |
| 07 | [index-and-fts](07-index-and-fts.md) | `index.db`, chunks, FTS5, rebuild-from-raw | 06 |
| 08 | [vector-store](08-vector-store.md) | sqlite-vec spike + VectorStore plugin | 04, 07 |
| 09 | [ingest-pipeline](09-ingest-pipeline.md) | Staged, resumable pipeline orchestrating the AI backend | 03–08 |
| 10 | [ollama-and-prompt-safety](10-ollama-and-prompt-safety.md) | Ollama plugin, `UntrustedText`, structured outputs, LLM cache | 04, 09 |
| 11 | [gmail-plugin](11-gmail-plugin.md) | Gmail provider: BYO OAuth, resilient tokens, sync, flags, labels, trash, send | 04, 05, 09 |
| 11b | [smtp-send](11b-smtp-send.md) | SMTP send-as provider: sends only, password in the keyring, TLS required | 04, 11 |
| 11c | [imap-provider](11c-imap-provider.md) | Plain IMAP provider (to be settled in discussion) | 04, 05, 09 |
| 12 | [read-state](12-read-state.md) | `mark_read` on open/summarize, write-back, audit + undo | 11 |
| 12b | [sync-scheduling](12b-sync-scheduling.md) | Core loads providers and syncs on a per-account schedule held in the database | 03, 11c, 12 |
| 13 | [operation-registry](13-operation-registry.md) | Typed operations; permission classes; confirm gate | 09, 12 |
| 14 | [enrichers](14-enrichers.md) | Summary, triage, actions, header/MIME inspector | 10, 13 |
| 15 | [retrieval-and-ask](15-retrieval-and-ask.md) | Hybrid search, token-budgeted context, cited Q&A, eval harness | 08, 14 |
| 16 | [api-daemon](16-api-daemon.md) | Local HTTP + SSE, token auth, `start/stop/status` | 13 |
| 17 | [cli-client](17-cli-client.md) | Thin CLI over the API | 16 |
| 18 | [mcp-server](18-mcp-shim.md) | MCP server hosted by core (+ `oaken mcp` stdio bridge), generated from registry | 16 |
| 19 | [security-hardening](19-security-hardening.md) | Injection suite, localhost threat tests, audit review | 16–18 |
| 20 | [oss-docs](20-oss-docs.md) | README, site content, plugin guide, ADRs, roadmap; site verified | all |
| 21 | [webui-server](21-webui-server.md) | `oaken-webui`: a thin client that serves the UI and proxies `/v1/*` to core (core stays headless) | 16, 17 |
| 22 | [api-parity](22-api-parity.md) | Expand the API for the UI; audit registry ⇄ CLI ⇄ UI; enforce 1:1 CLI/UI parity (one backend) | 13, 16, 17, 21 |
| 23 | [plugin-settings](23-plugin-settings.md) | Configure plugins: load/test/save (`get`/`check`/`set_plugin_config`), schema form, `config.toml` round-trip, reload | 04, 13, 21, 22 |
| 24 | [accounts-onboarding](24-accounts-onboarding.md) | Add/auth/test/configure accounts; Gmail "listen but not send" (receive-only) | 11–12b, 23 |
| 25 | [folders-labels-counts](25-folders-labels-counts.md) | Sidebar chrome: folders, labels and unread counts (new `mailbox_counts` op) | 07, 12, 22 |
| 26 | [sieve-management](26-sieve-management.md) | Manage Sieve in the UI: list/check/test + a write path (`sieve_save`/`delete`, never over MCP) | 06c, 22 |
| 27 | [contacts-people](27-contacts-people.md) | People view derived from mail (`list_people`/`get_person`); derive-vs-store ADR | 06, 07, 22 |

## Not written yet (deliberately)

- **Compose/send** (after the read side, search and AI are solid; send is never exposed over MCP).
- **Legacy-DB importer** (only if wanted), **packaging**
  (systemd/launchd, installers), **at-rest encryption**, **plugin sandboxing**,
  **embedding-throughput tuning on CPU**.

The **Web UI** is now a written phase (prompts 21–27): its own thin-client app (`oaken-webui`)
that serves the UI and proxies the general API, plus the registry operations each UI feature needs,
built with 1:1 CLI/UI parity (one backend).

## Decisions recorded while planning

- `raw.db` also holds the durable operational tables (job queue, audit log,
  sync cursors, read-state intents), so there are exactly **two** database files.
  Only `index.db` is disposable.
- Plugin extension points: Provider, Embedder, LLMBackend, Enricher, VectorStore.
- Clients reach core over HTTP on `127.0.0.1` + SSE; the CLI does **not** auto-start
  core; MCP is a stdio shim; core serves the built UI files (revisit at the UI phase).
