# SPDX-License-Identifier: Apache-2.0
"""What the three model-backed enrichers share: the model handle, the token budget, and the
rule for mail that nobody wrote to the reader."""

import hashlib
from typing import ClassVar

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


class LLMEnricher(BaseEnricher):
    """Base of the model-backed enrichers: ``self.llm`` and ``self.tokenizer`` after ``start``."""

    needs_llm = True
    skip_bulk: ClassVar[bool] = False  # a default, overridden by [plugins.<name>] skip_bulk

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
        return bool(self.ctx.config.get("skip_bulk", self.skip_bulk))

    def budget(self) -> int:
        return int(self.ctx.settings.ai.enrich_max_input_tokens)


def labeled(text: UntrustedText, label: str) -> UntrustedText:
    return UntrustedText(text.unwrap(), label)
