# 20. Attachments live in a core quarantine, and only a clean scan releases one

- Status: accepted (ratified 2026-11-03 by the owner)
- Date: 2026-11-03
- Rests on: ADR 0019 (the action kind, the attachment lane), ADR 0016 (plugin folders)
- Amends: ADR 0019 decision 5 (rewritten in place; 0019 is not yet ruled)
- Built: 2026-11-03, written but not run or verified by the owner's checks. Owed: the rescan prompt,
  backfill of old mail, a scanner plugin, provider streaming. Proof: `src/oaken/mail/tests/test_quarantine.py`.

## Decision

1. **Core moves every attachment out of `raw.db` when mail is stored.** `RawStore.put` calls the vault
   first, before Sieve, triage or any plugin. The body is streamed to
   `quarantine/<sha[:2]>/<sha>.quarantined`, mode 0000, named by its sha256. A stub
   (`X-Oaken-Detached`) stays in the stored message, so the parser still reports the true size and
   hash. `message/rfc822` parts stay inline; their own attachments are detached.
2. **It is core, not a plugin and not Sieve.** Turning a plugin off, or a script, cannot let an
   attachment through. The only off switch is `[mail.attachments] quarantine = false`, set by hand.
3. **Nothing is released until a scanner says `clean`.** A scanner is an action on the `scan` route
   (ADR 0019). Core queues that route for any message with quarantined files. The file opens (0400)
   only inside one block, for a scan or a release, then returns to 0000. With no scanner installed,
   nothing is ever released.
4. **Infected files stay.** Status `infected`, mode 0000, never released. Purging is a separate,
   deliberate step.
5. **No override.** A file that is `pending` or `error` is not released by anyone. Only `clean`.
6. **Release is a copy.** `PluginContext.attachments.copy_to` streams a clean file to a destination
   (the `attachment` action's `save` route). Plugins never read bytes into memory, and the old whole-
   bytes `read` is gone.
7. **New mail only.** Old mail keeps its inline attachments. A flagged inline one is never released.
8. **Re-scanning is the existing requeue.** When a scanner is installed later, core prompts the owner
   to requeue pending files with `reprocess(stage="scan")`. That one door, widened with `triage`, `action`
   (with a route), `scan` and `start`, is how every redo is asked: re-scan, re-triage, re-enrich,
   re-action, and requeue from the beginning share one shape. No new rescan mechanism.
9. **Re-syncing is a no-op.** Detach is deterministic: the same bytes give the same stub and file.

## Context

0019 left open "how an action reads attachment bytes, and how quarantine's rule survives." The first
build copied flagged attachments to a folder and left the originals in `raw.db`. Nothing was
quarantined. The owner: "we don't store attachment data in the database, we store it in a quarantine
(no perms so nothing can run, and then we run virus scanners and whatnot on it)" and, on the copy
build, "this is unsafe and I can't release it this way. attachment quarantining needs to actually be
a quarantine." Rulings, all cited: all attachments; mode 0000 with a brief 0400 open for a scan; the
original sha256 as key; new mail only; Sieve never handles it.

## Consequences

- "Raw is exact bytes" is no longer true. `raw.db` holds the message with stubs; the bytes of an
  attachment live in the vault. Anything that rebuilt a message from `raw.db` alone no longer gets
  its attachments.
- A part the streamer cannot locate stays inline and is logged. Failing the ingest would lose mail.
- Providers still pass the whole message in memory to `put`. Streaming that is owed.
- Backup must include the quarantine folder and `quarantined_files` (migration 0024).
- **`AttachmentHandler` is a new plugin contract** (later, not built). A helper in `oaken.plugins` that
  takes `ctx.attachments` and offers `list`, `scan` (open each pending file, record the verdict) and
  `save` (copy clean files out). It uses only the existing doors, so the `scan` route stays the one
  privilege. The `attachment` action's `save` route and any other attachment action import it.
- A scanner is `BaseScanner(BaseAction)`, a thin base that holds an `AttachmentHandler` and asks the
  author for one method, `scan(path) -> clean | infected | error`. Later. ClamAV is a later plugin on
  it. This ADR fixes the contract it plugs into.
- A derived-message door (an extractor turns a clean attachment into a new message that re-enters
  the pipeline) is a follow-up ADR.

## Confirmed by

The tests above, once run; the owner's check that a stored message holds a stub and the file is
mode 0000.

## Alternatives

- **A plugin does the quarantine.** Overruled: a disabled plugin lets attachments through.
- **Sieve moves them.** Overruled by the owner: Sieve runs after `put` and cannot skip it.
- **Copy, leave the original.** Built first, overruled: nothing was quarantined.

## History

- 2026-11-03: drafted from the owner's words and four answers (edit 0019 in place; infected files
  kept; no override; rescan by the existing requeue, with a prompt). Not ratified. Only the owner
  ratifies.
- 2026-11-03: ratified by the owner. `AttachmentHandler` and `BaseScanner(BaseAction)` built on it
  (CORE_57).
