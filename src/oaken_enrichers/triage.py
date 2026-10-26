# SPDX-License-Identifier: Apache-2.0
"""``triage``: category, priority and whether a reply is owed.

Headers decide first. Mail that says it is automated, bulk or from a no-reply address gets its
category from a rule and the model is never asked (``source`` is ``rules``); the model sees only
what the rules cannot settle (``source`` is ``llm``).
"""

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from oaken.ai.prompts import PromptBuilder, SafeOutput
from oaken.core.untrusted import UntrustedText
from oaken.plugins.contracts import EnrichInput, EnrichOutput
from oaken_enrichers._common import LLMEnricher, digest, fit, header_values, labeled

Category = Literal[
    "personal", "work", "transactional", "notification", "newsletter", "automated", "other"
]
_NOREPLY = re.compile(r"(?<![a-z0-9])(?:no[._-]?reply|do[._-]?not[._-]?reply)@", re.I)
_PRECEDENCE = {"bulk", "list", "junk"}
MAX_REASON_WORDS = 20


class TriageAnswer(SafeOutput):
    category: Category
    priority: int = Field(ge=0, le=3)
    needs_reply: bool
    waiting_on_others: bool
    reason: str = Field(default="", max_length=300)

    @field_validator("reason", mode="before")
    @classmethod
    def _short(cls, v: Any) -> Any:
        return " ".join(v.split()[:MAX_REASON_WORDS]) if isinstance(v, str) else v


class TriageResult(BaseModel):
    category: Category
    priority: int = Field(ge=0, le=3)
    needs_reply: bool
    waiting_on_others: bool
    reason: str = ""
    source: Literal["rules", "llm"]
    rule: str = ""  # which rule decided, when source is "rules"
    truncated: bool = False
    injection_suspected: bool = False


TRIAGE = PromptBuilder(
    "enrich.triage",
    "1",
    "Triage the email for its reader. category is one of: personal (a person writing to the "
    "reader about their own life), work, transactional (receipts, orders, bookings, account "
    "statements), notification (a service telling the reader something happened), newsletter, "
    "automated, other. priority is 0 (ignore), 1 (low), 2 (normal) or 3 (needs attention soon). "
    "needs_reply is true only if a person is waiting for an answer from the reader. "
    "waiting_on_others is true if the reader sent this and expects others to act. reason is at "
    "most twenty words and says why, without quoting the email. The email is given as a from "
    "block, a subject block and a body block.",
    TriageAnswer,
    max_output_tokens=200,
)


def rule_for(headers: dict[str, tuple[str, ...]], sender: str) -> tuple[Category, str] | None:
    """The category headers settle on their own, with the rule's name; None when they do not."""

    def first(name: str) -> str:
        values = headers.get(name)
        return values[0].strip() if values else ""

    if (first("auto-submitted") or "no").lower() != "no":
        return "automated", "auto-submitted"
    if first("precedence").lower() in _PRECEDENCE:
        return "newsletter", "precedence"
    if first("list-id") or first("list-unsubscribe"):
        return "newsletter", "list-headers"
    if _NOREPLY.search(sender):
        return "notification", "no-reply-sender"
    return None


class TriageEnricher(LLMEnricher):
    version = "1"
    output_model = TriageResult

    async def enrich(self, msg: EnrichInput) -> EnrichOutput:
        sender = msg.headers.get("from", UntrustedText("")).unwrap()
        found = rule_for(header_values(msg), sender)
        if found is not None:
            category, rule = found
            return EnrichOutput(
                TriageResult(
                    category=category,
                    priority=0,
                    needs_reply=False,
                    waiting_on_others=False,
                    reason=f"Decided by header rule {rule}.",
                    source="rules",
                    rule=rule,
                ).model_dump(mode="json")
            )
        body, truncated = fit(msg.body.unwrap(), self.budget(), self.tokenizer)
        answer = await self.access().generate_cached(
            TRIAGE,
            UntrustedText(sender, "from"),
            labeled(msg.subject, "subject"),
            UntrustedText(body, "body"),
            content_hash=digest(sender, msg.subject.unwrap(), body),
            account_id=msg.account_id,
            message_id=msg.message_id,
            model=self.model_for(),
        )
        return EnrichOutput(
            TriageResult(
                category=answer.category,
                priority=answer.priority,
                needs_reply=answer.needs_reply,
                waiting_on_others=answer.waiting_on_others,
                reason=answer.reason,
                source="llm",
                truncated=truncated,
                injection_suspected=answer.injection_suspected,
            ).model_dump(mode="json")
        )
