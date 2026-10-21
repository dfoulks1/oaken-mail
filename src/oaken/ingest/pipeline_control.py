# SPDX-License-Identifier: Apache-2.0
"""The durable "pause the model" switch (prompt 17 CLI gap).

Ingest has two stages that call a model: ``ingest.embed`` and ``ingest.enrich``. A person who wants
to stop the machine learning from working the queue — to free the GPU, or while the laptop is on
battery — pauses them here. Mail keeps landing and being parsed and filtered (``ingest.parse`` is
CPU-only); only the model-bound stages stop being claimed. The flag lives in ``raw.db`` so it
survives a restart: the job runner loads it at start and the operations flip it, and the change is
audited in the same transaction.

This is a single-row table (``id = 0``). ``LLM_STAGES`` names the kinds a pause withholds from the
runner; keep it in step with the model-bound stages in ``oaken.ingest.pipeline``.
"""

from __future__ import annotations

from dataclasses import dataclass

from oaken.core.actors import Actor
from oaken.core.clock import Clock, fmt_ts
from oaken.core.db import Database
from oaken.readstate.audit import AuditLog, new_audit_id

# The model-bound ingest kinds a pause withholds. Mirrors pipeline.EMBED / pipeline.ENRICH.
LLM_STAGES: frozenset[str] = frozenset({"ingest.embed", "ingest.enrich"})


@dataclass(frozen=True)
class PipelineState:
    llm_paused: bool
    reason: str
    updated_at: str
    updated_by: str


class PipelineControl:
    """Reads and writes the one ``pipeline_control`` row, and answers which kinds are paused."""

    def __init__(self, db: Database, clock: Clock, audit: AuditLog) -> None:
        self.db, self.clock, self.audit = db, clock, audit

    async def state(self) -> PipelineState:
        row = await self.db.fetchone(
            "SELECT llm_paused, reason, updated_at, updated_by FROM pipeline_control WHERE id = 0"
        )
        if row is None:  # a store older than migration 0013, or not yet seeded: treat as running
            return PipelineState(False, "", "", "")
        return PipelineState(
            llm_paused=bool(row["llm_paused"]),
            reason=row["reason"] or "",
            updated_at=row["updated_at"] or "",
            updated_by=row["updated_by"] or "",
        )

    async def paused_kinds(self) -> frozenset[str]:
        """The job kinds the runner must not claim right now."""
        return LLM_STAGES if (await self.state()).llm_paused else frozenset()

    async def set_llm_paused(self, paused: bool, *, actor: Actor, reason: str) -> PipelineState:
        """Pause or resume the model-bound stages, auditing the change in the same transaction.
        A no-op change (already in the wanted state) still records the row so the reason updates."""
        now = self.clock.now()
        async with self.db.transaction() as tx:
            before = await tx.fetchone("SELECT llm_paused FROM pipeline_control WHERE id = 0")
            was = bool(before["llm_paused"]) if before else False
            await tx.execute(
                "UPDATE pipeline_control SET llm_paused = ?, reason = ?, updated_at = ?,"
                " updated_by = ? WHERE id = 0",
                (int(paused), reason, fmt_ts(now), actor.label or actor.kind),
            )
            audit_id = new_audit_id()
            await self.audit.record(
                tx,
                audit_id=audit_id,
                actor=actor,
                reason=reason or ("llm pause" if paused else "llm resume"),
                op=f"pipeline.{'pause' if paused else 'resume'}",
                batch_id=audit_id,
                before={"llm_paused": was},
                after={"llm_paused": paused},
            )
        return await self.state()
