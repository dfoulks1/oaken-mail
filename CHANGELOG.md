# Changelog

All notable changes to Oaken are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). All three distributions (`oaken`, `oaken-cli`,
`oaken-core`) release in lockstep at one version (ADR 0014).

## [0.1.1] — Unreleased

Makes a fresh install usable without reading the docs.

### Added

- Triage is its own first step and its own plugin kind, `triager` (`oaken.triagers`, `BaseTriager`,
  `LLMTriager`). Two ship by default: `rules` (header and message rules, no model) and `ai` (a
  model); they run in `rank` order and the first verdict wins, stored under `triage`. New rules
  settle cron output, auto-replies and `X-Spam-Status`.
- One job kind per enricher, `ingest.enrich.<name>`, with `ingest.triage` for the gate. Triage fans
  out: embedding and the enrichers with a `min_priority` are queued once it has rated the message.
  An agent claims one enricher's job and answers just that. The old `ingest.enrich` job drains into
  the new kinds. Pause, limits and status reach each enricher (`oaken pipeline pause enrich.summary`,
  `oaken pipeline --by-enricher`).
- `[ingest] embed_min_priority` (default 1): mail triage rates below it is not embedded, and stays
  keyword-searchable.
- A `MessageTriaged` event.
- **The trace** (`oaken trace`), opt-in because it holds mail text: with the full text, every call into
  a plugin of any kind, every model call (system text, mail as sent, response, thinking, Ollama's
  timings and token counts, tokens a cache hit saved), the jobs and what they stored, and the agent
  door (the prompt an agent was handed, its heartbeats, the answer it committed, and any thinking
  or tool calls it reports). `oaken trace -f plugin.<kind>.<name>` follows one plugin and `--job ID`
  one job, pushed as each event is emitted (a `TraceEvent` on the bus that only explicit trace
  streams receive, over the authenticated `GET /v1/trace/stream`). `[log] trace`, `oaken trace on|off`, a 0600 `trace.jsonl`, the authenticated API only,
  never MCP or a console page. `LLMResponse` gains optional `thinking` and `meta`; every plugin
  built on a base class traces itself, and `self.trace(...)` adds your own events.
- The console is organised by stage and loads lazily. The masthead reads General, Pipeline, Triage,
  Embed, Enrich, Config, Capabilities and Docs. `/` is the front page (counts and the daemon's
  environment). **General** (`/general`) holds Ingest (every stage with its own pause, shown as a pill
  switch, plus live throughput), Queue (the active jobs by default; the old Jobs page is gone and
  `/jobs` leads here), and the Audit log. **Pipeline** holds the plugins behind
  the stages: Providers, LLM backends, Vector stores and Caches. **Triage**, **Embed** and
  **Enrich** each have everything about the stage on one page: its controls, the plugins of that
  kind (enable switch, model, settings, reload, and a pause for each enricher), the stage's own
  core settings (`ai.triage_gate` on Triage, `ingest.embed_min_priority` on Embed), the model and
  token usage for the stage, and the agents working it. The usage that belongs to no stage sits on
  Pipeline > LLM backends. **Capabilities** lists the
  operations grouped by plugin kind, with an MCP switch for each. Each page loads only its own data,
  and the queue numbers fill in after the page paints, so pages stay quick on a busy machine.
  `parse` can be paused and limited like the other stages. Buttons are icons (a gear to configure, a
  recycle to reload, a check to apply) with their words kept as hover text and screen-reader
  labels. A provider's gear opens its **schedule manager**: each account's interval or cron, timezone
  and quiet hours, a pill to pause its sync, and a sync-now icon. A triager made of several checks
  (the `rules` triager) lists them in order with a pill each, and its values are editable. The
  front page shows the sync state (backfill and model stages paused or running, each account's next
  and last run). Paths were tidied: `/stats` is `/general` (and `/stats.json` `/general.json`),
  `/getting_started` is `/getting-started`; those old paths redirect, keeping the query string.
  `/jobs/pipeline` is now `/pipeline.json` and does not redirect. `pipeline_status` takes optional
  `stages` and `counts` to ask for less.

- **A master MCP switch.** `[mcp] enabled` (default true) and a big ON/OFF toggle on Capabilities: off, the person's MCP door answers 503 and offers no tools. The door itself is still `[api] mcp` and a restart. The Capabilities tables fold, and core operations are grouped Mail, Pipeline, Enrichment and System, so the Enrichment table can be switched off to stop an AI client re-running the analysis.

- A live welcome page at `/`, a config editor at `/config`, a jobs and pipeline console,
  an in-app getting-started page and a shared nav, all on loopback. The `/stats` page now uses the
  same Bootstrap look. Four new operations back the editor: `config_get`, `config_validate`,
  `config_save` and `config_reload`.
- `oaken start` prints where the console is.
- Three shortcut commands, each a slice of `oaken`: `mill` (the daemon), `sawyer` (sync, the
  pipeline, jobs and schedules) and `cubby` (search, reading and filing mail).
- A commented default `config.toml` is written on first start (never over an existing file).
- Core remembers the last `OAKEN_HOME` it ran under. If the variable is unset it uses that home
  and logs a warning with the line to add to your shell profile.

- `capabilities = [...]` in a plugin's table narrows what it may do (Gmail receive-only, for
  example), and a plugin can ship switched off with `enabled_default = False`. `list_plugins`
  reports both.
- Plugin control is core's, under `oaken plugins`: `enable`, `disable`, `reload`, `config`, `check`
  and `set` (`oaken plugins set imap port=993`). A plugin offers none of these itself. The stats
  page's Plugins tab has an enable switch and a Reload button, and every one of these is audited.
- `oaken plugins check` tries a plugin's settings on a throwaway instance before they are saved;
  `oaken plugins config` and `set` read and write one `[plugins.<kind>.<name>]` table, keeping the
  comments and the rest of `config.toml` (new dependency: `tomlkit`).

- `oaken settings` shows and changes core's own settings (`index`, `retrieval`, `read_state`,
  `ui`, `sync`, `ai`, `log`) one table at a time, keeping your comments: `get`, `check` and `set`
  (with `--unset` and `--reload`). It reports what a change needs (a reload, a rebuild-index), and
  when an environment variable overrides a key. Operations: `get_settings`, `check_settings`,
  `set_settings`.
- `oaken storage` and `oaken vacuum` (`--wait` shows the sizes before and after). `vacuum` runs as a
  job, one at a time, refuses during an index rebuild or without room on the disk.
- `oaken cache` (stats) and `oaken cache clear [--task T]`: only derived model answers are touched.
- `oaken events` reads a persisted event log (`events.jsonl`, `[log] events_file`, owner-only and
  rotated); `--follow` keeps watching. `oaken audit` gains `--since`, `--cursor` and `--all`.
- `oaken status` adds a health summary when core answers.

### Changed

- A plugin that cannot load is switched off, with the reason shown, and so is one that needs
  settings it has not been given: `requires_config = ("base_url",)` on a plugin class keeps it off
  until its table has them (`ollama-remote` needs `base_url`), and it starts by itself once they are
  set. A plugin you turned off yourself stays off. Every plugin with options has a settings window,
  active or not (a second LLM backend included), and a provider's per-account instances
  (`gmail@account`) are listed under the provider, not beside it.
- Plugin settings are `[plugins.<kind>.<name>]` (`[plugins.triager.ai]`,
  `[plugins.enricher.summary]`, `[plugins.provider.gmail]`). The flat `[plugins.<name>]` still
  works and logs a warning; `oaken plugins set` writes the qualified form, and a name two kinds
  share (`ollama`) is now unambiguous. **Upgrade:** `[plugins.triage]` becomes `[plugins.triager.ai]`
  (drop `skip_bulk`).
- List headers (`List-Id`, `List-Unsubscribe`, `Precedence: list`) no longer settle triage at
  priority 0: a person can reply through a list, so list mail goes to the model.
- `oaken start` and `oaken restart` run core in the background by default; `--fg` keeps it attached
  to the terminal (`--detach` is still accepted). `oaken stop` says what it is waiting for, gives
  core `--grace` seconds (40 by default), then kills it and clears its runtime file; `--no-kill`
  reports an error instead. `restart` takes the same flags.
- A reload keeps the port core was already listening on when `[api] port` is `0`, so the stats page
  and other clients stay put.
- A plugin that is installed but has no `[plugins.<kind>.<name>]` settings (Gmail, IMAP, SMTP) is reported
  as "not configured" instead of logging an error.
- `sqlite-vec` is a core dependency, so the default vector store works after a plain install.

### Fixed

- The throughput chart no longer carries an inline `style=` that the CSP refuses; the
  plugin and stage tables no longer wrap their cells mid-word.

- `test_a_fixture_with_no_matching_enricher_is_reported` read the developer's real config and a
  running Ollama; it is now hermetic.

## [0.1.0] — 2026-10-22

**First public beta.** `0.y.z` means the surfaces may still change; see
[ADR 0010](docs/adr/0010-the-stable-surface-and-its-versioning.md) for what is held stable and how.

### The product

- **Oaken Core** — a local-first, AI-native mail engine: the ingestion & enrichment pipeline, the
  CLI, and the MCP server, so an AI can work with your mail while the mail stays on your machine.
  `pip install oaken-core` installs the whole product (ADR 0014).

### Added

- Ingestion & enrichment pipeline: sync → parse/clean/index (FTS5 + vectors) → embed → enrich
  (summary, triage, actions, header/MIME inspector), staged, idempotent and resumable.
- Local HTTP API daemon on `127.0.0.1` with token auth and a hardened request guard (ADR 0005),
  and the MCP server at `/mcp` — the AI-facing surface, confirm-gated and audited (ADR 0004).
- Providers: Gmail, generic IMAP, SMTP (send). Local AI via Ollama; sqlite-vec vector store.
- The `oaken` CLI: start/stop/status/reload, accounts and auth, sync, search and ask, schedules,
  pipeline control, agents, and the `oaken mcp` stdio bridge.
- A read-only **`/stats`** status page (loopback-only, counts/health, audit and operation
  reference), so you can see what core is doing without the shell (ADR 0013).
- Plugin extension hooks — job kinds, pipeline stages and scheduled tasks — with a conformance
  suite, so features can be added without touching core (ADR 0012).
- Optional log-to-file with rotation (`[log] file`).

### Stability & boundaries

- The public surface (operation schemas, plugin contracts, event shapes) is snapshot-tested and
  versioned (ADR 0010); import boundaries between core, plugins and clients are enforced by
  import-linter; the composition root is organised into dependency-ordered service units
  (ADR 0011).

### Known limitations

- Pre-alpha→beta: expect rough edges. Setting plugin/system config from a client is not built yet
  (edit `config.toml`). Off-host access (e.g. a phone) is not available — core is loopback-only
  (ADR 0005); a networked, authenticated surface is a future decision (ADR 0014).
- Embedding/enrichment need a local model (Ollama); on modest hardware the model-bound stages are
  slow, and `/stats` shows when the pipeline is backed up.

[0.1.0]: https://github.com/dfoulks1/oaken-mail/releases/tag/v0.1.0
