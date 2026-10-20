# SPDX-License-Identifier: Apache-2.0
"""Raw bytes to everything the index needs, with no database: parse, clean, thread key.

Pure, like ``oaken.mail``. A forwarded message inside another is cleaned on its own (depth first)
and indexed with its parent.
"""

from dataclasses import dataclass
from datetime import datetime

from oaken.core.settings import MailSettings
from oaken.mail.clean import CleanedText, clean
from oaken.mail.parse import ParsedMessage, parse
from oaken.mail.threading import ThreadInfo, thread_key
from oaken.mail.tokens import Tokenizer, load_tokenizer


@dataclass(frozen=True)
class Analysis:
    parsed: ParsedMessage
    cleaned: CleanedText
    thread: ThreadInfo
    forwarded: tuple[tuple[ParsedMessage, CleanedText], ...]
    fallback_date: datetime | None  # the provider's date, used when the Date header is missing


def analyze(
    raw: bytes,
    settings: MailSettings,
    *,
    tokenizer: Tokenizer | None = None,
    fallback_date: datetime | None = None,
) -> Analysis:
    tok = tokenizer or load_tokenizer(settings)
    parsed = parse(raw, settings)
    cleaned = clean(parsed, settings, tok)
    forwarded: list[tuple[ParsedMessage, CleanedText]] = []

    def walk(items: tuple[ParsedMessage, ...]) -> None:
        for item in items:
            forwarded.append((item, clean(item, settings, tok)))
            walk(item.forwarded)

    walk(parsed.forwarded)
    return Analysis(
        parsed,
        cleaned,
        thread_key(parsed, settings.threading),
        tuple(forwarded),
        fallback_date,
    )
