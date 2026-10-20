# 12b — Sync scheduling: core loads providers and runs them on a schedule

**Read first:** `docs/prompts/00-context.md`. **Depends on:** 03, 09, 11, 11c, 12.

## Goal

Nothing drives providers yet: `SyncRunner` and `ReadState` are only exercised against
fakes. Make core load the configured providers, run each account's sync on a schedule the
user controls, and wire inbound flags and write-back together, so a real mailbox is read
and written by the running daemon. The schedule is data in the database, visible and
editable from the UI and CLI.

## Decisions already made with the user

- Core does this internally: one asyncio process, providers as plugins, the existing
  `Scheduler` and `JobQueue`. Nothing external (no system cron, no second process).
- Schedules live in the **database** (so a UI edit is live and survives restarts).
  `config.toml` only supplies the defaults for a new account.
- The UI shows a **table of the configured jobs** (account or folder, schedule, timezone,
  next run, last run, last result, paused), with pause, resume and "sync now" per row.
  `oaken schedule list --crontab` is a read-only crontab-style export of the same rows.
  The database stays the only place a schedule is edited.
- Accounts may run at the same time or not; that is configuration, not code.

## Requirements

1. **Schedule table** (`raw.db` migration): per account, and per folder for IMAP. Holds an
   interval (`every 5m`) or a cron expression, a timezone, optional quiet hours, `paused`
   and `paused_reason`, `next_run`, `last_run`, `last_result`, `last_error` (scrubbed).
   The existing scheduler is UTC only: extend it to a per-schedule timezone, with tests
   across a DST change.
2. **Validation:** an interval floor (`sync.min_interval`, default 60s, because Gmail has
   quotas) enforced when a schedule is set, not discovered as a rate-limit error later.
3. **Provider loading:** start every enabled account's provider through the plugin host.
   An account whose provider cannot start is reported with its reason and does not stop
   the others.
4. **One sync at a time per account.** "Sync now" while a scheduled sync is running joins
   it (same job, dedupe key per account). A global `sync.max_concurrent` (default 2)
   limits how many accounts sync at once; the rest wait their turn. Sync and the
   `readstate.write` jobs are independent, so a slow pull never delays a mark-read.
5. **Wiring and startup order:** providers → `SyncRunner` with `flag_sink` set to
   `ReadState.apply_server_flags` → `ReadState` built with `providers_from_host` →
   `ReadState.resume_pending()` (closes the dedupe window noted in prompt 12) → scheduler.
6. **Missed runs:** after sleep or downtime run once to catch up; never replay every missed
   tick. A wake from sleep syncs once straight away.
7. **Failure handling:** backoff with jitter. An authentication failure pauses that account
   (`paused_reason`), publishes an event and does not retry until the user acts. Other
   accounts are unaffected.
8. **Live changes:** editing, pausing or resuming takes effect without a restart, and each
   change is written to the audit log (actor, reason, before and after).
9. **Hooks for later:** a "sync now" input that IMAP IDLE or Gmail push can call without
   redesigning the scheduler.

## Acceptance criteria (FakeClock, no sleeps, no network)

- Two schedules fire at the expected fake times in their own timezones, including across a
  DST change; a restart neither repeats nor skips.
- A schedule below the floor is rejected; a valid one is stored and shown by `list`.
- Sync-now during a running sync results in exactly one sync.
- With `max_concurrent = 1` and three due accounts, syncs run one after another.
- Three missed ticks produce one catch-up sync.
- An auth failure pauses that account only and records why; resume clears it.
- End to end with `FakeProvider`: a scheduled sync ingests mail, an inbound flag change
  reaches `apply_server_flags`, and a local mark-read is written back, with
  `resume_pending` run at startup.
- No test uses the network or a real mailbox.

## Out of scope

IMAP IDLE and Gmail push (only the hook), the HTTP API (16), the UI itself (a table over
the same data comes with the UI phase), CLI commands (17; rows are in its audit table).

## Docs site

Add a "Sync schedule" section to `docs/architecture/read-state.md` or a new page under
*Concepts*; document the schedule table, the floor, the concurrency cap and catch-up.
Update the software map: `readstate` and `audit` become `built` once this lands, and the
scheduler note about providers is removed. `check_docs.py` must pass.

## Checkpoint

Standard report. Say plainly whether a real mailbox is now read and written by a running
daemon, and what still stands between this and that (credentials, `oaken auth`).
