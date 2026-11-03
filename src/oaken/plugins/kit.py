# SPDX-License-Identifier: Apache-2.0
"""Small helpers the first-party plugins share, whatever their kind: reading a cleaned message's
headers, fitting text to a token budget, labelling untrusted text, and the model handle a plugin
that asks a model keeps (``self.llm``, ``self.tokenizer``). Neutral between kinds (ADR 0017): an
enricher and a triager both import from here, and neither imports the other's package."""

import hashlib
from typing import Any, ClassVar

from oaken.ai.prompts import PromptBuilder
from oaken.core.errors import PluginError
from oaken.core.untrusted import UntrustedText
from oaken.mail.clean import bulk_signals
from oaken.mail.tokens import Tokenizer, load_tokenizer
from oaken.plugins.contracts import CleanMessage, LLMAccess, PluginContext

CUT_MARKER = "\n[... the middle of this message was cut to fit the size limit ...]\n"


def header_values(msg: CleanMessage) -> dict[str, tuple[str, ...]]:
    """Every header as plain strings, for rules only (never passed to a model)."""
    return {k: tuple(v.unwrap() for v in vals) for k, vals in msg.raw_headers.items()}


def bulk_reasons(msg: CleanMessage) -> tuple[str, ...]:
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


def labeled(text: UntrustedText, label: str) -> UntrustedText:
    return UntrustedText(text.unwrap(), label)


class ModelHandle:
    """What a plugin that asks a model keeps: ``self.llm`` and ``self.tokenizer`` after ``start``,
    the model it is configured to run, and its input budget. Mix in before the plugin base."""

    # The instructions and answer shape the model gets. An external agent (the swarm) is handed
    # the same, so it answers what a native model would.
    prompt: ClassVar[PromptBuilder[Any] | None] = None

    ctx: PluginContext
    config: Any
    llm: LLMAccess
    tokenizer: Tokenizer

    async def start(self, ctx: PluginContext) -> None:
        await super().start(ctx)  # type: ignore[misc]
        self.tokenizer = load_tokenizer(ctx.settings.mail)

    def access(self) -> LLMAccess:
        llm = self.ctx.llm
        if llm is None:
            raise PluginError(f"{self.ctx.name} needs a language model and none was provided")
        return llm

    def model_for(self) -> str | None:
        """The model this plugin is configured to run, or None for the backend default. Validity is
        checked in the LLM service (which holds the backend); an unknown model falls back."""
        return self.config.model if self.config is not None else None

    def budget(self) -> int:
        return int(self.ctx.settings.ai.enrich_max_input_tokens)
