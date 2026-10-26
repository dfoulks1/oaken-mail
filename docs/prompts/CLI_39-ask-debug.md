# 39 — `--debug`: a command's own logs, to the terminal and the file

**Read first:** `docs/prompts/00-context.md`, then `CLI_17-cli-client.md` (the CLI is a thin client
over the API) and `CORE_16-api-daemon.md` (the daemon, SSE events).
**Depends on:** 16, 17. Nothing unbuilt.
**Track:** `CLI_`. Touches the CLI only (`apps/cli/src/oaken_cli/`) plus one small, additive change
to `oaken.core.logging.configure_logging` in core. It does **not** change any plugin, the job
runner, or the operation registry.

## Goal

A developer runs `uv run oaken ask "what's new" --debug` and watches that one command's DEBUG logs
on the terminal as it runs, start to finish, while the same lines also land in the configured
logfile. The shell blocks until the command returns, so the trace and the result arrive together.
Every plugin the command touches (provider, model backend, enricher, vector store) is included
with no per-plugin work, because they all log through one shared logger.

This is for debugging *a command you run*, not for watching the background daemon. The two are
different surfaces; see "Not in scope".

## Decisions already taken (do not re-litigate)

These are settled by what the code already is (verified 2026-10-26), not open for redesign:

1. **`--debug` means DEBUG, to stderr, in-process, and to the logfile.** One flag, one meaning.
   `-v`/`-vv` (the existing verbosity count) stay as they are; `--debug` is the single-word door
   that also keeps the logfile and forces in-process so the logs are actually local.
2. **Logs go to stderr, never stdout.** `_configure_logging` already routes core's structlog to
   stderr so `--json` on stdout stays clean (`main.py` `_configure_logging`, docstring says so).
   `--debug` keeps that rule. A log line must never corrupt machine output.
3. **The logfile is the one from settings** (`log.file`, resolved under `data_dir`, e.g.
   `~/.oaken/data/oaken.log`). `--debug` does not invent a second path. The existing
   `configure_logging` already writes to `settings.file` *and* the stream together, via
   `_LogFile(also=stream)`; the only gap is that the CLI's `_configure_logging` builds a bare
   `LogSettings(level=...)` with **no `file`**, so today the file half is dropped.
4. **Plugins obey it for free.** Every plugin receives its logger as `PluginContext.log`
   (`src/oaken/plugins/contracts.py`), which is the shared `oaken.core.logging.get_logger`. One
   process-wide `configure_logging` governs them all. No `BasePlugin`/`_Base` change, no per-plugin
   flag. This was the original framing ("modify BasePlugin"); the base is already correct.
5. **Secrets stay redacted at DEBUG.** `configure_logging`'s `scrub` processor redacts
   `token|secret|password|authorization` keys and truncates long values at every level. `--debug`
   raises the level; it does **not** remove the scrubber. Mail text and credentials must not leak
   into a debug trace.

## Not in scope

- **Watching the running daemon's work.** The daemon is a separate OS process; its structlog goes
  to the daemon's own stderr and logfile. A CLI flag cannot reach another process's log stream.
  What *is* already exposed for daemon-side live state is **`oaken events --follow`** (SSE over
  `/v1/events`), but that carries the **event bus** (job state changes, counts; "never mail text"
  per its help), **not** structlog DEBUG lines. If you want to see the daemon drain the enrich
  backlog, that is `oaken events --follow`, a different and existing tool. Do not conflate the two.
- **Streaming a specific call's daemon-side logs back over the socket** (a correlation id + a
  per-request log handler + an SSE log channel). That is a larger, separate feature and is only
  meaningful for commands that *run in the daemon*; `--debug` runs the command in-process, so there
  is nothing daemon-side correlated to it. Leave it for a future prompt if the need is real.
- **A new logfile, log rotation changes, or a JSON console mode for `--debug`.** Reuse what
  `configure_logging` already does (console renderer to stderr, `_LogFile` rotation to the file).

## The constraint that shapes the build (read before coding)

`--debug` must run the command **in-process** (`InProcessApiClient` wrapping a `CoreClient`), so the
logs are emitted in the terminal's process. But there is no CLI-side `CoreClient` builder today:
`make_client` (`apps/cli/src/oaken_cli/commands/runtime.py`) defaults to the **HTTP** client (talks
to the daemon), and the in-process path exists only behind `set_client_factory`, which only tests
install. The daemon builds core in its own process (`oaken.daemon.lifecycle`), and `serve()` raises
**`Conflict` at once if another core is running**.

So the honest shape of this work, and its one real risk:

- A `--debug` run opens core **in the CLI process**: the raw and index stores, the keyring, the
  configured plugins, the vector store. It must do this **without `serve()`** (no second HTTP
  server, no port bind, no runtime-file takeover) so it does not collide with a running daemon.
- Two processes then read the same SQLite stores at once (the CLI's debug core and the daemon).
  SQLite in WAL mode allows concurrent readers, and `ask` is read-only, so this is safe for `ask`.
  **A debug run of a *mutating* command while the daemon runs is a different question** and is out
  of scope for the first cut: `--debug` is specified for read-only commands (`ask`, `search`,
  `show`) first. A mutating command with `--debug` and a live daemon should refuse with a clear
  message rather than risk two writers.

## Requirements

Operations first, then the flag, then the wiring. (A feature is a registry operation before it is a
command — but this feature adds *no* operation; it is purely a CLI transport-and-logging concern.
That is why it is a `CLI_` prompt and touches core only in `configure_logging`.)

1. **`--debug` flag on the shared parent parser.** Add it in `_globals()` (`main.py`), beside the
   existing `--verbose`, so every command inherits it via `parents=[g]`. `store_true`;
   `SUPPRESS` default on subcommands, `False` on the top level (match `--json`/`--verbose`).
   Help: "Run this command in-process with DEBUG logging to the terminal and the logfile."
2. **`_configure_logging` keeps the logfile and honors `--debug`.** When `--debug` is set, build
   `LogSettings` from the **loaded config** (so `file`, `file_max_bytes`, `file_backups`, `format`
   are the real ones) with `level="DEBUG"`, then `configure_logging(settings, stream=sys.stderr)`.
   The existing `_LogFile(also=stderr)` then writes both. `-vv` without `--debug` keeps today's
   behavior (DEBUG to stderr, no file) so nothing regresses.
3. **`--debug` forces in-process.** `make_client` returns an `InProcessApiClient` over a
   CLI-built `CoreClient` when `args.debug` is set, instead of the HTTP client. Build core
   **without serving** (no port, no runtime file). If a CLI-side core builder does not exist, add
   one small factory next to `lifecycle`'s bring-up that constructs `Core`/`CoreClient` and starts
   plugins but does not call `serve()`.
4. **Mutating command + `--debug` + live daemon → refuse.** First cut scopes `--debug` to
   read-only commands. If the command mutates and a daemon is detected (discovery finds a runtime
   file), exit with a clear message ("--debug runs in-process; stop the daemon or drop --debug for
   mutating commands") and a stable code. Read-only commands proceed.
5. **The shell blocks until done.** `run()` already awaits the whole op in one `asyncio.run`;
   `--debug` adds no streaming, so blocking-until-return is automatic. The trace interleaves with
   execution on stderr and the result prints on stdout at the end.
6. **No plugin change.** Verify by reading, not editing: a plugin that logs via `PluginContext.log`
   appears in the trace with zero changes to the plugin. If a plugin uses its own
   `logging.getLogger` or `print`, that is a plugin bug to fix separately, not a reason to touch the
   base class here.

## Acceptance criteria

Each a test that passes or fails; include the refusals and what must never appear.

- `oaken ask "q" --debug` (read-only, no daemon or daemon up): prints DEBUG lines to **stderr**
  during the run, the final answer to **stdout**, and the **same** lines are appended to the
  configured logfile. Assert all three: a stderr DEBUG line, a clean stdout answer, a new logfile
  line.
- The run uses `InProcessApiClient` (assert the client type, or that no HTTP request was made).
- With a daemon running, a read-only `--debug` run does **not** raise `Conflict` and does **not**
  bind a port or rewrite the runtime file (assert the runtime file's mtime/pid is unchanged).
- A plugin's `log.debug(...)` from inside the op (e.g. the ollama client's request log) appears in
  the `--debug` output with no change to that plugin.
- **Redaction holds at DEBUG:** a log event carrying a `token`/`password`/`authorization` field, or
  a >200-char value, is redacted/truncated in both stderr and the file. Mail body text never
  appears raw in a debug line (the `scrub` processor and `UntrustedText` boundaries stand).
- `oaken ask "q" --json --debug`: stdout stays valid JSON (DEBUG went to stderr only).
- A **mutating** command with `--debug` and a live daemon exits with the documented message and
  code; the same command without a daemon runs in-process.
- `-vv` (no `--debug`) still logs DEBUG to stderr and still does **not** write the file — no
  regression to the existing verbosity path.

## Open questions (defaults in bold)

1. Does `--debug` imply `-vv`, or are they independent? **`--debug` sets the level to DEBUG on its
   own; `-v`/`-vv` remain a separate, additive verbosity count. `--debug` is the single switch that
   also keeps the file and forces in-process.**
2. Read-only only, or all commands, in the first cut? **Read-only first (`ask`, `search`, `show`).
   Mutating + live daemon refuses; mutating with no daemon may run in-process (same single-writer
   guarantee the daemon has when it is down).**
3. Does the debug run write to the daemon's *shared* logfile, or a per-run file? **The shared
   configured logfile (`log.file`). One place to look; the daemon and a debug run interleave there
   and that is acceptable. A per-run file is more machinery than asked for.**
4. If building a CLI-side core is heavier than one small factory, split it? **Yes — if bring-up
   without `serve()` is not a clean seam, land requirements 1–2 (the flag + the logfile fix for the
   `-vv`/in-process paths that already work) first, and do requirement 3 (force in-process) as a
   follow-up, rather than entangle this with a core refactor.**

## Report-back checkpoint

End with the six sections from `00-context.md` (what was built, what was decided, what was skipped
and why, how it was tested, what is left, and any risk). Name every file changed, and state
explicitly whether any plugin was touched (it should not be) and whether a running daemon was
present during the acceptance run.
