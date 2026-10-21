+++
title = "Sieve filtering"
subtitle = "Rules that organize mail as it lands, in the language mail people already know"
lead = "Your Sieve scripts run on every message once it is stored, and decide a disposition, labels and flags. Those are saved in raw.db as attributes, so a UI can show a sane mailbox. Nothing is ever sent, deleted or opened."
badges = [["text-bg-warning", "Pre-alpha"]]
group = "concepts"
order = 29
description = "Sieve (RFC 5228) in Oaken: where scripts live, what is supported and refused, how actions map to labels, flags and a disposition, what is stored in raw.db, and the limits that keep hostile mail harmless."
+++

## What it is for

A mailbox needs structure: this is a newsletter, that is from the list, this one can be hidden.
[Sieve](https://www.rfc-editor.org/rfc/rfc5228) is the standard language for exactly that, so rules
are portable, readable and already known to people who run mail servers. Oaken has its own small
Sieve engine (`oaken.mail.sieve`), pure like the parser: no database, no network, no model.

```text
raw.db message ──parse──▶ ParsedMessage ──sieve scripts──▶ disposition + labels + flags
                                                              │
                                                              └─▶ message_attrs in raw.db
```

Filtering runs **after** the message is stored and before it is indexed. Because the raw message is
kept, changing a script re-derives the attributes with no new download.

## Scripts

Scripts are `*.sieve` files in the `sieve` folder next to `config.toml` (`mail.sieve.dir` changes
it). They run **in name order** (`10-lists.sieve`, `20-work.sieve`), and `stop` ends them all.

```sieve
require ["fileinto", "imap4flags", "vnd.oaken.analysis"];

# Mailing lists go in a folder and are marked read.
if exists "list-id" {
    fileinto "Lists";
    addflag "\\Seen";
}

# Newsletters never go to a model.
if header :contains "list-unsubscribe" "" {
    skip_enrich;
}

# Mail from people I work with stays in the inbox.
if address :domain :is "from" "example.com" { keep; }
```

A script with an error is **reported and skipped**, so one typo does not switch off the rest. A
script that fails while running (a bad regular expression built from a variable, a limit reached)
loses its own actions only, and the message stays in the inbox.

### Trying a script

You edit the files; nothing writes them for you. `oaken sieve check FILE` compiles one and names the
line of the first error. `oaken sieve test FILE --message ID` (or `--eml PATH`) runs it on one
message and shows each effect with the `script:line` that caused it, storing nothing.
`oaken sieve list` shows what core would load and any script it would skip, and why.
`oaken sieve refilter` (the `filter` stage of `reprocess`) re-reads the files and re-runs them over
stored mail.

## What an action does

Oaken reads and organizes mail. It does not deliver it, so Sieve actions map onto a mailbox view:

| Action | In Oaken |
| --- | --- |
| `keep`, or nothing decided | disposition `inbox` |
| `fileinto "X"` | **folder** (location) `X`, disposition `filed` (the implicit keep is cancelled). `fileinto "INBOX"` is a keep. A message lives in one folder: the first `fileinto` wins. |
| `fileinto :copy "X"` | folder `X` and disposition `inbox` |
| `discard` | disposition `discarded`: **hidden from default views, never deleted** |
| `addflag`/`setflag`/`removeflag` of `\Seen` or `\Flagged` | flags on the message (read / starred) |
| `addflag "keyword"` (a non-`\` keyword) | **label** `keyword`: a search tag |
| `redirect`, `reject`, `ereject`, `vacation` | a **suppressed intent**, recorded and shown, **never executed** |
| `skip_enrich` (`vnd.oaken.analysis`) | the message is never sent to a model |
| `stop` | ends all scripts |

The disposition is one of three values. Another action beats `discard`, and an explicit `keep` beats
everything. A message is **one object**, so unlike a delivery agent, flags are the flags in effect when
the scripts finish (a discarded message carries none).

## Supported and refused

Everything in RFC 5228 (`header`, `address`, `envelope`, `exists`, `size`, `allof`, `anyof`, `not`,
`if`/`elsif`/`else`, `stop`, `keep`, `discard`, `fileinto`, `redirect`) plus these extensions:
`fileinto`, `copy`, `imap4flags`, `envelope`, `body` (`:text`), `relational`, `regex`, `variables`
(including match variables), `date`, `subaddress`, `reject`, `ereject`, `vacation`, the comparators
`i;ascii-casemap` (default), `i;octet` and `i;ascii-numeric`, and `vnd.oaken.analysis`.

A `require` of anything below is an error with the reason:

| Extension | Why not |
| --- | --- |
| `editheader` | The raw message is the source of truth and is never rewritten. |
| `include` | Scripts already run in name order; use several files. |
| `notify` | Oaken sends nothing. |
| `mime`, `foreverypart`, `extracttext`, `body :raw`/`:content` | Attachments and MIME parts are never opened. |

`body` looks at the message text only: the text parts, or the HTML turned into text when there is
no plain part. An attachment's contents are never visible to a script.

The SMTP **envelope** is not available from a provider, so `envelope` is derived from `Return-Path`
(sender) and `X-Original-To` or `Delivered-To` (recipient), falling back to `From` and the first `To`.
The `date` tests use UTC unless you pass `:zone`.

## What is stored

Attributes live in `raw.db` (`message_attrs`), because organization is what the user sees and a
backup of `raw.db` should keep it.

| Column | Meaning |
| --- | --- |
| `kind` | `disposition`, `location`, `label`, `flag`, `intent`, `analysis` or `enrichment` |
| `value` | `filed`, `Work` (a folder), `Lists` (a label), `\Seen`, `redirect x@example.net`, `skip_enrich` |
| `source` | `sieve` (a person's own attributes can use another source later) |
| `rule` | `10-lists.sieve:4`, the file and line, so a UI can say **why** |

A **location** is the folder a filed message is organized into (single-valued per source, like the
disposition); a **label** is a search tag (many per message). They are distinguished by `kind`, so a
UI can show folders apart from tags (see [ADR 0008](../adr/index.html)). The `enrichment` kind is a
namespaced escape hatch (`<producer>:<key>=<data>`) for enrichers and plugins to attach their own
data without a schema change.

A re-run replaces only the `sieve` rows of that message, in one transaction. `message_filters`
records the hash of the script set that last ran on each message. Edit a script and the hash changes,
every message becomes stale, and `FilterEngine.refilter()` walks them oldest first, resumably. A
`MessageFiltered` event is published for each message.

## Limits

Mail is hostile input, and a script is code, so both are bounded:

- A **step limit** (`max_steps`) counts every command, test and key comparison, and an **action limit**
  (`max_actions`) caps what one script can produce.
- A `:matches` pattern is matched by dynamic programming, so it cannot backtrack, and the text is cut
  to `max_match_chars`. A `:regex` is checked when the script is compiled and refused if it repeats a
  repetition (`(a+)+`), and the text is cut to `max_regex_chars`. The check is best effort.
- Scripts are size-limited, nesting is limited, and numbers are bounded.
- The package imports no network, process or mail-sending code, and a test checks that.

## Settings

```toml
[mail.sieve]
enabled = true
dir = "sieve"                 # relative to the config folder, or an absolute path
max_script_bytes = 262144
max_steps = 100000
max_actions = 200
max_match_chars = 10000
max_regex_chars = 4096
max_variable_chars = 4096
```
