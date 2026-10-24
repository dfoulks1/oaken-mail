+++
title = "Settings, storage and the event log"
subtitle = "Change core's settings, see where the data is, tidy it, and read what happened"
lead = "Everything here is a command on the running core. It edits one table of config.toml and leaves your comments alone, tells you what a change needs before it takes effect, and never lets an environment variable quietly win."
badges = [["text-bg-secondary", "Beta"]]
group = "start"
order = 12
description = "oaken settings (get, check, set), storage and vacuum, the enrichment cache, the event log and paged audit log, and the health summary in oaken status."
+++

## Core settings

```bash
oaken settings                       # every section: value, default, where it came from, what a change costs
oaken settings ai                    # one section
oaken settings check ai chunk_tokens=128   # test a change; nothing is written
oaken settings set ai ask_context_tokens=2500
oaken settings set ai --unset ask_context_tokens   # back to the default
oaken settings set ui mark_read_delay_ms=500 --reload   # save, then reload core on it
```

Only some of core's settings are editable here. Anything that changes where data lives, which
address or port core listens on, or touches security (`[api]`, `[storage]`, token files, the model
and backend choices) is not, and `settings` says so (`by hand` in the list, a refusal if you try).
Plugin settings, including the models, are separate: see [Plugins](plugins.md#who-controls-a-plugin).

| Section | Table in `config.toml` | What it holds |
|---|---|---|
| `index` | `[index]` | Threading by subject; limits on a search query. |
| `retrieval` | `[search]` | How keyword and vector results are merged; how much an answer reads. |
| `read_state` | `[read_state]` | Automatic marking as read; audit batching. |
| `ui` | `[ui]` | How long a message is open before it is marked read. |
| `sync` | `[sync]` | Default interval, concurrency, backoff and batch size. The minimum interval is a floor you set by hand. |
| `ai` | `[ai]` | Token thrift: how much of a message a model reads, and how chunks are cut. |
| `log` | `[log]` | The log level and format. |

### What a change costs

A saved change is in `config.toml` at once, and reaches the running core when it reloads. The answer
to `check` and `set` says what is needed:

- **reload**: `oaken reload` (or `--reload` on `set`). Core re-reads its configuration in place; no
  restart, and logging is reconfigured too, so `log level=DEBUG` takes effect on a reload.
- **reload and rebuild-index**: chunk size and overlap change how new mail is cut, so mail already
  indexed follows only after `oaken rebuild-index`.
- **restart**: for a key a reload cannot apply. No key on today's list needs one, but if one ever
  does, it is reported the same way.

### Safe by construction

- Only the one table is rewritten. Comments, other tables and your plugin settings are kept, and the
  previous file is saved beside it as a timestamped backup.
- The whole file is validated before anything is written, and each field's error names the key.
- A change that would not alter anything writes nothing and makes no backup.
- An environment variable such as `OAKEN_AI__CHUNK_TOKENS` outranks the file. `settings` shows
  `env (OAKEN_AI__CHUNK_TOKENS)` as the source, and saving that key warns you that the variable wins.
- No secrets pass through here: nothing on the list is one.

## Storage

```bash
oaken storage          # the two databases, their write-ahead logs, and the quarantine folder
oaken vacuum --wait    # give back the space deleted mail left; shows the sizes before and after
```

Sizes are read from the files, not by scanning mail. `vacuum` rewrites `raw.db` and `index.db`, one
at a time, and refuses while an index rebuild runs or if the disk does not have about the larger
database's size free. While it works each database is write-locked: ingest waits and reads carry on.

## The enrichment cache

```bash
oaken cache                      # entries, size, prompt versions, and how often it saved a model call
oaken cache clear --task triage  # one task's answers; no --task clears all
```

The cache holds model answers keyed by what produced them, so clearing it costs only the calls it
had saved. Mail, the index and the usage record are untouched. Without a terminal, `clear` needs
`--yes`.

## What happened: events and the audit log

```bash
oaken events --tail 20           # recent events, including from before core's last restart
oaken events --kind JobStateChanged --since 2026-10-24T09:00:00
oaken events --follow            # the history, then the live stream
oaken audit --limit 20           # recent changes: who changed what, and whether it can be undone
oaken audit --all --since 2026-10-01
```

Core writes every event to `events.jsonl` in the data directory (`[log] events_file`; empty turns
it off), one JSON line each, owner-only, rotated at `events_max_bytes` and keeping `events_backups`
older files. Events hold ids, states and numbers, never mail text. `audit` pages with a cursor: the
last line of each page gives the command for the next, and `--all` follows them all.

## Is it healthy?

`oaken status` says whether core is running and, when it answers, adds a line: accounts, messages
(and how many are indexed), the queue, healthy plugins, and anything that needs attention, such as a
stale index or sync schedules that are paused or failing.
