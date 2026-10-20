# 00 — Shared context (read this before every prompt)

This file is the contract that every numbered prompt in this directory assumes.
If a prompt and this file disagree, **stop and report** instead of picking one.

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
   ├─ scheduler            replaces APScheduler
   ├─ ingest pipeline      staged, idempotent, resumable; core orchestrates the AI backend
   ├─ plugin host          entry points; lifecycle, capabilities, health
   ├─ audit log            every state-changing action, with undo where possible
   └─ storage
        raw.db    source of truth: zstd RFC 822 blobs (content-addressed), provider
                  ids, sync cursors, job queue, audit log, read-state intents
        index.db  everything derived and REBUILDABLE from raw.db: parsed fields,
                  cleaned text, chunks, FTS5, vectors, enrichments

 plugins (extend core through declared capabilities; in-process; first-party only in v1)
   Provider     (Gmail first)      auth, sync.pull, flags.write, later: send
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
9. **One account in the UI for now**, but `account_id` is in every schema from day one.
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
- Keep the repo green at the end: `uv run ruff check`, `uv run mypy`, `uv run pytest`.
- Commit when the prompt's acceptance criteria are met, with a message that
  describes the change (Conventional Commits). Do not push. Do not create remotes.
- Do not add dependencies the prompt doesn't call for without saying why in the report.
- If you are blocked by an ambiguity that changes the design, **stop and ask**.

## Report-back checkpoint (end of every prompt)

End your final message with exactly these sections:

1. **Done** — what now works, as bullet points a reviewer can verify.
2. **Files** — files added/changed (paths only).
3. **How I verified** — commands run and their results (pass/fail counts).
4. **Deviations** — anything you did differently from the prompt, and why.
5. **Open questions** — decisions that need the user's input.
6. **Next prompt readiness** — anything the next prompt needs that isn't in place.

Then **stop**. Do not start the next prompt.
