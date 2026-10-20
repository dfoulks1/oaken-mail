+++
title = "Connect any IMAP server"
subtitle = "A generic provider: sync, flags, Trash and Archive, with TLS always on"
lead = "Oaken can mirror a mailbox from any server that speaks IMAP. The plugin knows the standard and nothing about any one service, so it works the same against Dovecot, Fastmail, iCloud or your own server."
badges = [["text-bg-warning", "Pre-alpha"]]
group = "start"
order = 14
description = "Configure the IMAP provider: host, port, STARTTLS or TLS, the keyring password, which folders are synced, how changes are found, what is written back, and a first live check."
+++

## What the plugin does

`oaken-imap` is a provider plugin. It declares three capabilities, `auth`, `sync.pull` and
`flags.write`, and nothing else. It does **not** send: sending is the job of a send-only provider
such as [your own SMTP server](smtp.html), named as the account's `send_via`.

It is deliberately generic. **For Gmail, use the [Gmail provider](gmail.html)**: it uses Google's
own API, which understands labels, and this plugin does not. Using Gmail's IMAP front end here works
for reading, but every label becomes a separate folder and mail with several labels arrives more than
once.

## Configure it

Add a table to `config.toml`. There is no password setting, on purpose.

```toml
[plugins.imap]
host = "imap.example.com"
username = "me@example.com"
security = "tls"             # "tls" (port 993) or "starttls" (port 143); there is no other choice
# port = 993                 # only if your server uses a different one
# account = "default"        # names the keyring entry, for more than one IMAP account
# include_folders = ["INBOX", "Work"]   # empty means every folder
# exclude_folders = ["Newsletters"]
# trash_folder = "Deleted Items"        # only if the server does not say which folder is Trash
# archive_folder = "Archive"
# page_size = 25             # messages fetched per command
# flag_scan_limit = 2000     # see "How changes are found"
# timeout = 60
```

Then store the password in your operating system's keyring (service `oaken`, entry
`imap:<account>`). It is never written to a file, a log or an error message. If you use two-step
verification, most providers want an **app password** here instead of your normal one.

## Always encrypted

- `tls` is encrypted from the first byte.
- `starttls` connects and **requires** the server to upgrade to TLS before anything else. If the
  server does not offer it, nothing is sent and the password is never transmitted: the plugin
  refuses rather than fall back to plaintext.
- The server's certificate is always checked against your system's trusted authorities. There is no
  option to skip the check.

## Folders and labels

Every folder is a label on the messages in it, and `INBOX` is `INBOX`. Nested folders keep the path
the server uses (`Lists/Dev`). Names with accents or other scripts are decoded from IMAP's modified
UTF-7.

**Trash and Junk are never synced.** The plugin trusts the server's own markings (the special-use
attributes `\Trash` and `\Junk`) and, only when the server gives none, guesses from the usual names
(`Trash`, `Deleted Items`, `Junk E-mail`, `Spam`). A message moved into one of them is reported as
deleted. Every other folder is synced, including Sent, Drafts and Archive, unless you narrow it with
`include_folders` or `exclude_folders`.

The markings are read from the ordinary `LIST` reply. A server that only reports them when asked
(`LIST-EXTENDED`) is treated as having none, so the name guesses apply; set `trash_folder` and
`archive_folder` if a guess is wrong. The plugin uses the BSD-licensed `imapclient` library, run in
a worker thread so the rest of Oaken never waits on the network.

A message that is in two folders is two messages to the server. Oaken sees the same mail twice and
stores its bytes once.

## How changes are found

Each folder is tracked on its own. The sync cursor remembers, per folder, the `UIDVALIDITY` and the
UIDs already delivered (as ranges, such as `1:5400,5402`), and a mod-sequence when the server keeps
them.

- **New mail** is whatever the server has that was not delivered. An interrupted sync therefore
  resumes where it stopped, and nothing is delivered twice.
- **Deletions** are whatever was delivered and the server no longer has.
- **Flag changes** (read on your phone, starred elsewhere) come from `CONDSTORE`: the server is asked
  only for what changed since the last mod-sequence. A server without `CONDSTORE` cannot say, so the
  plugin reads the flags of the newest `flag_scan_limit` messages in each folder again (2000 by
  default; `0` turns it off). Older changes are not noticed on such a server.
- **A changed `UIDVALIDITY`** means the server has renumbered the folder, so its old UIDs mean
  nothing. The folder's old messages are reported deleted and it is read again from the start. This
  is announced as an `ImapFolderReset` event.

Reading never marks mail read: messages are fetched with `BODY.PEEK`. A removed flag is only known
for the standard ones (`\Seen`, `\Flagged`, `\Answered`, `\Draft`, `\Deleted`); a keyword removed on
the server is not reported.

The plugin does not use `IDLE`. The scheduler polls, as it does for Gmail.

## What is written back

| Change | On the server |
|---|---|
| Read, starred, answered, draft | `\Seen`, `\Flagged`, `\Answered`, `\Draft` |
| Any other flag name | A keyword, if the server allows new ones |
| `trash` | Moved to the Trash folder |
| `archive` | Moved to the Archive folder |
| `move` | Moved to the folder you name |

`\Deleted` is never set by a flag change, because it is how mail is destroyed. A move uses `MOVE`
when the server offers it. Otherwise the message is copied and the original is removed with a UID
expunge, which only touches that message. A server with neither is refused, because a plain
`EXPUNGE` would also destroy any other mail you had marked deleted.

A change is applied to every message it can be and **reported per message** for the rest
(`PartialFailure.failed` maps each message id to why). An id from before a `UIDVALIDITY` change fails
rather than act on the wrong message. `trash` and `archive` need a folder: the one the server marks,
or `trash_folder` and `archive_folder` if it does not.

## When something goes wrong

A wrong password is not retried. The server's refusal makes the plugin unhealthy with "needs a new
password" and publishes an `ImapNeedsPassword` event. It makes no further attempt, so there is no
lockout loop, until you store a different password. No restart is needed: the next health check
notices the new one.

Connection failures say which kind they were. A timeout or refused connection is worth retrying; a
missing STARTTLS, an untrusted certificate or a server that is not IMAP is not.

## First live check, by hand

The plugin's tests run against an in-process fake server, so they prove the protocol handling and not
that your provider accepts it. To try yours:

1. Add `[plugins.imap]` and store the password as above.
2. Run `uv run pytest -m live_imap`. It requires TLS, logs in and reads up to ten messages from
   `INBOX` (set `OAKEN_LIVE_IMAP_FOLDER` for another folder). It writes nothing and marks nothing
   read.
