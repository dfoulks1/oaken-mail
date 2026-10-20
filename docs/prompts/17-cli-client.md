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

## CLI audit (kept up to date by every prompt, so 17 needs no audit of its own)

Everything an operator would want to run, collected from each prompt as it was built. **None of it
is decided**: each row is a question for the user when we get here. "Op" means it should be a
registry operation first (the CLI only calls it); "local" means it has to work with core stopped,
because it repairs or inspects files core would hold open. Each new prompt adds its own rows in the
same commit (see `00-context.md`).

| From | Capability (what is built) | Candidate command | Notes for the discussion |
|---|---|---|---|
| 02 | Settings with precedence and a source for each value | `oaken config show\|path\|check` | Show the effective value and where it came from. Never print secrets (there are none in settings). `check` validates a `config.toml` without starting anything. Local. |
| 02 | Platform paths, `OAKEN_HOME` | `oaken paths` | Where data, config and the runtime file are. Local. |
| 02 | Migrations with checksums, refuse a newer database | `oaken db status\|check` | Schema version of `raw.db` and `index.db`, file sizes and modes (0600), `PRAGMA integrity_check`. Local. A `db backup` (`VACUUM INTO` plus the LFS folder) is worth discussing: the user keeps the one copy of their mail here. |
| 03 | Job queue: enqueue, claim, cancel, retry, dedupe, parents and children | `oaken jobs list\|show\|cancel\|retry` | **The user wants the queue exposed.** Filter by kind, status, account and actor. Show attempts, the scrubbed last error and the children of a fan-out. Retry of a failed or parked job and a purge of finished jobs are not built: the queue has no `list`, `retry` or retention yet, so these are new operations. Decide whether cancel and retry need a confirmation. |
| 03 | Scheduler with `every` and cron (UTC) | `oaken schedules list\|add\|remove\|run-now` | `Scheduler` has `add`, `remove` and `next_run` but no `list`; `run-now` is new. Cron is UTC only. |
| 03 | Event bus (in memory, typed) | `oaken watch`, `oaken events` | **The user wants the event log exposed.** The bus keeps no history, so first decide: a durable event table (kept how long, which events, what is redacted) or only the live stream with a ring buffer. Either way an event never carries a message body. |
| 03b | Agents and leases | `oaken agents ...`, `oaken jobs claim\|heartbeat\|complete\|fail` | Already specified above. |
| 04 | Plugin host: statuses, health, capabilities | `oaken plugins [check]` | List with kind, version, capabilities and health; `check` runs the health probe now. Enable and disable per plugin is not built. |
| 05 | Accounts, non-secret config | `oaken admin account ...` | Wizard noted above. Also `list` and `show`. |
| 05 | `RawStore.stats()` | `oaken store stats` | Messages, blobs, LFS count, raw and stored bytes, ratio. |
| 05 | `gc_blobs()` | `oaken store gc [--dry-run]` | Deletes data, so `--dry-run` and a confirmation. `gc_blobs` has no dry run yet. |
| 05 | `get_raw(verify=True)` | `oaken store verify` | Re-hash every blob and report mismatches and missing LFS files. Slow on a big store, so a job. |
| 05 | Soft delete (`mark_deleted`) | `oaken message delete\|undelete ID` | There is no undelete in the store. Deleting mail is destructive to the user's view, so it needs the confirm gate. |
| 05 | Raw bytes | `oaken message export ID --eml PATH` | The only place raw bytes leave the store; **never over MCP**. Terminal output of a body is sanitized (see requirement 3). |
| 05 | Sync cursors | `oaken sync cursors [--reset ...]` | Reset forces a full resync: confirm. |
| 06 | `parse`, `clean`, `thread_key`, token counts | `oaken message parse\|clean ID` (and `--eml PATH`) | A debugging view: the headers, each segment with the rule that tagged it, the thread key and how it was derived, token counts before and after, warnings, attachment metadata and the quarantine flag. Reads from the store or a file. The most useful tool for tuning `[mail.cleaning]`. |
| 06 | Cleaner rules and `disabled_rules` | `oaken clean rules` | Name and one line for each rule, and which are disabled. |
| 06 | Tokenizer (estimate or a local `tokenizer.json`) | `oaken tokenizer info` | Which one is active and whether counts are exact. A command that fetches a `tokenizer.json` is an open question from 06 (it would be the only network call here). |
| 06c | Sieve scripts | `oaken sieve check\|test\|refilter` | Specified above. Add `oaken sieve list` (each script, its hash, and any that were skipped with the reason). |
| 06c | Labels, dispositions, flags | `oaken labels`, `oaken messages --label X --disposition Y` | `AttrStore.counts` and `message_ids` exist. Showing `discarded` mail must be explicit (`--disposition discarded`). |
| 07 | `rebuild_index` | `oaken rebuild-index` | **The user asked for this one by name.** A resumable job with progress. Candidates: `--account`, `--since`, `--wait`, `--force` (ignore the content hashes), `--dry-run` (what would be re-indexed). |
| 07 | Index status | `oaken index status` | Documents and chunks, size next to `raw.db`, `pipeline_version`, whether it belongs to this raw store, how many live messages are missing from it, and whether the chunk settings changed since it was built. |
| 07 | FTS5 integrity | `oaken index check [--repair]` | FTS5's `integrity-check`; repair is a rebuild of the FTS table from `chunks`, which is cheap and needs no mail. Local. |
| 07 | `keyword_search` with operators | `oaken search QUERY` | `from:`, `before:`, `after:`, `label:`, `in:`, `-term`, `"phrase"`, `term*`. Warnings the parser returned (an unreadable date, a lone `-term`) are printed, not hidden. |
| 08 | Vector store status | `oaken vectors status` | Models (active or building), vectors per model, file size, whether the sqlite-vec extension loads. `SqliteVecStore.stats()` and `health()` exist. |
| 08 | Model swap | `oaken vectors reembed MODEL` | `begin_model`, then embed, then `activate_model`; `reembed_plan` gives progress and a time estimate. `drop_model` for the old one needs a confirm. |
| 08 | Orphan cleanup | `oaken vectors prune` | `prune_orphans()` after a reindex. Local and cheap. |
| 08 | Several vector stores | `oaken vectors stores`, `oaken vectors split` | List each store with its size, cap, vector count and whether it is active or closed; `split` closes the active one and starts another, whenever the user says. Setting or clearing a cap (`max_mb`) belongs here too. Planned, not built; see the 09 notes. |
| 09 | `Pipeline.pipeline_status()` | `oaken pipeline status` | Queued, running, done and failed per stage, the embedding speed and a time estimate, whether a backfill is paused, and the parked jobs with their reasons. |
| 09 | Parked jobs | `oaken pipeline parked [--retry ID\|--all]` | Retrying re-queues a failed stage job. Needs a confirm for `--all`. |
| 09 | `Pipeline.reprocess(stage)` | `oaken reprocess parse\|filter\|embed\|enrich [--account A]` | One job per stage and account, at backfill priority, with progress. `embed` after an embedder change is how the model swap happens. |
| 09 | `UsageStore.usage_summary(since)` | `oaken usage [--since DATE] [--json]` | Calls, cached calls, tokens in and out and tokens saved, per stage and model. Enrichment tokens are estimates. |
| 09 | Quarantine folder | `oaken quarantine list\|stats\|check` | Files and bytes, and `problems()` for loose permissions. Never opens a file; there is no `show`. |
| 09 | `SyncRunner.sync` | `oaken sync [--account A] [--wait]` | Prints stored, unchanged, deleted, whether a backfill paused and any error. |
| 10 | `usage_summary(since, workspace)` | `oaken usage --workspace NAME` | The same table, narrowed to one workspace (usage is now grouped by workspace, stage and model). |
| 10 | `Workspaces` | `oaken workspaces [--json]` | Each workspace with its trust level, backend, and whether that backend is local; flags a secure workspace whose backend is remote. |
| 10 | `LLMCache.clear(workspace)` | `oaken ai cache clear [--workspace NAME]` | Confirm first. The cache can hold text derived from mail. |
| 10 | Ollama startup check | `oaken ai check` | Starts the configured embedder and LLM backend and reports the model, dimensions, context window and locality, or the exact `ollama pull` to run. Never pulls. |
| 11 | Gmail authorize (`GmailProvider.authorize`) | `oaken auth gmail [--account NAME]` | Prints the Google consent URL (or opens a browser), waits on the loopback port, and says whether the refresh token reached the keyring. Never prints a token. |
| 11 | Gmail health (`needs re-authorization`) | `oaken doctor` / `oaken accounts` | Shows the needs-re-authorization state with the cause list (Testing-mode app, password change, revoked access) and the command to fix it. |
| 11 | `list_labels`, `create_label`, `rename_label`, `delete_label` | `oaken labels [list\|create\|rename\|delete]` | Gmail labels; system labels refuse rename and delete. Confirm before delete. |
| 11 | `trash`, `untrash`, `archive`, `star`, `set_flags` | `oaken mail trash\|untrash\|archive\|star ID...` | Per-id results; a partial failure lists the ids that did not change. |
| 11 | `send` | (not in 11) | Sending goes through the outbox, audit and confirm step in a later prompt; never over MCP. |
| 11b | SMTP password (`SmtpProvider.set_password`) | `oaken auth smtp [--account NAME]` | Reads the password without echoing it, stores it in the keyring and says so. Never prints it, never takes it as an argument. |
| 11b | SMTP health (`needs a new password`) | `oaken doctor` / `oaken accounts` | Shows the needs-a-new-password state, and the command to fix it. |
| 11b | `authenticate` | `oaken auth smtp --check` | Connects, requires TLS and logs in; sends nothing. |
| 11c | IMAP password (`ImapProvider.set_password`) | `oaken auth imap [--account NAME]` | Reads the password without echoing it, stores it in the keyring and says so. Never prints it, never takes it as an argument. |
| 11c | IMAP health (`needs a new password`) | `oaken doctor` / `oaken accounts` | Shows the needs-a-new-password state, and the command to fix it. |
| 11c | `authenticate` | `oaken auth imap --check` | Connects, requires TLS and logs in; reads nothing. |
| 11c | Folder scope (`folders.to_sync`) | `oaken folders [--account A]` | Every folder, which are synced, which are skipped and why (server-marked Trash or Junk, a name guess, or your lists), and which folder `trash` and `archive` would use. |
| 11c | `trash`, `archive`, `move` | `oaken mail trash\|archive\|move ID... [--to FOLDER]` | Shares the Gmail `oaken mail` verbs; a partial failure lists the ids that did not move. Question: one command for both providers, or per-provider flags? |
| 11c | `ImapFolderReset` event | `oaken events` | The server renumbered a folder and it was read again; worth surfacing in `oaken doctor`. |

