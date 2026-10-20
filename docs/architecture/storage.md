+++
title = "Raw storage"
subtitle = "Where your mail lives, and why nothing else is the truth"
lead = "Every message is kept exactly as the server sent it, compressed and stored once. Everything else Oaken knows is derived from it and can be rebuilt."
badges = [["text-bg-warning", "Pre-alpha"]]
group = "concepts"
order = 27
description = "raw.db: content-addressed zstd messages, the LFS folder for large mail, soft deletes, garbage collection, sync cursors and accounts."
+++

## One source of truth

`raw.db` holds the original RFC 822 bytes of every message plus the little a provider needs to
sync again. The search index, vectors and enrichments live elsewhere and are rebuilt from it, so
losing them costs time, never mail. Back up `raw.db` (and the `lfs/` folder beside it) and you have
backed up everything.

| Table | What it holds |
| --- | --- |
| `accounts` | One row per mailbox: id, provider, display name and **non-secret** config. |
| `blobs` | One compressed message per distinct content, keyed by its sha256. |
| `messages` | A provider's id for a message, pointing at a blob, with a soft-delete mark. |
| `message_state` | Read, starred, labels, and what still has to be written back to the server. |
| `sync_cursors` | Where each provider's sync left off, per scope. |

## Stored once

A message is compressed with zstd (with a checksum) and keyed by the sha256 of its raw bytes. Two
provider ids that carry identical bytes share one blob. Putting the same provider id twice is a
no-op. If the bytes changed, a new blob is added and the message points at it. The old blob stays
until `gc_blobs()` runs, and `gc_blobs()` never removes a blob any message still points at.

Reading a message back always gives the identical bytes: invalid UTF-8, NUL bytes and enormous
headers included. Ask for `verify=True` and the bytes are hashed again and compared; any
difference raises `StorageError` instead of returning damaged mail.

## Large messages go to LFS

Mail up to `storage.inline_max_bytes` (8 MiB) is stored inside `raw.db`. A bigger message is
written whole to the **LFS folder** (`lfs/` next to the databases, one file per blob), and `raw.db`
keeps a link to it plus a small header stub. Nothing is ever truncated, and listing or parsing
headers never has to open the big file.

- A large message announces itself with a `LargeMessageStored` event (and a log line) so a UI or
  the CLI can tell you. Past `storage.max_message_bytes` (100 MiB) the event is marked
  `oversize`, which is worth a warning, but the message is still kept.
- The LFS folder is owner-only, and its files are written atomically. `gc_blobs()` removes the
  files of blobs it removes, and sweeps stray files older than an hour.

## Deleting

Deleting is soft: the message gets a `deleted_at` time and drops out of listings, but its bytes
stay readable. If the provider presents the same message again it is restored, and a `MessageStored`
event with `changed` set tells everything downstream to look again.

## Events

`MessageStored` is published after the commit, with `is_new` (first time seen) and `changed`
(different bytes, or restored). A repeat of the same message publishes nothing. Plugins can add
their own events too (see [Writing a plugin](../plugins.html#plugin-events)).

## Accounts and secrets

Account setup writes the account's settings into `accounts.config_json`, so the CLI wizard stores
them for you instead of asking you to edit files. Only non-secret settings go there. Credentials
belong in the OS keyring, and the store refuses a config key that looks like a secret (`token`,
`secret`, `password`, `api_key` and the like).

## Safety

- Database files and the LFS folder are readable by their owner only.
- Message content never reaches a log: log lines carry ids, sizes and hashes, and a test checks
  this by searching the captured log for the body.

## Settings

```toml
[storage]
inline_max_bytes = 8388608     # larger messages go to the LFS folder
max_message_bytes = 104857600  # larger still: kept, but flagged oversize
lfs_dir = "lfs"
```
