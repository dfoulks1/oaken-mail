# SPDX-License-Identifier: Apache-2.0
"""A real pipeline (fake embedder, in-memory vectors, fake model) with a ``Retrieval`` over it."""

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from oaken.ai import LLMCache, LLMService, Workspaces
from oaken.ai.access import WorkspaceLLM
from oaken.core.ids import MessageId
from oaken.core.search import Retrieval
from oaken.ingest.tests.conftest import Options, Rig, build
from oaken.plugins.contracts import LLMBackend, LLMRequest


@dataclass
class Box:
    rig: Rig
    retrieval: Retrieval
    count: int = 0

    async def add(
        self, subject: str, body: str, *, day: int = 1, sender: str = "alex"
    ) -> MessageId:
        """Store one message and return its message id (after sync and the pipeline have run)."""
        self.count += 1
        raw = (
            f"From: {sender.title()} <{sender}@example.com>\r\nTo: Sam <sam@example.org>\r\n"
            f"Subject: {subject}\r\nDate: {day:02d} Jan 2024 10:00:00 +0000\r\n"
            f"Message-ID: <s{self.count}@example.com>\r\n\r\n{body}\r\n"
        ).encode()
        pid = self.rig.provider.add_message(raw)
        await self.rig.run_sync()
        await self.rig.drain()
        return await self.rig.mid(pid)

    async def thread_of(self, mid: MessageId) -> str:
        doc = await self.rig.index.get_doc(mid)
        assert doc is not None
        return str(doc["thread_key"])

    async def enrich(self, mid: str, enricher: str, data: dict[str, Any]) -> None:
        await self.rig.raw_db.execute(
            "INSERT OR REPLACE INTO enrichments(message_id, enricher, version, model_id,"
            " content_hash, data_json, created_at) VALUES (?, ?, '1', 'test', 'h', ?, 0)",
            (mid, enricher, json.dumps(data)),
        )

    async def read(self, mid: str) -> None:
        await self.rig.raw_db.execute(
            "UPDATE message_state SET is_read = 1 WHERE message_id = ?", (mid,)
        )

    async def read_flags(self) -> dict[str, int]:
        rows = await self.rig.raw_db.fetchall("SELECT message_id, is_read FROM message_state")
        return {r["message_id"]: r["is_read"] for r in rows}


def pick(llm: LLMBackend) -> Callable[[str], LLMBackend]:
    return lambda _name: llm


def refuse(_request: LLMRequest) -> dict[str, Any]:
    return {"claims": [], "confidence": "low", "insufficient_evidence": True}


@pytest.fixture
async def box(tmp_path: Path) -> AsyncIterator[Box]:
    async for rig in build(tmp_path, Options(enrich=False)):
        rig.llm.responder = refuse
        llm = rig.llm
        service = LLMService(
            LLMCache(rig.raw_db, rig.clock), rig.usage, Workspaces(rig.pipeline.settings),
            pick(llm), rig.pipeline.settings,
        )  # fmt: skip
        yield Box(rig, Retrieval.from_pipeline(rig.pipeline, WorkspaceLLM(service, rig.raw)))
