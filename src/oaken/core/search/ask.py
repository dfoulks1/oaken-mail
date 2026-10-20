# SPDX-License-Identifier: Apache-2.0
"""Cited question answering.

The model answers in *claims*, each with the excerpt it rests on and the exact words it quotes.
Core keeps a claim only if its excerpt was really in the context and its quote really is in that
excerpt's stored text. What survives becomes the answer; a claim that does not is dropped and
counted, and an answer with nothing left says so instead of guessing. The evidence is mail, so it
travels only as ``UntrustedText``, and nothing the model says is executed or followed.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, Field, field_validator

from oaken.ai.prompts import PromptBuilder, SafeOutput
from oaken.core.search.context import Evidence
from oaken.core.search.quotes import verify
from oaken.core.untrusted import UntrustedText

MAX_CLAIMS: Final = 12
MAX_QUOTE: Final = 400
NOT_ENOUGH: Final = "There is not enough in your mail to answer that."
Confidence = Literal["high", "medium", "low"]


class ClaimAnswer(BaseModel):
    text: str = Field(min_length=1, max_length=600)
    ref: str = Field(max_length=40)
    quote: str = Field(max_length=MAX_QUOTE)


class AskAnswer(SafeOutput):
    claims: list[ClaimAnswer] = Field(default_factory=list, max_length=MAX_CLAIMS)
    confidence: Confidence = "low"
    insufficient_evidence: bool = False

    @field_validator("claims", mode="before")
    @classmethod
    def _cap(cls, v: Any) -> Any:
        return v[:MAX_CLAIMS] if isinstance(v, list) else v


ASK = PromptBuilder(
    "ask",
    "1",
    "Answer the question using only the evidence, which is excerpts of the reader's own email. "
    "The evidence comes as one block per message. A block starts with a line like [M1] giving "
    "the date, sender and subject, may have a summary line and thread lines (for orientation "
    "only: never cite them), and has excerpts labelled [M1.2], [M1.3] and so on. Write the answer "
    "as claims, each one plain sentence that the evidence supports. For every claim give ref (the "
    "excerpt label, such as M1.2) and quote (the exact words from that excerpt that support the "
    "claim, copied character for character, at most 200 characters). Never write a claim you "
    "cannot support with a quote. Do not use anything you know from outside the evidence and do "
    "not guess. If the evidence does not answer the question, set insufficient_evidence to true "
    "and return no claims. confidence is high when an excerpt states the answer directly, medium "
    "when you combined several excerpts, and low when the evidence is partial or indirect. The "
    "question is given as a question block and the evidence as one evidence block per message.",
    AskAnswer,
    max_output_tokens=800,
)


@dataclass(frozen=True, slots=True)
class Citation:
    claim: int  # 1-based position of the claim this supports
    message_id: str
    chunk_ordinal: int
    quote: str  # as it stands in the message
    start: int | None  # offsets in the chunk's text; None if only a normalized form matched
    end: int | None
    subject: str
    sender: str
    date: datetime | None


@dataclass(frozen=True, slots=True)
class AskResult:
    answer: str
    citations: tuple[Citation, ...]
    confidence: Confidence
    insufficient_evidence: bool
    dropped_claims: int = 0  # claims the model made that failed verification
    injection_suspected: bool = False
    degraded: bool = False  # the vector side of retrieval was unavailable
    degraded_reason: str = ""
    warnings: tuple[str, ...] = ()
    evidence_tokens: int = 0
    full_body_tokens: int = 0
    hits: int = 0
    references: tuple[str, ...] = field(default=())  # cited message ids, in order, to open

    @property
    def tokens_saved(self) -> int:
        return max(0, self.full_body_tokens - self.evidence_tokens)


def parts(question: str, evidence: Evidence) -> tuple[UntrustedText, ...]:
    """The question and one evidence block per message, each fenced as mail."""
    return (
        UntrustedText(question, "question"),
        *(UntrustedText(b, "evidence") for b in evidence.blocks),
    )


def content_hash(question: str, evidence: Evidence) -> str:
    h = hashlib.sha256(question.encode())
    for b in evidence.blocks:
        h.update(b"\x00" + b.encode())
    return h.hexdigest()


def verify_answer(answer: AskAnswer, evidence: Evidence) -> tuple[list[tuple[str, Citation]], int]:
    """The claims whose citation checks out as ``(text, citation)``, and how many did not."""
    kept: list[tuple[str, Citation]] = []
    dropped = 0
    for claim in answer.claims:
        found = evidence.excerpt(claim.ref.strip().strip("[]").upper())
        proof = verify(claim.quote, found[1].full_text) if found else None
        if found is None or proof is None:
            dropped += 1
            continue
        msg, ex = found
        quote, start, end = proof
        kept.append(
            (
                claim.text.strip(),
                Citation(
                    claim=len(kept) + 1,
                    message_id=msg.message_id,
                    chunk_ordinal=ex.ordinal,
                    quote=quote,
                    start=start,
                    end=end,
                    subject=msg.subject,
                    sender=msg.sender,
                    date=msg.date,
                ),
            )
        )
    return kept, dropped


def resolve(answer: AskAnswer, evidence: Evidence) -> AskResult:
    """What the model said, held to what the evidence shows."""
    kept, dropped = verify_answer(answer, evidence)
    if answer.insufficient_evidence or not kept:
        return AskResult(
            NOT_ENOUGH,
            (),
            "low",
            True,
            dropped_claims=dropped,
            injection_suspected=answer.injection_suspected,
            evidence_tokens=evidence.tokens_used,
            full_body_tokens=evidence.tokens_full_bodies,
            hits=evidence.hits_included,
        )
    text = " ".join(f"{t} [{c.claim}]" for t, c in kept)
    cited = tuple(dict.fromkeys(c.message_id for _, c in kept))
    return AskResult(
        answer=text,
        citations=tuple(c for _, c in kept),
        # a model that made an unverifiable claim is not trusted to rate itself
        confidence="low" if dropped else answer.confidence,
        insufficient_evidence=False,
        dropped_claims=dropped,
        injection_suspected=answer.injection_suspected,
        evidence_tokens=evidence.tokens_used,
        full_body_tokens=evidence.tokens_full_bodies,
        hits=evidence.hits_included,
        references=cited,
    )
