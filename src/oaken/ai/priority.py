# SPDX-License-Identifier: Apache-2.0
"""Interactive model calls go first.

On one local model, a long background enrich run starves a question someone is waiting on. The
model service asks this gate before every call: an interactive call (``summarize_message``,
``ask``, anything a person or an MCP client started) is counted while it runs, and a background
call (the job runner's own, ``CALLER == "internal"``) waits until none is running before it starts
its next model call. It cannot cut short a background call already running, so an interactive call
waits for at most one of those. A background call also gives up waiting after ``yield_seconds``, so
a steady stream of questions can slow background work but never stop it for good.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator


class ModelGate:
    def __init__(self) -> None:
        self._active = 0
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def interactive_calls(self) -> int:
        """Interactive model calls running now."""
        return self._active

    @contextlib.asynccontextmanager
    async def interactive(self) -> AsyncIterator[None]:
        self._active += 1
        self._idle.clear()
        try:
            yield
        finally:
            self._active -= 1
            if self._active == 0:
                self._idle.set()

    async def background_turn(self, yield_seconds: float) -> None:
        """Wait up to ``yield_seconds`` for no interactive call to be running; ``0`` never waits."""
        if self._active == 0 or yield_seconds <= 0:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._idle.wait(), yield_seconds)
