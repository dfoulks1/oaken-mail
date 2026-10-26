# Oaken Mail prompt series

Self-contained prompts for building Oaken Mail with Claude Code, one per session. Copy this
directory into the `oaken-mail` checkout (`docs/prompts/`) first. Start every session with: *"Read
`docs/prompts/00-context.md`, then execute `docs/prompts/TRACK_NN-….md`."* Each prompt ends with a
report-back checkpoint; review it before starting the next.

## Contributing

To run a prompt, ask for a feature (by writing its prompt), or change a prompt, see
[CONTRIBUTING.md](../../CONTRIBUTING.md#contributing-with-prompts). [`TEMPLATE.md`](TEMPLATE.md) is the
format for a new one.

**Status** is copied from the status table in [`CONTRIBUTING.md`](../../CONTRIBUTING.md#status-of-the-prompts), which
is the one to edit; `check_docs.py` fails if the two disagree. **Built** is in `main` with tests.
**Open** is free to take. **Partly built** and **In progress** say what is done in that table. **Written**
is a plan with no code yet.

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
| `DOCS_` | The documentation site and the public docs. |

**Which prompts are built, partly built or open is kept in
[`CONTRIBUTING.md`](../../CONTRIBUTING.md#picking-up-a-prompt)** (published on the docs site). Update it
in the same change that builds a prompt.

Grouped by track. The **number** is the permanent ID whatever the track, so numbering runs across
all tables (a `CLI_` prompt and a `CORE_` prompt do not restart the count).

### CORE — the engine and its operations

| # | Prompt | Status | Delivers | Depends on |
|---|---|---|---|---|
| 02 | [core-foundation](CORE_02-core-foundation.md) | Built | Settings, paths, logging, errors, IDs, clock, DB access layer | 01 |
| 03 | [events-and-jobs](CORE_03-events-and-jobs.md) | Built | Event bus, durable job queue, scheduler | 02 |
| 03b | [swarm-foundations](CORE_03b-swarm-foundations.md) | Built | Optional external agents: scoped tokens, quotas, validated results, fan-out and attribution | 03 |
| 04 | [plugin-host](CORE_04-plugin-host.md) | Built | Plugin contracts (5), host, fakes, contract tests | 03 |
| 05 | [raw-store](CORE_05-raw-store.md) | Built | `raw.db`, blob store, migrations, accounts/messages/cursors | 02 |
| 06 | [parsing-and-cleaning](CORE_06-parsing-and-cleaning.md) | Built | RFC 822 → structured, cleaned text; threading keys | 05 |
| 06c | [sieve-filtering](CORE_06c-sieve-filtering.md) | Built | Sieve scripts organize mail into labels, flags and a disposition, stored in `raw.db` | 05, 06 |
| 07 | [index-and-fts](CORE_07-index-and-fts.md) | Built | `index.db`, chunks, FTS5, rebuild-from-raw | 06 |
| 09 | [ingest-pipeline](CORE_09-ingest-pipeline.md) | Built | Staged, resumable pipeline orchestrating the AI backend | 03–08 |
| 12 | [read-state](CORE_12-read-state.md) | Built | `mark_read` on open/summarize, write-back, audit + undo | 11 |
| 12b | [sync-scheduling](CORE_12b-sync-scheduling.md) | Built | Core loads providers and syncs on a per-account schedule held in the database | 03, 11c, 12 |
| 13 | [operation-registry](CORE_13-operation-registry.md) | Built | Typed operations; permission classes; confirm gate | 09, 12 |
| 15 | [retrieval-and-ask](CORE_15-retrieval-and-ask.md) | Built | Hybrid search, token-budgeted context, cited Q&A, eval harness | 08, 14 |
| 16 | [api-daemon](CORE_16-api-daemon.md) | Built | Local HTTP + SSE, token auth, `start/stop/status` | 13 |
| 19 | [security-hardening](CORE_19-security-hardening.md) | **Open** | Injection suite, localhost threat tests, audit review | 16–18 |
| 22 | [api-parity](CORE_22-api-parity.md) | Built | Expand the API for the UI; audit registry ⇄ CLI ⇄ UI; enforce 1:1 CLI/UI parity (one backend) | 13, 16, 17, 21 |
| 23 | [plugin-settings](CORE_23-plugin-settings.md) | **Partly built** | Plugins only: per-plugin TOML textarea load/test/save, enable/disable, a `capabilities` allow-list, write-only secret rows, the shared config-section writer and live settings reload. Control is core's (`oaken plugins`), not the plugin's | 04, 13, 21, 22 |
| 24 | [accounts-onboarding](CORE_24-accounts-onboarding.md) | Built | Add/auth/test/configure accounts; Gmail "listen but not send" (receive-only) | 11–12b, 23 |
| 25 | [folders-labels-counts](CORE_25-folders-labels-counts.md) | Built | Sidebar chrome: folders, labels and unread counts (new `mailbox_counts` op) | 07, 12, 22 |
| 26 | [sieve-management](CORE_26-sieve-management.md) | Built | Manage Sieve in the UI: list/check/test + a write path (`sieve_save`/`delete`, never over MCP) | 06c, 22 |
| 28 | [compose-and-send](CORE_28-compose-and-send.md) | **Open** | Identities, `send_message` (permission `send`, never MCP), outbox job, the one compose path | 11, 11b, 22, 24 |
| 29 | [system-settings](CORE_29-system-settings.md) | **Partly built** | Core settings, storage figures, vacuum, cache stats/clear, event tail, MCP exposure link | 13, 22, 23 |
| 30 | [pipeline-management](CORE_30-pipeline-management.md) | Built | Manage the queue: job list with ids, pause/resume, retry, cancel, live concurrency, interactive-over-background priority | 05, 13, 17, 21b, 22 |
| 31 | [workspace-management](CORE_31-workspace-management.md) | **Partly built** | Ops + CLI built (`list/get/check/set/remove_workspace`, `oaken workspaces …`), reusing 23's section writer + live reload. Owed: `set_workspace` must enforce the `secure`⇒local-backend rule itself (today only `check` does — a save can skip it), and the Settings → Workspaces pane. `effective_backend`/`used_by.mcp` and the MCP-name removal guard wait on 32. `check_workspace` is `mcp="never"` | 10, 13, 22, 23 |
| 33 | [agent-enrichment](CORE_33-agent-enrichment.md) | **Built** | An external model enriches through the agent door: claimable payload with the fenced message, owed enrichers and schemas; `complete_job` validates and stores `{enrichments}` as `agent:<label>`; offered mail follows `[mcp] workspaces`. Live LM Studio demo not done | 03b, 05, 13, 18, 31, 32 |
| 34 | [stabilize-core](CORE_34-stabilize-core.md) | Built | Lock the public surface (snapshot tests + versioning ADR), enforce import boundaries (import-linter), turn `Core.open()` into dependency-ordered service units, let plugins declare+validate config, and add the missing extension hooks (job kinds, pipeline stages, scheduled tasks) with a conformance suite — so a second dev builds features without touching core | 02, 04, 13, 16, 23 |
| 35 | [onboarding-0-1-1](CORE_35-onboarding-0-1-1.md) | Built | The record of the 0.1.1 work: first-run defaults, core's console pages, plugin-first commands, plugin control under `oaken plugins` | 16, 17, 23, 34 |
| 36 | [after-0-1-1](CORE_36-after-0-1-1.md) | **Open** | What 0.1.1 left: label/folder write-back, workspace routing by Sieve rule, a datastore per account, send, Gmail sign-in, concurrent plugin start | 28, 31, 35 |
| 40 | [mcp-allowed-config](CORE_40-mcp-allowed-config.md) | **Written** | `[mcp.allowed]` in config.toml as the source of truth for MCP exposure (seeded to the store on load, file wins), a generated commented menu, op write-back, and a toggle on `/stats` → Reference. The plan only, not built | 13, 16, 18, 29 |
| 41 | [stats-controls](CORE_41-stats-controls.md) | **Built** | Configure AI from `/stats`: enable/disable, clear-usage, agent list/revoke, enricher `config_model` forms, a backend model picker and a per-enricher model field, both fed by `available_models()` (CORE_42 honors the per-enricher model). All controls `mcp="never"` | 13, 16, 23, 29, 40 |
| 42 | [per-enricher-models](CORE_42-per-enricher-models.md) | **Built** | Each enricher on its own model: `generate(request, *, model=…)` on `BaseLLMBackend` (bumped `PLUGIN_API_VERSION` 1→2, ADR 0010), the effective model carried into the cache key + usage (no `backend.model_id` reach-back, two models cache apart), `EnricherConfig.model` in the `/stats` form fed by `available_models()`. Unknown model warns + falls back. Per-model `context_window` is the documented follow-on. Carries the effective model to PLUGIN_43's cache slot | 41, 10, 43 |
| 44 | [workspace-ops-fixes](CORE_44-workspace-ops-fixes.md) | **Built** | Close the save-path confidentiality gap in `set_workspace` (one validator, refuses a secure workspace on a remote backend); honest `cache_entries`; two dead branches cut | 31 |
| 48 | [ask-router](CORE_48-ask-router.md) | **Built** | `ask` works out what was asked (summary, what needs me, actions, find, factual) with a small model that never sees mail, then runs the right read path. Enrichers extend its menu with an `AskIntent` | 13, 14, 15 |

### CLI — the command line

| # | Prompt | Status | Delivers | Depends on |
|---|---|---|---|---|
| 17 | [cli-client](CLI_17-cli-client.md) | Built | Thin CLI over the API | 16 |
| 39 | [ask-debug](CLI_39-ask-debug.md) | **Written** | `--debug` on a CLI command: run it in-process with DEBUG logs to the terminal and the logfile; every plugin obeys one shared logger. The plan only, not built | 16, 17 |

### MCP — the MCP server and its settings

| # | Prompt | Status | Delivers | Depends on |
|---|---|---|---|---|
| 18 | [mcp-server](MCP_18-mcp-shim.md) | Built | MCP server hosted by core (+ `oaken mcp` stdio bridge), generated from registry | 16 |
| 32 | [mcp-settings](MCP_32-mcp-settings.md) | **Partly built** | `[mcp] workspaces` scoping by the mail's account, `mcp_locked_reason`, a status line and picker on `/stats`. Search, list, ask, labels and counts are refused, not filtered, under a restricted scope; send waits for 28 | 13, 16, 17, 18, 22, 29, 31, 40 |

### PLUGIN — providers, models, vector store

| # | Prompt | Status | Delivers | Depends on |
|---|---|---|---|---|
| 08 | [vector-store](PLUGIN_08-vector-store.md) | Built | sqlite-vec spike + VectorStore plugin | 04, 07 |
| 10 | [ollama-and-prompt-safety](PLUGIN_10-ollama-and-prompt-safety.md) | Built | Ollama plugin, `UntrustedText`, structured outputs, LLM cache | 04, 09 |
| 11 | [gmail-plugin](PLUGIN_11-gmail-plugin.md) | Built | Gmail provider: BYO OAuth, resilient tokens, sync, flags, labels, trash, send | 04, 05, 09 |
| 11b | [smtp-send](PLUGIN_11b-smtp-send.md) | Built | SMTP send-as provider: sends only, password in the keyring, TLS required | 04, 11 |
| 11c | [imap-provider](PLUGIN_11c-imap-provider.md) | Built | Plain IMAP provider (to be settled in discussion) | 04, 05, 09 |
| 37 | [gmail-sign-in](PLUGIN_37-gmail-sign-in.md) | **Written** | Gmail sign-in as the standard consent flow: permissions that follow capabilities, `oaken gmail doctor`, a clearer consent experience, and a private copy of the client JSON. Written, not built | 11, 23, 24, 35 |
| 38 | [openai-compat-lmstudio](PLUGIN_38-openai-compat-lmstudio.md) | **Written** | `oaken_openai_compat`: an LLM backend and embedder over any OpenAI-compatible server, with LM Studio as the preset; plus a docs page for using Oaken from ollmcp or LM Studio as MCP clients. Written, not built | 04, 08, 10, 31 |
| 43 | [pluggable-llm-cache](PLUGIN_43-pluggable-llm-cache.md) | **Built** | A pluggable LLM answer cache: `cache_store` kind + `BaseCache`, key built by the cache from the effective model. Default stays core's SQLite, inline. No second cache yet | 15, 42 |

### ENRICHER — the enrichers

| # | Prompt | Status | Delivers | Depends on |
|---|---|---|---|---|
| 14 | [enrichers](ENRICHER_14-enrichers.md) | Built | Summary, triage, actions, header/MIME inspector | 10, 13 |

### DOCS — the documentation site

| # | Prompt | Status | Delivers | Depends on |
|---|---|---|---|---|
| 01 | [bootstrap](CORE_01-bootstrap.md) | Built | Clean repo, Apache-2.0, CI, secret scanning, legacy-data safeguard | — |
| 01a | [docs-site-scaffold](DOCS_01a-docs-site-scaffold.md) | Built | Generated GitHub Pages site (Libra look), build/check tooling, Pages deploy | 01 |
| 20 | [oss-docs](DOCS_20-oss-docs.md) | Built | README, site content, plugin guide, ADRs, roadmap; site verified | all |

## Not written yet (deliberately)

- **Legacy-DB importer** (only if wanted), **packaging**
  (systemd/launchd, installers), **at-rest encryption**, **plugin sandboxing**,
  **embedding-throughput tuning on CPU**.


## Decisions recorded while planning

- `raw.db` also holds the durable operational tables (job queue, audit log,
  sync cursors, read-state intents), so there are exactly **two** database files.
  Only `index.db` is disposable.
- Plugin extension points: Provider, Embedder, LLMBackend, Enricher, VectorStore.
- Clients reach core over HTTP on `127.0.0.1` + SSE; the CLI does **not** auto-start
  core; MCP is a stdio shim; core serves its own console pages.
