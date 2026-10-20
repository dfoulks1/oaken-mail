# 17 — CLI client

**Read first:** `docs/prompts/00-context.md`. **Depends on:** 16.

## Goal

A thin command-line client over the local API. It contains **no business logic**:
every command is a call to a registry operation. The CLI is also the reference
client other clients (UI, MCP) are compared against.

## Requirements

1. **Discovery:** read `runtime.json` for the port and `api_token` for the token (paths
   from settings/`OAKEN_HOME`). If core isn't running: print a one-line message
   (`core is not running — start it with "oaken start"`), exit code `3`. **Do not
   auto-start core.** `--url/--token-file` overrides for dev.
2. **Commands** (Typer or argparse — choose, justify; keep startup fast, lazy imports):
   `start|stop|status|restart` (from 16), `sync [--wait]`, `search QUERY [--from … --since … --unread --json]`,
   `ask QUESTION [--json]`, `show MESSAGE_ID [--raw|--headers]`, `summarize MESSAGE_ID`
   (marks read per policy — print a clear note when it does), `read|unread IDS…`,
   `undo AUDIT_ID`, `watch` (stream SSE events as lines or JSON), `pipeline`
   (stage counts, ETA, parked poison messages), `usage` (token accounting),
   `plugins`, `reprocess STAGE`, `rebuild-index`, `auth gmail` (runs the plugin's
   interactive auth through core), `ops` (list operations from `/v1/ops`).
   Commands for new operations should be **auto-registered from `/v1/ops`** as a generic
   `oaken op NAME --json '{…}'` fallback, so a registry change never requires a CLI release.
3. **Output:** human-readable by default (tables via `rich`, color only when a TTY),
   `--json` everywhere for scripting, stable exit codes (0 ok, 1 error, 2 usage,
   3 core down, 4 auth, 5 needs-confirmation). Message content printed to a terminal
   is **sanitized of control/escape sequences** (mail is untrusted — terminal injection).
4. **Confirmation UX:** mutating commands from a human at a TTY execute directly;
   `--yes` is not needed for read-state ops (explicit user command). Bulk mutations
   (>25 messages) print a count and require `--yes`.
5. Shell completion generation, `--help` text auto-derived from operation summaries.

## Acceptance criteria

- CLI tests run against the in-process ASGI app (no real daemon) and one end-to-end
  test against a real booted daemon on a free port.
- Core-down → exit 3 with the exact message; wrong token → exit 4.
- ANSI/OSC escape sequences and bidi-override characters inside a message subject/body
  are neutralized in terminal output; `--json` preserves the original data.
- `oaken summarize` output explicitly states that the message was marked read and
  shows the audit id for undo.
- Startup time for `oaken --help` under 300 ms on this machine (measure and report).

## Out of scope

Interactive TUI, compose/send, anything that duplicates server logic.

## Docs site

Register `docs/tools/gen_cli_reference.py`, generating a *Reference → CLI* page by walking
the real CLI definition (commands, options, exit codes) — not a hand-written copy.
`check_docs.py` must pass.

## Checkpoint

Standard report. Include sample sessions (copy-paste of real CLI output against the fakes).

## Swarm commands (from prompt 03b)

Add `oaken agents create|list|revoke` and `oaken jobs claim|heartbeat|complete|fail` so a
scripted worker can lease jobs. `agents create` prints the token once, with a warning that it
cannot be shown again. See `docs/architecture/swarm.md`.

## Note from prompt 05

Add an **administrative sub-section** (`oaken admin ...`) with an account wizard. It asks the
questions, then **stores** the answers through `RawStore.create_account` / `set_account_config`,
so the user never edits a file by hand. Only non-secret settings go in `config_json`; credentials go
to the OS keyring (the store refuses secret-looking keys).

## Note from prompt 06c

Add `oaken sieve check FILE` (`compile_script`), `oaken sieve test FILE --message ID|--eml PATH`
(`run`, showing each effect and its `script:line`), and `oaken sieve refilter` (a job).
