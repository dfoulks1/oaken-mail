# SPDX-License-Identifier: Apache-2.0
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from oaken.ai import LLMCache, LLMService, Workspaces
from oaken.core.clock import FakeClock
from oaken.core.db import Database, migrate
from oaken.core.logging import get_logger
from oaken.core.settings import Settings
from oaken.core.untrusted import UntrustedText
from oaken.ingest.usage import UsageStore
from oaken.plugins.contracts import EnrichAttachment, EnrichInput, PluginContext
from oaken.testing.fakes import FakeLLM

MIGRATIONS = Path(__file__).resolve().parents[2] / "oaken" / "core" / "migrations"


class Direct:
    """``ctx.llm`` for a unit test: the real ``LLMService`` with a fake model, default workspace."""

    def __init__(self, service: LLMService) -> None:
        self.service = service

    async def generate_cached(
        self, builder: Any, *parts: UntrustedText, content_hash: str,
        account_id: str | None = None, message_id: str | None = None, model: str | None = None,
    ) -> Any:  # fmt: skip
        return await self.service.generate_cached(
            builder, *parts, content_hash=content_hash, account_id=account_id,  # type: ignore[arg-type]
            message_id=message_id, model=model,  # type: ignore[arg-type]
        )  # fmt: skip


@dataclass
class Rig:
    llm: FakeLLM
    access: Direct
    usage: UsageStore
    settings: Settings
    clock: FakeClock

    def ctx(self, name: str = "under-test", config: dict[str, Any] | None = None) -> PluginContext:
        return PluginContext(
            name=name, kind="enricher", settings=self.settings, clock=self.clock,
            log=get_logger("test"), config=config or {}, llm=self.access,
        )  # fmt: skip

    async def start(self, plugin: Any, config: dict[str, Any] | None = None) -> Any:
        await plugin.start(self.ctx(config=config))
        return plugin


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    clock = FakeClock()
    settings = Settings()
    async with await Database.open(tmp_path / "raw.db", read_pool_size=1) as db:
        await migrate(db, MIGRATIONS)
        llm, usage = FakeLLM(model_id="local-model"), UsageStore(db, clock)
        service = LLMService(
            LLMCache(db, clock), usage, Workspaces(settings), lambda name: llm, settings
        )
        yield Rig(llm, Direct(service), usage, settings, clock)


def headers(**values: str | tuple[str, ...]) -> dict[str, tuple[UntrustedText, ...]]:
    """Headers for an ``EnrichInput``; ``from_`` stands for ``from``, ``_`` for ``-``."""
    out: dict[str, tuple[UntrustedText, ...]] = {}
    for key, value in values.items():
        vals = (value,) if isinstance(value, str) else value
        out[key.rstrip("_").replace("_", "-").lower()] = tuple(UntrustedText(v) for v in vals)
    return out


def message(
    body: str = "Please send the report by Friday.",
    subject: str = "Report",
    *,
    sender: str = "Alex <alex@example.com>",
    date: datetime | None = datetime(2024, 1, 1, 10, 0, tzinfo=UTC),
    attachments: tuple[EnrichAttachment, ...] = (),
    parent_summary: str | None = None,
    **raw: str | tuple[str, ...],
) -> EnrichInput:
    return EnrichInput(
        message_id="m1",
        account_id="main",
        subject=UntrustedText(subject),
        body=UntrustedText(body),
        headers={"from": UntrustedText(sender)},
        raw_headers=headers(from_=sender, **raw),
        attachments=attachments,
        date=date,
        parent_summary=UntrustedText(parent_summary) if parent_summary else None,
    )


def with_llm(cls: type) -> Any:
    """A subclass of an enricher class that is started with a language model, for contract tests
    (which start a plugin the way the host does, with a bare context)."""

    class Started(cls):  # type: ignore[misc]
        async def start(self, ctx: PluginContext) -> None:
            class Stub:
                async def generate_cached(self, builder: Any, *parts: Any, **kw: Any) -> Any:
                    return builder.output.model_validate(_FILL.get(builder.task, {}))

            await super().start(replace(ctx, llm=Stub()))

    return Started


_FILL: dict[str, dict[str, Any]] = {
    "enrich.summary": {"summary": "A summary.", "key_points": ["one"]},
    "enrich.triage": {
        "category": "work",
        "priority": 1,
        "needs_reply": False,
        "waiting_on_others": False,
        "reason": "ok",
    },  # fmt: skip
    "enrich.actions": {"action_items": [], "deadlines": []},
}
