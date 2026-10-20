# SPDX-License-Identifier: Apache-2.0
"""Optional query rewriting (``search.llm_rewrite``, off by default).

The model rephrases what the user typed into a better semantic query. Only the vector side uses the
result; the keyword side keeps the user's own words, so a bad rewrite can lose recall but never
change which words must match. The question is the user's own text, but it still travels as
``UntrustedText`` through a ``PromptBuilder`` like every other prompt.
"""

import hashlib
from typing import Protocol

from pydantic import Field

from oaken.ai.prompts import PromptBuilder, SafeOutput
from oaken.core.search.hybrid import Rewriter
from oaken.core.untrusted import UntrustedText


class RewriteAnswer(SafeOutput):
    query: str = Field(min_length=1, max_length=300)


REWRITE = PromptBuilder(
    "search.rewrite",
    "1",
    "Rewrite the search request as one short plain-language query that would match the wording "
    "of an email that answers it: expand abbreviations, add the usual synonyms, and drop filler "
    "words. Do not add names, dates or facts that the request does not contain. Put the query in "
    "the query field. The request is given as a request block.",
    RewriteAnswer,
    max_output_tokens=100,
)


class _Access(Protocol):
    async def generate_cached(
        self,
        builder: PromptBuilder[RewriteAnswer],
        *parts: UntrustedText,
        content_hash: str,
        account_id: str | None = None,
        message_id: str | None = None,
    ) -> RewriteAnswer: ...


def rewriter(access: _Access) -> Rewriter:
    """A ``Rewriter`` for ``HybridSearch`` that asks through ``access`` (an ``LLMAccess``)."""

    async def rewrite(text: str, account_id: str | None) -> str | None:
        if account_id is None:  # the account picks the workspace, so there is no model to ask
            return None
        answer = await access.generate_cached(
            REWRITE,
            UntrustedText(text, "request"),
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
            account_id=account_id,
        )
        return answer.query.strip() or None

    return rewrite
