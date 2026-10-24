# 06 — Parsing and cleaning (the first token-saver)

**Read first:** `docs/prompts/00-context.md`. **Depends on:** 05.

## Goal

Turn raw RFC 822 bytes into a structured, **deterministic**, LLM-free representation:
headers, bodies, attachments metadata, thread keys, and a *cleaned* text that drops
everything an LLM doesn't need to read. Cleaning is where most token savings come from.

## Requirements

1. **Parser** (`oaken/mail/parse.py`) using the stdlib `email` package with
   `policy.default`. Never raises on malformed mail: returns a `ParsedMessage` with a
   `warnings` list (bad charset → replace + warn, missing Date, broken MIME, etc.).
   Fields: `message_id`, `in_reply_to`, `references`, `subject`, `from`, `to`, `cc`,
   `reply_to`, `date` (UTC + original string), `list_id`, `headers` (all, lowercased
   keys, multi-valued preserved), `text_parts`, `html_parts`, `attachments`
   (filename, content-type, size, content-id, `inline` flag — **bytes not extracted here**).
2. **Thread keys** (`mail/threading.py`): derive a stable `thread_key` from
   `References`/`In-Reply-To`/`Message-ID`, with a normalized-subject fallback
   (strip `Re:`/`Fwd:`/list tags). Provider thread IDs, when present, are stored as
   hints only.
3. **HTML → text** with a conservative converter (add `selectolax` or `beautifulsoup4`+`lxml`;
   choose and justify). Drop `script/style/head`, tracking pixels, hidden preheaders;
   keep links as `text (url)` only when the text differs from the URL; preserve list/table
   structure well enough to be readable. Prefer `text/plain` when it is a faithful
   alternative; otherwise use the HTML conversion.
4. **Cleaner** (`mail/clean.py`) producing `CleanedText` with `segments`, each tagged
   `kind ∈ {new, quoted, signature, footer, disclaimer, boilerplate}` and a
   `cleaned` string = only `new` content. Rules (each independently testable, each
   with a unit test and a documented rationale): quoted-reply stripping (`>` blocks,
   "On … wrote:", Outlook `From:/Sent:/To:` blocks, Gmail `gmail_quote`), signature
   detection (`-- ` delimiter + heuristics), mailing-list footers (unsubscribe
   links, `List-*` driven), legal disclaimers, repeated whitespace/zero-width chars.
   **Never discard silently**: the original segments remain available and the cleaner
   reports `chars_in`, `chars_out`.
5. **Attachment text (stub only):** an interface `AttachmentExtractor` with an
   implementation for `text/plain` and a no-op default for everything else. Real
   extractors (PDF/Office) are out of scope.

## Acceptance criteria

- A fixture corpus of **synthetic** `.eml` files in `tests/fixtures/mail/` (author them
  by hand/generator: plain, HTML-only, multipart/alternative, nested forwards, quoted
  reply chains, mailing-list posts, non-UTF-8 charsets, malformed MIME, huge
  attachments, calendar invites, bounce messages) parses without exceptions.
- Golden tests: for each fixture, `cleaned` text equals an expected file. Include the
  `chars_in → chars_out` ratio per fixture in the test output.
- Thread-key tests: a reply chain across 4 messages gets one key; two unrelated threads
  with the same subject but disjoint `References` do not merge when `References` exist.
- Fuzz: `hypothesis` generating random bytes/headers never raises from `parse()`.
- No network, no LLM, deterministic across runs.

## Out of scope

Writing to any DB (07), language detection, attachment content extraction.

## Checkpoint

Standard report. Include a table of average cleaned/original size ratio per fixture
class so the user can see the expected token savings.

## Built differently

- **Settings, not constants.** Threading and cleaning are tuned in `[mail.threading]` and
  `[mail.cleaning]` (prefix lists, extra patterns, disabled rules, signature length), validated when
  settings load. Rules have names (`quote.prefix`, `footer.list`, ...).
- **No "drop at most 90%" guard.** Every segment is kept and tagged, so nothing is hidden.
- **Attachments are metadata only, with a quarantine flag.** The default `text/plain` extractor
  was dropped: `AttachmentExtractor` is an interface that nothing implements or calls by default.
  `read_attachment(raw, part_path)` fetches bytes on explicit request.
- **Token counts.** `tokens_in` and `tokens_out` come from a `Tokenizer`: an estimate by default,
  exact from a local `tokenizer.json` (`mail.tokenizer_file`, the `tokenizers` package). Nothing is
  downloaded. Dependencies added: `selectolax` and `tokenizers`.
- **Fixtures live next to the code** (`src/oaken/mail/tests/fixtures/`), with a golden
  `.cleaned.txt` beside each `.eml`; `OAKEN_UPDATE_GOLDENS=1 pytest` regenerates them.
- **The subject-fallback time window is not here.** It needs a look-up, so it belongs to the index.

## Note from prompt 06

Clean each entry of `ParsedMessage.forwarded` separately: a forwarded message is content, but
`clean()` of the outer message does not include it. For a reply with no reference headers,
`ThreadInfo.subject_key` is the hint for attaching it to the thread whose first message has the
same subject key. Store `CleanedText.tokens_out` and `chars_out`; the migrations continue at `0005`.
