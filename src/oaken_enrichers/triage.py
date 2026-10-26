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
from oaken.plugins.contracts import AskIntent, EnrichInput, EnrichOutput
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
    "automated, other. priority is 0, 1, 2 or 3. Use 0 (ignore) for junk, unsolicited or "
    "empty mail and for automated mail that asks nothing of the reader; 1 (low) for mail that "
    "only informs; 2 (normal) for mail from a real person or service about something the "
    "reader is involved in; 3 (needs attention soon) only when someone is waiting on the "
    "reader or a deadline is near. Do not choose 2 by default: when two priorities fit, choose "
    "the lower. needs_reply is true only if a person is waiting for an answer from the reader. "
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


def junk_for(msg: EnrichInput) -> tuple[Category, str] | None:
    """Mail that is junk on its face, settled without a model: the spam filter's own verdict, or a
    message with nothing in it (no body, no attachment, a subject of three words or fewer)."""
    headers = header_values(msg)
    flag = headers.get("x-spam-flag", ("",))[0].strip().lower()
    if flag in {"yes", "true"}:
        return "other", "spam-flag"
    if (
        not msg.body.unwrap().strip()
        and not msg.attachments
        and len(msg.subject.unwrap().split()) <= 3
    ):
        return "other", "empty-message"
    return None


def settled(msg: EnrichInput) -> tuple[Category, str] | None:
    """The category and rule when headers or the message itself settle triage with no model."""
    sender = msg.headers.get("from", UntrustedText("")).unwrap()
    return rule_for(header_values(msg), sender) or junk_for(msg)


class TriageEnricher(LLMEnricher):
    version = "1"
    output_model = TriageResult
    prompt = TRIAGE
    ask_intent = AskIntent(
        "triage",
        "The reader asks what is important, urgent or needs attention or a reply.",
        ("category", "priority", "needs_reply", "reason"),
    )

    def needs_model(self, msg: EnrichInput) -> bool:
        return settled(msg) is None

    async def enrich(self, msg: EnrichInput) -> EnrichOutput:
        sender = msg.headers.get("from", UntrustedText("")).unwrap()
        found = settled(msg)
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
        return self.finish(answer, truncated)

    def from_answer(self, msg: EnrichInput, answer: TriageAnswer) -> EnrichOutput:
        """The stored result for an answer a model (or an external agent) gave: what ``enrich``
        does after the model call, so every answer is held to the same rules."""
        _, truncated = fit(msg.body.unwrap(), self.budget(), self.tokenizer)
        return self.finish(answer, truncated)

    def finish(self, answer: TriageAnswer, truncated: bool) -> EnrichOutput:
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
