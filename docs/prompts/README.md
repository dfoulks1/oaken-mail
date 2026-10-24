# Oaken Mail prompt series

Self-contained prompts for building Oaken Mail with Claude Code, one per session. Copy this
directory into the `oaken-mail` checkout (`docs/prompts/`) first. Start every session with: *"Read
`docs/prompts/00-context.md`, then execute `docs/prompts/TRACK_NN-….md`."* Each prompt ends with a
report-back checkpoint; review it before starting the next.

## Contributing

To run a prompt, ask for a feature (by writing its prompt), or change a prompt, see
[CONTRIBUTING.md](../../CONTRIBUTING.md#contributing-with-prompts). [`TEMPLATE.md`](TEMPLATE.md) is the
format for a new one.

## How the files are named

`TRACK_NN-name.md`. The **track** says what the prompt builds, and the **number** is the prompt's
permanent ID: "prompt 28" in code, docs and the status table always means `CORE_28-compose-and-send`.
A new prompt takes the next free number, whatever its track. `00-context.md` (the rules every prompt
starts from) and this index have no track.

| Track | What it builds |
|---|---|
| `CORE_` | The core engine and its operations, and core's own console pages (`/`, `/config`, `/jobs`, `/stats`). |
| `CLI_` | The command line. |
| `MCP_` | The MCP server and its settings. |
| `PLUGIN_` | A plugin: a provider, an embedder, an LLM backend, a vector store. A plugin prompt must not change core beyond the touch points it lists. |
| `ENRICHER_` | The enrichers that run at ingest. |
| `WEBUI_` | The web UI, `oaken-webui`: its own product and its own thin client. It never lives inside a core prompt. |
| `DOCS_` | The documentation site and the public docs. |

**Which prompts are built, partly built or open is kept in
[`CONTRIBUTING.md`](../../CONTRIBUTING.md#picking-up-a-prompt)** (published on the docs site). Update it
in the same change that builds a prompt.

| # | Prompt | Delivers | Depends on |
|---|---|---|---|
| 01 | [bootstrap](CORE_01-bootstrap.md) | Clean repo, Apache-2.0, CI, secret scanning, legacy-data safeguard | — |
| 01a | [docs-site-scaffold](DOCS_01a-docs-site-scaffold.md) | Generated GitHub Pages site (Libra look), build/check tooling, Pages deploy | 01 |
| 02 | [core-foundation](CORE_02-core-foundation.md) | Settings, paths, logging, errors, IDs, clock, DB access layer | 01 |
| 03 | [events-and-jobs](CORE_03-events-and-jobs.md) | Event bus, durable job queue, scheduler | 02 |
| 03b | [swarm-foundations](CORE_03b-swarm-foundations.md) | Optional external agents: scoped tokens, quotas, validated results, fan-out and attribution | 03 |
| 04 | [plugin-host](CORE_04-plugin-host.md) | Plugin contracts (5), host, fakes, contract tests | 03 |
| 05 | [raw-store](CORE_05-raw-store.md) | `raw.db`, blob store, migrations, accounts/messages/cursors | 02 |
| 06 | [parsing-and-cleaning](CORE_06-parsing-and-cleaning.md) | RFC 822 → structured, cleaned text; threading keys | 05 |
| 06c | [sieve-filtering](CORE_06c-sieve-filtering.md) | Sieve scripts organize mail into labels, flags and a disposition, stored in `raw.db` | 05, 06 |
| 07 | [index-and-fts](CORE_07-index-and-fts.md) | `index.db`, chunks, FTS5, rebuild-from-raw | 06 |
| 08 | [vector-store](PLUGIN_08-vector-store.md) | sqlite-vec spike + VectorStore plugin | 04, 07 |
| 09 | [ingest-pipeline](CORE_09-ingest-pipeline.md) | Staged, resumable pipeline orchestrating the AI backend | 03–08 |
| 10 | [ollama-and-prompt-safety](PLUGIN_10-ollama-and-prompt-safety.md) | Ollama plugin, `UntrustedText`, structured outputs, LLM cache | 04, 09 |
| 11 | [gmail-plugin](PLUGIN_11-gmail-plugin.md) | Gmail provider: BYO OAuth, resilient tokens, sync, flags, labels, trash, send | 04, 05, 09 |
| 11b | [smtp-send](PLUGIN_11b-smtp-send.md) | SMTP send-as provider: sends only, password in the keyring, TLS required | 04, 11 |
| 11c | [imap-provider](PLUGIN_11c-imap-provider.md) | Plain IMAP provider (to be settled in discussion) | 04, 05, 09 |
| 12 | [read-state](CORE_12-read-state.md) | `mark_read` on open/summarize, write-back, audit + undo | 11 |
| 12b | [sync-scheduling](CORE_12b-sync-scheduling.md) | Core loads providers and syncs on a per-account schedule held in the database | 03, 11c, 12 |
| 13 | [operation-registry](CORE_13-operation-registry.md) | Typed operations; permission classes; confirm gate | 09, 12 |
| 14 | [enrichers](ENRICHER_14-enrichers.md) | Summary, triage, actions, header/MIME inspector | 10, 13 |
| 15 | [retrieval-and-ask](CORE_15-retrieval-and-ask.md) | Hybrid search, token-budgeted context, cited Q&A, eval harness | 08, 14 |
| 16 | [api-daemon](CORE_16-api-daemon.md) | Local HTTP + SSE, token auth, `start/stop/status` | 13 |
| 17 | [cli-client](CLI_17-cli-client.md) | Thin CLI over the API | 16 |
| 18 | [mcp-server](MCP_18-mcp-shim.md) | MCP server hosted by core (+ `oaken mcp` stdio bridge), generated from registry | 16 |
| 19 | [security-hardening](CORE_19-security-hardening.md) | Injection suite, localhost threat tests, audit review | 16–18 |
| 20 | [oss-docs](DOCS_20-oss-docs.md) | README, site content, plugin guide, ADRs, roadmap; site verified | all |
| 21 | [webui-server](WEBUI_21-webui-server.md) | `oaken-webui`: a thin client that serves the UI and proxies `/v1/*` to core (core stays headless) | 16, 17 |
| 21b | [webui-client-foundation](WEBUI_21b-webui-client-foundation.md) | The UI function ⇄ operation map, the contract gaps, and the shared client JS (api seam, safe text renderer, sidebar/list/reader, capability gate, sync button) | 13, 16, 21 |
| 22 | [api-parity](CORE_22-api-parity.md) | Expand the API for the UI; audit registry ⇄ CLI ⇄ UI; enforce 1:1 CLI/UI parity (one backend) | 13, 16, 17, 21 |
| 23 | [plugin-settings](CORE_23-plugin-settings.md) | Plugins only: per-plugin TOML textarea load/test/save, enable/disable, a `capabilities` allow-list, write-only secret rows, the shared config-section writer and live settings reload. Control is core's (`oaken plugins`), not the plugin's | 04, 13, 21, 22 |
| 24 | [accounts-onboarding](CORE_24-accounts-onboarding.md) | Add/auth/test/configure accounts; Gmail "listen but not send" (receive-only) | 11–12b, 23 |
| 25 | [folders-labels-counts](CORE_25-folders-labels-counts.md) | Sidebar chrome: folders, labels and unread counts (new `mailbox_counts` op) | 07, 12, 22 |
| 26 | [sieve-management](CORE_26-sieve-management.md) | Manage Sieve in the UI: list/check/test + a write path (`sieve_save`/`delete`, never over MCP) | 06c, 22 |
| 27 | [contacts-people](CORE_27-contacts-people.md) | People view derived from mail (`list_people`/`get_person`); derive-vs-store ADR | 06, 07, 22 |
| 28 | [compose-and-send](CORE_28-compose-and-send.md) | Identities, `send_message` (permission `send`, never MCP), outbox job, the one compose controller in the UI | 11, 11b, 21b, 22, 24, 27 |
| 29 | [system-settings](CORE_29-system-settings.md) | Core settings, storage figures, vacuum, cache stats/clear, event tail, MCP exposure link | 13, 21b, 22, 23 |
| 30 | [pipeline-management](CORE_30-pipeline-management.md) | Manage the queue: job list with ids, pause/resume, retry, cancel, live concurrency, interactive-over-background priority | 05, 13, 17, 21b, 22 |
| 31 | [workspace-management](CORE_31-workspace-management.md) | List/get/check/set/remove workspaces (Oaken's confidentiality levels) from the UI and CLI; `[workspaces.*]` stays the one source of truth | 10, 13, 22, 23 |
| 32 | [mcp-settings](MCP_32-mcp-settings.md) | Settings → MCP: the `[mcp]` section in a textarea (`allow_send`, `workspaces=[]`, enable/disable), operation list with presets, workspace scoping, live reload | 13, 18, 22, 23, 29, 31 |
| 33 | [agent-enrichment](CORE_33-agent-enrichment.md) | Let an external model (LM Studio, 27B) enrich a message through `/mcp/agent`: claimable payload with text and instructions, a real result type, storage, cache and workspace rules | 03b, 05, 13, 18, 31, 32 |
| 34 | [stabilize-core](CORE_34-stabilize-core.md) | Lock the public surface (snapshot tests + versioning ADR), enforce import boundaries (import-linter), turn `Core.open()` into dependency-ordered service units, let plugins declare+validate config, and add the missing extension hooks (job kinds, pipeline stages, scheduled tasks) with a conformance suite — so a second dev builds features without touching core | 02, 04, 13, 16, 23 |
| 35 | [onboarding-0-1-1](CORE_35-onboarding-0-1-1.md) | The record of the 0.1.1 work: first-run defaults, core's console pages, plugin-first commands, plugin control under `oaken plugins` | 16, 17, 23, 34 |
| 36 | [after-0-1-1](CORE_36-after-0-1-1.md) | What 0.1.1 left: label/folder write-back, workspace routing by Sieve rule, a datastore per account, send, Gmail sign-in, concurrent plugin start | 28, 31, 35 |
| 37 | [gmail-sign-in](PLUGIN_37-gmail-sign-in.md) | Gmail sign-in as the standard consent flow: permissions that follow capabilities, `oaken gmail doctor`, a clearer consent experience, and a private copy of the client JSON. Written, not built | 11, 23, 24, 35 |
| 38 | [openai-compat-lmstudio](PLUGIN_38-openai-compat-lmstudio.md) | `oaken_openai_compat`: an LLM backend and embedder over any OpenAI-compatible server, with LM Studio as the preset; plus a docs page for using Oaken from ollmcp or LM Studio as MCP clients. Written, not built | 04, 08, 10, 31 |

## Not written yet (deliberately)

- **Legacy-DB importer** (only if wanted), **packaging**
  (systemd/launchd, installers), **at-rest encryption**, **plugin sandboxing**,
  **embedding-throughput tuning on CPU**.

The **Web UI** is its own track (`WEBUI_21`, `WEBUI_21b`; start with 21b, the map). The UI halves of 23–29 are to be written as `WEBUI_` prompts, not inside the core ones: its own thin-client app (`oaken-webui`)
that serves the UI and proxies the general API, plus the registry operations each UI feature needs,
built with 1:1 CLI/UI parity (one backend).

## Decisions recorded while planning

- `raw.db` also holds the durable operational tables (job queue, audit log,
  sync cursors, read-state intents), so there are exactly **two** database files.
  Only `index.db` is disposable.
- Plugin extension points: Provider, Embedder, LLMBackend, Enricher, VectorStore.
- Clients reach core over HTTP on `127.0.0.1` + SSE; the CLI does **not** auto-start
  core; MCP is a stdio shim; core serves the built UI files (revisit at the UI phase).
