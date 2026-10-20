+++
title = "Connect Gmail"
subtitle = "Bring your own Google OAuth client, authorize once, and stay signed in"
lead = "Oaken reads and writes your Gmail through Google's REST API with an OAuth client that you create. The refresh token lives only in your operating system's keyring, and it is renewed quietly for as long as Google allows."
badges = [["text-bg-warning", "Pre-alpha"]]
group = "start"
order = 12
description = "Create a Google Cloud OAuth client, publish it so tokens do not expire after 7 days, authorize, configure the Gmail plugin, and run a read-only first sync. Covers scopes, the token design and what to do when Google revokes a token."
+++

## What the plugin does

`oaken-gmail` is a provider plugin. It:

- **reads**: a full first sync of All Mail (never Spam or Trash), then incremental syncs from
  Gmail's history, handing core the raw RFC 822 bytes, the labels, and the read and starred state;
- **writes back**: mark read or unread, star, archive, add and remove labels, create, rename and
  delete your own labels, move to Trash and back, and **send** (including from a send-as alias you
  have set up in Gmail);
- **never** deletes permanently and never touches drafts in this version.

Sending is only a method on the plugin. Nothing in Oaken calls it on its own, and it is never
exposed over MCP; the outbox, audit and confirmation step come in a later prompt. An account can
also send through a different provider (a plain SMTP server) with `send_via`, described in that
later work.

## Create the Google Cloud OAuth client

Oaken ships no client, so there is no shared secret and nobody else's quota to compete for.

1. In the [Google Cloud console](https://console.cloud.google.com/) create a project (any name).
2. **APIs and services, Library**: enable the **Gmail API**.
3. **OAuth consent screen**: choose **External**, fill in an app name and your email, and add
   yourself under **Test users**. Add the scope `https://www.googleapis.com/auth/gmail.modify`.
4. **Publish the app** (the step that matters most, below): on the consent screen press
   **Publish app** so the status reads **In production**.
5. **Credentials, Create credentials, OAuth client ID**, application type **Desktop app**. Download
   the JSON and keep it somewhere private, outside this repository. Oaken reads it when it starts
   and never copies it.

### Why "In production", even for an app only you use

While an app is in **Testing**, Google expires its refresh tokens after **7 days**. That is the
"log in again every week" problem. In production there is no 7-day limit. You do not need Google's
verification for your own use: an unverified app works for its owner, behind an "unverified app"
warning the first time you authorize, which you click through. (Verification, and a paid security
assessment, are only needed to give a client to other people, because `gmail.modify` is a
*restricted* scope.)

## Configure and authorize

```toml
[plugins.gmail]
client_secret_file = "~/secrets/oaken-google-client.json"   # the JSON you downloaded
account = "default"          # names the keyring entry: oaken / gmail:default
# include_labels = []        # label names or ids; empty means all mail
# exclude_labels = ["Newsletters"]
# page_size = 100            # messages per batch of the first sync
# concurrency = 8            # requests in flight at once
```

Authorizing opens Google's consent page in your browser, with a PKCE challenge and a redirect to a
random free port on `127.0.0.1`. Oaken listens there once, checks the `state`, exchanges the code,
confirms Google granted `gmail.modify`, and stores the refresh token in the keyring. The CLI
command for this arrives with the CLI work (see the audit table in prompt 17); until then the
plugin's `authorize()` method is the entry point.

Without a keyring service (Secret Service on Linux, Keychain on macOS, Credential Locker on
Windows) Oaken stops with a clear message. It never falls back to a file.

## How tokens stay valid

- The **refresh token** is in the keyring only. The **access token** is in memory only.
- The access token is renewed about five minutes before it expires, measured on a monotonic clock.
- Many tasks asking at once share **one** renewal. A `401` renews once and retries once; a second
  `401` is a clear authorization error, not a loop.
- If Google sends a rotated refresh token it is written back at once. The keyring is read again
  before every renewal, so a token written by another process, or by authorizing again, is used
  without a restart.
- Network errors, `5xx` and `429` from Google's token endpoint are retried with backoff and never
  mark the account as broken.
- Only `invalid_grant` does. Then the plugin reports **needs re-authorization** in its health,
  publishes a `GmailNeedsReauthorization` event, and stops asking Google until the stored token
  changes. Authorize again and the next sync tick carries on, no restart.

### Why Google revokes a token

`invalid_grant` means Google no longer honours the refresh token:

| Cause | Fix |
|---|---|
| The app is still in **Testing** (tokens last 7 days) | Publish it to **In production**, then authorize again |
| The Google account password changed (for a token with mail scopes) | Authorize again |
| You removed Oaken at your Google account's third-party access page | Authorize again |
| More than 100 live tokens for this account and client | Authorize again; older ones are dropped |
| The token went unused for six months | Authorize again |

## What is synced and how

The first sync lists All Mail in pages (Spam and Trash are never included) and fetches each message
as raw bytes with a capped number of requests at once, backing off on `429`, rate-limit `403` and
`5xx` and honouring `Retry-After`. The mailbox `historyId` is read **before** listing, so mail that
arrives meanwhile is picked up by the next incremental sync. The cursor records the page, so an
interrupted first sync resumes where it stopped.

Later syncs ask for what changed: new mail, deletions, and label changes. A message moved to Spam
or Trash is reported as deleted (it is out of scope); one brought back is delivered again. Read and
starred changes become `\Seen` and `\Flagged` flag changes. If Google answers `404` because the
history id is too old, Oaken does a full resync on its own and publishes `GmailFullResync`.
Deletions that happened while the history had expired are not seen by that resync.

## Scopes

One scope: `https://www.googleapis.com/auth/gmail.modify`. Google describes it as "read, compose,
and send emails" and it excludes only immediate permanent deletion, which would need the full
`https://mail.google.com/` scope; Oaken deliberately does not ask for that. Labels, Trash,
batch changes, history and sending are within it.

Listing your send-as aliases reads Gmail *settings*. Google's scope page does not say whether
`gmail.modify` covers that call, so `list_send_as()` may answer `403` on some accounts. Sending as
an alias does not depend on it: Gmail keeps the `From` header when it is a verified alias.

## First live sync, by hand

Do this once, read-only first. The test is skipped unless you ask for it.

1. Publish the app (above), create the client, and set `client_secret_file`.
2. Authorize. Check the keyring now has an entry `gmail:default` under the service `oaken`.
3. Run `uv run pytest -m live_gmail`. It checks the token and scope, lists ten messages and writes
   nothing.
4. Only then let a real sync run. Watch for `GmailNeedsReauthorization` and `GmailFullResync`.
5. Rotate any older OAuth client secret you used with other tools.

## Limits

Gmail's quota is per user and per project; a first sync of a large mailbox takes a while by design.
Push notifications (Pub/Sub) are not used: Oaken polls on the scheduler. Threading is computed by
core from headers, not taken from Gmail. Drafts, permanent delete and the IMAP provider are
separate work.
