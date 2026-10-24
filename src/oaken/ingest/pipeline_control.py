# SPDX-License-Identifier: Apache-2.0
"""The durable "pause the model" switch (prompt 17 CLI gap).

Ingest has two stages that call a model: ``ingest.embed`` and ``ingest.enrich``. A person who wants
to stop the machine learning from working the queue — to free the GPU, or while the laptop is on
battery — pauses them here. Mail keeps landing and being parsed and filtered (``ingest.parse`` is
CPU-only); only the model-bound stages stop being claimed. The flag lives in ``raw.db`` so it
survives a restart: the job runner loads it at start and the operations flip it, and the change is
audited in the same transaction.

``pipeline_control`` is a single row (``id = 0``) that pauses every model-bound stage at once.
Since prompt 30 one stage can also be paused alone (``pipeline_stage_pause``), and the number of
jobs a stage runs at once can be changed live (``pipeline_limits``). ``STAGES`` maps the stage
names a person uses to the job kinds; keep it in step with ``oaken.ingest.pipeline``.
"""

from __future__ import annotations

from dataclasses import dataclass

from oaken.core.actors import Actor
from oaken.core.clock import Clock, fmt_ts
from oaken.core.db import Database
from oaken.core.errors import InvalidInput
from oaken.readstate.audit import AuditLog, new_audit_id

# The model-bound ingest kinds a pause withholds. Mirrors pipeline.EMBED / pipeline.ENRICH.
LLM_STAGES: frozenset[str] = frozenset({"ingest.embed", "ingest.enrich"})
# The stage names a person uses, and the job kind each one runs.
STAGES: dict[str, str] = {"embed": "ingest.embed", "enrich": "ingest.enrich"}
MAX_CONCURRENCY = 64  # the same ceiling as ``[jobs] workers``


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
        kinds = set(LLM_STAGES) if (await self.state()).llm_paused else set()
        rows = await self.db.fetchall("SELECT kind FROM pipeline_stage_pause")
        return frozenset(kinds | {r["kind"] for r in rows})

    async def paused_stages(self) -> set[str]:
        """The stage names (``embed``, ``enrich``) that are paused, by either switch."""
        kinds = await self.paused_kinds()
        return {name for name, kind in STAGES.items() if kind in kinds}

    async def set_llm_paused(self, paused: bool, *, actor: Actor, reason: str) -> PipelineState:
        """Pause or resume every model-bound stage."""
        return await self.set_stage_paused(None, paused, actor=actor, reason=reason)

    async def set_stage_paused(
        self, stage: str | None, paused: bool, *, actor: Actor, reason: str
    ) -> PipelineState:
        """Pause or resume one stage (``embed``, ``enrich``), or with no stage every model-bound
        one. Resuming a single stage while everything is paused resumes just that stage. The change
        is audited in the same transaction; a no-op change still records the reason."""
        if stage is not None and stage not in STAGES:
            raise InvalidInput(f"unknown stage {stage!r}; one of {', '.join(STAGES)}")
        now = fmt_ts(self.clock.now())
        who = actor.label or actor.kind
        async with self.db.transaction() as tx:
            before = await tx.fetchone("SELECT llm_paused FROM pipeline_control WHERE id = 0")
            was_all = bool(before["llm_paused"]) if before else False
            was = sorted(
                r["kind"] for r in await tx.fetchall("SELECT kind FROM pipeline_stage_pause")
            )
            if stage is None:
                await tx.execute(
                    "UPDATE pipeline_control SET llm_paused = ?, reason = ?, updated_at = ?,"
                    " updated_by = ? WHERE id = 0",
                    (int(paused), reason, now, who),
                )
                if not paused:
                    await tx.execute("DELETE FROM pipeline_stage_pause")
            else:
                kind = STAGES[stage]
                if paused:
                    await tx.execute(
                        "INSERT OR REPLACE INTO pipeline_stage_pause"
                        " (kind, reason, updated_at, updated_by) VALUES (?, ?, ?, ?)",
                        (kind, reason, now, who),
                    )
                else:
                    await tx.execute("DELETE FROM pipeline_stage_pause WHERE kind = ?", (kind,))
                    if was_all:  # everything was paused: keep the other stages paused, one by one
                        await tx.execute(
                            "UPDATE pipeline_control SET llm_paused = 0, updated_at = ?,"
                            " updated_by = ? WHERE id = 0",
                            (now, who),
                        )
                        for other in STAGES.values():
                            if other != kind:
                                await tx.execute(
                                    "INSERT OR REPLACE INTO pipeline_stage_pause"
                                    " (kind, reason, updated_at, updated_by) VALUES (?, ?, ?, ?)",
                                    (other, reason, now, who),
                                )
            audit_id = new_audit_id()
            await self.audit.record(
                tx,
                audit_id=audit_id,
                actor=actor,
                reason=reason or ("pause" if paused else "resume"),
                op=f"pipeline.{'pause' if paused else 'resume'}",
                batch_id=audit_id,
                before={"llm_paused": was_all, "paused_kinds": was},
                after={"stage": stage or "all", "paused": paused},
            )
        return await self.state()

    async def limits(self) -> dict[str, int]:
        """Concurrency chosen at run time, by job kind. They outrank ``config.toml``."""
        rows = await self.db.fetchall("SELECT kind, concurrency FROM pipeline_limits")
        return {r["kind"]: int(r["concurrency"]) for r in rows}

    async def set_limit(self, stage: str, concurrency: int, *, actor: Actor) -> int:
        """How many jobs of one stage may run at once, from now on and after a restart."""
        if stage not in STAGES:
            raise InvalidInput(f"unknown stage {stage!r}; one of {', '.join(STAGES)}")
        if not 1 <= concurrency <= MAX_CONCURRENCY:
            raise InvalidInput(f"concurrency is between 1 and {MAX_CONCURRENCY}")
        kind = STAGES[stage]
        before = (await self.limits()).get(kind)
        async with self.db.transaction() as tx:
            await tx.execute(
                "INSERT OR REPLACE INTO pipeline_limits (kind, concurrency, updated_at, updated_by)"
                " VALUES (?, ?, ?, ?)",
                (kind, concurrency, fmt_ts(self.clock.now()), actor.label or actor.kind),
            )
            audit_id = new_audit_id()
            await self.audit.record(
                tx,
                audit_id=audit_id,
                actor=actor,
                reason="concurrency change",
                op="pipeline.limit",
                batch_id=audit_id,
                before={"stage": stage, "concurrency": before},
                after={"stage": stage, "concurrency": concurrency},
            )
        return concurrency
