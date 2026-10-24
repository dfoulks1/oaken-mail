# 11c — IMAP provider

**Read first:** `docs/prompts/00-context.md`. **Depends on:** 04, 05, 09.

## Goal

`oaken-imap`, a plain IMAP `Provider` for any server: sync, flags, and folder moves. It owns its
own auth (password in the OS keyring, TLS required). Top-level package `src/oaken_imap`.

## To settle in discussion before building

- Which IMAP library (or hand-rolled over `asyncio`), judged on async support and maintenance.
- Incremental sync with `UIDVALIDITY` and `UIDNEXT` (and `CONDSTORE`/`QRESYNC` when offered),
  with a full resync when `UIDVALIDITY` changes.
- How folders map to `provider_labels`, and which folders are skipped (Spam, Trash).
- Whether it also offers `send` (it should not: use 11b) and whether it offers `IDLE`.
- Flags written back: `\Seen`, `\Flagged`, move to Trash and Archive.

## Acceptance criteria (draft)

- Passes `ProviderContract` against a fake IMAP server; an interrupted sync resumes with each
  message delivered once; changed `UIDVALIDITY` triggers a full resync; the password never
  appears in logs; plaintext connections are refused.
- A skipped-by-default `live_imap` test and checklist.

## Checkpoint

Standard report.
