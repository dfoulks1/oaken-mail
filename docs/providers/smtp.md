+++
title = "Send through your own SMTP server"
subtitle = "A send-only provider with the password in your keyring and TLS always on"
lead = "Oaken can send mail through any SMTP server you already use. The plugin does one thing: it submits a message over an encrypted connection and reports who accepted it."
badges = [["text-bg-warning", "Pre-alpha"]]
group = "start"
order = 13
description = "Configure the SMTP send plugin: host, port, STARTTLS or TLS, the keyring password, what happens to Bcc, how failures are reported, and a first live check."
+++

## What the plugin does

`oaken-smtp` is a provider plugin that only **sends**. It declares two capabilities, `auth` and
`send`, and nothing else: it does not read, list, flag or delete mail, so it is never your mailbox.
Pair it with a provider that does read mail (Gmail, or IMAP later) by naming it as the account's
`send_via`.

Sending is only a method on the plugin. Nothing in Oaken calls it on its own, and it is never
exposed over MCP; the outbox, audit and confirmation step come in a later prompt.

## Configure it

Add a table to `config.toml`. There is no password setting, on purpose.

```toml
[plugins.smtp]
host = "smtp.example.com"
security = "starttls"        # "starttls" (port 587) or "tls" (port 465); there is no other choice
username = "me@example.com"  # defaults to from_address
from_address = "me@example.com"
# port = 587                 # only if your server uses a different one
# account = "default"        # names the keyring entry, for more than one SMTP account
# timeout = 30
```

Then store the password in your operating system's keyring (service `oaken`, entry
`smtp:<account>`). It is never written to a file, a log or an error message. If you use two-step
verification, most providers want an **app password** here instead of your normal one.

## Always encrypted

- `starttls` connects and **requires** the server to upgrade to TLS. If the server does not offer
  it, nothing is sent and the password is never transmitted: the plugin refuses rather than fall
  back to plaintext.
- `tls` is encrypted from the first byte.
- The server's certificate is always checked against your system's trusted authorities. There is no
  option to skip the check.

## What gets sent

The message goes out byte for byte, with two exceptions:

- **Bcc.** Bcc recipients are added to the envelope and the `Bcc` header is removed, so no
  recipient can see them. (A `Resent-Bcc` header is removed too.)
- **Message-ID.** If the message has none, one is added. The plugin returns the `Message-ID` of what
  it sent.

The envelope sender is `from_address`. The recipients are the addresses in `To`, `Cc` and `Bcc`,
without duplicates. A message with no recipient, or an address that is not an address, is refused
before the plugin connects.

Each send opens its own connection and closes it again. Nothing is held open between sends.

## When something goes wrong

A send either works for every recipient or raises `SendFailed`, which says exactly what happened:

| Field | Meaning |
|---|---|
| `accepted` | Recipients the server took the message for. It is on its way to them, so a retry must leave them out. |
| `refused` | Each other recipient and the server's reply code and text. |
| `transient` | `True` if trying again later can work (a 4xx reply or a network failure). `False` if it cannot (a 5xx reply, a refused TLS requirement, an untrusted certificate). |

A wrong password is not retried. The server's reply (530, 534, 535 or 538) makes the plugin
unhealthy with "needs a new password" and publishes an event. It makes no further attempt, so there
is no lockout loop, until you store a different password. No restart is needed: the next health
check notices the new one.

## First live check, by hand

The plugin's tests run against an in-process fake server, so they prove the protocol handling and
not that your provider accepts it. To try yours:

1. Add `[plugins.smtp]` and store the password as above.
2. Run `uv run pytest -m live_smtp`. By default it connects, requires TLS and logs in, and sends
   nothing.
3. To send one short test message, set `OAKEN_LIVE_SMTP_TO` to an address you own and run it again.
