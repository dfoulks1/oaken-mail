# 17 — CLI client

> **Superseded (0.1.1): `oaken auth`.** The `oaken auth gmail|smtp|imap` rows below are history.
> Sign-in is each provider's own command: `oaken gmail auth`, `oaken imap auth`, `oaken smtp auth`
> (`--account-id`, `--check`). A password is never an argument: it is read without echo, or from
> standard input with `--password-stdin`. The core operations `set_account_password`,
> `check_account`, `start_authorization` and `authorization_status` are gone; the provider's
> `<provider>.auth` and `<provider>.auth_status` replace them. See [35](35-onboarding-0-1-1.md).

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
| 03 | Job queue: enqueue, claim, cancel, retry, dedupe, parents and children | `oaken jobs list\|show\|cancel\|retry` | **The user wants the queue exposed.** Filter by kind, status, account and actor. Show attempts, the scrubbed last error and the children of a fan-out. Built (17): `oaken jobs list|show|cancel|retry` over the new ops `list_jobs`, `cancel_job`, `retry_job` (`JobQueue.query`/`retry`), in the same `jobs` group as the agent protocol (routed by subcommand). Cancel and retry are confirmed. A purge of finished jobs and retention are still not built. |
| 03 | Scheduler with `every` and cron (UTC) | `oaken schedules list\|add\|remove\|run-now` | `Scheduler` has `add`, `remove` and `next_run` but no `list`; `run-now` is new. Cron is UTC only. |
| 03 | Event bus (in memory, typed) | `oaken watch`, `oaken events` | **The user wants the event log exposed.** The bus keeps no history, so first decide: a durable event table (kept how long, which events, what is redacted) or only the live stream with a ring buffer. Either way an event never carries a message body. |
| 03b | Agents and leases | `oaken agents ...`, `oaken jobs claim\|heartbeat\|complete\|fail` | Already specified above. |
| 04 | Plugin host: statuses, health, capabilities | `oaken plugins [check]` | List with kind, version, capabilities and health; `check` runs the health probe now. Enable and disable per plugin is not built. |
| 05 | Accounts, non-secret config | `oaken admin account ...` | Built (17, 24). `oaken admin account list\|show\|add\|set\|remove` (ops `get_account`, `create_account`, `set_account_config`, `remove_account`); `add` asks questions on a terminal or takes `--provider --id --name --set`. `set` merges `--set KEY=VALUE` and flips the receive-only switch (`--receive-only`/`--can-send`); `remove` drops the account and its schedules but keeps stored mail. `add` for Gmail defaults to receive-only (prompt 24); `list`/`show` show the send posture. Wizard noted above. |
| 05 | `RawStore.stats()` | `oaken store stats` | Messages, blobs, LFS count, raw and stored bytes, ratio. |
| 05 | `gc_blobs()` | `oaken store gc [--dry-run]` | Deletes data, so `--dry-run` and a confirmation. `gc_blobs` has no dry run yet. |
| 05 | `get_raw(verify=True)` | `oaken store verify` | Re-hash every blob and report mismatches and missing LFS files. Slow on a big store, so a job. |
| 05 | Soft delete (`mark_deleted`) | `oaken message delete\|undelete ID` | There is no undelete in the store. Deleting mail is destructive to the user's view, so it needs the confirm gate. |
| 05 | Raw bytes | `oaken message export ID --eml PATH` | The only place raw bytes leave the store; **never over MCP**. Terminal output of a body is sanitized (see requirement 3). |
| 05 | Sync cursors | `oaken sync cursors [--reset ...]` | Reset forces a full resync: confirm. |
| 06 | `parse`, `clean`, `thread_key`, token counts | `oaken message parse\|clean ID` (and `--eml PATH`) | A debugging view: the headers, each segment with the rule that tagged it, the thread key and how it was derived, token counts before and after, warnings, attachment metadata and the quarantine flag. Reads from the store or a file. The most useful tool for tuning `[mail.cleaning]`. |
| 06 | Cleaner rules and `disabled_rules` | `oaken clean rules` | Name and one line for each rule, and which are disabled. |
| 06 | Tokenizer (estimate or a local `tokenizer.json`) | `oaken tokenizer info` | Which one is active and whether counts are exact. A command that fetches a `tokenizer.json` is an open question from 06 (it would be the only network call here). |
| 06c | Sieve scripts | `oaken sieve check\|test\|list\|get\|save\|delete\|refilter` | Built (17, 26). Read/test side over `sieve_check`, `sieve_test`, `sieve_list`, `sieve_get`; write side over `sieve_save`, `sieve_delete` (confirm-gated, **never over MCP**); `refilter` is `reprocess filter`. `save NAME FILE` compiles before it persists (a `-` FILE reads stdin); `save --suggest-name [--base NAME]` prints the next free `NN-name.sieve` since run order is by name. Scripts stay files on the person's disk. |
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
| 09 | Parked jobs | `oaken pipeline parked [--retry ID\|--all]` | Built (17): over `retry_parked`. Retrying re-queues a failed stage job; `--retry JOB_ID` for one, `--all` for every parked ingest job. Also `oaken pipeline pause\|resume` over `pause_backfill`/`resume_backfill`: a durable switch that stops the model-bound stages (embed, enrich) while mail keeps landing. Confirmed. |
| 09 | `Pipeline.reprocess(stage)` | `oaken reprocess parse\|filter\|embed\|enrich [--account A]` | One job per stage and account, at backfill priority, with progress. `embed` after an embedder change is how the model swap happens. |
| 09 | `UsageStore.usage_summary(since)` | `oaken usage [--since DATE] [--json]` | Calls, cached calls, tokens in and out and tokens saved, per stage and model. Enrichment tokens are estimates. |
| 09 | Quarantine folder | `oaken quarantine list\|stats\|check` | Files and bytes, and `problems()` for loose permissions. Never opens a file; there is no `show`. |
| 09 | `SyncRunner.sync` | `oaken sync [--account A] [--wait]` | Prints stored, unchanged, deleted, whether a backfill paused and any error. |
| 10 | `usage_summary(since, workspace)` | `oaken usage --workspace NAME` | The same table, narrowed to one workspace (usage is now grouped by workspace, stage and model). |
| 10 | `Workspaces` | `oaken workspaces [--json]` | Each workspace with its trust level, backend, and whether that backend is local; flags a secure workspace whose backend is remote. |
| 10 | `LLMCache.clear(workspace)` | `oaken ai cache clear [--workspace NAME]` | Confirm first. The cache can hold text derived from mail. |
| 10 | Ollama startup check | `oaken ai check` | Starts the configured embedder and LLM backend and reports the model, dimensions, context window and locality, or the exact `ollama pull` to run. Never pulls. |
| 11 | Gmail authorize (`GmailProvider.authorize`) | `oaken auth gmail [--account NAME]` | Built (17). Core runs the consent in the background (`start_authorization`, `authorization_status`); the CLI prints the address, opens a browser on a TTY unless `--no-browser`, and polls. Prints the Google consent URL (or opens a browser), waits on the loopback port, and says whether the refresh token reached the keyring. Never prints a token. |
| 11 | Gmail health (`needs re-authorization`) | `oaken doctor` / `oaken accounts` | Shows the needs-re-authorization state with the cause list (Testing-mode app, password change, revoked access) and the command to fix it. |
| 11 | `list_labels`, `create_label`, `rename_label`, `delete_label` | `oaken labels [list\|create\|rename\|delete]` | Gmail labels; system labels refuse rename and delete. Confirm before delete. |
| 11 | `trash`, `untrash`, `archive`, `star`, `set_flags` | `oaken mail trash\|untrash\|archive\|star ID...` | Per-id results; a partial failure lists the ids that did not change. |
| 11 | `send` | (not in 11) | Sending goes through the outbox, audit and confirm step in a later prompt; never over MCP. |
| 11b | SMTP password (`SmtpProvider.set_password`) | `oaken auth smtp [--account NAME]` | Built (17). Read without echo, or `--password-stdin`; stored by core via `set_account_password`, audited without a hash of it, then checked. Reads the password without echoing it, stores it in the keyring and says so. Never prints it, never takes it as an argument. |
| 11b | SMTP health (`needs a new password`) | `oaken doctor` / `oaken accounts` | Shows the needs-a-new-password state, and the command to fix it. |
| 11b | `authenticate` | `oaken auth smtp --check` | Built (17). Op `check_account`; exit 4 if the login fails. Connects, requires TLS and logs in; sends nothing. |
| 11c | IMAP password (`ImapProvider.set_password`) | `oaken auth imap [--account NAME]` | Built (17). Same as SMTP. Reads the password without echoing it, stores it in the keyring and says so. Never prints it, never takes it as an argument. |
| 11c | IMAP health (`needs a new password`) | `oaken doctor` / `oaken accounts` | Shows the needs-a-new-password state, and the command to fix it. |
| 11c | `authenticate` | `oaken auth imap --check` | Built (17). Op `check_account`. Connects, requires TLS and logs in; reads nothing. |
| 11c | Folder scope (`folders.to_sync`) | `oaken folders [--account A]` | Every folder, which are synced, which are skipped and why (server-marked Trash or Junk, a name guess, or your lists), and which folder `trash` and `archive` would use. |
| 11c | `trash`, `archive`, `move` | `oaken mail trash\|archive\|move ID... [--to FOLDER]` | Shares the Gmail `oaken mail` verbs; a partial failure lists the ids that did not move. Question: one command for both providers, or per-provider flags? |
| 11c | `ImapFolderReset` event | `oaken events` | The server renumbered a folder and it was read again; worth surfacing in `oaken doctor`. |

| 12 | `mark_read`, `mark_unread` | `oaken mark read\|unread ID...` | An explicit user command, so `user_marked_read` / `user_marked_unread` with no confirm. Prints the audit id. Bulk changes are chunked into audit rows (`read_state.audit_chunk`). |
| 12 | `undo(audit_id)` | `oaken undo AUDIT_ID` | Undoes the whole batch the row belongs to; prints the new audit id. Refuses a row that was already undone. |
| 12 | `AuditLog.recent`, `get`, `batch` | `oaken audit [--op OP] [--limit N]`, `oaken audit show ID` | Actor, reason, op, ids and before and after. No mail content is ever stored there. Add `oaken audit export [--since T]` (JSON Lines) and an optional `audit.tee_path` setting that appends each row to an owner-only file as it is written; rows hold ids and booleans, never mail content. Retention is deferred. |
| 12 | `ReadState.resume_pending` | `oaken read-state resume [--account A]` | Queues write-backs for changes that stayed local (no provider could write flags, or core stopped). Also worth running at startup. |
| 12 | Local-only read changes (`local_dirty_json` with an `error`) | `oaken read-state pending [--account A]` | Lists changes that exist only here and why (`no_flags_write_provider`, a refusal). The UI process for dealing with them is later; this is the operator's view. |
| 12 | `message_presented`, `message_dismissed` | `oaken open ID [--peek]` | The presented/dismissed timer is for UIs that can show and then leave a message. `oaken open ID` marks read straight away (`user_opened`); `--peek` shows it without marking. |
| 12b | Per-account sync schedules in the database (interval or cron, timezone, quiet hours, paused, next and last run) | `oaken schedule list [--crontab]\|set\|pause\|resume\|remove\|run` | `set` takes `--folder`, `--timezone` and `--quiet HH:MM-HH:MM`. `list` prints the same columns the web UI table shows; `--crontab` is a read-only export. Edits are live and audited; an interval below `sync.min_interval` is refused. Replaces the earlier `oaken schedules` row for sync work. |
| 13 | `list_operations`, `set_operation_exposure` | `oaken operations [list]`, `oaken mcp expose\|hide OP\|PLUGIN` | Lists each operation with its permission, ceiling and whether MCP is offered it. Changing exposure shows the proposal first and is audited. Never over MCP. **Not built:** the verbs would sit under `oaken mcp`, which is now the stdio bridge (18); until then use `oaken operation set_operation_exposure --json-input '{...}' --yes`, which the MCP docs page shows. |
| 13 | `list_messages`, `search_keyword`, `get_message`, `get_raw`, `list_labels`, `get_message_rules` | `oaken messages`, `oaken search`, `oaken show ID [--raw]` | Bounded results; text is cut and says so. `--raw` is never over MCP. |
| 13 | `summarize_message` (batch) | `oaken summarize ID... [--mark-read]` | Up to `max_summarize_ids` per call. Marking read is a separate confirmed step, offered at the end. |
| 13 | `sync_now`, schedule operations | `oaken sync`, `oaken schedule ...` | Same as the 12b row, now through the registry; schedule edits are never over MCP. |
| 13 | `status`, `pipeline_status`, `usage_summary`, `list_accounts`, `list_plugins`, `plugin_health`, `list_audit` | `oaken status`, `oaken pipeline status`, `oaken usage`, `oaken accounts`, `oaken plugins`, `oaken audit` | Read-only. `list_plugins` also shows each plugin's settings schema; there is no command to set them yet. |
| 13 | Plugin operations | `oaken <plugin> <operation>` | Generated from what a plugin offers. Start off over MCP. |
| 14 | `Pipeline.enrichment(message_id, enricher)`, `Pipeline.reprocess("enrich")` | `oaken enrich run ID [ENRICHER] [--force]`, `oaken enrich show ID [ENRICHER]`, `oaken reprocess enrich [--account A]` | Built (17). `oaken enrich run|show` over ops `enrich_message` (never over MCP) and `get_enrichment`; `--force` re-runs and asks the model afresh. Shows a stored result (summary, triage, actions, inspector) as JSON. `reprocess enrich` re-runs enrichers whose version changed; the rest are cache hits. `oaken enrich run ID` runs the enrichers on one message now (stored rows and cached model answers still count; `--force` ignores both). `enrich run` pairs with `enrich show`, so `enrich` is a group and an ID can never be mistaken for a subcommand. |
| 14 | `python -m oaken.evals enrich [--enricher NAME] [--fixtures DIR]` | `oaken evals enrich` | Built as a module because it needs a configured model and is manual. 17 decides whether to fold it into the CLI. |
| 14 | `[plugins.<enricher>] enabled`, `skip_bulk` | `oaken plugins settings` | Switching an enricher off is config only today; there is still no command to set plugin settings (see the row for 13). |
| 15 | `search` (hybrid), `ask` | `oaken search QUERY`, `oaken ask QUESTION` | `search` now merges keyword and vector results; each hit shows `why` (which retriever found it) and the result says `degraded` and why when the vector side was down. Operators: `from:`, `before:`, `after:`, `label:`, `in:`, `is:unread`, `category:`, `priority:`, `"phrase"`, `-term`; flags for thread, category, priority and unread. Print the warnings. `search_keyword` stays for plain BM25. `ask` prints the answer with numbered citations (message, quote), `confidence`, and says `insufficient evidence` plainly; show `dropped_claims` when non-zero and the evidence tokens against the full bodies. `ask` with mail in several accounts asks for `--account`. Neither marks mail read; opening a cited message does. `ask` stays off over MCP unless the user turns it on. |
| 15 | `python -m oaken.evals retrieval [--messages N --seed S --per-kind K --live --out DIR]` | `oaken evals retrieval` | Manual with `--live`; offline it is deterministic and writes `docs/evals/latest.{md,json}`, which the Evaluation results page is built from. 17 decides whether to fold it into the CLI. |
| 16 | `oaken.daemon`: `serve`, `detach()`, `status()`, `stop()`, `restart()`; `python -m oaken.daemon` | `oaken start [--detach]`, `oaken stop`, `oaken status`, `oaken restart` | Built (17). Thin wrappers. `start` is foreground by default and exits 1 with "already running" if the runtime file's pid is alive. `--detach` uses a new session on POSIX and a detached process group on Windows (stop sends `CTRL_BREAK`). Exit 0 on a clean stop. |
| 16 | `create_agent`, `list_agents`, `revoke_agent` (never over MCP) | `oaken agents create LABEL --kind K...`, `oaken agents list [--all]`, `oaken agents revoke ID` | Built (17). `create` prints the token once (`oaken_agent_...`) and says it cannot be shown again. Core op names are `create_agent` etc. because a `.` in a name means a plugin operation. |
| 16 | `/v1/agent/jobs/claim`, `/heartbeat`, `/complete`, `/fail` | `oaken jobs claim\|heartbeat\|complete\|fail` (uses `OAKEN_AGENT_TOKEN`) | Built (17). Not registry operations: a separate surface authenticated by the agent token only. 429 quota, 422 invalid result, 403 kind not given, 409 lease lost. (18) The same four are registry operations (`claim_job`, `heartbeat_job`, `complete_job`, `fail_job`) in an agent-only registry on the MCP door `/mcp/agent`, with the registry's audit row for claim, complete and fail. |
| 18 | The MCP server in core (`/mcp`), generated from the registry | `oaken mcp [--timeout S]` | Built (18). A stdio bridge, not a verb for the person: it forwards to `/mcp` with the `mcp_token`, adds nothing, never starts core and never opens a database. Core down: a tool call is an `isError` result `{code: core_not_running}` with the start hint. Configure Claude Desktop or `ollmcp` with `command: oaken, args: [mcp]`. |
| 18 | The swarm door (`/mcp/agent`) | `oaken mcp --agent [--agent-token-file PATH]` | Built (18). The same bridge to the agent door, with the agent's token from `OAKEN_AGENT_TOKEN`; it offers only the four job tools. |
| 16 | HTTP wire contract | `HttpApiClient`, `discovery` | Routes are `/v1/operations` and `/v1/operations/{name}` (not `/v1/ops`). `runtime.json` is at `paths.runtime_file()`, and the token at `config_dir()/api_token`. `GET /v1/health` returns only `{"status": "ok"}`, with no version: read the version from `runtime.json` or `status`. `GET /v1/operations` entries also carry `mcp` (the ceiling-aware exposure). Errors are `{code, message}`; 401 means a bad token. Send `X-Oaken-Client: cli`. |
| 16 | `GET /v1/events` (SSE) | `oaken watch` | Each frame has `id`, `event` (the event class name, or `gap`) and `data` (JSON of ids and counts). Reconnect with `Last-Event-ID`; on a `gap` event re-fetch state. `: heartbeat` comments keep the stream alive. |
| 34 | `GET /stats` read-only status page (ADR 0013) | (no verb — a browser URL) | `http://127.0.0.1:<port>/stats`, loopback-only and unauthenticated like `/v1/health`; a counts/health dashboard (version, uptime, message/index/queue counts, pipeline stages and parked jobs, token usage, per-account sync, plugin health), no mail content. Self-contained HTML (offline, no CDN, no scripts). Port is in `runtime.json`; pin with `[api] port`. `oaken status` stays the shell path. A friendly `oaken.local` name and a networked agent-facing variant are deferred. |
