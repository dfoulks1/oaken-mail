# 11b — SMTP send-as provider

**Read first:** `docs/prompts/00-context.md`. **Depends on:** 04, 11.

## Goal

`oaken-smtp`, a `Provider` plugin that does **one thing**: send a message through a
user-configured SMTP server. Its only capabilities are `auth` and `send`. It does not sync,
list, flag or delete, and must not claim those capabilities. Top-level package
`src/oaken_smtp`.

## Requirements

1. `send(raw_bytes)` submits the RFC 822 bytes unchanged (the envelope sender and recipients are
   taken from the message headers, or given explicitly) and returns the message id the server
   accepted. Bcc recipients go in the envelope only, never left in the headers that are sent.
2. Settings under `[plugins.smtp]`: `host`, `port`, `security` (`starttls` or `tls`, **no
   plaintext option**), `username`, `from_address`, `timeout`. The password is stored only in
   the OS keyring (service `oaken`, one entry per account) and never appears in config, logs or
   exceptions.
3. `authenticate` logs in once and reports a health state; a wrong password is "re-enter the
   password", not a retry loop. Connections are not held open between sends.
4. Failures are reported per recipient (accepted, refused with the server's code). A transient
   failure (4xx, network) is distinguishable from a permanent one (5xx) so a caller can retry
   only the former.
5. Testing without a server: an in-process fake SMTP server in `tests/`; the plugin passes the
   provider contract for its declared capabilities, and a test asserts that it declares only
   `auth` and `send`.

## Acceptance criteria

- A message is submitted once over STARTTLS and once over implicit TLS against the fake server.
- A server that offers no TLS is refused, never used in the clear.
- The password never appears in logs, exceptions or the DB (fake keyring).
- A Bcc address receives the message and is absent from the transmitted headers.
- A manual, skipped-by-default `live_smtp` test and a short checklist exist.

## Out of scope

Receiving, queueing and retry of outgoing mail (the later send prompt), OAuth for SMTP.

## Docs site

A short "Send through your own SMTP server" page; `check_docs.py` must pass.

## Checkpoint

Standard report.
