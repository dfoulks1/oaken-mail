# 00 — Shared context (read this before every prompt)

This file is the contract that every numbered prompt in this directory assumes.
If a prompt and this file disagree, **stop and report** instead of picking one.

## Where the design lives (source of record)

This file is the contract for **how to work here**. It is not the whole design. When sources
disagree, the higher one wins, and a disagreement is a bug to report, not to resolve silently:

1. **The code and its tests** are the truth about what exists.
2. **`docs/softmap.toml`** says what is built, partial or planned, and is checked against the
   repository by the docs build. Never claim more than it says.
3. **`docs/adr/`** records the decisions that are expensive to reverse (daemon and thin clients,
   the SQLite access layer, the MCP server in core). Add an ADR for the next one.
4. **`docs/architecture/*.md`** and **`docs/plugins.md`** describe how each part behaves and what
   a plugin may rely on. They are user-visible, so they are kept current with the code.
5. **This file** holds the rules, the invariants and the answers the user has given.
6. **The numbered prompts** are the build plan. A built prompt is history; where it differs from
   the docs above, the docs above are right.

Anyone, human or AI, contributing should read this file, `CONTRIBUTING.md`, the ADRs and the
architecture overview, then the page for the part they touch.

## What we are building

**Oaken Mail** is a local-first, AI-native mail client (MUA). Naming: the product is
"Oaken Mail"; the Python package, CLI, config/data directories and env prefix are `oaken`.
Code: `github.com/dfoulks1/oaken-mail`. Project website: `https://dfoulks1.github.io/oaken-mail/`.
Mail is pulled to local disk, processed by a local LLM/embedder **at ingest
time**, and stored in a form that makes search and Q&A cheap in tokens.
Nothing leaves the machine unless the user explicitly configures a remote
endpoint. License: **Apache-2.0**.

## Architecture (decided)

```
 clients (thin)     CLI    Web UI          MCP clients (Claude Desktop, ollmcp, ...)
                     \      /                  |  streamable HTTP      |  stdio
                      \    /                   |                  `oaken mcp` bridge
                 general API (HTTP + SSE)     MCP server (/mcp)  <------+
                       \                       /      127.0.0.1; separate tokens
                        +---------+-----------+
                                  |
 core (ONE long-running asyncio process)
   ├─ operation registry   typed ops; the general API and the MCP server are generated from it
   ├─ event bus            in-memory, typed events
   ├─ job queue            DURABLE (survives crash/restart), retries, backoff
   ├─ scheduler            replaces APScheduler; per-schedule timezone
   ├─ sync schedule        per-account (and per-IMAP-folder) schedules held in raw.db
   ├─ sieve filtering      scripts organise mail into labels, flags and a disposition
   ├─ ingest pipeline      staged, idempotent, resumable; core orchestrates the AI backend
   ├─ plugin host          entry points; lifecycle, capabilities, health
   ├─ audit log            every state-changing action, with undo where possible
   └─ storage
        raw.db    source of truth: zstd RFC 822 blobs (content-addressed), provider
                  ids, sync cursors, job queue, audit log, read-state intents
        index.db  everything derived and REBUILDABLE from raw.db: parsed fields,
                  cleaned text, chunks, FTS5, vectors, enrichments

 plugins (extend core through declared capabilities; in-process; first-party only in v1)
   Provider     Gmail, IMAP (generic), SMTP (send only)
                capabilities: auth, sync.pull, flags.write, send. One INSTANCE per account.
   Embedder     (Ollama first)
   LLMBackend   (Ollama first)
   Enricher     (summary, triage, actions, header/MIME inspector)
   VectorStore  (sqlite-vec first)
```

Key rules:

1. **Core never imports provider code**, and a provider plugin never touches the
   databases. Plugins talk to core through the contract (return values and events).
2. **Core is one process, with two API surfaces.** The general API serves the CLI and
   web UI; the MCP server (the AI-enabled surface, and the heart of the product, see ADR 0004)
   is hosted by core itself and runs whether or not the web UI does. `oaken mcp` is only a
   stdio bridge to it. For tests, core also runs in-process with fakes (no daemon, no network).
3. **Raw bytes are kept.** `index.db` can be deleted and rebuilt from `raw.db`.
   Changing the embedding model means re-running a job, never re-downloading mail.
4. **AI work happens in the ingest flow**, staged so a message is keyword-searchable
   (FTS5) immediately and embedded/enriched asynchronously. There is no separate
   "analyze the mailbox after sync" pass.
5. **Token thrift is a design goal.** Clean mail at ingest (strip quoted replies,
   signatures, footers, HTML boilerplate), store chunks + vectors + derived text
   (summary, category, actions), and cache every LLM result keyed by
   `(content_hash, model, prompt_version)`. Retrieval sends only the top chunks.
6. **Mail is data, never instructions.** All mail-derived text passed to an LLM is
   wrapped in an `UntrustedText` type and kept out of system prompts. Anything that
   mutates mail is enforced by a confirm gate **in code**, not in a prompt.
7. **Read state follows what the user has seen.** Messages are marked read only on
   user-initiated "opened/presented" or user-initiated "summarize" — never by
   background ingest/enrichment. Search results and bulk reads never mark read.
8. **Sending is never exposed over MCP.** (Send itself comes in a later phase.)
9. **Many accounts from day one.** `account_id` is in every schema, and the plugin host starts one
   provider instance per account (`<plugin>@<account id>`), each with its own keyring entry. The
   user runs several Google accounts and a Fastmail account.
10. **Platform independent**: Linux, macOS, Windows. Use `platformdirs` for paths,
    `keyring` for secrets, no POSIX-only calls without a fallback.

## Data locations (never inside the repo)

| What | Where (via `platformdirs`; Linux shown) |
|---|---|
| databases, blobs | `~/.local/share/oaken/` |
| config, API token, runtime file | `~/.config/oaken/` (token file mode `0600`) |
| OAuth refresh tokens | OS keyring |

## Tech baseline

- Python **>=3.11**, managed with **uv**, `src/` layout, package `oaken`.
- `ruff` (lint+format), `mypy --strict`, `pytest` + `pytest-asyncio`.
- Async everywhere in core. SQLite via the stdlib `sqlite3` driver run in a
  dedicated executor/thread (or `aiosqlite`; choose in prompt 02 and record an ADR).
- **Tests never use the network, Ollama, Gmail, Docker or real mail.** Use the
  fakes introduced in prompt 04 (`FakeProvider`, `FakeEmbedder`, `FakeLLM`) and
  synthetic fixtures. No real email content, addresses or tokens in the repo.

## The legacy tree (read-only reference)

`/home/dfoulks/local/mailmind-unified` is the previous implementation. You may
**read** it for ideas and port logic (e.g. header/MIME heuristics in
`mailmind/analysis.py`, body extraction in `mailmind/gmail.py`). You must **not**:

- copy any file wholesale or any git history,
- open or copy `.env`, `.env.bak.*`, `token.json`, `credentials.json`, or any `*.db`,
- reference its paths from code or docs in the new repo.

The user's real mail database (`…/OllMCP/Memories/mailmind.db`) is a legacy
BM25 index with no raw bytes. It is **not** migrated. It is backed up and left alone.

## Rules for every prompt

- **Docs are part of "done".** The project website is generated from `docs/` (see
  prompt `01a-docs-site-scaffold.md`). If you add or change user-visible behavior, an
  operation, a plugin contract, a CLI command, or a policy, update the matching page and
  run `uv run --group docs python docs/tools/check_docs.py` (build + link check) before
  committing. A prompt is not finished while that check fails.
- Work only in the repository root (your `oaken-mail` checkout). Touch only the files the prompt
  lists (new test files and docs for the thing you build are always allowed).
- **Keep the CLI audit current.** If you build something an operator would run (inspect, rebuild,
  repair, list, cancel, retry, export), add a row to the table in `17-cli-client.md` in the same
  commit, with what was built and any question it raises. Do not decide the command; 17 does.
- Keep the repo green at the end: `uv run ruff check`, `uv run mypy`, `uv run pytest`.
- Commit when the prompt's acceptance criteria are met, with a message that
  describes the change (Conventional Commits). **Push only when the user asks.** Do not create remotes.
- **Tests live next to the code** (`src/<pkg>/tests/`). Run the whole suite, `ruff check`,
  `ruff format --check`, `mypy` and the docs check before you report, and show real output.
- **Keep `docs/softmap.toml` honest.** A component is `built` only when it does what it says and
  its `path` exists; otherwise `partial` or `planned`. Say plainly what "built" means in the report.
- **Never** open the legacy tree's secrets or databases, put real mail, addresses or tokens in the
  repo, or log a message body. Data locations are outside the repo, and database files and the
  quarantine folder are owner-only.
- Do not add dependencies the prompt doesn't call for without saying why in the report.
- If you are blocked by an ambiguity that changes the design, **stop and ask**.

## Decisions from checkpoints (the user's answers; prompts must follow them)

- **Event log:** a plain JSON-lines file (one JSON object per line, append-only, cheap), not a database table. Decide the format and retention in 17 with the CLI `events` command.
- **Index:** `from:` matches name or address; a non-Sieve disposition beats a script's; `in:any` stays; rebuilds are deterministic for a given order.
- **Sieve `\Seen`:** Sieve is not the user, so `\Seen` never marks read. **`discarded` mail is not hidden from MCP** (it is hidden from default search only). Sieve `redirect`, `reject`, `ereject` and `vacation` are suppressed intents, never executed. Writing Sieve scripts is not exposed over MCP.
- **Starter `10-lists.sieve`:** yes, ship one.
- **`tokenizer.json`:** a command must be able to fetch it (17).
- **Quarantine folder:** required, with permissions that reflect it (owner-only, no execute bit, files never opened by core). Attachments flagged for quarantine go there, never analysed. Build it with prompt 05/06 follow-up or 09.
- **LFS:** not encrypted for now.
- **`oaken admin`:** is a CLI command (17).
- **Plugins may subscribe to core events** (read-only, through the plugin host), so core stays extensible. Settle the contract in prompt 04's follow-up or 09.
- **Re-enrich:** a separate job from the index rebuild (the index is keyword-only); it belongs to `reprocess("enrich")` in 09.
- **Chunk text:** stays duplicated in `chunks.text` for now. Storing chunks as offsets into `docs.cleaned_text` is a later prompt (generated with the UI); not 09.
- **OAuth app mode:** the user brings their own Google OAuth client and publishes it to production (a Testing-mode app's tokens expire in 7 days). Scope is `gmail.modify` only: no permanent delete, no drafts.

### Providers

- **Use well-made third-party libraries over writing our own** (COTS over RYO), but **no GPL dependencies**: this is an Apache project.
- **Gmail** is its own provider over Google's REST API. **IMAP** stays the ultra-generic provider; Gmail over IMAP is not special-cased. **SMTP** only sends.
- Secrets live only in the OS keyring (service `oaken`; users `gmail:<account>`, `smtp:<account>`, `imap:<account>`), never in a file, config, log or exception. `config_json` on an account is non-secret only. No plaintext IMAP or SMTP; certificates are always verified.
- **A provider raises `AuthError`** (core's, in `oaken.core.errors`) when credentials are missing or refused. Core pauses that account and does not retry.
- **`pull(cursor, folder=None)`** is one function for the whole account and for one folder. Only a provider with `supports_folders = True` (IMAP) accepts a folder.
- Plugins are in-process and not sandboxed in v1. Core never imports provider code.

### Read state and the audit log

- Read state follows what the user has seen: a message is marked read only when the user opens it (after `ui.mark_read_delay_ms`, owned by core) or asks for a summary (`mcp_summarized` counts as the user reading it). `read_state.auto_mark = false` turns those off.
- **Local first.** A change sets local state and a dirty marker, is audited, then written back through a durable `readstate.write` job. A change nothing can write **stays local** and says so, until the user deals with it (a UI for that comes later).
- A system actor is refused outright. Reasons that are not the user's need `confirm=True` until the operation registry's confirm gate (13) replaces the stub.
- Every undoable change is in the audit log (`raw.db`) in the same transaction, holding ids and booleans, never mail content. The log can be exported or tee'd to a file later (`oaken audit export`; not built).
- Only `ReadState` and the API layers over it may name read state. Ingest, sync, index, Sieve and plugins may not, and none may call `set_flags`; a test enforces it.

### Sync schedule

- Schedules are **data in the database**, editable live from the UI and CLI; `config.toml` only supplies defaults for a new account. Every edit is audited.
- Interval or cron, with a timezone from the **system tz database** (`tzdata` is a Windows-only dependency). The default is every **15 minutes**; there is a floor (`sync.min_interval_seconds`, 60) enforced when a schedule is set.
- One sync at a time per account (sync-now joins it); `sync.max_concurrent` caps accounts at once; missed runs collapse into one catch-up; failures back off with jitter; an authentication failure pauses only that account.
- **Quiet hours mute alerts, never the sync.**
- Accounts may sync together or apart. That is configuration, not code. The UI shows a table of the schedules; the crontab export is read-only.


## Report-back checkpoint (end of every prompt)

End your final message with exactly these sections:

1. **Done** — what now works, as bullet points a reviewer can verify.
2. **Files** — files added/changed (paths only).
3. **How I verified** — commands run and their results (pass/fail counts).
4. **Deviations** — anything you did differently from the prompt, and why.
5. **Open questions** — decisions that need the user's input.
6. **Next prompt readiness** — anything the next prompt needs that isn't in place.

Then **stop**. Do not start the next prompt.
