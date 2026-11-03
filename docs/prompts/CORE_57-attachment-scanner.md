# 57 — The attachment handler, `BaseScanner`, and the ClamAV scanner plugin

**Status:** built 2026-11-03. The design is ADR 0020 (ratified). Was prompt 52; renumbered to 57
because 52 was already the console-by-stage prompt (a prompt number is a permanent ID).
**Depends on:** the core quarantine (built, 0.2.0): `Vault`, `AttachmentAccess`, the `scan` route,
`PluginAttachments` (`list`, `copy_to`, `scanning`, `record_scan`), `reprocess(stage="scan")`.
**Track:** CORE (the two contracts), PLUGIN (ClamAV).

## Why

Core quarantines every attachment and releases one only after a scanner says `clean`. No scanner
exists, so nothing is ever released. Writing one today means hand-wiring the scan route, the 0400
open and the verdict. This task gives authors a small contract for that, then ships one scanner.

## Goal

1. **`AttachmentHandler`**: a helper in `oaken.plugins`, built from `ctx.attachments`. It adds no new
   door and no privilege; the `scan` route stays the only one that may open a quarantined file.
   - `list(message_id)`: the attachments with their scan state.
   - `scan(message_id, scanner)`: for each `pending` or `error` file, open it (`scanning`), call
     `scanner(path) -> Verdict`, record the result (`record_scan`). One bad file does not stop the
     rest.
   - `save(message_id, folder, ...)`: copy `clean` files out (`copy_to`, atomic write). Move the
     loop now in `oaken_actions/attachment.py` here, and have that action use it.
2. **`BaseScanner(BaseAction)`**: `routes = ("scan",)`, `auto_routes = ("scan",)` (safe unasked: it
   only reads and records). The author overrides one method, `scan(path: Path) -> Verdict`, where
   `Verdict` is `clean | infected | error` plus a `detail` string. `act` is implemented by the base
   through `AttachmentHandler`. It is idempotent: a `clean` file is not scanned again.
3. **`clamav`** plugin (`oaken_scanners`, entry point `oaken.actions`, `[plugins.action.clamav]`).
   Subclasses `BaseScanner`. Talks to `clamd` over its socket (`INSTREAM` or `SCAN` on the path;
   choose in the ADR, since the file is mode 0400 only inside the block and `clamd` may run as
   another user). Config: `socket` (unix path) or `host`/`port`, `timeout_seconds`. `clamd` down or
   a timeout is `error` (never `clean`), so the file stays held and `reprocess(stage="scan")` retries
   it. Infected: record the signature name in `detail`.

## Rules

- A scanner must never mark `clean` on doubt. Only an explicit "OK" from the engine is `clean`.
- Nothing reads attachment bytes into memory; stream to `clamd` in blocks.
- Never log file content or names in full; log the sha256 and the verdict.
- Prompt the reader to rescan (`oaken reprocess scan`) when a scanner is first enabled and files are
  pending. Use the existing `reprocess` door; add no new mechanism.
- No GPL code is copied in. `clamd` is a separate process the plugin only talks to.

## Tests

- A fake scanner through `BaseScanner`: clean, infected, error, and an exception in `scan`.
- `AttachmentHandler.scan` skips `clean`, retries `error`, and cannot open a file off the `scan`
  route (`PermissionDenied`).
- `clamav` against a stub `clamd` server (the EICAR string answers `FOUND`); down and timeout give
  `error`.
- The `attachment` action still saves only `clean` files after the move to `AttachmentHandler`.
- Contract snapshot regenerated; the plugin contract doc updated.

## Out of scope

The derived-message door (an extractor that turns a clean attachment into a new message), backfill
of pre-0.2.0 mail, and other scanners (VirusTotal, YARA).

## Open questions

- Name: `AttachmentHandler` (ADR 0020) or `AttachmentScanner`? The handler also saves, so I would
  keep `AttachmentHandler`.
- `clamd` `INSTREAM` (no file permission needed, slower) or `SCAN` by path (needs `clamd` to read a
  0400 file it may not own)?
- Where `Verdict` lives (`oaken.plugins.contracts`) and whether `BaseScanner` appears in the
  contract snapshot as its own kind or as an `action`.
