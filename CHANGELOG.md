# Changelog

All notable changes to Oaken are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). All four distributions (`oaken`, `oaken-cli`,
`oaken-webui`, `oaken-core`) release in lockstep at one version (ADR 0014).

## [0.1.1] — Unreleased

Makes a fresh install usable without reading the docs.

### Added

- A live welcome page at `/`, a config editor at `/config`, a jobs and pipeline console at `/jobs`,
  an in-app getting-started page and a shared nav, all on loopback. The `/stats` page now uses the
  same Bootstrap look. Four new operations back the editor: `config_get`, `config_validate`,
  `config_save` and `config_reload`.
- `oaken start` prints where the console is.
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
  `oaken plugins config` and `set` read and write one `[plugins.<name>]` table, keeping the
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

- `oaken start` and `oaken restart` run core in the background by default; `--fg` keeps it attached
  to the terminal (`--detach` is still accepted). `oaken stop` says what it is waiting for, gives
  core `--grace` seconds (40 by default), then kills it and clears its runtime file; `--no-kill`
  reports an error instead. `restart` takes the same flags.
- A reload keeps the port core was already listening on when `[api] port` is `0`, so the stats page
  and other clients stay put.
- A plugin that is installed but has no `[plugins.<name>]` settings (Gmail, IMAP, SMTP) is reported
  as "not configured" instead of logging an error.
- `sqlite-vec` is a core dependency, so the default vector store works after a plain install.

### Fixed

- `test_a_fixture_with_no_matching_enricher_is_reported` read the developer's real config and a
  running Ollama; it is now hermetic.

## [0.1.0] — 2026-10-22

**First public beta.** `0.y.z` means the surfaces may still change; see
[ADR 0010](docs/adr/0010-the-stable-surface-and-its-versioning.md) for what is held stable and how.

### The product

- **Oaken Core** — a local-first, AI-native mail engine: the ingestion & enrichment pipeline, the
  CLI, and the MCP server, so an AI can work with your mail while the mail stays on your machine.
  `pip install oaken-core` installs the whole non-UI product (ADR 0014).
- **Oaken WebUI** — a separate product built on the same API and CLI framework (`oaken-webui`).

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
