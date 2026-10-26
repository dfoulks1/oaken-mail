# SPDX-License-Identifier: Apache-2.0
"""``actions``: action items and deadlines, each tied to a quote from the message.

A model reading mail will sometimes invent a task. Every item must carry a quote, and the quote
must be found in the text the model was given (whitespace aside); an item whose quote is not there
is dropped, and the one that is kept stores the text as it stands in the message and where it is.
"""

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

from oaken.ai.prompts import PromptBuilder, SafeOutput
from oaken.core.untrusted import UntrustedText
from oaken.plugins.contracts import EnrichInput, EnrichOutput
from oaken_enrichers._common import GatedConfig, LLMEnricher, bulk_reasons, digest, fit, labeled

MAX_ITEMS = 20
MIN_QUOTE = 4
MAX_QUOTE = 600  # the stored quote's limit; must match ActionAnswer/DeadlineAnswer.quote max_length


class ActionAnswer(BaseModel):
    text: str = Field(min_length=1, max_length=300)
    owner: str | None = Field(default=None, max_length=120)
    due: str | None = Field(default=None, max_length=60)
    quote: str = Field(default="", max_length=600)


class DeadlineAnswer(BaseModel):
    text: str = Field(min_length=1, max_length=300)
    date: str | None = Field(default=None, max_length=60)
    quote: str = Field(default="", max_length=600)


class ActionsAnswer(SafeOutput):
    action_items: list[ActionAnswer] = Field(default_factory=list)
    deadlines: list[DeadlineAnswer] = Field(default_factory=list)

    @field_validator("action_items", "deadlines", mode="before")
    @classmethod
    def _cap(cls, v: Any) -> Any:
        return v[:MAX_ITEMS] if isinstance(v, list) else v


class ActionItem(ActionAnswer):
    start: int  # where the quote sits in the message's cleaned text
    end: int


class Deadline(DeadlineAnswer):
    start: int
    end: int


class ActionsResult(BaseModel):
    action_items: list[ActionItem] = Field(default_factory=list)
    deadlines: list[Deadline] = Field(default_factory=list)
    dropped: int = 0  # items the model proposed whose quote was not in the message
    truncated: bool = False
    injection_suspected: bool = False
    skipped: str = ""


ACTIONS = PromptBuilder(
    "enrich.actions",
    "1",
    "List what the email asks the reader to do (action_items) and any deadline it states "
    "(deadlines). For each action item give text (what to do), owner (who must do it, or null), "
    "due (the date or time only if the email states one explicitly, otherwise null) and quote: "
    "the exact words from the email that ask for it, copied character for character. For each "
    "deadline give text, date (as written) and quote. Never invent an item: if the email asks "
    "for nothing, return empty lists. Every quote must appear verbatim in the body block. The "
    "email is given as a subject block and a body block.",
    ActionsAnswer,
    max_output_tokens=800,
)


def locate(quote: str, text: str) -> tuple[int, int] | None:
    """Where ``quote`` occurs in ``text``, matching runs of whitespace as equal (a model re-flows
    lines). None if it does not occur or is too short to mean anything."""
    words = quote.split()
    if len(quote.strip()) < MIN_QUOTE or not words:
        return None
    found = re.search(r"\s+".join(re.escape(w) for w in words), text)
    return (found.start(), found.end()) if found else None


def verbatim(quote: str, sent: str, full: str) -> tuple[str, int, int] | None:
    """The quote as it stands in the message and where: found in what the model was given, and
    again in the whole cleaned text (a cut can make the two differ). None drops the item.

    Relocating the model's quote onto the full text can land on a longer span than the model
    returned (the model re-flows whitespace; the full text may have much more between the same
    words). A span past ``MAX_QUOTE`` is dropped rather than stored — storing it would exceed the
    result model's own limit and fail validation, parking the whole enrich job on one bad quote.
    """
    seen = locate(quote, sent)
    if seen is None:
        return None
    exact = sent[seen[0] : seen[1]]
    at = locate(exact, full)
    if at is None:
        return None
    span = full[at[0] : at[1]]
    if len(span) > MAX_QUOTE:
        return None
    return (span, at[0], at[1])


class ActionsEnricher(LLMEnricher):
    version = "1"
    skip_bulk = True
    config_model = GatedConfig
    prompt = ACTIONS
    output_model = ActionsResult

    def needs_model(self, msg: EnrichInput) -> bool:
        return not (self.skips_bulk() and bulk_reasons(msg))

    async def enrich(self, msg: EnrichInput) -> EnrichOutput:
        if self.skips_bulk() and bulk_reasons(msg):
            return EnrichOutput(ActionsResult(skipped="bulk").model_dump(mode="json"))
        body, truncated = fit(msg.body.unwrap(), self.budget(), self.tokenizer)
        answer = await self.access().generate_cached(
            ACTIONS,
            labeled(msg.subject, "subject"),
            UntrustedText(body, "body"),
            content_hash=digest(msg.subject.unwrap(), body),
            account_id=msg.account_id,
            message_id=msg.message_id,
            model=self.model_for(),
        )
        full = msg.body.unwrap()
        dropped = 0
        items: list[ActionItem] = []
        for a in answer.action_items:
            hit = verbatim(a.quote, body, full)
            if hit is None:
                dropped += 1
                continue
            quote, start, end = hit
            items.append(ActionItem(**{**a.model_dump(), "quote": quote}, start=start, end=end))
        deadlines: list[Deadline] = []
        for d in answer.deadlines:
            hit = verbatim(d.quote, body, full)
            if hit is None:
                dropped += 1
                continue
            quote, start, end = hit
            deadlines.append(Deadline(**{**d.model_dump(), "quote": quote}, start=start, end=end))
        return EnrichOutput(
            ActionsResult(
                action_items=items,
                deadlines=deadlines,
                dropped=dropped,
                truncated=truncated,
                injection_suspected=answer.injection_suspected,
            ).model_dump(mode="json")
        )
