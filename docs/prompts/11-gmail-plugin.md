# 11 — Gmail provider plugin

**Read first:** `docs/prompts/00-context.md`. **Depends on:** 04, 05, 09.

## Goal

`oaken-gmail`, the first `Provider` plugin: bring-your-own OAuth client, full
mirror, incremental sync. It owns **its own auth**. It returns raw RFC 822 bytes and
never touches the databases.

## Requirements

1. **Auth** (`capabilities: auth`): OAuth 2.0 installed-app flow with PKCE using a
   loopback redirect on a random free port (`google-auth-oauthlib`'s flow is acceptable;
   or hand-rolled with `httpx` — choose, justify). The **user supplies** their own
   Google Cloud OAuth client JSON path via config (`provider.gmail.client_secret_file`);
   the repo ships none. Refresh tokens are stored **only in the OS keyring**
   (`keyring`, service `oaken`, one entry per account); access tokens in memory.
   Scopes: `gmail.modify` (read + flag changes). No `send`/`compose` scope in v1.
   Document the Google-side realities in `docs/providers/gmail.md`: creating the OAuth
   client, "Testing" mode refresh tokens expiring after 7 days, restricted-scope
   verification requirements for anyone distributing a shared client.
2. **Sync scope:** All Mail **excluding Spam and Trash** (configurable label include/
   exclude lists). Gmail labels are passed through as `provider_labels`; folder-like
   semantics are core's concern, not the plugin's.
3. **Full sync:** `users.messages.list` (paged) → `messages.get?format=raw`
   (base64url decode → bytes) with batched requests, concurrency limit, and
   quota-aware backoff (`429`/`403 rateLimitExceeded`/`5xx`, honoring `Retry-After`).
   The cursor for full sync records progress so an interrupted sync resumes.
   Record the mailbox `historyId` **before** starting the listing so nothing is missed.
4. **Incremental sync:** `users.history.list` from the stored `historyId` handling
   `messagesAdded`, `messagesDeleted`, `labelsAdded/Removed` (→ `flag_changes`, incl.
   `UNREAD`/`STARRED`). If Gmail returns `404` for an expired history ID, fall back to a
   full resync automatically and report it via events.
5. **`flags.write`:** `set_flags(provider_ids, add, remove)` via `messages.batchModify`
   (e.g. remove label `UNREAD` = mark read). Idempotent; partial failures reported per id.
6. **Plugin hygiene:** HTTP via `httpx` (async) against the REST API — the legacy tree
   did this in `mailmind/gmail.py`; read it for ideas, don't copy. No `google-api-python-client`
   dependency. All requests have timeouts; tokens never appear in logs or exceptions.
7. **Testing without Google:** a `FakeGmailServer` (an `httpx.MockTransport` or `respx`
   app) in `tests/` implementing the subset of endpoints, with synthetic mailboxes,
   history, pagination, rate limits and expired history IDs. The plugin passes
   `ProviderContract` and Gmail-specific tests against it.

## Acceptance criteria

- Full sync of a 2,000-message fake mailbox, interrupted midway and resumed, yields
  each message exactly once downstream; incremental sync then picks up adds, deletes and
  label/unread changes; expired history ID triggers a full resync.
- Rate-limit responses are retried with backoff without exceeding the concurrency cap.
- Refresh token lives in a fake keyring backend in tests; assert it never appears in
  logs, exceptions, config files or the DB.
- `flags.write` of `UNREAD` removal issues the correct `batchModify` body.
- A manual, skipped-by-default `live_gmail` test + a documented checklist exists for
  the user's first real sync (read-only first: scope check, list 10 messages, no writes).

## Out of scope

Send/drafts, IMAP, Pub/Sub push notifications (poll via the scheduler for now),
Gmail threading semantics (core computes `thread_key`).

## Docs site

Publish `docs/providers/gmail.md` as a site page (*Start* group: Connect Gmail), including
the Google Cloud OAuth setup steps and the 7-day "Testing" token note. `check_docs.py`
must pass.

## Checkpoint

Standard report. Include the Google Cloud setup steps you documented, and flag anything
about the first live sync the user must do by hand.
