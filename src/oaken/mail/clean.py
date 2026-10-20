# SPDX-License-Identifier: Apache-2.0
"""The cleaner: the first token-saver. It splits a message body into tagged segments and keeps
only what is new.

Nothing is discarded silently. Every line lands in exactly one segment tagged ``new``, ``quoted``,
``signature``, ``footer``, ``disclaimer`` or ``boilerplate``; ``cleaned`` is only the ``new``
segments, and the result reports ``chars_in`` and ``chars_out`` (and token counts). Each rule has
a name, so it can be switched off in ``[mail.cleaning] disabled_rules``; the rules and their
rationale are documented in ``docs/architecture/parsing.md``.

Patterns match one line at a time, and only the first 500 characters of it, so a hostile message
cannot make a pattern slow.
"""

import re
from dataclasses import dataclass
from typing import Literal

from oaken.core.settings import CleaningSettings, MailSettings
from oaken.core.untrusted import UntrustedText
from oaken.mail.html import html_to_text
from oaken.mail.parse import ParsedMessage
from oaken.mail.tokens import EstimateTokenizer, Tokenizer

Kind = Literal["new", "quoted", "signature", "footer", "disclaimer", "boilerplate"]
Source = Literal["plain", "html", "none"]

_LINE_CAP = 500
_SIGNATURE_DELIMITER_MAX = 25

# Characters that carry no meaning in mail and are used to hide or reorder text: zero-width space,
# directional marks and overrides, word joiner, byte-order mark, soft hyphen, combining grapheme
# joiner, Mongolian vowel separator. ZWNJ and ZWJ stay: scripts and emoji need them.
_INVISIBLE = re.compile("[​‎‏‪-‮⁠⁦-⁩﻿­͏᠎]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_I = re.IGNORECASE
DEFAULT_ATTRIBUTION = (
    r"^on\s.{5,300}\bwrote:\s*$",
    r"^(?:am|le|el|il|em|op|den)\s.{5,300}\b(?:schrieb|a écrit|escribió|ha scritto|escreveu"
    r"|schreef|skrev)\b.*:\s*$",
)
_ORIGINAL_MARKER = re.compile(
    r"^\s*-{2,}\s*(?:original message|original-nachricht|message d.origine|mensaje original"
    r"|messaggio originale)\s*-{2,}\s*$",
    _I,
)
_FORWARD_MARKER = re.compile(
    r"^\s*(?:-{2,}\s*forwarded message\s*-{2,}|begin forwarded message:?)\s*$", _I
)
_HEADER_LABEL = re.compile(
    r"^\W{0,2}(from|von|de|sent|date|gesendet|envoyé|to|an|à|cc|subject|betreff|objet)\W{0,2}:\s",
    _I,
)
_HEADER_START = re.compile(r"^\W{0,2}(from|von|de):", _I)
DEFAULT_FOOTER = (
    r"\bunsubscribe\b",
    r"\byou(?:'re| are) receiving this\b",
    r"\b(?:manage|update|change) (?:your )?(?:e-?mail )?(?:preferences|subscription)",
    r"\bview (?:this (?:e-?mail|message) )?(?:in|on) (?:your |a )?(?:web )?browser",
    r"\bthis (?:e-?mail|message) was sent to\b",
    r"\bno longer wish to receive\b",
    r"\bmailman/listinfo\b",
    r"^\S+ mailing list$",
    r"^(?:©|\(c\)|copyright)\s",
    r"\ball rights reserved\b",
    r"\bour mailing address is\b",
)
DEFAULT_DISCLAIMER = (
    r"\b(?:this|the) (?:e-?mail|message|communication)(?: and any (?:files|attachments)[^.]*)?"
    r" (?:is|are|may be|may contain|contains?) (?:confidential|privileged|intended)",
    r"\bintended (?:solely |only )?for the (?:use of the )?"
    r"(?:individual|addressee|recipient|named)",
    r"\bif you (?:are not the intended|have received this (?:e-?mail|message|communication)"
    r" in error|received this in error)",
    r"\bconfidentiality notice\b|^disclaimer\b|\blegal disclaimer\b",
    r"\bplease (?:consider|think of) the (?:environment|planet)\b|\bbefore printing\b",
    r"\bunauthori[sz]ed (?:use|disclosure|review|distribution)\b",
    r"\bprivileged (?:and|or) confidential\b",
)
DEFAULT_BOILERPLATE = (
    r"^sent from my (?:iphone|ipad|android|phone|mobile|blackberry|samsung|galaxy)\b.*$",
    r"^get outlook for (?:ios|android)\b.*$",
    r"^sent (?:from|via) (?:mail|yahoo mail|proton ?mail|outlook)\b.*$",
    r"^\[(?:image|cid):[^\]]*\]$",
)
DEFAULT_CLOSING = (
    r"^(?:(?:best|kind|warm|many|with|my)\s+)?(?:regards|wishes|thanks|thank you|cheers"
    r"|sincerely|cordially|respectfully|yours(?:\s+(?:truly|sincerely|faithfully))?|best|ciao"
    r"|mfg|vg|cordialement|saludos|grazie)(?:\s+(?:regards|wishes))?[\s,.!-]*$",
)
_SEPARATOR = re.compile(r"^\s*[-_=*~]{3,}\s*$")
_SIG_DELIMITER = re.compile(r"^--\s?$")
_QUOTE_PREFIX = re.compile(r"^\s{0,3}>")
_LIST_PRECEDENCE = {"bulk", "list", "junk"}


@dataclass(frozen=True, slots=True)
class Segment:
    kind: Kind
    text: str
    rule: str | None  # the rule that tagged it; None for new text


@dataclass(frozen=True, slots=True)
class CleanedText:
    segments: tuple[Segment, ...]
    cleaned: str  # only the ``new`` segments
    source: Source  # which body it was made from
    chars_in: int
    chars_out: int
    tokens_in: int
    tokens_out: int
    tokenizer: str  # name of the tokenizer; "estimate" when counts are estimates
    warnings: tuple[str, ...]

    @property
    def ratio(self) -> float:
        """``chars_out / chars_in`` (1.0 for an empty message)."""
        return self.chars_out / self.chars_in if self.chars_in else 1.0

    def untrusted(self) -> UntrustedText:
        """The cleaned text, wrapped. This is the door to anything that talks to a model."""
        return UntrustedText(self.cleaned)


def normalize(text: str) -> str:
    """Line endings, invisible and control characters, repeated spaces, runs of blank lines."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    text = _INVISIBLE.sub("", text)
    text = _CONTROL.sub("", text)
    lines = []
    for line in text.split("\n"):
        stripped = line.rstrip()
        indent = len(stripped) - len(stripped.lstrip(" \t"))
        lines.append(stripped[:indent] + re.sub(r"[ \t]{2,}", " ", stripped[indent:]))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n")


def select_body(parsed: ParsedMessage, settings: CleaningSettings) -> tuple[str, Source]:
    """text/plain when it is a faithful alternative to the HTML, otherwise the HTML as text."""
    plain = "\n\n".join(p.strip() for p in parsed.text_parts if p.strip())
    html = "\n\n".join(t for t in (html_to_text(h) for h in parsed.html_parts) if t)
    if plain and not html:
        return plain, "plain"
    if html and not plain:
        return html, "html"
    if not plain:
        return "", "none"
    if len("".join(plain.split())) >= settings.plain_min_ratio * len("".join(html.split())):
        return plain, "plain"
    return html, "html"


def clean(
    parsed: ParsedMessage,
    settings: MailSettings | None = None,
    tokenizer: Tokenizer | None = None,
) -> CleanedText:
    cfg = settings or MailSettings()
    tok = tokenizer or EstimateTokenizer()
    body, source = select_body(parsed, cfg.cleaning)
    segments = clean_text(body, cfg.cleaning, bulk=_is_bulk(parsed))
    cleaned = "\n\n".join(s.text for s in segments if s.kind == "new")
    return CleanedText(
        segments=segments,
        cleaned=cleaned,
        source=source,
        chars_in=len(body),
        chars_out=len(cleaned),
        tokens_in=tok.count(body),
        tokens_out=tok.count(cleaned),
        tokenizer=tok.name,
        warnings=parsed.warnings,
    )


def _is_bulk(parsed: ParsedMessage) -> bool:
    precedence = (parsed.header("precedence") or "").lower()
    return bool(
        parsed.list_id
        or parsed.header("list-unsubscribe")
        or precedence in _LIST_PRECEDENCE
        or (parsed.header("auto-submitted") or "no").lower() != "no"
    )


def _compile(patterns: tuple[str, ...], extra: list[str]) -> list[re.Pattern[str]]:
    return [re.compile(p, _I) for p in (*patterns, *extra)]


def _hit(patterns: list[re.Pattern[str]], line: str) -> bool:
    head = line.strip()[:_LINE_CAP]
    return any(p.search(head) for p in patterns)


class _Lines:
    """The lines of a body and the tag each one has received so far."""

    def __init__(self, text: str) -> None:
        self.text = text.split("\n") if text else []
        self.tag: list[tuple[Kind, str] | None] = [None] * len(self.text)

    def free(self, i: int) -> bool:
        return self.tag[i] is None

    def blank(self, i: int) -> bool:
        return not self.text[i].strip()

    def mark(self, i: int, kind: Kind, rule: str) -> None:
        if self.tag[i] is None and not self.blank(i):
            self.tag[i] = (kind, rule)

    def mark_rest(self, start: int, kind: Kind, rule: str) -> None:
        for i in range(start, len(self.text)):
            self.mark(i, kind, rule)


def clean_text(text: str, settings: CleaningSettings, *, bulk: bool = False) -> tuple[Segment, ...]:
    """Split ``text`` into segments. Pure and deterministic."""
    off = set(settings.disabled_rules)
    lines = _Lines(normalize(text))
    _quotes(lines, settings, off)
    _signature_delimiter(lines, off)
    _footers(lines, settings, off, bulk)
    _disclaimers(lines, settings, off)
    _boilerplate(lines, settings, off)
    _closing(lines, settings, off)
    return _group(lines)


# --- rules -------------------------------------------------------------------------------


def _quotes(lines: _Lines, settings: CleaningSettings, off: set[str]) -> None:
    attribution = _compile(DEFAULT_ATTRIBUTION, settings.extra_attribution_patterns)
    n = len(lines.text)
    i = 0
    while i < n:
        line = lines.text[i]
        if "quote.original_marker" not in off and _ORIGINAL_MARKER.match(line):
            lines.mark_rest(i, "quoted", "quote.original_marker")
            return
        if "quote.header_block" not in off and _outlook_block(lines, i):
            lines.mark_rest(i, "quoted", "quote.header_block")
            return
        if "quote.attribution" not in off:
            span = _attribution_span(lines, i, attribution)
            if span:
                for j in range(i, i + span):
                    lines.mark(j, "quoted", "quote.attribution")
                k = i + span
                while k < n and lines.blank(k):
                    k += 1
                if k < n and not _QUOTE_PREFIX.match(lines.text[k]):
                    lines.mark_rest(k, "quoted", "quote.attribution")  # the quote lost its marks
                    return
                i += span
                continue
        if "quote.prefix" not in off and _QUOTE_PREFIX.match(line):
            lines.mark(i, "quoted", "quote.prefix")
        i += 1


def _attribution_span(lines: _Lines, i: int, patterns: list[re.Pattern[str]]) -> int:
    """How many lines the "On ... wrote:" line occupies at ``i`` (it may wrap), or 0."""
    for span in (1, 2, 3):
        chunk = lines.text[i : i + span]
        if len(chunk) < span or any(not c.strip() for c in chunk):
            return 0
        joined = " ".join(c.strip() for c in chunk)
        if _hit(patterns, joined) and ("@" in joined or any(c.isdigit() for c in joined)):
            return span
    return 0


def _outlook_block(lines: _Lines, i: int) -> bool:
    """``From:`` followed by at least two more header-like lines, and not a forwarded message."""
    if not _HEADER_START.match(lines.text[i].strip()[:_LINE_CAP]):
        return False
    window = lines.text[i + 1 : i + 7]
    labels = sum(1 for w in window if _HEADER_LABEL.match(w.strip()[:_LINE_CAP] + " "))
    if labels < 2:
        return False
    before = lines.text[max(0, i - 3) : i]
    return not any(_FORWARD_MARKER.match(b) for b in before)


def _signature_delimiter(lines: _Lines, off: set[str]) -> None:
    if "signature.delimiter" in off:
        return
    n = len(lines.text)
    for i in range(n):
        if lines.free(i) and _SIG_DELIMITER.match(lines.text[i]):
            end = min(n, i + 1 + _SIGNATURE_DELIMITER_MAX)
            for j in range(i, end):
                tag = lines.tag[j]
                if tag is not None and tag[0] == "quoted":
                    break
                lines.mark(j, "signature", "signature.delimiter")
            return


def _footers(lines: _Lines, settings: CleaningSettings, off: set[str], bulk: bool) -> None:
    if "footer.list" in off:
        return
    patterns = _compile(DEFAULT_FOOTER, settings.extra_footer_patterns)
    free = [i for i in range(len(lines.text)) if lines.free(i) and not lines.blank(i)]
    window = free[-15:]
    hits = [i for i in window if len(lines.text[i]) <= 300 and _hit(patterns, lines.text[i])]
    if len(hits) < (1 if bulk else 2):
        return
    start = hits[0]
    k = start - 1
    while k >= 0 and lines.blank(k):
        k -= 1
    if k >= 0 and lines.free(k) and _SEPARATOR.match(lines.text[k]):
        start = k
    lines.mark_rest(start, "footer", "footer.list")


def _disclaimers(lines: _Lines, settings: CleaningSettings, off: set[str]) -> None:
    if "disclaimer.legal" in off:
        return
    patterns = _compile(DEFAULT_DISCLAIMER, settings.extra_disclaimer_patterns)
    paragraphs: list[list[int]] = []
    for i in range(len(lines.text)):
        if lines.free(i) and not lines.blank(i):
            if paragraphs and i - paragraphs[-1][-1] == 1:
                paragraphs[-1].append(i)
            else:
                paragraphs.append([i])
    if len(paragraphs) < 2:
        return  # a message that is only a disclaimer is the message
    window = paragraphs[-4:]
    for pos, para in enumerate(window):
        if any(_hit(patterns, lines.text[i]) for i in para):
            for later in window[pos:]:
                for i in later:
                    lines.mark(i, "disclaimer", "disclaimer.legal")
            return


def _boilerplate(lines: _Lines, settings: CleaningSettings, off: set[str]) -> None:
    if "boilerplate.stock" in off:
        return
    patterns = _compile(DEFAULT_BOILERPLATE, settings.extra_boilerplate_patterns)
    for i in range(len(lines.text)):
        if lines.free(i) and _hit(patterns, lines.text[i]):
            lines.mark(i, "boilerplate", "boilerplate.stock")


def _closing(lines: _Lines, settings: CleaningSettings, off: set[str]) -> None:
    """ "Thanks," followed by a short block at the end of the new text: the block is a signature."""
    if "signature.closing" in off:
        return
    closings = _compile(DEFAULT_CLOSING, settings.extra_closing_patterns)
    n = len(lines.text)
    free = [i for i in range(n) if lines.free(i) and not lines.blank(i)]
    for c in reversed(free):
        if not _hit(closings, lines.text[c]):
            continue
        block = []
        for j in range(c + 1, n):
            tag = lines.tag[j]
            if tag is not None and tag[0] == "quoted":
                break
            if lines.free(j) and not lines.blank(j):
                block.append(j)
        if 1 <= len(block) <= settings.signature_max_lines and all(
            len(lines.text[j]) <= 100 for j in block
        ):
            for j in block:
                lines.mark(j, "signature", "signature.closing")
        return


def _group(lines: _Lines) -> tuple[Segment, ...]:
    """Contiguous lines with the same tag become one segment; blank lines go with the segment
    they follow, unless the next line continues the same kind."""
    segments: list[tuple[Kind, str | None, list[str]]] = []
    pending: list[str] = []
    for text, tag in zip(lines.text, lines.tag, strict=True):
        if not text.strip():
            pending.append(text)
            continue
        entry: tuple[Kind, str | None] = tag if tag is not None else ("new", None)
        kind, rule = entry
        if segments and segments[-1][0] == kind and segments[-1][1] == rule:
            segments[-1][2].extend(pending)
        elif segments:
            segments[-1][2].extend(pending)
            segments.append((kind, rule, []))
        else:
            segments.append((kind, rule, []))
        pending = []
        segments[-1][2].append(text)
    return tuple(Segment(k, "\n".join(ls).strip("\n"), r) for k, r, ls in segments if ls)
