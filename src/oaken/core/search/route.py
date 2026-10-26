# SPDX-License-Identifier: Apache-2.0
"""Working out what an ``ask`` is asking for.

A small model reads only the question, never mail, and picks an intent from a menu: the core ones
(``find``, ``qa``) plus one for each installed enricher that declares an ``AskIntent``. Its system
prompt is application text: a fixed template and that menu, which comes from plugin code checked
when it loaded. The question travels as ``UntrustedText`` in the user turn, so a question that
tries to give orders can still only choose a name from the menu.

If the model is missing, slow or wrong, plain words decide, and failing that the request is treated
as a lookup (a question) or a listing. Routing never raises.
"""

import hashlib
import re
from collections.abc import Mapping
from typing import Any, Final

from pydantic import Field

from oaken.ai.prompts import PromptBuilder, SafeOutput
from oaken.core.logging import get_logger
from oaken.core.untrusted import UntrustedText
from oaken.plugins.contracts import AskIntent

log = get_logger("oaken.ask")

FIND: Final = AskIntent("find", "The reader wants to see or list messages, with no analysis.")
QA: Final = AskIntent("qa", "A factual question about what the mail says, answered with quotes.")
CORE_INTENTS: Final = (FIND, QA)


class Route(SafeOutput):
    intent: str = Field(max_length=32)
    sender: str | None = Field(default=None, max_length=100)
    days: int | None = Field(default=None, ge=1, le=3650)
    unread: bool | None = None
    query: str = Field(default="", max_length=200)


ROUTE = PromptBuilder(
    "ask.route",
    "1",
    "Decide what the reader is asking their mail client to do. The question is given as a "
    "question block. Set intent to one of the names listed below. Use qa for a factual question "
    "about what their mail says, and find when they only want to see messages. Set sender only if "
    "the question names a person or address. Set days only if it names a period: today and "
    "yesterday are 1, last week is 7, last month is 30. Set unread to true only if it asks about "
    "unread mail. Set query to the key words to search for, or leave it empty to mean recent mail.",
    Route,
    max_output_tokens=120,
)

# Plain words that settle an intent when the model cannot, checked in this order.
_WORDS: Final = (
    ("summary", re.compile(r"\b(summar\w*|gist|tl;?dr|recap|brief me)\b", re.I)),
    ("actions", re.compile(r"\b(to-?dos?|action items?|tasks?|deadlines?|due)\b", re.I)),
    ("inspector", re.compile(r"\b(spam|phish\w*|suspicious|authentic\w*|spoof\w*)\b", re.I)),
    (
        "triage",
        re.compile(r"\b(urgent|important|priorit\w*|attention|need[s]? me|reply|respond)\b", re.I),
    ),
)
_QUESTION: Final = re.compile(
    r"\?\s*$|^\s*(what|who|when|where|why|how|which|did|does|do|is|are|was|were|can|could)\b", re.I
)
_DAYS: Final = (
    (re.compile(r"\b(today|yesterday)\b", re.I), 1),
    (re.compile(r"\b(this|last|past) week\b", re.I), 7),
    (re.compile(r"\b(this|last|past) month\b", re.I), 30),
)


def catalog(enrichers: Mapping[str, Any]) -> list[AskIntent]:
    """The menu: the core intents, then one per started enricher that declares one."""
    items = list(CORE_INTENTS)
    seen = {i.name for i in items}
    for _, enricher in sorted(enrichers.items()):
        intent = getattr(enricher, "ask_intent", None)
        if isinstance(intent, AskIntent) and intent.name not in seen:
            items.append(intent)
            seen.add(intent.name)
    return items


def sources(enrichers: Mapping[str, Any]) -> dict[str, tuple[str, AskIntent]]:
    """``{intent name: (enricher name, its AskIntent)}`` for the intents enrichers declare."""
    out: dict[str, tuple[str, AskIntent]] = {}
    for name, enricher in sorted(enrichers.items()):
        intent = getattr(enricher, "ask_intent", None)
        if isinstance(intent, AskIntent) and intent.name not in out:
            out[intent.name] = (name, intent)
    return out


def rule_route(question: str, menu: list[AskIntent]) -> Route:
    """The intent from plain words, for when no model can be asked."""
    names = {i.name for i in menu}
    intent = next((n for n, rx in _WORDS if n in names and rx.search(question)), None)
    if intent is None:
        intent = "qa" if _QUESTION.search(question) else "find"
    days = next((d for rx, d in _DAYS if rx.search(question)), None)
    return Route(
        intent=intent,
        days=days,
        unread=True if re.search(r"\bunread\b", question, re.I) else None,
        query="",
    )


async def route(llm: Any, question: str, menu: list[AskIntent]) -> Route:
    """Ask the model; on any failure or an intent that is not on the menu, use the plain words."""
    builder = ROUTE.extended([(i.name, i.description) for i in menu])
    try:
        found = await llm.generate_cached(
            builder,
            UntrustedText(question, "question"),
            content_hash=hashlib.sha256(question.encode()).hexdigest(),
        )
    except Exception as exc:  # noqa: BLE001 - routing must never fail an ask
        log.warning("ask router failed; using plain words", error=type(exc).__name__)
        return rule_route(question, menu)
    if found.intent not in {i.name for i in menu}:
        log.warning("ask router chose an unknown intent; using plain words")
        return rule_route(question, menu)
    return found  # type: ignore[no-any-return]
