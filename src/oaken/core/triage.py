# SPDX-License-Identifier: Apache-2.0
"""Triage as a core concept (ADR 0017): what a verdict is and what a level is.

A triager rates a message: a category, a level (``priority``) and whether a reply is owed. Core,
not any plugin, says what that shape is, because core reads it: search filters on it, a job's
urgency follows the level, an enricher's ``min_priority`` bars on it, and ``route`` names the actions
a message goes to (ADR 0019). Plugins produce a verdict of this shape; nothing here imports one.
"""

from typing import Literal

from pydantic import BaseModel, Field

Category = Literal[
    "personal", "work", "transactional", "notification", "newsletter", "automated", "other"
]
MAX_LEVEL = 99  # 0 to 3 are built in; a reader's own levels start at 4


def urgency(level: object) -> int:
    """How much sooner than the rest a job runs for a message of this level: 3 (level 0) down to 0.
    An unrated message (None, or not a number) gets none."""
    return max(0, 3 - level) if isinstance(level, int) and not isinstance(level, bool) else 0


class TriageResult(BaseModel):
    """A triager's verdict, as core stores and reads it."""

    category: Category
    priority: int = Field(ge=0, le=MAX_LEVEL)
    needs_reply: bool
    waiting_on_others: bool
    reason: str = ""
    source: Literal["rules", "custom", "llm"]
    rule: str = ""  # which rule decided, when source is "rules" or "custom"
    truncated: bool = False
    injection_suspected: bool = False
    # The actions this message goes to, by name (ADR 0019). Empty: it goes to none.
    route: list[str] = Field(default_factory=list)
