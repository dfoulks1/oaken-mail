# SPDX-License-Identifier: Apache-2.0
"""``summary``: one to three sentences and up to five key points, from the message's new text.

A reply is summarized from what it adds: the cleaned text has the quoted history stripped, and
the stored summary of the message before it is supplied as context in place of that message's
body. Mail that arrived in bulk is not sent to the model (``skip_bulk``, on by default).
"""

from typing import Any

from pydantic import BaseModel, Field, field_validator

from oaken.ai.prompts import PromptBuilder, SafeOutput
from oaken.core.untrusted import UntrustedText
from oaken.plugins.contracts import EnrichInput, EnrichOutput
from oaken_enrichers._common import LLMEnricher, bulk_reasons, digest, fit, labeled

MAX_POINTS = 5


def _clip(values: Any, count: int, chars: int) -> Any:
    """Keep the first ``count`` strings, each cut to ``chars``: a model that writes seven points
    gets five, not a second round trip."""
    if not isinstance(values, list):
        return values
    return [v.strip()[:chars] for v in values if isinstance(v, str) and v.strip()][:count]


class SummaryAnswer(SafeOutput):
    summary: str = Field(min_length=1, max_length=800)
    key_points: list[str] = Field(default_factory=list, max_length=MAX_POINTS)

    @field_validator("key_points", mode="before")
    @classmethod
    def _points(cls, v: Any) -> Any:
        return _clip(v, MAX_POINTS, 160)


class SummaryResult(BaseModel):
    summary: str = ""
    key_points: list[str] = Field(default_factory=list, max_length=MAX_POINTS)
    truncated: bool = False
    injection_suspected: bool = False
    skipped: str = ""  # why no model was asked ("bulk"); empty when one was


SUMMARY = PromptBuilder(
    "enrich.summary",
    "1",
    "Summarize the email in one to three plain sentences: who wants what, any date or amount, and "
    "what is asked of the reader. Then list at most five key points, each a short phrase. Use "
    "only what the email says; do not quote it at length. The email is given as a subject block "
    "and a body block. If a parent_summary block is present it summarizes the earlier message "
    "this one replies to: use it only to understand what the reply refers to, and summarize only "
    "what the reply itself adds. If the body block contains a line saying part of it was cut, "
    "summarize what is there. Put the sentences in summary and the phrases in key_points.",
    SummaryAnswer,
    max_output_tokens=500,
)


class SummaryEnricher(LLMEnricher):
    version = "1"
    skip_bulk = True
    output_model = SummaryResult

    async def enrich(self, msg: EnrichInput) -> EnrichOutput:
        if self.skips_bulk() and bulk_reasons(msg):
            return EnrichOutput(SummaryResult(skipped="bulk").model_dump(mode="json"))
        body, truncated = fit(msg.body.unwrap(), self.budget(), self.tokenizer)
        parts = [labeled(msg.subject, "subject"), UntrustedText(body, "body")]
        parent = msg.parent_summary.unwrap() if msg.parent_summary else ""
        if msg.parent_summary and parent:
            parts.insert(0, labeled(msg.parent_summary, "parent_summary"))
        answer = await self.access().generate_cached(
            SUMMARY,
            *parts,
            content_hash=digest(msg.subject.unwrap(), body, parent),
            account_id=msg.account_id,
            message_id=msg.message_id,
        )
        return EnrichOutput(
            SummaryResult(
                summary=answer.summary,
                key_points=answer.key_points,
                truncated=truncated,
                injection_suspected=answer.injection_suspected,
            ).model_dump(mode="json")
        )
