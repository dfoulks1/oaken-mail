# SPDX-License-Identifier: Apache-2.0
"""The bulk-read guard: how many messages, ids and characters one call may move.

Mail text costs tokens, and an agent that asks for "everything" should get a bounded, honest
answer (``truncated`` says so) rather than a flood. The limits come from ``[operations]`` and an
MCP caller's are tighter.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from oaken.core.errors import InvalidInput
from oaken.core.operations.caller import CallerContext
from oaken.core.settings import OperationsSettings


@dataclass(frozen=True)
class Limits:
    max_results: int
    max_chars: int
    snippet_chars: int
    max_batch_ids: int
    max_summarize_ids: int
    max_raw_bytes: int

    @classmethod
    def for_caller(cls, cfg: OperationsSettings, caller: CallerContext) -> "Limits":
        mcp = caller.client == "mcp"
        return cls(
            max_results=cfg.mcp_max_results if mcp else cfg.max_results,
            max_chars=cfg.mcp_max_chars if mcp else cfg.max_chars,
            snippet_chars=cfg.snippet_chars,
            max_batch_ids=cfg.max_batch_ids,
            max_summarize_ids=cfg.max_summarize_ids,
            max_raw_bytes=cfg.max_raw_bytes,
        )

    def results(self, requested: int | None) -> int:
        """How many results to return: what was asked for, never more than the limit."""
        if requested is None:
            return self.max_results
        return max(1, min(requested, self.max_results))

    def ids(self, ids: Iterable[str], *, limit: int | None = None) -> list[str]:
        """The ids without repeats, in order. More than the batch limit is refused, not cut: a
        silent cut would make "mark these 300 read" do something other than what was asked."""
        unique = list(dict.fromkeys(ids))
        cap = limit if limit is not None else self.max_batch_ids
        if not unique:
            raise InvalidInput("give at least one message id")
        if len(unique) > cap:
            raise InvalidInput(f"at most {cap} messages per call; got {len(unique)}")
        return unique

    def budget(self) -> "CharBudget":
        return CharBudget(self.max_chars)


class CharBudget:
    """Characters of mail text one reply may hold. ``take`` cuts text to what is left."""

    def __init__(self, total: int) -> None:
        self.left = total
        self.truncated = False

    def take(self, text: str, per_item: int | None = None) -> str:
        cap = self.left if per_item is None else min(per_item, self.left)
        if len(text) > cap:
            self.truncated = True
            text = text[: max(cap, 0)]
        self.left -= len(text)
        return text
