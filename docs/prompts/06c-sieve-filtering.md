# 06c — Sieve filtering (organize mail as it lands)

**Read first:** `docs/prompts/00-context.md`. **Depends on:** 05, 06. **Before:** 07.

## Goal

Run the user's **Sieve** scripts (RFC 5228) over each message once it is in the local stream, and
store the result as **attributes in `raw.db`**: labels, flags and a disposition (inbox, filed,
discarded). Those attributes are what a UI uses to build a sane mailbox. Sieve is the one
filtering language mail people already know, so Oaken's rules are portable and reviewable.

Filtering is **deterministic and local**: no model, no network, no clock except the one handed in.

## Requirements

1. **Parser and interpreter** (`oaken/mail/sieve/`), written for Oaken (no external Sieve
   engine), pure like `oaken.mail`:
   - Lexer and parser for the full RFC 5228 grammar (comments, `text:` blocks, string lists,
     numbers with `K`/`M`/`G`, tagged arguments, `if`/`elsif`/`else`, nested tests). Errors carry a
     line number. A script is validated (`require`d extensions, known commands) before it runs.
   - Core tests and actions: `true`, `false`, `not`, `allof`, `anyof`, `header`, `address`,
     `envelope`, `exists`, `size`; `keep`, `discard`, `fileinto`, `stop`, `require`.
   - Extensions: `fileinto`, `copy`, `imap4flags`, `envelope`, `body` (text only), `relational`,
     `comparator-i;ascii-numeric`, `regex`, `variables`, `date`, `subaddress`, `reject`,
     `ereject`, `vacation`, plus `redirect` (part of the core). **Not supported** (a `require` of
     them is an error): `editheader` (the raw message is the truth), `include`, `notify`,
     `mime`/`foreverypart` and `body :raw`/`:content` (attachments are never opened).
   - Oaken extension `vnd.oaken.analysis` with the command `skip_enrich;` (the message is never sent
     to a model).
2. **Sandbox.** A step limit, a limit on the number of actions, a cap on script size and on the text
   a pattern is matched against, regular expressions with the standard `re` engine on bounded
   input. A script that errors at run time stops with an implicit keep and an error recorded; it
   never loses mail and never stops other scripts.
3. **Sending is never done.** `redirect`, `reject`, `ereject` and `vacation` are parsed and
   validated, then recorded as **suppressed intents** (visible to the user) and **never executed**.
   `editheader` is refused.
4. **Attributes in `raw.db`** (migration `0005`): a `message_attrs` table of
   `(message_id, kind, value, source, rule)` and a `message_filters` table recording which scripts
   (by hash) last ran on each message. `kind` is one of `disposition`, `label`, `flag`, `intent`,
   `analysis`. `source` is `sieve` (a user's own attributes can share the table later). A re-run
   replaces only the `sieve` rows of that message, in one transaction. The `rule` is
   `script.sieve:LINE`, so the UI can say why.
5. **Disposition.** Exactly one per filtered message: `inbox` (kept, or nothing decided),
   `filed` (`fileinto` and no keep) or `discarded` (`discard` and nothing else). **A discarded
   message is hidden from default views and never deleted.**
6. **Scripts** live in `config_dir()/sieve/*.sieve`, run in name order, `stop` ends all of them.
   Settings in `[mail.sieve]`. A script set has a hash; a message whose last run used another hash
   is stale.
7. **Service** (`oaken/mail/filtering.py`): `FilterEngine` loads scripts, `filter_message()` reads the
   raw bytes, parses and runs the scripts and writes the attributes (emitting `MessageFiltered`),
   `refilter()` re-runs stale messages (as a job, resumable, in prompt 09/16). It never calls a
   model and never opens an attachment.
8. **Check and test entry points**: `check_script(text) -> list[problem]` and
   `run_script(text, parsed, envelope)` returning the actions and the line that caused each, so the
   CLI (prompt 17) can offer `oaken sieve check` and `oaken sieve test`.

## Acceptance criteria

- The RFC 5228 examples (section 10 and the test and action examples) parse and give the
  documented results.
- Unit tests for every test, action and extension above, including comparators, `:matches`
  wildcards, `:regex`, `:count`/`:value`, variables with match variables, `date`, `body`,
  `imap4flags`, and the unsupported-extension errors.
- Safety tests: a step-limit loop, a huge string list, a pathological regular expression on long
  input, `redirect`/`reject`/`vacation` never reach any sender, and a script error leaves the
  message in the inbox.
- Fuzz: `hypothesis` generating random scripts never raises from the parser or interpreter except
  `SieveError`.
- Store tests: re-filtering replaces only `sieve` rows, is idempotent, is atomic, and the
  script-set hash marks stale messages.
- Determinism: the same message and scripts give identical attributes.
- No network, no LLM, no real mail.

## Out of scope

The CLI and API commands (17, 16) and the UI (19), pushing labels to a provider (12), `include`,
`editheader`, and anything that reads an attachment.

## Checkpoint

Standard report. List the supported and refused extensions and say how the disposition is chosen.

## Built differently

- **Written for Oaken, no dependency.** The parser, validator and interpreter are about a thousand
  lines in `oaken/mail/sieve/`. `:matches` is matched by dynamic programming (no backtracking);
  `:regex` is refused when it repeats a repetition (best effort, checked at compile time).
- **Sending actions are suppressed intents**, as in the requirements, and they do **not** cancel the
  implicit keep: a message is never removed from the mailbox because a script asked to forward it.
- **Flags belong to the message**, not to a copy: the flags in effect when the scripts finish. A
  discarded message carries none.
- **A script that fails to compile is skipped and reported** (the rest still run); one that fails at
  run time loses its own actions only.
- **`stop` ends all scripts**, not just the one it is in.
- **Extra pieces:** `RawStore.get_message`, the `MessageFiltered` event, `[mail.sieve]` settings,
  `AttrStore` in `oaken/core/attrs.py` and migration `0005`.
- A test in the repo-level `tests/` hygiene guard now exempts the synthetic `.eml` fixtures of 06;
  it was failing on them.
