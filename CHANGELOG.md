# Changelog

All notable changes to Oaken are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). All four distributions (`oaken`, `oaken-cli`,
`oaken-webui`, `oaken-core`) release in lockstep at one version (ADR 0014).

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
