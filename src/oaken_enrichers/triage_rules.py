# SPDX-License-Identifier: Apache-2.0
"""The default deterministic triage rules: what headers and the message itself can settle with no
model. One place for every such check, in order; the first to match wins and triage rates the
message priority 0 with that rule's category. Mail none of them settles goes to the model.

Add a rule by writing one function from a message to ``(category, rule name)`` or None, and listing
it in ``RULES``. (CORE_51 turns this set into a standalone triager plugin.)
"""

import re
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from oaken.core.untrusted import UntrustedText
from oaken.plugins.contracts import EnrichInput
from oaken_enrichers._common import header_values

Category = Literal[
    "personal", "work", "transactional", "notification", "newsletter", "automated", "other"
]
Verdict = tuple[Category, str]  # the category, and the rule's name

_NOREPLY = re.compile(r"(?<![a-z0-9])(?:no[._-]?reply|do[._-]?not[._-]?reply)@", re.I)
_CRON_SUBJECT = re.compile(r"^Cron <[^>]*@[^>]*>", re.I)
_CRON_SENDER = re.compile(r"\bcron daemon\b", re.I)


class RulesConfig(BaseModel):
    """``[plugins.triager.rules]``: which rules run, and the values they match on."""

    skip_rules: list[str] = Field(
        default_factory=list,
        description="Rules to switch off, by name (see the list of rules).",
    )
    bulk_precedence: list[str] = Field(
        default_factory=lambda: ["bulk", "junk"],
        description="Precedence header values that mark bulk mail. 'list' is not one: people "
        "reply through lists.",
    )
    autoreply_headers: list[str] = Field(
        default_factory=lambda: ["x-autoreply", "x-autorespond"],
        description="Headers whose presence marks an automatic reply (lower case).",
    )
    spam_headers: list[str] = Field(
        default_factory=lambda: ["x-spam-status", "x-spam"],
        description="Headers whose value starting with 'yes' is the spam filter's verdict "
        "(lower case). X-Spam-Flag is always read.",
    )
    empty_max_subject_words: int = Field(
        default=3,
        ge=0,
        le=20,
        description="A message with no body and no attachment is junk when its subject has this "
        "many words or fewer.",
    )

    @field_validator("skip_rules")
    @classmethod
    def _known(cls, names: list[str]) -> list[str]:
        unknown = [n for n in names if n not in RULE_NAMES]
        if unknown:
            raise ValueError(f"no such rule: {', '.join(unknown)} (rules: {', '.join(RULE_NAMES)})")
        return names

    @field_validator("bulk_precedence", "autoreply_headers", "spam_headers")
    @classmethod
    def _lower(cls, values: list[str]) -> list[str]:
        return [v.strip().lower() for v in values if v.strip()]


def _first(msg: EnrichInput, name: str) -> str:
    values = header_values(msg).get(name)
    return values[0].strip() if values else ""


def _sender(msg: EnrichInput) -> str:
    return msg.headers.get("from", UntrustedText("")).unwrap()


def auto_submitted(msg: EnrichInput, cfg: RulesConfig) -> Verdict | None:
    if (_first(msg, "auto-submitted") or "no").lower() != "no":
        return "automated", "auto-submitted"
    return None


def cron(msg: EnrichInput, cfg: RulesConfig) -> Verdict | None:
    """Cron output: its body is a template and the job's own log has the content."""
    if (
        "x-cron-env" in header_values(msg)
        or _CRON_SUBJECT.match(msg.subject.unwrap())
        or _CRON_SENDER.search(_sender(msg))
    ):
        return "automated", "cron"
    return None


def auto_reply(msg: EnrichInput, cfg: RulesConfig) -> Verdict | None:
    headers = header_values(msg)
    return ("automated", "auto-reply") if any(h in headers for h in cfg.autoreply_headers) else None


def spam(msg: EnrichInput, cfg: RulesConfig) -> Verdict | None:
    """The spam filter's own verdict."""
    flag = _first(msg, "x-spam-flag").lower()
    if flag in {"yes", "true"} or any(
        _first(msg, h).lower().startswith("yes") for h in cfg.spam_headers
    ):
        return "other", "spam-flag"
    return None


def bulk(msg: EnrichInput, cfg: RulesConfig) -> Verdict | None:
    if _first(msg, "precedence").lower() in cfg.bulk_precedence:
        return "newsletter", "precedence"
    return None


def no_reply(msg: EnrichInput, cfg: RulesConfig) -> Verdict | None:
    return ("notification", "no-reply-sender") if _NOREPLY.search(_sender(msg)) else None


def empty(msg: EnrichInput, cfg: RulesConfig) -> Verdict | None:
    """No body, no attachment and a short subject."""
    if (
        not msg.body.unwrap().strip()
        and not msg.attachments
        and len(msg.subject.unwrap().split()) <= cfg.empty_max_subject_words
    ):
        return "other", "empty-message"
    return None


Rule = Callable[[EnrichInput, RulesConfig], Verdict | None]
RULES: tuple[Rule, ...] = (auto_submitted, bulk, no_reply, cron, auto_reply, spam, empty)
RULE_NAMES = tuple(r.__name__ for r in RULES)

# What each rule does, in a line, for the settings window.
RULE_SUMMARIES: dict[str, str] = {
    "auto_submitted": "An Auto-Submitted header other than 'no' (automated mail).",
    "bulk": "A Precedence header in bulk_precedence (bulk or junk).",
    "no_reply": "A no-reply or do-not-reply sender.",
    "cron": "Cron output: X-Cron-Env, a 'Cron <user@host>' subject or a Cron Daemon sender.",
    "auto_reply": "An auto-reply header from autoreply_headers.",
    "spam": "The spam filter's verdict: X-Spam-Flag, or a spam_headers value starting 'yes'.",
    "empty": "No body, no attachment and a subject of empty_max_subject_words or fewer.",
}


def describe(cfg: RulesConfig) -> list[dict[str, Any]]:
    """The rules in the order they run, each with its one-line meaning and whether it is on."""
    return [
        {
            "name": r.__name__,
            "summary": RULE_SUMMARIES[r.__name__],
            "enabled": r.__name__ not in cfg.skip_rules,
        }
        for r in RULES
    ]


def settled(msg: EnrichInput, cfg: RulesConfig | None = None) -> Verdict | None:
    """The category and rule when the first matching rule settles triage with no model."""
    cfg = cfg or RulesConfig()
    for rule in RULES:
        if rule.__name__ in cfg.skip_rules:
            continue
        if (found := rule(msg, cfg)) is not None:
            return found
    return None
