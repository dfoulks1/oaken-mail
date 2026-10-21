# SPDX-License-Identifier: Apache-2.0
"""``CoreClient``: calls the registry directly, in process.

This is the one path every operation takes, so the rules cannot be skipped: the MCP exposure check,
input validation, the permission class, the confirm gate, the handler, then the audit row. The
HTTP layer wraps this later; the tests drive it as it is.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from oaken.core.errors import InvalidInput, NotExposed, SendDisabled
from oaken.core.logging import get_logger
from oaken.core.operations.caller import CallerContext
from oaken.core.operations.context import OpContext
from oaken.core.operations.exposure import ExposureStore
from oaken.core.operations.gate import (
    Binding,
    InMemoryProposals,
    Proposal,
    ProposalStore,
    hash_input,
)
from oaken.core.operations.limits import Limits
from oaken.core.operations.registry import AnyOperation, OperationRegistry, Preview
from oaken.core.operations.services import Services
from oaken.readstate.audit import new_audit_id

log = get_logger("oaken.operations")


@dataclass(frozen=True)
class CallResult:
    """Either the operation ran (``output``) or it needs confirming first (``proposal``)."""

    request_id: str
    output: BaseModel | None = None
    proposal: Proposal | None = None

    @property
    def executed(self) -> bool:
        return self.proposal is None


class CoreClient:
    def __init__(
        self,
        registry: OperationRegistry,
        services: Services,
        *,
        proposals: ProposalStore | None = None,
        exposure: ExposureStore | None = None,
    ) -> None:
        self.registry = registry
        self.svc = services
        cfg = services.settings.operations
        self.proposals: ProposalStore = proposals or InMemoryProposals(
            services.clock, cfg.confirm_ttl_seconds, cfg.max_pending_confirmations
        )
        self.exposure = exposure or ExposureStore(services.raw.db, services.clock, services.audit)

    def context(self, caller: CallerContext) -> OpContext:
        return OpContext(
            svc=self.svc,
            caller=caller,
            limits=Limits.for_caller(self.svc.settings.operations, caller),
            registry=self.registry,
            exposure=self.exposure,
        )

    async def visible(self, caller: CallerContext) -> list[AnyOperation]:
        """The operations this caller may call: all of them, except that an MCP caller sees only
        what is allowed by code and turned on by the user."""
        if caller.client != "mcp":
            return self.registry.all()
        chosen = await self.exposure.settings()
        return [op for op in self.registry.all() if self.exposure.effective(op, chosen)]

    async def call(
        self,
        name: str,
        payload: BaseModel | Mapping[str, Any] | None,
        caller: CallerContext,
        *,
        confirm: bool = False,
        confirmation_token: str | None = None,
    ) -> CallResult:
        """Run an operation.

        ``confirm=True`` is the CLI's or UI's way of saying the person asked for this exact action;
        it is ignored for an MCP caller, which can only confirm with the token from a proposal.
        A mutation that is not confirmed returns a ``Proposal`` and changes nothing.
        """
        op = self.registry.get(name)
        if caller.client == "mcp" and not await self.exposure.enabled(op):
            # The same words whether it never exists for MCP or is switched off: an agent learns
            # nothing about what the user has not offered.
            raise NotExposed(f"operation {name!r} is not available to MCP clients")
        inp = self._parse(op, payload)
        if op.permission == "send":
            raise SendDisabled("sending is disabled in this version")
        ctx = self.context(caller)

        if op.permission == "mutate":
            binding = Binding(op.name, hash_input(op.name, inp), caller.key)
            if confirmation_token:
                self.proposals.redeem(confirmation_token, binding)
            elif not (confirm and caller.can_confirm):
                return CallResult(
                    caller.request_id, proposal=await self._propose(op, ctx, inp, binding)
                )

        output = await op.handler(ctx, inp)
        if not isinstance(output, op.output):
            output = op.output.model_validate(output)
        if op.audit == "always" or (op.permission == "mutate" and op.audit == "registry"):
            await self._audit(op, inp, caller)
        log.info(
            "operation ran", operation=op.name, client=caller.client, actor=caller.actor.kind,
            request_id=caller.request_id,
        )  # fmt: skip
        return CallResult(caller.request_id, output=output)

    @staticmethod
    def _parse(op: AnyOperation, payload: BaseModel | Mapping[str, Any] | None) -> BaseModel:
        try:
            data = payload.model_dump() if isinstance(payload, BaseModel) else dict(payload or {})
            parsed: BaseModel = op.input.model_validate(data)
            return parsed
        except ValidationError as exc:
            # Where and what, never the value: an input can hold a search the user typed.
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}"
                for e in exc.errors(include_input=False, include_url=False)[:5]
            )
            raise InvalidInput(f"{op.name}: {problems}") from None

    async def _propose(
        self, op: AnyOperation, ctx: OpContext, inp: BaseModel, binding: Binding
    ) -> Proposal:
        preview = await op.preview(ctx, inp) if op.preview else Preview(summary=op.summary)
        token, expires = self.proposals.issue(binding)
        shown = self.svc.settings.operations.proposal_changes_shown
        return Proposal(
            operation=op.name,
            summary=preview.summary,
            changes=preview.changes[:shown],
            total_changes=max(preview.total_changes, len(preview.changes)),
            confirmation_token=token,
            expires_at=expires,
        )

    async def _audit(self, op: AnyOperation, inp: BaseModel, caller: CallerContext) -> None:
        """The registry's own row: who ran what, through which door. It holds a hash of the input
        and the request id, never the input (a search can hold what the user typed)."""
        audit_id = new_audit_id()
        async with self.svc.raw.db.transaction() as tx:
            await self.svc.audit.record(
                tx,
                audit_id=audit_id,
                actor=caller.actor,
                reason=f"{caller.client}_call",
                op=f"call:{op.name}",
                batch_id=caller.request_id,
                before={},
                after={
                    "request_id": caller.request_id,
                    "input_sha256": hash_input(op.name, inp),
                    **({"agent_id": caller.agent_id} if caller.agent_id else {}),
                },
            )
