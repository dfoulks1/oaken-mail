# Oaken Mail

Oaken Mail is a local-first, AI-native mail engine. Mail is pulled to local disk, cleaned and
filtered by your own Sieve rules, indexed for keyword and meaning search, and enriched by a local
model, so that search and question answering are cheap in tokens. Nothing leaves your machine
unless you explicitly configure a remote endpoint. Its headline offering is a local **MCP server**
over that store, so a stronger model can work on sensitive mail without the mail leaving your
machine.

**Status: beta (0.1.x).** The engine, the CLI, the MCP server and the Gmail, IMAP and SMTP
providers work today. Sending mail and several settings screens are still to
come: see the [roadmap](ROADMAP.md). Surfaces may change before 1.0; the
[versioning ADR](docs/adr/0010-the-stable-surface-and-its-versioning.md) says what is held stable.

- Source: <https://github.com/dfoulks1/oaken-mail>
- Website: <https://dfoulks1.github.io/oaken-mail/>
- License: [Apache-2.0](LICENSE)

## How it fits together

```
 Gmail / IMAP / SMTP          plugins (providers, models, enrichers, vector store)
        │ pull                          │
        ▼                               ▼
   ┌─────────────────────────  oaken core (one daemon, loopback only)  ───────────────┐
   │  raw store ─▶ parse ─▶ sieve ─▶ index + vectors ─▶ enrich ─▶ search / ask          │
   │  operation registry ── one door ──▶  HTTP API · CLI · MCP · web console           │
   └────────────────────────────────────────────────────────────────────────────────────┘
        raw.db (the truth)   index.db (rebuildable)   keyring (credentials, never files)
```

Every capability is one registered operation. The CLI, the web console, the HTTP API and the MCP
server are all generated from the same registry, so there is no second path to anything. Mail text
is data, never instructions, and anything that changes your mail is confirmed first.

## Privacy

Local by default. The only network traffic is to your mail provider and, if you point Oaken at one,
a remote model. Credentials live in your OS keyring, never in a file. The API binds `127.0.0.1`
unless you opt in to a remote bind, and the console pages are served to this machine only. Sending
mail is off over MCP until you turn it on. See [SECURITY.md](SECURITY.md).

## Quick start

You need Python 3.11 or newer and [Ollama](https://ollama.com) for the local models.

```bash
pip install oaken-core                 # or from a checkout: git clone … && uv sync
ollama pull nomic-embed-text && ollama pull qwen2.5:3b

oaken start                            # in the background; prints where the console is (/, /config, /stats)
oaken admin account add                # asks for the provider, an id and its settings
oaken gmail auth                       # or: oaken imap auth (asks for the password, no echo)
oaken sync --wait                      # pulls from every account on every provider
oaken search "invoice"
oaken mcp                              # point a stdio MCP client (Claude Desktop) at this
```

Set `OAKEN_HOME=~/.oaken` in your shell profile to keep everything under one folder; core warns
and tells you the line to add if it is unset. A commented `config.toml` is written on first start;
edit it at `/config`. The full walkthrough is in
[Set up Oaken from scratch](docs/getting-started.md), and `oaken help` lists every command. Three shortcuts take a slice each: `mill` (the daemon),
`sawyer` (the mail flow) and `cubby` (the mailbox).

## Go further

- [Plugins](docs/plugins.md): providers, models and enrichers are plugins; a provider can offer its
  own commands (`oaken gmail auth`, `oaken PLUGIN VERB`).
- [Architecture](docs/architecture/overview.md) and the [ADRs](docs/adr).
- [Contributing](CONTRIBUTING.md), including which numbered build prompts are done or open.
