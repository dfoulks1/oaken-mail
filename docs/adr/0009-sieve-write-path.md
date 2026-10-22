# 9. Sieve scripts are written by a scoped, compile-first, owner-only write path

- Status: accepted
- Date: 2026-10-21

## Context

Sieve scripts organize a user's mail into labels, locations, flags and a disposition (prompt 06c,
ADR 0008). They are plain `*.sieve` files in the sieve folder beside `config.toml`, compiled and
run in **name order** by the filter engine (`oaken.mail.filtering`). Until now the client could
only read and test scripts (`sieve_list`, `sieve_check`, `sieve_test`); editing them meant opening
a file by hand. Prompt 26 adds a write path so the UI and CLI can save a script.

A write path to a file that core executes on every message is sensitive, so three questions had to
be settled before building it:

1. **Is a saved-but-invalid script ever allowed** (for example, saved while disabled), or is a save
   refused unless the script compiles?
2. **How is the on-disk write made safe** — against a partial file on failure, and against other
   processes reading the file owner-only?
3. **May an AI tool write a script?** The standing decision (prompt 00 context) is that *writing
   Sieve is never exposed over MCP*; this ADR records how that is enforced, not re-litigated.

## Decision

**Save is refused unless the script compiles.** `sieve_save(name, text)` compiles the script first
(reusing the same `compile_script` path as `sieve_check`) and persists it **only** if it compiles;
on a compile error it returns the error and its line and **writes nothing**. There is no
saved-but-invalid state and no per-script disable flag. An invalid script can therefore never enter
the run set, so one bad edit cannot silently break filtering for every later message. (A disabled
script is an open question deferred to a later prompt — prompt 26 Q1; today the "off switch" is to
delete the script or empty it.)

**Scripts live where they always have** — one `*.sieve` file per script in the sieve folder next to
`config.toml` — and the write path changes exactly one named file, leaving the others untouched.
Run order stays by file name, so a client names a new script `NN-name.sieve` and suggests the next
free `NN` (prompt 26 Q2). The file name is validated to a single path segment of `[A-Za-z0-9._-]`
ending `.sieve` (`check_script_name`): no path separators, no `..`, nothing outside the folder.

**The write is atomic and owner-only.** `save_script` writes to a temporary file in the same
directory with `O_EXCL` (mode `0o600`), `fsync`s it, then `os.replace`s it over the target — the
project's standard atomic-replace pattern — so a crash mid-write never leaves a partial or
half-parsed script in the run set, and a reader never sees a torn file. The final file is
`chmod 0o600` (owner read/write only), matching the confidentiality of a file that may name people
and mailing lists. `sieve_delete` removes exactly one named file and leaves the rest.

**Writing Sieve is never offered over MCP.** `sieve_save` and `sieve_delete` are `permission =
mutate`, confirm-gated, audited (the audit row holds the script name and a content hash, never mail
text), and declared `mcp = "never"` in the registry — the fixed ceiling that no user setting can
raise. `sieve_get` (needed to load a script into an editor) is also `mcp = "never"`, because a
script's text is the user's own configuration and may name correspondents. A test asserts all three
are absent from every MCP surface while the read/test ops (`sieve_check`, `sieve_test`,
`sieve_list`) behave as before. This is the explicit enforcement of the standing decision.

**Disposition precedence is unchanged.** A non-Sieve disposition still beats a script's, and
`discarded` mail is hidden from default search only, never from MCP (prompt 00 context). The Sieve
UI must not imply otherwise.

## Consequences

- No schema change and no new table: scripts remain files, and `reprocess('filter')` re-derives a
  message's organization from the kept raw bytes after an edit — no re-download, no reindex of mail.
- A saved script takes effect on the **next filter run**. The UI and CLI say so and offer a
  "Refilter now" affordance that enqueues the existing refilter job (`reprocess` with the `filter`
  stage) to re-run the scripts over stored mail at once.
- The one reader/writer of the sieve folder is `oaken.mail.filtering` (`scripts_dir`,
  `load_scripts`, `save_script`, `delete_script`, `check_script_name`); the operations layer calls
  it and owns no file logic of its own, so the CLI and UI reach the same code.
- Because a save that does not compile changes nothing, a client can treat `sieve_save` as both the
  validator and the writer: it shows the compile error and line on a refusal and the new `sha256`
  on success.
- A friendly rule builder over Sieve, and a per-script enable/disable, are explicitly **not** built
  here; either can be a later prompt that emits Sieve through this same `sieve_save` without
  changing the safety properties above.
