# SPDX-License-Identifier: Apache-2.0
"""The default deterministic triage rules: what headers and the message itself can settle with no
model. One place for every such check, in order; the first to match wins and triage rates the
message priority 0 with that rule's category. Mail none of them settles goes to the model.

Add a rule by writing one function from a message to ``(category, rule name)`` or None, and listing
it in ``RULES``. (CORE_50 turns this set into a standalone triager plugin.)
"""

import re
from collections.abc import Callable
from typing import Literal

from oaken.core.untrusted import UntrustedText
from oaken.plugins.contracts import EnrichInput
from oaken_enrichers._common import header_values

Category = Literal[
    "personal", "work", "transactional", "notification", "newsletter", "automated", "other"
]
Verdict = tuple[Category, str]  # the category, and the rule's name

_NOREPLY = re.compile(r"(?<![a-z0-9])(?:no[._-]?reply|do[._-]?not[._-]?reply)@", re.I)
_PRECEDENCE = {"bulk", "junk"}  # not "list": people reply through lists
_CRON_SUBJECT = re.compile(r"^Cron <[^>]*@[^>]*>", re.I)
_CRON_SENDER = re.compile(r"\bcron daemon\b", re.I)
_AUTOREPLY = ("x-autoreply", "x-autorespond")
_SPAM_HEADERS = ("x-spam-status", "x-spam")


def _first(msg: EnrichInput, name: str) -> str:
    values = header_values(msg).get(name)
    return values[0].strip() if values else ""


def _sender(msg: EnrichInput) -> str:
    return msg.headers.get("from", UntrustedText("")).unwrap()


def auto_submitted(msg: EnrichInput) -> Verdict | None:
    if (_first(msg, "auto-submitted") or "no").lower() != "no":
        return "automated", "auto-submitted"
    return None


def cron(msg: EnrichInput) -> Verdict | None:
    """Cron output: its body is a template and the job's own log has the content."""
    if (
        "x-cron-env" in header_values(msg)
        or _CRON_SUBJECT.match(msg.subject.unwrap())
        or _CRON_SENDER.search(_sender(msg))
    ):
        return "automated", "cron"
    return None


def auto_reply(msg: EnrichInput) -> Verdict | None:
    headers = header_values(msg)
    return ("automated", "auto-reply") if any(h in headers for h in _AUTOREPLY) else None


def spam(msg: EnrichInput) -> Verdict | None:
    """The spam filter's own verdict."""
    flag = _first(msg, "x-spam-flag").lower()
    if flag in {"yes", "true"} or any(_first(msg, h).lower().startswith("yes") for h in _SPAM_HEADERS):
        return "other", "spam-flag"
    return None


def bulk(msg: EnrichInput) -> Verdict | None:
    if _first(msg, "precedence").lower() in _PRECEDENCE:
        return "newsletter", "precedence"
    return None


def no_reply(msg: EnrichInput) -> Verdict | None:
    return ("notification", "no-reply-sender") if _NOREPLY.search(_sender(msg)) else None


def empty(msg: EnrichInput) -> Verdict | None:
    """No body, no attachment and a subject of three words or fewer."""
    if (
        not msg.body.unwrap().strip()
        and not msg.attachments
        and len(msg.subject.unwrap().split()) <= 3
    ):
        return "other", "empty-message"
    return None


Rule = Callable[[EnrichInput], Verdict | None]
RULES: tuple[Rule, ...] = (auto_submitted, bulk, no_reply, cron, auto_reply, spam, empty)


def settled(msg: EnrichInput) -> Verdict | None:
    """The category and rule when the first matching rule settles triage with no model."""
    for rule in RULES:
        if (found := rule(msg)) is not None:
            return found
    return None
