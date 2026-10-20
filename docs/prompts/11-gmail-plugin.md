# 11 — Gmail provider plugin

**Read first:** `docs/prompts/00-context.md`. **Depends on:** 04, 05, 09.

## Goal

`oaken-gmail`, the first `Provider` plugin: bring-your-own OAuth client, full
mirror, incremental sync, and the write side (flags, labels, trash, send). It owns **its own
auth**. It returns raw RFC 822 bytes and never touches the databases. The plugin is the
top-level package `src/oaken_gmail`, like `oaken_ollama`.

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
   **Token handling must not force a daily re-login (user requirement).** Before
   building, ask the user whether their Google OAuth app is in "Testing" mode (refresh
   tokens expire after 7 days) or published to "In production" (no 7-day expiry; an
   unverified app still works for its owner behind a warning screen). Then: refresh the
   access token silently and reuse the stored refresh token without prompting; persist a
   rotated refresh token if Google returns one; treat `invalid_grant` as "re-authorize
   needed", fail with a clear message and a health state, never a crash loop; and document
   the cause and the fix in `docs/providers/gmail.md`. Add a test with a fake token
   endpoint covering expiry, rotation and `invalid_grant`.
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

## Decisions made in discussion (these override the numbered requirements where they differ)

- **Fully featured, not read-only.** Besides sync and `flags.write`: label create, rename, delete,
  add and remove; trash and untrash; archive; star; and **send** (`send(raw_bytes)` through
  `messages.send`, including a send-as alias the user has set up in Gmail). Drafts and permanent
  delete are **not** in this prompt. The one `gmail.modify` scope is believed to cover all of
  this: verify against Google's docs first and document the exact scope list.
- **Sending stays gated.** The plugin only provides `send`. The outbox, audit and confirm gate are a
  later prompt, and sending is never exposed over MCP.
- **The user's app is in "Testing" mode**, so refresh tokens die after 7 days. Publishing it to
  "In production" is a precondition for the no-daily-login goal; the docs must walk the user through
  it, and the health state must say so when `invalid_grant` shows up.
- **Hand-rolled OAuth** (PKCE, loopback redirect) on `httpx2`, with `keyring` added as a
  dependency. No keyring backend means a clear failure, never a token file.
- **Token architecture** (a `TokenManager` per account):
  - The refresh token lives only in the keyring; the access token only in memory, refreshed
    about 5 minutes before expiry on a monotonic clock.
  - One refresh at a time per account (a lock), so concurrent calls share it.
  - A 401 forces one refresh and one retry; a second 401 is an auth failure.
  - A rotated refresh token is written back atomically, and the keyring is re-read before each
    refresh so a second process's newer token is used.
  - Network errors and 5xx from the token endpoint retry with backoff and never mark the account
    broken. `invalid_grant` sets a **needs re-authorization** health state, publishes an event and
    stops syncing that account: no loop, no crash.
  - Re-authorizing needs no restart; the next health or sync tick picks the new token up.
  - Document the causes of a revoked token: a "Testing" app, a password change, more than 100
    tokens per account and client, six months unused.
- **Another provider may be the sender.** An account has a receiving provider and an optional
  `send_via` naming any send-capable provider (see 11b). Wiring `send_via` through core is part
  of the later send prompt; here it is only documented.

## Acceptance criteria

- Full sync of a 2,000-message fake mailbox, interrupted midway and resumed, yields
  each message exactly once downstream; incremental sync then picks up adds, deletes and
  label/unread changes; expired history ID triggers a full resync.
- Rate-limit responses are retried with backoff without exceeding the concurrency cap.
- Refresh token lives in a fake keyring backend in tests; assert it never appears in
  logs, exceptions, config files or the DB.
- `flags.write` of `UNREAD` removal issues the correct `batchModify` body; label, trash and
  send calls issue the right requests against the fake server.
- Token tests (fake token endpoint): proactive refresh before expiry, one refresh for many
  concurrent calls, 401 then one retry, rotation persisted, a newer keyring token picked up, and
  `invalid_grant` giving the health state with no retry loop and recovery after re-authorizing.
- A manual, skipped-by-default `live_gmail` test + a documented checklist exists for
  the user's first real sync (read-only first: scope check, list 10 messages, no writes).

## Out of scope

Drafts, permanent delete, the outbox and confirm gate for sending, IMAP (11c), Pub/Sub push notifications (poll via the scheduler for now),
Gmail threading semantics (core computes `thread_key`).

## Docs site

Publish `docs/providers/gmail.md` as a site page (*Start* group: Connect Gmail), including
the Google Cloud OAuth setup steps and the 7-day "Testing" token note. `check_docs.py`
must pass.

## Checkpoint

Standard report. Include the Google Cloud setup steps you documented, and flag anything
about the first live sync the user must do by hand.

**Note from prompt 04:** write the Gmail provider as a `BaseProvider` subclass (override
`authenticate`, `pull`, `set_flags`; do not override `send`) and register it in `pyproject.toml`
under `oaken.providers`. Run `ProviderContract` against it with a recorded or faked Gmail client,
and read credentials from `ctx`/the keyring, never from `ctx.config`.
