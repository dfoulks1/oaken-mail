# SPDX-License-Identifier: Apache-2.0
"""A stable thread key from reference headers, with a subject-based fallback.

The key is stateless: it is computed from one message alone, so it can be recomputed any time
(the index is rebuildable). Reply chains agree because every reply carries the root's id first in
``References``. Two threads with the same subject but different roots never merge while
``References`` exist. What a single message cannot know (a reply with only ``In-Reply-To`` whose
parent is itself a reply) is left to the index, which can look the parent up; ``subject_key`` is
the hint it uses to attach reference-less replies.

See jwz's threading algorithm (https://www.jwz.org/doc/threading.html) for the reference design.
"""

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

from oaken.core.settings import ThreadingSettings
from oaken.mail.parse import ParsedMessage

Basis = Literal["references", "in-reply-to", "message-id", "subject", "none"]

_TAG = r"\[[^\]\[]{1,40}\]"


@dataclass(frozen=True, slots=True)
class ThreadInfo:
    key: str  # "mid:<root id>", "subj:<hash>" or "hdr:<hash>"
    subject_key: str  # "subj:<hash>" of the normalized subject (and participants)
    root_message_id: str | None
    basis: Basis
    is_reply: bool  # the subject carried a reply or forward prefix


def normalize_subject(subject: str, settings: ThreadingSettings | None = None) -> tuple[str, bool]:
    """``(normalized, had_reply_prefix)``: prefixes and list tags removed, lower-cased."""
    cfg = settings or ThreadingSettings()
    prefixes = "|".join(re.escape(p) for p in cfg.subject_prefixes) or r"(?!x)x"
    prefix = re.compile(rf"^\s*(?:{prefixes})\s*(?:\[\d+\]|\(\d+\))?\s*:\s*", re.IGNORECASE)
    tag = re.compile(rf"^\s*{_TAG}\s*")
    text = " ".join(subject.split())
    had_prefix = False
    while True:
        stripped = prefix.sub("", text, count=1)
        if stripped != text:
            had_prefix = True
        elif cfg.strip_list_tags:
            stripped = tag.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped
    return text.strip().casefold(), had_prefix


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8", "replace")).hexdigest()[:32]


def thread_key(parsed: ParsedMessage, settings: ThreadingSettings | None = None) -> ThreadInfo:
    cfg = settings or ThreadingSettings()
    subject, is_reply = normalize_subject(parsed.subject, cfg)
    people: tuple[str, ...] = ()
    if cfg.fallback_includes_participants:
        people = tuple(sorted({a.address for a in (*parsed.from_, *parsed.to)}))
    subject_key = "subj:" + _hash(subject, *people)

    if parsed.references:
        root, basis = parsed.references[0], "references"
    elif parsed.in_reply_to:
        root, basis = parsed.in_reply_to[0], "in-reply-to"
    elif parsed.message_id:
        root, basis = parsed.message_id, "message-id"
    else:
        root, basis = None, "none"

    use_subject = cfg.strategy == "references+subject" and bool(subject)
    if basis == "message-id" and is_reply and use_subject:
        # A reply that names no parent: fall back to the subject.
        return ThreadInfo(subject_key, subject_key, parsed.message_id, "subject", is_reply)
    if root is not None:
        return ThreadInfo(f"mid:{root}", subject_key, root, basis, is_reply)  # type: ignore[arg-type]
    if use_subject:
        return ThreadInfo(subject_key, subject_key, None, "subject", is_reply)
    sender = parsed.from_[0].address if parsed.from_ else ""
    return ThreadInfo(
        "hdr:" + _hash(sender, parsed.date_raw or "", parsed.subject),
        subject_key,
        None,
        "none",
        is_reply,
    )
