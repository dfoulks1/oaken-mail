+++
title = "Parsing and cleaning"
subtitle = "From raw mail to the text a model should actually read"
lead = "A message goes from bytes to structured fields, a thread key and cleaned text with no database, no network and no model involved. Cleaning is where most of the token savings come from."
badges = [["text-bg-warning", "Pre-alpha"]]
group = "concepts"
order = 28
description = "The parser, the cleaning rules and why each exists, thread keys, the tokenizer, attachments and quarantine, and every setting that tunes them."
+++

## The shape of it

```text
raw bytes ──parse──▶ ParsedMessage ──clean──▶ CleanedText      (segments, cleaned, counts)
                          │
                          └──thread_key──▶ ThreadInfo
```

Everything here is **pure and deterministic**: the same bytes and settings always give the same
result, nothing is written anywhere, and nothing calls a model. That makes it cheap to run on every
message at ingest and easy to test. Prompt 07 stores the results in `index.db`; because the raw
message is kept, any change to a rule is a rebuild, never a re-download.

## Parsing

`parse(raw)` uses the standard library's `email` package and **never raises**. Anything it cannot
read becomes a line in `warnings`: an unknown or wrong charset (decoded with replacements), a
missing `Date`, a multipart with no readable parts (its body is read as text), MIME defects.

Damaged base64 is repaired where it can be (missing padding, a cut-off tail, stray characters) and
flagged. A part that claims to be base64 but mostly is not gives no text, rather than its raw
garbage posing as content.

The result has the addresses, `Message-ID`, `In-Reply-To` and `References` (as ids), the subject, the
date in UTC with the original string, the `List-Id`, every header (lower-case keys, repeated headers
kept), the text and HTML bodies, attachment metadata, and any `message/rfc822` parts parsed as
messages of their own (`forwarded`). A forwarded message is content, so ingest cleans each one
separately.

## Cleaning

`clean(parsed)` first picks a body: the `text/plain` part when it holds at least
`plain_min_ratio` (half, by default) of the HTML's text, otherwise the HTML converted to text. A
plain part that only says "view this in your browser" loses to the real HTML.

It then tags **every line** with exactly one kind and groups neighbours into segments. Only `new`
text goes to `cleaned`. Nothing is thrown away: the full segment list stays on the result, along
with `chars_in`, `chars_out` and token counts, so a rule that is too eager can always be seen.

| Rule | Tags as | What it does and why |
| --- | --- | --- |
| `quote.prefix` | quoted | Lines starting with `>`. A reply quotes what the reader already has. Text between quote blocks (an interleaved reply) stays new. |
| `quote.attribution` | quoted | An "On … wrote:" line (it may wrap over lines, and has German, French, Spanish, Italian, Portuguese, Dutch and Danish forms). If `>` lines follow, only they are quoted; if the marks were lost (HTML to text), everything after is. It needs an `@` or a digit, so "as the author wrote:" is not mistaken for one. |
| `quote.original_marker` | quoted | `-----Original Message-----` and its translations: everything after it is the old message. |
| `quote.header_block` | quoted | An Outlook-style `From:` / `Sent:` / `To:` / `Subject:` block starts the old message. A block right after a "Forwarded message" line is **not** a quote: a forward's content is what the user sent you. |
| `signature.delimiter` | signature | The `-- ` line and up to 25 lines after it, stopping at a quote. |
| `signature.closing` | signature | A closing phrase ("Thanks,", "Best regards,") followed by a short block (at most `signature_max_lines`, each line under 100 characters) at the end of the new text. The closing phrase itself stays. |
| `footer.list` | footer | Mailing-list and newsletter footers (unsubscribe links, "you are receiving this", preference links, mailman info, copyright). One hit is enough when the message looks bulk (`List-Id`, `List-Unsubscribe`, `Precedence`, `Auto-Submitted`); two are needed otherwise, so a personal mail that mentions "unsubscribe" is safe. A separator line just above the footer goes with it. |
| `disclaimer.legal` | disclaimer | Legal and "please consider the environment" paragraphs among the last four. A message that is nothing but a disclaimer is kept, because then it is the message. |
| `boilerplate.stock` | boilerplate | Stock lines: "Sent from my iPhone", "Get Outlook for iOS", image placeholders. |

Before any rule runs, the text is normalized: line endings, non-breaking spaces, repeated spaces,
runs of blank lines, control characters, and **invisible characters** (zero-width space, directional
overrides, byte-order marks, soft hyphens). Those are used to hide text from readers and reorder
it, and they cost tokens. The zero-width joiners that emoji and some scripts need are kept.

### HTML to text

HTML goes through `selectolax` (the lexbor parser: fast, forgiving, one dependency). It drops
`script`, `style`, `head`, hidden elements (preheaders: `display:none`, `max-height:0`,
`mso-hide:all` and friends) and tracking pixels. Links become `text (url)` only when the text
differs from the URL. Lists keep `-` and `1.` markers, a table with several columns becomes
`a | b` rows, a layout table becomes plain text, and quoted blocks (`blockquote`, Gmail's
`gmail_quote`) get `> ` markers so the cleaner sees the same thing it sees in plain text.

## Thread keys

`thread_key(parsed)` gives a stable key from one message alone, so it can be recomputed any time.
The reference design is [jwz's threading algorithm](https://www.jwz.org/doc/threading.html).

- A reply carries the root's id first in `References`, so every message in a chain shares the key
  `mid:<root id>`. Two threads with the same subject but different roots never merge.
- With no `References`, `In-Reply-To` names the parent, and with neither, the message's own id is
  the key.
- A reply with no reference headers at all (some clients drop them) falls back to a hash of the
  normalized subject and the participants. `subject_key` is always available, so the index can
  attach such a reply to the thread whose first message has the same subject key.
- A limit such as "do not merge a recurring subject across months" needs a look-up, so it belongs
  to the index (prompt 07), not to this stateless function.

## Attachments

Oaken analyses email, not attachments. The parser records each attachment's filename, type, size,
`Content-ID`, whether it is inline, and where it sits in the message (`part_path`). It does not
decode or extract any bytes.

- **No copy.** The bytes are already inside the raw message, in `raw.db` or in the LFS folder.
  `read_attachment(raw, part_path)` is the explicit door to them, and nothing in ingest calls it.
- **Quarantine.** An attachment is flagged `quarantine` when its extension or declared type is on
  the risk list (executables, scripts, macro-enabled Office files, archives, disk images), or its
  filename hides its type with a right-to-left override. "invoice.pdf.exe" is judged on `exe`. The
  flag means "never opened, previewed or served inline without a deliberate action".
- **Extractors are opt-in.** `AttachmentExtractor` is the interface a future "analyse this
  attachment" request would use. Nothing implements it by default, and nothing in ingest calls it.

## Tokens

Counting is cheap, and it is **not** done by a model. `Tokenizer.count(text)` returns a number, so
it cannot carry an instruction anywhere. By default counts are an estimate (about one per short
word, label "estimate"). Set `mail.tokenizer_file` to a local Hugging Face `tokenizer.json`, such as
the one shipped with your Qwen model, for exact counts through the `tokenizers` package. Nothing is
ever downloaded. `CleanedText` carries `tokens_in` and `tokens_out`.

Mail text reaches a model only through `CleanedText.untrusted()`, which returns an `UntrustedText`.

## Measured on the synthetic corpus

The test suite parses and cleans 52 hand-written fixtures and prints this table on every run.
These are synthetic messages, so read them as the shape of the saving and not a promise:

| Class | Characters kept | Tokens kept |
| --- | --- | --- |
| Reply chains | 26% | 22% |
| Legal disclaimers | 37% | 39% |
| Mailing lists | 54% | 42% |
| HTML (marketing, tables) | 68% | 67% |
| Plain (signatures, mobile) | 78% | 77% |
| Forwards | 99% | 99% |
| Everything else (charsets, calendar, bounces, threads, attachments) | 100% | 100% |

Cleaning one message takes well under a millisecond here, so tokenizing and cleaning do not bog the
machine down; the time is in the model.

## Settings

```toml
[mail]
tokenizer_file = "~/models/qwen3/tokenizer.json"   # optional; unset means an estimate
max_text_chars = 2000000                           # a longer body is cut (raw.db keeps it all)

[mail.threading]
strategy = "references+subject"        # or "references"
subject_prefixes = ["re", "fwd", "fw", "aw", "wg", "sv", "vs", "antw", "tr", "rv", "enc", "res"]
strip_list_tags = true
fallback_includes_participants = true

[mail.cleaning]
disabled_rules = []                    # e.g. ["signature.closing"]
extra_attribution_patterns = []        # regular expressions, one line each
extra_footer_patterns = []
extra_disclaimer_patterns = []
extra_boilerplate_patterns = []
extra_closing_patterns = []
signature_max_lines = 8
plain_min_ratio = 0.5

[mail.attachments]
extra_quarantine_extensions = []
extra_quarantine_types = []
exempt_extensions = []
```

Patterns are checked against one line at a time and only its first 500 characters, so a hostile
message cannot make a pattern slow. A pattern that does not compile is a configuration error when
settings load.
