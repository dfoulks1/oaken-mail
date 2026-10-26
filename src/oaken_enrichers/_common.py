# SPDX-License-Identifier: Apache-2.0
"""What the three model-backed enrichers share: the model handle, the token budget, and the
rule for mail that nobody wrote to the reader."""

import hashlib
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from oaken.core.errors import PluginError
from oaken.core.untrusted import UntrustedText
from oaken.mail.clean import bulk_signals
from oaken.mail.tokens import Tokenizer, load_tokenizer
from oaken.plugins.base import BaseEnricher
from oaken.plugins.contracts import EnrichInput, LLMAccess, PluginContext

CUT_MARKER = "\n[... the middle of this message was cut to fit the size limit ...]\n"


def header_values(msg: EnrichInput) -> dict[str, tuple[str, ...]]:
    """Every header as plain strings, for rules only (never passed to a model)."""
    return {k: tuple(v.unwrap() for v in vals) for k, vals in msg.raw_headers.items()}


def bulk_reasons(msg: EnrichInput) -> tuple[str, ...]:
    return bulk_signals(header_values(msg))


def digest(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode())
        h.update(b"\x00")
    return h.hexdigest()


def fit(text: str, budget: int, tokenizer: Tokenizer) -> tuple[str, bool]:
    """``text`` within ``budget`` tokens: kept whole, or as its head and tail with a marker between
    (the start says who and what, the end says what is asked). The flag says whether it was cut."""
    total = tokenizer.count(text)
    if total <= budget:
        return text, False
    keep = max(int(len(text) * budget / total * 0.9), 40)
    while True:
        head = text[: keep * 2 // 3]
        tail = text[len(text) - (keep - keep * 2 // 3) :]
        out = f"{head}{CUT_MARKER}{tail}"
        if tokenizer.count(out) <= budget or keep <= 40:
            return out, True
        keep = int(keep * 0.85)


class EnricherConfig(BaseModel):
    """The ``[plugins.<name>]`` settings every model-backed enricher shares. Each enricher may
    subclass this to add its own. ``model`` is declared but not yet honored per-enricher (every
    enricher still uses the one shared backend); that waits on the many-model LLM contract."""

    model_config = ConfigDict(extra="forbid")

    skip_bulk: bool | None = Field(
        default=None,
        description="Skip bulk/automated mail (newsletters, notifications) for this enricher, to "
        "save model calls. Leave unset to use the enricher's own default.",
    )
    model: str | None = Field(
        default=None,
        description="Which installed model this enricher runs on. Leave as the backend default "
        "unless you want this one enricher on a different model.",
    )


class GatedConfig(EnricherConfig):
    """For enrichers that triage's priority can gate (summary, actions)."""

    min_priority: int | None = Field(
        default=None,
        ge=0,
        le=3,
        description="Skip mail that triage rated below this (0 ignore, 1 low, 2 normal, 3 soon) "
        "to save model calls. Leave unset to run on everything. Needs the triage enricher on; "
        "mail triage has not rated yet is never skipped.",
    )


class LLMEnricher(BaseEnricher):
    """Base of the model-backed enrichers: ``self.llm`` and ``self.tokenizer`` after ``start``."""

    needs_llm = True
    skip_bulk: ClassVar[bool] = False  # a default, overridden by [plugins.<name>] skip_bulk
    config_model = EnricherConfig

    llm: LLMAccess
    tokenizer: Tokenizer

    async def start(self, ctx: PluginContext) -> None:
        await super().start(ctx)
        self.tokenizer = load_tokenizer(ctx.settings.mail)

    def access(self) -> LLMAccess:
        llm = self.ctx.llm
        if llm is None:
            raise PluginError(f"{self.ctx.name} needs a language model and none was provided")
        return llm

    def skips_bulk(self) -> bool:
        chosen = self.config.skip_bulk if self.config is not None else None
        return self.skip_bulk if chosen is None else chosen

    def model_for(self) -> str | None:
        """The model this enricher is configured to run, or None for the backend default. Validity
        is checked in the LLM service (which holds the backend); an unknown model falls back."""
        return self.config.model if self.config is not None else None

    def budget(self) -> int:
        return int(self.ctx.settings.ai.enrich_max_input_tokens)


def labeled(text: UntrustedText, label: str) -> UntrustedText:
    return UntrustedText(text.unwrap(), label)
