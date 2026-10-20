# SPDX-License-Identifier: Apache-2.0
"""Mail parsing and cleaning: bytes to structure, cleaned text and a thread key. Pure and
deterministic: no database, no network, no model."""

from oaken.mail.attachments import AttachmentExtractor, AttachmentInfo, is_quarantined
from oaken.mail.clean import CleanedText, Segment, clean, clean_text, select_body
from oaken.mail.html import html_to_text
from oaken.mail.parse import Address, ParsedMessage, parse, read_attachment
from oaken.mail.threading import ThreadInfo, normalize_subject, thread_key
from oaken.mail.tokens import EstimateTokenizer, FileTokenizer, Tokenizer, load_tokenizer

__all__ = [
    "Address",
    "AttachmentExtractor",
    "AttachmentInfo",
    "CleanedText",
    "EstimateTokenizer",
    "FileTokenizer",
    "ParsedMessage",
    "Segment",
    "ThreadInfo",
    "Tokenizer",
    "clean",
    "clean_text",
    "html_to_text",
    "is_quarantined",
    "load_tokenizer",
    "normalize_subject",
    "parse",
    "read_attachment",
    "select_body",
    "thread_key",
]
