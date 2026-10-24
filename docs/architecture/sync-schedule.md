+++
title = "Sync schedule"
subtitle = "Core runs every account's sync on a schedule you control"
lead = "Core loads each account's provider and syncs it on a schedule stored in the database. You can see it, edit it live, pause it and run it now. Nothing outside core is involved: no system cron and no second process."
badges = [["text-bg-warning", "Pre-alpha"]]
group = "concepts"
order = 40
description = "The sync schedule table, intervals and cron with a timezone, the interval floor, the concurrency cap, catch-up after sleep, backoff, authentication pauses, quiet hours, per-account provider instances and per-folder schedules."
+++

## The schedule is data

Each account has one row in `sync_schedules` (`raw.db`). An IMAP account can also have a row per
folder. A row holds:

| Column | Meaning |
|---|---|
| `spec` | `every 15m` (an interval) or a five-field cron expression |
| `timezone` | an IANA name such as `America/New_York`, from your system's tz database |
| `quiet_start`, `quiet_end` | optional quiet hours, `HH:MM`, in the schedule's timezone |
| `paused`, `paused_reason` | whether it runs, and why not |
| `next_run`, `last_run`, `last_result`, `last_error` | what happens next and what happened last (the error is scrubbed) |
| `failures` | consecutive failures, which drive the backoff |

`config.toml` only supplies the defaults for a schedule created for a new account. After that the
database is the only place a schedule is edited, so a change made in the UI or CLI is live and
survives a restart.

| Setting | Default | Meaning |
|---|---|---|
| `sync.interval_seconds` | 900 | Default gap between syncs (15 minutes) |
| `sync.timezone` | `UTC` | Default timezone of a new schedule |
| `sync.min_interval_seconds` | 60 | The floor. A faster schedule is refused when you set it |
| `sync.max_concurrent` | 2 | How many accounts sync at once; the rest wait their turn |
| `sync.backoff_max_seconds` | 3600 | The longest a failing schedule waits |
| `sync.backoff_jitter` | 0.2 | Random spread added to a backoff so accounts do not retry in step |

## When it runs

- **One sync at a time per account.** "Sync now" while a scheduled sync is running joins it: there is
  one job, not two. A folder sync and a whole-account sync of the same account never overlap.
- **Accounts are independent.** They may sync together or one after another; `sync.max_concurrent`
  decides. A slow pull never delays a mark-read, because write-back is a different job kind.
- **Catch-up.** After sleep or downtime a schedule runs once straight away. Three missed ticks are
  one catch-up, never three syncs. An interval keeps its phase, so a restart neither repeats nor
  skips a run.
- **Daylight saving.** A cron time that does not exist that day (the spring gap) does not fire that
  day. One that happens twice (the autumn repeat) fires once, at the first.

## When it fails

A failure backs off: the base gap doubled for each consecutive failure, capped at
`sync.backoff_max_seconds`, with jitter. An **authentication** failure is different: that schedule is
paused with the reason *authentication failed*, an event is published and a row is written to the
audit log. It is not retried until you act. Other accounts carry on. A successful manual sync, or
`resume`, clears the pause.

An account whose provider cannot start is reported with its reason and does not stop the others.

## Quiet hours mute alerts, not syncs

Quiet hours never stop a sync. They mark the events a sync publishes (`SyncFinished`, `SyncPaused`)
as `quiet`, so a client can hold back notifications overnight while mail keeps arriving.

## Several accounts of one provider

A provider plugin is a class, and core starts **one instance per account**, named
`<plugin>@<account id>`. Its configuration is the plugin's `[plugins.<name>]` table with the
account's own non-secret settings on top, and its keyring entry defaults to the account id. Three
Google accounts and a Fastmail account are four accounts, each with its own credentials.

## Folders

`pull(cursor, folder=None)` is one function for both jobs. With no folder it syncs the whole account;
with a folder it syncs only that one, which lets an IMAP account check `INBOX` often and an archive
rarely. A provider that has no folders (`supports_folders = False`, as Gmail) refuses a folder
schedule when you set it. A schedule for a folder is stored under the key `account/folder`.

## Changes are audited

Every `set`, `pause`, `resume` and `remove` is written to the audit log with the actor, the reason and
the value before and after. Schedule changes hold only the spec, timezone, quiet window and pause
fields, never mail content. `undo` applies to read-state changes only and refuses these.

## Seeing it

The UI shows a table of the schedules with pause, resume and sync-now per row. The same rows can be
exported read-only in crontab form, with a `CRON_TZ` line per timezone, paused entries commented out
and intervals that cron cannot express commented out too. The database stays the only place to edit.

## Starting up

`SyncService.start()` runs in this order: start the account providers, resume any read-state
write-backs that were queued when the process stopped, make sure every account that can pull has a
schedule, then start the loop. Accounts with no usable provider are returned with their reasons.

## Still to come

IMAP IDLE and Gmail push can call `SyncService.request()` for "sync now" without any change to the
scheduler. The [daemon](daemon.md) now starts all this, and each provider's own `auth` command (`oaken gmail auth`) is what connects it to a
real mailbox.
